"""SimulatedDevice — 1 ESP32 node mô phỏng (đầy đủ tính năng firmware S1→S7).

Vòng đời (NI §8.3 + B1 provision flow):
  1. boot → NTP sync (sim = system clock UTC)
  2. provision (1 lần) — chỉ contract iot2-production
  3. loop ticker:
       - mỗi `ingest_interval_s`: poll BMS + sensor phụ → build payload → POST/MQTT
       - mỗi `heartbeat_interval_s`: POST heartbeat (chỉ iot2-production)
       - mỗi 5 phút: ambient reading (SHT31)
       - mỗi 6 giờ: firmware-check
       - khi POST fail → enqueue + exponential backoff
  4. shutdown: publish "offline" MQTT, stop thread.

Mọi reading cùng tick cùng battery dùng CHUNG 1 `time_iso` (cross-source pair §1.6.6).
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .bms import MockBattery, pinned_time_iso
from .config import (CONTRACT_CURRENT, CONTRACT_IOT2, BackendConfig,
                     DeviceConfig, MqttConfig)
from .http_client import IotHttpClient
from .mqtt_client import IotMqttClient, MqttOptions
from .ota import OtaRunner
from .queue import LocalQueue
from .sensors.ambient import make_ambient_reading
from .sensors.environmental import (make_fire_incident, make_gas_leak_incident,
                                    make_smoke_incident,
                                    make_water_leak_incident)
from .sensors.redundant import (GatewayReading, make_ds18b20_reading,
                                make_ina226_reading)

log = logging.getLogger("iot-sim.device")

# Firmware MQTT_PUBLISH_FAIL_THRESHOLD (config.example.h) — sau N publish telemetry FAIL
# liên tiếp thì bỏ MQTT, chuyển HTTPS cho tới khi reconnect reset streak (S4-FW-06).
MQTT_PUBLISH_FAIL_THRESHOLD = 3

# LedState (firmware ui/led_palette.h) — phản chiếu trạng thái WS2812 on-board.
LED_OFFLINE = "Offline"
LED_QUEUED = "Queued"
LED_ONLINE = "Online"
LED_PROVISIONING = "Provisioning"


@dataclass
class DeviceState:
    device_code: str
    status: str = "provisioning"
    last_seen: str = ""
    scenario: str = "normal"
    sent_batches: int = 0
    failed_batches: int = 0
    queued_batches: int = 0
    ambient_sent: int = 0
    incidents_sent: int = 0
    last_error: str = ""
    backoff_s: float = 0.0
    last_voltage: float = 0.0
    last_temperature: float = 0.0
    last_soc: float = 0.0
    led: str = LED_OFFLINE
    firmware_version: str = ""
    ota_checks: int = 0
    ota_updates: int = 0


@dataclass
class _ScenarioTriggers:
    smoke_armed_at: float = 0.0
    fire_armed_at: float = 0.0
    gas_armed_at: float = 0.0
    water_armed_at: float = 0.0
    smoke_sent: bool = False
    fire_sent: bool = False
    gas_sent: bool = False
    water_sent: bool = False
    offline_at: float = 0.0


# Ánh xạ scenario → environmental incident (cho luồng B3 EnvironmentalIncident path)
_ENV_INCIDENT_SCENARIOS = {"smoke", "fire_detected", "gas_leak", "water_leak"}

# Scenario triggers anomaly cấp battery — không cần xử lý đặc biệt ở đây, BMS đã gen data lệch
_BATTERY_SCENARIOS = {"overheat", "overvoltage", "undervoltage", "low_soc",
                       "rapid_discharge", "abnormal_charging", "soh_degradation",
                       "sensor_mismatch", "bms_error"}

# Scenario ambient — sensor SHT31 sinh data lệch
_AMBIENT_SCENARIOS = {"high_ambient_temp", "high_humidity", "high_temp_humidity_combo"}


class SimulatedDevice:
    def __init__(self, dev_cfg: DeviceConfig, backend_cfg: BackendConfig, mqtt_cfg: MqttConfig,
                 queue_dir: Path):
        self.cfg = dev_cfg
        self.backend_cfg = backend_cfg
        self.mqtt_cfg = mqtt_cfg

        self.state = DeviceState(device_code=dev_cfg.device_code, scenario=dev_cfg.scenario)
        self._batteries = {b.serial: MockBattery(b) for b in dev_cfg.batteries}
        self._triggers = _ScenarioTriggers()

        self._http = IotHttpClient(
            base_url=backend_cfg.base_url,
            device_code=dev_cfg.device_code,
            api_key=dev_cfg.api_key,
            tls_verify=backend_cfg.tls_verify,
            firmware_version=dev_cfg.firmware_version,
            contract_version=backend_cfg.contract_version,
        )

        self._mqtt: IotMqttClient | None = None
        if mqtt_cfg.enabled:
            self._mqtt = IotMqttClient(
                MqttOptions(
                    host=mqtt_cfg.host, port=mqtt_cfg.port,
                    username=mqtt_cfg.username, password=mqtt_cfg.password,
                    tls=mqtt_cfg.tls, qos=mqtt_cfg.qos,
                    topic_prefix=mqtt_cfg.topic_prefix,
                    device_code=dev_cfg.device_code, site_id=dev_cfg.site_label,
                ),
                on_command=self._on_mqtt_command,
                on_connect=self._on_mqtt_connect,
            )

        self._queue = LocalQueue(queue_dir / f"{dev_cfg.device_code}.jsonl")
        self.state.queued_batches = self._queue.size()
        self.state.firmware_version = dev_cfg.firmware_version

        # OTA runner (Sprint 7) — chỉ kích hoạt khi contract iot2-production.
        self._ota = OtaRunner(
            self._http,
            current_version_getter=lambda: self.cfg.firmware_version,
            apply_version=self._apply_firmware_version,
        )

        self._mqtt_fail_streak = 0          # S4-FW-06 consecutive publish fail
        self._stop = threading.Event()
        self._boot_t = time.time()
        self._next_backoff_at = 0.0
        self._current_backoff = backend_cfg.retry_base_s
        self._thread = threading.Thread(target=self._run, name=f"sim-{dev_cfg.device_code}", daemon=True)

    # public
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._mqtt:
            self._mqtt.disconnect()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    # main loop
    def _run(self) -> None:
        log.info("[%s] booting (contract=%s, scenario=%s, batteries=%d)",
                 self.cfg.device_code, self.backend_cfg.contract_version,
                 self.cfg.scenario, len(self.cfg.batteries))
        if self._mqtt and not self._mqtt.connect():
            log.warning("[%s] MQTT connect fail, fallback HTTPS only", self.cfg.device_code)

        # Provision chỉ tồn tại trong contract IoT-2
        if self.backend_cfg.contract_version == CONTRACT_IOT2:
            self._do_provision()
        else:
            self.state.status = "online"

        last_hb = 0.0
        last_ingest = 0.0
        last_ambient = 0.0
        last_fw_check = 0.0

        while not self._stop.is_set():
            now = time.time()

            if self.state.scenario == "device_offline":
                if self._triggers.offline_at == 0.0:
                    self._triggers.offline_at = now + 60.0
                if now >= self._triggers.offline_at:
                    if self.state.status != "halted":
                        log.warning("[%s] scenario device_offline → halt", self.cfg.device_code)
                        self.state.status = "halted"
                        if self._mqtt:
                            self._mqtt.disconnect()
                    time.sleep(1.0)
                    continue

            # heartbeat — chỉ contract IoT-2
            if (self.backend_cfg.contract_version == CONTRACT_IOT2
                    and now - last_hb >= self.backend_cfg.heartbeat_interval_s):
                self._send_heartbeat(now)
                last_hb = now

            if now - last_ingest >= self.backend_cfg.ingest_interval_s:
                self._tick_ingest(now)
                last_ingest = now

            # Ambient (SHT31 → /api/ambient/readings/batch) — Sprint 5 (S5-FW-06).
            # KHÔNG fire trong contract `current` để giữ strict Sprint 1 scope
            # (tasksprint S1-FW-04..07 chỉ có mock BMS ingest, không có ambient).
            if (self.backend_cfg.contract_version == CONTRACT_IOT2
                    and self.cfg.sensors.sht31
                    and now - last_ambient >= 300):
                self._send_ambient(now)
                last_ambient = now

            # OTA firmware-check + apply (Sprint 7) — mỗi `ota_check_interval_s` (firmware = 1h).
            # Firmware delay check đầu tiên tới uptime ~30s; ở đây gửi ngay tick đầu cho dễ demo.
            if (self.backend_cfg.contract_version == CONTRACT_IOT2
                    and now - last_fw_check >= self.backend_cfg.ota_check_interval_s):
                self._do_ota_check()
                last_fw_check = now

            # Environmental incident (MQ-2 / water leak → /api/environmental-incidents) — Sprint 6.
            # KHÔNG fire trong contract `current` để giữ strict Sprint 1 scope.
            if self.backend_cfg.contract_version == CONTRACT_IOT2:
                self._maybe_trigger_environmental(now)

            self._update_led()
            time.sleep(0.5)

        if self._mqtt:
            self._mqtt.disconnect()
        log.info("[%s] stopped", self.cfg.device_code)

    # provision
    def _do_provision(self) -> None:
        self.state.status = "provisioning"
        self.state.led = LED_PROVISIONING
        res = self._http.provision(
            hardware_revision=self.cfg.hardware_revision,
            device_timestamp_iso=_iso_now(),
        )
        if res.ok:
            log.info("[%s] provisioned (%d)", self.cfg.device_code, res.status_code)
            self._apply_provision_response(res.json)
            self.state.status = "online"
        else:
            log.warning("[%s] provision %s: %s", self.cfg.device_code, res.status_code, res.body[:160])
            self.state.status = "online" if res.status_code in (409, 400) else "offline"
            self.state.last_error = f"provision {res.status_code}"

    def _apply_provision_response(self, body: dict | None) -> None:
        """Áp dụng IotDeviceProvisionResultDto từ CommonResponse.data — khớp firmware
        provision.cpp::runProvisionFlow (đọc pollingIntervalSeconds / heartbeatIntervalSeconds
        / siteId / ntpServer rồi persist + dùng làm config runtime)."""
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return
        poll = data.get("pollingIntervalSeconds")
        hb = data.get("heartbeatIntervalSeconds")
        site = data.get("siteId")
        ntp = data.get("ntpServer")
        if isinstance(poll, (int, float)) and poll > 0:
            self.backend_cfg.ingest_interval_s = max(1, min(600, int(poll)))   # firmware bound [1,600]
        if isinstance(hb, (int, float)) and hb > 0:
            self.backend_cfg.heartbeat_interval_s = max(10, min(3600, int(hb)))  # bound [10,3600]
        if isinstance(site, str) and site:
            # siteId backend-assigned override seed — dùng cho ambient + environmental incident
            # (giống firmware: provision response cấp siteId rồi wire vào SHT31/MQ-2/water-leak).
            self.cfg.site_id_guid = site
        if isinstance(ntp, str) and ntp:
            self.cfg.ntp_server = ntp
        log.info("[%s] provision applied: poll=%ss hb=%ss site=%s",
                 self.cfg.device_code, self.backend_cfg.ingest_interval_s,
                 self.backend_cfg.heartbeat_interval_s, self.cfg.site_id_guid)

    # heartbeat (60s) — MO §52.2/§52.4 field mapping (đồng bộ với firmware ESP32 Sprint 2)
    def _send_heartbeat(self, now: float) -> None:
        uptime_s = int(now - self._boot_t)
        # MemoryUsageMb là long? trong IotDeviceHeartbeatCommand → ép integer,
        # tránh System.Text.Json strict-mode reject float cho integer type.
        mem_mb = int(round(random.uniform(120.0, 220.0)))
        free_heap_total_mb = 320  # ESP32-S3 internal SRAM = 320KB; trên N16R8 + PSRAM 8MB ≈ 8000+
        free_mem_pct = round((mem_mb / float(free_heap_total_mb)) * 100.0, 2) if free_heap_total_mb else 0.0
        body = {
            "FirmwareVersion": self.cfg.firmware_version,
            "Temperature": round(35.0 + random.uniform(-2.0, 5.0), 2),    # decimal? — chip temp °C
            "MemoryUsageMb": mem_mb,                                       # long?    — int, không float
            "FreeMemoryPercent": free_mem_pct,                             # decimal? — Sprint 2 review fix
            "Cpu": None,                                                   # decimal? — ESP32 không Linux CPU
            "DiskFreeMb": None,                                            # long?    — ESP32 không disk
            "LocalQueueDepth": self._queue.size(),                         # int?     — alias QueuedReadingCount
            "SignalStrengthDbm": random.randint(-80, -40),                 # int?     — alias RssiDbm
            "UptimeSeconds": uptime_s,                                     # long?
            "DeviceTimestamp": _iso_now(skew_min=10 if self.state.scenario == "clock_skew" else 0),
            # KHÔNG có ConnectedSensorCount + IpAddress: không trong IotDeviceHeartbeatCommand
            # và không trong spec MO §52.4 (firmware ESP32 cũng không gửi).
        }
        # Firmware heartbeat.cpp::sendOnce gửi qua HTTP POST (httpPostJson) — KHÔNG qua MQTT.
        # Giữ đúng transport để khớp firmware (broker chỉ nhận telemetry/status/cmd-ack).
        res = self._http.heartbeat(body)
        if res.ok:
            self.state.last_seen = _iso_now()
            self.state.status = "online"
        else:
            self.state.last_error = f"heartbeat {res.status_code}"

    # ingest
    def _tick_ingest(self, now: float) -> None:
        skew_min = 10 if self.state.scenario == "clock_skew" else 0
        tick_dt = datetime.now(timezone.utc) + timedelta(minutes=skew_min)
        tick_time_iso = tick_dt.isoformat().replace("+00:00", "Z")
        # Gom reading theo battery serial — firmware publish MỖI PIN 1 telemetry message
        # (ingestViaMqtt group-by-serial), HTTPS fallback gửi cả batch trong 1 POST.
        groups: dict[str, list[dict]] = {}

        for bat in self._batteries.values():
            # Scenario per-battery (bat.cfg.scenario) override scenario device. Cho phép
            # 2 pin cùng device chạy 2 kịch bản khác nhau. "" → kế thừa device.
            bat_scenario = bat.cfg.scenario or self.state.scenario
            bms = bat.step(
                dt_s=float(self.backend_cfg.ingest_interval_s),
                t_global=now,
                scenario=bat_scenario,
                time_iso=tick_time_iso,
            )
            self.state.last_voltage = bms.voltage
            self.state.last_temperature = bms.temperature
            self.state.last_soc = bms.soc_percent
            grp = groups.setdefault(bms.battery_serial, [])
            grp.append(self._bms_to_dict(bms))

            # Gateway readings (INA226 / DS18B20) CHỈ append khi contract iot2-production —
            # contract `current` có PK = (Time, BatteryAssetId) nên không phân biệt nhiều source/tick.
            # Production contract (S3-FW-04) thêm SourceType + SensorSourceCode → multi-source hợp lệ.
            if self.backend_cfg.contract_version == CONTRACT_IOT2:
                if self.cfg.sensors.ina226:
                    grp.append(self._gw_to_dict(
                        make_ina226_reading(bms, self.cfg.sensor_drift, bat_scenario)))
                if self.cfg.sensors.ds18b20:
                    grp.append(self._gw_to_dict(
                        make_ds18b20_reading(bms, self.cfg.sensor_drift, bat_scenario)))

        if not groups:
            return

        # Stagger `time` theo ms cho từng source TRONG 1 pin → unique (Time, BatteryAssetId),
        # tránh PK conflict hypertable backend. Backend KHUYẾN NGHỊ ms-resolution
        # (SensorReadingsController: "Time đủ resolution (ms)"); CrossSourceValidationService
        # ghép cặp theo PairingWindow 60s nên lệch ms vẫn pair BMS↔IotGateway đúng.
        # `deviceTimestamp` GIỮ NGUYÊN tick base (dùng cho clock-skew check #IoT2-15).
        # ⚠ Deviation có chủ đích so với firmware (firmware gửi Time giống nhau + dựa MQTT
        #   bridge); cần để multi-source ingest HTTP hoạt động trên backend hiện tại (audit #10).
        if self.backend_cfg.contract_version == CONTRACT_IOT2:
            for grp in groups.values():
                for j, item in enumerate(grp):
                    if j:
                        item["time"] = (tick_dt + timedelta(milliseconds=j)).isoformat().replace("+00:00", "Z")

        flat = [r for grp in groups.values() for r in grp]
        payload = self._build_ingest_payload(tick_time_iso, flat)
        idem_key = str(uuid.uuid4())

        if self._send_ingest(groups, payload, idem_key):
            self._flush_queue()
        else:
            self._queue.append("/api/sensor-readings/batch", payload, idem_key)
            self.state.queued_batches = self._queue.size()
            log.info("[%s] queued (size=%d)", self.cfg.device_code, self.state.queued_batches)

    def _send_ingest(self, groups: dict[str, list[dict]], payload: dict, idem_key: str) -> bool:
        if time.time() < self._next_backoff_at:
            return False

        # ---- MQTT-first (S4-FW-04 + S4-FW-06) — chỉ iot2; per-pin publish ----
        # Bỏ MQTT khi streak fail ≥ threshold; reconnect reset streak (xem _on mqtt reconnect).
        try_mqtt = (self._mqtt is not None and self._mqtt.connected
                    and self.backend_cfg.contract_version == CONTRACT_IOT2
                    and self._mqtt_fail_streak < MQTT_PUBLISH_FAIL_THRESHOLD)
        if try_mqtt:
            all_ok = True
            for serial, grp in groups.items():
                if not self._mqtt.publish_telemetry(serial, {"items": grp}):
                    all_ok = False
                    self._mqtt_fail_streak += 1
                    break
            if all_ok:
                self._mqtt_fail_streak = 0
                self.state.sent_batches += 1
                self.state.last_seen = _iso_now()
                self._reset_backoff()
                return True
            log.info("[%s] MQTT publish FAIL (streak=%d) → fallback HTTPS",
                     self.cfg.device_code, self._mqtt_fail_streak)

        # ---- Fallback HTTPS — gửi cả batch trong 1 POST (giữ idempotency + queue) ----
        res = self._http.ingest(payload, idem_key)
        if res.ok:
            self.state.sent_batches += 1
            self.state.last_seen = _iso_now()
            self._reset_backoff()
            return True
        self.state.failed_batches += 1
        self.state.last_error = f"ingest {res.status_code}: {res.body[:80]}"
        self._bump_backoff()
        return False

    def _flush_queue(self) -> None:
        items = self._queue.read_all()
        if not items:
            return
        flushed = 0
        for it in items:
            ep = it.get("endpoint")
            key = it["key"]
            payload = it["payload"]
            if ep == "/api/ambient/readings/batch":
                r = self._http.ambient_ingest(payload, key)
            elif ep == "/api/environmental-incidents":
                r = self._http.environmental_incident(payload)
            else:
                r = self._http.ingest(payload, key)
            if r.ok:
                flushed += 1
            else:
                break
        if flushed:
            self._queue.remove_first(flushed)
            self.state.queued_batches = self._queue.size()
            log.info("[%s] flushed %d (remaining=%d)",
                     self.cfg.device_code, flushed, self.state.queued_batches)

    # ambient — POST /api/ambient/readings/batch (api-battery.md contract)
    def _send_ambient(self, now: float) -> None:
        tick = pinned_time_iso()
        amb = make_ambient_reading(
            device_code=self.cfg.device_code,
            site_id_guid=self.cfg.site_id_guid,
            time_iso=tick,
            t_global=now,
            scenario=self.state.scenario,
        )
        payload = {
            "items": [{
                "siteId": amb.site_id_guid,
                "time": amb.time_iso,
                "ambientTemperature": amb.ambient_temperature,
                "humidity": amb.humidity,
                "solarIrradiance": amb.solar_irradiance,
                "source": amb.source,                                     # int 1 = IotSensor
                "sourceDeviceId": amb.source_device_id,
            }],
        }
        idem_key = str(uuid.uuid4())
        res = self._http.ambient_ingest(payload, idem_key)
        if res.ok:
            self.state.ambient_sent += 1
        else:
            self._queue.append("/api/ambient/readings/batch", payload, idem_key)
            self.state.queued_batches = self._queue.size()
            self.state.last_error = f"ambient {res.status_code}"

    # environmental incident — POST /api/environmental-incidents
    def _maybe_trigger_environmental(self, now: float) -> None:
        sc = self.state.scenario
        if sc not in _ENV_INCIDENT_SCENARIOS:
            return
        ttl = 30.0           # arm sau 30s
        detected_at = _iso_now()

        if sc == "smoke" and self.cfg.sensors.mq2:
            if self._triggers.smoke_armed_at == 0.0:
                self._triggers.smoke_armed_at = now + ttl
            if not self._triggers.smoke_sent and now >= self._triggers.smoke_armed_at:
                self._send_incident(make_smoke_incident(
                    self.cfg.device_code, self.cfg.site_id_guid, detected_at, adc_value=3100))
                self._triggers.smoke_sent = True

        if sc == "fire_detected" and self.cfg.sensors.mq2:
            if self._triggers.fire_armed_at == 0.0:
                self._triggers.fire_armed_at = now + ttl
            if not self._triggers.fire_sent and now >= self._triggers.fire_armed_at:
                self._send_incident(make_fire_incident(
                    self.cfg.device_code, self.cfg.site_id_guid, detected_at, temp_c=78.0))
                self._triggers.fire_sent = True

        if sc == "gas_leak" and self.cfg.sensors.mq2:
            if self._triggers.gas_armed_at == 0.0:
                self._triggers.gas_armed_at = now + ttl
            if not self._triggers.gas_sent and now >= self._triggers.gas_armed_at:
                self._send_incident(make_gas_leak_incident(
                    self.cfg.device_code, self.cfg.site_id_guid, detected_at, adc_value=2900))
                self._triggers.gas_sent = True

        if sc == "water_leak" and self.cfg.sensors.water_leak:
            if self._triggers.water_armed_at == 0.0:
                self._triggers.water_armed_at = now + ttl
            if not self._triggers.water_sent and now >= self._triggers.water_armed_at:
                self._send_incident(make_water_leak_incident(
                    self.cfg.device_code, self.cfg.site_id_guid, detected_at))
                self._triggers.water_sent = True

    def _send_incident(self, incident) -> None:
        body = {
            "siteId":       incident.site_id_guid,
            "incidentType": incident.incident_type,                       # int 1..5,99
            "severity":     incident.severity,                            # int 1..3
            "detectedAt":   incident.detected_at,
            "reportedBy":   incident.reported_by,
            "notes":        incident.notes,
        }
        res = self._http.environmental_incident(body)
        if res.ok:
            self.state.incidents_sent += 1
            log.warning("[%s] environmental incident type=%d reported",
                        self.cfg.device_code, incident.incident_type)
        else:
            self.state.last_error = f"incident {res.status_code}"
            self._queue.append("/api/environmental-incidents", body, str(uuid.uuid4()))
            self.state.queued_batches = self._queue.size()

    # payload builders — 2 contract, CẢ HAI dùng envelope {"items":[...]}.
    # Backend BatchIngestSensorReadingsCommand chỉ bind `Items` (System.Text.Json
    # case-insensitive → "items" khớp). KHÔNG có wrapper DeviceTimestamp/Readings.
    def _build_ingest_payload(self, device_ts: str, readings: list[dict]) -> dict:
        _ = device_ts   # production để deviceTimestamp PER-ITEM (firmware buildProductionBatchPayload)
        return {"items": readings}

    def _bms_to_dict(self, r) -> dict:
        if self.backend_cfg.contract_version == CONTRACT_CURRENT:
            # Sprint 1 legacy contract — firmware buildLegacyBatchPayload + newiot.md §7.4.
            # KHÔNG có sourceDeviceId/sourceType (Sprint 3 production — S3-FW-04).
            return {
                "batteryAssetId":  r.battery_asset_id,
                "time":            r.time_iso,
                "voltage":         r.voltage,
                "current":         r.current,
                "temperature":     r.temperature,
                "socPercent":      r.soc_percent,
                "cycleCount":      r.cycle_count,
            }
        # iot2-production — KHỚP firmware buildProductionBatchPayload (camelCase, per-item
        # deviceTimestamp, sourceType luôn có, optional field chỉ gửi khi tồn tại).
        item: dict = {
            "batteryAssetSerial": r.battery_serial,
            "time":               r.time_iso,
            "deviceTimestamp":    r.time_iso,          # #IoT2-15 clock-skew check
            "voltage":            r.voltage,
            "current":            r.current,
            "temperature":        r.temperature,
            "socPercent":         r.soc_percent,
            "cycleCount":         r.cycle_count,
            "sourceType":         r.source_type,        # 1 = Bms
            "sensorSourceCode":   r.sensor_source_code, # "primary"
            "sohPercent":         r.soh_percent,        # BMS biết SOH (hasSoh)
            "chargingState":      r.charging_state,     # BMS biết charging state
        }
        if r.bms_error_code:                            # chỉ gửi khi có (hasBmsError)
            item["bmsErrorCode"] = r.bms_error_code
        return item

    def _gw_to_dict(self, r: GatewayReading) -> dict:
        if self.backend_cfg.contract_version == CONTRACT_CURRENT:
            # current mode KHÔNG gửi gateway reading (gated trong _tick_ingest) — giữ branch
            # an toàn: flatten thành reading thường nếu có ai gọi.
            return {
                "batteryAssetId":  r.battery_asset_id,
                "time":            r.time_iso,
                "voltage":         r.voltage if r.voltage is not None else 0.0,
                "current":         r.current if r.current is not None else 0.0,
                "temperature":     r.temperature if r.temperature is not None else 0.0,
                "socPercent":      0.0,
                "cycleCount":      0,
            }
        # iot2-production — gateway (INA226/DS18B20): voltage non-nullable ở backend →
        # field thiếu emit 0.0 (firmware struct dùng 0 cho field không đo). KHÔNG có
        # sohPercent/chargingState/bmsErrorCode (gateway không biết → omit).
        return {
            "batteryAssetSerial": r.battery_serial,
            "time":               r.time_iso,
            "deviceTimestamp":    r.time_iso,
            "voltage":            r.voltage if r.voltage is not None else 0.0,
            "current":            r.current if r.current is not None else 0.0,
            "temperature":        r.temperature if r.temperature is not None else 0.0,
            "socPercent":         r.soc_percent if r.soc_percent is not None else 0.0,
            "cycleCount":         r.cycle_count if r.cycle_count is not None else 0,
            "sourceType":         r.source_type,        # 2 = IotGateway
            "sensorSourceCode":   r.sensor_source_code, # "redundant" | "external-temp"
        }

    # backoff
    def _bump_backoff(self) -> None:
        nxt = min(self._current_backoff * 2, self.backend_cfg.retry_max_s)
        jitter = nxt * (self.backend_cfg.retry_jitter_pct / 100.0)
        actual = nxt + random.uniform(-jitter, jitter)
        self._current_backoff = max(self.backend_cfg.retry_base_s, nxt)
        self._next_backoff_at = time.time() + actual
        self.state.backoff_s = round(actual, 1)

    def _reset_backoff(self) -> None:
        self._current_backoff = self.backend_cfg.retry_base_s
        self._next_backoff_at = 0.0
        self.state.backoff_s = 0.0

    # MQTT downlink command — KHỚP firmware cmd_logic.cpp + command_handler.cpp.
    #   Command JSON : { "cmdId", "type": "set_interval|request_heartbeat|trigger_ota", "params": {...} }
    #   type case-insensitive, chấp nhận cả "_" và "-" (set-interval == set_interval).
    #   Ack JSON     : { "cmdId", "status": "ok|failed|unknown", "error"? }
    def _on_mqtt_command(self, cmd: dict) -> None:
        cmd_id = str(cmd.get("cmdId", ""))
        raw_type = str(cmd.get("type", "")).strip().lower().replace("-", "_")
        params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}

        def ack(status: str, error: str | None = None) -> None:
            payload = {"cmdId": cmd_id, "status": status}
            if error:
                payload["error"] = error
            if self._mqtt:
                self._mqtt.publish_cmd_ack(payload)

        if not raw_type:
            ack("failed", "missing field: type")
            return

        try:
            if raw_type == "set_interval":
                secs = params.get("pollingSeconds", params.get("pollingIntervalSeconds"))
                if secs is None:
                    ack("failed", "missing pollingSeconds")
                    return
                secs = int(secs)
                if secs < 1 or secs > 3600:                       # firmware kPollingMin/Max
                    ack("failed", "pollingSeconds out of range [1, 3600]")
                    return
                self.backend_cfg.ingest_interval_s = secs
                ack("ok")
            elif raw_type == "request_heartbeat":
                self._send_heartbeat(time.time())
                ack("ok")
            elif raw_type == "trigger_ota":
                # Firmware Sprint 4 ack "ota scheduled" (placeholder); ở simulator chạy thật
                # 1 chu kỳ OTA check + apply (Sprint 7) cho demo end-to-end.
                if self.backend_cfg.contract_version == CONTRACT_IOT2:
                    self._do_ota_check()
                ack("ok", "ota check triggered")
            elif raw_type == "set_scenario":
                # Sim-only extension (firmware không có) — đổi scenario runtime để demo.
                self.state.scenario = str(params.get("scenario", params.get("value", "normal")))
                self._triggers = _ScenarioTriggers()
                ack("ok")
            else:
                ack("unknown", "unsupported command type")
        except (ValueError, KeyError, TypeError) as ex:
            ack("failed", str(ex))

    def _on_mqtt_connect(self) -> None:
        # (Re)connect MQTT → reset consecutive-fail streak để ưu tiên MQTT trở lại (S4-FW-06).
        self._mqtt_fail_streak = 0

    # OTA (Sprint 7) — firmware-check → quyết định → update-log lifecycle → bump version.
    def _do_ota_check(self) -> None:
        result = self._ota.check_and_apply()
        self.state.ota_checks = self._ota.check_count
        self.state.ota_updates = self._ota.update_ok_count
        if result.updated:
            log.warning("[%s] OTA updated → %s", self.cfg.device_code, result.target_version)
        elif not result.checked:
            self.state.last_error = f"ota {result.message}"

    def _apply_firmware_version(self, new_version: str) -> None:
        """Callback OtaRunner — 'flash' xong: đổi version đang chạy. Heartbeat + lần
        firmware-check kế tiếp dùng version mới (backend sẽ thấy trùng target → hết offer)."""
        self.cfg.firmware_version = new_version
        self._http.firmware_version = new_version
        self.state.firmware_version = new_version

    # LED state (firmware updateStatusLed) — phản chiếu trạng thái vận hành.
    def _update_led(self) -> None:
        if self.state.status == "provisioning":
            self.state.led = LED_PROVISIONING
        elif self.state.status in ("offline", "halted"):
            self.state.led = LED_OFFLINE
        elif self._queue.size() > 0:
            self.state.led = LED_QUEUED
        else:
            self.state.led = LED_ONLINE


def _iso_now(skew_min: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(minutes=skew_min)
    return t.isoformat().replace("+00:00", "Z")
