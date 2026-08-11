"""Phát hiện cháy — ⚠ PHẦN MỞ RỘNG RIÊNG CỦA SIMULATOR, firmware KHÔNG có.

Firmware hôm nay chỉ có hai đường báo sự cố môi trường: MQ-2 → `GasLeak (3)` và rò nước →
`Flood (4)`. Backend thì có sẵn enum `FireDetected (2)`, và luồng cảnh báo/ticket của nó rất đáng
demo, nên simulator giữ lại kịch bản này — nhưng phải nói rõ ở đây và ở README: **thiết bị thật
hiện KHÔNG phát ra loại sự cố này.** Không được dùng nó làm bằng chứng cho tính năng firmware.

Điều kiện kích hoạt (giữ giống một bộ dò tổ hợp thật): MQ-2 vượt ngưỡng ĐỒNG THỜI nhiệt độ pin
vượt `FIRE_TEMP_THRESHOLD_C`. Cùng cơ chế cạnh lên + hạ nhiệt + backoff như hai cảm biến kia.
"""
from __future__ import annotations

import logging

from ..backoff import Backoff
from .environmental import (IncidentReportResult, IncidentSeverity, IncidentType,
                            PendingReport)
from .incident_trigger import IncidentTrigger

log = logging.getLogger("iot-sim.fire")

FIRE_TEMP_THRESHOLD_C = 70.0
FIRE_POLL_INTERVAL_MS = 1000
FIRE_REARM_COOLDOWN_MS = 300000   # 5 phút, giống hai cảm biến còn lại


class FireWatch:
    def __init__(self, reporter, iso_now_minus, enabled: bool = True,
                 temp_threshold_c: float = FIRE_TEMP_THRESHOLD_C,
                 poll_interval_ms: int = FIRE_POLL_INTERVAL_MS,
                 rearm_cooldown_ms: int = FIRE_REARM_COOLDOWN_MS):
        self._reporter = reporter
        self._iso_now_minus = iso_now_minus
        self.enabled = bool(enabled)
        self.temp_threshold_c = float(temp_threshold_c)
        self.poll_interval_ms = int(poll_interval_ms)

        self._trigger = IncidentTrigger(rearm_cooldown_ms)
        self._pending = PendingReport(Backoff())
        self._last_poll_ms = 0
        self._pending_temp = 0.0
        self.report_count = 0

    def tick(self, now_ms: int, scenario: str, mq2_raw: int, mq2_threshold: int,
             battery_temp_c: float) -> None:
        if not self.enabled:
            return
        if now_ms - self._last_poll_ms < self.poll_interval_ms:
            return
        self._last_poll_ms = now_ms

        # Chỉ kịch bản `fire_detected` mới dựng được tổ hợp này — tránh báo cháy oan khi chạy
        # scenario `overheat` (nhiệt cao nhưng không có khí) hay `gas_leak` (khí nhưng pin mát).
        active = (scenario == "fire_detected"
                  and mq2_raw > mq2_threshold
                  and battery_temp_c >= self.temp_threshold_c)

        if self._trigger.update(active, now_ms):
            self._pending.arm(now_ms)
            self._pending_temp = battery_temp_c
            log.warning("PHÁT HIỆN CHÁY (mô phỏng): MQ-2 raw=%d + nhiệt pin %.1f°C",
                        mq2_raw, battery_temp_c)

        if self._pending.pending and now_ms >= self._pending.next_report_ms:
            self._send(now_ms)

    def _send(self, now_ms: int) -> None:
        seconds_ago = max(0, (now_ms - self._pending.detected_ms) // 1000)
        notes = (f"MQ-2 vượt ngưỡng đồng thời nhiệt pin {self._pending_temp:.1f}°C "
                 f"(kịch bản mô phỏng)")
        result = self._reporter.report(IncidentType.FIRE_DETECTED, IncidentSeverity.CRITICAL,
                                       notes, self._iso_now_minus(seconds_ago))
        if result is IncidentReportResult.SUCCESS:
            self._pending.on_success(now_ms)
            self.report_count += 1
        elif result is IncidentReportResult.PERMANENT:
            log.error("BỎ báo cháy (lỗi vĩnh viễn) — xem log env-incident")
            self._pending.on_permanent(now_ms)
        else:
            wait_ms = self._pending.on_transient(now_ms)
            log.warning("báo cháy lỗi tạm thời → thử lại sau %dms", wait_ms)
