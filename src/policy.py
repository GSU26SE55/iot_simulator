"""Các quyết định THUẦN tách khỏi vòng lặp chính — mirror thư mục `core/` của firmware.

  - `core/ingest_policy.h`      → ingest_action()
  - `core/ota_check_policy.h`   → decide_ota_check()
  - `core/reprovision_policy.h` → should_reprovision_on_auth_failure()
  - `core/retry_gate.h`         → should_attempt_report()

Tách ra để test được độc lập với I/O — đúng lý do firmware tách chúng khỏi `main.cpp`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# core/reprovision_policy.h — 15 phút.
REPROVISION_COOLDOWN_MS = 15 * 60 * 1000


class IngestAction(Enum):
    """`core::IngestAction` — làm gì với một chu kỳ lấy mẫu."""

    POST_ONLINE = 1     # có mạng + có giờ → đọc BMS rồi gửi (MQTT, fallback HTTPS)
    QUEUE_OFFLINE = 2   # mất mạng nhưng đồng hồ còn → VẪN lấy mẫu, xếp hàng, đẩy bù sau
    SKIP_NO_CLOCK = 3   # chưa có mốc thời gian hợp lệ → không tạo được bản ghi


def ingest_action(link_connected: bool, time_synced: bool) -> IngestAction:
    """`core::ingestAction` (GH-737).

    Mất mạng KHÔNG được phép làm mất dữ liệu: đồng hồ vẫn chạy nên bản ghi xếp hàng vẫn mang
    mốc thời gian THẬT của lúc lấy mẫu, không phải giờ lúc đẩy bù.
    """
    if not time_synced:
        return IngestAction.SKIP_NO_CLOCK
    return IngestAction.POST_ONLINE if link_connected else IngestAction.QUEUE_OFFLINE


class OtaCheckDecision(Enum):
    """`core::OtaCheckDecision` — nêu RÕ lý do để ack `trigger_ota` trả đúng nguyên nhân."""

    RUN = 1
    SKIP_DISABLED = 2
    SKIP_VERIFYING = 3
    SKIP_WARMUP = 4
    SKIP_TOO_SOON = 5


@dataclass
class OtaCheckInputs:
    """`core::OtaCheckInputs`."""

    enabled: bool = True
    verifying: bool = False
    forced: bool = False        # có lệnh trigger_ota đang chờ
    last_check_ms: int = 0      # 0 = chưa từng check
    now_ms: int = 0
    interval_ms: int = 3600000
    warmup_ms: int = 30000


def decide_ota_check(inp: OtaCheckInputs) -> OtaCheckDecision:
    """`core::decideOtaCheck` (GH-745).

    Thứ tự ưu tiên có chủ ý: enabled → verifying → forced → thời gian.
    `forced` KHÔNG vượt qua `verifying`: đang xác minh bản vừa flash mà tải tiếp bản mới là mất
    luôn đường lùi khi bản mới hỏng. Riêng warm-up thì forced ĐƯỢC vượt — người vận hành bấm
    "cập nhật ngay" ngay sau khi cắm điện là tình huống bình thường.
    """
    if not inp.enabled:
        return OtaCheckDecision.SKIP_DISABLED
    if inp.verifying:
        return OtaCheckDecision.SKIP_VERIFYING
    if inp.forced:
        return OtaCheckDecision.RUN
    if inp.last_check_ms == 0:
        return (OtaCheckDecision.SKIP_WARMUP if inp.now_ms < inp.warmup_ms
                else OtaCheckDecision.RUN)
    return (OtaCheckDecision.RUN if (inp.now_ms - inp.last_check_ms) >= inp.interval_ms
            else OtaCheckDecision.SKIP_TOO_SOON)


def should_reprovision_on_auth_failure(auth_fail_streak: int, threshold: int, now_ms: int,
                                       last_reprovision_ms: int, ever_reprovisioned: bool,
                                       cooldown_ms: int = REPROVISION_COOLDOWN_MS) -> bool:
    """`core::shouldReprovisionOnAuthFailure` (IOT3-44).

    Broker từ chối đăng nhập thì chờ bao lâu cũng vô ích — mật khẩu trong state đã không còn
    khớp `mqtt_password_plaintext` bên backend. Đường tự lành duy nhất là gọi lại `/provision`.
    Hai chốt chặn để không biến sự cố backend thành bão request:
      1. NGƯỠNG  — đủ N lần từ chối LIÊN TIẾP mới đi.
      2. HẠ NHIỆT — đã đi rồi thì im trong T phút, dù bị từ chối tiếp.
    """
    if threshold < 1:
        return False
    if auth_fail_streak < threshold:
        return False
    if not ever_reprovisioned:
        return True
    return (now_ms - last_reprovision_ms) >= cooldown_ms


def should_attempt_report(pending: bool, now_ms: int, next_allowed_ms: int) -> bool:
    """`core::shouldAttemptReport` (GH-741) — cổng chặn bão retry của cảm biến an toàn."""
    if not pending:
        return False
    return now_ms >= next_allowed_ms
