"""Parity firmware ↔ simulator cho: topic MQTT, lệnh downlink, heartbeat, đèn, và các luật
thuần về định danh / cấu hình broker / re-provision.
"""
from __future__ import annotations

import unittest

from src.cmd import (CommandKind, build_ack, classify_type, is_valid_polling_seconds,
                     parse_command_payload)
from src.config import CONTRACT_IOT2
from src.device import MQTT_PUBLISH_FAIL_THRESHOLD
from src.heartbeat import (FIRST_TICK_DELAY_MS, INTERVAL_MAX_MS, INTERVAL_MIN_MS, Heartbeat)
from src.led import LedPattern, LedState, is_lit, palette_for_state, pattern_for_state
from src.mqtt_client import MQTT_AUTH_FAIL_THRESHOLD, MQTT_MAX_PACKET_SIZE, MqttOptions
from src.net_rules import (derive_topic_prefix, identity_is_valid, mqtt_config_usable,
                           mqtt_port_usable, validate_identity_field, IdentityFieldError)
from src.policy import REPROVISION_COOLDOWN_MS, should_reprovision_on_auth_failure
from src.timeutil import ISO_FORMAT, iso_now, iso_now_minus

from tests.fakes import DeviceHarness, FakeHttp, FakeMqtt, ok


# ──────────────────────────────── MQTT topic ────────────────────────────────────────────────
class MqttTopicTest(unittest.TestCase):
    """Topic phải khớp `mqtt_client.cpp` — sai một chữ hoa là broker im lặng nuốt tin."""

    def _opts(self, prefix: str = "solar/esp32-sim-001") -> MqttOptions:
        return MqttOptions(host="localhost", port=1883, username="esp32-sim-001", password="p",
                           tls=False, qos=0, topic_prefix=prefix, device_code="esp32-sim-001")

    def _client(self):
        from src.mqtt_client import IotMqttClient
        return IotMqttClient(self._opts())

    def test_telemetry_topic_is_per_battery(self):
        c = self._client()
        self.assertEqual(c._telemetry_topic("BAT-2026-001"),
                         "solar/esp32-sim-001/BAT-2026-001/telemetry")

    def test_control_topics(self):
        c = self._client()
        self.assertEqual(c._t("status"), "solar/esp32-sim-001/status")
        self.assertEqual(c._t("heartbeat"), "solar/esp32-sim-001/heartbeat")
        self.assertEqual(c._t("cmd"), "solar/esp32-sim-001/cmd")
        self.assertEqual(c._t("cmd/ack"), "solar/esp32-sim-001/cmd/ack")

    def test_prefix_derivation_lowercases_device_code(self):
        """ACL Mosquitto khớp `solar/%u/...` với %u = username = deviceCode CHỮ THƯỜNG."""
        self.assertEqual(derive_topic_prefix("ESP32-SIM-001"), "solar/esp32-sim-001")
        self.assertEqual(derive_topic_prefix("  ESP32-Sim-001  "), "solar/esp32-sim-001")
        self.assertEqual(derive_topic_prefix("dev", root="custom"), "custom/dev")

    def test_prefix_empty_when_device_code_missing(self):
        """Trả rỗng chứ KHÔNG trả 'solar/' trần — publish lên `solar//telemetry` sẽ bị ACL chặn
        trong im lặng, không có gì trong log chỉ ra nguyên nhân."""
        self.assertEqual(derive_topic_prefix(""), "")
        self.assertEqual(derive_topic_prefix(None), "")
        self.assertEqual(derive_topic_prefix("   "), "")

    def test_defaults_match_firmware(self):
        self.assertEqual(MQTT_MAX_PACKET_SIZE, 4096)
        self.assertEqual(MQTT_AUTH_FAIL_THRESHOLD, 5)
        self.assertEqual(MQTT_PUBLISH_FAIL_THRESHOLD, 3)


class MqttRuntimeConfigTest(unittest.TestCase):
    def test_needs_all_four_fields(self):
        self.assertTrue(mqtt_config_usable("h", 1883, "u", "p"))
        self.assertFalse(mqtt_config_usable("", 1883, "u", "p"))
        self.assertFalse(mqtt_config_usable("h", 0, "u", "p"))
        self.assertFalse(mqtt_config_usable("h", 1883, "", "p"))
        self.assertFalse(mqtt_config_usable("h", 1883, "u", ""))

    def test_port_range(self):
        self.assertTrue(mqtt_port_usable(1))
        self.assertTrue(mqtt_port_usable(65535))
        self.assertFalse(mqtt_port_usable(0))
        self.assertFalse(mqtt_port_usable(65536))
        self.assertFalse(mqtt_port_usable("nope"))


