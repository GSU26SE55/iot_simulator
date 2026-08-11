"""Luật THUẦN cho định danh + cấu hình MQTT runtime.

Mirror:
  - `firmware-esp32/src/core/identity_validation.h`  (validateIdentityField)
  - `firmware-esp32/src/core/net_config_rules.h`     (mqttPortUsable, mqttConfigUsable,
                                                      deriveTopicPrefix)

Không import gì thuộc simulator → test được độc lập, giống các header `core/` của firmware.
"""
from __future__ import annotations

from enum import Enum

# ─────────────────────────── Identity (deviceCode / apiKey / MQTT fields) ───────────────────
# core/device_identity.h — số KÝ TỰ tối đa lấy đúng theo cột backend.
MAX_DEVICE_CODE_CHARS = 64      # device_code       HasMaxLength(64)
MAX_API_KEY_CHARS = 128         # api_key_plaintext HasMaxLength(128)

# core/net_config_rules.h
MAX_MQTT_HOST_CHARS = 64
MAX_MQTT_USER_CHARS = 64
MAX_MQTT_PASS_CHARS = 64
MAX_MQTT_PREFIX_CHARS = 96


class IdentityFieldError(Enum):
    OK = 0
    EMPTY = 1
    TOO_LONG = 2
    INVALID_CHAR = 3


def describe_identity_error(err: "IdentityFieldError") -> str:
    return {
        IdentityFieldError.OK: "hợp lệ",
        IdentityFieldError.EMPTY: "giá trị rỗng",
        IdentityFieldError.TOO_LONG: "quá dài, không vừa bộ nhớ thiết bị",
        IdentityFieldError.INVALID_CHAR: "chứa khoảng trắng hoặc ký tự không in được",
    }.get(err, "không rõ")


def validate_identity_field(value: str | None, max_chars: int) -> IdentityFieldError:
    """`core::validateIdentityField` — chỉ nhận ASCII in được KHÔNG kể khoảng trắng (0x21–0x7E).

    Đủ rộng cho mọi giá trị backend sinh ra (`iotk_` + base62, `gw-esp32-mvp-001`), đủ chặt để
    chặn CR/LF/tab/space — thứ vừa làm hỏng header HTTP (tiêm header qua `X-Api-Key`) vừa làm
    hỏng topic MQTT.
    """
    if value is None or value == "":
        return IdentityFieldError.EMPTY
    if len(value) > max_chars:
        return IdentityFieldError.TOO_LONG
    for ch in value:
        code = ord(ch)
        if code < 0x21 or code > 0x7E:
            return IdentityFieldError.INVALID_CHAR
    return IdentityFieldError.OK


def identity_is_valid(value: str | None, max_chars: int) -> bool:
    return validate_identity_field(value, max_chars) is IdentityFieldError.OK


# ───────────────────────────────────── MQTT ────────────────────────────────────────────────
def mqtt_port_usable(port: int | None) -> bool:
    """`core::mqttPortUsable` — 0 là 'chưa đặt', không phải cổng."""
    try:
        p = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return 0 < p <= 65535


def mqtt_config_usable(host: str | None, port: int | None,
                       user: str | None, password: str | None) -> bool:
    """`core::mqttConfigUsable` — phải ĐỦ CẢ BỐN mới tính là đã cấu hình.

    Thiếu bất kỳ trường nào thì kết nối cũng thất bại, nhưng thất bại theo kiểu im lặng lặp lại
    mỗi 5 giây — rất tốn công truy. Backend cam kết trả "cả sáu trường hoặc không trường nào",
    nên trạng thái nửa vời ở đây nghĩa là state file hỏng hoặc người dùng đặt tay một nửa.
    """
    if not host:
        return False
    if not mqtt_port_usable(port):
        return False
    if not user:
        return False
    if not password:
        return False
    return True


def derive_topic_prefix(device_code: str | None, root: str = "solar") -> str:
    """`core::deriveTopicPrefix` — suy tiền tố topic khi backend chưa cấp.

    Quy ước PHẢI KHỚP `MqttBrokerEndpointProvider.TopicPrefixFor()` của backend:
        "solar/" + deviceCode.Trim().ToLowerInvariant()

    `root` là phần mở rộng của simulator để đổi được gốc topic qua `seed.yaml`
    (firmware hằng số hoá "solar/"). Mặc định giữ đúng quy ước backend.

    ACL Mosquitto dùng `pattern write solar/%u/...` với `%u` = username = deviceCode chữ thường.
    So khớp topic MQTT PHÂN BIỆT hoa/thường và không tắt được → sai một chữ hoa là mất cả uplink
    lẫn downlink mà KHÔNG bên nào báo lỗi.

    Trả chuỗi rỗng nếu không suy được (deviceCode rỗng) — caller phải coi đó là "chưa cấu hình",
    tuyệt đối KHÔNG được dùng "solar/" trần làm tiền tố.
    """
    if not device_code:
        return ""
    code = device_code.strip().lower()
    if not code:
        return ""
    clean_root = (root or "solar").strip().strip("/")
    if not clean_root:
        clean_root = "solar"
    return f"{clean_root}/{code}"
