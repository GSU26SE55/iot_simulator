"""Test parity firmware ESP32 ↔ simulator cho các tính năng Sprint 4/7:
  - MQTT topic per-pin (solar/{deviceCode}/{batterySerial}/telemetry)
  - MQTT downlink command schema ({cmdId,type,params} → ack {cmdId,status,error?})
  - Provision response apply (pollingInterval / heartbeatInterval / siteId / ntpServer)
  - OTA decision + lifecycle (firmware-check → update-log → version bump)
"""
from __future__ import annotations

import unittest
from pathlib import Path

from src.config import (CONTRACT_IOT2, BackendConfig, BatteryConfig,
                        DeviceConfig, MqttConfig, SensorDrift, SensorToggles)
from src.device import MQTT_PUBLISH_FAIL_THRESHOLD, SimulatedDevice
from src.mqtt_client import IotMqttClient, MqttOptions
from src.ota import (FW_STATUS_DOWNLOADING, FW_STATUS_INSTALLING,
                     FW_STATUS_SUCCESS, OtaRunner, ota_should_update,
                     parse_firmware_check)

TMP = Path("/tmp/iot-sim-feat-test")


def _backend(contract: str = CONTRACT_IOT2) -> BackendConfig:
    return BackendConfig(
        base_url="https://localhost:7200", tls_verify=False,
        heartbeat_interval_s=60, ingest_interval_s=15, batch_size_per_battery=3,
        contract_version=contract, retry_base_s=2, retry_max_s=60, retry_jitter_pct=20,
    )


def _mqtt() -> MqttConfig:
    return MqttConfig(enabled=False, host="localhost", port=1883, tls=False,
                      topic_prefix="solar", qos=1)


def _device_cfg() -> DeviceConfig:
    return DeviceConfig(
        device_code="esp32-sim-001",
        site_id_guid="11111111-1111-1111-1111-111111111111",
        site_label="site-test",
        firmware_version="1.0.0",
        hardware_revision="rev-test",
        model="ESP32-WROOM-S3",
        api_key="iotk_testkey",
        batteries=[BatteryConfig(
            serial="BAT-2026-001", unit_id=1, nominal_voltage=12.8,
            nominal_capacity_ah=100, initial_soc=80, initial_soh=92, cycle_count=100,
            battery_asset_id="22222222-2222-2222-2222-222222222201")],
        sensors=SensorToggles(ina226=True, ds18b20=True),
        scenario="normal", sensor_drift=SensorDrift(),
    )


def _new_device(contract: str = CONTRACT_IOT2) -> SimulatedDevice:
    return SimulatedDevice(_device_cfg(), _backend(contract), _mqtt(), TMP)


class _FakeMqtt:
    """Capture cmd-ack publish để verify schema (không cần broker thật)."""
    def __init__(self, connected: bool = False):
        self.connected = connected
        self.acks: list[dict] = []
        self.telemetry: list[tuple[str, dict]] = []

    def publish_cmd_ack(self, payload: dict) -> bool:
        self.acks.append(payload)
        return True

    def publish_telemetry(self, serial: str, payload: dict) -> bool:
        self.telemetry.append((serial, payload))
        return True


# ─────────────────────────── MQTT topic ───────────────────────────
class MqttTopicTest(unittest.TestCase):
    def _client(self) -> IotMqttClient:
        opts = MqttOptions(host="localhost", port=1883, username="esp32-sim-001",
                           password="", tls=False, qos=1, topic_prefix="solar",
                           device_code="esp32-sim-001", site_id="site-x")
        return IotMqttClient(opts)

    def test_telemetry_topic_is_per_pin(self):
        c = self._client()
        self.assertEqual(c._telemetry_topic("BAT-2026-001"),
                         "solar/esp32-sim-001/BAT-2026-001/telemetry")

    def test_control_topics_match_firmware(self):
        c = self._client()
        self.assertEqual(c._t("status"), "solar/esp32-sim-001/status")
        self.assertEqual(c._t("heartbeat"), "solar/esp32-sim-001/heartbeat")
        self.assertEqual(c._t("cmd"), "solar/esp32-sim-001/cmd")
        self.assertEqual(c._t("cmd/ack"), "solar/esp32-sim-001/cmd/ack")