# ──────────────────────────────── lệnh downlink ─────────────────────────────────────────────
class CommandParseTest(unittest.TestCase):
    def test_aliases_case_insensitive(self):
        for text in ("set_interval", "set-interval", "SET_INTERVAL", "Set-Interval"):
            self.assertIs(classify_type(text), CommandKind.SET_INTERVAL)
        for text in ("request_heartbeat", "REQUEST-HEARTBEAT"):
            self.assertIs(classify_type(text), CommandKind.REQUEST_HEARTBEAT)
        for text in ("trigger_ota", "TRIGGER-OTA"):
            self.assertIs(classify_type(text), CommandKind.TRIGGER_OTA)

    def test_unknown_type(self):
        self.assertIs(classify_type("reboot"), CommandKind.UNKNOWN)
        self.assertIs(classify_type(""), CommandKind.UNKNOWN)
        self.assertIs(classify_type(None), CommandKind.UNKNOWN)

    def test_parses_raw_json_string(self):
        r = parse_command_payload('{"cmdId":"c1","type":"set_interval",'
                                  '"params":{"pollingSeconds":30}}')
        self.assertTrue(r.ok)
        self.assertEqual(r.cmd_id, "c1")
        self.assertEqual(r.polling_seconds, 30)

    def test_parses_bytes(self):
        r = parse_command_payload(b'{"cmdId":"c1","type":"trigger_ota"}')
        self.assertTrue(r.ok)
        self.assertIs(r.kind, CommandKind.TRIGGER_OTA)

    def test_broken_json_reports_parse_error(self):
        r = parse_command_payload("{not json")
        self.assertFalse(r.ok)
        self.assertTrue(r.parse_error)

    def test_missing_type_is_error(self):
        r = parse_command_payload({"cmdId": "c1"})
        self.assertFalse(r.ok)
        self.assertIn("type", r.parse_error)

    def test_polling_alias_field(self):
        r = parse_command_payload({"cmdId": "c", "type": "set_interval",
                                   "params": {"pollingIntervalSeconds": 12}})
        self.assertTrue(r.has_polling_seconds)
        self.assertEqual(r.polling_seconds, 12)

    def test_non_integer_polling_is_ignored_like_firmware(self):
        """Firmware chỉ nhận `uint32_t` — số âm/số thực bị bỏ qua ⇒ ack 'missing pollingSeconds'."""
        for bad in (-5, 1.5, "30", True, None):
            r = parse_command_payload({"cmdId": "c", "type": "set_interval",
                                       "params": {"pollingSeconds": bad}})
            self.assertFalse(r.has_polling_seconds, bad)

    def test_polling_range(self):
        self.assertTrue(is_valid_polling_seconds(1))
        self.assertTrue(is_valid_polling_seconds(3600))
        self.assertFalse(is_valid_polling_seconds(0))
        self.assertFalse(is_valid_polling_seconds(3601))

    def test_ack_shape(self):
        self.assertEqual(build_ack("c1", "ok"), {"cmdId": "c1", "status": "ok"})
        self.assertEqual(build_ack("c1", "failed", "boom"),
                         {"cmdId": "c1", "status": "failed", "error": "boom"})
        self.assertNotIn("message", build_ack("c1", "ok"))


