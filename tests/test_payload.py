"""Hình dạng payload gửi backend — khớp `core/payload.cpp` của firmware.

Đây là bộ test QUAN TRỌNG NHẤT: mọi thứ khác có thể sai mà vẫn chạy được, riêng payload sai là
backend hoặc từ chối (400) hoặc — tệ hơn — nhận rồi lặng lẽ bỏ.
"""
from __future__ import annotations

import re
import unittest

from src.bms import MockBattery
from src.config import SensorDrift
from src.payload import (SOURCE_CODE_EXTERNAL_TEMP, SOURCE_CODE_PRIMARY,
                         SOURCE_CODE_REDUNDANT, SensorReading, SourceType,
                         build_legacy_batch_payload, build_production_batch_payload,
                         filter_out_published, group_by_serial, patch_item_timestamp)
from src.sensors.redundant import make_ds18b20_reading, make_ina226_reading
from src.timeutil import ISO_FORMAT, iso_now

from tests.fakes import make_device_cfg

# Firmware `net::isoNow` sinh CHÍNH XÁC dạng này; `patchItemTimestamp` chỉ thêm đúng 3 chữ số ms.
ISO_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ISO_WITH_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _bms_reading() -> SensorReading:
    cfg = make_device_cfg().batteries[0]
    return MockBattery(cfg).step(dt_s=15.0, t_global=0.0, scenario="normal")


class TimestampFormatTest(unittest.TestCase):
    """`iso_now` PHẢI cho ra độ phân giải GIÂY — không có phần lẻ, không có offset `+00:00`."""

    def test_iso_now_is_second_resolution_with_z(self):
        self.assertRegex(iso_now(), ISO_SECONDS)
        self.assertEqual(ISO_FORMAT, "%Y-%m-%dT%H:%M:%SZ")

    def test_patch_item_timestamp_appends_index_ms(self):
        base = "2026-06-13T08:15:42Z"
        self.assertEqual(patch_item_timestamp(base, 0), "2026-06-13T08:15:42.000Z")
        self.assertEqual(patch_item_timestamp(base, 7), "2026-06-13T08:15:42.007Z")
        self.assertEqual(patch_item_timestamp(base, 123), "2026-06-13T08:15:42.123Z")

    def test_patch_wraps_at_1000(self):
        self.assertEqual(patch_item_timestamp("2026-06-13T08:15:42Z", 1001),
                         "2026-06-13T08:15:42.001Z")

    def test_patch_leaves_unknown_format_alone(self):
        # Thà giữ nguyên còn hơn cắt xén một chuỗi lạ.
        self.assertEqual(patch_item_timestamp("khong-phai-iso", 3), "khong-phai-iso")
        self.assertEqual(patch_item_timestamp("", 3), "")