# ─────────────────────── MQTT command schema ──────────────────────
class CommandSchemaTest(unittest.TestCase):
    def setUp(self):
        self.dev = _new_device(CONTRACT_IOT2)
        self.fake = _FakeMqtt()
        self.dev._mqtt = self.fake

    def test_set_interval_applies_and_acks_ok(self):
        self.dev._on_mqtt_command({"cmdId": "c1", "type": "set_interval",
                                   "params": {"pollingSeconds": 30}})
        self.assertEqual(self.dev.backend_cfg.ingest_interval_s, 30)
        self.assertEqual(self.fake.acks[-1], {"cmdId": "c1", "status": "ok"})

    def test_set_interval_hyphen_alias(self):
        self.dev._on_mqtt_command({"cmdId": "c2", "type": "SET-INTERVAL",
                                   "params": {"pollingIntervalSeconds": 12}})
        self.assertEqual(self.dev.backend_cfg.ingest_interval_s, 12)
        self.assertEqual(self.fake.acks[-1]["status"], "ok")

    def test_set_interval_out_of_range_fails(self):
        self.dev._on_mqtt_command({"cmdId": "c3", "type": "set_interval",
                                   "params": {"pollingSeconds": 99999}})
        ack = self.fake.acks[-1]
        self.assertEqual(ack["status"], "failed")
        self.assertIn("range", ack["error"])

    def test_missing_type_failed(self):
        self.dev._on_mqtt_command({"cmdId": "c4"})
        ack = self.fake.acks[-1]
        self.assertEqual(ack["status"], "failed")
        self.assertIn("type", ack["error"])

    def test_unknown_type_unknown_status(self):
        self.dev._on_mqtt_command({"cmdId": "c5", "type": "reboot_now"})
        self.assertEqual(self.fake.acks[-1]["status"], "unknown")

    def test_ack_has_no_message_key(self):
        # Contract firmware ack = {cmdId,status,error?} — KHÔNG có "message".
        self.dev._on_mqtt_command({"cmdId": "c6", "type": "set_interval",
                                   "params": {"pollingSeconds": 20}})
        self.assertNotIn("message", self.fake.acks[-1])


# ─────────────────────── Provision apply ──────────────────────────
class ProvisionApplyTest(unittest.TestCase):
    def test_apply_response_overrides_runtime(self):
        dev = _new_device(CONTRACT_IOT2)
        dev._apply_provision_response({"isSuccess": True, "data": {
            "pollingIntervalSeconds": 10,
            "heartbeatIntervalSeconds": 120,
            "siteId": "99999999-9999-9999-9999-999999999999",
            "ntpServer": "time.cloudflare.com",
        }})
        self.assertEqual(dev.backend_cfg.ingest_interval_s, 10)
        self.assertEqual(dev.backend_cfg.heartbeat_interval_s, 120)
        self.assertEqual(dev.cfg.site_id_guid, "99999999-9999-9999-9999-999999999999")
        self.assertEqual(dev.cfg.ntp_server, "time.cloudflare.com")

    def test_apply_bounds_clamped(self):
        dev = _new_device(CONTRACT_IOT2)
        dev._apply_provision_response({"data": {"pollingIntervalSeconds": 100000,
                                                "heartbeatIntervalSeconds": 5}})
        self.assertLessEqual(dev.backend_cfg.ingest_interval_s, 600)   # bound [1,600]
        self.assertGreaterEqual(dev.backend_cfg.heartbeat_interval_s, 10)  # bound [10,3600]

    def test_no_data_is_noop(self):
        dev = _new_device(CONTRACT_IOT2)
        before = dev.backend_cfg.ingest_interval_s
        dev._apply_provision_response(None)
        dev._apply_provision_response({"isSuccess": True})
        self.assertEqual(dev.backend_cfg.ingest_interval_s, before)


