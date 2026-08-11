"""Cảm biến môi trường + ambient — hành vi phải khớp `sensor/*.cpp` của firmware."""
from __future__ import annotations

import unittest

from src.config import CONTRACT_IOT2
from src.sensors.ambient import (AMBIENT_SOURCE_IOT_SENSOR, SHT31_POLL_INTERVAL_MS,
                                 Sht31Sensor, make_ambient_reading)
from src.sensors.environmental import (INC_FIRE_DETECTED, INC_FLOOD, INC_GAS_LEAK,
                                       SEV_CRITICAL, EnvironmentalIncidentReporter,
                                       IncidentReportResult, IncidentSeverity,
                                       IncidentType, make_gas_leak_incident,
                                       make_water_leak_incident)
from src.sensors.fire_watch import FireWatch
from src.sensors.incident_trigger import IncidentTrigger
from src.sensors.mq2 import MQ2_REARM_COOLDOWN_MS, MQ2_THRESHOLD_RAW, MQ2_WARMUP_MS, Mq2Sensor
from src.sensors.water_leak import WaterLeakSensor

from tests.fakes import DeviceHarness, FakeHttp, make_device_cfg, net_error, ok


class _FakeReporter:
    """Ghi lại lời gọi `report()` và trả kết quả theo kịch bản."""

    def __init__(self, result: IncidentReportResult = IncidentReportResult.SUCCESS):
        self.result = result
        self.calls: list[tuple] = []
        self.site_id = "site-guid"

    def report(self, incident_type, severity, notes, detected_at_iso):
        self.calls.append((int(incident_type), int(severity), notes, detected_at_iso))
        return self.result


def _iso_minus(seconds_ago: int) -> str:
    """Trả thẳng số giây để test khẳng định được `detectedAt` lùi đúng bao nhiêu (GH-736)."""
    return f"T-{seconds_ago}"


class IncidentTriggerTest(unittest.TestCase):
    """`sensor::IncidentTrigger`."""

    def test_fires_once_on_rising_edge(self):
        t = IncidentTrigger(cooldown_ms=1000)
        self.assertTrue(t.update(True, 0))
        self.assertFalse(t.update(True, 100), "giữ HIGH liên tục KHÔNG được bắn lại")
        self.assertFalse(t.update(True, 900))

    def test_first_sample_already_active_fires(self):
        # Nước có sẵn lúc bật máy vẫn phải báo.
        self.assertTrue(IncidentTrigger(1000).update(True, 0))

    def test_chattering_inside_cooldown_is_suppressed(self):
        t = IncidentTrigger(cooldown_ms=1000)
        self.assertTrue(t.update(True, 0))
        t.update(False, 100)
        self.assertFalse(t.update(True, 200), "nhấp nháy trong cooldown phải bị chặn")

    def test_rearms_after_cooldown(self):
        t = IncidentTrigger(cooldown_ms=1000)
        self.assertTrue(t.update(True, 0))
        t.update(False, 1500)
        self.assertTrue(t.update(True, 2000), "hết cooldown thì sự cố mới PHẢI báo lại")


