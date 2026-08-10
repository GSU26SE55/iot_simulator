"""Dựng JSON batch payload — mirror `firmware-esp32/src/core/payload.{h,cpp}`.

Đây là file quyết định HÌNH DẠNG CHÍNH XÁC của dữ liệu gửi lên backend. Mọi thứ trong đây phải
khớp 1:1 `core::buildLegacyBatchPayload` / `core::buildProductionBatchPayload`:

  * tên trường camelCase, đúng thứ tự;
  * trường optional chỉ xuất hiện khi có cờ `has_*` (không gửi `null` thừa);
  * `time` và `deviceTimestamp` của MỘT item luôn BẰNG NHAU (#IoT2-15 clock-skew check);
  * mili-giây của timestamp = INDEX của item trong mảng truyền vào — xem `patch_item_timestamp`.

Cũng chứa `filter_out_published` (mirror `core/reading_filter.h`, GH-740).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Sequence

# ─────────────────────────── enum backend (core/reading.h) ──────────────────────────────────


class SourceType(IntEnum):
    """`SensorReadingSourceTypeEnum` của backend."""

    BMS = 1          # đọc qua RS485/Modbus từ BMS
    IOT_GATEWAY = 2  # cảm biến ngoài của ESP32 (INA226 redundant, DS18B20 external-temp)
    EXTERNAL = 3     # nhập tay (chưa dùng)


class ChargingState(IntEnum):
    """`ChargingStateEnum` của backend."""

    IDLE = 1
    CHARGING = 2
    DISCHARGING = 3
    FLOAT = 4
    BYPASS = 5


# core/source_tags.h — single source of truth cho 3 chuỗi tag cross-source.
# Sai một chuỗi ở đây là backend KHÔNG ghép được cặp BMS↔IotGateway và cảnh báo SensorMismatch
# im lặng biến mất, không có lỗi ở bất kỳ đâu.
SOURCE_CODE_PRIMARY = "primary"
SOURCE_CODE_REDUNDANT = "redundant"
SOURCE_CODE_EXTERNAL_TEMP = "external-temp"

SOURCE_TYPE_PRIMARY = SourceType.BMS
SOURCE_TYPE_REDUNDANT = SourceType.IOT_GATEWAY
SOURCE_TYPE_EXTERNAL_TEMP = SourceType.IOT_GATEWAY

# Backend `bmsErrorCode` ≤ 64 ký tự (MO §52.5 + validation #IoT2-17).
MAX_BMS_ERROR_CHARS = 64
# Backend `sensorSourceCode` ≤ 20 ký tự.
MAX_SENSOR_SOURCE_CODE_CHARS = 20


@dataclass
class SensorReading:
    """`core::SensorReading` — struct trung gian dùng chung cho mọi nguồn đo."""

    battery_asset_id: str = ""
    serial: str = ""
    voltage: float = 0.0
    current: float = 0.0
    temperature: float = 0.0
    soc_percent: float = 0.0
    cycle_count: int = 0

    sensor_source_code: str = ""
    source_type: SourceType = SourceType.IOT_GATEWAY
    charging_state: ChargingState = ChargingState.IDLE
    soh_percent: float = 0.0
    bms_error_code: str = ""

    has_soh: bool = False
    has_charging_state: bool = False
    has_bms_error: bool = False

    # ── Tier 2 (backend Sprint 5B #101/#105) — CHỈ gửi khi được đặt ────────────────────────
    # ⚠ Firmware ESP32 hiện KHÔNG sinh hai trường này (`core::SensorReading` không có chúng), nên
    # thiết bị thật không bao giờ phát ra. Backend thì CÓ nhận (`SensorReadingItem`) và có luật
    # phát hiện `HighInternalResistance` / `CellImbalance`. Giữ ở đây để bộ chạy dataset anomaly
    # demo được hai loại cảnh báo đó; luồng chạy bình thường của simulator vẫn để None.
    internal_resistance_milliohm: float | None = None
    cell_voltage_delta_mv: float | None = None
    # `sourceDeviceId` ≤ 64 ký tự — firmware cũng không gửi (mã thiết bị đi ở header).
    source_device_id: str = ""


# ────────────────────────────── timestamp per-item ──────────────────────────────────────────
def patch_item_timestamp(base_iso: str, idx: int) -> str:
    """`core::patchItemTimestamp`.

    Khoá chính hypertable `sensor_readings` của backend là `(Time, BatteryAssetId)` — KHÔNG gồm
    `sensorSourceCode`. `isoNow()` chỉ có độ phân giải GIÂY, nên 3 reading cùng pin
    (primary/redundant/external-temp) trong cùng batch mà dùng chung timestamp sẽ vi phạm khoá
    chính → backend 500 CẢ BATCH.

    Vá mili-giây = index item: "…T08:15:42Z" + idx 7 → "…T08:15:42.007Z".
    Format lạ (không kết thúc bằng 'Z') → giữ nguyên, an toàn hơn cắt xén.

    Nếu chuỗi vào ĐÃ có phần lẻ giây thì THAY nó, không nối thêm: `net::isoNow` không bao giờ
    sinh phần lẻ nên firmware không gặp ca này, nhưng bộ chạy dataset anomaly có thể truyền vào
    mốc thời gian tự dựng. Nối thêm sẽ ra "…42.123.007Z" — chuỗi không phải ISO8601, và backend
    từ chối cả batch với một thông báo chẳng liên quan gì tới nguyên nhân thật.
    """
    if not base_iso or len(base_iso) < 2 or not base_iso.endswith("Z"):
        return base_iso
    body = base_iso[:-1]
    dot = body.rfind(".")
    if dot > 0 and body[dot + 1:].isdigit():
        body = body[:dot]
    return f"{body}.{idx % 1000:03d}Z"


# ─────────────────────────────── payload builders ───────────────────────────────────────────
def build_legacy_batch_payload(readings: Sequence[SensorReading], iso_timestamp: str,
                               device_code: str) -> dict | None:
    """`core::buildLegacyBatchPayload` — contract Sprint 1 (NI §7.4 backward compat).

    Top-level CHỈ có `items[]`; item chỉ có `batteryAssetId` (Guid) + 6 trường cơ bản.
    KHÔNG có sourceType / sensorSourceCode / deviceTimestamp (đó là contract Sprint 3).
    `device_code` chỉ đi ở header, KHÔNG nhúng vào body — giống firmware.
    """
    if not readings:
        return None
    if not iso_timestamp:
        return None
    if not device_code:
        return None

    items = []
    for i, r in enumerate(readings):
        items.append({
            "batteryAssetId": r.battery_asset_id,
            "time": patch_item_timestamp(iso_timestamp, i),
            "voltage": r.voltage,
            "current": r.current,
            "temperature": r.temperature,
            "socPercent": r.soc_percent,
            "cycleCount": r.cycle_count,
        })
    return {"items": items}


def build_production_batch_payload(readings: Sequence[SensorReading], iso_timestamp: str,
                                   device_code: str) -> dict | None:
    """`core::buildProductionBatchPayload` — contract Sprint 3 (S3-FW-04).

    Khớp `BatchIngestSensorReadingsCommand` + `SensorReadingItem` của backend:
      batteryAssetSerial (ưu tiên) | batteryAssetId, time, deviceTimestamp, voltage, current,
      temperature, socPercent, cycleCount, sourceType, sensorSourceCode?,
      sohPercent?, chargingState?, bmsErrorCode?
    """
    if not readings:
        return None
    if not iso_timestamp:
        return None
    if not device_code:
        return None

    items: list[dict] = []
    for i, r in enumerate(readings):
        item: dict = {}
        # Sprint 3 ưu tiên serial hơn id — backend §52.5 tự resolve serial → BatteryAssetId.
        if r.serial:
            item["batteryAssetSerial"] = r.serial
        else:
            item["batteryAssetId"] = r.battery_asset_id

        item_iso = patch_item_timestamp(iso_timestamp, i)
        item["time"] = item_iso
        item["deviceTimestamp"] = item_iso        # #IoT2-15 clock-skew check
        item["voltage"] = r.voltage
        item["current"] = r.current
        item["temperature"] = r.temperature
        item["socPercent"] = r.soc_percent
        item["cycleCount"] = r.cycle_count

        # sourceType + sensorSourceCode CẤM hard-code — cross-source S6 §1.6.6.
        item["sourceType"] = int(r.source_type)
        if r.sensor_source_code:
            item["sensorSourceCode"] = r.sensor_source_code[:MAX_SENSOR_SOURCE_CODE_CHARS]

        # Optional — chỉ gửi khi có cờ, tránh null thừa (giống firmware).
        if r.has_soh:
            item["sohPercent"] = r.soh_percent
        if r.has_charging_state:
            item["chargingState"] = int(r.charging_state)
        if r.has_bms_error and r.bms_error_code:
            item["bmsErrorCode"] = r.bms_error_code[:MAX_BMS_ERROR_CHARS]

        # Tier 2 — mặc định None ⇒ payload giống hệt firmware. Xem ghi chú ở `SensorReading`.
        if r.internal_resistance_milliohm is not None:
            item["internalResistanceMilliohm"] = r.internal_resistance_milliohm
        if r.cell_voltage_delta_mv is not None:
            item["cellVoltageDeltaMv"] = r.cell_voltage_delta_mv
        if r.source_device_id:
            item["sourceDeviceId"] = r.source_device_id[:64]

        items.append(item)
    return {"items": items}


def build_batch_payload(readings: Sequence[SensorReading], iso_timestamp: str,
                        device_code: str, production: bool) -> dict | None:
    """Chọn builder theo contract đang chạy."""
    if production:
        return build_production_batch_payload(readings, iso_timestamp, device_code)
    return build_legacy_batch_payload(readings, iso_timestamp, device_code)


# ───────────────────────── reading filter (core/reading_filter.h) ───────────────────────────
def filter_out_published(readings: Sequence[SensorReading],
                         published_serials: Iterable[str]) -> list[SensorReading]:
    """`core::filterOutPublished` (GH-740).

    `ingest_via_mqtt` publish theo TỪNG NHÓM serial. Nhóm đầu gửi xong, nhóm sau fail thì hàm
    trả false và caller rơi xuống fallback HTTPS — nếu gửi lại TOÀN BỘ batch thì nhóm đã vào
    backend qua MQTT bị ghi LẦN THỨ HAI. Khoá `Idempotency-Key` của đường HTTPS không cứu được:
    nó chỉ khử trùng giữa các lần gửi HTTPS với nhau, còn bản ghi kia vào bằng đường khác với
    hình dạng payload khác hẳn.

    Serial rỗng coi là CHƯA gửi (giữ lại): thà gửi thừa một bản ghi không định danh được còn hơn
    làm mất nó.
    """
    published = {s for s in published_serials if s}
    if not published:
        return list(readings)
    return [r for r in readings if not r.serial or r.serial not in published]


def group_by_serial(readings: Sequence[SensorReading]) -> "list[tuple[str, list[SensorReading]]]":
    """Gom reading theo battery serial, GIỮ THỨ TỰ xuất hiện đầu tiên.

    Mirror bước 1+2 của `ingestViaMqtt` (firmware enumerate unique serial rồi collect nhóm).
    Reading không có serial bị bỏ khỏi đường MQTT — firmware cũng vậy, vì topic
    `solar/{dev}//telemetry` sẽ bị ACL của broker từ chối trong im lặng.
    """
    order: list[str] = []
    groups: dict[str, list[SensorReading]] = {}
    for r in readings:
        if not r.serial:
            continue
        if r.serial not in groups:
            groups[r.serial] = []
            order.append(r.serial)
        groups[r.serial].append(r)
    return [(s, groups[s]) for s in order]
