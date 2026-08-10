"""`IotMqttClient` — hành vi publish, trần gói, đếm lỗi xác thực, và chống tự khoá luồng mạng.

Không cần broker: thay `_client` bằng đồ giả để soi đúng lời gọi đi xuống paho.
"""
from __future__ import annotations

import unittest

import paho.mqtt.client as paho

from src.mqtt_client import IotMqttClient, MqttOptions


class _FakeInfo:
    def __init__(self, rc=paho.MQTT_ERR_SUCCESS):
        self.rc = rc
        self.waited = False

    def wait_for_publish(self, timeout=None):
        self.waited = True


class _FakePaho:
    def __init__(self, rc=paho.MQTT_ERR_SUCCESS):
        self.rc = rc
        self.published: list[tuple] = []
        self.subscribed: list[tuple] = []
        self.infos: list[_FakeInfo] = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        info = _FakeInfo(self.rc)
        self.infos.append(info)
        return info

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))


def _client(qos: int = 0, max_packet: int = 4096) -> IotMqttClient:
    opts = MqttOptions(host="h", port=1883, username="esp32-sim-001", password="p", tls=False,
                       qos=qos, topic_prefix="solar/esp32-sim-001",
                       device_code="esp32-sim-001", max_packet_size=max_packet)
    c = IotMqttClient(opts)
    c._client = _FakePaho()
    c._connected = True
    return c


class PublishTest(unittest.TestCase):
    def test_telemetry_topic_and_payload(self):
        c = _client()
        self.assertTrue(c.publish_telemetry("BAT-1", {"items": [{"a": 1}]}))
        topic, payload, qos, retain = c._client.published[0]
        self.assertEqual(topic, "solar/esp32-sim-001/BAT-1/telemetry")
        self.assertEqual(qos, 0)
        self.assertFalse(retain)
        self.assertIn(b'"items"', payload)

    def test_status_is_retained(self):
        c = _client()
        c.publish_status("online")
        self.assertTrue(c._client.published[0][3], "status PHẢI retain để LWT bị đè đúng cách")

    def test_blank_serial_rejected_before_publishing(self):
        c = _client()
        self.assertFalse(c.publish_telemetry("", {"items": []}))
        self.assertEqual(c._client.published, [])
        self.assertEqual(c.consecutive_fail_count, 1)

    def test_publish_when_disconnected_counts_fail(self):
        c = _client()
        c._connected = False
        self.assertFalse(c.publish_cmd_ack({"cmdId": "x", "status": "ok"}))
        self.assertEqual(c.publish_fail_count, 1)

    def test_oversize_packet_fails_like_pubsubclient(self):
        """PubSubClient dùng MỘT buffer 4096 cho cả gói và trả false khi vượt — nó KHÔNG tự chia
        nhỏ. Simulator phải hỏng y hệt, nếu không sẽ che mất giới hạn thật của thiết bị."""
        c = _client(max_packet=512)
        big = {"items": [{"padding": "x" * 1000}]}
        self.assertFalse(c.publish_telemetry("BAT-1", big))
        self.assertEqual(c._client.published, [])
        self.assertEqual(c.consecutive_fail_count, 1)

    def test_rc_failure_counts_fail(self):
        c = _client()
        c._client.rc = paho.MQTT_ERR_NO_CONN
        self.assertFalse(c.publish_telemetry("BAT-1", {"items": []}))
        self.assertEqual(c.publish_fail_count, 1)

    def test_success_resets_consecutive_failures(self):
        c = _client()
        c.publish_telemetry("", {})            # hỏng
        self.assertEqual(c.consecutive_fail_count, 1)
        c.publish_telemetry("BAT-1", {"items": []})
        self.assertEqual(c.consecutive_fail_count, 0)


class NoSelfDeadlockTest(unittest.TestCase):
    """Chống tự khoá luồng mạng — lỗi đo được: mỗi lệnh downlink trễ ĐÚNG 5 giây.

    `on_message` chạy TRÊN luồng mạng của paho; `wait_for_publish()` lại đợi chính luồng đó gửi
    xong ⇒ khoá cứng tới lúc timeout, và ack không bao giờ ra tới broker.
    """

    def test_qos0_never_waits(self):
        c = _client(qos=0)
        c.publish_telemetry("BAT-1", {"items": []})
        self.assertFalse(c._client.infos[0].waited,
                         "QoS 0 không có PUBACK — chờ chỉ tổ tốn thời gian")

    def test_qos1_waits_on_normal_thread(self):
        c = _client(qos=1)
        c.publish_telemetry("BAT-1", {"items": []})
        self.assertTrue(c._client.infos[0].waited)

    def test_qos1_does_not_wait_inside_network_callback(self):
        c = _client(qos=1)
        c._in_network_callback = True
        c.publish_cmd_ack({"cmdId": "x", "status": "ok"})
        self.assertFalse(c._client.infos[0].waited)

    def test_flag_is_cleared_even_when_handler_raises(self):
        """Cờ kẹt ở True thì MỌI publish sau đó bỏ chờ vĩnh viễn — phải dọn trong `finally`."""
        opts = MqttOptions(host="h", port=1883, username="u", password="p", tls=False, qos=1,
                           topic_prefix="solar/dev", device_code="dev")

        def boom(_payload):
            raise ValueError("handler hỏng")

        c = IotMqttClient(opts, on_command=boom)
        c._client = _FakePaho()
        c._connected = True

        class _Msg:
            topic = "solar/dev/cmd"
            payload = b'{"cmdId":"x","type":"set_interval"}'

        c._on_message(None, None, _Msg())          # không được ném ra ngoài
        self.assertFalse(c._in_network_callback)


class ConnectCallbackTest(unittest.TestCase):
    def _connack(self, c, code):
        c._on_connect(None, None, None, code)

    def test_success_publishes_online_and_subscribes(self):
        c = _client()
        c._connected = False
        self._connack(c, 0)
        self.assertTrue(c.connected)
        self.assertIn(("solar/esp32-sim-001/status", "online", 1, True),
                      [(t, p, q, r) for t, p, q, r in c._client.published])
        self.assertEqual(c._client.subscribed, [("solar/esp32-sim-001/cmd", 1)])

    def test_auth_failures_counted_separately_from_network_errors(self):
        """IOT3-44 — mất mạng thì chờ là xong; sai credential thì chờ mãi cũng vô ích."""
        c = _client()
        for code in (4, 5, 0x86, 0x87):
            c.auth_fail_count = 0
            self._connack(c, code)
            self.assertEqual(c.auth_fail_count, 1, f"rc={code} phải tính là lỗi xác thực")

        c.auth_fail_count = 3
        self._connack(c, 3)                      # server unavailable = lỗi mạng
        self.assertEqual(c.auth_fail_count, 3, "lỗi mạng KHÔNG được xoá streak xác thực")

    def test_successful_connect_clears_auth_streak(self):
        c = _client()
        c.auth_fail_count = 4
        self._connack(c, 0)
        self.assertEqual(c.auth_fail_count, 0)


if __name__ == "__main__":
    unittest.main()