class Mq2Test(unittest.TestCase):
    def setUp(self):
        self.rep = _FakeReporter()
        self.mq2 = Mq2Sensor(self.rep, iso_now_minus=_iso_minus)
        self.mq2.begin(0)

    def test_warmup_blocks_detection(self):
        """MQ-2 chưa sấy xong thì đọc ra rác — báo lúc đó là báo động giả."""
        self.mq2.tick(1000, "gas_leak")
        self.mq2.tick(2000, "gas_leak")
        self.assertEqual(self.rep.calls, [])

    def test_reports_gas_leak_not_smoke(self):
        """⚠ MQ-2 → GasLeak(3). `Smoke(1)` dành cho cảm biến khói quang học (NS-24 #664)."""
        self.mq2.tick(MQ2_WARMUP_MS + 1000, "gas_leak")
        self.assertEqual(len(self.rep.calls), 1)
        self.assertEqual(self.rep.calls[0][0], INC_GAS_LEAK)
        self.assertEqual(self.rep.calls[0][1], SEV_CRITICAL)

    def test_smoke_scenario_also_maps_to_gas_leak(self):
        self.mq2.tick(MQ2_WARMUP_MS + 1000, "smoke")
        self.assertEqual(self.rep.calls[0][0], INC_GAS_LEAK)

    def test_normal_scenario_never_reports(self):
        for i in range(20):
            self.mq2.tick(MQ2_WARMUP_MS + 1000 * (i + 1), "normal")
        self.assertEqual(self.rep.calls, [])
        self.assertLess(self.mq2.last_raw, MQ2_THRESHOLD_RAW)

    def test_does_not_spam_while_gas_persists(self):
        base = MQ2_WARMUP_MS + 1000
        for i in range(30):
            self.mq2.tick(base + i * 1000, "gas_leak")
        self.assertEqual(len(self.rep.calls), 1, "giữ ngưỡng liên tục chỉ báo MỘT lần")

    def test_rearms_after_cooldown(self):
        base = MQ2_WARMUP_MS + 1000
        self.mq2.tick(base, "gas_leak")
        self.mq2.tick(base + 1000, "normal")                       # về bình thường
        self.mq2.tick(base + MQ2_REARM_COOLDOWN_MS + 2000, "gas_leak")
        self.assertEqual(len(self.rep.calls), 2, "sự cố lặp lại PHẢI báo lại sau cooldown")

    def test_notes_match_firmware_format(self):
        self.mq2.tick(MQ2_WARMUP_MS + 1000, "gas_leak")
        notes = self.rep.calls[0][2]
        self.assertRegex(notes, r"^MQ-2 raw=\d+ > thr=2000 \(GPIO1\)$")

    def test_transient_failure_retries_with_backoff_not_every_tick(self):
        """GH-741 — 403/5xx retry mỗi tick sinh ~180 request/phút, vô hạn."""
        self.rep.result = IncidentReportResult.TRANSIENT
        base = MQ2_WARMUP_MS + 1000
        self.mq2.tick(base, "gas_leak")
        self.assertEqual(len(self.rep.calls), 1)
        self.mq2.tick(base + 1000, "gas_leak")     # ngay sau đó — bị backoff chặn
        self.assertEqual(len(self.rep.calls), 1)
        self.mq2.tick(base + 60000, "gas_leak")    # sau khi hết backoff
        self.assertEqual(len(self.rep.calls), 2)

    def test_permanent_failure_stops_retrying(self):
        self.rep.result = IncidentReportResult.PERMANENT
        base = MQ2_WARMUP_MS + 1000
        self.mq2.tick(base, "gas_leak")
        for i in range(1, 20):
            self.mq2.tick(base + i * 1000, "gas_leak")
        self.assertEqual(len(self.rep.calls), 1, "lỗi vĩnh viễn thì DỪNG, không nện backend")

    def test_detected_at_uses_detection_time_not_send_time(self):
        """GH-736 — sự cố phát hiện lúc mất mạng chỉ gửi được rất lâu sau đó."""
        self.rep.result = IncidentReportResult.TRANSIENT
        base = MQ2_WARMUP_MS + 1000
        self.mq2.tick(base, "gas_leak")
        self.assertEqual(self.rep.calls[0][3], "T-0")
        self.mq2.tick(base + 60000, "gas_leak")
        self.assertEqual(self.rep.calls[1][3], "T-60", "phải lùi đúng 60s về lúc phát hiện")


class WaterLeakTest(unittest.TestCase):
    def setUp(self):
        self.rep = _FakeReporter()
        self.water = WaterLeakSensor(self.rep, iso_now_minus=_iso_minus)
        self.water.begin(0)

    def test_reports_flood(self):
        self.water.tick(1000, "water_leak")
        self.assertEqual(len(self.rep.calls), 1)
        self.assertEqual(self.rep.calls[0][0], INC_FLOOD)
        self.assertEqual(self.rep.calls[0][1], SEV_CRITICAL)
        self.assertTrue(self.water.is_wet)

    def test_no_warmup_needed(self):
        # Khác MQ-2: cảm biến số, không cần sấy.
        self.water.tick(600, "water_leak")
        self.assertEqual(len(self.rep.calls), 1)

    def test_dry_never_reports(self):
        for i in range(20):
            self.water.tick(600 * (i + 1), "normal")
        self.assertEqual(self.rep.calls, [])
        self.assertFalse(self.water.is_wet)

    def test_rearms_after_cooldown(self):
        self.water.tick(1000, "water_leak")
        self.water.tick(2000, "normal")
        self.water.tick(1000 + 300000 + 2000, "water_leak")
        self.assertEqual(len(self.rep.calls), 2)


class FireWatchTest(unittest.TestCase):
    """⚠ Mở rộng RIÊNG của simulator — firmware KHÔNG có đường báo cháy."""

    def test_requires_both_gas_and_heat(self):
        rep = _FakeReporter()
        fire = FireWatch(rep, iso_now_minus=_iso_minus)
        fire.tick(1000, "fire_detected", mq2_raw=3400, mq2_threshold=2000, battery_temp_c=30.0)
        self.assertEqual(rep.calls, [], "nhiệt thấp thì không báo cháy")
        fire.tick(2000, "overheat", mq2_raw=3400, mq2_threshold=2000, battery_temp_c=90.0)
        self.assertEqual(rep.calls, [], "scenario khác thì không báo cháy")
        fire.tick(3000, "fire_detected", mq2_raw=3400, mq2_threshold=2000, battery_temp_c=90.0)
        self.assertEqual(len(rep.calls), 1)
        self.assertEqual(rep.calls[0][0], INC_FIRE_DETECTED)


class EnvironmentalReporterTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.rep = EnvironmentalIncidentReporter(self.http, "esp32-sim-001")
        self.rep.set_site_id("11111111-1111-1111-1111-111111111111")

    def test_payload_shape(self):
        self.http.incident_response = ok(201)
        self.rep.report(IncidentType.GAS_LEAK, IncidentSeverity.CRITICAL, "note", "2026-01-01T00:00:00Z")
        body = self.http.incident_calls[0]
        self.assertEqual(set(body.keys()),
                         {"siteId", "incidentType", "severity", "reportedBy", "detectedAt",
                          "notes"})
        self.assertEqual(body["incidentType"], 3)
        self.assertEqual(body["severity"], 3)
        self.assertEqual(body["reportedBy"], "esp32-sim-001")
        self.assertIsInstance(body["incidentType"], int)   # backend chỉ nhận SỐ, không nhận chuỗi

    def test_notes_omitted_when_empty(self):
        self.http.incident_response = ok(201)
        self.rep.report(IncidentType.FLOOD, IncidentSeverity.CRITICAL, "", "2026-01-01T00:00:00Z")
        self.assertNotIn("notes", self.http.incident_calls[0])

    def test_missing_site_id_is_transient_not_permanent(self):
        rep = EnvironmentalIncidentReporter(self.http, "dev")
        result = rep.report(IncidentType.FLOOD, IncidentSeverity.CRITICAL, "x", "2026-01-01T00:00:00Z")
        self.assertIs(result, IncidentReportResult.TRANSIENT)
        self.assertEqual(self.http.incident_calls, [], "chưa có siteId thì đừng tốn round-trip")

    def test_403_is_permanent(self):
        """Thiếu scope EnvironmentalIngest → 403; retry vô hạn là bão request."""
        self.http.incident_response = ok(403, body="forbidden")
        result = self.rep.report(IncidentType.GAS_LEAK, IncidentSeverity.CRITICAL, "x",
                                 "2026-01-01T00:00:00Z")
        self.assertIs(result, IncidentReportResult.PERMANENT)
        self.assertEqual(self.rep.dropped_count, 1)

    def test_503_and_network_error_are_transient(self):
        self.http.incident_response = ok(503)
        self.assertIs(self.rep.report(IncidentType.FLOOD, IncidentSeverity.CRITICAL, "x", "t"),
                      IncidentReportResult.TRANSIENT)
        self.http.incident_response = net_error()
        self.assertIs(self.rep.report(IncidentType.FLOOD, IncidentSeverity.CRITICAL, "x", "t"),
                      IncidentReportResult.TRANSIENT)

    def test_200_and_201_both_count_as_success(self):
        for code in (200, 201):
            self.http.incident_response = ok(code)
            self.assertIs(self.rep.report(IncidentType.FLOOD, IncidentSeverity.CRITICAL, "x", "t"),
                          IncidentReportResult.SUCCESS)

    def test_builders_keep_backend_enums(self):
        inc = make_gas_leak_incident("dev", "site", "2026-01-01T00:00:00Z", adc_value=3100)
        self.assertEqual(inc.incident_type, INC_GAS_LEAK)
        self.assertLessEqual(len(inc.notes), 1000)
        self.assertLessEqual(len(inc.reported_by), 256)
        self.assertEqual(make_water_leak_incident("dev", "site", "t").incident_type, INC_FLOOD)


