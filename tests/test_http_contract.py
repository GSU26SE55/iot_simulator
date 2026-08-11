"""Hợp đồng HTTP THẬT của `IotHttpClient` — header, method, đường dẫn, tham số.

Test này chạy trên client THẬT (không phải đồ giả): chặn ở tầng `session.request` để soi đúng
những gì đi ra dây. Header sai là lớp lỗi rất khó truy — backend trả 401/403 mà không nói vì sao.
"""
from __future__ import annotations

import json
import unittest

from src.config import CONTRACT_CURRENT, CONTRACT_IOT2
from src.http_client import IotHttpClient


class _CapturedRequest:
    def __init__(self, method, url, kwargs):
        self.method = method
        self.url = url
        self.kwargs = kwargs

    @property
    def headers(self) -> dict:
        return self.kwargs.get("headers") or {}

    @property
    def body(self):
        data = self.kwargs.get("data")
        return json.loads(data) if data else None

    @property
    def params(self):
        return self.kwargs.get("params")


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"isSuccess": True}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _Recorder:
    """Thay `Session.request` — ghi lại rồi trả lời sẵn."""

    def __init__(self, response: _FakeResponse | None = None):
        self.calls: list[_CapturedRequest] = []
        self.response = response or _FakeResponse()

    def __call__(self, method, url, **kwargs):
        self.calls.append(_CapturedRequest(method, url, kwargs))
        return self.response

    @property
    def last(self) -> _CapturedRequest:
        return self.calls[-1]


def _client(contract: str = CONTRACT_IOT2, recorder: _Recorder | None = None):
    c = IotHttpClient(base_url="https://backend.test:7200/", device_code="esp32-sim-001",
                      api_key="iotk_secret", tls_verify=False, firmware_version="1.0.0-sim",
                      contract_version=contract)
    rec = recorder or _Recorder()
    c.session.request = rec           # type: ignore[assignment]
    return c, rec


class HeaderTest(unittest.TestCase):
    def _merged_headers(self, client: IotHttpClient, req: _CapturedRequest) -> dict:
        merged = dict(client.session.headers)
        merged.update(req.headers)
        return merged

    def test_production_sends_api_key_device_code_and_accept(self):
        c, rec = _client(CONTRACT_IOT2)
        c.ingest({"items": []}, "idem-1")
        headers = self._merged_headers(c, rec.last)
        self.assertEqual(headers["X-Api-Key"], "iotk_secret")
        self.assertEqual(headers["X-Device-Code"], "esp32-sim-001")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_idempotency_key_only_on_ingest(self):
        c, rec = _client(CONTRACT_IOT2)
        c.ingest({"items": []}, "idem-1")
        self.assertEqual(rec.last.headers["Idempotency-Key"], "idem-1")

        c.heartbeat({"UptimeSeconds": 1})
        self.assertNotIn("Idempotency-Key", rec.last.headers)

        c.provision("rev", "2026-01-01T00:00:00Z")
        self.assertNotIn("Idempotency-Key", rec.last.headers)

        c.environmental_incident({"siteId": "s"})
        self.assertNotIn("Idempotency-Key", rec.last.headers)

    def test_legacy_contract_omits_device_code_and_idempotency(self):
        """Backend Sprint 1 chưa biết `X-Device-Code`, và cũng chưa khử trùng theo khoá."""
        c, rec = _client(CONTRACT_CURRENT)
        c.ingest({"items": []}, "idem-1")
        headers = self._merged_headers(c, rec.last)
        self.assertNotIn("X-Device-Code", headers)
        self.assertNotIn("Idempotency-Key", rec.last.headers)

    def test_identity_hot_reload_updates_headers(self):
        c, rec = _client(CONTRACT_IOT2)
        c.set_identity(device_code="esp32-sim-999", api_key="iotk_new")
        c.ingest({"items": []}, None)
        headers = self._merged_headers(c, rec.last)
        self.assertEqual(headers["X-Api-Key"], "iotk_new")
        self.assertEqual(headers["X-Device-Code"], "esp32-sim-999")


