"""Đồ giả dùng chung cho test — backend giả + MQTT giả + hàm dựng thiết bị.

`FakeHttp` bắt chước ĐÚNG API công khai của `src.http_client.IotHttpClient` (kể cả
`HttpResult.duration_ms` và `download_artifact`), nên test đi qua đúng những nhánh mà code thật
đi qua — không có đường tắt nào riêng cho test.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from src.config import (CONTRACT_IOT2, BackendConfig, BatteryConfig, DeviceConfig,
                        MqttConfig, SensorDrift, SensorToggles, SensorTuning)
from src.device import SimulatedDevice
from src.http_client import HttpResult


def ok(status: int = 200, body: str = "", json_body=None) -> HttpResult:
    return HttpResult(ok=200 <= status < 300, status_code=status, body=body, json=json_body,
                      duration_ms=1)


def net_error(message: str = "connection refused") -> HttpResult:
    """Lỗi TRUYỀN TẢI — `status_code == 0`, đúng như `IotHttpClient` khi requests ném lỗi."""
    return HttpResult(ok=False, status_code=0, body=message, json=None, duration_ms=1)


class FakeHttp:
    """Backend giả có kịch bản. Ghi lại mọi lời gọi để test soi được payload thật."""

    def __init__(self, contract_version: str = CONTRACT_IOT2,
                 firmware_version: str = "1.0.0-sim"):
        self.contract_version = contract_version
        self.firmware_version = firmware_version
        self.device_code = "ESP32-TEST-001"
        self.api_key = "iotk_test"
        self.base_url = "http://localhost:4001"
        self.on_result_hook = None

        # Kịch bản trả lời — có thể thay bằng callable(payload) hoặc HttpResult.
        self.provision_response: object = ok(200)
        self.heartbeat_response: object = ok(200)
        self.ingest_response: object = ok(201)
        self.ambient_response: object = ok(201)
        self.incident_response: object = ok(201)
        self.firmware_check_response: object = ok(200, json_body={"isSuccess": True, "data": {}})
        self.update_log_response: object = ok(200)
        self.artifact: bytes | None = None
        self.artifact_error: str = ""

        # Nhật ký.
        self.provision_calls: list[dict] = []
        self.heartbeat_calls: list[dict] = []
        self.ingest_calls: list[tuple[dict, str | None]] = []
        self.ambient_calls: list[dict] = []
        self.incident_calls: list[dict] = []
        self.firmware_check_calls: list[str] = []
        self.update_log_calls: list[tuple] = []
        self.download_calls: list[str] = []

    # ── nội bộ ────────────────────────────────────────────────────────────────────────────
    def _resolve(self, scripted, *args) -> HttpResult:
        res = scripted(*args) if callable(scripted) else scripted
        if self.on_result_hook is not None:
            self.on_result_hook(res.status_code)
        return res

    # ── API giống IotHttpClient ───────────────────────────────────────────────────────────
    def set_identity(self, device_code: str | None = None, api_key: str | None = None) -> None:
        if device_code:
            self.device_code = device_code
        if api_key:
            self.api_key = api_key

    def provision(self, hardware_revision: str, device_timestamp_iso: str) -> HttpResult:
        body = {"FirmwareVersion": self.firmware_version,
                "HardwareRevision": hardware_revision,
                "DeviceTimestamp": device_timestamp_iso}
        self.provision_calls.append(body)
        return self._resolve(self.provision_response, body)

    def heartbeat(self, body: dict) -> HttpResult:
        self.heartbeat_calls.append(body)
        return self._resolve(self.heartbeat_response, body)

    def ingest(self, payload: dict, idempotency_key: str | None) -> HttpResult:
        self.ingest_calls.append((payload, idempotency_key))
        return self._resolve(self.ingest_response, payload, idempotency_key)

    def ambient_ingest(self, payload: dict, idempotency_key: str | None = None) -> HttpResult:
        self.ambient_calls.append(payload)
        return self._resolve(self.ambient_response, payload)

    def environmental_incident(self, payload: dict) -> HttpResult:
        self.incident_calls.append(payload)
        return self._resolve(self.incident_response, payload)

    def firmware_check(self, current_version: str) -> HttpResult:
        self.firmware_check_calls.append(current_version)
        return self._resolve(self.firmware_check_response, current_version)

    def firmware_update_log(self, log_id: str, status: int, bytes_downloaded=None,
                            failure_reason=None) -> HttpResult:
        self.update_log_calls.append((log_id, status, bytes_downloaded, failure_reason))
        return self._resolve(self.update_log_response, log_id, status)

    def download_artifact(self, url: str, expected_sha256: str = "",
                          timeout_s: float = 20.0):
        self.download_calls.append(url)
        if self.artifact_error:
            return False, 0, "", self.artifact_error
        data = self.artifact or b""
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.strip().lower():
            return False, len(data), digest, "checksum mismatch"
        return True, len(data), digest, ""

    # ── tiện ích cho test ─────────────────────────────────────────────────────────────────
    def last_ingest_items(self) -> list[dict]:
        return self.ingest_calls[-1][0]["items"] if self.ingest_calls else []

    def statuses_logged(self) -> list[int]:
        return [c[1] for c in self.update_log_calls]


class FakeMqtt:
    """MQTT giả — ghi lại telemetry + ack, mô phỏng được publish hỏng và streak fail."""

    def __init__(self, connected: bool = True, fail_after: int | None = None):
        self.connected = connected
        self.consecutive_fail_count = 0
        self.auth_fail_count = 0
        self.publish_ok_count = 0
        self.publish_fail_count = 0
        self.telemetry: list[tuple[str, dict]] = []
        self.acks: list[dict] = []
        self.status_published: list[str] = []
        self.disconnected = False
        # Sau `fail_after` lần publish telemetry thành công thì các lần sau HỎNG.
        self._fail_after = fail_after
        self._sent = 0

    def publish_telemetry(self, serial: str, payload: dict) -> bool:
        if self._fail_after is not None and self._sent >= self._fail_after:
            self.publish_fail_count += 1
            self.consecutive_fail_count += 1
            return False
        self._sent += 1
        self.publish_ok_count += 1
        self.consecutive_fail_count = 0
        self.telemetry.append((serial, payload))
        return True

    def publish_cmd_ack(self, payload: dict) -> bool:
        self.acks.append(payload)
        return True

    def publish_status(self, status: str, retain: bool = True) -> bool:
        self.status_published.append(status)
        return True

    def tick(self) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    def reset_consecutive_fails(self) -> None:
        self.consecutive_fail_count = 0

    def reset_auth_failures(self) -> None:
        self.auth_fail_count = 0

    def auth_failure_threshold(self) -> int:
        return 5


# ────────────────────────────── hàm dựng cấu hình ───────────────────────────────────────────
def make_backend(contract: str = CONTRACT_IOT2, **overrides) -> BackendConfig:
    cfg = BackendConfig(
        base_url="http://localhost:4001", tls_verify=False,
        heartbeat_interval_s=60, ingest_interval_s=5, batch_size_per_battery=3,
        contract_version=contract, retry_base_s=2, retry_max_s=60, retry_jitter_pct=20,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_mqtt_cfg(**overrides) -> MqttConfig:
    cfg = MqttConfig(enabled=False, host="localhost", port=1883, tls=False,
                     topic_prefix="solar", qos=0)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_device_cfg(device_code: str = "esp32-sim-001", batteries: int = 1,
                    **overrides) -> DeviceConfig:
    bats = [
        BatteryConfig(
            serial=f"BAT-T-{i + 1:03d}", unit_id=i + 1, nominal_voltage=12.8,
            nominal_capacity_ah=100, initial_soc=80.0, initial_soh=92.0, cycle_count=100,
            battery_asset_id=f"22222222-2222-2222-2222-2222222222{i + 1:02d}")
        for i in range(batteries)
    ]
    cfg = DeviceConfig(
        device_code=device_code,
        site_id_guid="11111111-1111-1111-1111-111111111111",
        site_label="site-test",
        firmware_version="1.0.0",
        hardware_revision="ESP32-S3-DevKitC-1-N16R8",
        model="ESP32-WROOM-S3",
        api_key="iotk_testkey_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        batteries=bats,
        sensors=SensorToggles(ina226=True, ds18b20=True),
        scenario="normal",
        sensor_drift=SensorDrift(),
        sensor_tuning=SensorTuning(),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class DeviceHarness:
    """Dựng `SimulatedDevice` với backend giả + thư mục tạm riêng cho mỗi test."""

    def __init__(self, contract: str = CONTRACT_IOT2, device_cfg: DeviceConfig | None = None,
                 backend_cfg: BackendConfig | None = None, mqtt_cfg: MqttConfig | None = None,
                 http: FakeHttp | None = None, persist_state: bool = True):
        self._tmp = tempfile.TemporaryDirectory(prefix="iot-sim-test-")
        root = Path(self._tmp.name)
        self.http = http or FakeHttp(contract_version=contract)
        self.device = SimulatedDevice(
            dev_cfg=device_cfg or make_device_cfg(),
            backend_cfg=backend_cfg or make_backend(contract),
            mqtt_cfg=mqtt_cfg or make_mqtt_cfg(),
            queue_dir=root / "queue",
            state_dir=root / "state",
            persist_state=persist_state,
            http=self.http,
        )
        self.root = root

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "DeviceHarness":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
