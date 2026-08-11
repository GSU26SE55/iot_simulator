"""Hàng đợi cục bộ khi backend không với tới — mirror `firmware-esp32/src/queue/local_queue.cpp`
+ `queue/queue_index.h` (S3-FW-01).

Firmware lưu mỗi batch thành 2 file trên LittleFS (`/queue/<epoch>.json` + `.idem`), tên file là
epoch giây zero-pad nên glob-sorted = FIFO. Simulator giữ định dạng JSONL cho dễ đọc/soi bằng mắt
nhưng có ĐÚNG các bất biến của firmware:

  * TRẦN 200 batch (`kMaxQueuedBatches`) — đầy thì **drop OLDEST** rồi mới push.
    Không có trần thì chạy offline vài giờ là file phình vô hạn — thiết bị thật không có RAM/flash
    để làm thế, nên simulator không được phép "khoẻ hơn" thiết bị.
  * Mỗi mục mang theo `Idempotency-Key` sinh NGAY LÚC LẤY MẪU. Đẩy bù nhiều lần cũng không sinh
    bản ghi trùng (backend dedup theo `(DeviceCode, Idempotency-Key)` — #IoT2-16).
  * `peek_oldest()` / `delete_oldest()` để vòng lặp chính đẩy MỘT batch mỗi vòng, có backoff —
    giống `tryFlushQueue()`; không phải "xả cả hàng đợi một lượt".
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("iot-sim.queue")

# queue/queue_index.h — spec S3-FW-01 "max 200 batch".
MAX_QUEUED_BATCHES = 200


class LocalQueue:
    """FIFO bền vững, mỗi dòng = 1 batch: {endpoint, key, payload, epoch}."""

    def __init__(self, path: Path, max_batches: int = MAX_QUEUED_BATCHES):
        self.path = Path(path)
        self.max_batches = int(max_batches)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self.dropped_count = 0

    # ── đọc/ghi thô ────────────────────────────────────────────────────────────────────────
    def _read_lines(self) -> list[str]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return []

    def _write_lines(self, lines: list[str]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    # ── API ────────────────────────────────────────────────────────────────────────────────
    def append(self, endpoint: str, payload: dict, idempotency_key: str,
               epoch_sec: int = 0) -> bool:
        """`queue::queueEnqueue`. Đầy → xoá mục CŨ NHẤT rồi mới push (drop-oldest)."""
        lines = self._read_lines()
        dropped = 0
        while len(lines) >= self.max_batches:
            lines.pop(0)
            dropped += 1
        if dropped:
            self.dropped_count += dropped
            log.warning("hàng đợi đầy (%d) — ĐÃ BỎ %d batch cũ nhất để nhận batch mới. "
                        "Dữ liệu bị bỏ KHÔNG lấy lại được.", self.max_batches, dropped)

        lines.append(json.dumps({
            "endpoint": endpoint,
            "key": idempotency_key,
            "payload": payload,
            "epoch": int(epoch_sec),
        }, ensure_ascii=False))
        self._write_lines(lines)
        return True

    def peek_oldest(self) -> dict | None:
        """`queue::queuePeekOldest` — đọc mục cũ nhất, KHÔNG xoá."""
        lines = self._read_lines()
        for ln in lines:
            try:
                item = json.loads(ln)
            except ValueError:
                continue
            if isinstance(item, dict):
                return item
        return None

    def delete_oldest(self) -> None:
        """`queue::queueDelete` — xoá mục cũ nhất sau khi đẩy thành công (hoặc bỏ vì 4xx)."""
        lines = self._read_lines()
        if lines:
            self._write_lines(lines[1:])

    def read_all(self) -> list[dict]:
        out: list[dict] = []
        for ln in self._read_lines():
            try:
                item = json.loads(ln)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def remove_first(self, count: int) -> None:
        if count <= 0:
            return
        self._write_lines(self._read_lines()[count:])

    def size(self) -> int:
        return len(self._read_lines())

    def clear(self) -> None:
        """`queue::queueClear` — debug/reset."""
        self._write_lines([])
