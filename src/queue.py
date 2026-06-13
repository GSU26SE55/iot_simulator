"""Local queue JSONL — buffer batch khi backend down (NI §9.1 + S3-FW-01).

Mỗi line = 1 ingest batch: { "endpoint": "...", "key": "<idem>", "payload": {...} }.
Khi mạng quay lại, simulator flush từ đầu file. Backend dedup theo `Idempotency-Key` →
KHÔNG bị trùng (Sprint IoT-2 #IoT2-16).
"""
from __future__ import annotations

import json
from pathlib import Path


class LocalQueue:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, endpoint: str, payload: dict, idempotency_key: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "endpoint": endpoint,
                "key": idempotency_key,
                "payload": payload,
            }, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            return [json.loads(ln) for ln in f.read().splitlines() if ln.strip()]

    def remove_first(self, count: int) -> None:
        lines: list[str] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        with self.path.open("w", encoding="utf-8") as f:
            for ln in lines[count:]:
                f.write(ln + "\n")

    def size(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())
