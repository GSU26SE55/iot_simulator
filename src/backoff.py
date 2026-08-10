"""Exponential backoff + phân loại lỗi tạm thời/vĩnh viễn.

Mirror `firmware-esp32/src/net/backoff.h` + `net/backoff.cpp` (S3-FW-03).

Thuật toán (KHỚP firmware):
    attempt 0 → chưa chờ
    attempt N → chờ min(2^(N-1) · 2000ms, 300_000ms) ± 20% jitter, sàn 100ms
"""
from __future__ import annotations

import random

from .timeutil import monotonic_ms

# net::kBackoff* — giữ nguyên tên/giá trị của firmware.
BACKOFF_BASE_MS = 2000
BACKOFF_MAX_MS = 300000
BACKOFF_JITTER_PCT = 0.20
BACKOFF_MIN_MS = 100


def is_transient_failure(http_code: int) -> bool:
    """`net::isTransientFailure` — quyết định giữ hay BỎ một batch.

    - Tạm thời (giữ + backoff): lỗi mạng (code 0), 5xx, 408 timeout, 429 rate-limit.
    - Vĩnh viễn (BỎ, không xếp hàng): mọi 4xx còn lại — 400 sai dữ liệu, 401/403 sai key/scope,
      404 sai route, 409 xung đột trạng thái.

    Vì sao phải phân loại: một batch 4xx nằm đầu hàng đợi mà cứ retry thì nó chặn VĨNH VIỄN
    toàn bộ batch phía sau, rồi hàng đợi đầy → drop dữ liệu tốt. Đúng lớp lỗi GH-725.
    """
    if http_code <= 0:
        return True
    if http_code >= 500:
        return True
    if http_code in (408, 429):
        return True
    return False


class Backoff:
    """Bộ đếm backoff cho MỘT đường gửi (ingest, hoặc 1 cảm biến sự cố)."""

    def __init__(self, base_ms: int = BACKOFF_BASE_MS, max_ms: int = BACKOFF_MAX_MS,
                 jitter_pct: float = BACKOFF_JITTER_PCT):
        self._base_ms = int(base_ms)
        self._max_ms = int(max_ms)
        self._jitter = float(jitter_pct)
        self._attempt = 0
        self._next_retry_ms = 0

    def record_failure(self, now_ms: int | None = None) -> int:
        """Ghi nhận 1 lần thất bại. Trả số ms nên chờ trước lần thử kế tiếp."""
        now_ms = monotonic_ms() if now_ms is None else now_ms
        self._attempt += 1

        base_delay = self._base_ms
        for _ in range(1, self._attempt):
            if base_delay > self._max_ms // 2:
                base_delay = self._max_ms
                break
            base_delay *= 2
        base_delay = min(base_delay, self._max_ms)

        delta = int(base_delay * random.uniform(-self._jitter, self._jitter))
        final_delay = base_delay + delta
        final_delay = min(final_delay, self._max_ms)
        final_delay = max(final_delay, BACKOFF_MIN_MS)

        self._next_retry_ms = now_ms + final_delay
        return final_delay

    def reset(self) -> None:
        self._attempt = 0
        self._next_retry_ms = 0

    def next_retry_at(self) -> int:
        return self._next_retry_ms

    def allowed(self, now_ms: int | None = None) -> bool:
        """True nếu đã hết thời gian chờ (mirror `millis() >= nextRetryAt()`)."""
        now_ms = monotonic_ms() if now_ms is None else now_ms
        return now_ms >= self._next_retry_ms

    def remaining_ms(self, now_ms: int | None = None) -> int:
        now_ms = monotonic_ms() if now_ms is None else now_ms
        return max(0, self._next_retry_ms - now_ms)

    def attempt_count(self) -> int:
        return self._attempt
