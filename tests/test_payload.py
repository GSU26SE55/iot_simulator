"""Verify payload structure khớp 2 contract version:
  - current        — api-battery.md TODAY (items[].batteryAssetId Guid + sourceDeviceId)
  - iot2-production — Sprint IoT-2 #IoT2-14 (DeviceTimestamp wrapper + BatteryAssetSerial + SourceType + Idempotency)
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.bms import MockBattery, pinned_time_iso
from src.config import (CONTRACT_CURRENT, CONTRACT_IOT2, BackendConfig,
                        BatteryConfig, DeviceConfig, MqttConfig, SensorDrift,
                        SensorToggles)
from src.device import SimulatedDevice
from src.sensors.ambient import make_ambient_reading
from src.sensors.environmental import (INC_FLOOD, INC_SMOKE, SEV_CRITICAL,
                                        make_smoke_incident,
                                        make_water_leak_incident)
from src.sensors.redundant import make_ds18b20_reading, make_ina226_reading

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _backend(contract: str = CONTRACT_CURRENT) -> BackendConfig:
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
        device_code="ESP32-TEST-001",
        site_id_guid="11111111-1111-1111-1111-111111111111",
        site_label="site-test",
        firmware_version="1.0.0-test",
        hardware_revision="rev-test",
        model="ESP32-WROOM-S3",
        api_key="iotk_testkey_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        batteries=[BatteryConfig(
            serial="BAT-T-001",
            unit_id=1,
            nominal_voltage=12.8,
            nominal_capacity_ah=100,
            initial_soc=80,
            initial_soh=92,
            cycle_count=100,
            battery_asset_id="22222222-2222-2222-2222-222222222201",
        )],
        sensors=SensorToggles(ina226=True, ds18b20=True),
        scenario="normal",
        sensor_drift=SensorDrift(),
    )


class CurrentContractTest(unittest.TestCase):
    """Contract `current` = endpoint TODAY (api-battery.md §POST /api/sensor-readings/batch)."""

    def test_items_wrapper_no_devicetimestamp(self):
        dev = SimulatedDevice(_device_cfg(), _backend(CONTRACT_CURRENT), _mqtt(),
                              Path("/tmp/iot-sim-test"))
        bat = MockBattery(dev.cfg.batteries[0])
        t = pinned_time_iso()
        bms = bat.step(15, 0, "normal", t)
        payload = dev._build_ingest_payload(t, [dev._bms_to_dict(bms)])
        self.assertIn("items", payload)
        self.assertNotIn("Readings", payload)
        self.assertNotIn("DeviceTimestamp", payload)

    def test_reading_field_names_match_api_battery_md(self):
        dev = SimulatedDevice(_device_cfg(), _backend(CONTRACT_CURRENT), _mqtt(),
                              Path("/tmp/iot-sim-test"))
        t = pinned_time_iso()
        bms = MockBattery(dev.cfg.batteries[0]).step(15, 0, "normal", t)
        d = dev._bms_to_dict(bms)
        # field thật của backend hôm nay
        for k in ("batteryAssetId", "time", "voltage", "current",
                  "temperature", "socPercent", "cycleCount", "sourceDeviceId"):
            self.assertIn(k, d, f"thiếu field {k}")
        # batteryAssetId phải là Guid
        self.assertRegex(d["batteryAssetId"], GUID)
        # KHÔNG có field của contract iot2
        for k in ("BatteryAssetSerial", "SourceType", "SensorSourceCode",
                  "BmsErrorCode", "ChargingState", "SohPercent"):
            self.assertNotIn(k, d, f"không được có field iot2 {k}")
        # ISO8601 Z
        self.assertRegex(d["time"], ISO_Z)


class Iot2ContractTest(unittest.TestCase):
    """Contract `iot2-production` = Sprint IoT-2 #IoT2-14."""

    def test_wrapper_has_devicetimestamp_and_readings(self):
        dev = SimulatedDevice(_device_cfg(), _backend(CONTRACT_IOT2), _mqtt(),
                              Path("/tmp/iot-sim-test"))
        t = pinned_time_iso()
        bms = MockBattery(dev.cfg.batteries[0]).step(15, 0, "normal", t)
        payload = dev._build_ingest_payload(t, [dev._bms_to_dict(bms)])
        self.assertIn("DeviceTimestamp", payload)
        self.assertIn("Readings", payload)

    def test_multi_source_for_cross_validation(self):
        dev = SimulatedDevice(_device_cfg(), _backend(CONTRACT_IOT2), _mqtt(),
                              Path("/tmp/iot-sim-test"))
        t = pinned_time_iso()
        bat = MockBattery(dev.cfg.batteries[0])
        bms = bat.step(15, 0, "normal", t)
        ina = make_ina226_reading(bms, dev.cfg.sensor_drift, "normal")
        ds = make_ds18b20_reading(bms, dev.cfg.sensor_drift, "normal")
        readings = [dev._bms_to_dict(bms), dev._gw_to_dict(ina), dev._gw_to_dict(ds)]
        # phải có ít nhất SourceType=1 (Bms) + SourceType=2 (IotGateway)
        source_types = {r["SourceType"] for r in readings}
        self.assertEqual(source_types, {1, 2})
        # SensorSourceCode khớp MO §52.9
        codes = {r["SensorSourceCode"] for r in readings}
        self.assertEqual(codes, {"primary", "redundant", "external-temp"})

    def test_bms_error_code_max_64_chars(self):
        cfg = _device_cfg()
        dev = SimulatedDevice(cfg, _backend(CONTRACT_IOT2), _mqtt(),
                              Path("/tmp/iot-sim-test"))
        bms = MockBattery(cfg.batteries[0]).step(15, 0, "bms_error", pinned_time_iso())
        d = dev._bms_to_dict(bms)
        self.assertIsNotNone(d["BmsErrorCode"])
        self.assertLessEqual(len(d["BmsErrorCode"]), 64)

    def test_pinned_timestamp_across_sources(self):
        """Cross-source pair §1.6.6 cần BMS + IotGateway cùng phút.
        Pin timestamp per tick → mọi reading cùng battery cùng tick có cùng Time."""
        cfg = _device_cfg()
        dev = SimulatedDevice(cfg, _backend(CONTRACT_IOT2), _mqtt(),
                              Path("/tmp/iot-sim-test"))
        t = pinned_time_iso()
        bms = MockBattery(cfg.batteries[0]).step(15, 0, "normal", t)
        ina = make_ina226_reading(bms, cfg.sensor_drift, "normal")
        ds = make_ds18b20_reading(bms, cfg.sensor_drift, "normal")
        self.assertEqual(bms.time_iso, ina.time_iso)
        self.assertEqual(bms.time_iso, ds.time_iso)


