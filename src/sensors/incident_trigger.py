"""Cạnh lên + hạ nhiệt — mirror `firmware-esp32/src/sensor/incident_trigger.h` (S6-FW-01/02).

Dùng chung cho MQ-2 và cảm biến rò nước: phát hiện CHUYỂN CẠNH bình thường→bất thường và chỉ
"fire" một lần mỗi chu kỳ hạ nhiệt, để không nện backend khi điều kiện còn kéo dài.

Ngữ nghĩa (giữ y hệt firmware):
  - `update(active, now_ms)` trả True ĐÚNG MỘT lần tại cạnh lên (inactive→active), với điều kiện
    đã qua cooldown kể từ lần fire trước (hoặc chưa fire bao giờ).
  - Giữ active liên tục: chỉ fire ở cạnh lên đầu tiên, KHÔNG lặp.
  - Xuống rồi lên lại trong cooldown → chặn (chống chattering).
  - Lần đọc đầu đã active (vd nước có sẵn lúc bật máy) → coi là cạnh lên → fire.

⚠ Bản simulator cũ KHÔNG có lớp này: mỗi scenario chỉ bắn ĐÚNG MỘT sự cố rồi thôi (cờ `*_sent`),
nên không mô phỏng được sự cố lặp lại — thứ mà backend phải khử trùng và dựng ticket.
"""
from __future__ import annotations


class IncidentTrigger:
    def __init__(self, cooldown_ms: int):
        self._cooldown_ms = int(cooldown_ms)
        self._prev_active = False
        self._has_fired = False
        self._last_fire_ms = 0

    def update(self, active: bool, now_ms: int) -> bool:
        fire = False
        if active and not self._prev_active:
            if not self._has_fired or (now_ms - self._last_fire_ms) >= self._cooldown_ms:
                fire = True
                self._has_fired = True
                self._last_fire_ms = now_ms
        self._prev_active = active
        return fire

    def is_active(self) -> bool:
        return self._prev_active

    def has_fired(self) -> bool:
        return self._has_fired

    def last_fire_ms(self) -> int:
        return self._last_fire_ms
