"""Đọc kết quả NHẬN MỘT PHẦN nằm trong response HTTP 2xx.

Mirror `firmware-esp32/src/core/ingest_result.{h,cpp}` (GH-748).

Lỗi gốc bên firmware — và y hệt bên simulator trước bản này: coi MỌI 2xx là "cả batch đã vào",
không hề đọc thân response. Backend lại trả `{ totalReceived, inserted, skipped }`, nên khi
backend chỉ nhận một phần (serial pin chưa được map cho thiết bị, hoặc giá trị ngoài dải vật lý)
thì việc bỏ dữ liệu diễn ra HOÀN TOÀN IM LẶNG.

Vì sao KHÔNG gửi lại phần bị bỏ: `skipped` của backend gồm `mapping_invalid` và
`rejectedOutliers` — cả hai đều VĨNH VIỄN, gửi lại đúng dữ liệu đó chỉ ra đúng kết quả đó.
Cái thực sự mất là **tín hiệu**. ⇒ Việc đúng là ĐỌC và LA LÊN, không phải retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class IngestResult:
    """`core::IngestResult`."""

    parsed: bool = False
    total_received: int = 0
    inserted: int = 0
    skipped: int = 0

    def is_partial(self) -> bool:
        return self.parsed and self.total_received > 0 and self.inserted < self.total_received


def parse_ingest_result(body_json: Any) -> IngestResult:
    """Đọc `data.{totalReceived,inserted,skipped}` từ thân `CommonResponse`.

    Thiếu `totalReceived` hoặc `inserted` → trả `parsed=False` thay vì mặc định 0: `inserted=0`
    mặc định sẽ bị hiểu thành "backend không nhận gì cả" và sinh cảnh báo giả.
    """
    out = IngestResult()
    if not isinstance(body_json, dict):
        return out
    data = body_json.get("data")
    if not isinstance(data, dict):
        return out

    total = data.get("totalReceived")
    inserted = data.get("inserted")
    if not isinstance(total, int) or isinstance(total, bool):
        return out
    if not isinstance(inserted, int) or isinstance(inserted, bool):
        return out

    skipped = data.get("skipped")
    out.total_received = total
    out.inserted = inserted
    out.skipped = skipped if isinstance(skipped, int) and not isinstance(skipped, bool) else 0
    out.parsed = True
    return out
