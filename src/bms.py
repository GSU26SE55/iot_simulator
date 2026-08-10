"""Mock BMS — sinh số đo vật lý cho 1 cục pin theo scenario.

Tương ứng `firmware-esp32/src/bms/mock_bms.cpp` (đường `USE_MOCK_BMS=1`), tức là ĐÚNG chế độ mà
simulator mô phỏng: thiết bị chạy firmware thật nhưng không gắn BMS/pin.

Reading từ BMS mang `sourceType=Bms (1)` + `sensorSourceCode="primary"` (MO §52.9). Mô hình vật lý
ở đây chi tiết hơn mock của firmware (đường cong OCV(SOC), Coulomb counting, sinh nhiệt I²R, suy
giảm SOH) để dựng được các kịch bản cảnh báo thật — nhưng HÌNH DẠNG dữ liệu gửi backend thì y hệt.

Trả thẳng `payload.SensorReading` — cùng một struct mà firmware dùng cho mọi nguồn đo, nên không
còn lớp chuyển đổi nào có thể trôi khỏi hợp đồng.
"""
from __future__ import annotations

import math
import random

from .config import BatteryConfig
from .payload import (SOURCE_CODE_PRIMARY, SOURCE_TYPE_PRIMARY, ChargingState,
                      SensorReading)
from .timeutil import iso_now

# Giữ tên cũ cho script/test đã dùng — nay chỉ là bí danh của `timeutil.iso_now`.
pinned_time_iso = iso_now


