"""MQTT client (Sprint 4 — NI §8.3, MO §52.14).

Topic design — KHỚP 1:1 firmware ESP32 `mqtt_client.cpp` (`solar/{deviceCode}/...`):
  solar/{deviceCode}/{batterySerial}/telemetry  publish (uplink reading, per-pin)
  solar/{deviceCode}/heartbeat                   publish (firmware HTTP-only, giữ helper)
  solar/{deviceCode}/status                      publish (LWT "offline" retain)
  solar/{deviceCode}/cmd                         subscribe (downlink command)
  solar/{deviceCode}/cmd/ack                     publish ack sau khi exec

> Firmware publish telemetry MỖI PIN 1 message lên `solar/{dev}/{serial}/telemetry`
> (mqtt_client.cpp::mqttPublishTelemetry). Trước đây simulator gom cả batch lên
> `solar/{siteId}/{dev}/telemetry` (sai cả topic lẫn grouping) → broker ACL
> `solar/%u/+/telemetry` reject. Đã sửa khớp firmware.

Để giữ simulator nhẹ, MQTT là optional — bật bằng `mqtt.enabled: true` trong seed.yaml hoặc env IOT_MQTT_ENABLED=true.
Khi tắt, simulator chỉ dùng HTTPS.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("iot-sim.mqtt")

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO = True
except ImportError:
    _HAS_PAHO = False


@dataclass
class MqttOptions:
    host: str
    port: int
    username: str
    password: str
    tls: bool
    qos: int
    topic_prefix: str
    device_code: str
    site_id: str


class IotMqttClient:
    """Wrapper paho-mqtt với LWT + publish helpers + downlink callback."""

    def __init__(self, opts: MqttOptions, on_command: Callable[[dict], None] | None = None,
                 on_connect: Callable[[], None] | None = None):
        if not _HAS_PAHO:
            raise RuntimeError("paho-mqtt chưa cài. pip install paho-mqtt>=2.0.0")
        self.opts = opts
        self.on_command = on_command
        # Gọi khi (re)connect thành công — device reset consecutive-fail streak (S4-FW-06,
        # firmware mqttResetConsecutiveFails trong tryConnect success).
        self.on_connect_cb = on_connect
        self._lock = threading.Lock()
        self._connected = False
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=opts.device_code,
            clean_session=True,
        )
        if opts.username:
            self._client.username_pw_set(opts.username, opts.password)
        if opts.tls:
            self._client.tls_set()
        # LWT — broker tự publish "offline" lên status nếu mất kết nối đột ngột (NI §8.3)
        self._client.will_set(
            topic=self._t("status"),
            payload="offline",
            qos=1,
            retain=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # topic helpers — khớp firmware mqtt_client.cpp (snprintf "%s/%s/...").
    def _t(self, kind: str) -> str:
        return f"{self.opts.topic_prefix}/{self.opts.device_code}/{kind}"

    def _telemetry_topic(self, battery_serial: str) -> str:
        # firmware: solar/{deviceCode}/{batterySerial}/telemetry (mqttPublishTelemetry).
        return f"{self.opts.topic_prefix}/{self.opts.device_code}/{battery_serial}/telemetry"

    def connect(self) -> bool:
        try:
            self._client.connect(self.opts.host, self.opts.port, keepalive=60)
            self._client.loop_start()
            return True
        except OSError as ex:
            log.warning("MQTT connect FAIL: %s", ex)
            return False

    def disconnect(self) -> None:
        try:
            # publish "offline" before disconnect — clean shutdown
            self._client.publish(self._t("status"), payload="offline", qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except OSError:
            pass

    def publish_telemetry(self, battery_serial: str, payload: dict) -> bool:
        # Defensive: serial rỗng → topic invalid `solar/dev//telemetry` (firmware cũng guard).
        if not battery_serial:
            log.warning("[mqtt] publish_telemetry FAIL — batterySerial rỗng")
            return False
        return self._publish(self._telemetry_topic(battery_serial), payload)

    def publish_heartbeat(self, payload: dict) -> bool:
        return self._publish(self._t("heartbeat"), payload)

    def publish_cmd_ack(self, payload: dict) -> bool:
        return self._publish(self._t("cmd/ack"), payload)

    def _publish(self, topic: str, payload: dict) -> bool:
        if not self._connected:
            return False
        try:
            info = self._client.publish(topic, json.dumps(payload), qos=self.opts.qos, retain=False)
            info.wait_for_publish(timeout=5)
            return info.is_published()
        except (OSError, ValueError) as ex:
            log.warning("MQTT publish FAIL %s: %s", topic, ex)
            return False

    @property
    def connected(self) -> bool:
        return self._connected

    # callbacks
    def _on_connect(self, _client, _userdata, _flags, rc, _props=None):
        if rc == 0:
            with self._lock:
                self._connected = True
            log.info("[mqtt] connected to %s:%d", self.opts.host, self.opts.port)
            # publish "online" retained
            self._client.publish(self._t("status"), payload="online", qos=1, retain=True)
            # subscribe downlink command
            self._client.subscribe(self._t("cmd"), qos=1)
            if self.on_connect_cb:
                self.on_connect_cb()
        else:
            log.warning("[mqtt] connect failed rc=%s", rc)

    def _on_disconnect(self, _client, _userdata, _flags, _rc, _props=None):
        with self._lock:
            self._connected = False
        log.warning("[mqtt] disconnected")

    def _on_message(self, _client, _userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as ex:
            log.warning("[mqtt] invalid cmd payload: %s", ex)
            return
        log.info("[mqtt] cmd received: %s", data)
        if self.on_command:
            self.on_command(data)