class CommandDispatchTest(unittest.TestCase):
    def setUp(self):
        self.h = DeviceHarness(contract=CONTRACT_IOT2)
        self.addCleanup(self.h.close)
        self.dev = self.h.device
        self.dev._provision_done = True
        self.mqtt = FakeMqtt(connected=True)
        self.dev._mqtt = self.mqtt

    def _last_ack(self) -> dict:
        return self.mqtt.acks[-1]

    def test_set_interval_applies_and_acks_ok(self):
        self.dev._on_mqtt_command({"cmdId": "c1", "type": "set_interval",
                                   "params": {"pollingSeconds": 30}})
        self.assertEqual(self.dev.backend_cfg.ingest_interval_s, 30)
        self.assertEqual(self._last_ack(), {"cmdId": "c1", "status": "ok"})

    def test_set_interval_persists_so_restart_keeps_it(self):
        self.dev._on_mqtt_command({"cmdId": "c", "type": "set_interval",
                                   "params": {"pollingSeconds": 42}})
        from src import nvs as nvskeys
        self.assertEqual(self.dev._nvs.get_int(nvskeys.KEY_POLL_MS, 0), 42000)

    def test_set_interval_out_of_range_fails(self):
        self.dev._on_mqtt_command({"cmdId": "c3", "type": "set_interval",
                                   "params": {"pollingSeconds": 99999}})
        ack = self._last_ack()
        self.assertEqual(ack["status"], "failed")
        self.assertIn("range", ack["error"])

    def test_set_interval_missing_param_fails(self):
        self.dev._on_mqtt_command({"cmdId": "c", "type": "set_interval"})
        self.assertEqual(self._last_ack()["status"], "failed")
        self.assertIn("pollingSeconds", self._last_ack()["error"])

    def test_missing_type_failed(self):
        self.dev._on_mqtt_command({"cmdId": "c4"})
        self.assertEqual(self._last_ack()["status"], "failed")

    def test_unknown_type_gets_unknown_status(self):
        self.dev._on_mqtt_command({"cmdId": "c5", "type": "reboot"})
        self.assertEqual(self._last_ack()["status"], "unknown")
        self.assertEqual(self.dev.state.cmd_unknown, 1)

    def test_request_heartbeat_sends_immediately(self):
        self.h.http.heartbeat_response = ok(200)
        self.dev._on_mqtt_command({"cmdId": "c6", "type": "request_heartbeat"})
        self.assertEqual(len(self.h.http.heartbeat_calls), 1)
        self.assertEqual(self._last_ack()["status"], "ok")

    def test_request_heartbeat_failure_acks_failed(self):
        self.h.http.heartbeat_response = ok(500)
        self.dev._on_mqtt_command({"cmdId": "c7", "type": "request_heartbeat"})
        self.assertEqual(self._last_ack()["status"], "failed")

    def test_trigger_ota_schedules_check(self):
        self.dev._on_mqtt_command({"cmdId": "c8", "type": "trigger_ota"})
        ack = self._last_ack()
        self.assertEqual(ack["status"], "ok")
        self.assertIn("ota", ack["error"])
        self.assertTrue(self.dev._ota._force_check)

    def test_trigger_ota_rejected_while_verifying(self):
        """Status `rejected` — bản simulator cũ KHÔNG có, nên người vận hành luôn thấy 'ok'
        dù thiết bị không làm gì."""
        self.dev._ota.verify_mode = True
        self.dev._on_mqtt_command({"cmdId": "c9", "type": "trigger_ota"})
        ack = self._last_ack()
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("verifying", ack["error"])

    def test_raw_json_payload_from_broker(self):
        self.dev._on_mqtt_command(b'{"cmdId":"c10","type":"set-interval",'
                                  b'"params":{"pollingSeconds":15}}')
        self.assertEqual(self.dev.backend_cfg.ingest_interval_s, 15)
        self.assertEqual(self._last_ack()["status"], "ok")

    def test_broken_payload_still_acks(self):
        self.dev._on_mqtt_command("{broken")
        self.assertEqual(self._last_ack()["status"], "failed")

    def test_set_scenario_is_marked_sim_only(self):
        self.dev._on_mqtt_command({"cmdId": "c11", "type": "set_scenario",
                                   "params": {"scenario": "overheat"}})
        self.assertEqual(self.dev.state.scenario, "overheat")
        self.assertEqual(self._last_ack()["status"], "ok")
        self.assertIn("sim-only", self._last_ack()["error"])


