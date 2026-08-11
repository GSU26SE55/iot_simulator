"""Bộ nhớ không mất khi tắt nguồn — mirror `firmware-esp32/src/config/nvs_store.cpp`.

Trên ESP32 đây là partition NVS (Preferences). Ở simulator là MỘT file JSON cho MỖI thiết bị:
`<state_dir>/<device_code>.nvs.json`.

Vì sao simulator CẦN cái này (trước đây không có):
  1. `provd` — firmware chỉ gọi `/provision` ở LẦN BOOT ĐẦU; các lần sau nạp cấu hình từ NVS.
     Không có persist thì simulator provision lại mỗi lần chạy, tức là mô phỏng SAI luồng
     backend nhìn thấy.
  2. `mqhost/mqport/mqtls/mquser/mqpass/mqprefix` — credential broker do `/provision` cấp lúc
     chạy (IOT3-42). Mất chúng sau mỗi lần chạy nghĩa là MQTT chỉ hoạt động khi vừa provision.
  3. `batmap` — bảng ánh xạ pin backend trả về (IOT3-49).
  4. `otaPend/otaBootN/otaRb/otaLogId/otaToVer/otaFromVer/otaBadVer/otaFailN/otaFailVer` —
     máy trạng thái OTA verify/rollback BẮT BUỘC phải sống qua "reboot" mới có ý nghĩa.

Khoá giữ NGUYÊN tên của firmware (≤ 15 ký tự theo giới hạn Preferences) để đối chiếu log hai bên.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("iot-sim.nvs")

# ── Khoá NVS — PHẢI khớp firmware (grep `kKey` trong config/ + provision/ + ota/) ────────────
KEY_PROVISIONED = "provd"        # uint8 1 = đã provision
KEY_SITE_ID = "siteid"           # string Guid site
KEY_POLL_MS = "pollIntS"         # int32 — chu kỳ ingest tính bằng MS (tên kế thừa firmware)
KEY_HB_MS = "hbIntS"             # int32 — chu kỳ heartbeat tính bằng MS
KEY_NTP = "ntpsv"                # string NTP server
KEY_FW_VER = "fwver"             # string firmware version lúc provision

KEY_MQTT_HOST = "mqhost"
KEY_MQTT_PORT = "mqport"
KEY_MQTT_TLS = "mqtls"
KEY_MQTT_USER = "mquser"
KEY_MQTT_PASS = "mqpass"
KEY_MQTT_PREFIX = "mqprefix"

KEY_BATTERY_MAP = "batmap"

KEY_OTA_PENDING = "otaPend"      # uint8 1 = FW mới chờ verify
KEY_OTA_BOOT_N = "otaBootN"      # uint8 số lần boot từ khi flash mà chưa confirm
KEY_OTA_ROLLED_BACK = "otaRb"    # uint8 1 = đã rollback, FW cũ cần report
KEY_OTA_LOG_ID = "otaLogId"
KEY_OTA_TO_VER = "otaToVer"
KEY_OTA_FROM_VER = "otaFromVer"
KEY_OTA_BAD_VER = "otaBadVer"    # version đã xác định lỗi → skip re-OTA
KEY_OTA_FAIL_N = "otaFailN"
KEY_OTA_FAIL_VER = "otaFailVer"

# Version đang chạy sau khi "flash" (riêng simulator — thiết bị thật lấy từ FW_VERSION compile-in).
KEY_RUNNING_FW = "runfw"


class NvsStore:
    """Key/value bền vững, ghi nguyên tử (tmp + rename) để không hỏng file khi bị Ctrl-C."""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self._data: dict[str, Any] = {}
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    # ── I/O ────────────────────────────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as ex:
            # State hỏng thì BỎ và bắt đầu lại — giống firmware tự format NVS khi mount fail.
            # Giữ file hỏng lại để còn truy nguyên nhân.
            log.warning("state file hỏng (%s) — bỏ qua, bắt đầu như thiết bị mới: %s",
                        self.path, ex)
            return
        if isinstance(raw, dict):
            self._data = raw

    def _flush(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as ex:
            log.warning("không ghi được state file %s: %s", self.path, ex)

    # ── API mirror storage:: của firmware ──────────────────────────────────────────────────
    def has_key(self, key: str) -> bool:
        return key in self._data

    def get_string(self, key: str, default: str = "") -> str:
        v = self._data.get(key, default)
        return v if isinstance(v, str) else default

    def put_string(self, key: str, value: str) -> bool:
        self._data[key] = "" if value is None else str(value)
        self._flush()
        return True

    def get_int(self, key: str, default: int = 0) -> int:
        v = self._data.get(key, default)
        if isinstance(v, bool):
            return default
        if isinstance(v, int):
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def put_int(self, key: str, value: int) -> bool:
        self._data[key] = int(value)
        self._flush()
        return True

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.get_int(key, 1 if default else 0) == 1

    def put_bool(self, key: str, value: bool) -> bool:
        return self.put_int(key, 1 if value else 0)

    def erase(self) -> bool:
        """`clear` của Serial CLI — xoá sạch, quay về cấu hình compile-time (ở đây là seed)."""
        self._data = {}
        self._flush()
        return True
