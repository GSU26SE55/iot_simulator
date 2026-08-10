"""Chống mất/trùng dữ liệu — nhóm bug mà firmware đã vá và simulator phải mô phỏng đủ.

Bao gồm: phân loại lỗi tạm/vĩnh viễn, trần hàng đợi + drop-oldest, đẩy bù có backoff,
GH-737 (mất mạng vẫn lấy mẫu), GH-740 (không gửi trùng sau MQTT publish một phần),
GH-748 (đọc `inserted/skipped` trong 2xx).
"""
from __future__ import annotations

import unittest

from src.backoff import Backoff, is_transient_failure
from src.config import CONTRACT_IOT2
from src.ingest_result import parse_ingest_result
from src.led import LedState
from src.link import LinkPhase, LinkState
from src.policy import IngestAction, ingest_action
from src.queue import MAX_QUEUED_BATCHES, LocalQueue

from tests.fakes import DeviceHarness, FakeMqtt, net_error, ok


class TransientClassificationTest(unittest.TestCase):
    """`net::isTransientFailure` — quyết định GIỮ hay BỎ một batch."""

    def test_network_error_is_transient(self):
        self.assertTrue(is_transient_failure(0))

    def test_5xx_is_transient(self):
        for code in (500, 502, 503, 504):
            self.assertTrue(is_transient_failure(code), code)

    def test_timeout_and_rate_limit_are_transient(self):
        self.assertTrue(is_transient_failure(408))
        self.assertTrue(is_transient_failure(429))

    def test_other_4xx_are_permanent(self):
        for code in (400, 401, 403, 404, 409, 422):
            self.assertFalse(is_transient_failure(code), code)


class BackoffTest(unittest.TestCase):
    def test_grows_exponentially_and_caps(self):
        b = Backoff(base_ms=2000, max_ms=300000, jitter_pct=0.0)
        waits = [b.record_failure(now_ms=0) for _ in range(10)]
        self.assertEqual(waits[0], 2000)
        self.assertEqual(waits[1], 4000)
        self.assertEqual(waits[2], 8000)
        self.assertLessEqual(max(waits), 300000)

    def test_reset_clears_gate(self):
        b = Backoff(jitter_pct=0.0)
        b.record_failure(now_ms=0)
        self.assertFalse(b.allowed(now_ms=0))
        b.reset()
        self.assertTrue(b.allowed(now_ms=0))

    def test_jitter_stays_inside_twenty_percent(self):
        for _ in range(200):
            b = Backoff(base_ms=2000, max_ms=300000, jitter_pct=0.20)
            wait = b.record_failure(now_ms=0)
            self.assertGreaterEqual(wait, 1600)
            self.assertLessEqual(wait, 2400)


class QueueTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.q = LocalQueue(Path(self._tmp.name) / "q.jsonl")

    def test_fifo_peek_and_delete(self):
        self.q.append("/api/sensor-readings/batch", {"items": [{"n": 1}]}, "k1")
        self.q.append("/api/sensor-readings/batch", {"items": [{"n": 2}]}, "k2")
        self.assertEqual(self.q.peek_oldest()["key"], "k1")
        self.q.delete_oldest()
        self.assertEqual(self.q.peek_oldest()["key"], "k2")
        self.assertEqual(self.q.size(), 1)

    def test_cap_drops_oldest(self):
        """Trần 200 batch (`kMaxQueuedBatches`). Không có trần thì file phình vô hạn — thiết bị
        thật không có flash để làm thế, nên simulator không được 'khoẻ hơn'."""
        q = LocalQueue(self.q.path, max_batches=3)
        for i in range(5):
            q.append("/api/sensor-readings/batch", {"items": [{"n": i}]}, f"k{i}")
        self.assertEqual(q.size(), 3)
        self.assertEqual(q.peek_oldest()["key"], "k2")
        self.assertEqual(q.dropped_count, 2)

    def test_default_cap_matches_firmware(self):
        self.assertEqual(MAX_QUEUED_BATCHES, 200)