class AmbientPayloadTest(unittest.TestCase):
    """Endpoint /api/ambient/readings/batch (api-battery.md §POST)."""

    def test_ambient_field_names_match(self):
        amb = make_ambient_reading(
            device_code="ESP32-T",
            site_id_guid="11111111-1111-1111-1111-111111111111",
            time_iso=pinned_time_iso(),
            t_global=0.0,
            scenario="normal",
        )
        self.assertEqual(amb.source, 1)                                   # IotSensor=1 (int)
        self.assertRegex(amb.site_id_guid, GUID)
        self.assertRegex(amb.time_iso, ISO_Z)
        self.assertGreaterEqual(amb.ambient_temperature, -90)
        self.assertLessEqual(amb.ambient_temperature, 90)
        self.assertGreaterEqual(amb.humidity, 0)
        self.assertLessEqual(amb.humidity, 100)

    def test_high_ambient_temp_scenario(self):
        amb = make_ambient_reading(
            device_code="ESP32-T",
            site_id_guid="11111111-1111-1111-1111-111111111111",
            time_iso=pinned_time_iso(),
            t_global=0.0,
            scenario="high_ambient_temp",
        )
        self.assertGreater(amb.ambient_temperature, 40.0)                 # > HighAmbientTempCritical

    def test_high_humidity_scenario(self):
        amb = make_ambient_reading(
            device_code="ESP32-T",
            site_id_guid="11111111-1111-1111-1111-111111111111",
            time_iso=pinned_time_iso(),
            t_global=0.0,
            scenario="high_humidity",
        )
        self.assertGreater(amb.humidity, 85.0)                            # > HighHumidityCritical


class EnvironmentalIncidentPayloadTest(unittest.TestCase):
    """Endpoint /api/environmental-incidents (api-battery.md)."""

    def test_smoke_payload_uses_int_enums(self):
        inc = make_smoke_incident("ESP32-T",
                                   "11111111-1111-1111-1111-111111111111",
                                   pinned_time_iso(), adc_value=3100)
        self.assertEqual(inc.incident_type, INC_SMOKE)                    # 1
        self.assertEqual(inc.severity, SEV_CRITICAL)                      # 3
        self.assertRegex(inc.site_id_guid, GUID)
        self.assertRegex(inc.detected_at, ISO_Z)
        self.assertLessEqual(len(inc.notes), 1000)
        self.assertLessEqual(len(inc.reported_by), 256)

    def test_water_leak_maps_to_flood_enum(self):
        inc = make_water_leak_incident("ESP32-T",
                                        "11111111-1111-1111-1111-111111111111",
                                        pinned_time_iso())
        self.assertEqual(inc.incident_type, INC_FLOOD)                    # 4


class SensorMismatchTest(unittest.TestCase):
    def test_sensor_mismatch_breaks_voltage_threshold(self):
        bat = MockBattery(_device_cfg().batteries[0])
        bms = bat.step(15.0, 0.0, "normal", pinned_time_iso())
        ina = make_ina226_reading(bms, SensorDrift(), "sensor_mismatch")
        # §1.6.6: > 0.5V → SensorMismatch
        self.assertGreater(abs(bms.voltage - (ina.voltage or 0)), 0.5)

    def test_sensor_mismatch_breaks_temperature_threshold(self):
        bat = MockBattery(_device_cfg().batteries[0])
        bms = bat.step(15.0, 0.0, "normal", pinned_time_iso())
        ds = make_ds18b20_reading(bms, SensorDrift(), "sensor_mismatch")
        self.assertGreater(abs(bms.temperature - (ds.temperature or 0)), 5.0)


class ChargingStateEnumTest(unittest.TestCase):
    """ChargingStateEnum: Idle=1, Charging=2, Discharging=3, Float=4, Bypass=5."""

    def test_discharging_when_current_negative(self):
        bat = MockBattery(_device_cfg().batteries[0])
        # ép current negative bằng scenario low_soc
        r = bat.step(15.0, 0.0, "low_soc", pinned_time_iso())
        self.assertEqual(r.charging_state, 3)                              # Discharging

    def test_charging_when_current_positive(self):
        bat = MockBattery(_device_cfg().batteries[0])
        bat.soc = 50.0
        r = bat.step(15.0, 0.0, "abnormal_charging", pinned_time_iso())
        self.assertEqual(r.charging_state, 2)                              # Charging


if __name__ == "__main__":
    unittest.main()
