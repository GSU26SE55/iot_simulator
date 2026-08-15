"""HTTP control server CỤC BỘ — cho phép chỉnh thông số simulator ĐANG CHẠY từ trình duyệt.

Không phải contract của backend/firmware thật — đây là tiện ích RIÊNG của simulator để người
vận hành đổi scenario / polling interval mà không cần MQTT broker. Tái dùng đúng đường xử lý
lệnh `SimulatedDevice._on_mqtt_command()` nên hành vi giống hệt lệnh downlink MQTT thật.

Chỉ bind 127.0.0.1 — không expose ra mạng.

    GET  /devices                  → danh sách thiết bị + state hiện tại (JSON)
    POST /devices/<code>/command   → { "type": "set_scenario", "params": {...} } → ack
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger("iot-sim.control")


def _state_to_dict(device) -> dict:
    s = asdict(device.state)
    s["led"] = device.state.led.name
    s["device_code"] = device.cfg.device_code
    return s


def make_handler(devices_by_code: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/devices":
                self._send(200, {"devices": [_state_to_dict(d) for d in devices_by_code.values()]})
                return
            self._send(404, {"error": "route không tồn tại"})

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "devices" or parts[2] != "command":
                self._send(404, {"error": "route không tồn tại"})
                return
            code = parts[1]
            device = devices_by_code.get(code)
            if device is None:
                self._send(404, {"error": f"không có thiết bị {code!r}"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "body không phải JSON hợp lệ"})
                return
            if not isinstance(body, dict) or "type" not in body:
                self._send(400, {"error": "body cần có trường 'type'"})
                return

            cmd_id = body.get("cmdId") or str(uuid.uuid4())
            payload = {"cmdId": cmd_id, "type": body["type"], "params": body.get("params", {})}
            before = device.state.cmd_ack_ok + device.state.cmd_ack_failed
            device._on_mqtt_command(payload)
            acked_ok = device.state.cmd_ack_ok + device.state.cmd_ack_failed > before and \
                device.state.cmd_ack_failed == 0
            log.info("control-server: %s ← %s", code, payload)
            self._send(200, {"cmdId": cmd_id, "sent": True, "state": _state_to_dict(device)})

    return Handler


def start_control_server(devices: list, host: str = "127.0.0.1", port: int = 8787):
    """Khởi động server trong thread daemon riêng. Trả về (server, thread)."""
    devices_by_code = {d.cfg.device_code: d for d in devices}
    handler_cls = make_handler(devices_by_code)
    server = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, name="control-server", daemon=True)
    thread.start()
    log.info("control server nghe tại http://%s:%d (chỉ local)", host, port)
    return server, thread