class IngestActionTest(unittest.TestCase):
    def test_online_posts(self):
        self.assertIs(ingest_action(True, True), IngestAction.POST_ONLINE)

    def test_offline_still_samples_and_queues(self):
        self.assertIs(ingest_action(False, True), IngestAction.QUEUE_OFFLINE)

    def test_no_clock_skips(self):
        self.assertIs(ingest_action(True, False), IngestAction.SKIP_NO_CLOCK)
        self.assertIs(ingest_action(False, False), IngestAction.SKIP_NO_CLOCK)


class IngestResultTest(unittest.TestCase):
    """GH-748 — 2xx KHÔNG có nghĩa cả batch đã vào."""

    def test_partial_detected(self):
        r = parse_ingest_result({"data": {"totalReceived": 6, "inserted": 4, "skipped": 2}})
        self.assertTrue(r.parsed)
        self.assertTrue(r.is_partial())

    def test_full_insert_is_not_partial(self):
        r = parse_ingest_result({"data": {"totalReceived": 6, "inserted": 6, "skipped": 0}})
        self.assertFalse(r.is_partial())

    def test_missing_fields_do_not_raise_false_alarm(self):
        """Thiếu trường → parsed=False, KHÔNG được mặc định inserted=0 rồi báo 'mất hết'."""
        self.assertFalse(parse_ingest_result({"data": {"skipped": 1}}).is_partial())
        self.assertFalse(parse_ingest_result({}).is_partial())
        self.assertFalse(parse_ingest_result(None).is_partial())
        self.assertFalse(parse_ingest_result("cắt ngắn {").is_partial())


class LinkStateTest(unittest.TestCase):
    def test_http_response_means_link_up_even_on_error_status(self):
        link = LinkState()
        link.note_result(500, now_ms=0)
        self.assertTrue(link.is_up())      # 500 = tới được backend, chỉ là backend lỗi

    def test_transport_error_means_link_down(self):
        link = LinkState()
        link.note_result(200, now_ms=0)
        link.note_result(0, now_ms=1000)
        self.assertFalse(link.is_up())

    def test_phases(self):
        link = LinkState()
        link.note_result(200, now_ms=0)
        self.assertIs(link.phase(now_ms=0), LinkPhase.CONNECTED)
        link.note_result(0, now_ms=1000)
        self.assertIs(link.phase(now_ms=2000), LinkPhase.CONNECTING)
        self.assertIs(link.phase(now_ms=1000 + 30001), LinkPhase.RECOVERY)

    def test_unconfigured_when_identity_missing(self):
        link = LinkState(identity_ready=False)
        self.assertIs(link.phase(now_ms=0), LinkPhase.UNCONFIGURED)


