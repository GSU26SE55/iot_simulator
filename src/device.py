"""SimulatedDevice — một node ESP32 mô phỏng, mirror `firmware-esp32/src/main.cpp`.

Vòng đời khớp `setup()` + `appLoopBody()` của firmware:

    setup():
      state(NVS) → identity → mqttcfg → bảng pin → link → HTTP → cấu hình đã provision
      → OTA(begin: verify/rollback) → nguồn BMS → cảm biến → heartbeat → hàng đợi → MQTT

    loop():
      mqtt tick → kiểm tra sức khoẻ credential (IOT3-44) → ensureProvisioned
      → ingest theo pollingInterval:
            đọc BMS đa nguồn → gom theo pin
            MQTT trước: publish <prefix>/<serial>/telemetry cho từng pin
            fallback HTTPS CHỈ phần MQTT chưa đẩy được (GH-740)
            hỏng tạm thời → xếp hàng + backoff; hỏng vĩnh viễn (4xx) → BỎ
      → flush hàng đợi (1 batch/vòng, có backoff)
      → OTA tick (verify-mode chạy cả khi mất mạng)
      → heartbeat theo heartbeatInterval (HTTP POST)
      → SHT31 ambient theo chu kỳ 60s
      → MQ-2 / rò nước / (mô phỏng) cháy — chạy VÔ ĐIỀU KIỆN, kể cả offline (GH-736)
      → cập nhật đèn + log thống kê

Toàn bộ điểm khác biệt CÓ CHỦ Ý so với firmware đều được ghi rõ tại chỗ và trong README.
"""
from __future__ import annotations

import logging
import random
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .backoff import Backoff, is_transient_failure
from .battery_map import BatteryMap, BatteryMapEntry
from .bms import MockBattery
from .cmd import CommandKind, build_ack, is_valid_polling_seconds, parse_command_payload
from .config import (CONTRACT_IOT2, BackendConfig, BatteryConfig, DeviceConfig,
                     MqttConfig)
from .heartbeat import Heartbeat
from .http_client import IotHttpClient
from .ingest_result import parse_ingest_result
from .led import LedState
from .link import LinkPhase, LinkState
from . import nvs as nvskeys
from .mqtt_client import IotMqttClient, MqttOptions
from .mqtt_config import MqttRuntimeConfig
from .net_rules import (MAX_API_KEY_CHARS, MAX_DEVICE_CODE_CHARS,
                        describe_identity_error, identity_is_valid,
                        validate_identity_field)
from .nvs import NvsStore
from .ota import OtaManager
from .payload import (build_batch_payload, filter_out_published, group_by_serial)
from .policy import IngestAction, ingest_action, should_reprovision_on_auth_failure
from .provision import PROVISION_RETRY_MS, ProvisionRunner
from .queue import LocalQueue
from .sensors.ambient import Sht31Sensor
from .sensors.environmental import EnvironmentalIncidentReporter
from .sensors.fire_watch import FireWatch
from .sensors.mq2 import Mq2Sensor
from .sensors.redundant import make_ds18b20_reading, make_ina226_reading
from .sensors.water_leak import WaterLeakSensor
from .timeutil import iso_now, iso_now_minus, monotonic_ms

log = logging.getLogger("iot-sim.device")

# `MQTT_PUBLISH_FAIL_THRESHOLD` (include/config.h) — sau N lần publish telemetry FAIL liên tiếp thì
# bỏ MQTT, chuyển HTTPS cho tới khi kết nối lại làm sạch bộ đếm (S4-FW-06).
MQTT_PUBLISH_FAIL_THRESHOLD = 3

# main.cpp::logStatsPeriodic — 60s một lần.
STATS_INTERVAL_MS = 60000

# Nhịp vòng lặp. Firmware chạy `vTaskDelay(10ms)`; ở đây 100ms là đủ mịn cho cảm biến nhanh nhất
# (rò nước 500ms) mà không đốt CPU khi chạy nhiều thiết bị trong một tiến trình.
LOOP_TICK_S = 0.1


class PostOutcome(Enum):
    """`PostOutcome` của main.cpp."""

    SUCCESS = 1
    TRANSIENT_FAILURE = 2   # thử lại có backoff (5xx, lỗi mạng, 408, 429)
    PERMANENT_FAILURE = 3   # BỎ khỏi hàng đợi (4xx — dữ liệu sai)


@dataclass
class DeviceState:
    """Ảnh chụp trạng thái cho dashboard + log."""

    device_code: str
    status: str = "booting"
    last_seen: str = ""
    scenario: str = "normal"
    sent_batches: int = 0
    failed_batches: int = 0
    queued_batches: int = 0
    dropped_batches: int = 0
    partial_ingests: int = 0
    ambient_sent: int = 0
    incidents_sent: int = 0
    last_error: str = ""
    backoff_s: float = 0.0
    last_voltage: float = 0.0
    last_temperature: float = 0.0
    last_soc: float = 0.0
    led: LedState = LedState.OFF
    firmware_version: str = ""
    ota_checks: int = 0
    ota_updates: int = 0
    ota_rollbacks: int = 0
    ota_failed: int = 0
    battery_count: int = 0
    battery_map_source: str = "seed"
    mqtt_connected: bool = False
    mqtt_publish_ok: int = 0
    mqtt_publish_fail: int = 0
    mqtt_auth_fail: int = 0
    heartbeat_ok: int = 0
    heartbeat_fail: int = 0
    cmd_received: int = 0
    cmd_ack_ok: int = 0
    cmd_ack_failed: int = 0
    cmd_unknown: int = 0
    reprovision_count: int = 0
    link_phase: str = "connecting"
    warnings: list[str] = field(default_factory=list)


