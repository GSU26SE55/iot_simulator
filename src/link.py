"""Trạng thái đường lên backend — tương ứng `firmware-esp32/src/net/wifi_manager.cpp` (IOT3-51).

Simulator không có Wi-Fi riêng (nó dùng mạng của máy chủ), nên thứ tương đương "có mạng hay không"
là **backend có với tới được hay không**. Đó cũng chính là thứ mà mọi quyết định trong `main.cpp`
thật sự cần: xếp hàng đợi hay gửi thẳng, có gửi heartbeat không, đèn màu gì.

Bốn pha giữ đúng ngữ nghĩa firmware để LED nói được cùng một ngôn ngữ:

    UNCONFIGURED — chưa có deviceCode/apiKey hợp lệ ⇒ chờ người cấu hình (đèn tím NHÁY)
    CONNECTING   — đang mất kết nối nhưng chưa lâu (< 30s)                 (đèn cam)
    RECOVERY     — mất kết nối ≥ 30s, vẫn kiên trì thử lại                 (tím/cam xen kẽ)
    CONNECTED    — vừa nói chuyện được với backend                          (xanh)

Cập nhật trạng thái từ KẾT QUẢ THẬT của mỗi request: `status_code > 0` nghĩa là đã bắt tay được
với backend (kể cả 4xx/5xx — đó là lỗi ứng dụng, không phải lỗi đường truyền); `status_code == 0`
là lỗi truyền tải (DNS/TCP/TLS/timeout) — đúng nghĩa "mất mạng".
"""
from __future__ import annotations

from enum import Enum

from .timeutil import monotonic_ms

# `CONFIG_PORTAL_AP_FALLBACK_MS` của firmware — mất mạng quá lâu thì đổi cách báo hiệu.
RECOVERY_AFTER_MS = 30000


class LinkPhase(Enum):
    UNCONFIGURED = 0
    CONNECTING = 1
    RECOVERY = 2
    CONNECTED = 3


class LinkState:
    """`net::wifiPhase()` + `net::wifiIsConnected()` phiên bản simulator."""

    def __init__(self, identity_ready: bool = True,
                 recovery_after_ms: int = RECOVERY_AFTER_MS):
        self._identity_ready = bool(identity_ready)
        self._recovery_after_ms = int(recovery_after_ms)
        # Khởi đầu coi như CÓ kết nối, để lần gửi đầu tiên là một request THẬT.
        #
        # Đây là ảnh phản chiếu đúng của firmware: `wifiIsConnected()` đã true trước khi có lần
        # POST đầu tiên (STA associate xong trong lúc setup), nên chu kỳ ingest đầu đi thẳng
        # đường online. Nếu khởi đầu bằng "offline" thì mọi lần chạy đều xếp hàng một batch vô
        # cớ rồi mới đẩy bù — dữ liệu không mất nhưng log sai lệch và dashboard nháy đỏ oan.
        self._connected = True
        self._ever_connected = False
        self._offline_since_ms = monotonic_ms()
        self.transport_error_count = 0

    def set_identity_ready(self, ready: bool) -> None:
        self._identity_ready = bool(ready)

    def note_result(self, status_code: int, now_ms: int | None = None) -> None:
        """Gọi sau MỖI request HTTP. `status_code == 0` = lỗi truyền tải."""
        now_ms = monotonic_ms() if now_ms is None else now_ms
        if status_code > 0:
            if not self._connected:
                self._connected = True
                self._ever_connected = True
            return
        self.transport_error_count += 1
        if self._connected or not self._ever_connected:
            self._offline_since_ms = now_ms
        self._connected = False

    def is_up(self) -> bool:
        return self._connected

    def phase(self, now_ms: int | None = None) -> LinkPhase:
        if not self._identity_ready:
            return LinkPhase.UNCONFIGURED
        if self._connected:
            return LinkPhase.CONNECTED
        now_ms = monotonic_ms() if now_ms is None else now_ms
        if (now_ms - self._offline_since_ms) >= self._recovery_after_ms:
            return LinkPhase.RECOVERY
        return LinkPhase.CONNECTING

    def offline_duration_ms(self, now_ms: int | None = None) -> int:
        if self._connected:
            return 0
        now_ms = monotonic_ms() if now_ms is None else now_ms
        return max(0, now_ms - self._offline_since_ms)

    def rssi_dbm(self) -> int:
        """Giá trị RSSI mô phỏng cho heartbeat.

        Không có Wi-Fi thật nên không có RSSI thật. Trả 0 khi mất kết nối — đúng như
        `heartbeat.cpp::wifiRssi()` (trả 0 nếu `WiFi.status() != WL_CONNECTED`), và trả một mức
        sóng hợp lý khi đang online để dashboard/backend có dữ liệu để vẽ.
        """
        if not self._connected:
            return 0
        import random
        return random.randint(-75, -45)
