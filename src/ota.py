"""OTA firmware update — mirror `firmware-esp32/src/ota/ota_update.cpp` + `ota/ota_decision.h`
(S7-FW-01/02).

Luồng ĐẦY ĐỦ, đúng thứ tự firmware:

    GET  /api/iot-devices/firmware-check?currentVersion=...
      → so version bằng CHUỖI TUYỆT ĐỐI (khớp `CheckIotFirmwareUpdateQueryHandler` phía backend)
      → bỏ qua version đã bị đánh dấu hỏng + PUT Skipped(6)
    PUT  firmware-update-log/{id}  Downloading(2)
    GET  <downloadUrl>             tải .bin + tính SHA-256 + đối chiếu Content-Length
      → sai checksum/size/mạng → PUT Failed(5) kèm `failureReason`, GIỮ bản đang chạy
    PUT  firmware-update-log/{id}  Installing(3)
      → "reboot" sang bản mới → VERIFY-MODE
    verify: khoẻ (mạng + giờ + broker, hoặc PUT Success 2xx) → PUT Success(4)
            quá 2 phút chưa khoẻ → ROLLBACK về bản cũ → PUT RolledBack(7)

Khác thiết bị thật (chấp nhận được — simulator không có phần cứng):
  - KHÔNG ghi OTA partition, KHÔNG reboot thật. "Flash" = đổi version đang chạy (lưu bền vững)
    nên máy trạng thái verify/rollback vẫn sống qua các lần chạy, y như qua reboot.
  - KHÔNG có bootloader rollback của ESP-IDF; rollback ở đây là quay lại version cũ + báo backend.

Giống thiết bị thật 100% ở phần **tương tác backend**: đúng endpoint, đúng thứ tự status, đúng
`failureReason`, có tải artifact thật và xác minh SHA-256 thật, có chống re-OTA loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from . import nvs as nvskeys
from .policy import OtaCheckDecision, OtaCheckInputs, decide_ota_check
from .timeutil import monotonic_ms

log = logging.getLogger("iot-sim.ota")

# `IotFirmwareUpdateStatusEnum` (verified backend) — khớp `FwStatus` của firmware.
FW_STATUS_PENDING = 1
FW_STATUS_DOWNLOADING = 2
FW_STATUS_INSTALLING = 3
FW_STATUS_SUCCESS = 4
FW_STATUS_FAILED = 5
FW_STATUS_SKIPPED = 6
FW_STATUS_ROLLED_BACK = 7

# include/config.h
OTA_CHECK_INTERVAL_MS = 3600000     # poll mỗi 1h
OTA_HEALTH_TIMEOUT_MS = 120000      # 2 phút health-check sau OTA → quá thì rollback
OTA_HTTP_TIMEOUT_S = 20.0
OTA_MAX_BOOT_ATTEMPTS = 5           # chống boot-loop brick
OTA_MAX_VERSION_FAILS = 3           # phân biệt mất mạng transient vs binary hỏng
OTA_WARMUP_MS = 30000               # chờ mạng/giờ/provision ổn định sau boot


def ota_should_update(has_update: bool, current_version: str | None,
                      target_version: str | None) -> bool:
    """`ota::otaShouldUpdate`.

    Backend offer update khi `target.Version != currentVersion` (so sánh Ordinal) VÀ
    `hasUpdate=true`. `currentVersion` rỗng/None là hợp lệ — coi như "" khác mọi target không rỗng.
    """
    if not has_update:
        return False
    if not target_version:
        return False
    return (current_version or "") != target_version


def ota_sha256_equal(a: str | None, b: str | None) -> bool:
    """`ota::otaSha256Equal` — so hex KHÔNG phân biệt hoa thường, bỏ khoảng trắng thừa."""
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


@dataclass
class OtaOffer:
    has_update: bool
    target_version: str
    download_url: str
    sha256: str
    log_id: str
    size_bytes: int


def parse_firmware_check(data: dict | None) -> OtaOffer | None:
    """Đọc `IotFirmwareCheckDto` từ `CommonResponse.data`.

    Chấp nhận đúng bộ alias mà firmware đọc: `updateAvailable|hasUpdate`,
    `downloadUrl|artifactUrl`.
    """
    if not data:
        return None
    has_update = bool(data.get("updateAvailable", data.get("hasUpdate", False)))
    try:
        size_bytes = int(data.get("artifactSizeBytes", 0) or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    return OtaOffer(
        has_update=has_update,
        target_version=str(data.get("targetVersion", "") or ""),
        download_url=str(data.get("downloadUrl", data.get("artifactUrl", "")) or ""),
        sha256=str(data.get("sha256Checksum", "") or ""),
        log_id=str(data.get("updateLogId", "") or ""),
        size_bytes=size_bytes,
    )


@dataclass
class OtaResult:
    """Kết quả một chu kỳ check — dùng cho log/dashboard."""

    checked: bool = False
    updated: bool = False
    target_version: str = ""
    log_id: str = ""
    message: str = ""


class OtaManager:
    """`ota::` — toàn bộ máy trạng thái OTA, gồm cả verify-mode và rollback.

    `apply_version(v)` là callback "flash xong": device đổi version đang chạy (và lưu bền vững)
    để lần `firmware-check` kế tiếp báo đúng version — backend sẽ thấy trùng target và ngừng offer.
    """

    def __init__(self, http, store, device_code: str,
                 current_version_getter: Callable[[], str],
                 apply_version: Callable[[str], None],
                 enabled: bool = True,
                 check_interval_ms: int = OTA_CHECK_INTERVAL_MS,
                 health_timeout_ms: int = OTA_HEALTH_TIMEOUT_MS,
                 max_boot_attempts: int = OTA_MAX_BOOT_ATTEMPTS,
                 max_version_fails: int = OTA_MAX_VERSION_FAILS,
                 warmup_ms: int = OTA_WARMUP_MS,
                 download_timeout_s: float = OTA_HTTP_TIMEOUT_S):
        self._http = http
        self._nvs = store
        self.device_code = device_code
        self._current = current_version_getter
        self._apply = apply_version

        self.enabled = bool(enabled)
        self.check_interval_ms = int(check_interval_ms)
        self.health_timeout_ms = int(health_timeout_ms)
        self.max_boot_attempts = int(max_boot_attempts)
        self.max_version_fails = int(max_version_fails)
        self.warmup_ms = int(warmup_ms)
        self.download_timeout_s = float(download_timeout_s)

        self.verify_mode = False
        self.report_rollback = False
        self._verify_deadline_ms = 0
        self._last_check_ms = 0
        self._force_check = False
        self._last_reject = ""
        self._boot_ms = monotonic_ms()

        self.check_count = 0
        self.update_ok_count = 0
        self.rollback_count = 0
        self.failed_count = 0
        self.last_message = ""

        self._log_id = ""
        self._to_ver = ""
        self._from_ver = ""

    # ── helper NVS ────────────────────────────────────────────────────────────────────────
    def _put_log(self, log_id: str, status: int, size_bytes: int | None = None,
                 reason: str | None = None) -> bool:
        if not log_id:
            return False
        res = self._http.firmware_update_log(log_id, status,
                                             bytes_downloaded=size_bytes,
                                             failure_reason=reason)
        log.info("[%s] PUT log status=%d → %d %s", self.device_code, status,
                 res.status_code, "OK" if res.ok else "FAIL")
        return res.ok

    def _mark_bad_version(self, version: str) -> None:
        if version:
            self._nvs.put_string(nvskeys.KEY_OTA_BAD_VER, version)

    def _clear_failure_tracking(self) -> None:
        """Version mới verify OK → xoá sạch bad-marker + bộ đếm fail."""
        self._nvs.put_string(nvskeys.KEY_OTA_BAD_VER, "")
        self._nvs.put_string(nvskeys.KEY_OTA_FAIL_VER, "")
        self._nvs.put_int(nvskeys.KEY_OTA_FAIL_N, 0)

    def _record_version_failure(self, version: str) -> None:
        """Đếm số chu kỳ OTA→rollback FAIL của MỘT version.

        Phân biệt hai thứ rất khác nhau:
          - Mất mạng thoáng qua: fail 1–2 lần rồi mạng về → verify OK → xoá đếm → KHÔNG chặn.
          - Binary thực sự hỏng kết nối: fail đủ N lần → mark bad → dừng vòng lặp OTA vô hạn.
        """
        if not version:
            return
        fail_ver = self._nvs.get_string(nvskeys.KEY_OTA_FAIL_VER, "")
        n = self._nvs.get_int(nvskeys.KEY_OTA_FAIL_N, 0) if fail_ver == version else 0
        n += 1
        self._nvs.put_string(nvskeys.KEY_OTA_FAIL_VER, version)
        self._nvs.put_int(nvskeys.KEY_OTA_FAIL_N, n)
        log.warning("[%s] version %s fail verify lần %d/%d", self.device_code, version, n,
                    self.max_version_fails)
        if n >= self.max_version_fails:
            log.warning("[%s] version %s fail %d lần → ĐÁNH DẤU HỎNG (nghi binary lỗi)",
                        self.device_code, version, n)
            self._mark_bad_version(version)

    def _clear_verify_state(self) -> None:
        self._nvs.put_bool(nvskeys.KEY_OTA_PENDING, False)
        self._nvs.put_int(nvskeys.KEY_OTA_BOOT_N, 0)
        self.verify_mode = False

    # ── begin ─────────────────────────────────────────────────────────────────────────────
    def begin(self) -> None:
        """`otaBegin` — gọi SỚM, trước mọi init rủi ro khác.

        Bộ đếm boot phải tăng được ngay cả khi bản mới hỏng ở các bước init sau đó — nếu không thì
        không bao giờ rollback được.
        """
        if not self.enabled:
            log.info("[%s] OTA tắt (ota_enabled=false)", self.device_code)
            return

        self._log_id = self._nvs.get_string(nvskeys.KEY_OTA_LOG_ID, "")
        self._to_ver = self._nvs.get_string(nvskeys.KEY_OTA_TO_VER, "")
        self._from_ver = self._nvs.get_string(nvskeys.KEY_OTA_FROM_VER, "")

        if self._nvs.get_bool(nvskeys.KEY_OTA_PENDING, False):
            boot_n = self._nvs.get_int(nvskeys.KEY_OTA_BOOT_N, 0) + 1
            self._nvs.put_int(nvskeys.KEY_OTA_BOOT_N, boot_n)
            if boot_n > self.max_boot_attempts:
                log.warning("[%s] boot lần %d > tối đa %d mà chưa confirm → ROLLBACK "
                            "(nghi boot-loop)", self.device_code, boot_n, self.max_boot_attempts)
                self._enter_rollback(mark_bad=True)
                return
            self.verify_mode = True
            self._verify_deadline_ms = monotonic_ms() + self.health_timeout_ms
            log.warning("[%s] khởi động sau OTA — VERIFY mode (target=%s, boot %d/%d, health 2')",
                        self.device_code, self._to_ver, boot_n, self.max_boot_attempts)
        elif self._nvs.get_bool(nvskeys.KEY_OTA_ROLLED_BACK, False):
            self.report_rollback = True
            log.warning("[%s] khởi động sau ROLLBACK (%s) — sẽ báo backend khi có mạng",
                        self.device_code, self._from_ver)
        else:
            log.info("[%s] OTA sẵn sàng (current=%s, poll mỗi %ds)", self.device_code,
                     self._current(), self.check_interval_ms // 1000)

    # ── tick ──────────────────────────────────────────────────────────────────────────────
    def tick(self, link_up: bool, time_synced: bool, broker_up: bool) -> None:
        """`otaTick`. Verify-mode chạy KỂ CẢ khi mất mạng (phải bắt được hạn rollback)."""
        if not self.enabled:
            return

        if self.verify_mode:
            self._handle_verify(link_up, time_synced, broker_up)
            return

        if self.report_rollback:
            self._handle_report_rollback(link_up, time_synced)
            # không return — vẫn cho phép check bình thường (sẽ bỏ qua version hỏng)

        if not link_up or not time_synced:
            return

        now = monotonic_ms()
        decision = decide_ota_check(OtaCheckInputs(
            enabled=self.enabled,
            verifying=self.verify_mode,
            forced=self._force_check,
            last_check_ms=self._last_check_ms,
            now_ms=now - self._boot_ms,   # tính theo uptime để warm-up có nghĩa
            interval_ms=self.check_interval_ms,
            warmup_ms=self.warmup_ms,
        ))
        if decision is not OtaCheckDecision.RUN:
            return

        if self._force_check:
            log.info("[%s] check do lệnh trigger_ota (bỏ qua khoảng chờ định kỳ)",
                     self.device_code)
            self._force_check = False
        self._last_check_ms = now - self._boot_ms
        self.check_and_apply()

    def request_check(self) -> bool:
        """`otaRequestCheck` — lệnh downlink `trigger_ota`. Trả False kèm lý do từ chối."""
        if not self.enabled:
            self._last_reject = "OTA disabled"
            log.info("[%s] trigger_ota TỪ CHỐI — OTA tắt bằng cấu hình", self.device_code)
            return False
        if self.verify_mode:
            self._last_reject = "verifying previous update"
            log.info("[%s] trigger_ota TỪ CHỐI — đang xác minh bản vừa flash", self.device_code)
            return False
        self._force_check = True
        self._last_reject = ""
        log.info("[%s] trigger_ota ĐÃ NHẬN — sẽ check ở tick kế tiếp", self.device_code)
        return True

    def last_reject_reason(self) -> str:
        return self._last_reject

    # ── check + apply ─────────────────────────────────────────────────────────────────────
    def check_and_apply(self) -> OtaResult:
        """`doCheckAndApply`."""
        self.check_count += 1
        current = self._current()

        res = self._http.firmware_check(current)
        if not res.ok:
            msg = f"firmware-check FAIL code={res.status_code}"
            log.warning("[%s] %s", self.device_code, msg)
            self.last_message = msg
            return OtaResult(checked=False, message=msg)

        data = res.json.get("data") if isinstance(res.json, dict) else None
        offer = parse_firmware_check(data)
        if offer is None:
            self.last_message = "firmware-check không có data"
            return OtaResult(checked=True, message=self.last_message)

        if not ota_should_update(offer.has_update, current, offer.target_version):
            self.last_message = f"không có bản mới (current={current})"
            log.info("[%s] %s", self.device_code, self.last_message)
            return OtaResult(checked=True, message=self.last_message)

        if not offer.download_url or not offer.sha256 or not offer.log_id:
            # Firmware cũng skip khi thiếu bất kỳ cái nào — không có SHA thì không xác minh được,
            # mà cài một binary chưa xác minh còn tệ hơn không cài.
            self.last_message = "có bản mới nhưng thiếu url/sha/logId — bỏ qua"
            log.warning("[%s] %s", self.device_code, self.last_message)
            return OtaResult(checked=True, target_version=offer.target_version,
                             message=self.last_message)

        bad_ver = self._nvs.get_string(nvskeys.KEY_OTA_BAD_VER, "")
        if bad_ver and bad_ver == offer.target_version:
            log.warning("[%s] BỎ QUA version đã xác định hỏng %s (đã rollback) — chờ bản mới",
                        self.device_code, offer.target_version)
            self._put_log(offer.log_id, FW_STATUS_SKIPPED,
                          reason="known-bad version (previously rolled back)")
            self.last_message = f"bỏ qua version hỏng {offer.target_version}"
            return OtaResult(checked=True, target_version=offer.target_version,
                             log_id=offer.log_id, message=self.last_message)

        log.warning("[%s] OTA %s → %s (%d byte) log=%s", self.device_code, current,
                    offer.target_version, offer.size_bytes, offer.log_id)

        # Ghi trạng thái TRƯỚC khi tải: mất điện giữa chừng thì lần chạy sau vẫn biết đang dở việc.
        self._log_id = offer.log_id
        self._to_ver = offer.target_version
        self._from_ver = current
        self._nvs.put_string(nvskeys.KEY_OTA_LOG_ID, offer.log_id)
        self._nvs.put_string(nvskeys.KEY_OTA_TO_VER, offer.target_version)
        self._nvs.put_string(nvskeys.KEY_OTA_FROM_VER, current)
        self._nvs.put_int(nvskeys.KEY_OTA_BOOT_N, 0)
        self._nvs.put_bool(nvskeys.KEY_OTA_PENDING, True)

        self._put_log(offer.log_id, FW_STATUS_DOWNLOADING)

        ok, written, digest, err = self._http.download_artifact(
            offer.download_url, offer.sha256, timeout_s=self.download_timeout_s)
        if not ok:
            # Tải/kiểm hỏng: KHÔNG mark bad → lần poll sau thử lại (tự lành nếu do mạng).
            # Artifact hỏng vĩnh viễn thì mỗi chu kỳ sinh một log Failed → admin nhìn thấy.
            self.failed_count += 1
            log.error("[%s] tải/xác minh firmware HỎNG: %s (đã tải %d byte, sha=%s)",
                      self.device_code, err, written, digest[:16] or "?")
            self._put_log(offer.log_id, FW_STATUS_FAILED, size_bytes=written, reason=err)
            self._nvs.put_bool(nvskeys.KEY_OTA_PENDING, False)
            self.last_message = f"OTA thất bại: {err}"
            return OtaResult(checked=True, target_version=offer.target_version,
                             log_id=offer.log_id, message=self.last_message)

        log.info("[%s] SHA-256 OK (%d byte) — 'flash' + chuyển sang bản %s",
                 self.device_code, written, offer.target_version)
        self._put_log(offer.log_id, FW_STATUS_INSTALLING, size_bytes=written or offer.size_bytes)

        # "Reboot" sang bản mới → verify-mode.
        self._apply(offer.target_version)
        self.verify_mode = True
        self._verify_deadline_ms = monotonic_ms() + self.health_timeout_ms
        self._nvs.put_int(nvskeys.KEY_OTA_BOOT_N, 1)
        self.last_message = f"đang xác minh {offer.target_version}"
        return OtaResult(checked=True, updated=True, target_version=offer.target_version,
                         log_id=offer.log_id, message=self.last_message)

    # ── verify + rollback ─────────────────────────────────────────────────────────────────
    def _handle_verify(self, link_up: bool, time_synced: bool, broker_up: bool) -> None:
        """`handleVerify` — sức khoẻ = nối được, KHÔNG gắn cứng vào PUT Success 2xx.

        Gắn cứng vào 2xx sẽ rollback nhầm một bản firmware TỐT chỉ vì backend đang tạm lỗi.
        """
        running = self._current()
        if running != self._to_ver:
            log.error("[%s] verify: đang chạy %s != target %s → flash không áp dụng → Failed",
                      self.device_code, running, self._to_ver)
            self._mark_bad_version(self._to_ver)
            self._put_log(self._log_id, FW_STATUS_FAILED,
                          reason="boot version mismatch (flash not applied)")
            self.failed_count += 1
            self._clear_verify_state()
            return

        healthy = link_up and time_synced and broker_up
        if not healthy and link_up and time_synced:
            # Broker chưa lên nhưng HTTPS tới được backend cũng tính là khoẻ.
            if self._put_log(self._log_id, FW_STATUS_SUCCESS):
                self._confirm_success(via="HTTPS")
                return

        if healthy:
            self._put_log(self._log_id, FW_STATUS_SUCCESS)
            self._confirm_success(via="broker")
            return

        if monotonic_ms() > self._verify_deadline_ms:
            log.error("[%s] health-check HỎNG trong %ds → ROLLBACK (transient — không mark bad)",
                      self.device_code, self.health_timeout_ms // 1000)
            self._enter_rollback(mark_bad=False)

    def _confirm_success(self, via: str) -> None:
        self._clear_failure_tracking()
        self._clear_verify_state()
        self.update_ok_count += 1
        self.last_message = f"đã xác nhận {self._to_ver}"
        log.warning("[%s] verify OK (%s) — %s đã được xác nhận", self.device_code, via,
                    self._to_ver)

    def _enter_rollback(self, mark_bad: bool) -> None:
        """`enterRollback`.

        `mark_bad=True`  → nghi BINARY lỗi rõ (boot-loop) → chặn re-OTA version này NGAY.
        `mark_bad=False` → rollback do MẤT KẾT NỐI → đếm fail per-version, đủ N lần mới chặn.
        """
        if mark_bad:
            self._mark_bad_version(self._to_ver)
        else:
            self._record_version_failure(self._to_ver)

        self._nvs.put_bool(nvskeys.KEY_OTA_ROLLED_BACK, True)
        self._put_log(self._log_id, FW_STATUS_ROLLED_BACK,
                      reason="health/boot check failed after OTA")
        self._nvs.put_bool(nvskeys.KEY_OTA_PENDING, False)
        self._nvs.put_int(nvskeys.KEY_OTA_BOOT_N, 0)
        self.verify_mode = False
        self.rollback_count += 1

        if self._from_ver:
            log.error("[%s] ROLLBACK %s → %s", self.device_code, self._to_ver, self._from_ver)
            self._apply(self._from_ver)
        else:
            log.error("[%s] ROLLBACK nhưng KHÔNG biết version cũ — giữ bản đang chạy",
                      self.device_code)
        self.report_rollback = True
        self.last_message = f"đã rollback về {self._from_ver or 'bản cũ'}"

    def _handle_report_rollback(self, link_up: bool, time_synced: bool) -> None:
        """`handleReportRollback` — nguyên nhân rollback thường là mất mạng, nên phải báo sau."""
        if not link_up or not time_synced:
            return
        reason = f"rolled back {self._from_ver}→{self._to_ver} (health fail)"
        if self._put_log(self._log_id, FW_STATUS_ROLLED_BACK, reason=reason):
            self._nvs.put_bool(nvskeys.KEY_OTA_ROLLED_BACK, False)
            self.report_rollback = False
            log.info("[%s] đã báo RolledBack lên backend", self.device_code)
