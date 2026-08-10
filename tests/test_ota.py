"""OTA — vòng đời đầy đủ, kể cả verify-mode và rollback.

Simulator không ghi partition thật, nhưng TOÀN BỘ phần tương tác backend phải giống thiết bị
thật: đúng endpoint, đúng thứ tự status, có tải artifact, có xác minh SHA-256, có chống re-OTA
loop, và trạng thái sống qua "reboot" (state bền vững).
"""
from __future__ import annotations

import hashlib
import unittest

from src import nvs as nvskeys
from src.config import CONTRACT_IOT2
from src.ota import (FW_STATUS_DOWNLOADING, FW_STATUS_FAILED, FW_STATUS_INSTALLING,
                     FW_STATUS_ROLLED_BACK, FW_STATUS_SKIPPED, FW_STATUS_SUCCESS,
                     OtaManager, ota_sha256_equal, ota_should_update, parse_firmware_check)
from src.policy import OtaCheckDecision, OtaCheckInputs, decide_ota_check

from tests.fakes import DeviceHarness, FakeHttp, make_backend, ok

ARTIFACT = b"\x00firmware-binary-bytes\xff" * 32
ARTIFACT_SHA = hashlib.sha256(ARTIFACT).hexdigest()


def _offer(target: str = "1.2.0", sha: str = ARTIFACT_SHA, log_id: str = "log-42",
           url: str = "https://cdn.example/fw.bin", size: int = len(ARTIFACT),
           available: bool = True) -> dict:
    return {"isSuccess": True, "data": {
        "updateAvailable": available, "targetVersion": target,
        "downloadUrl": url, "sha256Checksum": sha,
        "updateLogId": log_id, "artifactSizeBytes": size,
    }}


class DecisionTest(unittest.TestCase):
    def test_should_update_uses_absolute_string_compare(self):
        self.assertTrue(ota_should_update(True, "1.0.0", "1.1.0"))
        self.assertFalse(ota_should_update(True, "1.1.0", "1.1.0"))
        self.assertFalse(ota_should_update(False, "1.0.0", "1.1.0"))
        self.assertFalse(ota_should_update(True, "1.0.0", ""))
        self.assertTrue(ota_should_update(True, None, "1.0.0"))

    def test_sha_compare_ignores_case_and_spaces(self):
        self.assertTrue(ota_sha256_equal("AB" * 32, ("ab" * 32) + " "))
        self.assertFalse(ota_sha256_equal("ab" * 32, "cd" * 32))
        self.assertFalse(ota_sha256_equal("", "ab" * 32))

    def test_parse_accepts_both_field_aliases(self):
        offer = parse_firmware_check({"hasUpdate": True, "targetVersion": "2.0.0",
                                      "artifactUrl": "http://x/fw.bin",
                                      "sha256Checksum": "ab" * 32,
                                      "updateLogId": "log-1", "artifactSizeBytes": 1234})
        self.assertTrue(offer.has_update)
        self.assertEqual(offer.download_url, "http://x/fw.bin")
        self.assertEqual(offer.log_id, "log-1")
        self.assertEqual(offer.size_bytes, 1234)


class CheckPolicyTest(unittest.TestCase):
    """`core::decideOtaCheck` (GH-745)."""

    def test_warmup_blocks_first_check(self):
        d = decide_ota_check(OtaCheckInputs(now_ms=1000, warmup_ms=30000))
        self.assertIs(d, OtaCheckDecision.SKIP_WARMUP)

    def test_runs_after_warmup(self):
        d = decide_ota_check(OtaCheckInputs(now_ms=30001, warmup_ms=30000))
        self.assertIs(d, OtaCheckDecision.RUN)

    def test_forced_beats_warmup(self):
        d = decide_ota_check(OtaCheckInputs(now_ms=100, forced=True, warmup_ms=30000))
        self.assertIs(d, OtaCheckDecision.RUN)

    def test_forced_never_beats_verifying(self):
        """Đang xác minh bản vừa flash mà tải tiếp bản mới là mất luôn đường lùi."""
        d = decide_ota_check(OtaCheckInputs(now_ms=10**7, forced=True, verifying=True))
        self.assertIs(d, OtaCheckDecision.SKIP_VERIFYING)

    def test_disabled_wins_over_everything(self):
        d = decide_ota_check(OtaCheckInputs(enabled=False, forced=True, now_ms=10**7))
        self.assertIs(d, OtaCheckDecision.SKIP_DISABLED)

    def test_interval_gate(self):
        self.assertIs(decide_ota_check(OtaCheckInputs(last_check_ms=1000, now_ms=2000,
                                                      interval_ms=3600000)),
                      OtaCheckDecision.SKIP_TOO_SOON)
        self.assertIs(decide_ota_check(OtaCheckInputs(last_check_ms=1000, now_ms=1000 + 3600000,
                                                      interval_ms=3600000)),
                      OtaCheckDecision.RUN)