class ProductionPayloadTest(unittest.TestCase):
    """Contract Sprint 3 — `buildProductionBatchPayload` + `SensorReadingItem` của backend."""

    def setUp(self):
        self.iso = "2026-06-13T08:15:42Z"
        self.bms = _bms_reading()

    def test_items_envelope_without_wrapper(self):
        payload = build_production_batch_payload([self.bms], self.iso, "esp32-sim-001")
        self.assertIn("items", payload)
        self.assertNotIn("DeviceTimestamp", payload)   # KHÔNG có wrapper — backend chỉ bind Items
        self.assertNotIn("Readings", payload)
        self.assertEqual(len(payload), 1)

    def test_bms_item_fields_camelcase(self):
        item = build_production_batch_payload([self.bms], self.iso, "dev")["items"][0]
        for key in ("batteryAssetSerial", "time", "deviceTimestamp", "voltage", "current",
                    "temperature", "socPercent", "cycleCount", "sourceType",
                    "sensorSourceCode", "sohPercent", "chargingState"):
            self.assertIn(key, item, f"thiếu trường {key}")
        for key in ("BatteryAssetSerial", "SourceType", "Voltage", "sourceDeviceId"):
            self.assertNotIn(key, item)
        self.assertEqual(item["sourceType"], int(SourceType.BMS))
        self.assertEqual(item["sensorSourceCode"], SOURCE_CODE_PRIMARY)
        self.assertEqual(item["deviceTimestamp"], item["time"])   # #IoT2-15
        self.assertRegex(item["time"], ISO_WITH_MS)

    def test_no_bms_error_code_when_healthy(self):
        item = build_production_batch_payload([self.bms], self.iso, "dev")["items"][0]
        self.assertNotIn("bmsErrorCode", item)

    def test_bms_error_code_present_and_capped(self):
        cfg = make_device_cfg().batteries[0]
        r = MockBattery(cfg).step(dt_s=15.0, t_global=0.0, scenario="bms_error")
        item = build_production_batch_payload([r], self.iso, "dev")["items"][0]
        self.assertEqual(item["bmsErrorCode"], "OVT-PROTECT")
        self.assertLessEqual(len(item["bmsErrorCode"]), 64)

    def test_prefers_serial_over_asset_id(self):
        r = SensorReading(serial="BAT-X", battery_asset_id="guid-here")
        item = build_production_batch_payload([r], self.iso, "dev")["items"][0]
        self.assertEqual(item["batteryAssetSerial"], "BAT-X")
        self.assertNotIn("batteryAssetId", item)

    def test_falls_back_to_asset_id_when_no_serial(self):
        r = SensorReading(serial="", battery_asset_id="22222222-2222-2222-2222-222222222201")
        item = build_production_batch_payload([r], self.iso, "dev")["items"][0]
        self.assertEqual(item["batteryAssetId"], "22222222-2222-2222-2222-222222222201")
        self.assertNotIn("batteryAssetSerial", item)

    def test_millisecond_index_makes_time_unique(self):
        """Khoá chính hypertable là (Time, BatteryAssetId) — 3 nguồn cùng pin PHẢI khác Time."""
        ina = make_ina226_reading(self.bms, SensorDrift(), "normal")
        ds = make_ds18b20_reading(self.bms, SensorDrift(), "normal")
        items = build_production_batch_payload([self.bms, ina, ds], self.iso, "dev")["items"]
        times = [i["time"] for i in items]
        self.assertEqual(times, ["2026-06-13T08:15:42.000Z",
                                 "2026-06-13T08:15:42.001Z",
                                 "2026-06-13T08:15:42.002Z"])
        self.assertEqual(len(set(times)), 3)

    def test_cross_source_tags(self):
        ina = make_ina226_reading(self.bms, SensorDrift(), "normal")
        ds = make_ds18b20_reading(self.bms, SensorDrift(), "normal")
        items = build_production_batch_payload([self.bms, ina, ds], self.iso, "dev")["items"]
        self.assertEqual({i["sourceType"] for i in items}, {1, 2})
        self.assertEqual({i["sensorSourceCode"] for i in items},
                         {SOURCE_CODE_PRIMARY, SOURCE_CODE_REDUNDANT,
                          SOURCE_CODE_EXTERNAL_TEMP})

    def test_gateway_has_no_bms_only_fields(self):
        ds = make_ds18b20_reading(self.bms, SensorDrift(), "normal")
        item = build_production_batch_payload([ds], self.iso, "dev")["items"][0]
        for key in ("sohPercent", "chargingState", "bmsErrorCode"):
            self.assertNotIn(key, item, f"cảm biến ngoài KHÔNG biết {key}")

    def test_gateway_voltage_never_zero(self):
        """⚠ Bẫy auto-decommission: backend coi voltage ∉ (0,1000] là outlier và >50/giờ thì KHOÁ
        thiết bị. DS18B20 chỉ đo nhiệt nên phải SAO CHÉP điện áp của BMS, không được gửi 0.0."""
        ds = make_ds18b20_reading(self.bms, SensorDrift(), "normal")
        item = build_production_batch_payload([ds], self.iso, "dev")["items"][0]
        self.assertGreater(item["voltage"], 0.0)
        self.assertEqual(item["voltage"], self.bms.voltage)
        ina = make_ina226_reading(self.bms, SensorDrift(), "normal")
        ina_item = build_production_batch_payload([ina], self.iso, "dev")["items"][0]
        self.assertGreater(ina_item["voltage"], 0.0)
        self.assertGreaterEqual(ina_item["temperature"], -50.0)
        self.assertLessEqual(ina_item["temperature"], 150.0)
        self.assertGreaterEqual(ina_item["socPercent"], 0.0)
        self.assertLessEqual(ina_item["socPercent"], 100.0)

    def test_empty_or_invalid_input_returns_none(self):
        self.assertIsNone(build_production_batch_payload([], self.iso, "dev"))
        self.assertIsNone(build_production_batch_payload([self.bms], "", "dev"))
        self.assertIsNone(build_production_batch_payload([self.bms], self.iso, ""))


