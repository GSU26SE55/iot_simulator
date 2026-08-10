"""Đèn trạng thái — mirror `firmware-esp32/src/ui/status_led.h` + `ui/led_palette.h`
(S3-FW-05 + IOT3-54).

Đây là công cụ chẩn đoán DUY NHẤT mà khách hàng dùng được qua điện thoại ("đèn màu gì? có nháy
không?"), nên simulator phải nói cùng một ngôn ngữ với thiết bị thật — 8 trạng thái và 3 kiểu
nháy, không phải 4 trạng thái tĩnh như bản cũ.

    Off           — tắt
    Online        — xanh, sáng đều      — mọi thứ bình thường
    Queued        — xanh NHÁY           — còn hàng đợi chưa đẩy hết
    Offline       — đỏ                  — backend không với tới
    Provisioning  — tím, sáng đều       — đang gọi /provision
    Setup         — tím NHÁY            — chưa cấu hình (thiếu deviceCode/apiKey)
    WifiSearching — cam, sáng đều       — mất kết nối, đang thử lại
    Recovery      — tím/cam XEN KẼ      — mất kết nối lâu, vẫn kiên trì thử lại

Quy ước "có nháy = cần người xử lý, sáng đều = cứ để yên" giúp nói qua điện thoại được: hỏi
"đèn có nháy không?" trước cả khi hỏi màu gì.
"""
from __future__ import annotations

from enum import Enum

# `kBlinkHalfPeriodMs` — 500ms mỗi nửa chu kỳ.
BLINK_HALF_PERIOD_MS = 500


class LedState(Enum):
    OFF = 0
    ONLINE = 1
    QUEUED = 2
    OFFLINE = 3
    PROVISIONING = 4
    SETUP = 5
    WIFI_SEARCHING = 6
    RECOVERY = 7


class LedPattern(Enum):
    SOLID = 0
    BLINK = 1
    ALTERNATE = 2


# `paletteForState` — độ sáng thấp (≤ 32/255) để không chói khi test ban đêm.
_PALETTE = {
    LedState.OFF: (0, 0, 0),
    LedState.ONLINE: (0, 32, 0),            # xanh lá
    LedState.QUEUED: (0, 32, 0),            # xanh lá — phân biệt bằng NHÁY
    LedState.OFFLINE: (32, 0, 0),           # đỏ
    LedState.PROVISIONING: (16, 0, 32),     # tím
    LedState.SETUP: (16, 0, 32),            # tím — phân biệt bằng NHÁY
    LedState.WIFI_SEARCHING: (32, 12, 0),   # cam
    LedState.RECOVERY: (16, 0, 32),         # tím, xen kẽ với cam
}

_SECONDARY_PALETTE = {
    LedState.RECOVERY: (32, 12, 0),         # cam — nửa còn lại của nhịp
}

_PATTERN = {
    LedState.QUEUED: LedPattern.BLINK,
    LedState.SETUP: LedPattern.BLINK,
    LedState.RECOVERY: LedPattern.ALTERNATE,
}

# Nhãn hiển thị trên dashboard.
LABELS = {
    LedState.OFF: "Off",
    LedState.ONLINE: "Online",
    LedState.QUEUED: "Queued",
    LedState.OFFLINE: "Offline",
    LedState.PROVISIONING: "Provisioning",
    LedState.SETUP: "Setup",
    LedState.WIFI_SEARCHING: "WifiSearching",
    LedState.RECOVERY: "Recovery",
}


def palette_for_state(state: LedState) -> tuple[int, int, int]:
    return _PALETTE.get(state, (0, 0, 0))


def secondary_palette_for_state(state: LedState) -> tuple[int, int, int]:
    return _SECONDARY_PALETTE.get(state, palette_for_state(state))


def pattern_for_state(state: LedState) -> LedPattern:
    return _PATTERN.get(state, LedPattern.SOLID)


def is_lit(state: LedState, now_ms: int) -> bool:
    """Nửa chu kỳ hiện tại có sáng không — dùng để vẽ chấm nhấp nháy trên dashboard.

    Không bơm nhịp thì `Setup` trông y hệt `Provisioning` và `Recovery` trông y hệt `Setup`,
    mất sạch giá trị chẩn đoán mà không có gì báo lỗi (đúng lý do firmware có `ledTick`).
    """
    if pattern_for_state(state) is not LedPattern.BLINK:
        return True
    return ((now_ms // BLINK_HALF_PERIOD_MS) % 2) == 0


def current_color(state: LedState, now_ms: int) -> tuple[int, int, int]:
    """Màu đang hiển thị tại thời điểm `now_ms`, đã tính cả nháy/xen kẽ."""
    pattern = pattern_for_state(state)
    if pattern is LedPattern.SOLID:
        return palette_for_state(state)
    first_half = ((now_ms // BLINK_HALF_PERIOD_MS) % 2) == 0
    if pattern is LedPattern.BLINK:
        return palette_for_state(state) if first_half else (0, 0, 0)
    return palette_for_state(state) if first_half else secondary_palette_for_state(state)