class _OtaFixture:
    """Dựng `OtaManager` trên state thật + backend giả."""

    def __init__(self, testcase, current_version: str = "1.0.0"):
        import tempfile
        from pathlib import Path
        from src.nvs import NvsStore

        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.nvs = NvsStore(Path(tmp.name) / "dev.nvs.json")
        self.http = FakeHttp()
        self.http.artifact = ARTIFACT
        self.version = current_version
        self.applied: list[str] = []
        self.ota = OtaManager(
            http=self.http, store=self.nvs, device_code="esp32-sim-001",
            current_version_getter=lambda: self.version,
            apply_version=self._apply, enabled=True, health_timeout_ms=120000)

    def _apply(self, v: str) -> None:
        self.version = v
        self.applied.append(v)

    def reboot(self) -> OtaManager:
        """Dựng lại manager trên CÙNG state — tương đương thiết bị khởi động lại."""
        self.ota = OtaManager(
            http=self.http, store=self.nvs, device_code="esp32-sim-001",
            current_version_getter=lambda: self.version,
            apply_version=self._apply, enabled=True, health_timeout_ms=120000)
        self.ota.begin()
        return self.ota


class LifecycleTest(unittest.TestCase):
    def test_happy_path_downloads_verifies_and_confirms(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())

        result = f.ota.check_and_apply()
        self.assertTrue(result.updated)
        self.assertEqual(f.version, "1.2.0")
        self.assertEqual(f.http.download_calls, ["https://cdn.example/fw.bin"])
        self.assertEqual(f.http.statuses_logged(),
                         [FW_STATUS_DOWNLOADING, FW_STATUS_INSTALLING])
        self.assertTrue(f.ota.verify_mode)

        # Xác minh: mạng + giờ + broker đều tốt → Success.
        f.ota.tick(link_up=True, time_synced=True, broker_up=True)
        self.assertFalse(f.ota.verify_mode)
        self.assertEqual(f.http.statuses_logged()[-1], FW_STATUS_SUCCESS)
        self.assertEqual(f.ota.update_ok_count, 1)

    def test_verify_succeeds_over_https_when_broker_down(self):
        """Không được gắn cứng vào broker: bản firmware TỐT sẽ bị rollback oan khi broker tắt."""
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())
        f.ota.check_and_apply()
        f.ota.tick(link_up=True, time_synced=True, broker_up=False)
        self.assertFalse(f.ota.verify_mode)
        self.assertEqual(f.http.statuses_logged()[-1], FW_STATUS_SUCCESS)

    def test_no_update_when_versions_match(self):
        f = _OtaFixture(self, current_version="1.2.0")
        f.http.firmware_check_response = ok(200, json_body=_offer(target="1.2.0"))
        result = f.ota.check_and_apply()
        self.assertFalse(result.updated)
        self.assertEqual(f.http.update_log_calls, [])
        self.assertEqual(f.version, "1.2.0")

    def test_checksum_mismatch_reports_failed_and_keeps_running_version(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer(sha="00" * 32))
        result = f.ota.check_and_apply()
        self.assertFalse(result.updated)
        self.assertEqual(f.version, "1.0.0", "checksum sai thì TUYỆT ĐỐI không được đổi version")
        statuses = f.http.statuses_logged()
        self.assertIn(FW_STATUS_FAILED, statuses)
        reason = [c[3] for c in f.http.update_log_calls if c[1] == FW_STATUS_FAILED][0]
        self.assertIn("checksum", reason)
        self.assertFalse(f.ota.verify_mode)

    def test_download_error_reports_failed_and_retries_next_cycle(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())
        f.http.artifact_error = "download http 500"
        f.ota.check_and_apply()
        self.assertIn(FW_STATUS_FAILED, f.http.statuses_logged())
        # KHÔNG mark bad → lần sau vẫn thử lại (tự lành nếu lỗi do mạng).
        self.assertEqual(f.nvs.get_string(nvskeys.KEY_OTA_BAD_VER, ""), "")

    def test_offer_without_sha_or_log_id_is_skipped(self):
        for bad in (_offer(sha=""), _offer(log_id=""), _offer(url="")):
            f = _OtaFixture(self)
            f.http.firmware_check_response = ok(200, json_body=bad)
            result = f.ota.check_and_apply()
            self.assertFalse(result.updated)
            self.assertEqual(f.version, "1.0.0")
            self.assertEqual(f.http.update_log_calls, [])

    def test_firmware_check_error_is_not_treated_as_no_update(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(500)
        result = f.ota.check_and_apply()
        self.assertFalse(result.checked)
        self.assertFalse(result.updated)


class RollbackTest(unittest.TestCase):
    def test_health_timeout_rolls_back_and_reports(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())
        f.ota.check_and_apply()
        self.assertEqual(f.version, "1.2.0")

        f.ota._verify_deadline_ms = 0            # ép quá hạn
        f.ota.tick(link_up=False, time_synced=True, broker_up=False)

        self.assertEqual(f.version, "1.0.0", "phải quay lại bản cũ")
        self.assertEqual(f.http.statuses_logged()[-1], FW_STATUS_ROLLED_BACK)
        self.assertEqual(f.ota.rollback_count, 1)
        self.assertTrue(f.nvs.get_bool(nvskeys.KEY_OTA_ROLLED_BACK))

    def test_rollback_is_reported_after_reboot_when_network_returns(self):
        """Nguyên nhân rollback thường là MẤT MẠNG → không báo được ngay lúc đó."""
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())
        f.ota.check_and_apply()
        f.ota._verify_deadline_ms = 0
        f.http.update_log_response = ok(0)       # backend không với tới lúc rollback
        f.ota.tick(link_up=False, time_synced=True, broker_up=False)
        self.assertTrue(f.nvs.get_bool(nvskeys.KEY_OTA_ROLLED_BACK))

        f.http.update_log_response = ok(200)
        ota = f.reboot()
        self.assertTrue(ota.report_rollback)
        ota.tick(link_up=True, time_synced=True, broker_up=True)
        self.assertFalse(ota.report_rollback)
        self.assertFalse(f.nvs.get_bool(nvskeys.KEY_OTA_ROLLED_BACK))
        self.assertEqual(f.http.statuses_logged()[-1], FW_STATUS_ROLLED_BACK)

    def test_boot_loop_triggers_rollback_and_marks_version_bad(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())
        f.ota.check_and_apply()

        # Mô phỏng bản mới crash sớm: khởi động lại nhiều lần mà chưa bao giờ confirm.
        for _ in range(5):
            ota = f.reboot()
            if not ota.verify_mode:
                break
        self.assertEqual(f.version, "1.0.0")
        self.assertEqual(f.nvs.get_string(nvskeys.KEY_OTA_BAD_VER, ""), "1.2.0")

    def test_known_bad_version_is_skipped_with_status_six(self):
        f = _OtaFixture(self)
        f.nvs.put_string(nvskeys.KEY_OTA_BAD_VER, "1.2.0")
        f.http.firmware_check_response = ok(200, json_body=_offer(target="1.2.0"))
        result = f.ota.check_and_apply()
        self.assertFalse(result.updated)
        self.assertEqual(f.version, "1.0.0")
        self.assertEqual(f.http.statuses_logged(), [FW_STATUS_SKIPPED])

    def test_new_version_after_bad_one_is_still_installed(self):
        f = _OtaFixture(self)
        f.nvs.put_string(nvskeys.KEY_OTA_BAD_VER, "1.2.0")
        f.http.firmware_check_response = ok(200, json_body=_offer(target="1.3.0"))
        result = f.ota.check_and_apply()
        self.assertTrue(result.updated)
        self.assertEqual(f.version, "1.3.0")

    def test_transient_rollbacks_mark_bad_only_after_three_strikes(self):
        """Mất mạng thoáng qua KHÔNG được chặn version; binary hỏng thật thì phải chặn."""
        f = _OtaFixture(self)
        for i in range(3):
            f.version = "1.0.0"
            f.http.firmware_check_response = ok(200, json_body=_offer())
            f.ota._last_check_ms = 0
            f.ota.verify_mode = False
            f.ota.check_and_apply()
            f.ota._verify_deadline_ms = 0
            f.ota.tick(link_up=False, time_synced=True, broker_up=False)
            if i < 2:
                self.assertEqual(f.nvs.get_string(nvskeys.KEY_OTA_BAD_VER, ""), "",
                                 f"chưa đủ 3 lần mà đã chặn (lần {i + 1})")
        self.assertEqual(f.nvs.get_string(nvskeys.KEY_OTA_BAD_VER, ""), "1.2.0")

    def test_version_mismatch_after_flash_reports_failed(self):
        f = _OtaFixture(self)
        f.http.firmware_check_response = ok(200, json_body=_offer())
        f.ota.check_and_apply()
        f.version = "9.9.9"                      # bootloader rơi về bản khác
        f.ota.tick(link_up=True, time_synced=True, broker_up=True)
        self.assertFalse(f.ota.verify_mode)
        self.assertIn(FW_STATUS_FAILED, f.http.statuses_logged())
        self.assertEqual(f.nvs.get_string(nvskeys.KEY_OTA_BAD_VER, ""), "1.2.0")


