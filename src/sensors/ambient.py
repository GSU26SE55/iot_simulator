"""SHT31 — nhiệt độ + độ ẩm môi trường. Mirror `firmware-esp32/src/sensor/sht31.cpp` (S5-FW-06).

`POST /api/ambient/readings/batch` — auth ApiKey, scope `EnvironmentalIngest`.

Payload (KHỚP CHÍNH XÁC `sht31PostNow`, đúng thứ tự trường):
    { "items": [ { "siteId": "<Guid>", "time": "<ISO8601 Z>", "ambientTemperature": 34.2,
                   "humidity": 72.5, "source": 1, "sourceDeviceId": "ESP32-SIM-001" } ] }

Hai điểm bản simulator cũ làm SAI so với thiết bị thật, nay đã sửa:
  1. **Chu kỳ 60s**, không phải 300s (`SHT31_POLL_INTERVAL_MS = 60000UL`).
  2. **KHÔNG có `solarIrradiance`** — firmware không đo và không gửi trường này. Simulator gửi
     thêm nghĩa là dashboard/AI thấy một nguồn dữ liệu mà thiết bị thật không bao giờ cấp.

Ngoài ra firmware KHÔNG xếp hàng đợi cho ambient: hỏng thì bỏ qua, 60s sau đo lại. Giữ nguyên —
đẩy ambient vào hàng đợi telemetry (bản cũ) vừa sai hành vi vừa chiếm chỗ của số đo pin.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass

log = logging.getLogger("iot-sim.sht31")

ENDPOINT = "/api/ambient/readings/batch"

# include/config.h — SHT31_POLL_INTERVAL_MS
SHT31_POLL_INTERVAL_MS = 60000

# `AmbientReadingSourceEnum`: IotSensor=1, WeatherApi=2.
# Backend KHÔNG đăng ký JsonStringEnumConverter ⇒ System.Text.Json chỉ nhận SỐ; gửi chuỗi là 400.
AMBIENT_SOURCE_IOT_SENSOR = 1
AMBIENT_SOURCE_WEATHER_API = 2

# Dải hợp lệ mà `sht31ReadOnce` tự kiểm trước khi gửi (sensor lỗi I2C hay trả giá trị vô lý).
SHT31_TEMP_MIN_C, SHT31_TEMP_MAX_C = -40.0, 125.0
SHT31_HUMIDITY_MIN, SHT31_HUMIDITY_MAX = 0.0, 100.0


@dataclass
class AmbientReading:
    site_id_guid: str
    time_iso: str
    ambient_temperature: float
    humidity: float
    source: int
    source_device_id: str

    def to_item(self) -> dict:
        return {
            "siteId": self.site_id_guid,
            "time": self.time_iso,
            "ambientTemperature": self.ambient_temperature,
            "humidity": self.humidity,
            "source": self.source,
            "sourceDeviceId": self.source_device_id,
        }


def make_ambient_reading(device_code: str, site_id_guid: str, time_iso: str,
                         t_global: float, scenario: str,
                         base_temp_c: float = 28.0) -> AmbientReading:
    """Sinh một mẫu đo SHT31. Scenario đẩy vượt ngưỡng để backend dựng AnomalyType 9/10/11."""
    temperature = base_temp_c + 3.0 * math.sin(t_global / 600.0) + random.uniform(-0.4, 0.4)
    humidity = 60.0 + 10.0 * math.sin(t_global / 800.0 + 1.3) + random.uniform(-2.0, 2.0)

    if scenario == "high_ambient_temp":
        temperature = 45.0 + random.uniform(-0.5, 0.5)     # > HighAmbientTempCritical (~40°C)
    elif scenario == "high_humidity":
        humidity = 92.0 + random.uniform(-1.0, 1.0)        # > HighHumidityCritical (~85%)
    elif scenario == "high_temp_humidity_combo":
        temperature = 42.0 + random.uniform(-0.5, 0.5)
        humidity = 88.0 + random.uniform(-1.0, 1.0)

    return AmbientReading(
        site_id_guid=site_id_guid,
        time_iso=time_iso,
        ambient_temperature=round(temperature, 2),
        humidity=round(max(SHT31_HUMIDITY_MIN, min(SHT31_HUMIDITY_MAX, humidity)), 2),
        source=AMBIENT_SOURCE_IOT_SENSOR,
        source_device_id=device_code,
    )


class Sht31Sensor:
    """`sensor::sht31*` — đo + đẩy ambient theo chu kỳ, tự bỏ qua khi chưa có siteId."""

    def __init__(self, http, device_code: str, iso_now, enabled: bool = True,
                 poll_interval_ms: int = SHT31_POLL_INTERVAL_MS):
        self._http = http
        self.device_code = device_code
        self._iso_now = iso_now
        self.enabled = bool(enabled)
        self.poll_interval_ms = int(poll_interval_ms)
        self.site_id = ""
        self._last_post_ms = 0
        self._first = True
        self.post_ok_count = 0
        self.post_fail_count = 0
        self.last_temperature = 0.0
        self.last_humidity = 0.0

    def set_site_id(self, site_id_guid: str | None) -> None:
        """`sht31SetSiteId` — backend đòi `AmbientReadingItem.SiteId` (Guid, required)."""
        self.site_id = site_id_guid or ""

    def tick(self, now_ms: int, t_global: float, scenario: str) -> None:
        if not self.enabled:
            return
        if not self._first and now_ms - self._last_post_ms < self.poll_interval_ms:
            return
        self._first = False
        self._last_post_ms = now_ms
        self.post_now(t_global, scenario)

    def post_now(self, t_global: float, scenario: str) -> bool:
        if not self.enabled:
            return False
        # Chưa provision xong thì backend chắc chắn reject (400) — bỏ qua để khỏi tốn round-trip.
        if not self.site_id:
            log.info("[%s] siteId chưa có (provision chưa xong?) — bỏ qua ambient",
                     self.device_code)
            return False

        reading = make_ambient_reading(
            device_code=self.device_code, site_id_guid=self.site_id,
            time_iso=self._iso_now(), t_global=t_global, scenario=scenario)

        # `sht31ReadOnce` tự loại giá trị ngoài dải vật lý trước khi gửi — cảm biến lỗi I2C trả
        # NaN/giá trị vô lý, mà mỗi giá trị vô lý gửi đi là một outlier tính vào hạn mức
        # auto-decommission của backend.
        if not (SHT31_TEMP_MIN_C <= reading.ambient_temperature <= SHT31_TEMP_MAX_C):
            log.warning("[%s] ambient temp NGOÀI DẢI = %.2f°C — bỏ mẫu", self.device_code,
                        reading.ambient_temperature)
            self.post_fail_count += 1
            return False
        if not (SHT31_HUMIDITY_MIN <= reading.humidity <= SHT31_HUMIDITY_MAX):
            log.warning("[%s] ambient humidity NGOÀI DẢI = %.2f%% — bỏ mẫu", self.device_code,
                        reading.humidity)
            self.post_fail_count += 1
            return False

        self.last_temperature = reading.ambient_temperature
        self.last_humidity = reading.humidity

        res = self._http.ambient_ingest({"items": [reading.to_item()]})
        if res.ok:
            self.post_ok_count += 1
            log.info("[%s] ambient OK %.1f°C %.1f%% (%d) [%dms]", self.device_code,
                     reading.ambient_temperature, reading.humidity, res.status_code,
                     res.duration_ms)
            return True

        self.post_fail_count += 1
        log.warning("[%s] ambient FAIL code=%d resp=%s", self.device_code, res.status_code,
                    res.body[:120])
        if res.status_code in (401, 403):
            log.error("[%s]   → API key nhiều khả năng THIẾU scope EnvironmentalIngest (bitmask 4)",
                      self.device_code)
        return False
