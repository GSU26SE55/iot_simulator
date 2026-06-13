"""Load + validate config từ seed.yaml và env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Contract version
# ─────────────────────────────────────────────────────────────────────────────
# `current`        — contract đã merge trong backend HÔM NAY (api-battery.md `POST /api/sensor-readings/batch`):
#                    body = { "items": [{ "batteryAssetId": "<guid>", ..., "sourceDeviceId": "<string>" }] }
#                    header = X-Api-Key (KHÔNG có X-Device-Code, KHÔNG có Idempotency-Key, KHÔNG có DeviceTimestamp wrapper)
# `iot2-production`— production contract Sprint IoT-2 #IoT2-14 (chưa merge):
#                    body = { "DeviceTimestamp": "...", "Readings": [{ "BatteryAssetSerial": "...", "SourceType": 1|2,
#                             "SensorSourceCode": "primary|redundant|external-temp", "BmsErrorCode": "≤64chars", ... }] }
#                    header = X-Api-Key + X-Device-Code + Idempotency-Key
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
    # Guid của BatteryAsset trong DB backend — BẮT BUỘC khi contract_version=current vì
    # backend chấp nhận `items[].batteryAssetId` Guid (chưa biết mapping serial → assetId).
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
class DeviceConfig:
    device_code: str
    site_id_guid: str          # Guid của Site (cho /api/ambient + /api/environmental-incidents)
    site_label: str            # tên ngắn site (dùng cho topic MQTT)
    firmware_version: str
    hardware_revision: str
    model: str
    api_key: str
    batteries: list[BatteryConfig]
    sensors: SensorToggles
    scenario: str = "normal"
    sensor_drift: SensorDrift = field(default_factory=SensorDrift)


@dataclass
class BackendConfig:
    base_url: str
    tls_verify: bool
    heartbeat_interval_s: int
    ingest_interval_s: int
    batch_size_per_battery: int      # số reading/battery/tick (3 = BMS+INA226+DS18B20)
    contract_version: str            # "current" | "iot2-production"
    retry_base_s: float
    retry_max_s: float
    retry_jitter_pct: float


@dataclass
class MqttConfig:
    enabled: bool
    host: str
    port: int
    tls: bool
    topic_prefix: str
    qos: int
    username: str = ""
    password: str = ""


@dataclass
class SimulatorConfig:
    backend: BackendConfig
    mqtt: MqttConfig
    devices: list[DeviceConfig]
    queue_dir: Path
    log_level: str


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config(seed_path: Path | str | None = None) -> SimulatorConfig:
    """Đọc seed YAML + override từ env. Env ưu tiên hơn YAML."""
    seed_path = Path(seed_path or os.getenv("IOT_SEED_FILE", "config/seed.yaml"))
    if not seed_path.exists():
        raise FileNotFoundError(f"Không tìm thấy seed file: {seed_path}")

    raw: dict[str, Any] = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}

    be_raw = raw.get("backend", {})
    contract = os.getenv("IOT_CONTRACT_VERSION", be_raw.get("contract_version", CONTRACT_CURRENT))
    if contract not in VALID_CONTRACTS:
        raise ValueError(f"contract_version='{contract}' không hợp lệ. Chọn: {sorted(VALID_CONTRACTS)}")

    backend = BackendConfig(
        base_url=os.getenv("IOT_BASE_URL", be_raw.get("base_url", "https://localhost:7200")),
        tls_verify=_env_bool("IOT_TLS_VERIFY", be_raw.get("tls_verify", True)),
        heartbeat_interval_s=int(be_raw.get("heartbeat_interval_s", 60)),
        ingest_interval_s=int(be_raw.get("ingest_interval_s", 15)),
        batch_size_per_battery=int(be_raw.get("batch_size_per_battery", 3)),
        contract_version=contract,
        retry_base_s=float(be_raw.get("retry_base_s", 2)),
        retry_max_s=float(be_raw.get("retry_max_s", 300)),
        retry_jitter_pct=float(be_raw.get("retry_jitter_pct", 20)),
    )

    mq_raw = raw.get("mqtt", {})
    mqtt = MqttConfig(
        enabled=_env_bool("IOT_MQTT_ENABLED", mq_raw.get("enabled", False)),
        host=os.getenv("IOT_MQTT_HOST", mq_raw.get("host", "localhost")),
        port=int(os.getenv("IOT_MQTT_PORT", str(mq_raw.get("port", 1883)))),
        tls=_env_bool("IOT_MQTT_TLS", mq_raw.get("tls", False)),
        topic_prefix=mq_raw.get("topic_prefix", "solar"),
        qos=int(mq_raw.get("qos", 1)),
        username=os.getenv("IOT_MQTT_USERNAME", mq_raw.get("username", "")),
        password=os.getenv("IOT_MQTT_PASSWORD", mq_raw.get("password", "")),
    )

    default_key = os.getenv("IOT_API_KEY", "")
    devices: list[DeviceConfig] = []
    for d in raw.get("devices", []):
        batteries = [BatteryConfig(**b) for b in d.get("batteries", [])]
        sensors = SensorToggles(**d.get("sensors", {}))
        drift_raw = d.get("sensor_drift", {})
        drift = SensorDrift(
            voltage_v=float(drift_raw.get("voltage_v", 0.05)),
            temperature_c=float(drift_raw.get("temperature_c", 1.0)),
        )
        devices.append(DeviceConfig(
            device_code=d["device_code"],
            site_id_guid=d["site_id_guid"],
            site_label=d.get("site_label", "site-demo-01"),
            firmware_version=d.get("firmware_version", "1.0.0-sim"),
            hardware_revision=d.get("hardware_revision", "ESP32-S3-DevKitC-1"),
            model=d.get("model", "ESP32-WROOM-S3"),
            api_key=d.get("api_key") or default_key,
            batteries=batteries,
            sensors=sensors,
            scenario=d.get("scenario", "normal"),
            sensor_drift=drift,
        ))

    if not devices:
        raise ValueError("Seed YAML không có device nào. Thêm vào devices: ...")

    missing_keys = [d.device_code for d in devices if not d.api_key]
    if missing_keys:
        raise ValueError(
            f"Devices thiếu api_key: {missing_keys}. "
            "Đặt env IOT_API_KEY hoặc devices[].api_key trong seed.yaml. "
            "API key sinh từ admin endpoint POST /api/v1/admin/iot-devices (chỉ trả 1 lần — copy ngay)."
        )

    # Validate Guid present in current mode
    if contract == CONTRACT_CURRENT:
        bad = []
        for d in devices:
            for b in d.batteries:
                if not b.battery_asset_id:
                    bad.append(f"{d.device_code}/{b.serial}")
        if bad:
            raise ValueError(
                f"contract_version=current bắt buộc batteries[].battery_asset_id (Guid). Thiếu: {bad}. "
                "Lấy Guid từ /api/battery-assets list rồi điền vào seed.yaml. "
                "Nếu chưa có asset → POST tạo trước, hoặc đổi contract_version: iot2-production."
            )

    return SimulatorConfig(
        backend=backend,
        mqtt=mqtt,
        devices=devices,
        queue_dir=Path(os.getenv("IOT_QUEUE_DIR", "logs/queue")),
        log_level=os.getenv("IOT_LOG_LEVEL", "INFO"),
    )
