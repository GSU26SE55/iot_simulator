"""MQTT client — mirror `firmware-esp32/src/net/mqtt_client.cpp` (S4-FW-01..06 + IOT3-38..44).

Topic (KHỚP 1:1 firmware; `<prefix>` = `mqttcfg::topicPrefix()` = `solar/<deviceCode chữ thường>`
hoặc chuỗi backend cấp trong provision response):

    <prefix>/<batterySerial>/telemetry   publish  — uplink reading, MỖI PIN 1 message
    <prefix>/heartbeat                   publish  — firmware gửi heartbeat qua HTTP, giữ helper
    <prefix>/status                      publish  — "online" retain; LWT "offline" retain
    <prefix>/cmd                         subscribe— downlink command
    <prefix>/cmd/ack                     publish  — ack sau khi thực thi

Ba điểm cố ý mô phỏng ĐÚNG GIỚI HẠN của thiết bị thật, không "khoẻ hơn":

  1. **QoS mặc định 0.** Firmware dùng PubSubClient v2.8 — thư viện KHÔNG hỗ trợ publish QoS 1
     (xem `mqtt_client.cpp` GH-746 + audit `iot-backend-contract-gaps.md` #4). Simulator chạy
     QoS 1 sẽ che mất lớp lỗi "mạng chập chờn làm rơi message mà thiết bị vẫn tin đã gửi".
     Đổi được qua `mqtt.qos` trong seed nếu muốn thử nghiệm.
  2. **Trần gói 4096 byte** (`MQTT_MAX_PACKET_SIZE`). Payload vượt trần thì PubSubClient trả
     false chứ không tự chia nhỏ — simulator cũng phải fail y hệt.
  3. **`publish()` thành công chỉ có nghĩa "đã đẩy được đi"**, KHÔNG phải "backend đã nhận".
     Nếu `Mqtt__Enabled=false` ở backend thì broker vẫn nhận và telemetry biến mất im lặng
     (audit #1). Đó là hành vi của hệ thống thật; simulator giữ nguyên để demo trung thực.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .timeutil import monotonic_ms

log = logging.getLogger("iot-sim.mqtt")

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO = True
except ImportError:  # pragma: no cover - phụ thuộc môi trường
    mqtt = None
    _HAS_PAHO = False

# include/config.h
MQTT_MAX_PACKET_SIZE = 4096
MQTT_KEEPALIVE_SEC = 30
MQTT_RECONNECT_INTERVAL_MS = 5000
# net/mqtt_client.cpp — kAuthFailThreshold
MQTT_AUTH_FAIL_THRESHOLD = 5

# Mã CONNACK báo lỗi XÁC THỰC. Hai dạng vì paho 2.x trả `ReasonCode` (0x86/0x87) cho MQTT v3.1.1
# còn bản cũ trả int thô (4/5). Gộp cả hai để không phụ thuộc phiên bản thư viện.
_AUTH_FAIL_CODES = frozenset({4, 5, 0x86, 0x87})


@dataclass
class MqttOptions:
    host: str
    port: int
    username: str
    password: str
    tls: bool
    qos: int
    topic_prefix: str          # ⚠ TIỀN TỐ ĐẦY ĐỦ, vd "solar/esp32-sim-001"
    device_code: str
    site_id: str = ""
    keepalive_s: int = MQTT_KEEPALIVE_SEC
    max_packet_size: int = MQTT_MAX_PACKET_SIZE
    reconnect_interval_ms: int = MQTT_RECONNECT_INTERVAL_MS
    auth_fail_threshold: int = MQTT_AUTH_FAIL_THRESHOLD


def _reason_value(rc) -> int:
    """Đưa `rc` của paho (int hoặc ReasonCode) về số nguyên để so sánh."""
    try:
        return int(getattr(rc, "value", rc))
    except (TypeError, ValueError):
        return -1


class IotMqttClient:
    """Wrapper paho-mqtt với LWT + publish helper + downlink callback + thống kê."""

    def __init__(self, opts: MqttOptions,
                 on_command: Callable[[object], None] | None = None,
                 on_connect: Callable[[], None] | None = None):
        if not _HAS_PAHO:
            raise RuntimeError("paho-mqtt chưa cài. pip install 'paho-mqtt>=2.0.0'")
        self.opts = opts
        self.on_command = on_command
        self.on_connect_cb = on_connect

        self._lock = threading.Lock()
        self._connected = False
        self._loop_started = False
        self._last_reconnect_ms = 0
        self._client = None
        # True trong lúc đang chạy callback `on_message` — xem ghi chú ở `_publish`.
        self._in_network_callback = False

        # Thống kê — mirror mqttPublishOkCount/FailCount/ConnectCount/ConsecutiveFailCount.
        self.publish_ok_count = 0
        self.publish_fail_count = 0
        self.connect_count = 0
        self.consecutive_fail_count = 0
        self.auth_fail_count = 0
        self.last_state = ""

        self._build_client()

    # ── dựng client ────────────────────────────────────────────────────────────────────────
    def _build_client(self) -> None:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id=self.opts.device_code,   # IOT3-38: clientId = deviceCode runtime
                        clean_session=True)
        if self.opts.username:
            c.username_pw_set(self.opts.username, self.opts.password)
        if self.opts.tls:
            c.tls_set()
        # LWT — S4-FW-02: topic <prefix>/status, payload "offline", QoS 1, retain=true.
        c.will_set(topic=self._t("status"), payload="offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=max(1, self.opts.reconnect_interval_ms // 1000),
                              max_delay=max(1, self.opts.reconnect_interval_ms // 1000))
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._client = c

    # ── topic helper ───────────────────────────────────────────────────────────────────────
    def _t(self, suffix: str) -> str:
        """`buildTopic` — MỘT công thức duy nhất cho mọi topic điều khiển."""
        return f"{self.opts.topic_prefix}/{suffix}"

    def _telemetry_topic(self, battery_serial: str) -> str:
        return f"{self.opts.topic_prefix}/{battery_serial}/telemetry"

    # ── vòng đời ───────────────────────────────────────────────────────────────────────────
    def connect(self) -> bool:
        return self._try_connect()

    def _try_connect(self) -> bool:
        self._last_reconnect_ms = monotonic_ms()
        try:
            self._client.connect(self.opts.host, self.opts.port,
                                 keepalive=self.opts.keepalive_s)
        except (OSError, ValueError) as ex:
            # Lỗi TCP/DNS — KHÔNG phải lỗi xác thực, nên không tăng auth streak (IOT3-44).
            self.last_state = f"connect lỗi mạng: {ex}"
            log.warning("[%s] MQTT connect FAIL — %s", self.opts.device_code, ex)
            return False
        if not self._loop_started:
            self._client.loop_start()
            self._loop_started = True
        return True

    def tick(self) -> None:
        """`mqttTick` — thử nối lại mỗi `reconnect_interval_ms` khi chưa có kết nối.

        Sau lần connect ĐẦU TIÊN thành công, paho tự nối lại (loop thread) với cùng nhịp; hàm này
        lo trường hợp lần đầu thất bại — lúc đó loop chưa chạy nên không ai retry hộ.
        """
        if self._connected:
            return
        if self._loop_started:
            return  # paho đang tự retry
        now = monotonic_ms()
        if now - self._last_reconnect_ms < self.opts.reconnect_interval_ms:
            return
        self._try_connect()

    def disconnect(self) -> None:
        """Ngắt SẠCH: publish "offline" retain trước khi đi (LWT chỉ lo lúc rớt đột ngột)."""
        try:
            if self._connected:
                self._client.publish(self._t("status"), payload="offline", qos=1, retain=True)
            if self._loop_started:
                self._client.loop_stop()
                self._loop_started = False
            self._client.disconnect()
        except (OSError, ValueError):
            pass
        with self._lock:
            self._connected = False

    def apply_config(self, opts: MqttOptions) -> bool:
        """`mqttApplyConfig` (IOT3-41) — áp cấu hình mới, chỉ khi cấu hình ĐỔI THẬT.

        Cấu hình y hệt thì KHÔNG đụng vào phiên đang chạy: ngắt rồi dựng lại y nguyên chỉ tạo ra
        khoảng mất kết nối, và mỗi khoảng ấy là số đo rơi mất.
        """
        same = (self.opts.host == opts.host and self.opts.port == opts.port
                and self.opts.username == opts.username
                and self.opts.password == opts.password
                and self.opts.topic_prefix == opts.topic_prefix
                and self.opts.tls == opts.tls)
        if same:
            return False

        log.info("[%s] cấu hình MQTT đổi → dựng lại phiên (broker=%s:%d prefix=%s)",
                 opts.device_code, opts.host, opts.port, opts.topic_prefix)
        self.disconnect()
        self.opts = opts
        # Credential mới ⇒ streak xác thực cũ không còn ý nghĩa, xoá kẻo re-provision oan.
        self.auth_fail_count = 0
        self.consecutive_fail_count = 0
        self._last_reconnect_ms = 0
        self._build_client()
        self._try_connect()
        return True

    # ── publish ────────────────────────────────────────────────────────────────────────────
    def publish_telemetry(self, battery_serial: str, payload: dict) -> bool:
        # Serial rỗng → topic `<prefix>//telemetry`, broker ACL `solar/%u/+/telemetry` từ chối
        # trong im lặng. Báo lỗi sớm để không tốn một vòng gửi.
        if not battery_serial:
            log.warning("[%s] publish_telemetry FAIL — batterySerial rỗng", self.opts.device_code)
            self._count_fail()
            return False
        return self._publish(self._telemetry_topic(battery_serial), payload)

    def publish_heartbeat(self, payload: dict) -> bool:
        return self._publish(self._t("heartbeat"), payload)

    def publish_cmd_ack(self, payload: dict) -> bool:
        return self._publish(self._t("cmd/ack"), payload)

    def publish_status(self, status: str, retain: bool = True) -> bool:
        return self._publish(self._t("status"), status, retain=retain)

    def _count_fail(self) -> None:
        self.publish_fail_count += 1
        self.consecutive_fail_count += 1

    def _publish(self, topic: str, payload, retain: bool = False) -> bool:
        if not self._connected:
            self._count_fail()
            return False

        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        encoded = body.encode("utf-8")
        # PubSubClient dùng MỘT buffer cho cả gói (fixed header + topic + payload).
        # +16 là phần header/độ dài topic — xấp xỉ đủ chặt để bắt đúng ngưỡng firmware sẽ fail.
        if len(encoded) + len(topic.encode("utf-8")) + 16 > self.opts.max_packet_size:
            log.warning("[%s] publish FAIL — gói %d byte vượt trần %d của PubSubClient (topic=%s)",
                        self.opts.device_code, len(encoded), self.opts.max_packet_size, topic)
            self._count_fail()
            return False

        try:
            info = self._client.publish(topic, encoded, qos=self.opts.qos, retain=retain)
        except (OSError, ValueError) as ex:
            log.warning("[%s] MQTT publish FAIL %s: %s", self.opts.device_code, topic, ex)
            self._count_fail()
            return False

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("[%s] MQTT publish FAIL %s rc=%s", self.opts.device_code, topic, info.rc)
            self._count_fail()
            return False

        # ⚠ KHÔNG chờ ở hai trường hợp dưới — chờ sai chỗ là tự khoá chính mình:
        #
        #   · QoS 0: `rc == MQTT_ERR_SUCCESS` đã có nghĩa "đã đẩy được vào socket", đúng bằng
        #     ngữ nghĩa `PubSubClient::publish()` của firmware. Chờ thêm không mang lại bảo đảm
        #     nào (QoS 0 không có PUBACK).
        #   · Đang ở TRONG callback `on_message`: callback đó chạy trên chính luồng mạng của
        #     paho, mà `wait_for_publish()` lại đợi luồng đó gửi xong ⇒ DEADLOCK tới lúc timeout.
        #     Triệu chứng đo được: mỗi lệnh downlink bị trễ ĐÚNG 5 giây và ack không bao giờ ra
        #     tới broker. Ack phải đi ngay trong callback (giống firmware), nên lối thoát là
        #     không chờ — `rc` đã đủ để biết gói được nhận vào hàng gửi.
        if self.opts.qos > 0 and not self._in_network_callback:
            try:
                info.wait_for_publish(timeout=5)
            except (RuntimeError, ValueError):
                self._count_fail()
                return False

        self.publish_ok_count += 1
        self.consecutive_fail_count = 0
        return True

    # ── trạng thái ─────────────────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    def reset_consecutive_fails(self) -> None:
        self.consecutive_fail_count = 0

    def reset_auth_failures(self) -> None:
        self.auth_fail_count = 0

    def auth_failure_threshold(self) -> int:
        return self.opts.auth_fail_threshold

    # ── callback paho ──────────────────────────────────────────────────────────────────────
    def _on_connect(self, _client, _userdata, _flags, rc, _props=None):
        code = _reason_value(rc)
        if code == 0:
            with self._lock:
                self._connected = True
            self.connect_count += 1
            self.consecutive_fail_count = 0
            self.auth_fail_count = 0   # IOT3-44 — nối được nghĩa là credential đang dùng ĐÚNG
            self.last_state = "connected"
            log.info("[%s] MQTT CONNECTED %s:%d (count=%d)", self.opts.device_code,
                     self.opts.host, self.opts.port, self.connect_count)
            # S4-FW-03: "online" retain QoS 1 để đè LWT "offline".
            self._client.publish(self._t("status"), payload="online", qos=1, retain=True)
            # S4-FW-03: subscribe downlink cmd QoS 1.
            self._client.subscribe(self._t("cmd"), qos=1)
            log.info("[%s] subscribe %s", self.opts.device_code, self._t("cmd"))
            if self.on_connect_cb:
                self.on_connect_cb()
            return

        with self._lock:
            self._connected = False
        if code in _AUTH_FAIL_CODES:
            # Tách RIÊNG khỏi lỗi mạng: mất mạng thì chờ là xong, còn sai thông tin đăng nhập thì
            # chờ bao lâu cũng vô ích — chỉ `/provision` cấp credential mới mới cứu được.
            self.auth_fail_count += 1
            self.last_state = f"từ chối XÁC THỰC (rc={code})"
            log.warning("[%s] MQTT bị từ chối XÁC THỰC lần thứ %d liên tiếp (ngưỡng %d → "
                        "xin credential mới qua /provision)", self.opts.device_code,
                        self.auth_fail_count, self.opts.auth_fail_threshold)
        else:
            # Lỗi mạng KHÔNG được xoá streak xác thực — mạng chập chờn xen giữa các lần bị từ chối
            # sẽ reset đếm về 0 mãi mãi, và thiết bị không bao giờ tự lành.
            self.last_state = f"connect từ chối rc={code}"
            log.warning("[%s] MQTT connect FAIL rc=%s", self.opts.device_code, code)

    def _on_disconnect(self, _client, _userdata, _flags=None, rc=None, _props=None):
        with self._lock:
            self._connected = False
        log.warning("[%s] MQTT disconnected", self.opts.device_code)

    def _on_message(self, _client, _userdata, msg):
        try:
            raw = msg.payload.decode("utf-8")
        except UnicodeDecodeError as ex:
            log.warning("[%s] cmd payload không decode được: %s", self.opts.device_code, ex)
            return
        log.info("[%s] MQTT RX topic=%s len=%d", self.opts.device_code, msg.topic, len(raw))
        if not self.on_command:
            return
        # Cờ này cho `_publish` biết đang chạy trên luồng mạng của paho (xem ghi chú ở đó).
        # `finally` là bắt buộc: handler ném lỗi mà cờ còn bật thì mọi publish sau đó bỏ chờ
        # vĩnh viễn.
        self._in_network_callback = True
        try:
            # Đưa chuỗi THÔ lên trên: parse lỗi cũng phải ack `failed` kèm nguyên nhân, giống
            # `cmd::onCommandPayload` của firmware. Parse ở đây sẽ nuốt mất lỗi đó.
            self.on_command(raw)
        except Exception:                       # noqa: BLE001 - luồng mạng không được chết
            log.exception("[%s] xử lý lệnh downlink lỗi", self.opts.device_code)
        finally:
            self._in_network_callback = False
