"""INA226 (V/I redundant) + DS18B20 (external temp) — sourceType=IotGateway (2).

Cross-source validation (NI §7.6, MO §1.6.6): so cặp reading cùng battery cùng phút giữa
BMS (sourceType=1, primary) và IotGateway (sourceType=2, redundant/external-temp):
  - |V_bms − V_iot| > 0.5V → SensorMismatch (Warning)
  - |T_bms − T_iot| > 5°C  → SensorMismatch (Warning)

KHI scenario=sensor_mismatch → INA226/DS18B20 cố tình lệch BMS lớn hơn ngưỡng trên.

LƯU Ý: `time_iso` được PIN từ caller (cùng tick với BMS reading) để cross-source pair theo đúng phút.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..bms import BmsReading
from ..config import SensorDrift


@dataclass
class GatewayReading:
    battery_serial: str
    battery_asset_id: str
    time_iso: str
    voltage: float | None
    current: float | None
    temperature: float | None
    soc_percent: float | None
    soh_percent: float | None
    cycle_count: int | None
    charging_state: int | None
    bms_error_code: str | None
    source_type: int               # IotGateway = 2
    sensor_source_code: str        # "redundant" | "external-temp"


def make_ina226_reading(bms: BmsReading, drift: SensorDrift, scenario: str) -> GatewayReading:
    """INA226 đo V/I qua I2C — redundant với BMS. Không biết SOC/SOH/cycle/state."""
    v_offset = drift.voltage_v + random.uniform(-0.01, 0.01)
    if scenario == "sensor_mismatch":
        v_offset += 0.8                                 # > 0.5V threshold
    return GatewayReading(
        battery_serial=bms.battery_serial,
        battery_asset_id=bms.battery_asset_id,
        time_iso=bms.time_iso,                          # cùng tick với BMS
        voltage=round(bms.voltage + v_offset, 3),
        current=round(bms.current + random.uniform(-0.05, 0.05), 3),
        temperature=None,
        soc_percent=None,
        soh_percent=None,
        cycle_count=None,
        charging_state=None,
        bms_error_code=None,
        source_type=2,
        sensor_source_code="redundant",
    )


def make_ds18b20_reading(bms: BmsReading, drift: SensorDrift, scenario: str) -> GatewayReading:
    """DS18B20 đo nhiệt thân pin — external-temp."""
    t_offset = drift.temperature_c + random.uniform(-0.3, 0.3)
    if scenario == "sensor_mismatch":
        t_offset += 6.5                                 # > 5°C threshold
    return GatewayReading(
        battery_serial=bms.battery_serial,
        battery_asset_id=bms.battery_asset_id,
        time_iso=bms.time_iso,
        voltage=None,
        current=None,
        temperature=round(bms.temperature + t_offset, 2),
        soc_percent=None,
        soh_percent=None,
        cycle_count=None,
        charging_state=None,
        bms_error_code=None,
        source_type=2,
        sensor_source_code="external-temp",
    )