class Sht31Test(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.sensor = Sht31Sensor(self.http, "esp32-sim-001", iso_now=lambda: "2026-01-01T00:00:00Z")
        self.sensor.set_site_id("11111111-1111-1111-1111-111111111111")

    def test_poll_interval_matches_firmware(self):
        self.assertEqual(SHT31_POLL_INTERVAL_MS, 60000)
        self.assertEqual(self.sensor.poll_interval_ms, 60000)

    def test_payload_has_no_solar_irradiance(self):
        """Firmware KHÔNG đo và KHÔNG gửi trường này — gửi thêm là cấp cho dashboard/AI một nguồn
        dữ liệu mà thiết bị thật không bao giờ có."""
        self.http.ambient_response = ok(201)
        self.sensor.post_now(t_global=0.0, scenario="normal")
        item = self.http.ambient_calls[0]["items"][0]
        self.assertEqual(set(item.keys()),
                         {"siteId", "time", "ambientTemperature", "humidity", "source",
                          "sourceDeviceId"})
        self.assertNotIn("solarIrradiance", item)
        self.assertEqual(item["source"], AMBIENT_SOURCE_IOT_SENSOR)
        self.assertIsInstance(item["source"], int)   # enum backend chỉ nhận SỐ

    def test_skips_when_site_id_missing(self):
        s = Sht31Sensor(self.http, "dev", iso_now=lambda: "t")
        self.assertFalse(s.post_now(0.0, "normal"))
        self.assertEqual(self.http.ambient_calls, [])

    def test_tick_respects_interval(self):
        self.http.ambient_response = ok(201)
        self.sensor.tick(0, 0.0, "normal")            # lần đầu gửi ngay
        self.sensor.tick(1000, 0.0, "normal")         # chưa tới hạn
        self.assertEqual(len(self.http.ambient_calls), 1)
        self.sensor.tick(60001, 0.0, "normal")
        self.assertEqual(len(self.http.ambient_calls), 2)

    def test_scenarios_cross_backend_thresholds(self):
        hot = make_ambient_reading("d", "s", "t", 0.0, "high_ambient_temp")
        self.assertGreater(hot.ambient_temperature, 40.0)
        humid = make_ambient_reading("d", "s", "t", 0.0, "high_humidity")
        self.assertGreater(humid.humidity, 85.0)
        combo = make_ambient_reading("d", "s", "t", 0.0, "high_temp_humidity_combo")
        self.assertGreater(combo.ambient_temperature, 40.0)
        self.assertGreater(combo.humidity, 85.0)

    def test_normal_values_stay_in_sensor_range(self):
        for i in range(100):
            r = make_ambient_reading("d", "s", "t", i * 10.0, "normal")
            self.assertGreaterEqual(r.ambient_temperature, -40.0)
            self.assertLessEqual(r.ambient_temperature, 125.0)
            self.assertGreaterEqual(r.humidity, 0.0)
            self.assertLessEqual(r.humidity, 100.0)


class DeviceSensorWiringTest(unittest.TestCase):
    def test_incidents_are_not_queued(self):
        """Firmware KHÔNG xếp sự cố vào hàng đợi — làm thế vừa sai hành vi vừa chiếm chỗ của
        số đo pin trong hàng đợi."""
        cfg = make_device_cfg()
        cfg.sensors.mq2 = True
        h = DeviceHarness(contract=CONTRACT_IOT2, device_cfg=cfg)
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        dev._env_reporter.set_site_id("11111111-1111-1111-1111-111111111111")
        h.http.incident_response = ok(503)
        dev._mq2.begin(0)
        dev._mq2.tick(MQ2_WARMUP_MS + 1000, "gas_leak")
        self.assertEqual(len(h.http.incident_calls), 1)
        self.assertEqual(dev._queue.size(), 0)

    def test_safety_sensors_run_while_offline(self):
        """GH-736 — mất mạng KHÔNG được làm mất mẫu của cảm biến an toàn."""
        cfg = make_device_cfg()
        cfg.sensors.mq2 = True
        cfg.sensors.water_leak = True
        h = DeviceHarness(contract=CONTRACT_IOT2, device_cfg=cfg)
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        dev._env_reporter.set_site_id("11111111-1111-1111-1111-111111111111")
        h.http.ingest_response = net_error()
        h.http.incident_response = net_error()
        dev.state.scenario = "water_leak"
        dev._last_ingest_ms = -10**9
        dev._loop_body()
        self.assertFalse(dev._link.is_up())
        # Mẫu VẪN được lấy và sự cố VẪN được chốt để gửi khi có mạng lại.
        self.assertTrue(dev._water._pending.pending)

    def test_sensors_disabled_on_legacy_contract(self):
        from src.config import CONTRACT_CURRENT
        cfg = make_device_cfg()
        cfg.sensors.mq2 = True
        cfg.sensors.sht31 = True
        cfg.sensors.water_leak = True
        h = DeviceHarness(contract=CONTRACT_CURRENT, device_cfg=cfg)
        self.addCleanup(h.close)
        self.assertFalse(h.device._mq2.enabled)
        self.assertFalse(h.device._sht31.enabled)
        self.assertFalse(h.device._water.enabled)

    def test_legacy_contract_sends_no_heartbeat(self):
        """Backend Sprint 1 KHÔNG có `/api/iot-devices/heartbeat` — gửi vào đó chỉ sinh 401/404
        mỗi phút và làm người đọc log đi tìm một sự cố không có thật."""
        from src.config import CONTRACT_CURRENT
        h = DeviceHarness(contract=CONTRACT_CURRENT)
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        dev._heartbeat._boot_ms -= 10_000        # vượt qua khoảng chờ 5s đầu
        for _ in range(3):
            dev._last_ingest_ms = -10 ** 9
            dev._loop_body()
        self.assertEqual(h.http.heartbeat_calls, [])
        self.assertGreater(len(h.http.ingest_calls), 0, "ingest thì VẪN phải chạy")


if __name__ == "__main__":
    unittest.main()