class DeviceResilienceTest(unittest.TestCase):
    def setUp(self):
        self.h = DeviceHarness(contract=CONTRACT_IOT2)
        self.addCleanup(self.h.close)
        self.dev = self.h.device
        self.dev._provision_done = True
        self.dev._prov_cfg.provisioned = True

    # ── phân loại lỗi ────────────────────────────────────────────────────────────────────
    def test_permanent_4xx_is_dropped_not_queued(self):
        """4xx nằm đầu hàng đợi sẽ chặn VĨNH VIỄN mọi batch phía sau ⇒ phải BỎ ngay."""
        self.h.http.ingest_response = ok(400, body='{"message":"invalid"}')
        self.assertFalse(self.dev._ingest_once())
        self.assertEqual(self.dev._queue.size(), 0)
        self.assertEqual(self.dev.state.dropped_batches, 1)

    def test_transient_5xx_is_queued_with_backoff(self):
        self.h.http.ingest_response = ok(503)
        self.assertFalse(self.dev._ingest_once())
        self.assertEqual(self.dev._queue.size(), 1)
        self.assertGreater(self.dev.state.backoff_s, 0.0)

    def test_network_error_is_queued(self):
        self.h.http.ingest_response = net_error()
        self.assertFalse(self.dev._ingest_once())
        self.assertEqual(self.dev._queue.size(), 1)

    def test_queued_batch_keeps_its_idempotency_key(self):
        self.h.http.ingest_response = ok(503)
        self.dev._ingest_once()
        item = self.dev._queue.peek_oldest()
        self.assertTrue(item["key"])
        # Đẩy bù dùng LẠI đúng khoá đó ⇒ backend khử trùng, gửi nhiều lần không sinh bản ghi thừa.
        self.h.http.ingest_response = ok(201)
        self.dev._backoff.reset()
        self.dev._try_flush_queue(now=10**9)
        self.assertEqual(self.h.http.ingest_calls[-1][1], item["key"])

    # ── GH-737 ───────────────────────────────────────────────────────────────────────────
    def test_offline_tick_still_samples_and_queues(self):
        self.h.http.ingest_response = net_error()
        self.dev._last_ingest_ms = -10**9
        self.dev._loop_body()                 # lần đầu: POST hỏng → link down + xếp hàng
        depth_after_first = self.dev._queue.size()
        self.assertGreaterEqual(depth_after_first, 1)
        self.assertFalse(self.dev._link.is_up())

        calls_before = len(self.h.http.ingest_calls)
        self.dev._last_ingest_ms = -10**9
        self.dev._loop_body()                 # lần sau: biết đang offline → lấy mẫu + xếp hàng
        self.assertGreater(self.dev._queue.size(), depth_after_first,
                           "mất mạng vẫn phải lấy mẫu, không được mất trắng dữ liệu")
        # Vẫn có thể có 1 request đẩy bù (đóng vai trò dò mạng) nhưng không được nện backend.
        self.assertLessEqual(len(self.h.http.ingest_calls) - calls_before, 1)

    def test_queue_flush_recovers_link_after_outage(self):
        self.h.http.ingest_response = net_error()
        self.dev._last_ingest_ms = -10**9
        self.dev._loop_body()
        self.assertFalse(self.dev._link.is_up())

        self.h.http.ingest_response = ok(201)
        self.dev._backoff.reset()
        self.dev._try_flush_queue(now=10**9)
        self.assertTrue(self.dev._link.is_up(), "đẩy bù phải đóng vai trò dò lại mạng")

    def test_flush_sends_one_batch_per_tick(self):
        self.h.http.ingest_response = ok(503)
        for _ in range(3):
            self.dev._ingest_once()
        self.assertEqual(self.dev._queue.size(), 3)

        self.h.http.ingest_response = ok(201)
        before = len(self.h.http.ingest_calls)
        self.dev._backoff.reset()
        self.dev._try_flush_queue(now=10**9)
        self.assertEqual(len(self.h.http.ingest_calls) - before, 1)
        self.assertEqual(self.dev._queue.size(), 2)

    def test_flush_drops_permanent_failure_instead_of_blocking_queue(self):
        self.h.http.ingest_response = ok(503)
        self.dev._ingest_once()
        self.dev._ingest_once()
        self.assertEqual(self.dev._queue.size(), 2)

        self.h.http.ingest_response = ok(400)
        self.dev._backoff.reset()
        self.dev._try_flush_queue(now=10**9)
        self.assertEqual(self.dev._queue.size(), 1, "batch 4xx phải bị BỎ, không chặn hàng đợi")
        self.assertEqual(self.dev.state.dropped_batches, 1)

    def test_flush_is_gated_by_backoff(self):
        self.h.http.ingest_response = ok(503)
        self.dev._ingest_once()
        calls = len(self.h.http.ingest_calls)
        self.dev._try_flush_queue(now=0)      # backoff chưa hết
        self.assertEqual(len(self.h.http.ingest_calls), calls)

    # ── GH-748 ───────────────────────────────────────────────────────────────────────────
    def test_partial_ingest_is_detected_and_counted(self):
        self.h.http.ingest_response = ok(
            201, json_body={"isSuccess": True,
                            "data": {"totalReceived": 3, "inserted": 1, "skipped": 2}})
        self.assertTrue(self.dev._ingest_once())
        self.assertEqual(self.dev.state.partial_ingests, 1)

    def test_full_ingest_raises_no_partial_flag(self):
        self.h.http.ingest_response = ok(
            201, json_body={"isSuccess": True,
                            "data": {"totalReceived": 3, "inserted": 3, "skipped": 0}})
        self.assertTrue(self.dev._ingest_once())
        self.assertEqual(self.dev.state.partial_ingests, 0)

    # ── GH-740 ───────────────────────────────────────────────────────────────────────────
    def test_partial_mqtt_publish_does_not_resend_over_https(self):
        """Nhóm đã vào backend qua MQTT mà gửi lại qua HTTPS là GHI TRÙNG — khoá idempotency
        của đường HTTPS không cứu được vì bản ghi kia vào bằng đường khác."""
        from tests.fakes import make_device_cfg
        h = DeviceHarness(contract=CONTRACT_IOT2, device_cfg=make_device_cfg(batteries=3))
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        dev._mqtt = FakeMqtt(connected=True, fail_after=1)   # pin đầu OK, pin sau hỏng
        h.http.ingest_response = ok(201)

        self.assertFalse(dev._ingest_once() is None)
        published = [serial for serial, _ in dev._mqtt.telemetry]
        self.assertEqual(published, ["BAT-T-001"])

        sent_serials = {item["batteryAssetSerial"] for item in h.http.last_ingest_items()}
        self.assertNotIn("BAT-T-001", sent_serials, "pin đã publish qua MQTT KHÔNG được gửi lại")
        self.assertEqual(sent_serials, {"BAT-T-002", "BAT-T-003"})

    def test_full_mqtt_publish_skips_https_entirely(self):
        dev = self.dev
        dev._mqtt = FakeMqtt(connected=True)
        self.assertTrue(dev._ingest_once())
        self.assertEqual(self.h.http.ingest_calls, [])

    def test_mqtt_skipped_after_consecutive_failures(self):
        """S4-FW-06 — streak fail ≥ 3 thì bỏ MQTT, chạy HTTPS cho tới khi nối lại."""
        dev = self.dev
        mqtt = FakeMqtt(connected=True, fail_after=0)
        dev._mqtt = mqtt
        self.h.http.ingest_response = ok(201)
        for _ in range(3):
            dev._ingest_once()
        self.assertGreaterEqual(mqtt.consecutive_fail_count, 3)

        calls_before = len(self.h.http.ingest_calls)
        dev._ingest_once()
        self.assertEqual(len(self.h.http.ingest_calls) - calls_before, 1)
        self.assertEqual(len(mqtt.telemetry), 0)

    def test_mqtt_publishes_one_message_per_battery(self):
        from tests.fakes import make_device_cfg
        h = DeviceHarness(contract=CONTRACT_IOT2, device_cfg=make_device_cfg(batteries=2))
        self.addCleanup(h.close)
        dev = h.device
        dev._provision_done = True
        dev._mqtt = FakeMqtt(connected=True)
        dev._ingest_once()
        self.assertEqual([s for s, _ in dev._mqtt.telemetry], ["BAT-T-001", "BAT-T-002"])
        for _, payload in dev._mqtt.telemetry:
            self.assertIn("items", payload)
            serials = {i["batteryAssetSerial"] for i in payload["items"]}
            self.assertEqual(len(serials), 1, "mỗi message chỉ chứa reading của MỘT pin")

    # ── đèn ──────────────────────────────────────────────────────────────────────────────
    def test_led_reports_network_state_above_queue_state(self):
        """IOT3-54 — chưa có mạng thì hàng đợi đầy là HỆ QUẢ, đèn phải nói cái gốc."""
        dev = self.dev
        self.h.http.ingest_response = net_error()
        dev._ingest_once()
        dev._update_led()
        self.assertIn(dev.state.led, (LedState.WIFI_SEARCHING, LedState.RECOVERY))

        self.h.http.ingest_response = ok(201)
        dev._backoff.reset()
        dev._try_flush_queue(now=10**9)
        dev._backend_acknowledged = True
        dev._update_led()
        self.assertIs(dev.state.led, LedState.ONLINE)

    def test_led_queued_when_backlog_exists(self):
        dev = self.dev
        dev._backend_acknowledged = True
        self.h.http.ingest_response = ok(503)
        dev._ingest_once()
        dev._link.note_result(200)
        dev._update_led()
        self.assertIs(dev.state.led, LedState.QUEUED)

    def test_led_red_when_provisioned_but_backend_not_acknowledging(self):
        dev = self.dev
        dev._link.note_result(200)
        dev._backend_acknowledged = False
        dev._update_led()
        self.assertIs(dev.state.led, LedState.OFFLINE)


if __name__ == "__main__":
    unittest.main()
