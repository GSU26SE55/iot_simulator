"""Unit test cho MockBattery + scenario engine. Mọi step() yêu cầu `time_iso` pinned per tick."""
from __future__ import annotations

import unittest

from src.bms import MockBattery, pinned_time_iso
from src.config import BatteryConfig


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
        bat = _make_battery()
        r = bat.step(dt_s=15.0, t_global=0.0, scenario="normal", time_iso=pinned_time_iso())
        self.assertEqual(r.source_type, 1)
        self.assertEqual(r.sensor_source_code, "primary")
        self.assertGreater(r.voltage, 10.0)
        self.assertLess(r.voltage, 14.5)
        self.assertGreater(r.temperature, 0.0)
        self.assertLess(r.temperature, 60.0)
        self.assertIsNone(r.bms_error_code)
        # ChargingState in valid range
        self.assertIn(r.charging_state, {1, 2, 3, 4, 5})

    def test_overheat_eventually_exceeds_threshold(self):
        bat = _make_battery()
        peak = 0.0
        for i in range(40):
            r = bat.step(15.0, i * 15.0, "overheat", pinned_time_iso())
            peak = max(peak, r.temperature)
        self.assertGreater(peak, 50.0, f"overheat không kích hoạt, peak={peak}")

    def test_low_soc_drops_under_threshold(self):
        bat = _make_battery()
        bat.soc = 30.0
        end_soc = 100.0
        for i in range(60):
            r = bat.step(15.0, i * 15.0, "low_soc", pinned_time_iso())
            end_soc = r.soc_percent
        self.assertLess(end_soc, 20.0, f"low_soc không tụt, end={end_soc}")

    def test_overvoltage_pushes_voltage_high(self):
        bat = _make_battery()
        peak = 0.0
        for i in range(50):
            r = bat.step(15.0, i * 15.0, "overvoltage", pinned_time_iso())
            peak = max(peak, r.voltage)
        self.assertGreater(peak, 14.4)

    def test_undervoltage_drops_voltage_low(self):
        bat = _make_battery()
        bat.soc = 25.0
        trough = 100.0
        for i in range(60):
            r = bat.step(15.0, i * 15.0, "undervoltage", pinned_time_iso())
            trough = min(trough, r.voltage)
        self.assertLess(trough, 10.5)

    def test_rapid_discharge_current(self):
        bat = _make_battery()
        r = bat.step(15.0, 0.0, "rapid_discharge", pinned_time_iso())
        self.assertLess(r.current, -10.0)

    def test_abnormal_charging_current(self):
        bat = _make_battery()
        r = bat.step(15.0, 0.0, "abnormal_charging", pinned_time_iso())
        self.assertGreater(r.current, 10.0)

    def test_soh_degradation(self):
        bat = _make_battery()
        last = 0.0
        for i in range(200):
            r = bat.step(15.0, i * 15.0, "soh_degradation", pinned_time_iso())
            last = r.soh_percent
        self.assertLess(last, 80.0, f"soh_degradation không tụt, end={last}")

    def test_bms_error_scenario_sets_code(self):
        bat = _make_battery()
        r = bat.step(15.0, 0.0, "bms_error", pinned_time_iso())
        self.assertEqual(r.bms_error_code, "OVT-PROTECT")
        self.assertLessEqual(len(r.bms_error_code or ""), 64)

    def test_pinned_timestamp_preserved(self):
        bat = _make_battery()
        t = "2026-06-12T10:00:00Z"
        r = bat.step(15.0, 0.0, "normal", t)
        self.assertEqual(r.time_iso, t)

    def test_battery_asset_id_propagated(self):
        bat = _make_battery()
        r = bat.step(15.0, 0.0, "normal", pinned_time_iso())
        self.assertEqual(r.battery_asset_id, "22222222-2222-2222-2222-222222222200")


if __name__ == "__main__":
    unittest.main()
