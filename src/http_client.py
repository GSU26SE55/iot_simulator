"""HTTPS client — mirror `firmware-esp32/src/net/http_client.cpp`.

Header gửi kèm MỌI request (khớp firmware `httpPostJson` / `httpGetJsonRecv` / `httpPutJson`):
    X-Api-Key      : <apiKey runtime>
    X-Device-Code  : <deviceCode runtime>       ← S2-FW-04
    Accept         : application/json
    Content-Type   : application/json           ← chỉ với POST/PUT có body
    Idempotency-Key: <uuidv4>                   ← CHỈ endpoint ingest (S3-FW-02)

Endpoint (route backend THẬT — không có `/v1/`, đã verify với controller):
    POST /api/sensor-readings/batch
    POST /api/ambient/readings/batch
    POST /api/environmental-incidents
    POST /api/iot-devices/provision
    POST /api/iot-devices/heartbeat
    GET  /api/iot-devices/firmware-check?currentVersion=...
    PUT  /api/iot-devices/firmware-update-log/{updateLogId}
    GET  <downloadUrl>                          ← tải artifact OTA (KHÔNG kèm header auth,
                                                   giống firmware `downloadAndFlash`)

Contract `current` (legacy Sprint 1) chỉ gửi `X-Api-Key` — backend đời đó chưa biết
`X-Device-Code` và chưa có provision/heartbeat/firmware-check.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import requests
import urllib3

from .config import CONTRACT_IOT2

log = logging.getLogger("iot-sim.http")

# Firmware cắt response để in log; giữ cùng ý tưởng để `responseSnippet` hai bên so được.
RESPONSE_SNIPPET_CHARS = 500


@dataclass
class HttpResult:
    """`net::PostResult` — thêm `json` vì Python parse sẵn được."""

    ok: bool
    status_code: int
    body: str
    json: Any = None
    duration_ms: int = 0


class IotHttpClient:
    def __init__(self, base_url: str, device_code: str, api_key: str, *,
                 tls_verify: bool = True, firmware_version: str = "1.0.0-sim",
                 contract_version: str = "current", timeout_s: float = 15.0,
                 on_result: Callable[[int], None] | None = None):
        self.base_url = base_url.rstrip("/")
        self.device_code = device_code
        self.api_key = api_key
        self.firmware_version = firmware_version
        self.contract_version = contract_version
        self.timeout_s = float(timeout_s)
        # Callback báo cho `LinkState` biết có với tới backend không (code 0 = lỗi truyền tải).
        self._on_result = on_result

        self.session = requests.Session()
        self.session.verify = tls_verify
        if not tls_verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._apply_headers()

    # ── identity hot-reload (mirror identity::setApiKey/setDeviceCode) ──────────────────────
    def _apply_headers(self) -> None:
        headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": f"iot-simulator/{self.firmware_version} ({self.device_code})",
        }
        if self.contract_version == CONTRACT_IOT2:
            headers["X-Device-Code"] = self.device_code
        self.session.headers.update(headers)

    def set_identity(self, device_code: str | None = None, api_key: str | None = None) -> None:
        if device_code:
            self.device_code = device_code
        if api_key:
            self.api_key = api_key
        self._apply_headers()

    # ── Provision (§52.3) ──────────────────────────────────────────────────────────────────
    def provision(self, hardware_revision: str, device_timestamp_iso: str) -> HttpResult:
        body = {
            "FirmwareVersion": self.firmware_version,
            "HardwareRevision": hardware_revision,
            "DeviceTimestamp": device_timestamp_iso,
        }
        return self.post("/api/iot-devices/provision", body)

    # ── Heartbeat (§52.4) ──────────────────────────────────────────────────────────────────
    def heartbeat(self, body: dict) -> HttpResult:
        return self.post("/api/iot-devices/heartbeat", body)

    # ── Sensor ingest ──────────────────────────────────────────────────────────────────────
    def ingest(self, payload: dict, idempotency_key: str | None) -> HttpResult:
        extra = {}
        if self.contract_version == CONTRACT_IOT2 and idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        return self.post("/api/sensor-readings/batch", payload, extra_headers=extra)

    # ── Ambient (SHT31) ────────────────────────────────────────────────────────────────────
    def ambient_ingest(self, payload: dict, idempotency_key: str | None = None) -> HttpResult:
        extra = {}
        if self.contract_version == CONTRACT_IOT2 and idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        return self.post("/api/ambient/readings/batch", payload, extra_headers=extra)

    # ── Environmental incident (MQ-2 / rò nước) ────────────────────────────────────────────
    def environmental_incident(self, payload: dict) -> HttpResult:
        return self.post("/api/environmental-incidents", payload)

    # ── OTA (S7-FW-01/02) ──────────────────────────────────────────────────────────────────
    def firmware_check(self, current_version: str) -> HttpResult:
        return self.get("/api/iot-devices/firmware-check",
                        params={"currentVersion": current_version})

    def firmware_update_log(self, log_id: str, status: int,
                            bytes_downloaded: int | None = None,
                            failure_reason: str | None = None) -> HttpResult:
        """PUT /api/iot-devices/firmware-update-log/{id}.

        `IotFirmwareUpdateStatusEnum`: Pending=1 Downloading=2 Installing=3 Success=4
        Failed=5 Skipped=6 RolledBack=7. `failureReason` ≤ 500 ký tự (backend validation).
        """
        body: dict = {"status": int(status)}
        if bytes_downloaded is not None and bytes_downloaded > 0:
            body["bytesDownloaded"] = int(bytes_downloaded)
        if failure_reason:
            body["failureReason"] = failure_reason[:500]
        return self.put(f"/api/iot-devices/firmware-update-log/{log_id}", body)

    def download_artifact(self, url: str, expected_sha256: str = "",
                          timeout_s: float = 20.0) -> tuple[bool, int, str, str]:
        """Tải .bin OTA + tính SHA-256 song song — mirror `ota::downloadAndFlash`.

        Trả `(ok, bytes_downloaded, sha256_hex, error)`.

        ⚠ KHÔNG kèm `X-Api-Key`/`X-Device-Code`: firmware dùng `HTTPClient` trần cho URL này
        (`http.begin(client, url)`), tức artifact URL phải public hoặc pre-signed. Gửi thêm header
        ở đây sẽ khiến simulator tải được trong khi thiết bị thật 401 — che mất đúng lỗi cần thấy.

        Simulator DỪNG ở đây (không ghi partition, không reboot thật) nhưng phần **tương tác với
        backend** — tải đủ byte, đối chiếu Content-Length, xác minh SHA-256 — thì làm THẬT.
        """
        full_url = url if url.startswith(("http://", "https://")) else f"{self.base_url}{url}"
        sha = hashlib.sha256()
        written = 0
        try:
            with requests.get(full_url, stream=True, timeout=timeout_s,
                              verify=self.session.verify) as r:
                if r.status_code != 200:
                    hint = ""
                    if r.status_code in (401, 403):
                        hint = (" — artifact URL đòi xác thực; firmware tải KHÔNG kèm header auth "
                                "nên thiết bị thật cũng sẽ hỏng ở đây")
                    return False, 0, "", f"download http {r.status_code}{hint}"
                content_len = r.headers.get("Content-Length")
                for chunk in r.iter_content(chunk_size=1024):
                    if not chunk:
                        continue
                    sha.update(chunk)
                    written += len(chunk)
        except requests.RequestException as ex:
            return False, written, "", f"download lỗi mạng: {ex}"

        if content_len is not None:
            try:
                expected_len = int(content_len)
            except ValueError:
                expected_len = -1
            if expected_len >= 0 and written != expected_len:
                return False, written, "", f"size mismatch: got {written} / expected {expected_len}"

        digest = sha.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.strip().lower():
            return False, written, digest, "checksum mismatch"
        return True, written, digest, ""

    # ── verb chung ─────────────────────────────────────────────────────────────────────────
    def post(self, path: str, body: dict, extra_headers: dict | None = None) -> HttpResult:
        return self._request("POST", path, body=body, extra_headers=extra_headers)

    def put(self, path: str, body: dict, extra_headers: dict | None = None) -> HttpResult:
        return self._request("PUT", path, body=body, extra_headers=extra_headers)

    def get(self, path: str, params: dict | None = None) -> HttpResult:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None,
                 extra_headers: dict | None = None) -> HttpResult:
        import time as _time

        url = f"{self.base_url}{path}"
        headers = dict(extra_headers or {})
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)

        t0 = _time.monotonic()
        try:
            r = self.session.request(method, url, data=data, params=params,
                                     headers=headers, timeout=self.timeout_s)
        except requests.RequestException as ex:
            log.warning("%s %s LỖI MẠNG: %s", method, path, ex)
            res = HttpResult(ok=False, status_code=0, body=str(ex),
                             duration_ms=int((_time.monotonic() - t0) * 1000))
            self._notify(res)
            return res

        res = self._wrap(r, int((_time.monotonic() - t0) * 1000))
        self._notify(res)
        return res

    def _notify(self, res: HttpResult) -> None:
        if self._on_result is not None:
            self._on_result(res.status_code)

    @staticmethod
    def _wrap(r: requests.Response, duration_ms: int) -> HttpResult:
        body = r.text[:RESPONSE_SNIPPET_CHARS]
        parsed = None
        try:
            parsed = r.json()
        except ValueError:
            pass
        return HttpResult(ok=200 <= r.status_code < 300, status_code=r.status_code,
                          body=body, json=parsed, duration_ms=duration_ms)
