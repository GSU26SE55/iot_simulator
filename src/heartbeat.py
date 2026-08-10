"""Heartbeat — mirror `firmware-esp32/src/telemetry/heartbeat.cpp` (S2-FW-03).

`POST /api/iot-devices/heartbeat` — HTTP, KHÔNG qua MQTT (broker chỉ nhận telemetry/status/cmd-ack).

Ánh xạ trường → `IotDeviceHeartbeatCommand` (MO §52.2 + §52.4):

    ESP32                      JSON                  Backend
    ─────────────────────────────────────────────────────────────────
    temperatureRead()          Temperature           decimal?
    (freeHeap+PSRAM)/MB        MemoryUsageMb         long?
    freeHeap % heapSize        FreeMemoryPercent     decimal?
    WiFi.RSSI()                SignalStrengthDbm     int?   (alias RssiDbm)
    queue depth                LocalQueueDepth       int?   (alias QueuedReadingCount)
    FW_VERSION                 FirmwareVersion       string?
    millis()/1000              UptimeSeconds         long?
    isoNow()                   DeviceTimestamp       DateTime (required)
    null                       Cpu                   decimal? — ESP32 không tính
    null                       DiskFreeMb            long?    — ESP32 không có disk

⚠ MỘT chỗ simulator CỐ Ý ĐÚNG HƠN firmware: `LocalQueueDepth`.
Firmware vẫn hard-code `0` kèm chú thích "Sprint 3 sẽ có queue thật" — trong khi hàng đợi đã tồn
tại từ Sprint 3, nên backend KHÔNG BAO GIỜ thấy được độ sâu hàng đợi thật của thiết bị. Simulator
gửi số thật vì đó mới là thứ hợp đồng backend mô tả; gửi 0 chỉ để "giống bug" là làm hỏng đúng
tính năng mà trường này sinh ra. Điểm lệch này được ghi rõ ở đây và trong README.
"""
from __future__ import annotations

import logging
import random
from typing import Callable

from .timeutil import monotonic_ms

log = logging.getLogger("iot-sim.heartbeat")

ENDPOINT = "/api/iot-devices/heartbeat"

# heartbeatBegin/SetInterval — cùng biên với backend.
INTERVAL_MIN_MS = 10000
INTERVAL_MAX_MS = 3600000
INTERVAL_DEFAULT_MS = 60000

# Heartbeat đầu tiên gửi 5s sau boot (chờ NTP + provision xong) — heartbeatTick::s_firstTick.
FIRST_TICK_DELAY_MS = 5000

# ESP32-S3-DevKitC-1-N16R8: SRAM nội 320 KB + PSRAM 8 MB.
_HEAP_SIZE_BYTES = 320 * 1024
_PSRAM_SIZE_BYTES = 8 * 1024 * 1024


