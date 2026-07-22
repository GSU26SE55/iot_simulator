"""Mock BMS — sinh sensor reading vật lý cho 1 cục pin theo scenario.

Reading từ BMS = `sourceType=Bms (1)`, `sensorSourceCode="primary"` (MO §52.9).
Mỗi pin có state riêng: SOC giảm theo Coulomb counting (I·dt), voltage là OCV(SOC) − I·R_internal,
nhiệt độ là ambient + heat từ I²·R + scenario delta.

Mọi reading TRONG CÙNG 1 TICK của cùng battery dùng CHUNG 1 `time_iso` để giữ tính nguyên tử
cho cross-source pair (§1.6.6 — backend match BMS vs IotGateway theo timestamp).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import BatteryConfig


@dataclass
class BmsReading:
    """1 reading đọc từ BMS, format theo `newiot.md §7.4` production contract."""
    battery_serial: str
    battery_asset_id: str          # Guid (cho contract `current`)
    time_iso: str                  # pinned theo tick
    voltage: float                 # V
    current: float                 # A (âm = discharge, dương = charge — convention backend)
    temperature: float             # °C
    soc_percent: float             # 0..100
    soh_percent: float             # 0..100
    cycle_count: int
    # ChargingStateEnum: Idle=1, Charging=2, Discharging=3, Float=4, Bypass=5
    charging_state: int
    bms_error_code: str | None     # ≤ 64 chars
    source_type: int = 1           # Bms = 1
    sensor_source_code: str = "primary"


class MockBattery:
    """Trạng thái mô phỏng vật lý 1 pin."""

    def __init__(self, cfg: BatteryConfig, ambient_c: float = 28.0):
        self.cfg = cfg
        self.soc = cfg.initial_soc
        self.soh = cfg.initial_soh
        self.cycle_count = cfg.cycle_count
        self.ambient_c = ambient_c
        # state động:
        self._scenario_temp_offset = 0.0
        self._scenario_soc_drift = 0.0
        self._scenario_voltage_offset = 0.0
        self._scenario_soh_drift = 0.0
        self._last_current = 0.0

    _SOC_OCV_TABLE_LIFEPO4_12V = [
        (0,    10.0),
        (10,   12.0),
        (20,   12.8),
        (50,   13.1),
        (80,   13.3),
        (95,   13.4),
        (100,  13.6),
    ]

    @classmethod
    def _ocv_from_soc(cls, soc: float, nominal_v: float) -> float:
        table = cls._SOC_OCV_TABLE_LIFEPO4_12V
        soc = max(0.0, min(100.0, soc))
        ref_nominal = 12.8
        for i in range(len(table) - 1):
            s0, v0 = table[i]
            s1, v1 = table[i + 1]
            if s0 <= soc <= s1:
                ratio = (soc - s0) / (s1 - s0) if s1 != s0 else 0.0
                v = v0 + ratio * (v1 - v0)
                return v * (nominal_v / ref_nominal)
        return table[-1][1] * (nominal_v / ref_nominal)

    def step(self, dt_s: float, t_global: float, scenario: str, time_iso: str) -> BmsReading:
        """Tiến mô phỏng 1 bước dt_s giây. `time_iso` đến từ caller (pinned per tick)."""

        # 1. Current
        load_phase = math.sin(t_global / 45.0)
        base_current = -1.0 + 1.5 * load_phase + random.uniform(-0.2, 0.2)
        if scenario == "low_soc":
            base_current = -3.5 + random.uniform(-0.3, 0.3)
        elif scenario == "rapid_discharge":
            base_current = -12.0 + random.uniform(-1.0, 1.0)     # vượt RapidDischarge threshold
        elif scenario == "abnormal_charging":
            base_current = 15.0 + random.uniform(-1.0, 1.0)      # charging current cao bất thường
        elif scenario == "overheat":
            base_current = abs(base_current) + 1.5
        current = round(base_current, 3)
        self._last_current = current

        # 2. SOC Coulomb counting
        capacity = max(self.cfg.nominal_capacity_ah, 1e-3)
        delta_soc = (current * dt_s / 3600.0) / capacity * 100.0
        self.soc = max(0.0, min(100.0, self.soc + delta_soc))
        if scenario == "low_soc":
            self._scenario_soc_drift += dt_s * 0.05
            self.soc = max(5.0, self.soc - self._scenario_soc_drift * 0.01)

        # 3. Voltage
        r_internal = 0.02
        ocv = self._ocv_from_soc(self.soc, self.cfg.nominal_voltage)
        voltage = ocv - current * r_internal + 0.01 * math.sin(t_global / 12.0) + random.uniform(-0.005, 0.005)
        if scenario == "overvoltage":
            self._scenario_voltage_offset = min(self._scenario_voltage_offset + dt_s * 0.05, 2.5)
            voltage += self._scenario_voltage_offset
        elif scenario == "undervoltage":
            self._scenario_voltage_offset = min(self._scenario_voltage_offset + dt_s * 0.05, 3.5)
            voltage -= self._scenario_voltage_offset
        voltage = round(voltage, 3)

        # 4. Temperature
        heat = (current * current) * 0.15
        temperature = self.ambient_c + heat + 1.5 * math.sin(t_global / 180.0) + random.uniform(-0.3, 0.3)
        if scenario == "overheat":
            # Ramp nhanh để vượt ngưỡng TemperatureMax (~45–50°C per battery-type) trong
            # ~30–60s demo: seed +8°C ngay tick đầu rồi +4.5°C/s (dt=5s → +22.5°C/tick),
            # cap +40°C. Với ambient ~28°C + heat → chạm ~55°C sau 1–2 tick.
            if self._scenario_temp_offset == 0.0:
                self._scenario_temp_offset = 8.0
            self._scenario_temp_offset = min(self._scenario_temp_offset + dt_s * 4.5, 40.0)
            temperature += self._scenario_temp_offset
        temperature = round(temperature, 2)

        # 5. SOH degrade — scenario soh_degradation đẩy nhanh
        soh = max(0.0, self.cfg.initial_soh - max(0, self.cycle_count - self.cfg.cycle_count) * 0.005)
        if scenario == "soh_degradation":
            self._scenario_soh_drift += dt_s * 0.02
            soh = max(40.0, soh - self._scenario_soh_drift)
        soh = round(soh, 2)

        # 6. ChargingState — ChargingStateEnum: Idle=1, Charging=2, Discharging=3, Float=4, Bypass=5
        if abs(current) < 0.1:
            cs = 1                                    # Idle
        elif current > 0.1:
            cs = 4 if (self.soc >= 99.5 and current < 0.5) else 2     # Float khi gần full + dòng nhỏ
        else:
            cs = 3                                    # Discharging

        # 7. bms_error_code
        err: str | None = None
        if scenario == "bms_error":
            err = "OVT-PROTECT"
        elif voltage > 15.0:
            err = "OVP"
        elif voltage < 9.0:
            err = "UVP"
        elif temperature > 60.0:
            err = "OTP"
        elif current < -20.0:
            err = "OCD"                                # over-current discharge

        return BmsReading(
            battery_serial=self.cfg.serial,
            battery_asset_id=self.cfg.battery_asset_id,
            time_iso=time_iso,
            voltage=voltage,
            current=current,
            temperature=temperature,
            soc_percent=round(self.soc, 2),
            soh_percent=soh,
            cycle_count=self.cycle_count,
            charging_state=cs,
            bms_error_code=err,
        )


def pinned_time_iso(skew_min: int = 0) -> str:
    """Sinh 1 timestamp ISO8601 UTC `Z` để dùng cho mọi reading cùng 1 tick."""
    from datetime import timedelta
    t = datetime.now(timezone.utc) + timedelta(minutes=skew_min)
    return t.isoformat().replace("+00:00", "Z")