class TriggerOtaTest(unittest.TestCase):
    def test_request_check_accepted(self):
        f = _OtaFixture(self)
        self.assertTrue(f.ota.request_check())
        self.assertEqual(f.ota.last_reject_reason(), "")

    def test_request_check_rejected_while_verifying(self):
        f = _OtaFixture(self)
        f.ota.verify_mode = True
        self.assertFalse(f.ota.request_check())
        self.assertIn("verifying", f.ota.last_reject_reason())

    def test_request_check_rejected_when_disabled(self):
        f = _OtaFixture(self)
        f.ota.enabled = False
        self.assertFalse(f.ota.request_check())
        self.assertIn("disabled", f.ota.last_reject_reason())


class DeviceOtaIntegrationTest(unittest.TestCase):
    def test_running_version_survives_restart(self):
        """Không lưu version đang chạy thì backend sẽ offer đúng bản đó mãi mãi."""
        h = DeviceHarness(contract=CONTRACT_IOT2)
        self.addCleanup(h.close)
        h.http.artifact = ARTIFACT
        h.http.firmware_check_response = ok(200, json_body=_offer())
        h.device._ota.check_and_apply()
        self.assertEqual(h.device.cfg.firmware_version, "1.2.0")
        self.assertEqual(h.device.state.firmware_version, "1.2.0")

        from src.device import SimulatedDevice
        from tests.fakes import make_device_cfg, make_mqtt_cfg
        http2 = FakeHttp()
        dev2 = SimulatedDevice(make_device_cfg(), make_backend(), make_mqtt_cfg(),
                               queue_dir=h.root / "queue", state_dir=h.root / "state",
                               http=http2)
        self.assertEqual(dev2.cfg.firmware_version, "1.2.0")

    def test_ota_disabled_on_legacy_contract(self):
        """Contract Sprint 1 của backend không có endpoint firmware-check."""
        from src.config import CONTRACT_CURRENT
        h = DeviceHarness(contract=CONTRACT_CURRENT)
        self.addCleanup(h.close)
        self.assertFalse(h.device._ota.enabled)
        h.device._ota.tick(link_up=True, time_synced=True, broker_up=True)
        self.assertEqual(h.http.firmware_check_calls, [])


if __name__ == "__main__":
    unittest.main()
