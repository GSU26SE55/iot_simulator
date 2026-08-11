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
            elif kind == "environmental_incident":
                self.assertTrue(c.get("incident"), f"case {c['id']} thiếu khối `incident`")
            else:
                self.assertTrue(c.get("reading"), f"case {c['id']} thiếu reading")

    def test_every_kind_is_known_to_the_runner(self):
        """Kind lạ sẽ rơi vào nhánh `sensor_reading` và hỏng theo kiểu khó hiểu."""
        known = {"sensor_reading", "cross_source", "ambient", "environmental_incident", "manual"}
        for c in self.cases:
            self.assertIn(c.get("kind", "sensor_reading"), known, f"case {c['id']}")

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
        # Case 5/6 CỐ Ý vượt dải điện áp (Overvoltage/Undervoltage); case `dangerous` CỐ Ý gửi
        # giá trị phi vật lý. Mọi case còn lại phải nằm trong dải của đúng loại pin.
        intentional = {5, 6}
        for c in self.ds["cases"]:
            if c.get("kind", "sensor_reading") != "sensor_reading":
                continue
            if c["id"] in intentional or c.get("dangerous"):
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


class EnvironmentalIncidentCaseTest(unittest.TestCase):
    """Case sự cố môi trường (AnomalyType 14) — cảnh báo CẤP SITE, không qua bộ quét ngưỡng."""

    # `EnvironmentalIncidentTypeEnum` của backend.
    VALID_INCIDENT_TYPES = {1, 2, 3, 4, 5, 99}
    VALID_SEVERITIES = {1, 2, 3}

    def setUp(self):
        self.ds = _dataset()
        self.cases = [c for c in self.ds["cases"] if c.get("kind") == "environmental_incident"]

    def test_at_least_one_case_exists(self):
        self.assertGreaterEqual(len(self.cases), 1)

    def test_incident_fields_match_backend_enums(self):
        for c in self.cases:
            inc = c["incident"]
            self.assertIn(inc["incident_type"], self.VALID_INCIDENT_TYPES, f"case {c['id']}")
            self.assertIn(inc["severity"], self.VALID_SEVERITIES, f"case {c['id']}")
            self.assertLessEqual(len(inc.get("notes") or ""), 1000, f"case {c['id']}")

    def test_incident_types_are_distinct(self):
        """Trùng loại thì case sau bị backend dùng lại sự cố của case trước ⇒ demo mất một case."""
        types = [c["incident"]["incident_type"] for c in self.cases]
        self.assertEqual(len(types), len(set(types)))

    def test_avoids_types_already_open_in_seed_data(self):
        """Site Long An có sẵn sự cố mở loại 1 (Smoke) và 5 (OverheatHazard) từ dữ liệu seed.

        Dùng lại hai loại đó thì backend trả 200 "reused" và KHÔNG có cảnh báo mới — case coi như
        chết. Ba loại 2/3/4 mới là loại còn trống.
        """
        for c in self.cases:
            self.assertNotIn(c["incident"]["incident_type"], {1, 5},
                             f"case {c['id']} dùng loại đã có sự cố mở trong dữ liệu seed")

    def test_case_declares_no_battery(self):
        """Cảnh báo cấp site — gắn pin vào là sai ngữ nghĩa và bộ chạy cũng không dùng tới."""
        for c in self.cases:
            self.assertIsNone(c.get("battery"), f"case {c['id']}")

    def test_runner_builds_payload_matching_backend_dto(self):
        runner = _runner()
        runner._provision["ESP32-SIM-001"] = {
            "site_id": "11111111-1111-1111-1111-111111111111", "serials": [], "polling_s": 10}
        from src.anomaly import CaseResult
        case = self.cases[0]
        r = CaseResult(case_id=case["id"], anomaly="EnvironmentalIncident", severity="Critical",
                       status="OK")
        runner._run_incident(case, r)
        import json
        payload = json.loads(r.detail)
        self.assertEqual(set(payload.keys()),
                         {"siteId", "incidentType", "severity", "reportedBy", "detectedAt", "notes"})
        self.assertIsInstance(payload["incidentType"], int)
        self.assertIsInstance(payload["severity"], int)
        self.assertEqual(payload["reportedBy"], "ESP32-SIM-001")
        self.assertRegex(payload["detectedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_detected_at_is_never_in_the_future(self):
        """Backend từ chối `DetectedAt > now + 5 phút`; bộ chạy lùi 1 giây cho chắc."""
        from datetime import datetime, timezone
        from src.anomaly import CaseResult
        runner = _runner()
        runner._provision["ESP32-SIM-001"] = {
            "site_id": "11111111-1111-1111-1111-111111111111", "serials": [], "polling_s": 10}
        r = CaseResult(case_id=0, anomaly="x", severity="Critical", status="OK")
        runner._run_incident(self.cases[0], r)
        import json
        stamp = datetime.strptime(json.loads(r.detail)["detectedAt"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(stamp.replace(tzinfo=timezone.utc), datetime.now(timezone.utc))


class ConflictAndDangerTest(unittest.TestCase):
    """Hai cơ chế bảo vệ người demo khỏi hai kiểu 'im lặng không có gì xảy ra'."""

    def setUp(self):
        self.ds = _dataset()
        self.cases = self.ds["cases"]
        self.by_id = {c["id"]: c for c in self.cases}

    # ── conflicts_with ────────────────────────────────────────────────────────────────────
    def test_conflicts_point_at_an_existing_case(self):
        for c in self.cases:
            if c.get("conflicts_with"):
                self.assertIn(c["conflicts_with"], self.by_id, f"case {c['id']}")

    def test_conflicting_pair_shares_anomaly_and_kind(self):
        """Chỉ những case cùng `(site, loại)` mới thật sự khử trùng lẫn nhau."""
        for c in self.cases:
            other_id = c.get("conflicts_with")
            if not other_id:
                continue
            other = self.by_id[other_id]
            self.assertEqual(c["anomaly"], other["anomaly"], f"case {c['id']} ⟷ {other_id}")
            self.assertEqual(c.get("kind"), other.get("kind"), f"case {c['id']} ⟷ {other_id}")

    def test_conflicting_pair_uses_different_severities(self):
        """Lý do tồn tại của cặp này là để demo NỐT mức nghiêm trọng còn thiếu."""
        for c in self.cases:
            other_id = c.get("conflicts_with")
            if other_id:
                self.assertNotEqual(c.get("severity"), self.by_id[other_id].get("severity"))

    def test_resolver_drops_the_conflicting_case_when_both_selected(self):
        from src.anomaly import _resolve_conflicts
        kept, dropped = _resolve_conflicts([self.by_id[17], self.by_id[24]])
        self.assertEqual([c["id"] for c in kept], [17])
        self.assertEqual([(c["id"], other) for c, other in dropped], [(24, 17)])

    def test_resolver_keeps_the_case_when_run_alone(self):
        from src.anomaly import _resolve_conflicts
        kept, dropped = _resolve_conflicts([self.by_id[24]])
        self.assertEqual([c["id"] for c in kept], [24])
        self.assertEqual(dropped, [])

    # ── dangerous ─────────────────────────────────────────────────────────────────────────
    def test_dangerous_case_documents_recovery(self):
        for c in self.cases:
            if not c.get("dangerous"):
                continue
            danger = str(c.get("danger") or "")
            self.assertTrue(danger, f"case {c['id']} thiếu khối `danger`")
            self.assertIn("UPDATE iot_devices", danger,
                          f"case {c['id']} phải kèm lệnh khôi phục thiết bị")
            self.assertIn("status = 2", danger, f"case {c['id']}")

    def test_dangerous_case_uses_a_spare_device(self):
        """KHÔNG được làm hỏng thiết bị demo chính — nó bị khoá 401 vĩnh viễn sau đó."""
        default_device = self.ds["defaults"]["device_code"]
        for c in self.cases:
            if c.get("dangerous"):
                self.assertNotEqual(c.get("device_code"), default_device, f"case {c['id']}")
                self.assertTrue(c.get("device_code"), f"case {c['id']} phải chỉ rõ device_code")

    def test_dangerous_case_held_back_by_default(self):
        from src.anomaly import _filter_dangerous
        safe, held = _filter_dangerous(self.cases, include_dangerous=False)
        self.assertTrue(held, "phải có ít nhất một case nguy hiểm bị giữ lại")
        self.assertTrue(all(not c.get("dangerous") for c in safe))
        self.assertEqual(len(safe) + len(held), len(self.cases))

    def test_dangerous_case_runs_only_with_explicit_flag(self):
        from src.anomaly import _filter_dangerous
        safe, held = _filter_dangerous(self.cases, include_dangerous=True)
        self.assertEqual(held, [])
        self.assertEqual(len(safe), len(self.cases))


class DataIntegrityCaseTest(unittest.TestCase):
    """Case 26 — con đường duy nhất tới `IotDataIntegrityViolation`."""

    # Ngưỡng outlier hard-code trong `BatchIngestSensorReadingsCommandHandler`.
    MAX_VOLTAGE = 1000
    OUTLIER_THRESHOLD_PER_HOUR = 50

    def setUp(self):
        self.ds = _dataset()
        self.case = next(c for c in self.ds["cases"]
                         if c["anomaly"] == "IotDataIntegrityViolation")

    def test_value_is_an_outlier_by_backend_rules(self):
        self.assertGreater(self.case["reading"]["voltage"], self.MAX_VOLTAGE)

    def test_value_still_passes_request_validation(self):
        """`ValidateAsync` từ chối CẢ batch với 400 nếu `Voltage < 0`.

        Giá trị âm không bao giờ tới được bộ đếm outlier — chỉ giá trị dương quá lớn mới lọt qua
        kiểm tra cú pháp rồi bị handler đếm là outlier. Đây là chi tiết quyết định case chạy được
        hay không.
        """
        r = self.case["reading"]
        self.assertGreater(r["voltage"], 0)
        self.assertGreaterEqual(r["cycle_count"], 0)
        self.assertIn(r["charging_state"], {1, 2, 3, 4, 5})

    def test_repeat_exceeds_hourly_outlier_threshold(self):
        self.assertGreater(self.case["repeat"], self.OUTLIER_THRESHOLD_PER_HOUR)

    def test_single_wave_because_noise_suppression_does_not_apply(self):
        """Luật này nằm trong handler ingest, không đi qua bộ chống nhiễu ⇒ chia đợt là vô nghĩa."""
        self.assertTrue(self.case.get("bypass_noise"))
        self.assertEqual(_runner().waves_for(self.case), [self.case["repeat"]])

    def test_batch_size_within_backend_limit(self):
        """Backend từ chối batch > 1000 item."""
        self.assertLessEqual(self.case["repeat"], 1000)

    def test_millisecond_patch_keeps_timestamps_unique_across_the_batch(self):
        """60 item cùng một giây — nếu ms không đánh index thì đụng khoá chính và bị đếm là
        `duplicate` thay vì `outlier`, và luật sẽ không bao giờ kích hoạt."""
        from src.payload import build_production_batch_payload
        readings = [build_reading(self.case["reading"], "BAT-X")
                    for _ in range(self.case["repeat"])]
        items = build_production_batch_payload(readings, "2026-08-10T10:00:00Z", "dev")["items"]
        times = [i["time"] for i in items]
        self.assertEqual(len(set(times)), len(times))


class SingleShotRoutingTest(unittest.TestCase):
    """Ambient và sự cố môi trường KHÔNG gắn với pin ⇒ không được đi qua bước kiểm quyền pin."""

    def test_kinds_declared(self):
        from src.anomaly import SINGLE_SHOT_KINDS
        self.assertEqual(SINGLE_SHOT_KINDS, {"ambient", "environmental_incident"})

    def test_single_shot_cases_have_no_battery(self):
        for c in _dataset()["cases"]:
            from src.anomaly import SINGLE_SHOT_KINDS
            if c.get("kind") in SINGLE_SHOT_KINDS:
                self.assertIsNone(c.get("battery"), f"case {c['id']}")


class DeviceOfflineDocTest(unittest.TestCase):
    """Case 15 phải trỏ vào đường DeviceOffline còn SỐNG, không phải hàm chết."""

    def setUp(self):
        self.case = next(c for c in _dataset()["cases"] if c["anomaly"] == "DeviceOffline")

    def test_notes_flag_the_dead_code_path(self):
        note = str(self.case.get("note") or "")
        self.assertIn("IotDeviceOfflineDetectionService", note)
        self.assertIn("AnomalyRules.DetectOffline", note)
        self.assertIn("CODE CHẾT", note)

    def test_instructions_query_the_live_table(self):
        instructions = str(self.case.get("instructions") or "")
        self.assertIn("iot_devices", instructions)
        self.assertIn("iot_device_id", instructions,
                      "câu SQL phải join theo thiết bị — alert của đường sống KHÔNG có battery_asset_id")


class EnumCoverageTest(unittest.TestCase):
    """Bất biến cuối: dataset phải phủ TOÀN BỘ `AnomalyTypeEnum` của backend."""

    def test_every_backend_anomaly_type_has_a_case(self):
        covered = {c["anomaly"] for c in _dataset()["cases"]}
        missing = sorted(set(ANOMALY_TYPE_IDS) - covered)
        self.assertEqual(missing, [], f"còn thiếu case cho: {missing}")

    def test_no_case_references_an_unknown_anomaly(self):
        known = set(ANOMALY_TYPE_IDS) | {"bms_error_code"}
        for c in _dataset()["cases"]:
            self.assertIn(c["anomaly"], known, f"case {c['id']}")

    def test_both_severities_covered_where_backend_can_emit_both(self):
        """Loại nào backend sinh được cả Warning lẫn Critical thì dataset phải có đủ hai."""
        both = {"Overheat", "Undertemp", "LowSoc", "SohDegradation",
                "HighAmbientTemp", "HighHumidity"}
        by_anomaly: dict[str, set] = {}
        for c in _dataset()["cases"]:
            by_anomaly.setdefault(c["anomaly"], set()).add(c.get("severity"))
        for name in both:
            self.assertEqual(by_anomaly.get(name), {"Warning", "Critical"},
                             f"{name} phải có cả hai mức, đang có {by_anomaly.get(name)}")


if __name__ == "__main__":
    unittest.main()