class SimulatedDevice:
    def __init__(self, dev_cfg: DeviceConfig, backend_cfg: BackendConfig, mqtt_cfg: MqttConfig,
                 queue_dir: Path, state_dir: Path | None = None, persist_state: bool = True,
                 http=None, battery_catalog: list[BatteryConfig] | None = None):
        """`http` là mối nối để test tiêm client giả — chạy thật thì để None.

        `battery_catalog` là danh mục pin dùng chung, tra khi backend giao một serial không nằm
        trong `devices[].batteries` (xem `_rebuild_batteries`).
        """
        self.cfg = dev_cfg
        self.backend_cfg = backend_cfg
        self.mqtt_cfg = mqtt_cfg
        self.production = backend_cfg.contract_version == CONTRACT_IOT2

        queue_dir = Path(queue_dir)
        state_dir = Path(state_dir) if state_dir is not None else queue_dir.parent / "state"

        self.state = DeviceState(device_code=dev_cfg.device_code, scenario=dev_cfg.scenario)

        # ── 1. State bền vững (tương đương NVS) ────────────────────────────────────────────
        self._nvs = NvsStore(state_dir / f"{dev_cfg.device_code}.nvs.json", enabled=persist_state)

        # ── 2. Identity + kiểm giá trị TRƯỚC khi dùng ─────────────────────────────────────
        self._identity_ready = self._check_identity()

        # ── 3. Version firmware đang chạy (sống qua các lần chạy để OTA có nghĩa) ─────────
        running = self._nvs.get_string(nvskeys.KEY_RUNNING_FW, "")
        if running:
            self.cfg.firmware_version = running
        self.state.firmware_version = self.cfg.firmware_version

        # ── 4. Cấu hình MQTT runtime (seed = đường lui, provision = nguồn chân lý) ────────
        self._mqtt_runtime = MqttRuntimeConfig(
            device_code=dev_cfg.device_code, nvs=self._nvs,
            fallback_host=mqtt_cfg.host, fallback_port=mqtt_cfg.port,
            fallback_tls=mqtt_cfg.tls, fallback_user=mqtt_cfg.username,
            fallback_pass=mqtt_cfg.password,
            fallback_prefix="")
        self._mqtt_runtime._fb["prefix"] = ""   # luôn suy từ deviceCode + gốc trong seed
        self._topic_root = mqtt_cfg.topic_prefix or "solar"
        self._mqtt_runtime.begin()
        if not self._mqtt_runtime.topic_prefix():
            self._mqtt_runtime._prefix = ""
        self._ensure_prefix_root()

        # ── 5. Bảng ánh xạ pin ────────────────────────────────────────────────────────────
        fallback_entries = [
            BatteryMapEntry(serial=b.serial, unit_id=b.unit_id, sensor_source_code="primary")
            for b in dev_cfg.batteries
        ]
        self._batmap = BatteryMap(dev_cfg.device_code, self._nvs, fallback_entries)
        self._batmap.begin()
        self._seed_batteries = {b.serial: b for b in (battery_catalog or [])}
        self._seed_batteries.update({b.serial: b for b in dev_cfg.batteries})
        self._batteries: dict[str, MockBattery] = {}
        self._rebuild_batteries()

        # ── 6. Đường lên backend + HTTP ───────────────────────────────────────────────────
        self._link = LinkState(identity_ready=self._identity_ready)
        if http is not None:
            self._http = http
            if hasattr(http, "on_result_hook"):
                http.on_result_hook = self._link.note_result
        else:
            self._http = IotHttpClient(
                base_url=backend_cfg.base_url,
                device_code=dev_cfg.device_code,
                api_key=dev_cfg.api_key,
                tls_verify=backend_cfg.tls_verify,
                firmware_version=self.cfg.firmware_version,
                contract_version=backend_cfg.contract_version,
                timeout_s=backend_cfg.http_timeout_s,
                on_result=self._link.note_result,
            )

        # ── 7. Provision ──────────────────────────────────────────────────────────────────
        self._provision = ProvisionRunner(
            device_code=dev_cfg.device_code, http=self._http, store=self._nvs,
            mqtt_cfg=self._mqtt_runtime, battery_map=self._batmap,
            apply_battery_map=backend_cfg.apply_battery_map)
        self._prov_cfg = self._provision.load_provisioned()
        if self._prov_cfg.provisioned:
            # Cấu hình do backend cấp GHI ĐÈ seed — đúng như firmware nạp từ NVS lúc boot.
            self.backend_cfg.ingest_interval_s = self._prov_cfg.polling_interval_s
            self.backend_cfg.heartbeat_interval_s = self._prov_cfg.heartbeat_interval_s
            if self._prov_cfg.site_id:
                self.cfg.site_id_guid = self._prov_cfg.site_id
            self.cfg.ntp_server = self._prov_cfg.ntp_server
        self._provision_done = self._prov_cfg.provisioned or not self.production
        self._provision_next_attempt_ms = 0
        self._backend_acknowledged = False

        # ── 8. Hàng đợi ───────────────────────────────────────────────────────────────────
        self._queue = LocalQueue(queue_dir / f"{dev_cfg.device_code}.jsonl")
        self.state.queued_batches = self._queue.size()

        # ── 9. OTA (gọi SỚM — máy trạng thái verify/rollback phải chạy trước mọi thứ khác) ─
        self._ota = OtaManager(
            http=self._http, store=self._nvs, device_code=dev_cfg.device_code,
            current_version_getter=lambda: self.cfg.firmware_version,
            apply_version=self._apply_firmware_version,
            enabled=backend_cfg.ota_enabled and self.production,
            check_interval_ms=int(backend_cfg.ota_check_interval_s * 1000),
            health_timeout_ms=int(backend_cfg.ota_health_timeout_s * 1000),
            max_boot_attempts=backend_cfg.ota_max_boot_attempts,
            max_version_fails=backend_cfg.ota_max_version_fails,
            warmup_ms=int(backend_cfg.ota_warmup_s * 1000),
            download_timeout_s=backend_cfg.ota_download_timeout_s,
        )
        self._ota.begin()

        # ── 10. Cảm biến ─────────────────────────────────────────────────────────────────
        tuning = dev_cfg.sensor_tuning
        self._env_reporter = EnvironmentalIncidentReporter(self._http, dev_cfg.device_code)
        self._sht31 = Sht31Sensor(
            self._http, dev_cfg.device_code, iso_now=self._iso_now,
            enabled=dev_cfg.sensors.sht31 and self.production,
            poll_interval_ms=int(tuning.sht31_poll_interval_s * 1000))
        self._mq2 = Mq2Sensor(
            self._env_reporter, iso_now_minus=self._iso_now_minus,
            enabled=dev_cfg.sensors.mq2 and self.production,
            threshold_raw=tuning.mq2_threshold_raw,
            warmup_ms=int(tuning.mq2_warmup_s * 1000),
            poll_interval_ms=int(tuning.mq2_poll_interval_s * 1000),
            rearm_cooldown_ms=int(tuning.mq2_rearm_cooldown_s * 1000))
        self._water = WaterLeakSensor(
            self._env_reporter, iso_now_minus=self._iso_now_minus,
            enabled=dev_cfg.sensors.water_leak and self.production,
            poll_interval_ms=int(tuning.water_leak_poll_interval_s * 1000),
            rearm_cooldown_ms=int(tuning.water_leak_rearm_cooldown_s * 1000))
        self._fire = FireWatch(
            self._env_reporter, iso_now_minus=self._iso_now_minus,
            enabled=dev_cfg.sensors.mq2 and self.production)

        # ── 11. Heartbeat ────────────────────────────────────────────────────────────────
        self._boot_ms = monotonic_ms()
        self._heartbeat = Heartbeat(
            http=self._http,
            firmware_version_getter=lambda: self.cfg.firmware_version,
            queue_depth_getter=self._queue.size,
            rssi_getter=self._link.rssi_dbm,
            iso_now=self._iso_now,
            boot_ms=self._boot_ms,
            interval_ms=self.backend_cfg.heartbeat_interval_s * 1000)

        # ── 12. MQTT ─────────────────────────────────────────────────────────────────────
        self._mqtt: IotMqttClient | None = None

        # ── 13. Trạng thái vòng lặp ──────────────────────────────────────────────────────
        self._backoff = Backoff(base_ms=int(backend_cfg.retry_base_s * 1000),
                                max_ms=int(backend_cfg.retry_max_s * 1000),
                                jitter_pct=backend_cfg.retry_jitter_pct / 100.0)
        self._stop = threading.Event()
        self._last_ingest_ms = 0
        self._last_stats_ms = 0
        self._offline_scenario_at_ms = 0
        self._last_reprovision_ms = 0
        self._ever_reprovisioned = False
        self._t_global = 0.0
        self._thread = threading.Thread(target=self._run, name=f"sim-{dev_cfg.device_code}",
                                        daemon=True)

        self.state.battery_count = len(self._batteries)
        self.state.battery_map_source = "nvs" if self._batmap.is_from_nvs() else "seed"
        self.state.status = "provisioning" if not self._provision_done else "connecting"

    # ══════════════════════════════ công khai ══════════════════════════════════════════════
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._mqtt:
            self._mqtt.disconnect()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    # ══════════════════════════════ khởi tạo phụ ═══════════════════════════════════════════
    def _check_identity(self) -> bool:
        """`identity::identityBegin` + `core::validateIdentityField` (GH-749).

        Giá trị sai (rỗng, quá dài, có khoảng trắng/CR/LF) bị TỪ CHỐI chứ không cắt cụt:
        `apiKey` đi thẳng vào header `X-Api-Key`, một ký tự CR/LF là tiêm header HTTP; `deviceCode`
        còn được ghép vào topic MQTT.
        """
        ok = True
        err = validate_identity_field(self.cfg.device_code, MAX_DEVICE_CODE_CHARS)
        if not identity_is_valid(self.cfg.device_code, MAX_DEVICE_CODE_CHARS):
            msg = f"deviceCode không hợp lệ — {describe_identity_error(err)}"
            log.error("[%s] %s", self.cfg.device_code, msg)
            self.state.warnings.append(msg)
            ok = False
        err = validate_identity_field(self.cfg.api_key, MAX_API_KEY_CHARS)
        if not identity_is_valid(self.cfg.api_key, MAX_API_KEY_CHARS):
            msg = f"apiKey không hợp lệ — {describe_identity_error(err)}"
            log.error("[%s] %s", self.cfg.device_code, msg)
            self.state.warnings.append(msg)
            ok = False
        return ok

    def _ensure_prefix_root(self) -> None:
        """Suy tiền tố đầy đủ từ gốc trong seed khi state chưa có tiền tố backend cấp."""
        from .net_rules import derive_topic_prefix
        if not self._nvs.get_string(nvskeys.KEY_MQTT_PREFIX, ""):
            self._mqtt_runtime._prefix = derive_topic_prefix(self.cfg.device_code,
                                                             self._topic_root)

    def _rebuild_batteries(self) -> None:
        """Dựng mô hình pin theo BẢNG ÁNH XẠ đang dùng, giữ nguyên trạng thái pin đã có.

        Giữ trạng thái là quan trọng: bảng được áp lại sau mỗi lần provision, mà dựng lại
        `MockBattery` sẽ reset SOC/nhiệt độ về giá trị ban đầu — biểu đồ nhảy bậc mà không có
        nguyên nhân vật lý nào.
        """
        entries = self._batmap.entries()
        new: dict[str, MockBattery] = {}
        for e in entries:
            if e.serial in self._batteries:
                new[e.serial] = self._batteries[e.serial]
                continue
            seed = self._seed_batteries.get(e.serial)
            if seed is None:
                # Backend giao một pin không có trong seed lẫn `battery_catalog` → dựng mô hình
                # mặc định. KHÔNG bỏ qua: bỏ qua nghĩa là thiết bị im lặng không gửi số đo của pin
                # mà nó được giao.
                #
                # ⚠ CẢNH BÁO chứ không phải thông báo: mặc định là LiFePO4 12,8V. Nếu pin đó thực
                # ra là NMC 48V (ngưỡng 42–54,6V) thì 12,8V sẽ bị backend chấm là **Undervoltage
                # Critical** — cảnh báo giả, liên tục, rất khó truy nếu không đọc dòng này.
                # Cách sửa: thêm pin vào `battery_catalog` trong seed.yaml với đúng điện áp danh định.
                log.warning("[%s] pin %s do backend giao nhưng KHÔNG có trong seed/battery_catalog "
                            "— dùng mô hình LiFePO4 12.8V/100Ah mặc định. Nếu pin này khác loại, "
                            "số đo sinh ra sẽ nằm ngoài ngưỡng và tạo CẢNH BÁO GIẢ. "
                            "Thêm nó vào `battery_catalog` để sửa.",
                            self.cfg.device_code, e.serial)
                seed = BatteryConfig(
                    serial=e.serial, unit_id=e.unit_id, nominal_voltage=12.8,
                    nominal_capacity_ah=100, initial_soc=round(random.uniform(45.0, 85.0), 1),
                    initial_soh=95.0, cycle_count=0, chemistry="LiFePO4",
                    battery_asset_id="")
            new[e.serial] = MockBattery(seed)
        self._batteries = new
        self.state.battery_count = len(new)
        self.state.battery_map_source = "nvs" if self._batmap.is_from_nvs() else "seed"

    # ══════════════════════════════ tiện ích thời gian ═════════════════════════════════════
    @property
    def _skew_min(self) -> int:
        """Scenario `clock_skew` — phần mở rộng của simulator để kích hoạt kiểm tra #IoT2-15."""
        return 10 if self.state.scenario == "clock_skew" else 0

    def _iso_now(self) -> str:
        return iso_now(self._skew_min)

    def _iso_now_minus(self, seconds_ago: int) -> str:
        return iso_now_minus(seconds_ago, self._skew_min)

    @property
    def _time_synced(self) -> bool:
        """Tương đương `net::timeIsSynced()`.

        Simulator dùng đồng hồ của máy chủ nên luôn có mốc thời gian hợp lệ; nhánh
        `SkipNoClock` vì thế không bao giờ xảy ra ở đây, nhưng vẫn giữ trong máy quyết định để
        logic khớp firmware từng nhánh một.
        """
        return True

    # ══════════════════════════════ vòng lặp chính ═════════════════════════════════════════
    def _run(self) -> None:
        log.info("[%s] khởi động (contract=%s, scenario=%s, pin=%d, fw=%s)",
                 self.cfg.device_code, self.backend_cfg.contract_version,
                 self.cfg.scenario, len(self._batteries), self.cfg.firmware_version)

        if not self._identity_ready:
            log.error("[%s] identity KHÔNG hợp lệ — thiết bị dừng ở trạng thái chờ cấu hình",
                      self.cfg.device_code)

        self._mq2.begin(monotonic_ms())
        self._water.begin(monotonic_ms())
        self._wire_site_id()
        self._apply_mqtt_runtime()

        while not self._stop.is_set():
            try:
                self._loop_body()
            except Exception:                      # noqa: BLE001 - vòng lặp không được chết
                # Một thiết bị lỗi không được kéo cả dàn simulator xuống. In đủ traceback rồi đi
                # tiếp — đúng tinh thần `appTask` của firmware (task chạy mãi, không thoát).
                log.exception("[%s] lỗi trong vòng lặp — bỏ qua tick này", self.cfg.device_code)
            self._stop.wait(LOOP_TICK_S)

        if self._mqtt:
            self._mqtt.disconnect()
        log.info("[%s] đã dừng", self.cfg.device_code)

    def _loop_body(self) -> None:
        now = monotonic_ms()
        self._t_global = (now - self._boot_ms) / 1000.0

        if not self._identity_ready:
            self._update_led()
            return

        # Scenario `device_offline` — mô phỏng thiết bị mất nguồn/rớt mạng hẳn để demo LWT.
        if self.state.scenario == "device_offline":
            if self._offline_scenario_at_ms == 0:
                self._offline_scenario_at_ms = now + 60000
            if now >= self._offline_scenario_at_ms:
                if self.state.status != "halted":
                    log.warning("[%s] scenario device_offline → dừng hoạt động",
                                self.cfg.device_code)
                    self.state.status = "halted"
                    self.state.led = LedState.OFFLINE
                    if self._mqtt:
                        self._mqtt.disconnect()
                return

        # MQTT: nối lại + xử lý downlink.
        if self._mqtt:
            self._mqtt.tick()

        # IOT3-44 — PHẢI chạy TRƯỚC ensure_provisioned().
        self._check_mqtt_credential_health(now)

        self._ensure_provisioned(now)

        poll_interval_ms = self.backend_cfg.ingest_interval_s * 1000
        if self._provision_done and now - self._last_ingest_ms >= poll_interval_ms:
            self._last_ingest_ms = now
            action = ingest_action(self._link.is_up(), self._time_synced)
            if action is IngestAction.POST_ONLINE:
                if self._ingest_once():
                    self.state.sent_batches += 1
                else:
                    self.state.failed_batches += 1
            elif action is IngestAction.QUEUE_OFFLINE:
                # GH-737 — mất mạng nhưng đồng hồ vẫn chạy: VẪN lấy mẫu và xếp hàng để đẩy bù.
                if self._sample_and_queue_offline():
                    log.info("[%s] offline → đã xếp hàng (độ sâu=%d)", self.cfg.device_code,
                             self._queue.size())
                else:
                    self.state.failed_batches += 1
            else:
                self.state.failed_batches += 1

        if self._provision_done:
            self._try_flush_queue(now)

        # OTA — verify-mode phải chạy cả khi mất mạng để bắt được hạn rollback.
        broker_up = bool(self._mqtt and self._mqtt.connected)
        self._ota.tick(self._link.is_up(), self._time_synced, broker_up)

        # Heartbeat CHỈ tồn tại ở contract production: backend Sprint 1 không có
        # `/api/iot-devices/heartbeat`, gửi vào đó chỉ sinh 401/404 mỗi phút và làm nhiễu log —
        # đúng thứ khiến người dùng đi tìm một sự cố không có thật.
        if self.production and self._provision_done and self._link.is_up() and self._time_synced:
            ok_before = self._heartbeat.ok_count
            fail_before = self._heartbeat.fail_count
            self._heartbeat.tick()
            if self._heartbeat.ok_count != ok_before:
                self._backend_acknowledged = True
                self.state.last_seen = self._iso_now()
                self.state.status = "online"
            elif self._heartbeat.fail_count != fail_before:
                self._backend_acknowledged = False
                self.state.last_error = self._heartbeat.last_error

        # SHT31 là cảm biến BÁO CÁO — gate theo mạng là hợp lý (firmware cũng vậy).
        if self._provision_done and self._link.is_up() and self._time_synced:
            self._sht31.tick(now, self._t_global, self.state.scenario)

        # GH-736 — CẢM BIẾN AN TOÀN CHẠY VÔ ĐIỀU KIỆN.
        # Xung khí/nước xảy ra lúc mất mạng mà không lấy mẫu thì sự cố biến mất không dấu vết.
        # Mất mạng không làm pin bớt cháy; gộp cảm biến an toàn chung với cảm biến báo cáo là
        # biến sự cố mạng thành sự cố an toàn.
        self._mq2.tick(now, self.state.scenario)
        self._water.tick(now, self.state.scenario)
        self._fire.tick(now, self.state.scenario, self._mq2.last_raw,
                        self._mq2.threshold_raw, self.state.last_temperature)

        self._sync_state(now)
        self._update_led()
        self._log_stats_periodic(now)

    # ══════════════════════════════ provision ══════════════════════════════════════════════
    def _ensure_provisioned(self, now: int) -> None:
        """`ensureProvisioned` của main.cpp."""
        if not self.production:
            # Contract legacy không có endpoint provision — chạy thẳng như firmware Sprint 1.
            self._provision_done = True
            return
        if self._provision_done:
            return
        if self._prov_cfg.provisioned:
            self._provision_done = True
            self._wire_site_id()
            self._apply_mqtt_runtime()
            return
        if not self._time_synced:
            return
        if now < self._provision_next_attempt_ms:
            return

        self.state.status = "provisioning"
        self.state.led = LedState.PROVISIONING
        log.info("[%s] chưa provisioned — chạy provision flow...", self.cfg.device_code)

        ok, cfg, err = self._provision.run(
            firmware_version=self.cfg.firmware_version,
            hardware_revision=self.cfg.hardware_revision,
            device_timestamp_iso=self._iso_now())

        if not ok:
            self._provision_next_attempt_ms = now + PROVISION_RETRY_MS
            self.state.last_error = f"provision: {err}"
            self.state.status = "provisioning"
            log.warning("[%s] provision hỏng — thử lại sau %ds", self.cfg.device_code,
                        PROVISION_RETRY_MS // 1000)
            return

        self._prov_cfg = cfg
        self._provision_done = True
        self._backend_acknowledged = True
        self.state.status = "online"
        self.state.last_error = ""

        # Cấu hình backend cấp GHI ĐÈ seed.
        self.backend_cfg.ingest_interval_s = cfg.polling_interval_s
        self.backend_cfg.heartbeat_interval_s = cfg.heartbeat_interval_s
        self._heartbeat.set_interval(cfg.heartbeat_interval_s * 1000)
        if cfg.site_id:
            self.cfg.site_id_guid = cfg.site_id
        self.cfg.ntp_server = cfg.ntp_server

        self._rebuild_batteries()
        self._wire_site_id()
        self._apply_mqtt_runtime()

    def _wire_site_id(self) -> None:
        """`sht31SetSiteId` + `envIncidentSetSiteId` — siteId chỉ có sau provision."""
        site = self._prov_cfg.site_id or self.cfg.site_id_guid
        self._sht31.set_site_id(site)
        self._env_reporter.set_site_id(site)

    def _apply_provision_response(self, body) -> bool:
        """Áp `CommonResponse<IotDeviceProvisionResultDto>` — điểm vào cho test/gỡ rối.

        Dùng CHUNG đường xử lý với luồng thật (`ProvisionRunner`) để không có hai nhánh áp cấu
        hình trôi khỏi nhau.
        """
        from .provision import parse_provision_response

        parsed = parse_provision_response(body)
        if not parsed.ok:
            return False
        self.backend_cfg.ingest_interval_s = parsed.polling_interval_s
        self.backend_cfg.heartbeat_interval_s = parsed.heartbeat_interval_s
        self._heartbeat.set_interval(parsed.heartbeat_interval_s * 1000)
        if parsed.site_id:
            self.cfg.site_id_guid = parsed.site_id
            self._prov_cfg.site_id = parsed.site_id
        self.cfg.ntp_server = parsed.ntp_server
        self._provision.apply_mqtt(parsed.data)
        self._provision.apply_battery_mappings(parsed.data)
        self._rebuild_batteries()
        self._wire_site_id()
        return True

    def _check_mqtt_credential_health(self, now: int) -> None:
        """IOT3-44 — broker từ chối xác thực liên tục ⇒ xin lại credential qua /provision."""
        if not self.production or self._mqtt is None:
            return
        if not self._provision_done:
            return
        if not should_reprovision_on_auth_failure(
                self._mqtt.auth_fail_count, self._mqtt.auth_failure_threshold(), now,
                self._last_reprovision_ms, self._ever_reprovisioned):
            return

        log.warning("[%s] MQTT bị từ chối xác thực %d lần liên tiếp — xin lại credential qua "
                    "/provision", self.cfg.device_code, self._mqtt.auth_fail_count)
        self._provision.clear_provision_flag()
        self._prov_cfg.provisioned = False
        self._provision_done = False
        self._provision_next_attempt_ms = 0
        self._mqtt.reset_auth_failures()
        self._last_reprovision_ms = now
        self._ever_reprovisioned = True
        self.state.reprovision_count += 1

    # ══════════════════════════════ MQTT ═══════════════════════════════════════════════════
    def _apply_mqtt_runtime(self) -> None:
        """`mqttBegin` / `mqttApplyConfig` — chỉ khởi tạo khi cấu hình ĐỦ DÙNG.

        Chưa provision thì chưa có credential broker; đó là trạng thái HỢP LỆ và bình thường
        (HTTPS-only), không phải lỗi để mà gào lên mỗi 5 giây.
        """
        if not self.mqtt_cfg.enabled or not self.production:
            return
        if not self._mqtt_runtime.is_configured():
            if self._mqtt is None:
                log.info("[%s] chưa có cấu hình MQTT dùng được (chưa provision?) — chạy HTTPS-only",
                         self.cfg.device_code)
            return

        opts = MqttOptions(
            host=self._mqtt_runtime.host,
            port=self._mqtt_runtime.port,
            username=self._mqtt_runtime.username,
            password=self._mqtt_runtime.password,
            tls=self._mqtt_runtime.want_tls,
            qos=self.mqtt_cfg.qos,
            topic_prefix=self._mqtt_runtime.topic_prefix(),
            device_code=self.cfg.device_code,
            site_id=self.cfg.site_label,
            keepalive_s=self.mqtt_cfg.keepalive_s,
            max_packet_size=self.mqtt_cfg.max_packet_size,
            reconnect_interval_ms=int(self.mqtt_cfg.reconnect_interval_s * 1000),
            auth_fail_threshold=self.mqtt_cfg.auth_fail_threshold,
        )
        self.state.warnings = [w for w in self.state.warnings if "tiền tố" not in w]
        self.state.warnings.extend(self._mqtt_runtime.warn_if_prefix_mismatch())

        if self._mqtt is None:
            try:
                self._mqtt = IotMqttClient(opts, on_command=self._on_mqtt_command,
                                           on_connect=self._on_mqtt_connect)
            except RuntimeError as ex:
                log.warning("[%s] không khởi tạo được MQTT: %s", self.cfg.device_code, ex)
                return
            if not self._mqtt.connect():
                log.warning("[%s] MQTT connect hỏng — chạy HTTPS-only cho tới khi nối lại được",
                            self.cfg.device_code)
            return

        self._mqtt.apply_config(opts)

    def _on_mqtt_connect(self) -> None:
        """(Re)connect ⇒ xoá streak publish fail để ưu tiên MQTT trở lại (S4-FW-06)."""
        if self._mqtt:
            self._mqtt.reset_consecutive_fails()

    # ══════════════════════════════ ingest ═════════════════════════════════════════════════
    def _collect_readings(self) -> list:
        """`bmsSourcePollAll` — sinh reading đa nguồn, xen kẽ theo từng pin.

        Layout khớp firmware: [pin0.bms, pin0.ina, pin0.ds, pin1.bms, ...]. Nhờ thế mili-giây vá
        theo index cho ra thứ tự thời gian đúng với thứ tự đọc thật.
        """
        readings = []
        dt_s = float(self.backend_cfg.ingest_interval_s)
        for battery in self._batteries.values():
            bms = battery.step(dt_s=dt_s, t_global=self._t_global, scenario=self.state.scenario)
            self.state.last_voltage = bms.voltage
            self.state.last_temperature = bms.temperature
            self.state.last_soc = bms.soc_percent
            readings.append(bms)

            # Nguồn dự phòng CHỈ có ở contract production: contract legacy có khoá chính
            # (Time, BatteryAssetId) và KHÔNG có `sourceType`, nên backend đời đó không phân biệt
            # được nhiều nguồn trong cùng một pin.
            if not self.production:
                continue
            if self.cfg.sensors.ina226:
                readings.append(make_ina226_reading(bms, self.cfg.sensor_drift,
                                                    self.state.scenario))
            if self.cfg.sensors.ds18b20:
                readings.append(make_ds18b20_reading(bms, self.cfg.sensor_drift,
                                                     self.state.scenario))
        return readings

    def _ingest_once(self) -> bool:
        """`ingestOnce` — MQTT trước, fallback HTTPS chỉ phần chưa đẩy được."""
        iso_ts = self._iso_now()
        readings = self._collect_readings()
        if not readings:
            return False

        # S4-FW-04 + S4-FW-06: ưu tiên MQTT trừ khi chưa nối được / streak fail ≥ ngưỡng.
        published: list[str] = []
        try_mqtt = (self._mqtt is not None and self._mqtt.connected and self.production
                    and self._mqtt.consecutive_fail_count < MQTT_PUBLISH_FAIL_THRESHOLD)
        if try_mqtt:
            if self._ingest_via_mqtt(iso_ts, readings, published):
                self._backoff.reset()
                self.state.last_seen = self._iso_now()
                return True
            log.info("[%s] MQTT publish hỏng (streak=%d) → fallback HTTPS",
                     self.cfg.device_code, self._mqtt.consecutive_fail_count)
        elif (self._mqtt is not None
              and self._mqtt.consecutive_fail_count >= MQTT_PUBLISH_FAIL_THRESHOLD):
            log.info("[%s] bỏ qua MQTT (streak=%d ≥ %d) → HTTPS", self.cfg.device_code,
                     self._mqtt.consecutive_fail_count, MQTT_PUBLISH_FAIL_THRESHOLD)

        # GH-740 — CHỈ gửi phần MQTT chưa đẩy được.
        remaining = filter_out_published(readings, published)
        if not remaining:
            log.info("[%s] MQTT đã đẩy hết — không cần fallback HTTPS", self.cfg.device_code)
            self._backoff.reset()
            return True
        if len(remaining) < len(readings):
            log.info("[%s] fallback HTTPS %d/%d reading (bỏ %d đã publish qua MQTT)",
                     self.cfg.device_code, len(remaining), len(readings),
                     len(readings) - len(remaining))

        payload = build_batch_payload(remaining, iso_ts, self.cfg.device_code, self.production)
        if payload is None:
            return False
        idem_key = str(uuid.uuid4())

        outcome = self._post_batch(payload, idem_key)
        if outcome is PostOutcome.SUCCESS:
            self._backoff.reset()
            self.state.last_seen = self._iso_now()
            if not self.production:
                # Contract legacy không có heartbeat/provision — ingest là tín hiệu DUY NHẤT cho
                # biết backend còn sống, nên dùng nó để bật đèn xanh.
                self._backend_acknowledged = True
                self.state.status = "online"
            return True

        if outcome is PostOutcome.PERMANENT_FAILURE:
            # 4xx → dữ liệu sai, gửi lại vẫn sai → BỎ, KHÔNG xếp hàng, reset backoff.
            log.error("[%s] BỎ batch — lỗi vĩnh viễn 4xx (dữ liệu không hợp lệ)",
                      self.cfg.device_code)
            self.state.dropped_batches += 1
            self._backoff.reset()
            return False

        # Tạm thời → xếp hàng + backoff.
        self._enqueue(payload, idem_key)
        wait_ms = self._backoff.record_failure()
        self.state.backoff_s = round(wait_ms / 1000.0, 1)
        log.info("[%s] đã xếp hàng (độ sâu=%d) backoff=%dms", self.cfg.device_code,
                 self._queue.size(), wait_ms)
        return False

    def _ingest_via_mqtt(self, iso_ts: str, readings: list, published: list[str]) -> bool:
        """`ingestViaMqtt` — MỖI PIN một message lên `<prefix>/<serial>/telemetry`.

        Dừng ngay ở nhóm đầu tiên hỏng, NHƯNG vẫn trả về danh sách serial đã publish để caller
        loại chúng khỏi fallback HTTPS (GH-740).
        """
        groups = group_by_serial(readings)
        if not groups:
            log.warning("[%s] không có battery serial nào trong readings — bỏ qua MQTT",
                        self.cfg.device_code)
            return False

        for serial, group in groups:
            # Mỗi nhóm là MỘT payload độc lập ⇒ mili-giây đánh index lại từ 0, y hệt firmware.
            payload = build_batch_payload(group, iso_ts, self.cfg.device_code, self.production)
            if payload is None:
                log.warning("[%s] dựng payload cho pin %s HỎNG", self.cfg.device_code, serial)
                return False
            if not self._mqtt.publish_telemetry(serial, payload):
                return False
            published.append(serial)
        return True

    def _post_batch(self, payload: dict, idem_key: str) -> PostOutcome:
        """`postBatch` — có đọc `{totalReceived, inserted, skipped}` trong thân 2xx (GH-748)."""
        res = self._http.ingest(payload, idem_key)
        if res.ok:
            ing = parse_ingest_result(res.json)
            if ing.is_partial():
                self.state.partial_ingests += 1
                log.warning("[%s] ⚠ NHẬN THIẾU: %d/%d reading vào được, %d bị bỏ.",
                            self.cfg.device_code, ing.inserted, ing.total_received, ing.skipped)
                log.warning("[%s]   Nguyên nhân thường gặp: serial pin chưa được map cho thiết bị "
                            "này, hoặc giá trị ngoài dải vật lý. Kiểm tra provisioning/hiệu chuẩn.",
                            self.cfg.device_code)
            return PostOutcome.SUCCESS

        transient = is_transient_failure(res.status_code)
        self.state.last_error = f"ingest {res.status_code}: {res.body[:80]}"
        log.warning("[%s] ingest %s FAIL (code=%d) resp=%s", self.cfg.device_code,
                    "TẠM THỜI" if transient else "VĨNH VIỄN", res.status_code, res.body[:120])
        return PostOutcome.TRANSIENT_FAILURE if transient else PostOutcome.PERMANENT_FAILURE

    def _sample_and_queue_offline(self) -> bool:
        """`sampleAndQueueOffline` (GH-737).

        Khoá idempotency sinh NGAY LÚC LẤY MẪU và đi cùng bản ghi vào hàng đợi: khi đẩy bù sau
        khi có mạng, backend nhận đúng khoá đó nên gửi lại nhiều lần cũng không sinh bản ghi trùng.
        """
        if not self._time_synced:
            return False
        iso_ts = self._iso_now()
        readings = self._collect_readings()
        if not readings:
            return False
        payload = build_batch_payload(readings, iso_ts, self.cfg.device_code, self.production)
        if payload is None:
            return False
        self._enqueue(payload, str(uuid.uuid4()))
        return True

    def _enqueue(self, payload: dict, idem_key: str) -> None:
        before = self._queue.dropped_count
        self._queue.append("/api/sensor-readings/batch", payload, idem_key)
        if self._queue.dropped_count != before:
            self.state.dropped_batches += self._queue.dropped_count - before
        self.state.queued_batches = self._queue.size()

    def _try_flush_queue(self, now: int) -> None:
        """`tryFlushQueue` — MỘT batch mỗi vòng, có backoff, luôn qua HTTPS.

        ⚠ Khác firmware một chỗ CÓ CHỦ Ý: firmware chặn hàm này khi Wi-Fi chưa nối, vì driver
        Wi-Fi tự lo việc dò lại mạng. Simulator không có driver đó — trạng thái "có mạng" chỉ suy
        ra ĐƯỢC từ chính kết quả request. Nếu cũng chặn theo `link_up` thì một khi mất kết nối,
        sẽ không còn request nào để phát hiện lúc backend sống lại: thiết bị kẹt offline vĩnh viễn.
        Vì thế lần đẩy hàng đợi đóng luôn vai trò "dò mạng", và vẫn bị backoff ghìm nên không có
        chuyện nện backend.
        """
        if self._queue.size() == 0:
            return
        if not self._time_synced:
            return
        if not self._backoff.allowed(now):
            return

        item = self._queue.peek_oldest()
        if item is None:
            return

        endpoint = item.get("endpoint", "/api/sensor-readings/batch")
        key = item.get("key", "")
        payload = item.get("payload")
        if not isinstance(payload, dict):
            # Dòng hỏng (bản cũ ghi, hoặc file bị sửa tay) — bỏ để không chặn hàng đợi.
            log.warning("[%s] bỏ một mục hàng đợi hỏng", self.cfg.device_code)
            self._queue.delete_oldest()
            self.state.queued_batches = self._queue.size()
            return

        # Chỉ telemetry mới vào hàng đợi (ambient/incident không xếp hàng — giống firmware).
        # Nhánh dưới vẫn xử lý endpoint khác để tương thích file hàng đợi của bản cũ.
        if endpoint == "/api/ambient/readings/batch":
            res = self._http.ambient_ingest(payload, key)
        elif endpoint == "/api/environmental-incidents":
            res = self._http.environmental_incident(payload)
        else:
            res = self._http.ingest(payload, key)

        if res.ok:
            ing = parse_ingest_result(res.json)
            if ing.is_partial():
                self.state.partial_ingests += 1
                log.warning("[%s] ⚠ đẩy bù NHẬN THIẾU: %d/%d reading, %d bị bỏ",
                            self.cfg.device_code, ing.inserted, ing.total_received, ing.skipped)
            self._queue.delete_oldest()
            self._backoff.reset()
            self.state.backoff_s = 0.0
            self.state.queued_batches = self._queue.size()
            self.state.sent_batches += 1
            self.state.last_seen = self._iso_now()
            log.info("[%s] đẩy bù OK — hàng đợi còn %d", self.cfg.device_code,
                     self.state.queued_batches)
            return

        if not is_transient_failure(res.status_code):
            # 4xx → dữ liệu sai, retry vô ích → BỎ khỏi hàng đợi + reset backoff để các batch sau
            # được thử lại bình thường. Giữ lại là chặn vĩnh viễn mọi thứ phía sau.
            log.error("[%s] BỎ một batch trong hàng đợi — lỗi vĩnh viễn %d. Còn lại %d",
                      self.cfg.device_code, res.status_code, self._queue.size() - 1)
            self._queue.delete_oldest()
            self.state.dropped_batches += 1
            self._backoff.reset()
            self.state.backoff_s = 0.0
            self.state.queued_batches = self._queue.size()
            return

        wait_ms = self._backoff.record_failure(now)
        self.state.backoff_s = round(wait_ms / 1000.0, 1)
        log.info("[%s] đẩy bù lỗi tạm thời — backoff=%dms (hàng đợi giữ nguyên %d)",
                 self.cfg.device_code, wait_ms, self._queue.size())

    # ══════════════════════════════ OTA ════════════════════════════════════════════════════
    def _apply_firmware_version(self, new_version: str) -> None:
        """Callback "flash xong": đổi version đang chạy + LƯU BỀN VỮNG.

        Lưu lại là bắt buộc — nếu không, mỗi lần chạy simulator lại quay về version trong seed và
        backend sẽ offer đúng bản đó mãi mãi, che mất việc OTA có thành công hay không.
        """
        self.cfg.firmware_version = new_version
        self.state.firmware_version = new_version
        self._http.firmware_version = new_version
        self._http.set_identity()          # cập nhật User-Agent
        self._nvs.put_string(nvskeys.KEY_RUNNING_FW, new_version)

    # ══════════════════════════════ lệnh downlink ══════════════════════════════════════════
    def _on_mqtt_command(self, payload) -> None:
        """`cmd::onCommandPayload` — parse thuần rồi mới điều phối, mọi nhánh đều có ack."""
        self.state.cmd_received += 1
        parsed = parse_command_payload(payload)

        if not parsed.ok:
            log.warning("[%s] parse lệnh HỎNG: %s", self.cfg.device_code, parsed.parse_error)
            self._ack(parsed.cmd_id, "failed", parsed.parse_error)
            return

        log.info("[%s] nhận lệnh cmdId=%s type=%s", self.cfg.device_code, parsed.cmd_id,
                 parsed.type)

        if parsed.kind is CommandKind.SET_INTERVAL:
            if not parsed.has_polling_seconds:
                self._ack(parsed.cmd_id, "failed", "missing pollingSeconds")
                return
            if not is_valid_polling_seconds(parsed.polling_seconds):
                self._ack(parsed.cmd_id, "failed", "pollingSeconds out of range [1, 3600]")
                return
            self.backend_cfg.ingest_interval_s = parsed.polling_seconds
            self._prov_cfg.polling_interval_s = parsed.polling_seconds
            self._nvs.put_int(nvskeys.KEY_POLL_MS, parsed.polling_seconds * 1000)
            log.info("[%s] polling interval đổi runtime → %ds", self.cfg.device_code,
                     parsed.polling_seconds)
            self._ack(parsed.cmd_id, "ok")

        elif parsed.kind is CommandKind.REQUEST_HEARTBEAT:
            ok = self._heartbeat.send_now()
            if ok:
                self._backend_acknowledged = True
            self._ack(parsed.cmd_id, "ok" if ok else "failed",
                      None if ok else "heartbeat POST failed")

        elif parsed.kind is CommandKind.TRIGGER_OTA:
            if self._ota.request_check():
                self._ack(parsed.cmd_id, "ok", "ota check scheduled")
            else:
                self._ack(parsed.cmd_id, "rejected", self._ota.last_reject_reason())

        elif parsed.kind is CommandKind.SET_SCENARIO:
            # Mở rộng RIÊNG của simulator — firmware trả `unknown` cho lệnh này.
            params = parsed.params or {}
            new_scenario = str(params.get("scenario", params.get("value", "normal")))
            self.state.scenario = new_scenario
            self.cfg.scenario = new_scenario
            self._offline_scenario_at_ms = 0
            log.info("[%s] scenario đổi runtime → %s", self.cfg.device_code, new_scenario)
            self._ack(parsed.cmd_id, "ok", "sim-only command")

        else:
            self.state.cmd_unknown += 1
            log.warning("[%s] lệnh KHÔNG HIỂU type=%s", self.cfg.device_code, parsed.type)
            self._ack(parsed.cmd_id, "unknown", "unsupported command type")

    def _ack(self, cmd_id: str, status: str, error: str | None = None) -> None:
        ack = build_ack(cmd_id, status, error)
        if status == "ok":
            self.state.cmd_ack_ok += 1
        else:
            self.state.cmd_ack_failed += 1
        if self._mqtt:
            self._mqtt.publish_cmd_ack(ack)

    # ══════════════════════════════ trạng thái + đèn ═══════════════════════════════════════
    def _sync_state(self, now: int) -> None:
        self.state.queued_batches = self._queue.size()
        self.state.ambient_sent = self._sht31.post_ok_count
        self.state.incidents_sent = self._env_reporter.report_ok_count
        self.state.ota_checks = self._ota.check_count
        self.state.ota_updates = self._ota.update_ok_count
        self.state.ota_rollbacks = self._ota.rollback_count
        self.state.ota_failed = self._ota.failed_count
        self.state.heartbeat_ok = self._heartbeat.ok_count
        self.state.heartbeat_fail = self._heartbeat.fail_count
        self.state.link_phase = self._link.phase(now).name.lower()
        if self._mqtt:
            self.state.mqtt_connected = self._mqtt.connected
            self.state.mqtt_publish_ok = self._mqtt.publish_ok_count
            self.state.mqtt_publish_fail = self._mqtt.publish_fail_count
            self.state.mqtt_auth_fail = self._mqtt.auth_fail_count
        if not self._backoff.allowed(now):
            self.state.backoff_s = round(self._backoff.remaining_ms(now) / 1000.0, 1)
        else:
            self.state.backoff_s = 0.0

        if self.state.status not in ("halted",):
            if not self._link.is_up():
                self.state.status = "offline"
            elif self._provision_done and self._backend_acknowledged:
                self.state.status = "online"

    def _update_led(self) -> None:
        """`updateStatusLed` — IOT3-54: trạng thái MẠNG đứng TRÊN trạng thái hàng đợi.

        Chưa có mạng thì hàng đợi đầy là HỆ QUẢ, không phải nguyên nhân; mà đèn chỉ nói được MỘT
        điều nên phải nói cái gốc.
        """
        if self.state.status == "halted":
            self.state.led = LedState.OFFLINE
            return
        if not self._identity_ready:
            self.state.led = LedState.SETUP
            return

        phase = self._link.phase()
        if phase is LinkPhase.UNCONFIGURED:
            self.state.led = LedState.SETUP
            return
        if phase is LinkPhase.RECOVERY:
            self.state.led = LedState.RECOVERY
            return
        if phase is LinkPhase.CONNECTING:
            self.state.led = LedState.WIFI_SEARCHING
            return

        if not self._backend_acknowledged:
            # Chưa provision: tím, để người cài đặt biết thiết bị vẫn đang ghép backend.
            # Đã provision nhưng heartbeat hiện tại hỏng: ĐỎ, không xanh giả.
            self.state.led = (LedState.OFFLINE if self._provision_done
                              else LedState.PROVISIONING)
            return

        self.state.led = (LedState.QUEUED if self._queue.size() > 0 else LedState.ONLINE)

    def _log_stats_periodic(self, now: int) -> None:
        """`logStatsPeriodic` — 60s một lần, cùng bộ đếm với firmware để đối chiếu log hai bên."""
        if now - self._last_stats_ms < STATS_INTERVAL_MS:
            return
        self._last_stats_ms = now
        s = self.state
        log.info(
            "[stats %s] uptime=%ds ingest ok=%d fail=%d queued=%d dropped=%d partial=%d / "
            "hb ok=%d fail=%d / queue=%d backoff=%.1fs / "
            "mqtt conn=%s pubok=%d pubfail=%d authfail=%d / "
            "cmd rx=%d ok=%d fail=%d unk=%d / "
            "amb=%d inc=%d / ota checks=%d ok=%d rb=%d failed=%d verify=%s / "
            "fw=%s link=%s batmap=%s(%d pin)",
            self.cfg.device_code, (now - self._boot_ms) // 1000,
            s.sent_batches, s.failed_batches, s.queued_batches, s.dropped_batches,
            s.partial_ingests, s.heartbeat_ok, s.heartbeat_fail, self._queue.size(),
            s.backoff_s, "UP" if s.mqtt_connected else "DOWN", s.mqtt_publish_ok,
            s.mqtt_publish_fail, s.mqtt_auth_fail, s.cmd_received, s.cmd_ack_ok,
            s.cmd_ack_failed, s.cmd_unknown, s.ambient_sent, s.incidents_sent,
            s.ota_checks, s.ota_updates, s.ota_rollbacks, s.ota_failed,
            "yes" if self._ota.verify_mode else "no",
            self.cfg.firmware_version, s.link_phase, s.battery_map_source, s.battery_count)
