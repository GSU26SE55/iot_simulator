"""Provision — đọc ĐỦ response, không chỉ 4 trường đầu.

Ba nhóm quan trọng:
  1. Biên + từ chối envelope hỏng (khớp `provision.cpp`).
  2. Sáu trường MQTT (IOT3-42) — thứ bản simulator cũ bỏ qua hoàn toàn.
  3. `batteryMappings[]` (IOT3-49) — nguyên nhân gốc của "NHẬN THIẾU" (GH-748).
"""
from __future__ import annotations

import unittest

from src.battery_map import BatteryMapEntry, decode_battery_map, encode_battery_map
from src.config import CONTRACT_IOT2
from src.net_rules import derive_topic_prefix
from src.provision import (parse_battery_mappings, parse_mqtt_settings,
                           parse_provision_response)

from tests.fakes import DeviceHarness, make_device_cfg, ok


def _full_response(**overrides) -> dict:
    data = {
        "deviceId": "44444444-4444-4444-4444-444444444444",
        "deviceCode": "esp32-sim-001",
        "siteId": "99999999-9999-9999-9999-999999999999",
        "pollingIntervalSeconds": 10,
        "heartbeatIntervalSeconds": 120,
        "ntpServer": "time.cloudflare.com",
        "mqttBrokerHost": "broker.local",
        "mqttBrokerPort": 8883,
        "mqttUseTls": True,
        "mqttTopicPrefix": "solar/esp32-sim-001",
        "mqttUsername": "esp32-sim-001",
        "mqttPassword": "mqtt-secret-123",
        "batteryMappings": [
            {"batteryAssetSerial": "BAT-T-001", "unitId": 1, "sensorSourceCode": "primary"},
            {"batteryAssetSerial": "BAT-NEW-9", "unitId": 2, "sensorSourceCode": "primary"},
        ],
    }
    data.update(overrides)
    return {"isSuccess": True, "statusCode": 200, "data": data}


class EnvelopeTest(unittest.TestCase):
    def test_accepts_full_response(self):
        r = parse_provision_response(_full_response())
        self.assertTrue(r.ok)
        self.assertEqual(r.polling_interval_s, 10)
        self.assertEqual(r.heartbeat_interval_s, 120)
        self.assertEqual(r.site_id, "99999999-9999-9999-9999-999999999999")
        self.assertEqual(r.ntp_server, "time.cloudflare.com")

    def test_rejects_is_success_false(self):
        """2xx nhưng `isSuccess=false` là backend nói 'thiết bị chưa được phép'.

        Coi đó là thành công thì thiết bị chạy tiếp với cấu hình mặc định và gửi rác lên backend.
        """
        r = parse_provision_response({"isSuccess": False, "message": "device revoked"})
        self.assertFalse(r.ok)
        self.assertIn("device revoked", r.error)

    def test_rejects_missing_is_success(self):
        r = parse_provision_response({"data": {"pollingIntervalSeconds": 10}})
        self.assertFalse(r.ok)

    def test_rejects_missing_data(self):
        r = parse_provision_response({"isSuccess": True})
        self.assertFalse(r.ok)

    def test_rejects_non_dict(self):
        self.assertFalse(parse_provision_response(None).ok)
        self.assertFalse(parse_provision_response("nope").ok)


class BoundsTest(unittest.TestCase):
    """Khớp CHÍNH XÁC `provision.cpp` §3 'Sanity bounds'."""

    def test_polling_below_min_falls_back_to_default(self):
        r = parse_provision_response(_full_response(pollingIntervalSeconds=0))
        self.assertEqual(r.polling_interval_s, 5)

    def test_polling_above_max_clamps_to_max_not_default(self):
        r = parse_provision_response(_full_response(pollingIntervalSeconds=100000))
        self.assertEqual(r.polling_interval_s, 600)

    def test_heartbeat_below_min_falls_back_to_default(self):
        r = parse_provision_response(_full_response(heartbeatIntervalSeconds=5))
        self.assertEqual(r.heartbeat_interval_s, 60)

    def test_heartbeat_above_max_clamps(self):
        r = parse_provision_response(_full_response(heartbeatIntervalSeconds=99999))
        self.assertEqual(r.heartbeat_interval_s, 3600)

    def test_missing_intervals_use_firmware_defaults(self):
        body = _full_response()
        body["data"].pop("pollingIntervalSeconds")
        body["data"].pop("heartbeatIntervalSeconds")
        r = parse_provision_response(body)
        self.assertEqual(r.polling_interval_s, 5)
        self.assertEqual(r.heartbeat_interval_s, 60)