# ───────────────────────────── OTA ────────────────────────────────
class OtaDecisionTest(unittest.TestCase):
    def test_should_update_string_compare(self):
        self.assertTrue(ota_should_update(True, "1.0.0", "1.1.0"))
        self.assertFalse(ota_should_update(True, "1.1.0", "1.1.0"))   # trùng → không update
        self.assertFalse(ota_should_update(False, "1.0.0", "1.1.0"))  # hasUpdate=false
        self.assertFalse(ota_should_update(True, "1.0.0", ""))        # thiếu target
        self.assertTrue(ota_should_update(True, None, "1.0.0"))       # current null hợp lệ

    def test_parse_aliases(self):
        offer = parse_firmware_check({"hasUpdate": True, "targetVersion": "2.0.0",
                                      "artifactUrl": "http://x/fw.bin",
                                      "sha256Checksum": "ab" * 32, "updateLogId": "log-1",
                                      "artifactSizeBytes": 1234})
        self.assertTrue(offer.has_update)
        self.assertEqual(offer.target_version, "2.0.0")
        self.assertEqual(offer.download_url, "http://x/fw.bin")
        self.assertEqual(offer.log_id, "log-1")
        self.assertEqual(offer.size_bytes, 1234)


class _FakeHttp:
    """Fake IotHttpClient cho OtaRunner — firmware_check trả offer, log ghi lại."""
    def __init__(self, check_json: dict):
        self.firmware_version = "1.0.0"
        self._check_json = check_json
        self.log_calls: list[tuple] = []

    def firmware_check(self, current_version):
        from src.http_client import HttpResult
        return HttpResult(ok=True, status_code=200, body="", json=self._check_json)

    def firmware_update_log(self, log_id, status, bytes_downloaded=None, failure_reason=None):
        from src.http_client import HttpResult
        self.log_calls.append((log_id, status, bytes_downloaded))
        return HttpResult(ok=True, status_code=200, body="")


class OtaRunnerTest(unittest.TestCase):
    def test_lifecycle_and_version_bump(self):
        applied = {}
        http = _FakeHttp({"isSuccess": True, "data": {
            "updateAvailable": True, "targetVersion": "1.2.0",
            "downloadUrl": "http://x/fw.bin", "sha256Checksum": "cd" * 32,
            "updateLogId": "log-42", "artifactSizeBytes": 2048,
        }})
        runner = OtaRunner(http, current_version_getter=lambda: http.firmware_version,
                           apply_version=lambda v: applied.update(ver=v))
        result = runner.check_and_apply()
        self.assertTrue(result.updated)
        self.assertEqual(result.target_version, "1.2.0")
        self.assertEqual(applied["ver"], "1.2.0")
        # lifecycle Downloading → Installing → Success theo đúng thứ tự
        statuses = [c[1] for c in http.log_calls]
        self.assertEqual(statuses, [FW_STATUS_DOWNLOADING, FW_STATUS_INSTALLING, FW_STATUS_SUCCESS])
        self.assertEqual((http.log_calls[0][0]), "log-42")

    def test_no_update_when_same_version(self):
        http = _FakeHttp({"data": {"updateAvailable": False, "targetVersion": "1.0.0"}})
        runner = OtaRunner(http, current_version_getter=lambda: "1.0.0",
                           apply_version=lambda v: None)
        result = runner.check_and_apply()
        self.assertFalse(result.updated)
        self.assertEqual(http.log_calls, [])   # không PUT log nếu không update


# ─────────────────── MQTT-first fallback threshold ─────────────────
class MqttFirstThresholdTest(unittest.TestCase):
    def test_threshold_constant_matches_firmware(self):
        self.assertEqual(MQTT_PUBLISH_FAIL_THRESHOLD, 3)


if __name__ == "__main__":
    unittest.main()