class Heartbeat:
    def __init__(self, http, firmware_version_getter: Callable[[], str],
                 queue_depth_getter: Callable[[], int],
                 rssi_getter: Callable[[], int],
                 iso_now: Callable[[], str],
                 boot_ms: int,
                 interval_ms: int = INTERVAL_DEFAULT_MS):
        self._http = http
        self._fw = firmware_version_getter
        self._queue_depth = queue_depth_getter
        self._rssi = rssi_getter
        self._iso_now = iso_now
        self._boot_ms = boot_ms

        self._interval_ms = INTERVAL_DEFAULT_MS
        self.begin(interval_ms)

        self._last_sent_ms = 0
        self._first_tick = True
        self.ok_count = 0
        self.fail_count = 0
        self.last_error = ""
        self.last_clock_skew_warning = ""

    # ── cấu hình ──────────────────────────────────────────────────────────────────────────
    def begin(self, interval_ms: int) -> None:
        """`heartbeatBegin` — state hỏng (0, cực lớn) thì lui về mặc định."""
        if interval_ms < INTERVAL_MIN_MS:
            interval_ms = INTERVAL_DEFAULT_MS
        if interval_ms > INTERVAL_MAX_MS:
            interval_ms = INTERVAL_MAX_MS
        self._interval_ms = int(interval_ms)

    def set_interval(self, interval_ms: int) -> None:
        """`heartbeatSetInterval` — kẹp về [10s, 1h] (khác `begin`: <10s kẹp lên 10s)."""
        interval_ms = max(INTERVAL_MIN_MS, min(INTERVAL_MAX_MS, int(interval_ms)))
        if interval_ms != self._interval_ms:
            log.info("interval %dms → %dms", self._interval_ms, interval_ms)
            self._interval_ms = interval_ms

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    @property
    def interval_s(self) -> int:
        return self._interval_ms // 1000

    # ── thân request ──────────────────────────────────────────────────────────────────────
    def build_body(self) -> dict:
        """Đúng tập trường + đúng kiểu của `IotDeviceHeartbeatCommand`.

        `MemoryUsageMb` là `long?` ⇒ PHẢI là số nguyên. System.Text.Json ở strict-mode từ chối
        float cho kiểu nguyên, và lỗi đó hiện ra thành 400 rất khó truy.
        """
        uptime_s = max(0, (monotonic_ms() - self._boot_ms) // 1000)

        free_heap = random.randint(int(_HEAP_SIZE_BYTES * 0.45), int(_HEAP_SIZE_BYTES * 0.75))
        free_psram = random.randint(int(_PSRAM_SIZE_BYTES * 0.90), int(_PSRAM_SIZE_BYTES * 0.98))
        # freeHeapMb() của firmware: làm tròn tới MB gần nhất, không under-report.
        memory_usage_mb = (free_heap + free_psram + 512 * 1024) // (1024 * 1024)
        free_memory_percent = round(free_heap * 100.0 / _HEAP_SIZE_BYTES, 2)

        return {
            "DeviceTimestamp": self._iso_now(),
            "FirmwareVersion": self._fw(),
            "Temperature": round(35.0 + random.uniform(-2.0, 5.0), 2),  # decimal? chip temp °C
            "MemoryUsageMb": int(memory_usage_mb),                      # long?  — SỐ NGUYÊN
            "FreeMemoryPercent": free_memory_percent,                   # decimal?
            "SignalStrengthDbm": int(self._rssi()),                     # int?   — alias RssiDbm
            "LocalQueueDepth": int(self._queue_depth()),                # int?   — alias QueuedReadingCount
            "UptimeSeconds": int(uptime_s),                             # long?
            "Cpu": None,                                                # decimal? — ESP32 không tính
            "DiskFreeMb": None,                                         # long?    — không có disk
        }

    def send_now(self) -> bool:
        """`heartbeatSendNow` — dùng cho lệnh downlink `request_heartbeat`; đặt lại mốc chu kỳ."""
        self._last_sent_ms = monotonic_ms()
        self._first_tick = False
        return self._send_once()

    def tick(self) -> None:
        """`heartbeatTick` — heartbeat đầu tiên ở mốc uptime 5s, sau đó theo chu kỳ."""
        now = monotonic_ms()
        uptime_ms = now - self._boot_ms
        if self._first_tick:
            if uptime_ms < FIRST_TICK_DELAY_MS:
                return
            self._first_tick = False
            self._last_sent_ms = now - self._interval_ms   # ép gửi ngay tick này
        if now - self._last_sent_ms < self._interval_ms:
            return
        self._last_sent_ms = now
        self._send_once()

    def _send_once(self) -> bool:
        body = self.build_body()
        res = self._http.heartbeat(body)
        if res.ok:
            self.ok_count += 1
            self.last_error = ""
            self._note_clock_skew(res.json)
            log.info("heartbeat ok#%d (%d, %dms)", self.ok_count, res.status_code,
                     res.duration_ms)
            return True
        self.fail_count += 1
        self.last_error = f"heartbeat {res.status_code}: {res.body[:80]}"
        log.warning("heartbeat FAIL#%d code=%d body=%s", self.fail_count, res.status_code,
                    res.body[:120])
        return False

    def _note_clock_skew(self, body) -> None:
        """Backend trả `data.clockSkewWarning/clockSkewSeconds` (#IoT2-15) — phải LA LÊN.

        Lệch giờ làm hỏng cả biểu đồ lẫn tương quan cảnh báo, mà triệu chứng duy nhất là dữ liệu
        "trông sai giờ" — gần như không truy được nếu thiết bị không nói.
        """
        if not isinstance(body, dict):
            return
        data = body.get("data")
        if not isinstance(data, dict):
            return
        if not data.get("clockSkewWarning"):
            self.last_clock_skew_warning = ""
            return
        skew = data.get("clockSkewSeconds")
        self.last_clock_skew_warning = f"clock skew {skew}s"
        log.warning("⚠ backend báo LỆCH GIỜ %ss — kiểm tra NTP của thiết bị", skew)
