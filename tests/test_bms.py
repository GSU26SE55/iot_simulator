"""Mô hình pin + bộ kịch bản. `step()` trả thẳng `payload.SensorReading` — đúng struct mà
firmware dùng cho mọi nguồn đo (`core/reading.h`), nên không còn lớp chuyển đổi trung gian.
"""
from __future__ import annotations

import unittest

from src.bms import MockBattery
from src.config import BatteryConfig
from src.payload import SOURCE_CODE_PRIMARY, ChargingState, SourceType


def _make_battery() -> MockBattery:
    return MockBattery(BatteryConfig(
        serial="BAT-TEST-001",
        unit_id=1,
        nominal_voltage=12.8,
        nominal_capacity_ah=100,
        initial_soc=80.0,
        initial_soh=95.0,
        cycle_count=100,
        chemistry="LiFePO4",
        battery_asset_id="22222222-2222-2222-2222-222222222200",
    ))


class BmsScenarioTest(unittest.TestCase):

    def test_normal_within_safe_range(self):
        r = _make_battery().step(dt_s=15.0, t_global=0.0, scenario="normal")
        self.assertEqual(r.source_type, SourceType.BMS)
        self.assertEqual(r.sensor_source_code, SOURCE_CODE_PRIMARY)
        self.assertGreater(r.voltage, 10.0)
        self.assertLess(r.voltage, 14.5)
        self.assertGreater(r.temperature, 0.0)
        self.assertLess(r.temperature, 60.0)
        self.assertEqual(r.bms_error_code, "")
        self.assertFalse(r.has_bms_error)
        self.assertIn(int(r.charging_state), {1, 2, 3, 4, 5})

    def test_bms_always_reports_soh_and_charging_state(self):
        r = _make_battery().step(15.0, 0.0, "normal")
        self.assertTrue(r.has_soh)
        self.assertTrue(r.has_charging_state)

    def test_overheat_eventually_exceeds_threshold(self):
        bat = _make_battery()
        peak = max(bat.step(15.0, i * 15.0, "overheat").temperature for i in range(40))
        self.assertGreater(peak, 50.0, f"overheat không kích hoạt, peak={peak}")

    def test_low_soc_drops_under_threshold(self):
        bat = _make_battery()
        bat.soc = 30.0
        end_soc = 100.0
        for i in range(60):
            end_soc = bat.step(15.0, i * 15.0, "low_soc").soc_percent
        self.assertLess(end_soc, 20.0, f"low_soc không tụt, end={end_soc}")

    def test_overvoltage_pushes_voltage_high(self):
        bat = _make_battery()
        peak = max(bat.step(15.0, i * 15.0, "overvoltage").voltage for i in range(50))
        self.assertGreater(peak, 14.4)

    def test_undervoltage_drops_voltage_low(self):
        bat = _make_battery()
        bat.soc = 25.0
        trough = min(bat.step(15.0, i * 15.0, "undervoltage").voltage for i in range(60))
        self.assertLess(trough, 10.5)

    def test_rapid_discharge_current(self):
        r = _make_battery().step(15.0, 0.0, "rapid_discharge")
        self.assertLess(r.current, -10.0)

    def test_abnormal_charging_current(self):
        r = _make_battery().step(15.0, 0.0, "abnormal_charging")
        self.assertGreater(r.current, 10.0)

    def test_soh_degradation(self):
        bat = _make_battery()
        last = 0.0
        for i in range(200):
            last = bat.step(15.0, i * 15.0, "soh_degradation").soh_percent
        self.assertLess(last, 80.0, f"soh_degradation không tụt, end={last}")

    def test_bms_error_scenario_sets_code(self):
        r = _make_battery().step(15.0, 0.0, "bms_error")
        self.assertEqual(r.bms_error_code, "OVT-PROTECT")
        self.assertTrue(r.has_bms_error)
        self.assertLessEqual(len(r.bms_error_code), 64)

    def test_battery_identity_propagated(self):
        r = _make_battery().step(15.0, 0.0, "normal")
        self.assertEqual(r.battery_asset_id, "22222222-2222-2222-2222-222222222200")
        self.assertEqual(r.serial, "BAT-TEST-001")

    def test_charging_state_discharging_when_current_negative(self):
        r = _make_battery().step(15.0, 0.0, "low_soc")
        self.assertEqual(r.charging_state, ChargingState.DISCHARGING)

    def test_charging_state_charging_when_current_positive(self):
        bat = _make_battery()
        bat.soc = 50.0
        r = bat.step(15.0, 0.0, "abnormal_charging")
        self.assertEqual(r.charging_state, ChargingState.CHARGING)

    def test_values_stay_inside_backend_outlier_bounds(self):
        """Backend đếm outlier theo dải vật lý và >50/giờ thì tự KHOÁ thiết bị.
        Kịch bản `normal` tuyệt đối không được sinh ra giá trị ngoài dải."""
        bat = _make_battery()
        for i in range(200):
            r = bat.step(5.0, i * 5.0, "normal")
            self.assertGreater(r.voltage, 0.0)
            self.assertLessEqual(r.voltage, 1000.0)
            self.assertGreaterEqual(r.temperature, -50.0)
            self.assertLessEqual(r.temperature, 150.0)
            self.assertLessEqual(abs(r.current), 1000.0)
            self.assertGreaterEqual(r.soc_percent, 0.0)
            self.assertLessEqual(r.soc_percent, 100.0)
            self.assertGreaterEqual(r.soh_percent, 0.0)
            self.assertLessEqual(r.soh_percent, 100.0)


if __name__ == "__main__":
    unittest.main()