class EndpointTest(unittest.TestCase):
    """Route backend THẬT — không có `/v1/` (đã đối chiếu controller)."""

    def test_all_routes_and_methods(self):
        c, rec = _client(CONTRACT_IOT2)
        cases = [
            (lambda: c.ingest({"items": []}, "k"), "POST",
             "https://backend.test:7200/api/sensor-readings/batch"),
            (lambda: c.ambient_ingest({"items": []}), "POST",
             "https://backend.test:7200/api/ambient/readings/batch"),
            (lambda: c.environmental_incident({"siteId": "s"}), "POST",
             "https://backend.test:7200/api/environmental-incidents"),
            (lambda: c.provision("rev", "ts"), "POST",
             "https://backend.test:7200/api/iot-devices/provision"),
            (lambda: c.heartbeat({}), "POST",
             "https://backend.test:7200/api/iot-devices/heartbeat"),
            (lambda: c.firmware_check("1.0.0"), "GET",
             "https://backend.test:7200/api/iot-devices/firmware-check"),
            (lambda: c.firmware_update_log("log-1", 4), "PUT",
             "https://backend.test:7200/api/iot-devices/firmware-update-log/log-1"),
        ]
        for call, method, url in cases:
            call()
            self.assertEqual(rec.last.method, method, url)
            self.assertEqual(rec.last.url, url)

    def test_base_url_trailing_slash_is_normalised(self):
        c, rec = _client(CONTRACT_IOT2)
        self.assertEqual(c.base_url, "https://backend.test:7200")
        c.heartbeat({})
        self.assertNotIn("//api", rec.last.url.replace("https://", ""))

    def test_firmware_check_sends_current_version_query(self):
        c, rec = _client(CONTRACT_IOT2)
        c.firmware_check("1.2.3")
        self.assertEqual(rec.last.params, {"currentVersion": "1.2.3"})


class BodyTest(unittest.TestCase):
    def test_provision_body_is_pascal_case(self):
        """Khớp `provision.cpp`: FirmwareVersion / HardwareRevision / DeviceTimestamp."""
        c, rec = _client(CONTRACT_IOT2)
        c.provision("ESP32-S3-DevKitC-1-N16R8", "2026-06-13T08:15:42Z")
        self.assertEqual(rec.last.body, {
            "FirmwareVersion": "1.0.0-sim",
            "HardwareRevision": "ESP32-S3-DevKitC-1-N16R8",
            "DeviceTimestamp": "2026-06-13T08:15:42Z",
        })

    def test_update_log_body_omits_optional_fields(self):
        c, rec = _client(CONTRACT_IOT2)
        c.firmware_update_log("log-1", 4)
        self.assertEqual(rec.last.body, {"status": 4})

        c.firmware_update_log("log-1", 5, bytes_downloaded=1024, failure_reason="boom")
        self.assertEqual(rec.last.body,
                         {"status": 5, "bytesDownloaded": 1024, "failureReason": "boom"})

    def test_update_log_failure_reason_capped_at_500(self):
        c, rec = _client(CONTRACT_IOT2)
        c.firmware_update_log("log-1", 5, failure_reason="x" * 900)
        self.assertEqual(len(rec.last.body["failureReason"]), 500)

    def test_zero_bytes_downloaded_is_omitted(self):
        c, rec = _client(CONTRACT_IOT2)
        c.firmware_update_log("log-1", 2, bytes_downloaded=0)
        self.assertNotIn("bytesDownloaded", rec.last.body)


class ResultTest(unittest.TestCase):
    def test_network_error_maps_to_status_zero(self):
        """`status_code == 0` là quy ước 'lỗi truyền tải' mà cả backoff lẫn LinkState dựa vào."""
        import requests
        c, _ = _client(CONTRACT_IOT2)

        def boom(*a, **k):
            raise requests.ConnectionError("refused")

        c.session.request = boom      # type: ignore[assignment]
        res = c.heartbeat({})
        self.assertFalse(res.ok)
        self.assertEqual(res.status_code, 0)

    def test_2xx_all_count_as_ok(self):
        for code in (200, 201, 202, 204):
            c, _ = _client(CONTRACT_IOT2, _Recorder(_FakeResponse(code)))
            self.assertTrue(c.ingest({"items": []}, "k").ok, code)

    def test_link_callback_receives_status(self):
        seen: list[int] = []
        c = IotHttpClient(base_url="https://x", device_code="d", api_key="k",
                          tls_verify=False, contract_version=CONTRACT_IOT2,
                          on_result=seen.append)
        c.session.request = _Recorder(_FakeResponse(503))   # type: ignore[assignment]
        c.heartbeat({})
        self.assertEqual(seen, [503])


if __name__ == "__main__":
    unittest.main()
