"""MQ-2 (khói/gas) — mirror `firmware-esp32/src/sensor/mq2.cpp` (S6-FW-01 + GH-736/741).

Đường đi: khởi động → warm-up 30s → đọc ADC 12-bit mỗi 1s → so ngưỡng → cạnh lên + hạ nhiệt 5'
→ reporter HTTPS.

⚠⚠ LOẠI SỰ CỐ LÀ **`GasLeak (3)`, KHÔNG PHẢI `Smoke (1)`**.
MQ-2 về bản chất là cảm biến GAS (LPG/propane/methane/khói khí cháy); `Smoke` được backend dành
cho cảm biến khói quang học sau này. Quyết định NS-24 (#664, E4, Q10=B), xem `mq2.cpp`.
Bản simulator cũ gửi `Smoke (1)` cho scenario `smoke` — lệch enum so với thiết bị thật, và
lệch enum nghĩa là quy tắc cảnh báo/ticket phía backend chạy nhánh khác hẳn.

Bốn thứ bản cũ không có, nay có đủ:
  * warm-up 30s (mẫu trong lúc sấy KHÔNG được đánh giá — MQ-2 chưa ổn định thì đọc ra rác),
  * đọc theo chu kỳ 1s thay vì bắn một phát rồi thôi,
  * cạnh lên + hạ nhiệt 5 phút → sự cố LẶP LẠI được, đúng như hiện trường,
  * chốt `pending` + backoff + phân loại tạm thời/vĩnh viễn khi báo hỏng.
"""
from __future__ import annotations

import logging
import random

from ..backoff import Backoff
from .environmental import (IncidentReportResult, IncidentSeverity, IncidentType,
                            PendingReport)
from .incident_trigger import IncidentTrigger

log = logging.getLogger("iot-sim.mq2")

# include/config.h — MQ2_*
MQ2_ADC_PIN = 1                    # GPIO1 = ADC1_CH0
MQ2_THRESHOLD_RAW = 2000           # 0..4095 (12-bit); raw > ngưỡng = phát hiện
MQ2_WARMUP_MS = 30000              # 30s sấy sau khi cấp nguồn
MQ2_POLL_INTERVAL_MS = 1000        # đọc ADC mỗi 1s
MQ2_REARM_COOLDOWN_MS = 300000     # 5 phút tối thiểu giữa 2 lần báo

# Mức ADC mô phỏng theo scenario. Nền ~600 là giá trị điển hình của MQ-2 trong không khí sạch.
_BASELINE_RAW = 600
_SCENARIO_RAW = {
    "smoke": 3100,
    "gas_leak": 2900,
    "fire_detected": 3400,   # cháy thì cả khói lẫn khí đều vọt lên
}


class Mq2Sensor:
    def __init__(self, reporter, iso_now_minus, enabled: bool = True,
                 threshold_raw: int = MQ2_THRESHOLD_RAW,
                 warmup_ms: int = MQ2_WARMUP_MS,
                 poll_interval_ms: int = MQ2_POLL_INTERVAL_MS,
                 rearm_cooldown_ms: int = MQ2_REARM_COOLDOWN_MS,
                 adc_pin: int = MQ2_ADC_PIN):
        self._reporter = reporter
        self._iso_now_minus = iso_now_minus
        self.enabled = bool(enabled)
        self.threshold_raw = int(threshold_raw)
        self.warmup_ms = int(warmup_ms)
        self.poll_interval_ms = int(poll_interval_ms)
        self.adc_pin = int(adc_pin)

        self._trigger = IncidentTrigger(rearm_cooldown_ms)
        self._pending = PendingReport(Backoff())
        self._warmup_start_ms = 0
        self._last_poll_ms = 0
        self._pending_raw = 0
        self.last_raw = 0
        self.report_count = 0

    def begin(self, now_ms: int) -> None:
        """`mq2Begin` — mốc warm-up bắt đầu từ đây."""
        self._warmup_start_ms = now_ms
        self._last_poll_ms = 0

    def _in_warmup(self, now_ms: int) -> bool:
        return (now_ms - self._warmup_start_ms) < self.warmup_ms

    def read_raw(self, scenario: str) -> int:
        """Giá trị ADC mô phỏng (thiết bị thật: `analogRead(GPIO1)`)."""
        base = _SCENARIO_RAW.get(scenario, _BASELINE_RAW)
        return max(0, min(4095, int(base + random.uniform(-60, 60))))

    def tick(self, now_ms: int, scenario: str) -> None:
        if not self.enabled:
            return
        if now_ms - self._last_poll_ms < self.poll_interval_ms:
            return
        self._last_poll_ms = now_ms

        raw = self.read_raw(scenario)
        self.last_raw = raw

        # Trong lúc sấy: CHỈ đọc/log, KHÔNG đánh giá sự cố.
        if self._in_warmup(now_ms):
            return

        if self._trigger.update(raw > self.threshold_raw, now_ms):
            self._pending.arm(now_ms)
            self._pending_raw = raw
            log.warning("PHÁT HIỆN GAS raw=%d > thr=%d → báo backend", raw, self.threshold_raw)

        if self._pending.pending and now_ms >= self._pending.next_report_ms:
            self._send(now_ms)

    def _send(self, now_ms: int) -> None:
        seconds_ago = max(0, (now_ms - self._pending.detected_ms) // 1000)
        notes = f"MQ-2 raw={self._pending_raw} > thr={self.threshold_raw} (GPIO{self.adc_pin})"
        result = self._reporter.report(IncidentType.GAS_LEAK, IncidentSeverity.CRITICAL,
                                       notes, self._iso_now_minus(seconds_ago))
        if result is IncidentReportResult.SUCCESS:
            self._pending.on_success(now_ms)
            self.report_count += 1
        elif result is IncidentReportResult.PERMANENT:
            # Gửi lại vẫn hỏng (sai scope, chưa provision, payload sai) ⇒ DỪNG. Sự cố vẫn được
            # ghi nhận cục bộ qua log + bộ đếm `dropped` của reporter.
            log.error("BỎ báo MQ-2 (lỗi vĩnh viễn) — xem log env-incident")
            self._pending.on_permanent(now_ms)
        else:
            wait_ms = self._pending.on_transient(now_ms)
            log.warning("báo MQ-2 lỗi tạm thời → thử lại sau %dms", wait_ms)
