"""OTA firmware update — mô phỏng Sprint 7 firmware ESP32 (S7-FW-01/02).

Khớp firmware `src/ota/ota_decision.h` + `src/ota/ota_update.cpp`:
  1. GET /api/iot-devices/firmware-check?currentVersion=...  → CommonResponse<IotFirmwareCheckDto>
  2. Quyết định update: `ota_should_update` so version bằng CHUỖI tuyệt đối
     (StringComparison.Ordinal — khớp backend CheckIotFirmwareUpdateQueryHandler).
  3. PUT /api/iot-devices/firmware-update-log/{logId} theo lifecycle:
       Downloading(2) → Installing(3) → Success(4)
     Backend khi Success set IotDevice.CurrentFirmwareVersion = target → lần check sau
     KHÔNG còn offer update (version trùng).

Khác firmware (chấp nhận — không có hardware/partition thật):
  - KHÔNG tải .bin / verify SHA-256 / ghi OTA partition / reboot.
    Simulator "flash" = bump `firmware_version` in-memory + report Success.
  - KHÔNG có verify-mode boot-counter / rollback partition (cần 2 OTA slot vật lý).
    Rollback chỉ xảy ra trên board thật; ở demo dùng firmware ESP32.

OTA chỉ chạy khi contract = iot2-production (cần per-device API key + scope FirmwareCheck).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("iot-sim.ota")

# IotFirmwareUpdateStatusEnum (verified backend) — KHỚP firmware ota_update.cpp FwStatus.
FW_STATUS_DOWNLOADING = 2
FW_STATUS_INSTALLING = 3
FW_STATUS_SUCCESS = 4
FW_STATUS_FAILED = 5
FW_STATUS_SKIPPED = 6


def ota_should_update(has_update: bool, current_version: str | None, target_version: str | None) -> bool:
    """Mirror `ota::otaShouldUpdate` (ota_decision.h).

    Backend offer update khi target.Version != currentVersion (Ordinal) VÀ hasUpdate=true.
    null/empty currentVer hợp lệ — coi như "" khác mọi target không rỗng.
    """
    if not has_update:
        return False
    if not target_version:
        return False
    return (current_version or "") != target_version


@dataclass
class OtaOffer:
    has_update: bool
    target_version: str
    download_url: str
    sha256: str
    log_id: str
    size_bytes: int


def parse_firmware_check(data: dict | None) -> OtaOffer | None:
    """Đọc IotFirmwareCheckDto từ `CommonResponse.data`.

    Backend field (System.Text.Json camelCase): updateAvailable | hasUpdate, targetVersion,
    downloadUrl | artifactUrl, sha256Checksum, updateLogId, artifactSizeBytes.
    Khớp firmware ota_update.cpp::doCheckAndApply (đọc cùng các alias này).
    """
    if not data:
        return None
    has_update = bool(data.get("updateAvailable", data.get("hasUpdate", False)))
    return OtaOffer(
        has_update=has_update,
        target_version=str(data.get("targetVersion", "") or ""),
        download_url=str(data.get("downloadUrl", data.get("artifactUrl", "")) or ""),
        sha256=str(data.get("sha256Checksum", "") or ""),
        log_id=str(data.get("updateLogId", "") or ""),
        size_bytes=int(data.get("artifactSizeBytes", 0) or 0),
    )


@dataclass
class OtaResult:
    checked: bool                 # đã gọi firmware-check thành công?
    updated: bool                 # đã "flash" version mới?
    target_version: str = ""
    log_id: str = ""
    message: str = ""


class OtaRunner:
    """Orchestrate 1 chu kỳ OTA. Network qua `http` (IotHttpClient).

    `apply_version(new_ver)` callback để device cập nhật firmware_version đang chạy
    (dùng cho heartbeat/firmware-check kế tiếp).
    """

    def __init__(self, http, current_version_getter: Callable[[], str],
                 apply_version: Callable[[str], None]):
        self._http = http
        self._current = current_version_getter
        self._apply = apply_version
        self.check_count = 0
        self.update_ok_count = 0

    def check_and_apply(self) -> OtaResult:
        self.check_count += 1
        current = self._current()
        res = self._http.firmware_check(current)
        if not res.ok:
            return OtaResult(checked=False, updated=False, message=f"firmware-check {res.status_code}")

        data = (res.json or {}).get("data") if isinstance(res.json, dict) else None
        offer = parse_firmware_check(data)
        if offer is None:
            return OtaResult(checked=True, updated=False, message="no data")

        if not ota_should_update(offer.has_update, current, offer.target_version):
            return OtaResult(checked=True, updated=False, message=f"no update (current={current})")

        if not offer.log_id or not offer.download_url:
            # firmware cũng skip nếu thiếu url/sha/logId.
            return OtaResult(checked=True, updated=False,
                             target_version=offer.target_version, message="offer thiếu url/logId")

        log.info("[ota] UPDATE %s → %s (log=%s)", current, offer.target_version, offer.log_id)

        # Lifecycle log — best-effort (lỗi PUT không chặn flash mô phỏng).
        self._http.firmware_update_log(offer.log_id, FW_STATUS_DOWNLOADING)
        self._http.firmware_update_log(offer.log_id, FW_STATUS_INSTALLING,
                                       bytes_downloaded=offer.size_bytes or None)

        # "Flash" — bump version (simulator không có partition thật).
        self._apply(offer.target_version)
        res_log = self._http.firmware_update_log(offer.log_id, FW_STATUS_SUCCESS,
                                                 bytes_downloaded=offer.size_bytes or None)
        if res_log.ok:
            self.update_ok_count += 1
        log.info("[ota] flash OK → now running %s (log Success %s)",
                 offer.target_version, res_log.status_code)
        return OtaResult(checked=True, updated=True, target_version=offer.target_version,
                         log_id=offer.log_id, message=f"updated → {offer.target_version}")
