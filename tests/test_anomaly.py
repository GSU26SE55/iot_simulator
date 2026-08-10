"""Bộ chạy dataset anomaly — kiểm phần logic thuần + hình dạng payload thật sự đi ra dây.

Ba nhóm bất biến, mỗi nhóm ứng với một lỗi ĐÃ QUAN SÁT ĐƯỢC khi chạy trên backend thật:
  · chia đợt      — gửi một lượt thì bộ chống nhiễu luôn ra `effectiveCount = 1` ⇒ 0 cảnh báo;
  · ô thời gian   — hai case cùng pin, cùng giây ⇒ đụng khoá `(Time, BatteryAssetId)`;
  · trường Tier 2 — backend nhận `internalResistanceMilliohm`/`cellVoltageDeltaMv`.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import yaml

from src.anomaly import ANOMALY_TYPE_IDS, AnomalyRunner, build_reading
from src.config import CONTRACT_IOT2
from src.payload import ChargingState, SourceType, build_production_batch_payload

from tests.fakes import make_backend, make_device_cfg, make_mqtt_cfg

DATASET_PATH = "config/anomaly-dataset.yaml"


def _sim_cfg():
    from src.config import SimulatorConfig
    from pathlib import Path
    return SimulatorConfig(
        backend=make_backend(CONTRACT_IOT2), mqtt=make_mqtt_cfg(),
        devices=[make_device_cfg(device_code="ESP32-SIM-001")],
        queue_dir=Path("/tmp"), state_dir=Path("/tmp"), log_level="WARNING")


def _runner(dataset: dict | None = None) -> AnomalyRunner:
    return AnomalyRunner(_sim_cfg(), dataset if dataset is not None else _dataset(),
                         dry_run=True)


def _dataset() -> dict:
    with open(DATASET_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class DatasetIntegrityTest(unittest.TestCase):
    """Dataset phải tự nhất quán — sai ở đây là demo chạy ra kết quả khác mô tả."""

    def setUp(self):
        self.ds = _dataset()
        self.cases = self.ds["cases"]

    def test_ids_unique(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_anomaly_name_exists_in_backend_enum(self):
        for c in self.cases:
            name = c["anomaly"]
            if name == "bms_error_code":
                continue          # cố ý KHÔNG phải AnomalyType — xem ghi chú của case 20
            self.assertIn(name, ANOMALY_TYPE_IDS, f"case {c['id']}: '{name}' không có trong enum")

    def test_every_case_has_payload_or_instructions(self):
        for c in self.cases:
            kind = c.get("kind", "sensor_reading")
            if kind == "manual":
                self.assertTrue(c.get("instructions"), f"case {c['id']} thiếu instructions")
            elif kind == "cross_source":
                self.assertGreaterEqual(len(c.get("readings") or []), 2, f"case {c['id']}")
            else:
                self.assertTrue(c.get("reading"), f"case {c['id']} thiếu reading")

    def test_sensor_reading_cases_declare_battery(self):
        for c in self.cases:
            if c.get("kind", "sensor_reading") in ("sensor_reading", "cross_source"):
                self.assertTrue(c.get("battery"), f"case {c['id']} thiếu battery")

    def test_meta_gap_exceeds_scan_interval(self):
        """Đợt 2 phải cách đợt 1 hơn một nhịp quét, nếu không breach đợt 1 chưa kịp ghi."""
        meta = self.ds["meta"]
        self.assertGreater(meta["wave_gap_s"], meta["scan_interval_s"])

    def test_meta_cross_source_isolation_exceeds_pairing_window(self):
        meta = self.ds["meta"]
        self.assertGreater(meta["cross_source_isolation_s"],
                           meta["cross_source_pairing_window_s"])

    def test_default_repeat_exceeds_noise_count(self):
        self.assertGreater(self.ds["defaults"]["repeat"],
                           self.ds["meta"]["noise_suppression_count"])

    def test_cross_source_pair_avoids_redundant_code(self):
        """Backend BỎ so sánh nhiệt với nguồn `redundant` (NS-09) — dùng nó là case chết."""
        for c in self.cases:
            if c.get("kind") != "cross_source":
                continue
            codes = [r.get("sensor_source_code") for r in c["readings"]]
            self.assertNotIn("redundant", codes, f"case {c['id']}")
            self.assertIn(1, [r.get("source_type") for r in c["readings"]])
            self.assertIn(2, [r.get("source_type") for r in c["readings"]])


class WaveSplitTest(unittest.TestCase):
    """Chia đợt — bất biến quan trọng nhất của bộ chạy."""

    def setUp(self):
        self.runner = _runner()

    def test_default_case_splits_into_five_then_rest(self):
        """Đợt 1 phải đúng `noise_count` (5) để đợt 2 đẩy `effectiveCount` lên 6 ≥ 5 ngay lượt
        quét đầu tiên của nó."""
        self.assertEqual(self.runner.waves_for({"id": 1}), [5, 2])

    def test_bypass_noise_case_uses_single_wave(self):
        # Overheat Critical được backend miễn luật chống nhiễu → chờ 14s là phí.
        self.assertEqual(self.runner.waves_for({"id": 2, "repeat": 1, "bypass_noise": True}), [1])

    def test_repeat_one_uses_single_wave(self):
        self.assertEqual(self.runner.waves_for({"repeat": 1}), [1])

    def test_small_repeat_never_makes_empty_second_wave(self):
        for repeat in (2, 3, 4, 5, 6):
            waves = self.runner.waves_for({"repeat": repeat})
            self.assertEqual(sum(waves), repeat)
            self.assertTrue(all(w > 0 for w in waves), repeat)


class TimeSlotTest(unittest.TestCase):
    """Ô thời gian — chống đụng khoá chính `(Time, BatteryAssetId)`."""

    def setUp(self):
        self.runner = _runner()

    def test_same_battery_gets_distinct_seconds(self):
        a = self.runner._base_time("BAT-A")
        b = self.runner._base_time("BAT-A")
        self.assertNotEqual(a, b)
        self.assertEqual((a - b).total_seconds(), 1.0)

    def test_different_batteries_may_share_a_second(self):
        # Khoá chính gồm cả pin nên hai pin khác nhau dùng chung một giây là hợp lệ.
        self.assertEqual(self.runner._base_time("BAT-A"), self.runner._base_time("BAT-B"))

    def test_slots_never_run_into_the_future(self):
        now = datetime.now(timezone.utc)
        for _ in range(5):
            self.assertLessEqual(self.runner._base_time("BAT-A"), now)

    def test_rebase_resets_slots(self):
        self.runner._base_time("BAT-A")
        self.runner.rebase_clock()
        self.assertEqual(self.runner._slots, {})


class BuildReadingTest(unittest.TestCase):
    def test_maps_all_fields(self):
        r = build_reading({
            "voltage": 15.2, "current": -3.5, "temperature": 62.0, "soc_percent": 15,
            "soh_percent": 82, "cycle_count": 250, "charging_state": 3,
            "bms_error_code": "OVT-PROTECT", "internal_resistance_milliohm": 65,
            "cell_voltage_delta_mv": 135,
        }, "BAT-X")
        self.assertEqual(r.serial, "BAT-X")
        self.assertEqual(r.voltage, 15.2)
        self.assertEqual(r.charging_state, ChargingState.DISCHARGING)
        self.assertEqual(r.source_type, SourceType.BMS)
        self.assertTrue(r.has_soh)
        self.assertTrue(r.has_charging_state)
        self.assertTrue(r.has_bms_error)
        self.assertEqual(r.internal_resistance_milliohm, 65.0)
        self.assertEqual(r.cell_voltage_delta_mv, 135.0)

    def test_absent_optionals_are_not_flagged(self):
        r = build_reading({"voltage": 13.0, "temperature": 30.0, "soc_percent": 60}, "BAT-X")
        self.assertFalse(r.has_soh)
        self.assertFalse(r.has_charging_state)
        self.assertFalse(r.has_bms_error)
        self.assertIsNone(r.internal_resistance_milliohm)
        self.assertIsNone(r.cell_voltage_delta_mv)

    def test_defaults_to_primary_bms(self):
        r = build_reading({"voltage": 13.0}, "BAT-X")
        self.assertEqual(r.sensor_source_code, "primary")
        self.assertEqual(r.source_type, SourceType.BMS)

    def test_cross_source_second_reading_is_gateway(self):
        r = build_reading({"source_type": 2, "sensor_source_code": "external-temp",
                           "voltage": 13.4, "temperature": 36.5, "soh_percent": None}, "BAT-X")
        self.assertEqual(r.source_type, SourceType.IOT_GATEWAY)
        self.assertEqual(r.sensor_source_code, "external-temp")
        self.assertFalse(r.has_soh, "`soh_percent: null` KHÔNG được gửi lên")


class Tier2PayloadTest(unittest.TestCase):
    """Trường Tier 2 — backend CÓ nhận, firmware thì không gửi."""

    def test_tier2_fields_serialised_when_present(self):
        r = build_reading({"voltage": 12.8, "internal_resistance_milliohm": 65,
                           "cell_voltage_delta_mv": 135}, "BAT-X")
        item = build_production_batch_payload([r], "2026-08-10T10:00:00Z", "dev")["items"][0]
        self.assertEqual(item["internalResistanceMilliohm"], 65.0)
        self.assertEqual(item["cellVoltageDeltaMv"], 135.0)

    def test_tier2_absent_by_default_so_normal_run_matches_firmware(self):
        from src.bms import MockBattery
        r = MockBattery(make_device_cfg().batteries[0]).step(5.0, 0.0, "normal")
        item = build_production_batch_payload([r], "2026-08-10T10:00:00Z", "dev")["items"][0]
        self.assertNotIn("internalResistanceMilliohm", item)
        self.assertNotIn("cellVoltageDeltaMv", item)
        self.assertNotIn("sourceDeviceId", item)


class CaseValuesCrossThresholdTest(unittest.TestCase):
    """Từng case phải THỰC SỰ vượt đúng ngưỡng mà nó tuyên bố.

    Ngưỡng lấy từ `threshold_configs` của backend đang chạy (xem khối `thresholds` trong dataset).
    Sai một con số ở đây là case im lặng không sinh cảnh báo nào.
    """

    def setUp(self):
        self.ds = _dataset()
        self.by_id = {c["id"]: c for c in self.ds["cases"]}
        self.thr = self.ds["thresholds"]

    def _r(self, case_id: int) -> dict:
        return self.by_id[case_id]["reading"]

    def test_overheat_warning_above_max_but_below_critical_delta(self):
        t_max = self.thr["LiFePO4-12V-100Ah"]["temperature"][1]
        temp = self._r(1)["temperature"]
        self.assertGreater(temp, t_max)
        self.assertLess(temp, t_max + 5, "delta Critical THỰC TẾ là 5°C, không phải 8°C")

    def test_overheat_critical_above_delta(self):
        t_max = self.thr["LiFePO4-12V-100Ah"]["temperature"][1]
        self.assertGreater(self._r(2)["temperature"], t_max + 5)

    def test_undertemp_thresholds(self):
        t_min = self.thr["LiFePO4-12V-100Ah"]["temperature"][0]
        self.assertLess(self._r(3)["temperature"], t_min)
        self.assertGreater(self._r(3)["temperature"], t_min - 5)
        self.assertLess(self._r(4)["temperature"], t_min - 5)

    def test_voltage_cases(self):
        v_min, v_max = self.thr["LiFePO4-12V-100Ah"]["voltage"]
        self.assertGreater(self._r(5)["voltage"], v_max)
        self.assertLess(self._r(6)["voltage"], v_min)
        self.assertGreater(self._r(6)["voltage"], 0, "V ≤ 0 sẽ bị tính là outlier, không phải anomaly")

    def test_soc_cases(self):
        li = self.thr["LiFePO4-12V-100Ah"]["soc"]
        self.assertLess(self._r(7)["soc_percent"], li["warning"])
        self.assertGreaterEqual(self._r(7)["soc_percent"], li["critical"])
        nmc = self.thr["NMC-48V-200Ah"]["soc"]
        self.assertLess(self._r(8)["soc_percent"], nmc["critical"])

    def test_current_cases_use_the_only_battery_with_current_thresholds(self):
        spec = self.thr["LiFePO4-24V-30Ah"]
        self.assertIsNotNone(spec["current_max_discharge"])
        self.assertIsNotNone(spec["current_max_charge"])
        self.assertEqual(self.by_id[9]["battery"], "BAT-2026-REAL-001")
        self.assertEqual(self.by_id[10]["battery"], "BAT-2026-REAL-001")
        self.assertGreater(abs(self._r(9)["current"]), spec["current_max_discharge"])
        self.assertLess(self._r(9)["current"], 0, "xả phải là dòng ÂM")
        self.assertGreater(self._r(10)["current"], spec["current_max_charge"])

    def test_soh_cases(self):
        soh = self.thr["LiFePO4-12V-100Ah"]["soh"]
        self.assertLess(self._r(11)["soh_percent"], soh["warning"])
        self.assertGreaterEqual(self._r(11)["soh_percent"], soh["critical"])
        self.assertLess(self._r(12)["soh_percent"], self.thr["NMC-48V-200Ah"]["soh"]["critical"])

    def test_battery_voltage_matches_its_own_type_range(self):
        """Pin NMC mà gửi 11V sẽ kích THÊM Undervoltage và làm lẫn kết quả demo."""
        ranges = {
            "BAT-2026-001": self.thr["LiFePO4-12V-100Ah"]["voltage"],
            "BAT-2026-002": self.thr["LiFePO4-12V-100Ah"]["voltage"],
            "BAT-2026-003": self.thr["NMC-48V-200Ah"]["voltage"],
            "BAT-2026-004": self.thr["NMC-48V-200Ah"]["voltage"],
            "BAT-2026-REAL-001": self.thr["LiFePO4-24V-30Ah"]["voltage"],
        }
        # Case 5/6 CỐ Ý vượt dải điện áp; các case còn lại thì không được.
        intentional = {5, 6}
        for c in self.ds["cases"]:
            if c.get("kind", "sensor_reading") != "sensor_reading" or c["id"] in intentional:
                continue
            lo, hi = ranges[c["battery"]]
            v = c["reading"]["voltage"]
            self.assertGreaterEqual(v, lo, f"case {c['id']} ({c['battery']}) V={v} dưới dải")
            self.assertLessEqual(v, hi, f"case {c['id']} ({c['battery']}) V={v} trên dải")

    def test_ambient_cases_cross_the_right_thresholds(self):
        amb = self.thr["ambient"]
        self.assertGreater(self.by_id[17]["reading"]["ambient_temperature"],
                           amb["high_ambient_temp"]["critical"])
        self.assertLess(self.by_id[17]["reading"]["humidity"], amb["combo"]["humidity"],
                        "để không kích thêm luật kết hợp làm lẫn kết quả")
        h18 = self.by_id[18]["reading"]["humidity"]
        self.assertGreater(h18, amb["high_humidity"]["warning"])
        self.assertLess(h18, amb["high_humidity"]["critical"])
        self.assertLess(self.by_id[18]["reading"]["ambient_temperature"], amb["combo"]["temp"])
        self.assertGreaterEqual(self.by_id[19]["reading"]["ambient_temperature"],
                                amb["combo"]["temp"])
        self.assertGreaterEqual(self.by_id[19]["reading"]["humidity"], amb["combo"]["humidity"])

    def test_cross_source_deltas_exceed_both_thresholds(self):
        a, b = self.by_id[16]["readings"]
        self.assertGreater(abs(a["voltage"] - b["voltage"]), 0.5)
        self.assertGreater(abs(a["temperature"] - b["temperature"]), 5.0)

    def test_bms_error_case_stays_inside_every_threshold(self):
        """Case 20 chỉ kiểm trường `bmsErrorCode` — nó KHÔNG được kích bất kỳ luật nào."""
        r = self._r(20)
        thr = self.thr["LiFePO4-12V-100Ah"]
        self.assertGreater(r["voltage"], thr["voltage"][0])
        self.assertLess(r["voltage"], thr["voltage"][1])
        self.assertGreater(r["temperature"], thr["temperature"][0])
        self.assertLess(r["temperature"], thr["temperature"][1])
        self.assertGreaterEqual(r["soc_percent"], thr["soc"]["warning"])
        self.assertGreaterEqual(r["soh_percent"], thr["soh"]["warning"])
        self.assertLessEqual(len(r["bms_error_code"]), 64)


if __name__ == "__main__":
    unittest.main()
