"""HTTPS client cho ESP32 simulator.

Hỗ trợ 2 contract version:

  `current`           — endpoint TODAY (api-battery.md):
      POST /api/sensor-readings/batch        header X-Api-Key only
      POST /api/ambient/readings/batch       header X-Api-Key only
      POST /api/environmental-incidents      header X-Api-Key only
    Provision/heartbeat/firmware-check: chưa có trong backend — call sẽ 404
    (giữ method cho khi Sprint IoT-2 #IoT2-07..10 merge).

  `iot2-production`   — endpoint Sprint IoT-2 (#IoT2-14..20):
      POST /api/sensor-readings/batch        header thêm X-Device-Code + Idempotency-Key
      POST /api/iot-devices/provision     header X-Device-Code + X-Api-Key
      POST /api/iot-devices/heartbeat     header X-Device-Code + X-Api-Key
      GET  /api/iot-devices/firmware-check
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests
import urllib3

from .config import CONTRACT_IOT2

log = logging.getLogger("iot-sim.http")


@dataclass
class HttpResult:
    ok: bool
    status_code: int
    body: str
    json: Any = None


class IotHttpClient:
    def __init__(self, base_url: str, device_code: str, api_key: str, *,
                 tls_verify: bool = True, firmware_version: str = "1.0.0-sim",
                 contract_version: str = "current"):
        self.base_url = base_url.rstrip("/")
        self.device_code = device_code
        self.api_key = api_key
        self.firmware_version = firmware_version
        self.contract_version = contract_version
        self.session = requests.Session()
        self.session.verify = tls_verify
        if not tls_verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        headers = {
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": f"iot-simulator/{firmware_version} ({device_code})",
        }
        # X-Device-Code chỉ áp dụng cho contract IoT-2 (Sprint IoT-2 #IoT2-14)
        if contract_version == CONTRACT_IOT2:
            headers["X-Device-Code"] = device_code
        self.session.headers.update(headers)

    # ─────────────── Provision (Sprint IoT-2 #IoT2-07, §52.3) ───────────────
    def provision(self, hardware_revision: str, device_timestamp_iso: str) -> HttpResult:
        body = {
            "FirmwareVersion": self.firmware_version,
            "HardwareRevision": hardware_revision,
            "DeviceTimestamp": device_timestamp_iso,
        }
        return self._post("/api/iot-devices/provision", body)

    # ─────────────── Heartbeat (Sprint IoT-2 #IoT2-10, §52.4) ───────────────
    def heartbeat(self, body: dict) -> HttpResult:
        return self._post("/api/iot-devices/heartbeat", body)

    # ─────────────── Sensor ingest ───────────────
    def ingest(self, payload: dict, idempotency_key: str | None) -> HttpResult:
        extra = {}
        if self.contract_version == CONTRACT_IOT2 and idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        return self._post("/api/sensor-readings/batch", payload, extra_headers=extra)

    # ─────────────── Ambient readings ───────────────
    # Endpoint thật: /api/ambient/readings/batch (api-battery.md)
    def ambient_ingest(self, payload: dict, idempotency_key: str | None) -> HttpResult:
        extra = {}
        if self.contract_version == CONTRACT_IOT2 and idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        return self._post("/api/ambient/readings/batch", payload, extra_headers=extra)

    # ─────────────── Environmental incident ───────────────
    def environmental_incident(self, payload: dict) -> HttpResult:
        return self._post("/api/environmental-incidents", payload)

    # ─────────────── Firmware OTA check (Sprint IoT-2 #IoT2-35, §52.7) ──────
    def firmware_check(self, current_version: str) -> HttpResult:
        url = f"{self.base_url}/api/iot-devices/firmware-check"
        try:
            r = self.session.get(url, params={"currentVersion": current_version}, timeout=10)
            return self._wrap(r)
        except requests.RequestException as ex:
            log.warning("firmware_check FAIL: %s", ex)
            return HttpResult(ok=False, status_code=0, body=str(ex))

    # ─────────────── helpers ───────────────
    def _post(self, path: str, body: dict, extra_headers: dict | None = None,
              timeout: int = 15) -> HttpResult:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.post(url, data=json.dumps(body), headers=extra_headers or {}, timeout=timeout)
            return self._wrap(r)
        except requests.RequestException as ex:
            log.warning("POST %s FAIL: %s", path, ex)
            return HttpResult(ok=False, status_code=0, body=str(ex))

    @staticmethod
    def _wrap(r: requests.Response) -> HttpResult:
        body = r.text[:500]
        parsed = None
        try:
            parsed = r.json()
        except ValueError:
            pass
        return HttpResult(ok=200 <= r.status_code < 300, status_code=r.status_code, body=body, json=parsed)
