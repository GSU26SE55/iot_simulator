"""Đồng hồ + mốc thời gian — mirror `firmware-esp32/src/net/time_sync.cpp`.

⚠ ĐIỂM CONTRACT QUAN TRỌNG NHẤT CỦA FILE NÀY:
Firmware sinh timestamp bằng ``strftime("%Y-%m-%dT%H:%M:%SZ")`` → **độ phân giải GIÂY**,
hậu tố ``Z``, KHÔNG có phần lẻ giây:

    2026-06-13T08:15:42Z

Phần mili-giây (nếu có) do `core::payload.cpp::patchItemTimestamp` vá thêm theo index item,
KHÔNG phải do đồng hồ sinh ra. Trước đây simulator dùng `datetime.isoformat()` →
``2026-06-13T08:15:42.789012+00:00`` rồi replace ``+00:00``→``Z``, tức là gửi lên backend một
chuỗi mà thiết bị thật KHÔNG BAO GIỜ phát ra (lỗi cùng lớp với GH-739 bên repo `iot`).

Mọi timestamp gửi backend PHẢI đi qua `iso_now()` / `iso_now_minus()` trong file này.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# Format duy nhất được phép gửi lên backend (khớp net::isoNow).
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# net::isoNowMinus — không cho lùi quá 7 ngày (sai lệch lớn hơn gần như luôn là lỗi tính toán).
MAX_BACKDATE_SECONDS = 7 * 24 * 3600


def monotonic_ms() -> int:
    """Tương đương `millis()` của Arduino — đồng hồ đơn điệu tính từ lúc process khởi động.

    Dùng cho MỌI quyết định thời điểm (interval, cooldown, backoff, deadline). KHÔNG dùng
    `time.time()` vì NTP/người dùng chỉnh giờ hệ thống sẽ làm nhảy mốc và treo cứng các cổng
    chờ — đúng lớp lỗi mà firmware né bằng `millis()`.
    """
    return int(time.monotonic() * 1000.0)


def epoch_now() -> int:
    """Tương đương `net::timeEpoch()` — giây epoch UTC, dùng làm khoá FIFO của hàng đợi."""
    return int(time.time())


def iso_now(skew_min: int = 0) -> str:
    """Mốc thời gian UTC độ phân giải GIÂY, hậu tố `Z` — khớp `net::isoNow()`.

    `skew_min` là phần mở rộng RIÊNG của simulator (scenario `clock_skew`): đẩy lệch đồng hồ
    thiết bị để kích hoạt kiểm tra clock-skew phía backend (#IoT2-15). Firmware không có.
    """
    t = datetime.now(timezone.utc) + timedelta(minutes=skew_min)
    return t.strftime(ISO_FORMAT)


def iso_now_minus(seconds_ago: int, skew_min: int = 0) -> str:
    """Mốc thời gian lùi về quá khứ — khớp `net::isoNowMinus()` (GH-736).

    Cảm biến an toàn lấy mẫu cả khi mất mạng, nên thời điểm PHÁT HIỆN và thời điểm GỬI ĐƯỢC
    có thể cách nhau rất xa. Backend phải nhận đúng thời điểm phát hiện.
    """
    if seconds_ago <= 0:
        return iso_now(skew_min)
    clamped = min(int(seconds_ago), MAX_BACKDATE_SECONDS)
    t = datetime.now(timezone.utc) + timedelta(minutes=skew_min) - timedelta(seconds=clamped)
    return t.strftime(ISO_FORMAT)


def elapsed_ms(start_ms: int, now_ms: int) -> int:
    """`core::elapsedMs` — số ms đã trôi qua (không âm)."""
    return max(0, now_ms - start_ms)


def elapsed_seconds(start_ms: int, now_ms: int) -> int:
    """`core::elapsedSeconds` — làm tròn xuống."""
    return elapsed_ms(start_ms, now_ms) // 1000
