"""Lệnh downlink qua MQTT — mirror `firmware-esp32/src/cmd/cmd_logic.cpp` +
`cmd/command_handler.cpp` (S4-FW-05 + GH-745).

Envelope (backend `IotDeviceCommandDtos`):
    { "cmdId": "...", "type": "set_interval|request_heartbeat|trigger_ota", "params": { ... } }

Ack publish lên `<prefix>/cmd/ack`:
    { "cmdId": "...", "status": "ok|failed|unknown|rejected", "error"?: "..." }

⚠ `rejected` là status mà bản simulator cũ KHÔNG có. Firmware trả nó khi `trigger_ota` bị từ chối
kèm nguyên nhân (`ota::otaLastRejectReason`) — ví dụ đang xác minh bản vừa flash. Thiếu status này
thì người vận hành bấm "cập nhật ngay" luôn thấy "ok" dù thiết bị không làm gì.

⚠ Từ vựng lệnh hai bên CHƯA thống nhất (audit `iot-backend-contract-gaps.md` #3): backend gợi ý
`reboot | ota | calibrate | sample-now | set-config` nhưng KHÔNG validate, còn firmware chỉ hiểu 3
lệnh dưới đây. Simulator giữ đúng tập của firmware để lộ ra đúng điểm lệch đó khi test.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

# cmd_logic.h — khoảng hợp lệ của set_interval.
POLLING_SECONDS_MIN = 1
POLLING_SECONDS_MAX = 3600


class CommandKind(Enum):
    UNKNOWN = 0
    SET_INTERVAL = 1
    REQUEST_HEARTBEAT = 2
    TRIGGER_OTA = 3
    # Phần mở rộng RIÊNG của simulator (firmware trả `unknown`) — đổi scenario lúc đang chạy để
    # demo. Được đánh dấu rõ ở đây để không ai nhầm là contract của thiết bị thật.
    SET_SCENARIO = 90


# Cặp bí danh `_` / `-`, so sánh KHÔNG phân biệt hoa thường — khớp `logic::matches`.
_TYPE_ALIASES: dict[str, CommandKind] = {
    "set_interval": CommandKind.SET_INTERVAL,
    "set-interval": CommandKind.SET_INTERVAL,
    "request_heartbeat": CommandKind.REQUEST_HEARTBEAT,
    "request-heartbeat": CommandKind.REQUEST_HEARTBEAT,
    "trigger_ota": CommandKind.TRIGGER_OTA,
    "trigger-ota": CommandKind.TRIGGER_OTA,
    "set_scenario": CommandKind.SET_SCENARIO,
    "set-scenario": CommandKind.SET_SCENARIO,
}


@dataclass
class ParsedCommand:
    """`cmd::logic::ParsedCommand`."""

    ok: bool = False
    cmd_id: str = ""
    type: str = ""
    kind: CommandKind = CommandKind.UNKNOWN
    polling_seconds: int = 0
    has_polling_seconds: bool = False
    params: dict | None = None
    parse_error: str = ""


def classify_type(type_str: str | None) -> CommandKind:
    """`logic::classifyType`."""
    if not type_str:
        return CommandKind.UNKNOWN
    return _TYPE_ALIASES.get(type_str.strip().lower(), CommandKind.UNKNOWN)


def is_valid_polling_seconds(seconds: int) -> bool:
    """`logic::isValidPollingSeconds`."""
    return POLLING_SECONDS_MIN <= seconds <= POLLING_SECONDS_MAX


def parse_command_payload(payload) -> ParsedCommand:
    """`logic::parseCommandPayload` — parse THUẦN, KHÔNG side effect.

    Nhận `bytes` / `str` (đường MQTT thật) hoặc `dict` (test/gọi nội bộ).
    """
    r = ParsedCommand()

    if payload is None:
        r.parse_error = "empty payload"
        return r

    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as ex:
            r.parse_error = f"payload không decode được: {ex}"
            return r

    if isinstance(payload, str):
        if not payload.strip():
            r.parse_error = "empty payload"
            return r
        try:
            payload = json.loads(payload)
        except ValueError as ex:
            r.parse_error = str(ex)
            return r

    if not isinstance(payload, dict):
        r.parse_error = "payload không phải JSON object"
        return r

    r.cmd_id = str(payload.get("cmdId", "") or "")
    r.type = str(payload.get("type", "") or "")
    if not r.type:
        r.parse_error = "missing field: type"
        return r

    r.kind = classify_type(r.type)

    params = payload.get("params")
    if isinstance(params, dict):
        r.params = params
        # Firmware chỉ nhận `uint32_t` — số âm hay số thực bị BỎ QUA (hasPollingSeconds=false),
        # dẫn tới ack "missing pollingSeconds". Giữ nguyên hành vi đó.
        for key in ("pollingSeconds", "pollingIntervalSeconds"):
            value = params.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                r.polling_seconds = value
                r.has_polling_seconds = True
                break

    r.ok = True
    return r


def build_ack(cmd_id: str, status: str, error: str | None = None) -> dict:
    """`cmd::publishAck` — ack CHỈ có `cmdId`, `status`, và `error` khi có lỗi.

    KHÔNG có khoá `message`: backend bind theo đúng 3 khoá này.
    """
    ack: dict = {"cmdId": cmd_id or "", "status": status or "unknown"}
    if error:
        ack["error"] = error
    return ack
