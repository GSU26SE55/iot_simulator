"""Nạp + kiểm tra cấu hình từ `config/seed.yaml` và biến môi trường (env ưu tiên hơn YAML).

Vai trò của file này tương ứng `include/config.h` bên firmware: **chỉ là ĐƯỜNG LUI**. Từ khi
simulator đọc đủ provision response (IOT3-42/49), nguồn chân lý của chu kỳ đo, chu kỳ heartbeat,
siteId, credential MQTT và bảng ánh xạ pin là **backend**, lưu bền vững trong `logs/state/*.json`
(tương đương NVS). Seed chỉ dùng khi state trống.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Contract version
# ─────────────────────────────────────────────────────────────────────────────
# `current`         — contract Sprint 1 (firmware `buildLegacyBatchPayload`, NI §7.4):
#                     body = { "items": [{ "batteryAssetId": "<guid>", "time", "voltage",
#                              "current", "temperature", "socPercent", "cycleCount" }] }
#                     header = X-Api-Key (KHÔNG có X-Device-Code / Idempotency-Key)
#                     KHÔNG có provision / heartbeat / firmware-check / MQTT / ambient / incident.
#                     Chỉ giữ cho backend đời cũ — firmware THẬT không còn dùng nhánh này.
#
# `iot2-production` — contract Sprint 3 production (firmware `buildProductionBatchPayload`):
#                     ⚠ Đây là nhánh DUY NHẤT mà firmware thật chạy, và là nhánh có ĐỦ tính năng.
#                     body = { "items": [{ "batteryAssetSerial", "time", "deviceTimestamp",
#                              "voltage", "current", "temperature", "socPercent", "cycleCount",
#                              "sourceType", "sensorSourceCode",
#                              "sohPercent"?, "chargingState"?, "bmsErrorCode"? }] }
#                     header = X-Api-Key + X-Device-Code + Idempotency-Key
CONTRACT_CURRENT = "current"
CONTRACT_IOT2 = "iot2-production"
VALID_CONTRACTS = {CONTRACT_CURRENT, CONTRACT_IOT2}


@dataclass
class BatteryConfig:
    serial: str
    unit_id: int
    nominal_voltage: float
    nominal_capacity_ah: float
    initial_soc: float
    initial_soh: float
    cycle_count: int
    chemistry: str = "LiFePO4"
    # Guid của BatteryAsset — BẮT BUỘC khi contract=current (contract đó định danh pin bằng Guid).
    # Contract production dùng `batteryAssetSerial`, backend tự resolve → Guid chỉ là đường lui.
    battery_asset_id: str = ""


@dataclass
class SensorToggles:
    ina226: bool = True
    ds18b20: bool = True
    sht31: bool = False
    mq2: bool = False
    water_leak: bool = False


@dataclass
class SensorDrift:
    voltage_v: float = 0.05
    temperature_c: float = 1.0


@dataclass
class SensorTuning:
    """Tham số cảm biến — mặc định LẤY ĐÚNG `include/config.h` của firmware."""

    sht31_poll_interval_s: float = 60.0        # SHT31_POLL_INTERVAL_MS
    mq2_threshold_raw: int = 2000              # MQ2_THRESHOLD_RAW
    mq2_warmup_s: float = 30.0                 # MQ2_WARMUP_MS
    mq2_poll_interval_s: float = 1.0           # MQ2_POLL_INTERVAL_MS
    mq2_rearm_cooldown_s: float = 300.0        # MQ2_REARM_COOLDOWN_MS
    water_leak_poll_interval_s: float = 0.5    # WATER_LEAK_POLL_INTERVAL_MS
    water_leak_rearm_cooldown_s: float = 300.0  # WATER_LEAK_REARM_COOLDOWN_MS


@dataclass
class DeviceConfig:
    device_code: str
    site_id_guid: str          # Guid Site — đường lui; provision response sẽ ghi đè
    site_label: str
    firmware_version: str
    hardware_revision: str
    model: str
    api_key: str
    batteries: list[BatteryConfig]
    sensors: SensorToggles
    scenario: str = "normal"
    sensor_drift: SensorDrift = field(default_factory=SensorDrift)
    sensor_tuning: SensorTuning = field(default_factory=SensorTuning)
    ntp_server: str = "time.google.com"


@dataclass
class BackendConfig:
    base_url: str
    tls_verify: bool
    heartbeat_interval_s: int
    ingest_interval_s: int
    batch_size_per_battery: int
    contract_version: str
    retry_base_s: float
    retry_max_s: float
    retry_jitter_pct: float
    http_timeout_s: float = 15.0
    # OTA — mặc định khớp `include/config.h`.
    ota_enabled: bool = True
    ota_check_interval_s: float = 3600.0       # OTA_CHECK_INTERVAL_MS
    ota_warmup_s: float = 30.0                 # chờ mạng/giờ/provision ổn định sau boot
    ota_health_timeout_s: float = 120.0        # OTA_HEALTH_TIMEOUT_MS
    ota_download_timeout_s: float = 20.0       # OTA_HTTP_TIMEOUT_MS
    ota_max_boot_attempts: int = 5             # OTA_MAX_BOOT_ATTEMPTS
    ota_max_version_fails: int = 3             # OTA_MAX_VERSION_FAILS
    # IOT3-49 — có dùng `batteryMappings[]` backend trả về làm nguồn chân lý không.
    # Để `false` nếu backend trả bảng khác với seed và bạn muốn giữ seed (chỉ dùng khi gỡ rối).
    apply_battery_map: bool = True


@dataclass
class MqttConfig:
    enabled: bool
    host: str
    port: int
    tls: bool
    topic_prefix: str          # GỐC topic (vd "solar"); tiền tố đầy đủ = "<gốc>/<mã thiết bị>"
    qos: int
    username: str = ""
    password: str = ""
    keepalive_s: int = 30                # MQTT_KEEPALIVE_SEC
    max_packet_size: int = 4096          # MQTT_MAX_PACKET_SIZE
    reconnect_interval_s: float = 5.0    # MQTT_RECONNECT_INTERVAL_MS
    auth_fail_threshold: int = 5         # kAuthFailThreshold (IOT3-44)
    publish_fail_threshold: int = 3      # MQTT_PUBLISH_FAIL_THRESHOLD (S4-FW-06)


@dataclass
class SimulatorConfig:
    backend: BackendConfig
    mqtt: MqttConfig
    devices: list[DeviceConfig]
    queue_dir: Path
    state_dir: Path
    log_level: str
    persist_state: bool = True
    # Danh mục pin dùng CHUNG — tra cứu khi backend giao một serial không có trong
    # `devices[].batteries`. Xem `battery_catalog` trong seed.yaml để biết vì sao cần.
    battery_catalog: list[BatteryConfig] = field(default_factory=list)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config(seed_path: Path | str | None = None) -> SimulatorConfig:
    seed_path = Path(seed_path or os.getenv("IOT_SEED_FILE", "config/seed.yaml"))
    if not seed_path.exists():
        raise FileNotFoundError(f"Không tìm thấy seed file: {seed_path}")

    raw: dict[str, Any] = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}

    be_raw = raw.get("backend", {}) or {}
    contract = os.getenv("IOT_CONTRACT_VERSION", be_raw.get("contract_version", CONTRACT_IOT2))
    if contract not in VALID_CONTRACTS:
        raise ValueError(f"contract_version='{contract}' không hợp lệ. Chọn: {sorted(VALID_CONTRACTS)}")

    backend = BackendConfig(
        base_url=os.getenv("IOT_BASE_URL", be_raw.get("base_url", "https://localhost:7200")),
        tls_verify=_env_bool("IOT_TLS_VERIFY", be_raw.get("tls_verify", True)),
        heartbeat_interval_s=int(be_raw.get("heartbeat_interval_s", 60)),
        ingest_interval_s=int(be_raw.get("ingest_interval_s", 5)),
        batch_size_per_battery=int(be_raw.get("batch_size_per_battery", 3)),
        contract_version=contract,
        retry_base_s=float(be_raw.get("retry_base_s", 2)),
        retry_max_s=float(be_raw.get("retry_max_s", 300)),
        retry_jitter_pct=float(be_raw.get("retry_jitter_pct", 20)),
        http_timeout_s=float(be_raw.get("http_timeout_s", 15)),
        ota_enabled=_env_bool("IOT_OTA_ENABLED", be_raw.get("ota_enabled", True)),
        ota_check_interval_s=float(be_raw.get("ota_check_interval_s", 3600)),
        ota_warmup_s=float(be_raw.get("ota_warmup_s", 30)),
        ota_health_timeout_s=float(be_raw.get("ota_health_timeout_s", 120)),
        ota_download_timeout_s=float(be_raw.get("ota_download_timeout_s", 20)),
        ota_max_boot_attempts=int(be_raw.get("ota_max_boot_attempts", 5)),
        ota_max_version_fails=int(be_raw.get("ota_max_version_fails", 3)),
        apply_battery_map=bool(be_raw.get("apply_battery_map", True)),
    )

    mq_raw = raw.get("mqtt", {}) or {}
    mqtt = MqttConfig(
        enabled=_env_bool("IOT_MQTT_ENABLED", mq_raw.get("enabled", False)),
        host=os.getenv("IOT_MQTT_HOST", mq_raw.get("host", "localhost")),
        port=int(os.getenv("IOT_MQTT_PORT", str(mq_raw.get("port", 1883)))),
        tls=_env_bool("IOT_MQTT_TLS", mq_raw.get("tls", False)),
        topic_prefix=mq_raw.get("topic_prefix", "solar"),
        qos=int(mq_raw.get("qos", 0)),
        username=os.getenv("IOT_MQTT_USERNAME", mq_raw.get("username", "")),
        password=os.getenv("IOT_MQTT_PASSWORD", mq_raw.get("password", "")),
        keepalive_s=int(mq_raw.get("keepalive_s", 30)),
        max_packet_size=int(mq_raw.get("max_packet_size", 4096)),
        reconnect_interval_s=float(mq_raw.get("reconnect_interval_s", 5)),
        auth_fail_threshold=int(mq_raw.get("auth_fail_threshold", 5)),
        publish_fail_threshold=int(mq_raw.get("publish_fail_threshold", 3)),
    )

    default_key = os.getenv("IOT_API_KEY", "")
    devices: list[DeviceConfig] = []
    for d in raw.get("devices", []) or []:
        batteries = [BatteryConfig(**b) for b in d.get("batteries", []) or []]
        sensors = SensorToggles(**(d.get("sensors", {}) or {}))
        drift_raw = d.get("sensor_drift", {}) or {}
        drift = SensorDrift(
            voltage_v=float(drift_raw.get("voltage_v", 0.05)),
            temperature_c=float(drift_raw.get("temperature_c", 1.0)),
        )
        tuning_raw = d.get("sensor_tuning", {}) or {}
        tuning = SensorTuning(**tuning_raw) if tuning_raw else SensorTuning()

        devices.append(DeviceConfig(
            device_code=d["device_code"],
            site_id_guid=d.get("site_id_guid", ""),
            site_label=d.get("site_label", "site-demo-01"),
            firmware_version=d.get("firmware_version", "1.0.0-sim"),
            hardware_revision=d.get("hardware_revision", "ESP32-S3-DevKitC-1-N16R8"),
            model=d.get("model", "ESP32-WROOM-S3"),
            api_key=d.get("api_key") or default_key,
            batteries=batteries,
            sensors=sensors,
            scenario=d.get("scenario", "normal"),
            sensor_drift=drift,
            sensor_tuning=tuning,
            ntp_server=d.get("ntp_server", "time.google.com"),
        ))

    if not devices:
        raise ValueError("Seed YAML không có device nào. Thêm vào devices: ...")

    missing_keys = [d.device_code for d in devices if not d.api_key]
    if missing_keys:
        raise ValueError(
            f"Devices thiếu api_key: {missing_keys}. "
            "Đặt env IOT_API_KEY hoặc devices[].api_key trong seed.yaml. "
            "API key sinh từ admin endpoint POST /api/v1/admin/iot-devices (chỉ trả 1 lần)."
        )

    missing_batteries = [d.device_code for d in devices if not d.batteries]
    if missing_batteries:
        raise ValueError(
            f"Devices không có pin nào trong seed: {missing_batteries}. "
            "Seed là bảng pin ĐƯỜNG LUI khi backend chưa trả batteryMappings[]; "
            "để trống thì lần chạy đầu (chưa provision) sẽ không gửi được gì."
        )

    if contract == CONTRACT_CURRENT:
        bad = [f"{d.device_code}/{b.serial}"
               for d in devices for b in d.batteries if not b.battery_asset_id]
        if bad:
            raise ValueError(
                f"contract_version=current bắt buộc batteries[].battery_asset_id (Guid). Thiếu: {bad}. "
                "Lấy Guid từ /api/battery-assets rồi điền vào seed.yaml, "
                "hoặc đổi contract_version: iot2-production (contract mà firmware thật dùng)."
            )

    catalog = [BatteryConfig(**b) for b in raw.get("battery_catalog", []) or []]

    return SimulatorConfig(
        backend=backend,
        mqtt=mqtt,
        devices=devices,
        battery_catalog=catalog,
        queue_dir=Path(os.getenv("IOT_QUEUE_DIR", "logs/queue")),
        state_dir=Path(os.getenv("IOT_STATE_DIR", "logs/state")),
        log_level=os.getenv("IOT_LOG_LEVEL", "INFO"),
        persist_state=_env_bool("IOT_PERSIST_STATE", raw.get("persist_state", True)),
    )
