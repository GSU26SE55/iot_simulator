"""Cấu hình MQTT lúc CHẠY — mirror `firmware-esp32/src/config/mqtt_config.cpp` (IOT3-37/42).

Nguồn chân lý là NVS (`mqhost`/`mqport`/`mqtls`/`mquser`/`mqpass`/`mqprefix`), do
`POST /api/iot-devices/provision` cấp lúc chạy. `config/seed.yaml` chỉ là ĐƯỜNG LUI, dùng khi
NVS trống — y hệt vai trò của các macro trong `include/config.h` bên firmware.

Trước bản này simulator chỉ đọc `mqtt:` trong seed và **bỏ qua hoàn toàn** 6 trường MQTT mà
backend trả về trong provision response. Hệ quả: mỗi thiết bị phải chép tay credential broker
vào seed, và admin xoay key ở backend là simulator câm luôn — đúng thứ IOT3-42 sinh ra để sửa.
"""
from __future__ import annotations

import logging

from . import nvs as nvskeys
from .net_rules import (MAX_MQTT_HOST_CHARS, MAX_MQTT_PASS_CHARS,
                        MAX_MQTT_PREFIX_CHARS, MAX_MQTT_USER_CHARS,
                        derive_topic_prefix, describe_identity_error,
                        mqtt_config_usable, mqtt_port_usable,
                        validate_identity_field, IdentityFieldError)

log = logging.getLogger("iot-sim.mqttcfg")


