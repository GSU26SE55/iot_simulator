"""INA226 (V/I dự phòng) + DS18B20 (nhiệt ngoài thân pin) — `sourceType=IotGateway (2)`.

Mirror `mock_bms.cpp::mockGenerateMultiSource` (đường `USE_MOCK_BMS=1`) — đúng chế độ mà simulator
mô phỏng.

Cross-source validation (NI §7.6, MO §1.6.6): backend ghép cặp reading CÙNG pin, CÙNG khung thời
gian nhưng KHÁC `sourceType` rồi so:
    |V_bms − V_iot| > 0.5V  → SensorMismatch (Warning)
    |T_bms − T_iot| > 5°C   → SensorMismatch (Warning)
Scenario `sensor_mismatch` cố ý đẩy lệch vượt hai ngưỡng đó.

⚠ TRƯỜNG KHÔNG ĐO ĐƯỢC THÌ **SAO CHÉP TỪ BMS**, KHÔNG GỬI 0.0.
Đây là điểm phải hết sức cẩn thận. Backend đếm outlier theo dải vật lý — `voltage ∈ (0, 1000]` —
và **>50 outlier/giờ thì tự chuyển thiết bị sang `Decommissioned`**, mọi request sau trả 409
(audit `iot-backend-contract-gaps.md` #5). DS18B20 chỉ đo nhiệt; nếu gửi `voltage: 0.0` thì với
chu kỳ 5s là 720 reading/giờ → thiết bị bị khoá chỉ sau vài phút chạy.
`mockGenerateMultiSource` của firmware sao chép giá trị BMS cho đúng lý do này; bản simulator cũ
gửi 0.0 nên đang dính đúng cái bẫy đó.
"""
from __future__ import annotations

import random

from ..config import SensorDrift
from ..payload import (SOURCE_CODE_EXTERNAL_TEMP, SOURCE_CODE_REDUNDANT,
                       SOURCE_TYPE_EXTERNAL_TEMP, SOURCE_TYPE_REDUNDANT,
                       SensorReading)

# Ngưỡng cross-source của backend — scenario `sensor_mismatch` phải vượt hẳn để chắc chắn trigger.
VOLTAGE_MISMATCH_THRESHOLD_V = 0.5
TEMPERATURE_MISMATCH_THRESHOLD_C = 5.0


def make_ina226_reading(bms: SensorReading, drift: SensorDrift,
                        scenario: str) -> SensorReading:
    """INA226 đo V/I qua I2C — dự phòng cho BMS. KHÔNG biết SOC/SOH/cycle/trạng thái sạc."""
    v_offset = drift.voltage_v + random.uniform(-0.01, 0.01)
    if scenario == "sensor_mismatch":
        v_offset += 0.8                                  # > ngưỡng 0.5V
    return SensorReading(
        battery_asset_id=bms.battery_asset_id,
        serial=bms.serial,
        voltage=round(bms.voltage + v_offset, 3),
        current=round(bms.current + random.uniform(-0.05, 0.05), 3),
        temperature=bms.temperature,     # INA226 không đo nhiệt → sao chép BMS (xem ghi chú đầu file)
        soc_percent=bms.soc_percent,     # không đo SOC → sao chép BMS
        cycle_count=bms.cycle_count,
        sensor_source_code=SOURCE_CODE_REDUNDANT,
        source_type=SOURCE_TYPE_REDUNDANT,
        # Cảm biến ngoài KHÔNG biết SOH / trạng thái sạc / mã lỗi → không gửi 3 trường đó.
        has_soh=False,
        has_charging_state=False,
        has_bms_error=False,
    )


def make_ds18b20_reading(bms: SensorReading, drift: SensorDrift,
                         scenario: str) -> SensorReading:
    """DS18B20 đo nhiệt thân pin (1-Wire) — `external-temp`."""
    t_offset = drift.temperature_c + random.uniform(-0.3, 0.3)
    if scenario == "sensor_mismatch":
        t_offset += 6.5                                  # > ngưỡng 5°C
    return SensorReading(
        battery_asset_id=bms.battery_asset_id,
        serial=bms.serial,
        voltage=bms.voltage,             # không đo điện → sao chép BMS (xem ghi chú đầu file)
        current=bms.current,
        temperature=round(bms.temperature + t_offset, 2),
        soc_percent=bms.soc_percent,
        cycle_count=bms.cycle_count,
        sensor_source_code=SOURCE_CODE_EXTERNAL_TEMP,
        source_type=SOURCE_TYPE_EXTERNAL_TEMP,
        has_soh=False,
        has_charging_state=False,
        has_bms_error=False,
    )