class MqttSettingsTest(unittest.TestCase):
    """IOT3-42 — sáu trường MQTT."""

    def test_reads_all_six_fields(self):
        s = parse_mqtt_settings(_full_response()["data"])
        self.assertEqual(s["host"], "broker.local")
        self.assertEqual(s["port"], 8883)
        self.assertTrue(s["use_tls"])
        self.assertEqual(s["prefix"], "solar/esp32-sim-001")
        self.assertEqual(s["username"], "esp32-sim-001")
        self.assertEqual(s["password"], "mqtt-secret-123")

    def test_empty_host_means_mqtt_disabled(self):
        """`mqttBrokerHost` rỗng = backend tắt MQTT ⇒ KHÔNG được xoá cấu hình đã có."""
        self.assertIsNone(parse_mqtt_settings(_full_response(mqttBrokerHost="")["data"]))
        data = _full_response()["data"]
        data.pop("mqttBrokerHost")
        self.assertIsNone(parse_mqtt_settings(data))


class BatteryMappingsTest(unittest.TestCase):
    """IOT3-49."""

    def test_reads_valid_entries(self):
        entries, skipped, present = parse_battery_mappings(_full_response()["data"])
        self.assertTrue(present)
        self.assertEqual(skipped, 0)
        self.assertEqual([e.serial for e in entries], ["BAT-T-001", "BAT-NEW-9"])
        self.assertEqual(entries[1].unit_id, 2)

    def test_missing_array_reports_not_present(self):
        data = _full_response()["data"]
        data.pop("batteryMappings")
        entries, skipped, present = parse_battery_mappings(data)
        self.assertFalse(present)
        self.assertEqual(entries, [])

    def test_invalid_entries_are_skipped_and_counted(self):
        data = _full_response(batteryMappings=[
            {"batteryAssetSerial": "", "unitId": 1},                      # serial rỗng
            {"batteryAssetSerial": "BAT-OK-2", "unitId": 300},            # unitId ngoài dải
            {"batteryAssetSerial": "BAT,BAD", "unitId": 3},               # ký tự phân cách
            {"batteryAssetSerial": "BAT-GOOD", "unitId": 7},
        ])["data"]
        entries, skipped, present = parse_battery_mappings(data)
        self.assertTrue(present)
        self.assertEqual([e.serial for e in entries], ["BAT-GOOD"])
        self.assertEqual(skipped, 3)

    def test_null_unit_id_is_accepted_because_backend_sends_null(self):
        """⚠ Backend THẬT trả `"unitId": null` cho mọi mapping (đã kiểm trực tiếp trên
        `POST /api/iot-devices/provision`). Firmware loại sạch bảng vì bắt buộc unitId ∈ [1,247]
        — nên lại quay về bảng cứng, đúng lớp lỗi IOT3-49 sinh ra để chặn. Simulator không nói
        Modbus nên `unitId` chỉ là nhãn; bỏ pin backend đã giao chỉ vì thiếu nhãn là sai."""
        data = _full_response(batteryMappings=[
            {"batteryAssetSerial": "BAT-2026-001", "unitId": None, "sensorSourceCode": "primary"},
            {"batteryAssetSerial": "BAT-2026-003", "unitId": None, "sensorSourceCode": "primary"},
        ])["data"]
        entries, skipped, present = parse_battery_mappings(data)
        self.assertTrue(present)
        self.assertEqual(skipped, 0)
        self.assertEqual([e.serial for e in entries], ["BAT-2026-001", "BAT-2026-003"])
        self.assertEqual([e.unit_id for e in entries], [1, 2], "cấp số thứ tự thay thế")

    def test_zero_unit_id_treated_as_absent(self):
        data = _full_response(batteryMappings=[
            {"batteryAssetSerial": "BAT-X", "unitId": 0},
        ])["data"]
        entries, skipped, _ = parse_battery_mappings(data)
        self.assertEqual(skipped, 0)
        self.assertEqual(entries[0].unit_id, 1)

    def test_caps_at_eight_entries_and_counts_overflow(self):
        data = _full_response(batteryMappings=[
            {"batteryAssetSerial": f"BAT-{i}", "unitId": i + 1} for i in range(12)
        ])["data"]
        entries, skipped, _ = parse_battery_mappings(data)
        self.assertEqual(len(entries), 8)
        self.assertEqual(skipped, 4)

    def test_codec_roundtrip_matches_firmware_format(self):
        entries = [BatteryMapEntry("BAT-A", 1, "primary"),
                   BatteryMapEntry("BAT-B", 247, "primary")]
        encoded = encode_battery_map(entries)
        self.assertEqual(encoded, "BAT-A,1,primary;BAT-B,247,primary")
        decoded = decode_battery_map(encoded)
        self.assertEqual([(e.serial, e.unit_id) for e in decoded], [("BAT-A", 1), ("BAT-B", 247)])

    def test_codec_skips_broken_row_instead_of_dropping_table(self):
        decoded = decode_battery_map("BAT-A,1,primary;HONG;BAT-B,2,primary")
        self.assertEqual([e.serial for e in decoded], ["BAT-A", "BAT-B"])