class MockBattery:
    """Trạng thái mô phỏng vật lý của MỘT pin."""

    # Đường cong OCV(SOC) của LiFePO4 12.8V danh định, nội suy tuyến tính giữa các điểm.
    _SOC_OCV_TABLE_LIFEPO4_12V = [
        (0, 10.0),
        (10, 12.0),
        (20, 12.8),
        (50, 13.1),
        (80, 13.3),
        (95, 13.4),
        (100, 13.6),
    ]
    _REF_NOMINAL_V = 12.8

    def __init__(self, cfg: BatteryConfig, ambient_c: float = 28.0):
        self.cfg = cfg
        self.soc = cfg.initial_soc
        self.soh = cfg.initial_soh
        self.cycle_count = cfg.cycle_count
        self.ambient_c = ambient_c
        self._scenario_temp_offset = 0.0
        self._scenario_soc_drift = 0.0
        self._scenario_voltage_offset = 0.0
        self._scenario_soh_drift = 0.0
        self._last_current = 0.0

    @classmethod
    def _ocv_from_soc(cls, soc: float, nominal_v: float) -> float:
        table = cls._SOC_OCV_TABLE_LIFEPO4_12V
        soc = max(0.0, min(100.0, soc))
        for i in range(len(table) - 1):
            s0, v0 = table[i]
            s1, v1 = table[i + 1]
            if s0 <= soc <= s1:
                ratio = (soc - s0) / (s1 - s0) if s1 != s0 else 0.0
                return (v0 + ratio * (v1 - v0)) * (nominal_v / cls._REF_NOMINAL_V)
        return table[-1][1] * (nominal_v / cls._REF_NOMINAL_V)

    def step(self, dt_s: float, t_global: float, scenario: str) -> SensorReading:
        """Tiến mô phỏng 1 bước `dt_s` giây, trả reading của nguồn BMS."""

        # 1. Dòng điện — quy ước backend: âm = xả, dương = sạc.
        load_phase = math.sin(t_global / 45.0)
        base_current = -1.0 + 1.5 * load_phase + random.uniform(-0.2, 0.2)
        if scenario == "low_soc":
            base_current = -3.5 + random.uniform(-0.3, 0.3)
        elif scenario == "rapid_discharge":
            base_current = -12.0 + random.uniform(-1.0, 1.0)     # vượt ngưỡng RapidDischarge
        elif scenario == "abnormal_charging":
            base_current = 15.0 + random.uniform(-1.0, 1.0)      # dòng sạc cao bất thường
        elif scenario == "overheat":
            base_current = abs(base_current) + 1.5
        current = round(base_current, 3)
        self._last_current = current

        # 2. SOC — Coulomb counting.
        capacity = max(self.cfg.nominal_capacity_ah, 1e-3)
        delta_soc = (current * dt_s / 3600.0) / capacity * 100.0
        self.soc = max(0.0, min(100.0, self.soc + delta_soc))
        if scenario == "low_soc":
            self._scenario_soc_drift += dt_s * 0.05
            self.soc = max(5.0, self.soc - self._scenario_soc_drift * 0.01)

        # 3. Điện áp = OCV(SOC) − I·R_nội + nhiễu.
        r_internal = 0.02
        ocv = self._ocv_from_soc(self.soc, self.cfg.nominal_voltage)
        voltage = (ocv - current * r_internal + 0.01 * math.sin(t_global / 12.0)
                   + random.uniform(-0.005, 0.005))
        if scenario == "overvoltage":
            self._scenario_voltage_offset = min(self._scenario_voltage_offset + dt_s * 0.05, 2.5)
            voltage += self._scenario_voltage_offset
        elif scenario == "undervoltage":
            self._scenario_voltage_offset = min(self._scenario_voltage_offset + dt_s * 0.05, 3.5)
            voltage -= self._scenario_voltage_offset
        voltage = round(voltage, 3)

        # 4. Nhiệt độ = môi trường + sinh nhiệt I²R + dao động ngày.
        heat = (current * current) * 0.15
        temperature = (self.ambient_c + heat + 1.5 * math.sin(t_global / 180.0)
                       + random.uniform(-0.3, 0.3))
        if scenario == "overheat":
            self._scenario_temp_offset = min(self._scenario_temp_offset + dt_s * 0.15, 35.0)
            temperature += self._scenario_temp_offset
        temperature = round(temperature, 2)

        # 5. SOH — suy giảm theo cycle; scenario soh_degradation đẩy nhanh.
        soh = max(0.0, self.cfg.initial_soh
                  - max(0, self.cycle_count - self.cfg.cycle_count) * 0.005)
        if scenario == "soh_degradation":
            self._scenario_soh_drift += dt_s * 0.02
            soh = max(40.0, soh - self._scenario_soh_drift)
        soh = round(soh, 2)
        self.soh = soh

        # 6. ChargingState — Idle=1, Charging=2, Discharging=3, Float=4, Bypass=5.
        if abs(current) < 0.1:
            charging_state = ChargingState.IDLE
        elif current > 0.1:
            charging_state = (ChargingState.FLOAT
                              if (self.soc >= 99.5 and current < 0.5)
                              else ChargingState.CHARGING)
        else:
            charging_state = ChargingState.DISCHARGING

        # 7. Mã lỗi BMS (≤ 64 ký tự — ràng buộc backend #IoT2-17).
        err = ""
        if scenario == "bms_error":
            err = "OVT-PROTECT"
        elif voltage > 15.0:
            err = "OVP"
        elif voltage < 9.0:
            err = "UVP"
        elif temperature > 60.0:
            err = "OTP"
        elif current < -20.0:
            err = "OCD"        # over-current discharge

        return SensorReading(
            battery_asset_id=self.cfg.battery_asset_id,
            serial=self.cfg.serial,
            voltage=voltage,
            current=current,
            temperature=temperature,
            soc_percent=round(self.soc, 2),
            cycle_count=self.cycle_count,
            sensor_source_code=SOURCE_CODE_PRIMARY,
            source_type=SOURCE_TYPE_PRIMARY,
            charging_state=charging_state,
            soh_percent=soh,
            bms_error_code=err,
            has_soh=True,               # BMS biết SOH
            has_charging_state=True,    # BMS biết trạng thái sạc
            has_bms_error=bool(err),    # chỉ gửi khi thực sự có lỗi
        )