class LegacyPayloadTest(unittest.TestCase):
    """Contract Sprint 1 — `buildLegacyBatchPayload` (NI §7.4)."""

    def setUp(self):
        self.iso = "2026-06-13T08:15:42Z"
        self.bms = _bms_reading()

    def test_only_legacy_fields(self):
        item = build_legacy_batch_payload([self.bms], self.iso, "dev")["items"][0]
        self.assertEqual(set(item.keys()),
                         {"batteryAssetId", "time", "voltage", "current", "temperature",
                          "socPercent", "cycleCount"})
        self.assertRegex(item["batteryAssetId"], GUID)
        self.assertRegex(item["time"], ISO_WITH_MS)

    def test_no_production_fields(self):
        item = build_legacy_batch_payload([self.bms], self.iso, "dev")["items"][0]
        for key in ("batteryAssetSerial", "deviceTimestamp", "sourceType",
                    "sensorSourceCode", "sohPercent", "chargingState", "bmsErrorCode",
                    "sourceDeviceId"):
            self.assertNotIn(key, item)


class ReadingFilterTest(unittest.TestCase):
    """GH-740 — loại reading đã publish qua MQTT khỏi payload fallback HTTPS."""

    def test_filters_published_serials(self):
        a = SensorReading(serial="BAT-A")
        b = SensorReading(serial="BAT-B")
        c = SensorReading(serial="BAT-C")
        out = filter_out_published([a, b, c], ["BAT-A", "BAT-C"])
        self.assertEqual([r.serial for r in out], ["BAT-B"])

    def test_empty_published_keeps_all(self):
        readings = [SensorReading(serial="BAT-A"), SensorReading(serial="BAT-B")]
        self.assertEqual(len(filter_out_published(readings, [])), 2)

    def test_blank_serial_is_kept(self):
        """Thà gửi thừa một bản ghi không định danh được còn hơn làm mất nó."""
        readings = [SensorReading(serial=""), SensorReading(serial="BAT-A")]
        out = filter_out_published(readings, ["BAT-A"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].serial, "")


class GroupBySerialTest(unittest.TestCase):
    def test_groups_preserve_first_seen_order(self):
        readings = [SensorReading(serial="B"), SensorReading(serial="A"),
                    SensorReading(serial="B")]
        groups = group_by_serial(readings)
        self.assertEqual([s for s, _ in groups], ["B", "A"])
        self.assertEqual(len(groups[0][1]), 2)

    def test_blank_serial_excluded_from_mqtt_path(self):
        # Topic `<prefix>//telemetry` bị ACL của broker từ chối trong im lặng.
        groups = group_by_serial([SensorReading(serial=""), SensorReading(serial="A")])
        self.assertEqual([s for s, _ in groups], ["A"])

    def test_per_group_millisecond_restarts_at_zero(self):
        """Firmware dựng payload RIÊNG cho từng pin ⇒ index ms đánh lại từ 0 trong mỗi nhóm."""
        iso = "2026-06-13T08:15:42Z"
        readings = [SensorReading(serial="A"), SensorReading(serial="A"),
                    SensorReading(serial="B")]
        groups = group_by_serial(readings)
        first = build_production_batch_payload(groups[0][1], iso, "dev")["items"]
        second = build_production_batch_payload(groups[1][1], iso, "dev")["items"]
        self.assertEqual([i["time"] for i in first],
                         ["2026-06-13T08:15:42.000Z", "2026-06-13T08:15:42.001Z"])
        self.assertEqual([i["time"] for i in second], ["2026-06-13T08:15:42.000Z"])


class SensorMismatchTest(unittest.TestCase):
    """Scenario `sensor_mismatch` phải VƯỢT ngưỡng cross-source của backend."""

    def test_voltage_mismatch_exceeds_threshold(self):
        bms = _bms_reading()
        ina = make_ina226_reading(bms, SensorDrift(), "sensor_mismatch")
        self.assertGreater(abs(bms.voltage - ina.voltage), 0.5)

    def test_temperature_mismatch_exceeds_threshold(self):
        bms = _bms_reading()
        ds = make_ds18b20_reading(bms, SensorDrift(), "sensor_mismatch")
        self.assertGreater(abs(bms.temperature - ds.temperature), 5.0)

    def test_normal_stays_inside_threshold(self):
        bms = _bms_reading()
        ina = make_ina226_reading(bms, SensorDrift(), "normal")
        ds = make_ds18b20_reading(bms, SensorDrift(), "normal")
        self.assertLess(abs(bms.voltage - ina.voltage), 0.5)
        self.assertLess(abs(bms.temperature - ds.temperature), 5.0)


if __name__ == "__main__":
    unittest.main()
