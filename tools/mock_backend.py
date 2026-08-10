#!/usr/bin/env python3
"""Backend GIẢ, kiểm hợp đồng NGHIÊM NGẶT — để chạy end-to-end simulator mà không cần backend thật.

Tương đương `iot/tools/mock-backend/mock_backend.py` bên repo firmware. Điểm mấu chốt: nó **cố ý
khắt khe hơn ASP.NET Core**. Backend thật bind JSON không phân biệt hoa/thường và bỏ qua trường
lạ, nên nó che mất chỗ payload sai; mock này từ chối thẳng để lỗi lộ ra ngay tại chỗ.

Chạy:
    python3 tools/mock_backend.py --port 4001
    python3 tools/mock_backend.py --port 4001 --offer-version 1.2.0   # để thử OTA đầy đủ

Rồi trỏ simulator vào nó:
    IOT_BASE_URL=http://localhost:4001 python -m src.main --no-dashboard

Endpoint mô phỏng:
    POST /api/iot-devices/provision
    POST /api/iot-devices/heartbeat
    POST /api/sensor-readings/batch
    POST /api/ambient/readings/batch
    POST /api/environmental-incidents
    GET  /api/iot-devices/firmware-check?currentVersion=...
    PUT  /api/iot-devices/firmware-update-log/{id}
    GET  /firmware/demo.bin        (artifact OTA sinh tại chỗ)
    GET  /healthz  ·  GET /         (tóm tắt những gì đã nhận)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Đúng dạng mà `net::isoNow` của firmware phát ra; cho phép phần mili-giây do
# `patchItemTimestamp` thêm vào.
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?Z$")

# Dải vật lý mà backend thật dùng để đếm outlier (>50/giờ → tự Decommission thiết bị).
VOLTAGE_RANGE = (0.0, 1000.0)          # (0, 1000] — mở ở cận dưới
TEMPERATURE_RANGE = (-50.0, 150.0)
CURRENT_ABS_MAX = 1000.0
PERCENT_RANGE = (0.0, 100.0)

VALID_SOURCE_TYPES = {1, 2, 3}
VALID_CHARGING_STATES = {1, 2, 3, 4, 5}
VALID_INCIDENT_TYPES = {1, 2, 3, 4, 5, 99}
VALID_SEVERITIES = {1, 2, 3}
VALID_AMBIENT_SOURCES = {1, 2}

# Artifact OTA sinh tại chỗ để thử trọn vòng tải + xác minh SHA-256.
FIRMWARE_BLOB = b"MOCK-FIRMWARE-IMAGE\n" * 512
FIRMWARE_SHA256 = hashlib.sha256(FIRMWARE_BLOB).hexdigest()


class State:
    def __init__(self, offer_version: str = "", mqtt: dict | None = None):
        self.lock = threading.Lock()
        self.offer_version = offer_version
        # Sáu trường MQTT trả về trong provision response. `host` rỗng = backend TẮT MQTT.
        self.mqtt = mqtt or {"host": "", "port": 0, "tls": False, "prefix": "",
                             "username": "", "password": ""}
        self.readings = 0
        self.skipped = 0
        self.batches = 0
        self.heartbeats = 0
        self.provisions = 0
        self.ambient = 0
        self.incidents = 0
        self.update_logs: list[dict] = []
        self.seen_idempotency: dict[str, dict] = {}
        self.errors: list[str] = []
        # Mã thiết bị → phiên bản firmware đang chạy (theo heartbeat/provision báo lên).
        self.device_versions: dict[str, str] = {}


def _iso_ok(value) -> bool:
    return isinstance(value, str) and bool(ISO_RE.match(value))


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Handler(BaseHTTPRequestHandler):
    state: State = State()
    protocol_version = "HTTP/1.1"

    # ── tiện ích ──────────────────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):        # bớt ồn, tự in dòng gọn hơn ở dưới
        return

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "__INVALID__"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, status: int, data=None, message: str = "OK") -> None:
        self._send(status, {"isSuccess": True, "statusCode": status, "message": message,
                            "data": data})

    def _bad(self, status: int, message: str) -> None:
        with self.state.lock:
            self.state.errors.append(f"{self.command} {self.path} → {status}: {message}")
        print(f"  ✗ {self.command} {self.path} → {status}: {message}", flush=True)
        self._send(status, {"isSuccess": False, "statusCode": status, "message": message,
                            "data": None})

    def _check_auth(self, need_device_code: bool = True) -> str | None:
        """Trả `device_code` nếu hợp lệ, None nếu đã gửi lỗi."""
        api_key = self.headers.get("X-Api-Key")
        if not api_key:
            self._bad(401, "thiếu header X-Api-Key")
            return None
        device_code = self.headers.get("X-Device-Code") or ""
        if need_device_code and not device_code:
            self._bad(401, "thiếu header X-Device-Code (contract Sprint 3)")
            return None
        if self.headers.get("Accept") != "application/json":
            self._bad(400, "thiếu header Accept: application/json")
            return None
        return device_code or "(legacy)"

    # ── GET ───────────────────────────────────────────────────────────────────────────────
    def do_GET(self):                                    # noqa: N802 - API của BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if parsed.path == "/firmware/demo.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FIRMWARE_BLOB)))
            self.end_headers()
            self.wfile.write(FIRMWARE_BLOB)
            print(f"  ↓ tải firmware ({len(FIRMWARE_BLOB)} byte)", flush=True)
            return
        if parsed.path == "/api/iot-devices/firmware-check":
            device_code = self._check_auth()
            if device_code is None:
                return
            current = (parse_qs(parsed.query).get("currentVersion") or [""])[0]
            if not current:
                self._bad(400, "thiếu tham số currentVersion")
                return
            offer = self.state.offer_version
            if not offer or offer == current:
                self._ok(200, {"updateAvailable": False, "targetVersion": current})
                return
            host = self.headers.get("Host") or "localhost"
            self._ok(200, {
                "updateAvailable": True,
                "targetVersion": offer,
                "downloadUrl": f"http://{host}/firmware/demo.bin",
                "sha256Checksum": FIRMWARE_SHA256,
                "updateLogId": f"log-{device_code}-{offer}",
                "artifactSizeBytes": len(FIRMWARE_BLOB),
            })
            return
        if parsed.path == "/":
            self._send(200, self._summary())
            return
        self._bad(404, "route không tồn tại")

    def _summary(self) -> dict:
        s = self.state
        with s.lock:
            return {
                "provisions": s.provisions, "heartbeats": s.heartbeats, "batches": s.batches,
                "readings": s.readings, "skipped": s.skipped, "ambient": s.ambient,
                "incidents": s.incidents, "updateLogs": s.update_logs[-10:],
                "deviceVersions": s.device_versions, "errors": s.errors[-20:],
            }

    # ── PUT ───────────────────────────────────────────────────────────────────────────────
    def do_PUT(self):                                    # noqa: N802
        if not self.path.startswith("/api/iot-devices/firmware-update-log/"):
            self._bad(404, "route không tồn tại")
            return
        if self._check_auth() is None:
            return
        log_id = self.path.rsplit("/", 1)[-1]
        body = self._read_json()
        if body == "__INVALID__" or not isinstance(body, dict):
            self._bad(400, "body không phải JSON object")
            return
        status = body.get("status")
        if not isinstance(status, int) or status not in range(1, 8):
            self._bad(400, f"status không hợp lệ: {status!r} (mong đợi 1..7)")
            return
        reason = body.get("failureReason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            self._bad(400, "failureReason phải là chuỗi ≤ 500 ký tự")
            return
        with self.state.lock:
            self.state.update_logs.append({"logId": log_id, "status": status,
                                           "bytesDownloaded": body.get("bytesDownloaded"),
                                           "failureReason": reason})
        print(f"  · update-log {log_id} status={status}", flush=True)
        self._ok(200, {"updateLogId": log_id, "status": status})

    # ── POST ──────────────────────────────────────────────────────────────────────────────
    def do_POST(self):                                   # noqa: N802
        routes = {
            "/api/iot-devices/provision": self._provision,
            "/api/iot-devices/heartbeat": self._heartbeat,
            "/api/sensor-readings/batch": self._ingest,
            "/api/ambient/readings/batch": self._ambient,
            "/api/environmental-incidents": self._incident,
        }
        handler = routes.get(urlparse(self.path).path)
        if handler is None:
            self._bad(404, "route không tồn tại")
            return
        body = self._read_json()
        if body == "__INVALID__":
            self._bad(400, "body không phải JSON hợp lệ")
            return
        handler(body)

    # ── provision ─────────────────────────────────────────────────────────────────────────
    def _provision(self, body) -> None:
        device_code = self._check_auth()
        if device_code is None:
            return
        if not isinstance(body, dict):
            self._bad(400, "body rỗng")
            return
        for field in ("FirmwareVersion", "HardwareRevision", "DeviceTimestamp"):
            if field not in body:
                self._bad(400, f"thiếu trường {field}")
                return
        if not _iso_ok(body["DeviceTimestamp"]):
            self._bad(400, f"DeviceTimestamp sai định dạng: {body['DeviceTimestamp']!r} "
                           "(mong đợi 2026-06-13T08:15:42Z)")
            return

        with self.state.lock:
            self.state.provisions += 1
            self.state.device_versions[device_code] = body["FirmwareVersion"]
        print(f"  ✓ provision {device_code} fw={body['FirmwareVersion']}", flush=True)

        self._ok(200, {
            "deviceId": "44444444-4444-4444-4444-444444444444",
            "deviceCode": device_code,
            "siteId": "b6d83be5-050c-47a0-9f73-3160f517be80",
            "pollingIntervalSeconds": 5,
            "heartbeatIntervalSeconds": 60,
            "ntpServer": "vn.pool.ntp.org",
            # Sáu trường MQTT — `mqttBrokerHost` rỗng nghĩa là backend TẮT MQTT (mặc định).
            # Bật bằng `--mqtt-host ... --mqtt-user ... --mqtt-pass ...`.
            "mqttBrokerHost": self.state.mqtt["host"],
            "mqttBrokerPort": self.state.mqtt["port"],
            "mqttUseTls": self.state.mqtt["tls"],
            "mqttTopicPrefix": self.state.mqtt["prefix"] or f"solar/{device_code.lower()}",
            "mqttUsername": self.state.mqtt["username"] or device_code.lower(),
            "mqttPassword": self.state.mqtt["password"],
            # Bảng ánh xạ pin — thiết bị PHẢI dùng đúng tập này.
            "batteryMappings": [
                {"batteryAssetSerial": "BAT-2026-001", "unitId": 1,
                 "sensorSourceCode": "primary"},
                {"batteryAssetSerial": "BAT-2026-002", "unitId": 2,
                 "sensorSourceCode": "primary"},
            ],
            "supportedSensors": ["bms", "ina226", "ds18b20", "sht31", "mq2", "water_leak"],
        })

    # ── heartbeat ─────────────────────────────────────────────────────────────────────────
    def _heartbeat(self, body) -> None:
        device_code = self._check_auth()
        if device_code is None:
            return
        if not isinstance(body, dict):
            self._bad(400, "body rỗng")
            return
        if not _iso_ok(body.get("DeviceTimestamp")):
            self._bad(400, f"DeviceTimestamp sai định dạng: {body.get('DeviceTimestamp')!r}")
            return
        # `MemoryUsageMb` / `LocalQueueDepth` / `UptimeSeconds` là kiểu NGUYÊN ở backend.
        for field in ("MemoryUsageMb", "LocalQueueDepth", "UptimeSeconds"):
            value = body.get(field)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                self._bad(400, f"{field} phải là số nguyên, nhận {value!r}")
                return

        skew_seconds = 0.0
        try:
            device_time = datetime.strptime(body["DeviceTimestamp"].split(".")[0] + "Z",
                                            "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            skew_seconds = abs((datetime.now(timezone.utc) - device_time).total_seconds())
        except ValueError:
            pass

        with self.state.lock:
            self.state.heartbeats += 1
            if body.get("FirmwareVersion"):
                self.state.device_versions[device_code] = body["FirmwareVersion"]
        print(f"  ✓ heartbeat {device_code} queue={body.get('LocalQueueDepth')} "
              f"mem={body.get('MemoryUsageMb')}MB rssi={body.get('SignalStrengthDbm')}",
              flush=True)

        self._ok(200, {"clockSkewWarning": skew_seconds > 300,
                       "clockSkewSeconds": round(skew_seconds, 1)})

    # ── ingest ────────────────────────────────────────────────────────────────────────────
    def _ingest(self, body) -> None:
        device_code = self._check_auth(need_device_code=False)
        if device_code is None:
            return
        if not isinstance(body, dict) or "items" not in body:
            self._bad(400, "body phải có mảng `items`")
            return
        items = body["items"]
        if not isinstance(items, list) or not items:
            self._bad(400, "`items` phải là mảng không rỗng")
            return

        idem = self.headers.get("Idempotency-Key")
        if idem:
            with self.state.lock:
                cached = self.state.seen_idempotency.get(idem)
            if cached is not None:
                print(f"  ↺ trùng Idempotency-Key {idem[:8]}… → trả lại kết quả cũ", flush=True)
                self._ok(200, cached, message="duplicate")
                return

        inserted = 0
        skipped = 0
        seen_keys: set[tuple] = set()
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                self._bad(400, f"items[{idx}] không phải object")
                return
            problem = self._validate_reading(item, idx)
            if problem == "__FATAL__":
                return
            if problem:
                skipped += 1
                continue
            key = (item.get("batteryAssetSerial") or item.get("batteryAssetId"), item["time"])
            if key in seen_keys:
                # Khoá chính hypertable là (Time, BatteryAssetId) — trùng trong CÙNG batch là lỗi
                # thật của thiết bị (thiếu vá mili-giây), phải nói to.
                self._bad(400, f"items[{idx}] trùng khoá chính (Time, BatteryAsset): {key}")
                return
            seen_keys.add(key)
            inserted += 1

        with self.state.lock:
            self.state.batches += 1
            self.state.readings += inserted
            self.state.skipped += skipped
            data = {"totalReceived": len(items), "inserted": inserted, "skipped": skipped}
            if idem:
                self.state.seen_idempotency[idem] = data

        flag = "" if skipped == 0 else f"  ⚠ bỏ {skipped}"
        print(f"  ✓ ingest {device_code} {inserted}/{len(items)} reading{flag}", flush=True)
        self._ok(201, data)

    def _validate_reading(self, item: dict, idx: int) -> str:
        """Trả "" nếu hợp lệ, mô tả nếu bị BỎ (outlier), "__FATAL__" nếu đã gửi 400."""
        if not (item.get("batteryAssetSerial") or item.get("batteryAssetId")):
            self._bad(400, f"items[{idx}] thiếu batteryAssetSerial/batteryAssetId")
            return "__FATAL__"
        if not _iso_ok(item.get("time")):
            self._bad(400, f"items[{idx}].time sai định dạng: {item.get('time')!r}")
            return "__FATAL__"
        if "deviceTimestamp" in item and not _iso_ok(item["deviceTimestamp"]):
            self._bad(400, f"items[{idx}].deviceTimestamp sai định dạng")
            return "__FATAL__"
        for field in ("voltage", "current", "temperature", "socPercent"):
            if not _num(item.get(field)):
                self._bad(400, f"items[{idx}].{field} phải là số, nhận {item.get(field)!r}")
                return "__FATAL__"
        if "sourceType" in item and item["sourceType"] not in VALID_SOURCE_TYPES:
            self._bad(400, f"items[{idx}].sourceType không hợp lệ: {item['sourceType']!r}")
            return "__FATAL__"
        if "chargingState" in item and item["chargingState"] not in VALID_CHARGING_STATES:
            self._bad(400, f"items[{idx}].chargingState không hợp lệ")
            return "__FATAL__"
        if "sensorSourceCode" in item and len(str(item["sensorSourceCode"])) > 20:
            self._bad(400, f"items[{idx}].sensorSourceCode > 20 ký tự")
            return "__FATAL__"
        if "bmsErrorCode" in item and len(str(item["bmsErrorCode"])) > 64:
            self._bad(400, f"items[{idx}].bmsErrorCode > 64 ký tự")
            return "__FATAL__"

        # Ngoài dải vật lý → BỎ (không phải 400) — đúng như backend thật đếm outlier.
        v = item["voltage"]
        if not (VOLTAGE_RANGE[0] < v <= VOLTAGE_RANGE[1]):
            return f"voltage {v} ngoài dải"
        t = item["temperature"]
        if not (TEMPERATURE_RANGE[0] <= t <= TEMPERATURE_RANGE[1]):
            return f"temperature {t} ngoài dải"
        if abs(item["current"]) > CURRENT_ABS_MAX:
            return f"current {item['current']} ngoài dải"
        for field in ("socPercent", "sohPercent"):
            if field in item and _num(item[field]):
                if not (PERCENT_RANGE[0] <= item[field] <= PERCENT_RANGE[1]):
                    return f"{field} {item[field]} ngoài dải"
        return ""

    # ── ambient ───────────────────────────────────────────────────────────────────────────
    def _ambient(self, body) -> None:
        if self._check_auth(need_device_code=False) is None:
            return
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            self._bad(400, "body phải có mảng `items`")
            return
        for idx, item in enumerate(body["items"]):
            if not isinstance(item, dict):
                self._bad(400, f"items[{idx}] không phải object")
                return
            if not item.get("siteId"):
                self._bad(400, f"items[{idx}].siteId là bắt buộc (Guid)")
                return
            if not _iso_ok(item.get("time")):
                self._bad(400, f"items[{idx}].time sai định dạng")
                return
            if not _num(item.get("ambientTemperature")):
                self._bad(400, f"items[{idx}].ambientTemperature phải là số")
                return
            if item.get("source") not in VALID_AMBIENT_SOURCES:
                self._bad(400, f"items[{idx}].source phải là SỐ 1|2 "
                               f"(nhận {item.get('source')!r})")
                return
        with self.state.lock:
            self.state.ambient += len(body["items"])
        print(f"  ✓ ambient {len(body['items'])} mẫu", flush=True)
        self._ok(201, {"inserted": len(body["items"])})

    # ── environmental incident ────────────────────────────────────────────────────────────
    def _incident(self, body) -> None:
        if self._check_auth(need_device_code=False) is None:
            return
        if not isinstance(body, dict):
            self._bad(400, "body rỗng")
            return
        if not body.get("siteId"):
            self._bad(400, "siteId là bắt buộc (Guid)")
            return
        if body.get("incidentType") not in VALID_INCIDENT_TYPES:
            self._bad(400, f"incidentType không hợp lệ: {body.get('incidentType')!r}")
            return
        if body.get("severity") not in VALID_SEVERITIES:
            self._bad(400, f"severity không hợp lệ: {body.get('severity')!r}")
            return
        if not _iso_ok(body.get("detectedAt")):
            self._bad(400, f"detectedAt sai định dạng: {body.get('detectedAt')!r}")
            return
        try:
            detected = datetime.strptime(body["detectedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            if detected > datetime.now(timezone.utc) + timedelta(minutes=5):
                self._bad(400, "detectedAt ở tương lai quá 5 phút")
                return
        except ValueError:
            pass
        if len(str(body.get("notes", ""))) > 1000:
            self._bad(400, "notes > 1000 ký tự")
            return
        if len(str(body.get("reportedBy", ""))) > 256:
            self._bad(400, "reportedBy > 256 ký tự")
            return

        with self.state.lock:
            self.state.incidents += 1
        print(f"  ✓ sự cố type={body['incidentType']} severity={body['severity']} "
              f"detectedAt={body['detectedAt']}", flush=True)
        self._ok(201, {"incidentId": "55555555-5555-5555-5555-555555555555"})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--offer-version", default="",
                   help="phiên bản firmware để offer OTA (vd 1.2.0). Bỏ trống = không offer.")
    p.add_argument("--mqtt-host", default="",
                   help="broker cấp cho thiết bị qua provision. Bỏ trống = backend TẮT MQTT.")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--mqtt-tls", action="store_true")
    p.add_argument("--mqtt-prefix", default="", help="mặc định solar/<mã thiết bị chữ thường>")
    p.add_argument("--mqtt-user", default="", help="mặc định = mã thiết bị chữ thường")
    p.add_argument("--mqtt-pass", default="")
    args = p.parse_args()

    Handler.state = State(offer_version=args.offer_version, mqtt={
        "host": args.mqtt_host, "port": args.mqtt_port if args.mqtt_host else 0,
        "tls": args.mqtt_tls, "prefix": args.mqtt_prefix,
        "username": args.mqtt_user, "password": args.mqtt_pass,
    })
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock backend nghe tại http://{args.host}:{args.port}")
    if args.offer_version:
        print(f"  OTA: offer version {args.offer_version} "
              f"(sha256 {FIRMWARE_SHA256[:16]}…, {len(FIRMWARE_BLOB)} byte)")
    if args.mqtt_host:
        print(f"  MQTT: cấp broker {args.mqtt_host}:{args.mqtt_port} qua provision")
    else:
        print("  MQTT: TẮT (provision trả mqttBrokerHost rỗng) → thiết bị chạy HTTPS-only")
    print("  GET /  → tóm tắt những gì đã nhận\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDừng.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