# ──────────────────────────────── heartbeat ─────────────────────────────────────────────────
class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.queue_depth = 7
        self.hb = Heartbeat(http=self.http, firmware_version_getter=lambda: "1.0.0-sim",
                            queue_depth_getter=lambda: self.queue_depth,
                            rssi_getter=lambda: -55,
                            iso_now=lambda: "2026-01-01T00:00:00Z",
                            boot_ms=0, interval_ms=60000)

    def test_body_fields_match_command_dto(self):
        body = self.hb.build_body()
        self.assertEqual(set(body.keys()),
                         {"DeviceTimestamp", "FirmwareVersion", "Temperature", "MemoryUsageMb",
                          "FreeMemoryPercent", "SignalStrengthDbm", "LocalQueueDepth",
                          "UptimeSeconds", "Cpu", "DiskFreeMb"})
        # Trường KHÔNG có trong `IotDeviceHeartbeatCommand` thì tuyệt đối không được gửi.
        for absent in ("ConnectedSensorCount", "IpAddress", "RssiDbm", "QueuedReadingCount"):
            self.assertNotIn(absent, body)

    def test_memory_usage_is_integer(self):
        """`MemoryUsageMb` là `long?` — System.Text.Json strict-mode từ chối float cho kiểu nguyên."""
        body = self.hb.build_body()
        self.assertIsInstance(body["MemoryUsageMb"], int)
        self.assertNotIsInstance(body["MemoryUsageMb"], bool)
        self.assertGreater(body["MemoryUsageMb"], 0)
        self.assertLess(body["MemoryUsageMb"], 64, "ESP32-S3 N16R8 chỉ có ~8MB PSRAM")

    def test_local_queue_depth_is_real(self):
        """Firmware vẫn hard-code 0; simulator gửi SỐ THẬT vì đó mới là thứ hợp đồng mô tả."""
        self.assertEqual(self.hb.build_body()["LocalQueueDepth"], 7)
        self.queue_depth = 0
        self.assertEqual(self.hb.build_body()["LocalQueueDepth"], 0)

    def test_null_fields_for_hardware_without_cpu_or_disk(self):
        body = self.hb.build_body()
        self.assertIsNone(body["Cpu"])
        self.assertIsNone(body["DiskFreeMb"])

    def test_interval_bounds_on_begin(self):
        self.hb.begin(0)
        self.assertEqual(self.hb.interval_ms, 60000)          # 0 → mặc định
        self.hb.begin(99_999_999)
        self.assertEqual(self.hb.interval_ms, INTERVAL_MAX_MS)

    def test_interval_bounds_on_set(self):
        self.hb.set_interval(1000)
        self.assertEqual(self.hb.interval_ms, INTERVAL_MIN_MS)   # kẹp LÊN 10s
        self.hb.set_interval(99_999_999)
        self.assertEqual(self.hb.interval_ms, INTERVAL_MAX_MS)

    def test_first_heartbeat_waits_five_seconds(self):
        hb = Heartbeat(http=self.http, firmware_version_getter=lambda: "1",
                       queue_depth_getter=lambda: 0, rssi_getter=lambda: -50,
                       iso_now=lambda: "t", boot_ms=0, interval_ms=60000)
        hb._boot_ms = __import__("src.timeutil", fromlist=["monotonic_ms"]).monotonic_ms()
        hb.tick()
        self.assertEqual(self.http.heartbeat_calls, [], "chờ 5s sau boot mới gửi lần đầu")
        hb._boot_ms -= FIRST_TICK_DELAY_MS + 100
        hb.tick()
        self.assertEqual(len(self.http.heartbeat_calls), 1)

    def test_clock_skew_warning_is_surfaced(self):
        self.http.heartbeat_response = ok(200, json_body={
            "isSuccess": True, "data": {"clockSkewWarning": True, "clockSkewSeconds": 620.5}})
        self.hb.send_now()
        self.assertIn("620.5", self.hb.last_clock_skew_warning)


# ──────────────────────────────── đèn trạng thái ────────────────────────────────────────────
class LedTest(unittest.TestCase):
    def test_eight_states_exist(self):
        self.assertEqual(len(LedState), 8)

    def test_palette_matches_firmware(self):
        self.assertEqual(palette_for_state(LedState.ONLINE), (0, 32, 0))
        self.assertEqual(palette_for_state(LedState.QUEUED), (0, 32, 0))
        self.assertEqual(palette_for_state(LedState.OFFLINE), (32, 0, 0))
        self.assertEqual(palette_for_state(LedState.PROVISIONING), (16, 0, 32))
        self.assertEqual(palette_for_state(LedState.WIFI_SEARCHING), (32, 12, 0))

    def test_patterns_match_firmware(self):
        """Online và Queued CÙNG màu xanh — chỉ phân biệt bằng NHÁY, nên kiểu nháy là contract."""
        self.assertIs(pattern_for_state(LedState.ONLINE), LedPattern.SOLID)
        self.assertIs(pattern_for_state(LedState.QUEUED), LedPattern.BLINK)
        self.assertIs(pattern_for_state(LedState.SETUP), LedPattern.BLINK)
        self.assertIs(pattern_for_state(LedState.RECOVERY), LedPattern.ALTERNATE)

    def test_blink_toggles_every_half_period(self):
        self.assertTrue(is_lit(LedState.QUEUED, 0))
        self.assertFalse(is_lit(LedState.QUEUED, 500))
        self.assertTrue(is_lit(LedState.QUEUED, 1000))
        self.assertTrue(is_lit(LedState.ONLINE, 500), "trạng thái sáng đều KHÔNG nháy")


