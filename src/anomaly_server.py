"""HTTP server CỤC BỘ cho bảng điều khiển bộ case anomaly (`tools/anomaly.html`).

Mục đích: bấm chạy TỪNG case từ trình duyệt thay vì gõ `--case N` cho mỗi lần, và xem ngay
cảnh báo/ticket sinh ra — không phải mở psql ở cửa sổ khác.

Mọi đường gửi đều gọi LẠI `AnomalyRunner` của `src/anomaly.py`, nên payload ra dây giống hệt
lúc chạy CLI. Server này KHÔNG có nhánh gửi riêng — nếu nó khác CLI thì tức là bug.

    GET  /api/cases                 → danh sách case + metadata dataset
    GET  /api/state                 → alert/ticket/breach hiện có (đọc qua psql trong container)
    POST /api/run     {ids:[1,2]}   → chạy các case, trả kết quả từng case
    POST /api/reset   {scope:...}   → dọn alert/ticket/incident để chạy lại
    GET  /                          → phục vụ chính `tools/anomaly.html`

Chỉ bind 127.0.0.1 — đây là công cụ demo, không phải dịch vụ.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .anomaly import (SINGLE_SHOT_KINDS, AnomalyRunner, CaseResult, _load_dataset, _new_result,
                      _resolve_conflicts)
from .config import load_config

log = logging.getLogger("iot-sim.anomaly-server")

PANEL_HTML = Path(__file__).resolve().parent.parent / "tools" / "anomaly.html"

# Chạy psql TRONG container postgres — máy chủ demo không cần cài client.
PSQL = ["docker", "exec", "solar-postgres", "psql", "-U", "postgres", "-t", "-A", "-F", "\x1f"]


def _psql(db: str, sql: str, timeout: int = 20) -> list[list[str]]:
    """Trả về danh sách dòng đã tách cột. Lỗi thì trả rỗng — bảng điều khiển không được chết
    chỉ vì DB tạm thời không với tới."""
    try:
        out = subprocess.run(PSQL + ["-d", db, "-c", sql], capture_output=True, text=True,
                             timeout=timeout)
    except Exception as exc:  # docker chưa chạy, container đổi tên…
        log.warning("psql lỗi: %s", exc)
        return []
    if out.returncode != 0:
        log.warning("psql rc=%s: %s", out.returncode, out.stderr.strip()[:200])
        return []
    return [ln.split("\x1f") for ln in out.stdout.strip().splitlines() if ln.strip()]


class AnomalyPanel:
    """Giữ dataset + một `AnomalyRunner` dùng lại giữa các lần bấm.

    Dùng lại runner để giữ cache provision (tránh đâm vào giới hạn tần suất của backend, đã
    gặp HTTP 429 khi provision lại mỗi lần bấm).
    """

    def __init__(self, dataset_path: str, seed_path: str | None = None):
        self.dataset_path = dataset_path
        self.seed_path = seed_path
        self.dataset = _load_dataset(dataset_path)
        self.sim_cfg = load_config(seed_path)
        self._runner: AnomalyRunner | None = None
        self._lock = threading.Lock()          # chạy case là thao tác có trạng thái ⇒ tuần tự

    def runner(self) -> AnomalyRunner:
        if self._runner is None:
            self._runner = AnomalyRunner(self.sim_cfg, self.dataset, dry_run=False)
        return self._runner

    # ── /api/cases ─────────────────────────────────────────────────────────────────────────
    def cases_payload(self) -> dict:
        meta = self.dataset.get("meta", {}) or {}
        defaults = self.dataset.get("defaults", {}) or {}
        out = []
        for c in self.dataset.get("cases", []) or []:
            kind = str(c.get("kind") or "sensor_reading")
            reading = c.get("reading") or {}
            out.append({
                "id": c.get("id"),
                "anomaly": c.get("anomaly"),
                "severity": c.get("severity"),
                "kind": kind,
                "title": c.get("title", ""),
                "note": (c.get("note") or "").strip(),
                "requires": (c.get("requires") or "").strip(),
                "battery": c.get("battery"),
                "reading": reading or None,
                "readings": c.get("readings"),
                "ambient": c.get("ambient"),
                "incident": c.get("incident"),
                "repeat": int(c.get("repeat") or defaults.get("repeat") or 1),
                "bypass_noise": bool(c.get("bypass_noise")),
                "dangerous": bool(c.get("dangerous")),
                "conflicts_with": c.get("conflicts_with"),
                "expect_no_anomaly": bool(c.get("expect_no_anomaly")),
                # Case Critical mới sinh ticket — Warning chỉ có notification.
                "makes_ticket": str(c.get("severity") or "") == "Critical",
                "single_shot": kind in SINGLE_SHOT_KINDS,
            })
        return {"meta": meta, "defaults": defaults, "cases": out}

    # ── /api/state ─────────────────────────────────────────────────────────────────────────
    def state_payload(self) -> dict:
        alerts = _psql("battery_db", """
            SELECT COALESCE(ba.serial_number,'(site)'), a.anomaly_type, a.severity,
                   a.threshold_value, a.actual_value, COALESCE(a.unit,''), a.status,
                   to_char(a.detected_at AT TIME ZONE 'UTC','HH24:MI:SS')
            FROM alerts a LEFT JOIN battery_assets ba ON ba.id = a.battery_asset_id
            WHERE NOT a.is_deleted AND a.detected_at > now() - interval '2 hours'
            ORDER BY a.detected_at DESC LIMIT 100;""")
        breaches = _psql("battery_db", """
            SELECT ba.serial_number, n.anomaly_type, count(*)::text
            FROM noise_breach_events n JOIN battery_assets ba ON ba.id = n.battery_asset_id
            WHERE n.time > now() - interval '2 hours'
            GROUP BY 1,2 ORDER BY 3 DESC;""")
        incidents = _psql("battery_db", """
            SELECT incident_type::text, severity::text, status::text,
                   to_char(detected_at AT TIME ZONE 'UTC','HH24:MI:SS')
            FROM environmental_incidents
            WHERE detected_at > now() - interval '2 hours'
            ORDER BY detected_at DESC LIMIT 50;""")
        tickets = _psql("ticket_db", """
            SELECT t.code, t.title, t.status::text, COALESCE(t.priority::text,'-'),
                   to_char(t.created_at AT TIME ZONE 'UTC','HH24:MI:SS'),
                   (SELECT count(*)::text FROM ticket_ai_suggestions s WHERE s.ticket_id = t.id)
            FROM tickets t WHERE NOT t.is_deleted
            ORDER BY t.created_at DESC LIMIT 50;""")
        sagas = _psql("ticket_db", """
            SELECT current_state, count(*)::text FROM alert_ticket_saga_states
            GROUP BY 1 ORDER BY 2 DESC;""")
        return {
            "at": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "alerts": [{"battery": r[0], "type": r[1], "severity": r[2], "threshold": r[3],
                        "actual": r[4], "unit": r[5], "status": r[6], "at": r[7]}
                       for r in alerts if len(r) >= 8],
            "breaches": [{"battery": r[0], "type": r[1], "count": int(r[2])}
                         for r in breaches if len(r) >= 3],
            "incidents": [{"type": r[0], "severity": r[1], "status": r[2], "at": r[3]}
                          for r in incidents if len(r) >= 4],
            "tickets": [{"code": r[0], "title": r[1], "status": r[2], "priority": r[3],
                         "at": r[4], "ai_suggestions": int(r[5] or 0)}
                        for r in tickets if len(r) >= 6],
            "sagas": [{"state": r[0], "count": int(r[1])} for r in sagas if len(r) >= 2],
        }

    # ── /api/run ───────────────────────────────────────────────────────────────────────────
    def run_cases(self, ids: list[int], include_dangerous: bool = False,
                  dry_run: bool = False) -> dict:
        """Chạy các case đã chọn. Giữ nguyên luật hai đợt của CLI: case cần vượt ngưỡng chống
        nhiễu phải gửi đợt 1, CHỜ `wave_gap_s`, rồi gửi đợt 2 — gộp một lượt thì backend đếm
        `effectiveCount = 1` và không cảnh báo nào nổ."""
        with self._lock:
            all_cases = self.dataset.get("cases", []) or []
            by_id = {int(c["id"]): c for c in all_cases if c.get("id") is not None}
            chosen = [by_id[i] for i in ids if i in by_id]
            missing = [i for i in ids if i not in by_id]

            blocked = [c for c in chosen if c.get("dangerous") and not include_dangerous]
            chosen = [c for c in chosen if c not in blocked]
            chosen, conflicting = _resolve_conflicts(chosen)

            runner = self.runner()
            runner.dry_run = dry_run
            meta = self.dataset.get("meta", {}) or {}
            gap = float(meta.get("wave_gap_s", 14))

            results: list[tuple[dict, CaseResult]] = []
            pending: list[tuple[dict, CaseResult, list[int]]] = []

            runner.rebase_clock()
            for c in chosen:
                r = _new_result(c)
                kind = str(c.get("kind") or "sensor_reading")
                if kind == "manual":
                    r.status = "MANUAL"
                    r.detail = (c.get("instructions") or "chạy tay").strip()
                elif kind in SINGLE_SHOT_KINDS:
                    runner.run_case_into(c, r)
                elif runner.precheck(c, r):
                    # Nền trước số đo gây lỗi — xem `send_warmup`. Panel luôn bật vì đây là
                    # đường dùng để demo, chỗ mà bảng bằng chứng được nhìn kỹ nhất.
                    runner.send_warmup(c, r)
                    waves = runner.waves_for(c)
                    if runner.send_wave(c, r, waves[0]) and len(waves) > 1:
                        pending.append((c, r, waves))
                results.append((c, r))

            waited = 0.0
            if pending and not dry_run:
                import time as _t
                _t.sleep(gap)
                waited = gap
                runner.rebase_clock()
                for c, r, waves in pending:
                    runner.send_wave(c, r, waves[1])

            return {
                "ran": [self._result_dict(c, r) for c, r in results],
                "waves_waited_s": waited,
                "blocked_dangerous": [int(c["id"]) for c in blocked],
                "conflicting": [{"id": int(c["id"]), "conflicts_with": int(o)}
                                for c, o in conflicting],
                "unknown_ids": missing,
            }

    @staticmethod
    def _result_dict(case: dict, r: CaseResult) -> dict:
        return {
            "id": r.case_id, "anomaly": r.anomaly, "severity": r.severity,
            "status": r.status, "sent": r.sent, "inserted": r.inserted,
            "skipped": r.skipped, "http_codes": sorted(set(r.http_codes)),
            "detail": r.detail, "title": case.get("title", ""),
        }

    # ── /api/warmup ────────────────────────────────────────────────────────────────────────
    def warmup(self, count: int = 30) -> dict:
        """Bơm `count` số đo BÌNH THƯỜNG để job AI có đủ cửa sổ mà chạy.

        `SohPredictionBackgroundService` gom đúng 30 dòng mỗi lượt (`AiOptions.WindowSize` — hằng
        số nướng vào trọng số model, `predict.py` từ chối mọi payload khác 30). Một case anomaly
        chỉ gửi 1–7 số đo nên chưa bao giờ chạm ngưỡng đó: job `continue`, bỏ qua pin, và ticket
        sinh ra không có `ticket_ai_suggestions` nào. Đây là lý do phần AI trông như không hoạt
        động, chứ không phải nó hỏng.

        Mọi giá trị dưới đây nằm GIỮA dải an toàn của LiFePO4 24V 30Ah (20–29,2V · −10…60°C ·
        SOC > 20 · SOH > 85) nên không luật ngưỡng nào nổ — nếu chúng vượt ngưỡng thì bước
        chuẩn bị này lại tự đẻ alert rác và làm hỏng chính case sắp chạy.
        """
        with self._lock:
            runner = self.runner()
            runner.dry_run = False
            serial = str(self.defaults.get("battery") or "")
            if not serial:
                for c in self.dataset.get("cases", []) or []:
                    if c.get("battery"):
                        serial = str(c["battery"])
                        break
            if not serial:
                return {"ok": False, "detail": "không tìm thấy pin nào trong dataset"}

            case = {
                "id": 0,
                "anomaly": "warmup",
                "severity": None,
                "kind": "sensor_reading",
                "battery": serial,
                "reading": {
                    "voltage": 25.6, "current": 2.0, "temperature": 30.0,
                    "soc_percent": 60, "soh_percent": 90,
                    "cycle_count": 300, "charging_state": 1,
                },
            }
            r = _new_result(case)
            runner.rebase_clock()
            if not runner.precheck(case, r):
                return {"ok": False, "detail": r.detail, "serial": serial}
            runner.send_wave(case, r, count)
            return {
                "ok": r.status == "OK",
                "serial": serial,
                "sent": r.sent,
                "inserted": r.inserted,
                "http_codes": sorted(set(r.http_codes)),
                "detail": r.detail,
            }

    # ── /api/reset ─────────────────────────────────────────────────────────────────────────
    def reset(self, scope: str) -> dict:
        """Dọn theo ĐÚNG ba luật khử trùng khác nhau của backend — xem `anomaly verify`."""
        done = []
        if scope in ("alerts", "all"):
            _psql("battery_db", "DELETE FROM alerts WHERE detected_at > now() - interval '24 hours';")
            _psql("battery_db", "DELETE FROM noise_breach_events WHERE time > now() - interval '24 hours';")
            done.append("alerts + noise_breach_events")
        if scope in ("incidents", "all"):
            # Đóng thay vì xoá: dedup sự cố không có cửa sổ thời gian, còn Open/Ack là bị reuse.
            _psql("battery_db", "UPDATE environmental_incidents SET status = 3, resolved_at = now() WHERE status IN (1,2);")
            done.append("environmental_incidents → Resolved")
        if scope in ("tickets", "all"):
            _psql("ticket_db", """
                TRUNCATE TABLE ticket_ai_suggestions, ticket_kb_references, ticket_activities,
                  ticket_assignments, ticket_battery_assets, ticket_participants,
                  ticket_attachments, ticket_audit_logs, sla_pause_events, sla_timers,
                  alert_ticket_saga_states, tickets RESTART IDENTITY CASCADE;""")
            done.append("tickets + saga states")
        return {"reset": done, "scope": scope}


def make_handler(panel: AnomalyPanel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self) -> None:
            if not PANEL_HTML.exists():
                self._send_json(404, {"error": f"không tìm thấy {PANEL_HTML}"})
                return
            body = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/anomaly.html"):
                return self._send_html()
            if path == "/api/cases":
                return self._send_json(200, panel.cases_payload())
            if path == "/api/state":
                return self._send_json(200, panel.state_payload())
            return self._send_json(404, {"error": "route không tồn tại"})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            body = self._body()
            if path == "/api/run":
                ids = [int(x) for x in (body.get("ids") or [])]
                if not ids:
                    return self._send_json(400, {"error": "thiếu `ids`"})
                try:
                    return self._send_json(200, panel.run_cases(
                        ids,
                        include_dangerous=bool(body.get("include_dangerous")),
                        dry_run=bool(body.get("dry_run"))))
                except Exception as exc:
                    log.exception("chạy case lỗi")
                    return self._send_json(500, {"error": str(exc)})
            if path == "/api/reset":
                scope = str(body.get("scope") or "alerts")
                if scope not in ("alerts", "incidents", "tickets", "all"):
                    return self._send_json(400, {"error": f"scope lạ: {scope}"})
                return self._send_json(200, panel.reset(scope))
            return self._send_json(404, {"error": "route không tồn tại"})

    return Handler


def serve(dataset_path: str, seed_path: str | None = None,
          host: str = "127.0.0.1", port: int = 8099) -> None:
    panel = AnomalyPanel(dataset_path, seed_path)
    server = ThreadingHTTPServer((host, port), make_handler(panel))
    n = len(panel.dataset.get("cases", []) or [])
    print(f"Bảng điều khiển bộ case  →  http://{host}:{port}")
    print(f"Backend                  →  {panel.sim_cfg.backend.base_url}")
    print(f"Dataset                  →  {dataset_path}  ({n} case)")
    print("Ctrl-C để dừng.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nđã dừng.")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="python -m src.anomaly_server")
    ap.add_argument("--dataset", default="config/anomaly-dataset.yaml")
    ap.add_argument("--seed", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--log-level", default="WARNING")
    a = ap.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s — %(message)s")
    serve(a.dataset, a.seed, a.host, a.port)