class DeviceProvisionFlowTest(unittest.TestCase):
    """Luồng đầy đủ trên `SimulatedDevice` — đúng đường mà vòng lặp thật đi."""

    def setUp(self):
        self.h = DeviceHarness(contract=CONTRACT_IOT2)
        self.addCleanup(self.h.close)

    def test_applies_everything_from_response(self):
        self.h.http.provision_response = ok(200, json_body=_full_response())
        self.h.device._ensure_provisioned(now=0)

        dev = self.h.device
        self.assertTrue(dev._provision_done)
        self.assertEqual(dev.backend_cfg.ingest_interval_s, 10)
        self.assertEqual(dev.backend_cfg.heartbeat_interval_s, 120)
        self.assertEqual(dev._heartbeat.interval_s, 120)
        self.assertEqual(dev.cfg.site_id_guid, "99999999-9999-9999-9999-999999999999")
        self.assertEqual(dev.cfg.ntp_server, "time.cloudflare.com")

        # siteId được nối vào CẢ SHT31 lẫn reporter sự cố — thiếu nó thì backend trả 400.
        self.assertEqual(dev._sht31.site_id, "99999999-9999-9999-9999-999999999999")
        self.assertEqual(dev._env_reporter.site_id, "99999999-9999-9999-9999-999999999999")

        # Credential MQTT do backend cấp.
        self.assertEqual(dev._mqtt_runtime.host, "broker.local")
        self.assertEqual(dev._mqtt_runtime.port, 8883)
        self.assertTrue(dev._mqtt_runtime.want_tls)
        self.assertEqual(dev._mqtt_runtime.username, "esp32-sim-001")
        self.assertEqual(dev._mqtt_runtime.password, "mqtt-secret-123")
        self.assertEqual(dev._mqtt_runtime.topic_prefix(), "solar/esp32-sim-001")
        self.assertTrue(dev._mqtt_runtime.is_configured())

        # Bảng pin của backend thay bảng seed → thiết bị gửi đúng tập pin nó được giao.
        self.assertEqual(sorted(dev._batteries.keys()), ["BAT-NEW-9", "BAT-T-001"])

    def test_state_persists_so_second_boot_skips_provision(self):
        self.h.http.provision_response = ok(200, json_body=_full_response())
        self.h.device._ensure_provisioned(now=0)
        self.assertEqual(len(self.h.http.provision_calls), 1)

        # "Khởi động lại": dựng thiết bị mới trên CÙNG state dir.
        from src.device import SimulatedDevice
        from tests.fakes import FakeHttp, make_backend, make_mqtt_cfg
        http2 = FakeHttp()
        dev2 = SimulatedDevice(make_device_cfg(), make_backend(), make_mqtt_cfg(),
                               queue_dir=self.h.root / "queue", state_dir=self.h.root / "state",
                               http=http2)
        dev2._ensure_provisioned(now=0)
        self.assertTrue(dev2._provision_done)
        self.assertEqual(http2.provision_calls, [], "boot thứ hai KHÔNG được gọi lại /provision")
        self.assertEqual(dev2.backend_cfg.ingest_interval_s, 10)
        self.assertEqual(dev2._mqtt_runtime.password, "mqtt-secret-123")

    def test_failed_provision_retries_after_30s_not_every_tick(self):
        self.h.http.provision_response = ok(503, body="backend down")
        dev = self.h.device
        dev._ensure_provisioned(now=0)
        self.assertFalse(dev._provision_done)
        self.assertEqual(len(self.h.http.provision_calls), 1)

        dev._ensure_provisioned(now=1000)           # 1s sau — chưa tới hạn
        self.assertEqual(len(self.h.http.provision_calls), 1)

        dev._ensure_provisioned(now=30001)          # sau 30s — thử lại
        self.assertEqual(len(self.h.http.provision_calls), 2)

    def test_ingest_blocked_until_provisioned(self):
        """Firmware chặn ingest sau `s_provisionDone` — thiết bị chưa đăng ký thì KHÔNG gửi gì."""
        self.h.http.provision_response = ok(401, body="unknown device")
        dev = self.h.device
        dev._last_ingest_ms = -999999
        dev._loop_body()
        self.assertEqual(self.h.http.ingest_calls, [])

    def test_topic_prefix_derives_from_device_code_when_backend_omits_it(self):
        body = _full_response()
        body["data"].pop("mqttTopicPrefix")
        self.h.http.provision_response = ok(200, json_body=body)
        self.h.device._ensure_provisioned(now=0)
        self.assertEqual(self.h.device._mqtt_runtime.topic_prefix(),
                         derive_topic_prefix("esp32-sim-001"))

    def test_mqtt_settings_rejected_when_port_invalid(self):
        self.h.http.provision_response = ok(200, json_body=_full_response(mqttBrokerPort=0))
        self.h.device._ensure_provisioned(now=0)
        # Provision vẫn thành công (các trường khác hợp lệ) nhưng cấu hình MQTT bị TỪ CHỐI
        # nguyên khối — ghi nửa vời còn tệ hơn không ghi.
        self.assertTrue(self.h.device._provision_done)
        self.assertNotEqual(self.h.device._mqtt_runtime.host, "broker.local")


if __name__ == "__main__":
    unittest.main()
