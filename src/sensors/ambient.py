"""SHT31 — ambient temp + humidity. Đi vào endpoint `/api/ambient/readings/batch` (api-battery.md §POST).

Auth: API Key (scheme=ApiKey + policy=EnvironmentalIngest).

Payload format đúng theo api-battery.md:
  {
    "items": [{
      "siteId":             "<Guid>",
      "time":               "2026-06-12T08:00:00Z",
      "ambientTemperature": 34.2,             # -90..90 °C
      "humidity":           72.5,             # 0..100 %  (optional)
      "solarIrradiance":    580.0,            # W/m²       (optional)
      "source":             1,                # IotSensor=1, WeatherApi=2
      "sourceDeviceId":     "ESP32-SIM-001"   # optional (string)
    }]
  }

Tối đa 100 readings/batch (backend cap).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

AMBIENT_SOURCE_IOT_SENSOR = 1
AMBIENT_SOURCE_WEATHER_API = 2


@dataclass
class AmbientReading:
    site_id_guid: str
    time_iso: str
    ambient_temperature: float
    humidity: float
    solar_irradiance: float | None
    source: int                     # 1=IotSensor
    source_device_id: str


def make_ambient_reading(
    device_code: str,
    site_id_guid: str,
    time_iso: str,
    t_global: float,
    scenario: str,
    base_temp_c: float = 28.0,
) -> AmbientReading:
    temperature = base_temp_c + 3.0 * math.sin(t_global / 600.0) + random.uniform(-0.4, 0.4)
    humidity = 60.0 + 10.0 * math.sin(t_global / 800.0 + 1.3) + random.uniform(-2.0, 2.0)
    solar = max(0.0, 400.0 + 250.0 * math.sin(t_global / 1800.0) + random.uniform(-30, 30))

    # Scenario-driven: đẩy ambient vượt ngưỡng để trigger AnomalyType=9/10/11
    if scenario == "high_ambient_temp":
        temperature = 45.0 + random.uniform(-0.5, 0.5)        # > HighAmbientTempCritical (~40°C)
    elif scenario == "high_humidity":
        humidity = 92.0 + random.uniform(-1, 1)               # > HighHumidityCritical (~85%)
    elif scenario == "high_temp_humidity_combo":
        temperature = 42.0 + random.uniform(-0.5, 0.5)
        humidity = 88.0 + random.uniform(-1, 1)

    return AmbientReading(
        site_id_guid=site_id_guid,
        time_iso=time_iso,
        ambient_temperature=round(temperature, 2),
        humidity=round(max(0.0, min(100.0, humidity)), 2),
        solar_irradiance=round(solar, 1),
        source=AMBIENT_SOURCE_IOT_SENSOR,
        source_device_id=device_code,
    )