class MqttRuntimeConfig:
    """`mqttcfg::` — giữ cấu hình broker đang dùng, có xác thực trước khi ghi."""

    def __init__(self, device_code: str, nvs, fallback_host: str, fallback_port: int,
                 fallback_tls: bool, fallback_user: str, fallback_pass: str,
                 fallback_prefix: str = ""):
        self._device_code = device_code
        self._nvs = nvs
        self._fb = {
            "host": fallback_host or "",
            "port": int(fallback_port or 0),
            "tls": bool(fallback_tls),
            "user": fallback_user or "",
            "pass": fallback_pass or "",
            "prefix": fallback_prefix or "",
        }
        self.host = ""
        self.port = 0
        self.want_tls = False
        self.username = ""
        self.password = ""
        self._prefix = ""
        self._from_nvs = False

    # ── nạp ────────────────────────────────────────────────────────────────────────────────
    def begin(self) -> None:
        """`mqttcfg::begin` — fallback seed trước, rồi NVS ghi đè TỪNG trường."""
        self.host = self._fb["host"]
        self.port = self._fb["port"]
        self.want_tls = self._fb["tls"]
        self.username = self._fb["user"] or self._derived_username()
        self.password = self._fb["pass"]
        self._prefix = self._fb["prefix"] or derive_topic_prefix(self._device_code)

        any_nvs = False
        pairs = (
            (nvskeys.KEY_MQTT_HOST, "host", MAX_MQTT_HOST_CHARS, "host"),
            (nvskeys.KEY_MQTT_USER, "username", MAX_MQTT_USER_CHARS, "username"),
            (nvskeys.KEY_MQTT_PASS, "password", MAX_MQTT_PASS_CHARS, "mật khẩu"),
            (nvskeys.KEY_MQTT_PREFIX, "_prefix", MAX_MQTT_PREFIX_CHARS, "topic prefix"),
        )
        for key, attr, max_chars, label in pairs:
            raw = self._nvs.get_string(key, "")
            if not raw:
                continue
            err = validate_identity_field(raw, max_chars)
            if err is not IdentityFieldError.OK:
                log.warning("[%s] BỎ %s trong state — %s", self._device_code, label,
                            describe_identity_error(err))
                continue
            setattr(self, attr, raw)
            any_nvs = True

        stored_port = self._nvs.get_int(nvskeys.KEY_MQTT_PORT, 0)
        if mqtt_port_usable(stored_port):
            self.port = int(stored_port)
            any_nvs = True
        elif stored_port != 0:
            log.warning("[%s] BỎ port=%s trong state — ngoài dải [1,65535]",
                        self._device_code, stored_port)

        if self._nvs.has_key(nvskeys.KEY_MQTT_TLS):
            self.want_tls = self._nvs.get_bool(nvskeys.KEY_MQTT_TLS, self.want_tls)
            any_nvs = True

        self._from_nvs = any_nvs
        log.info("[%s] mqtt host=%s port=%s prefix=%s user=%s (nguồn=%s) usable=%s",
                 self._device_code, self.host, self.port, self.topic_prefix(),
                 self.username, "nvs" if any_nvs else "seed",
                 "có" if self.is_configured() else "KHÔNG")

    def _derived_username(self) -> str:
        """Backend đặt `mqtt_username = lowercase(DeviceCode)` (IotApiKeyService).

        ACL Mosquitto khớp `pattern write solar/%u/...` với `%u` = username, nên username và
        đoạn thiết bị trong topic prefix BẮT BUỘC là cùng một chuỗi chữ thường.
        """
        return (self._device_code or "").strip().lower()

    # ── truy vấn ──────────────────────────────────────────────────────────────────────────
    def topic_prefix(self) -> str:
        if not self._prefix:
            self._prefix = derive_topic_prefix(self._device_code)
        return self._prefix

    def is_configured(self) -> bool:
        return (mqtt_config_usable(self.host, self.port, self.username, self.password)
                and bool(self.topic_prefix()))

    def is_from_nvs(self) -> bool:
        return self._from_nvs

    def snapshot(self) -> tuple:
        """Ảnh chụp để `mqtt_client` biết cấu hình có ĐỔI THẬT không (IOT3-41).

        Không so trước khi áp thì mỗi lần gọi `apply_config()` lại ngắt một phiên đang chạy tốt
        để dựng lại y nguyên — tự tạo khoảng mất kết nối, và mỗi khoảng ấy là số đo rơi mất.
        """
        return (self.host, self.port, self.username, self.password, self.topic_prefix(),
                self.want_tls)

    # ── áp cấu hình từ provision (IOT3-42) ─────────────────────────────────────────────────
    def apply_from_provision(self, host: str, port: int, use_tls: bool,
                             prefix: str | None, user: str, password: str) -> bool:
        """Kiểm TOÀN BỘ trước khi ghi bất kỳ trường nào.

        Ghi nửa vời (host mới + mật khẩu cũ) tạo ra một cấu hình không tồn tại ở đâu cả, và lần
        chạy sau nạp lên sẽ nối thất bại mà log không chỉ ra được sai ở đâu.

        Trả True nếu cấu hình ĐỔI và đã ghi; False nếu bị từ chối hoặc trùng cấu hình cũ.
        """
        fields = (
            (host, MAX_MQTT_HOST_CHARS, "host"),
            (user, MAX_MQTT_USER_CHARS, "username"),
            (password, MAX_MQTT_PASS_CHARS, "mật khẩu"),
        )
        for value, max_chars, label in fields:
            err = validate_identity_field(value, max_chars)
            if err is not IdentityFieldError.OK:
                log.warning("[%s] TỪ CHỐI cấu hình provision — %s %s",
                            self._device_code, label, describe_identity_error(err))
                return False
        if not mqtt_port_usable(port):
            log.warning("[%s] TỪ CHỐI cấu hình provision — port=%s ngoài dải [1,65535]",
                        self._device_code, port)
            return False

        if prefix:
            if validate_identity_field(prefix, MAX_MQTT_PREFIX_CHARS) is not IdentityFieldError.OK:
                log.warning("[%s] TỪ CHỐI cấu hình provision — topic prefix không hợp lệ",
                            self._device_code)
                return False
            wanted_prefix = prefix
        else:
            wanted_prefix = derive_topic_prefix(self._device_code)
            if not wanted_prefix:
                log.warning("[%s] TỪ CHỐI cấu hình provision — không suy được topic prefix",
                            self._device_code)
                return False

        changed = (self.host != host or self.username != user or self.password != password
                   or self.topic_prefix() != wanted_prefix or self.port != int(port)
                   or self.want_tls != bool(use_tls))
        if not changed:
            log.info("[%s] cấu hình MQTT từ provision trùng cấu hình đang chạy — không ghi",
                     self._device_code)
            return False

        self._nvs.put_string(nvskeys.KEY_MQTT_HOST, host)
        self._nvs.put_int(nvskeys.KEY_MQTT_PORT, int(port))
        self._nvs.put_bool(nvskeys.KEY_MQTT_TLS, bool(use_tls))
        self._nvs.put_string(nvskeys.KEY_MQTT_USER, user)
        self._nvs.put_string(nvskeys.KEY_MQTT_PASS, password)
        self._nvs.put_string(nvskeys.KEY_MQTT_PREFIX, wanted_prefix)

        self.host = host
        self.port = int(port)
        self.want_tls = bool(use_tls)
        self.username = user
        self.password = password
        self._prefix = wanted_prefix
        self._from_nvs = True

        log.info("[%s] áp cấu hình MQTT từ provision: host=%s port=%d tls=%d prefix=%s user=%s",
                 self._device_code, self.host, self.port, 1 if self.want_tls else 0,
                 self._prefix, self.username)
        return True

    def warn_if_prefix_mismatch(self) -> list[str]:
        """IOT3-40 — đối chiếu 3 nguồn phải quy về CÙNG một chuỗi chữ thường.

        Tiền tố backend cấp, username MQTT, và deviceCode. Lệch một chữ hoa là mất cả uplink lẫn
        downlink mà KHÔNG bên nào báo lỗi — broker chỉ lặng lẽ không chuyển tin. Chỉ CẢNH BÁO,
        không chặn: backend có thể cố ý dùng quy ước khác.
        """
        warnings: list[str] = []
        actual = self.topic_prefix()
        expected = derive_topic_prefix(self._device_code)
        if not expected:
            warnings.append("không suy được tiền tố từ deviceCode để đối chiếu (mã rỗng?)")
            return warnings
        if actual != expected:
            warnings.append(
                f"tiền tố topic đang dùng '{actual}' KHÁC tiền tố suy từ deviceCode '{expected}' "
                "— đây chính là kiểu lỗi làm broker im lặng nuốt tin")
        seg = actual.split("/", 1)
        if len(seg) == 2 and self.username and seg[1] != self.username:
            warnings.append(
                f"username MQTT '{self.username}' KHÁC đoạn thiết bị trong topic '{seg[1]}' — "
                "ACL `pattern ... solar/%u/...` sẽ TỪ CHỐI publish")
        for w in warnings:
            log.warning("[%s] ⚠ %s", self._device_code, w)
        return warnings
