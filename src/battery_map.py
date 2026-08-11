"""Bảng ánh xạ pin lúc CHẠY — mirror `core/battery_map_codec.h` + `config/battery_map_runtime.cpp`.

IOT3-49. Nạp theo thứ tự:
  1. NVS (khoá `batmap`) — do `POST /api/iot-devices/provision` trả về trong `batteryMappings[]`
  2. Bảng trong `config/seed.yaml` — chỉ là ĐƯỜNG LUI (tương đương `config::kBatteryMappings`)

Vì sao quan trọng: backend ĐÃ trả `batteryMappings[]` từ Sprint IoT-2, nhưng cả firmware (trước
IOT3-49) lẫn simulator (trước bản này) đều bỏ qua hoàn toàn mảng này và tiếp tục dùng bảng cứng.
Khi serial trong bảng cứng không khớp `battery_assets` của backend thì backend **vẫn trả 201**
nhưng lặng lẽ bỏ reading — đúng triệu chứng `[ingest] ⚠ NHẬN THIẾU: 2/4 reading vào được`
(GH-748). Đọc bảng này là cách DUY NHẤT để thiết bị gửi đúng những pin nó thực sự được giao.

Định dạng lưu NVS (khớp firmware): `serial,unitId,sourceCode;serial,unitId,sourceCode;...`
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger("iot-sim.batmap")

# core/battery_map_codec.h
MAX_BATTERY_MAP_ENTRIES = 8
BATTERY_SERIAL_MAX_CHARS = 39      # kBatterySerialBufLen(40) - 1
BATTERY_SOURCE_CODE_MAX_CHARS = 23  # kBatterySourceCodeBufLen(24) - 1


class BatteryMapError(Enum):
    OK = 0
    EMPTY_SERIAL = 1
    TOO_LONG = 2
    RESERVED_CHAR = 3
    BAD_UNIT_ID = 4


def describe_battery_map_error(err: BatteryMapError) -> str:
    return {
        BatteryMapError.OK: "hợp lệ",
        BatteryMapError.EMPTY_SERIAL: "serial rỗng",
        BatteryMapError.TOO_LONG: "serial hoặc sourceCode quá dài",
        BatteryMapError.RESERVED_CHAR: "chứa ký tự phân cách ',' hoặc ';'",
        BatteryMapError.BAD_UNIT_ID: "unitId ngoài dải Modbus [1,247]",
    }.get(err, "không rõ")


@dataclass
class BatteryMapEntry:
    """`core::BatteryMapEntry`."""

    serial: str
    unit_id: int
    sensor_source_code: str = "primary"


def _check_field(value: str | None, max_chars: int, allow_empty: bool) -> BatteryMapError:
    if not value:
        return BatteryMapError.OK if allow_empty else BatteryMapError.EMPTY_SERIAL
    if len(value) > max_chars:
        return BatteryMapError.TOO_LONG
    if "," in value or ";" in value:
        return BatteryMapError.RESERVED_CHAR
    return BatteryMapError.OK


def validate_battery_map_entry(serial: str | None, unit_id, source_code: str | None
                               ) -> BatteryMapError:
    """`core::validateBatteryMapEntry`, có MỘT nới lỏng CÓ CHỦ Ý về `unitId`.

    Firmware bắt buộc `unitId ∈ [1, 247]` vì đó là địa chỉ Modbus RTU dùng để hỏi đúng con BMS
    trên bus RS485 — thiếu nó thì không đọc được pin.

    ⚠ Backend THẬT trả `"unitId": null` cho mọi mapping (đã đối chiếu trực tiếp trên
    `POST /api/iot-devices/provision`). Firmware vì thế loại sạch bảng backend gửi xuống và lại
    quay về bảng cứng — đúng lớp lỗi mà IOT3-49 sinh ra để chặn.

    Simulator KHÔNG nói Modbus: `unitId` với nó chỉ là nhãn. Bỏ một pin mà backend đã giao chỉ vì
    thiếu một trường không dùng tới là làm hỏng đúng thứ cần mô phỏng. Nên ở đây:
      · `unitId` vắng mặt (None / 0)  → HỢP LỆ, caller tự cấp số thứ tự thay thế;
      · `unitId` CÓ giá trị           → vẫn kiểm dải [1, 247] như firmware.
    """
    err = _check_field(serial, BATTERY_SERIAL_MAX_CHARS, allow_empty=False)
    if err is not BatteryMapError.OK:
        return err
    err = _check_field(source_code, BATTERY_SOURCE_CODE_MAX_CHARS, allow_empty=True)
    if err is not BatteryMapError.OK:
        return err
    if unit_id is None:
        return BatteryMapError.OK
    try:
        uid = int(unit_id)
    except (TypeError, ValueError):
        return BatteryMapError.BAD_UNIT_ID
    if uid == 0:
        return BatteryMapError.OK       # backend không cấp — coi như vắng mặt
    if uid < 1 or uid > 247:
        return BatteryMapError.BAD_UNIT_ID
    return BatteryMapError.OK


def encode_battery_map(entries: list[BatteryMapEntry]) -> str:
    """`core::encodeBatteryMap` — nối bảng thành chuỗi phẳng để cất NVS."""
    return ";".join(f"{e.serial},{int(e.unit_id)},{e.sensor_source_code}" for e in entries)


def decode_battery_map(text: str | None, max_out: int = MAX_BATTERY_MAP_ENTRIES
                       ) -> list[BatteryMapEntry]:
    """`core::decodeBatteryMap` — bỏ qua mục hỏng thay vì vứt cả bảng.

    Một dòng lỗi do bản cũ ghi không nên làm chết cả gateway.
    """
    out: list[BatteryMapEntry] = []
    if not text:
        return out
    for chunk in text.split(";"):
        if not chunk or len(out) >= max_out:
            continue
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        serial = parts[0]
        try:
            unit_id = int(parts[1])
        except ValueError:
            continue
        code = parts[2] if len(parts) > 2 else ""
        if validate_battery_map_entry(serial, unit_id, code) is not BatteryMapError.OK:
            continue
        out.append(BatteryMapEntry(serial=serial, unit_id=unit_id,
                                   sensor_source_code=code or "primary"))
    return out


class BatteryMap:
    """`batmap::` — bảng đang dùng lúc chạy + cờ cho biết nguồn của nó."""

    def __init__(self, device_code: str, nvs, fallback: list[BatteryMapEntry]):
        self._device_code = device_code
        self._nvs = nvs
        self._fallback = list(fallback)
        self._entries: list[BatteryMapEntry] = []
        self._from_nvs = False

    def begin(self) -> None:
        """Nạp từ NVS, trống thì dựng từ bảng seed."""
        from .nvs import KEY_BATTERY_MAP

        stored = decode_battery_map(self._nvs.get_string(KEY_BATTERY_MAP, ""))
        if stored:
            self._entries = stored
            self._from_nvs = True
        else:
            self._entries = list(self._fallback)
            self._from_nvs = False
        log.info("[%s] bảng pin: %d mục (nguồn=%s)",
                 self._device_code, len(self._entries), "nvs" if self._from_nvs else "seed")

    def apply_from_provision(self, entries: list[BatteryMapEntry]) -> bool:
        """`batmap::applyFromProvision` — ghi bảng backend trả về vào NVS + dùng ngay.

        Bảng RỖNG bị TỪ CHỐI: backend không gửi `batteryMappings[]` (bản cũ, hoặc thiết bị chưa
        được giao pin nào) không có nghĩa là "xoá hết pin đang đo". Xoá đi là thiết bị im lặng
        ngừng gửi telemetry mà không có lỗi ở đâu cả.
        """
        from .nvs import KEY_BATTERY_MAP

        if not entries:
            log.info("[%s] batteryMappings[] rỗng — giữ bảng pin đang dùng", self._device_code)
            return False

        encoded = encode_battery_map(entries)
        if encoded == self._nvs.get_string(KEY_BATTERY_MAP, ""):
            self._entries = entries
            self._from_nvs = True
            return False

        self._nvs.put_string(KEY_BATTERY_MAP, encoded)
        self._entries = entries
        self._from_nvs = True
        log.info("[%s] áp bảng pin từ provision: %s", self._device_code,
                 ", ".join(f"{e.serial}(unit={e.unit_id})" for e in entries))
        return True

    # ── truy vấn ──────────────────────────────────────────────────────────────────────────
    def entries(self) -> list[BatteryMapEntry]:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def is_from_nvs(self) -> bool:
        return self._from_nvs

    def find_by_serial(self, serial: str) -> BatteryMapEntry | None:
        for e in self._entries:
            if e.serial == serial:
                return e
        return None
