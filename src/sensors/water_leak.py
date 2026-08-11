"""Cảm biến rò nước — mirror `firmware-esp32/src/sensor/water_leak.cpp` (S6-FW-02 + GH-736/741).

Đọc digital GPIO2 mỗi 0,5s → cạnh khô→ướt → hạ nhiệt 5 phút → reporter HTTPS với
`incidentType = Flood (4)`.

Trên phần cứng thật chân dùng `INPUT_PULLUP` và mặc định `ACTIVE_HIGH=0` (ướt→LOW) để cảm biến
rút ra không bị đọc nhầm là "ướt". Simulator không có chân thật nên chỉ giữ lại phần HÀNH VI:
lấy mẫu định kỳ, chỉ báo ở cạnh lên, và có thể báo LẠI sau khi hết hạ nhiệt.
"""
from __future__ import annotations

import logging

from ..backoff import Backoff
from .environmental import (IncidentReportResult, IncidentSeverity, IncidentType,
                            PendingReport)
from .incident_trigger import IncidentTrigger

log = logging.getLogger("iot-sim.water")

# include/config.h — WATER_LEAK_*
WATER_LEAK_GPIO = 2
WATER_LEAK_POLL_INTERVAL_MS = 500
WATER_LEAK_REARM_COOLDOWN_MS = 300000   # 5 phút


class WaterLeakSensor:
    def __init__(self, reporter, iso_now_minus, enabled: bool = True,
                 poll_interval_ms: int = WATER_LEAK_POLL_INTERVAL_MS,
                 rearm_cooldown_ms: int = WATER_LEAK_REARM_COOLDOWN_MS,
                 gpio: int = WATER_LEAK_GPIO):
        self._reporter = reporter
        self._iso_now_minus = iso_now_minus
        self.enabled = bool(enabled)
        self.poll_interval_ms = int(poll_interval_ms)
        self.gpio = int(gpio)

        self._trigger = IncidentTrigger(rearm_cooldown_ms)
        self._pending = PendingReport(Backoff())
        self._last_poll_ms = 0
        self._has_sample = False
        self.is_wet = False
        self.report_count = 0

    def begin(self, now_ms: int) -> None:
        self._last_poll_ms = 0
        self._has_sample = False

    def read_wet(self, scenario: str) -> bool:
        """Trạng thái chân mô phỏng (thiết bị thật: `digitalRead(GPIO2)`)."""
        return scenario == "water_leak"

    def tick(self, now_ms: int, scenario: str) -> None:
        if not self.enabled:
            return
        if now_ms - self._last_poll_ms < self.poll_interval_ms:
            return
        self._last_poll_ms = now_ms

        wet = self.read_wet(scenario)
        if not self._has_sample or wet != self.is_wet:
            log.info("GPIO%d state=%s", self.gpio, "WET" if wet else "DRY")
        self.is_wet = wet
        self._has_sample = True

        if self._trigger.update(wet, now_ms):
            self._pending.arm(now_ms)
            log.warning("PHÁT HIỆN RÒ NƯỚC GPIO%d → báo backend", self.gpio)

        if self._pending.pending and now_ms >= self._pending.next_report_ms:
            self._send(now_ms)

    def _send(self, now_ms: int) -> None:
        seconds_ago = max(0, (now_ms - self._pending.detected_ms) // 1000)
        notes = f"water leak GPIO{self.gpio}"
        result = self._reporter.report(IncidentType.FLOOD, IncidentSeverity.CRITICAL,
                                       notes, self._iso_now_minus(seconds_ago))
        if result is IncidentReportResult.SUCCESS:
            self._pending.on_success(now_ms)
            self.report_count += 1
        elif result is IncidentReportResult.PERMANENT:
            log.error("BỎ báo rò nước (lỗi vĩnh viễn) — xem log env-incident")
            self._pending.on_permanent(now_ms)
        else:
            wait_ms = self._pending.on_transient(now_ms)
            log.warning("báo rò nước lỗi tạm thời → thử lại sau %dms", wait_ms)