# ──────────────────────────────── định danh + re-provision ──────────────────────────────────
class IdentityValidationTest(unittest.TestCase):
    """GH-749 — `apiKey` đi thẳng vào header `X-Api-Key`; CR/LF là tiêm header HTTP."""

    def test_rejects_control_characters_and_spaces(self):
        self.assertIs(validate_identity_field("abc\r\ndef", 64), IdentityFieldError.INVALID_CHAR)
        self.assertIs(validate_identity_field("has space", 64), IdentityFieldError.INVALID_CHAR)
        self.assertIs(validate_identity_field("tab\there", 64), IdentityFieldError.INVALID_CHAR)

    def test_rejects_empty_and_too_long(self):
        self.assertIs(validate_identity_field("", 64), IdentityFieldError.EMPTY)
        self.assertIs(validate_identity_field(None, 64), IdentityFieldError.EMPTY)
        self.assertIs(validate_identity_field("x" * 65, 64), IdentityFieldError.TOO_LONG)

    def test_accepts_real_values(self):
        self.assertTrue(identity_is_valid("gw-esp32-mvp-001", 64))
        self.assertTrue(identity_is_valid("iotk_p-MbplnKWR1VjEVRgbsFMIeTWZsnhx5e86PNNiEkstU", 128))
        self.assertTrue(identity_is_valid("x" * 64, 64), "đúng 64 ký tự PHẢI hợp lệ")


class ReprovisionPolicyTest(unittest.TestCase):
    """IOT3-44 — hai chốt chặn để không biến sự cố backend thành bão request."""

    def test_below_threshold_does_nothing(self):
        self.assertFalse(should_reprovision_on_auth_failure(4, 5, 0, 0, False))

    def test_at_threshold_first_time_fires(self):
        self.assertTrue(should_reprovision_on_auth_failure(5, 5, 0, 0, False))

    def test_cooldown_blocks_second_attempt(self):
        self.assertFalse(should_reprovision_on_auth_failure(9, 5, 1000, 0, True))
        self.assertTrue(should_reprovision_on_auth_failure(
            9, 5, REPROVISION_COOLDOWN_MS + 1, 0, True))

    def test_cooldown_is_fifteen_minutes(self):
        self.assertEqual(REPROVISION_COOLDOWN_MS, 15 * 60 * 1000)


class DeviceReprovisionTest(unittest.TestCase):
    def test_auth_failures_trigger_reprovision(self):
        h = DeviceHarness(contract=CONTRACT_IOT2)
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        dev._prov_cfg.provisioned = True
        mqtt = FakeMqtt(connected=False)
        mqtt.auth_fail_count = 5
        dev._mqtt = mqtt

        dev._check_mqtt_credential_health(now=0)
        self.assertFalse(dev._provision_done, "phải chạy lại /provision để xin credential mới")
        self.assertEqual(mqtt.auth_fail_count, 0)
        self.assertEqual(dev.state.reprovision_count, 1)
        from src import nvs as nvskeys
        self.assertFalse(dev._nvs.get_bool(nvskeys.KEY_PROVISIONED))

    def test_network_failures_do_not_trigger_reprovision(self):
        """Mất mạng thì chờ là xong — gọi /provision lúc đó chỉ nện thêm vào backend đang hỏng."""
        h = DeviceHarness(contract=CONTRACT_IOT2)
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        mqtt = FakeMqtt(connected=False)
        mqtt.auth_fail_count = 0
        mqtt.consecutive_fail_count = 50
        dev._mqtt = mqtt
        dev._check_mqtt_credential_health(now=0)
        self.assertTrue(dev._provision_done)


# ──────────────────────────────── thời gian ─────────────────────────────────────────────────
class TimeTest(unittest.TestCase):
    def test_iso_format_constant(self):
        self.assertEqual(ISO_FORMAT, "%Y-%m-%dT%H:%M:%SZ")

    def test_clock_skew_shifts_timestamp(self):
        from datetime import datetime
        base = datetime.strptime(iso_now(), ISO_FORMAT)
        skewed = datetime.strptime(iso_now(skew_min=10), ISO_FORMAT)
        self.assertGreaterEqual((skewed - base).total_seconds(), 590)

    def test_iso_now_minus_clamps_to_seven_days(self):
        from datetime import datetime
        far = datetime.strptime(iso_now_minus(999 * 24 * 3600), ISO_FORMAT)
        now = datetime.strptime(iso_now(), ISO_FORMAT)
        self.assertLessEqual((now - far).total_seconds(), 7 * 24 * 3600 + 5)


if __name__ == "__main__":
    unittest.main()
