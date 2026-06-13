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

from .bms import BmsReading, MockBattery, pinned_time_iso
from .config import (CONTRACT_CURRENT, CONTRACT_IOT2, BackendConfig,
                     DeviceConfig, MqttConfig)
from .http_client import IotHttpClient
from .mqtt_client import IotMqttClient, MqttOptions
from .queue import LocalQueue
from .sensors.ambient import make_ambient_reading
from .sensors.environmental import (make_fire_incident, make_gas_leak_incident,
                                    make_smoke_incident,
                                    make_water_leak_incident)
from .sensors.redundant import (GatewayReading, make_ds18b20_reading,
                                make_ina226_reading)

log = logging.getLogger("iot-sim.device")


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
            )

        self._queue = LocalQueue(queue_dir / f"{dev_cfg.device_code}.jsonl")
        self.state.queued_batches = self._queue.size()

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

            if self.cfg.sensors.sht31 and now - last_ambient >= 300:
                self._send_ambient(now)
                last_ambient = now

            if self.backend_cfg.contract_version == CONTRACT_IOT2 and now - last_fw_check >= 6 * 3600:
                self._http.firmware_check(self.cfg.firmware_version)
                last_fw_check = now

            self._maybe_trigger_environmental(now)

            time.sleep(0.5)

        if self._mqtt:
            self._mqtt.disconnect()
        log.info("[%s] stopped", self.cfg.device_code)

    # provision
    def _do_provision(self) -> None:
        self.state.status = "provisioning"
        res = self._http.provision(
            hardware_revision=self.cfg.hardware_revision,
            device_timestamp_iso=_iso_now(),
        )
        if res.ok:
            log.info("[%s] provisioned (%d)", self.cfg.device_code, res.status_code)
            self.state.status = "online"
        else:
            log.warning("[%s] provision %s: %s", self.cfg.device_code, res.status_code, res.body[:160])
            self.state.status = "online" if res.status_code in (409, 400) else "offline"
            self.state.last_error = f"provision {res.status_code}"

    # heartbeat (60s)
    def _send_heartbeat(self, now: float) -> None:
        uptime_s = int(now - self._boot_t)
        body = {
            "FirmwareVersion": self.cfg.firmware_version,
            "Temperature": round(35.0 + random.uniform(-2.0, 5.0), 2),
            "MemoryUsageMb": round(random.uniform(120.0, 220.0), 1),
            "Cpu": None,                                       # ESP32 — không Linux concept
            "DiskFreeMb": None,
            "ConnectedSensorCount": len(self.cfg.batteries) + (
                (1 if self.cfg.sensors.ina226 else 0)
                + (1 if self.cfg.sensors.ds18b20 else 0)
                + (1 if self.cfg.sensors.sht31 else 0)
            ),
            "LocalQueueDepth": self._queue.size(),
            "SignalStrengthDbm": random.randint(-80, -40),
            "IpAddress": "192.168.1." + str(random.randint(20, 250)),
            "UptimeSeconds": uptime_s,
            "DeviceTimestamp": _iso_now(skew_min=10 if self.state.scenario == "clock_skew" else 0),
        }
        if self._mqtt and self._mqtt.connected:
            if self._mqtt.publish_heartbeat(body):
                self.state.last_seen = _iso_now()
                return
        res = self._http.heartbeat(body)
        if res.ok:
            self.state.last_seen = _iso_now()
            self.state.status = "online"
        else:
            self.state.last_error = f"heartbeat {res.status_code}"

    # ingest
    def _tick_ingest(self, now: float) -> None:
        tick_time_iso = pinned_time_iso(skew_min=10 if self.state.scenario == "clock_skew" else 0)
        readings: list[dict] = []
        last_bms: BmsReading | None = None

        for bat in self._batteries.values():
            bms = bat.step(
                dt_s=float(self.backend_cfg.ingest_interval_s),
                t_global=now,
                scenario=self.state.scenario,
                time_iso=tick_time_iso,
            )
            last_bms = bms
            self.state.last_voltage = bms.voltage
            self.state.last_temperature = bms.temperature
            self.state.last_soc = bms.soc_percent
            readings.append(self._bms_to_dict(bms))

            # Gateway readings (INA226 / DS18B20) CHỈ append khi contract iot2-production —
            # contract `current` có PK = (Time, BatteryAssetId) nên không phân biệt nhiều source/tick.
            # Backend sẽ throw "instance ... already being tracked" nếu push 2+ readings cùng PK.
            # Sprint IoT-2 #IoT2-14 sẽ thêm SourceType vào PK → khi đó multi-source mới hợp lệ.
            if self.backend_cfg.contract_version == CONTRACT_IOT2:
                if self.cfg.sensors.ina226:
                    readings.append(self._gw_to_dict(
                        make_ina226_reading(bms, self.cfg.sensor_drift, self.state.scenario)))
                if self.cfg.sensors.ds18b20:
                    readings.append(self._gw_to_dict(
                        make_ds18b20_reading(bms, self.cfg.sensor_drift, self.state.scenario)))

        if not readings:
            return

        payload = self._build_ingest_payload(tick_time_iso, readings)
        idem_key = str(uuid.uuid4())

        if self._send_ingest(payload, idem_key):
            self._flush_queue()
        else:
            self._queue.append("/api/sensor-readings/batch", payload, idem_key)
            self.state.queued_batches = self._queue.size()
            log.info("[%s] queued (size=%d)", self.cfg.device_code, self.state.queued_batches)

        _ = last_bms

    def _send_ingest(self, payload: dict, idem_key: str) -> bool:
        if time.time() < self._next_backoff_at:
            return False

        if self._mqtt and self._mqtt.connected and self.backend_cfg.contract_version == CONTRACT_IOT2:
            if self._mqtt.publish_telemetry(payload):
                self.state.sent_batches += 1
                self.state.last_seen = _iso_now()
                self._reset_backoff()
                return True

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

    # payload builders — 2 contract
    def _build_ingest_payload(self, device_ts: str, readings: list[dict]) -> dict:
        if self.backend_cfg.contract_version == CONTRACT_CURRENT:
            # api-battery.md TODAY: items[].batteryAssetId + sourceDeviceId
            # KHÔNG có DeviceTimestamp wrapper, KHÔNG có SourceType field.
            return {"items": readings}
        # iot2-production: Sprint IoT-2 #IoT2-14 contract
        return {
            "DeviceTimestamp": device_ts,
            "Readings": readings,
        }

    def _bms_to_dict(self, r: BmsReading) -> dict:
        if self.backend_cfg.contract_version == CONTRACT_CURRENT:
            # Field shape THẬT của backend api-battery.md §POST /api/sensor-readings/batch
            return {
                "batteryAssetId":  r.battery_asset_id,
                "time":            r.time_iso,
                "voltage":         r.voltage,
                "current":         r.current,
                "temperature":     r.temperature,
                "socPercent":      r.soc_percent,
                "cycleCount":      r.cycle_count,
                "sourceDeviceId":  self.cfg.device_code,
            }
        # iot2-production
        return {
            "BatteryAssetSerial": r.battery_serial,
            "Time":               r.time_iso,
            "Voltage":            r.voltage,
            "Current":            r.current,
            "Temperature":        r.temperature,
            "SocPercent":         r.soc_percent,
            "SohPercent":         r.soh_percent,
            "CycleCount":         r.cycle_count,
            "ChargingState":      r.charging_state,
            "BmsErrorCode":       r.bms_error_code,
            "SourceType":         r.source_type,                          # 1 = Bms
            "SensorSourceCode":   r.sensor_source_code,                   # "primary"
        }

    def _gw_to_dict(self, r: GatewayReading) -> dict:
        if self.backend_cfg.contract_version == CONTRACT_CURRENT:
            # `current` contract chưa support multi-source — gửi giống reading thường (Voltage/Temperature
            # có thể null). Field SourceType chưa tồn tại → backend sẽ bỏ qua. Vẫn hợp lệ vì các field
            # null được backend skip nếu optional.
            return {
                "batteryAssetId":  r.battery_asset_id,
                "time":            r.time_iso,
                "voltage":         r.voltage if r.voltage is not None else 0.0,
                "current":         r.current if r.current is not None else 0.0,
                "temperature":     r.temperature if r.temperature is not None else 0.0,
                "socPercent":      0.0,                                   # required field; redundant không biết SOC → 0
                "cycleCount":      0,
                "sourceDeviceId":  f"{self.cfg.device_code}:{r.sensor_source_code}",
            }
        return {
            "BatteryAssetSerial": r.battery_serial,
            "Time":               r.time_iso,
            "Voltage":            r.voltage,
            "Current":            r.current,
            "Temperature":        r.temperature,
            "SocPercent":         r.soc_percent,
            "SohPercent":         r.soh_percent,
            "CycleCount":         r.cycle_count,
            "ChargingState":      r.charging_state,
            "BmsErrorCode":       r.bms_error_code,
            "SourceType":         r.source_type,                          # 2 = IotGateway
            "SensorSourceCode":   r.sensor_source_code,                   # "redundant" | "external-temp"
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

    # MQTT downlink command
    def _on_mqtt_command(self, cmd: dict) -> None:
        action = cmd.get("action", "")
        cmd_id = cmd.get("cmdId", "")
        result = {"cmdId": cmd_id, "status": "ok", "message": ""}
        try:
            if action == "set_interval":
                self.backend_cfg.ingest_interval_s = max(1, int(cmd.get("value", 15)))
                result["message"] = f"ingest_interval_s = {self.backend_cfg.ingest_interval_s}"
            elif action == "set_scenario":
                self.state.scenario = str(cmd.get("value", "normal"))
                self._triggers = _ScenarioTriggers()
                result["message"] = f"scenario = {self.state.scenario}"
            elif action == "request_heartbeat":
                self._send_heartbeat(time.time())
                result["message"] = "heartbeat sent"
            elif action == "trigger_ota":
                result["message"] = "OTA simulated (no-op)"
            else:
                result["status"] = "failed"
                result["message"] = f"unknown action {action}"
        except (ValueError, KeyError) as ex:
            result["status"] = "failed"
            result["message"] = str(ex)
        if self._mqtt:
            self._mqtt.publish_cmd_ack(result)


def _iso_now(skew_min: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(minutes=skew_min)
    return t.isoformat().replace("+00:00", "Z")
