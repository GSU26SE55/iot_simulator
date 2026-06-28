# IoT Simulator — Solar Battery (ESP32-S3 mock)

Simulator Python mô phỏng **đầy đủ** chức năng firmware ESP32-S3 dự án IoT Capstone GSU26SE55, dùng khi **chưa có hardware thật**. Chạy end-to-end các luồng vận hành B1–B7 (`overall.iot.md`) và contract Sprint IoT-1 → IoT-6 mà không cần pin/BMS/MAX485.

> **Phạm vi:** thay phần `Site / BMS / ESP32` trong sơ đồ `newiot.md §4` bằng simulator này. Backend (`BatteryService`), broker (EMQX/Mosquitto), DB (TimescaleDB), TicketService, NotificationService chạy thật như môi trường dev.

---

## ⚠ Quan trọng: chọn `contract_version`

Backend `BatteryService` đang ở giữa quá trình migrate Sprint IoT-2. Có 2 contract:

| Mode | Endpoint hoạt động hôm nay | Khi nào dùng |
|---|---|---|
| **`current`** (mặc định) | `POST /api/sensor-readings/batch` với `items[].batteryAssetId` (Guid) **theo Sprint 1 legacy contract** (`tasksprint.md` S1-FW-05 + `newiot.md §7.4`). Header: `X-Api-Key` only. KHÔNG có `sourceDeviceId`, `X-Device-Code`, `Idempotency-Key`, `DeviceTimestamp` wrapper, `SourceType` field. | Backend hôm nay accept legacy. Yêu cầu `batteries[].battery_asset_id` Guid trong `seed.yaml`. |
| **`iot2-production`** | `{ items[]: { batteryAssetSerial, time, deviceTimestamp, voltage, current, temperature, socPercent, cycleCount, sourceType, sensorSourceCode, sohPercent?, chargingState?, bmsErrorCode? } }` — **KHỚP firmware `buildProductionBatchPayload`**. Header thêm `X-Device-Code` + `Idempotency-Key`. + endpoint `/api/iot-devices/{provision,heartbeat,firmware-check,firmware-update-log}` + MQTT per-pin. | Production contract (S3-FW-04). Dùng `batteryAssetSerial` thay Guid. ⚠ Xem audit #10 — backend còn 1 bug idempotency chặn ingest. |

Đổi bằng `backend.contract_version` trong `seed.yaml` hoặc env `IOT_CONTRACT_VERSION=iot2-production`.

---

## Cấu trúc

```
iot-simulator/
├── README.md                ← file này
├── Makefile                 ← venv / run / test / provision
├── requirements.txt
├── env.example.txt          ← copy thành .env
├── config/
│   └── seed.yaml            ← devices + batteries + scenarios
├── scripts/
│   ├── run.sh               ← quick start
│   └── provision_devices.py ← gọi admin POST /api/v1/admin/iot-devices (chỉ contract iot2)
├── src/
│   ├── main.py              ← CLI entry — python -m src.main
│   ├── config.py            ← load seed.yaml + contract_version validator
│   ├── bms.py               ← MockBattery: SOC/OCV/heat physics + 11 scenarios, pinned timestamp
│   ├── sensors/
│   │   ├── redundant.py     ← INA226 (V/I) + DS18B20 (temp) — sourceType=IotGateway
│   │   ├── ambient.py       ← SHT31 → /api/ambient/readings/batch  (source=1 IotSensor)
│   │   └── environmental.py ← MQ-2 smoke / fire / gas leak + water flood → /api/environmental-incidents (int enums)
│   ├── http_client.py       ← HTTPS REST — header & path theo contract + firmware-update-log PUT
│   ├── mqtt_client.py       ← MQTT (paho-mqtt) — LWT + telemetry per-pin/heartbeat/status/cmd/cmd-ack
│   ├── queue.py             ← Local JSONL queue + endpoint routing khi flush
│   ├── ota.py               ← OTA decision (ota_should_update) + runner lifecycle (S7)
│   ├── device.py            ← SimulatedDevice — vòng đời ESP32 + scenarios + cmd downlink + OTA + LED
│   └── dashboard.py         ← Rich live dashboard (LED + FW + OTA cols)
├── tests/  (44 tests — 100% PASS)
│   ├── test_bms.py          ← 11 scenarios + pinned timestamp + ChargingState enum
│   ├── test_payload.py      ← cả 2 contract (items[] camelCase) + cross-source + ambient + incident
│   └── test_features.py     ← MQTT topic/command schema + provision-apply + OTA decision/lifecycle
└── logs/queue/              ← JSONL per device (auto-create)
```

---

## Coverage matrix — đối chiếu với tasksprint.md + api-battery.md

| Spec đầu vào | Implementation |
|---|---|
| **S1-FW-04..07** mock BMS scenarios + HTTPS POST | `bms.py` + `device._tick_ingest` |
| **S2-FW-02/03** provision + heartbeat 60s (chip temp/heap/RSSI/queue depth, Cpu/Disk=null) | `device._do_provision` + `_send_heartbeat` (chỉ `iot2-production`) |
| **S2-FW-04** header `X-Api-Key`+`X-Device-Code` | `http_client` tự gắn theo `contract_version` |
| **S3-FW-01** local queue (NVS) | `queue.LocalQueue` (JSONL) — flush đúng endpoint khi resume |
| **S3-FW-02** Idempotency-Key UUIDv4 | `device._tick_ingest` gen `uuid.uuid4()`; chỉ gửi nếu contract iot2 |
| **S3-FW-03** exponential backoff + jitter | `device._bump_backoff` (base 2s, max 300s, jitter ±20%) |
| **S3-FW-04** production contract `deviceTimestamp` + `sourceType` per-source + `bmsErrorCode ≤64 chars` | `device._build_ingest_payload` + `_bms_to_dict` + `_gw_to_dict` |
| **S4-FW-01..06** MQTT + LWT + cmd subscribe + cmd/ack publish + fallback HTTPS | `mqtt_client.py` (telemetry per-pin `solar/{dev}/{serial}/telemetry`) + `device._on_mqtt_command` (schema `{cmdId,type,params}` → ack `{cmdId,status,error?}`) + MQTT-first streak threshold=3 |
| **S5-FW-01** BMS reading + `bmsErrorCode` | `bms.py` (OVP/UVP/OTP/OCD/scenario `bms_error`) |
| **S5-FW-04** INA226 `sourceType=2/redundant` | `sensors/redundant.make_ina226_reading` |
| **S5-FW-05** DS18B20 `sourceType=2/external-temp` | `sensors/redundant.make_ds18b20_reading` |
| **S5-FW-06** SHT31 → `/api/ambient/readings/batch` `source=1 IotSensor` | `sensors/ambient.py` + `device._send_ambient` (đúng path & field names api-battery.md) |
| **S6-FW-01** MQ-2 → environmental incident | `sensors/environmental.make_smoke_incident` (int enum) |
| **S6-FW-02** water leak → flood incident | `make_water_leak_incident` → `incidentType=4` (Flood) |
| **S6-FW-03** cross-source pair BMS vs IotGateway cùng phút | pinned `time_iso` per tick — verified bởi `test_pinned_timestamp_across_sources` |
| **S7-FW-01/02** OTA: firmware-check → decision → update-log lifecycle → version bump | `ota.py` (`ota_should_update` + `OtaRunner`) + `device._do_ota_check` mỗi 1h (firmware `OTA_CHECK_INTERVAL_MS`); `trigger_ota` cmd chạy ngay |
| **S2-FW-02** provision response apply (polling/heartbeat/siteId/ntp) | `device._apply_provision_response` (siteId backend-assigned override seed) |
| **LedState** Online/Queued/Offline/Provisioning | `device._update_led` + `dashboard` cột LED |
| **ChargingStateEnum** Idle/Charging/Discharging/Float/Bypass = 1..5 | `bms.py` (sửa từ Full=4 → Float=4) |

---

## Scenario engine — 17 kịch bản, map 1-1 với AnomalyType + EnvironmentalIncidentType backend

| Scenario | Backend enum |
|---|---|
| `normal` | (không trigger) |
| `overheat` | `AnomalyType=1 Overheat` (P1) |
| `overvoltage` | `AnomalyType=2` |
| `undervoltage` | `AnomalyType=3` |
| `low_soc` | `AnomalyType=4` (P2) |
| `rapid_discharge` | `AnomalyType=5` |
| `abnormal_charging` | `AnomalyType=6` |
| `device_offline` | `AnomalyType=7` (sau 60s ngừng heartbeat → backend job 2 phút detect, hoặc MQTT LWT tức thì) |
| `soh_degradation` | `AnomalyType=8` |
| `high_ambient_temp` | `AnomalyType=9` (SHT31 → `/api/ambient/readings/batch`) |
| `high_humidity` | `AnomalyType=10` |
| `high_temp_humidity_combo` | `AnomalyType=11` |
| `sensor_mismatch` | `AnomalyType=15` (INA226 lệch > 0.5V hoặc DS18B20 lệch > 5°C) |
| `smoke` | `EnvironmentalIncidentType=1 Smoke` (Critical) |
| `fire_detected` | `EnvironmentalIncidentType=2 FireDetected` |
| `gas_leak` | `EnvironmentalIncidentType=3 GasLeak` |
| `water_leak` | `EnvironmentalIncidentType=4 Flood` |
| `bms_error` | `bms_error_code = "OVT-PROTECT"` |
| `clock_skew` | `deviceTimestamp` lệch +10 phút → backend reject (Sprint IoT-2 `#IoT2-15`) |

---

## Quick start

```bash
cd iot-simulator
make install                       # tạo venv + cài deps
cp env.example.txt .env            # sửa IOT_BASE_URL + IOT_API_KEY
# (contract current) lấy battery_asset_id từ DB → điền vào config/seed.yaml
make run                           # rich dashboard
# hoặc
python -m src.main --no-dashboard
python -m src.main --once                       # smoke test
python -m src.main --scenario overheat
python -m src.main --device ESP32-SIM-001
```

### Provision device (chỉ contract iot2-production)

Khi backend đã có admin endpoint (Sprint IoT-2 `#IoT2-07`):

```bash
export ADMIN_TOKEN=<JWT admin>
make provision        # in rawApiKey cho mỗi device — copy ngay (1 lần)
```

### Test

```bash
make test     # 26/26 PASS
```

---

## Production contract — payload thực tế

### Mode `current` (Sprint 1 legacy — `tasksprint.md` S1-FW-05 + `newiot.md §7.4`)

```http
POST /api/sensor-readings/batch
X-Api-Key: iotk_xxx
Content-Type: application/json

{
  "items": [{
    "batteryAssetId": "22222222-2222-2222-2222-222222222201",
    "time":           "2026-06-12T10:15:30Z",
    "voltage":        12.94,
    "current":        -1.05,
    "temperature":    29.4,
    "socPercent":     78.5,
    "cycleCount":     120
  }]
}
```

> Mode này khớp **firmware ESP32 Sprint 1** (`capstone/iot/firmware-esp32/src/core/payload.cpp`).
> Sprint 3 chuyển sang `iot2-production` mode để có wrapper `DeviceTimestamp`, `BatteryAssetSerial`, `SourceType`, `SensorSourceCode`, `Idempotency-Key`.

### Mode `iot2-production` — KHỚP firmware `buildProductionBatchPayload` (S3-FW-04)

```http
POST /api/sensor-readings/batch
X-Api-Key: iotk_xxx
X-Device-Code: ESP32-SIM-001
Idempotency-Key: <uuidv4>

{
  "items": [
    { "batteryAssetSerial": "BAT-2026-001",
      "time": "2026-06-12T10:15:30.000Z", "deviceTimestamp": "2026-06-12T10:15:30.000Z",
      "voltage": 12.94, "current": -1.05, "temperature": 29.4,
      "socPercent": 78.5, "cycleCount": 120,
      "sourceType": 1, "sensorSourceCode": "primary",
      "sohPercent": 94.2, "chargingState": 3 },
    { "batteryAssetSerial": "BAT-2026-001",
      "time": "2026-06-12T10:15:30.001Z", "deviceTimestamp": "2026-06-12T10:15:30.000Z",
      "voltage": 12.99, "current": -1.04, "temperature": 0.0, "socPercent": 0.0, "cycleCount": 0,
      "sourceType": 2, "sensorSourceCode": "redundant" },
    { "batteryAssetSerial": "BAT-2026-001",
      "time": "2026-06-12T10:15:30.002Z", "deviceTimestamp": "2026-06-12T10:15:30.000Z",
      "voltage": 0.0, "current": 0.0, "temperature": 35.1, "socPercent": 0.0, "cycleCount": 0,
      "sourceType": 2, "sensorSourceCode": "external-temp" }
  ]
}
```

> ⚠ KHÔNG có wrapper `DeviceTimestamp`/`Readings`. Backend `BatchIngestSensorReadingsCommand`
> chỉ bind `List<SensorReadingItem> Items` (camelCase). `time` stagger theo ms PER-SOURCE để
> tránh trùng PK hypertable `(Time, BatteryAssetId)` (backend khuyến nghị ms-resolution;
> `CrossSourceValidationService` ghép cặp theo cửa sổ 60s nên lệch ms vẫn pair đúng).
> `deviceTimestamp` giữ nguyên 1 mốc/tick cho clock-skew check (#IoT2-15).

### Ambient (`/api/ambient/readings/batch`)

```json
{ "items": [{
    "siteId":             "11111111-1111-1111-1111-111111111111",
    "time":               "2026-06-12T10:15:30Z",
    "ambientTemperature": 34.2,
    "humidity":           72.5,
    "solarIrradiance":    580.0,
    "source":             1,
    "sourceDeviceId":     "ESP32-SIM-001"
}] }
```

### Environmental incident (`/api/environmental-incidents`)

```json
{ "siteId":      "11111111-1111-1111-1111-111111111111",
  "incidentType": 1,                                       // Smoke=1
  "severity":     3,                                       // Critical=3
  "detectedAt":   "2026-06-12T10:15:30Z",
  "reportedBy":   "ESP32-SIM-001",
  "notes":        "MQ-2 smoke ADC=3100 vượt threshold 2500" }
```

---

## Audit log — sửa gì so với bản đầu

Sau khi đối chiếu với `backend/docs/api-battery.md`, đã sửa:

1. **Sensor batch contract** — trước gửi nhầm contract Sprint IoT-2 cho backend hôm nay → 400. Tách 2 mode `current` vs `iot2-production`.
2. **Environmental incident** — sửa `DeviceCode`/`Description`/`SensorReading` → `siteId`+`incidentType`(int)+`severity`(int)+`detectedAt`+`reportedBy`+`notes`.
3. **Ambient endpoint** — sửa `/api/ambient-readings/batch` → `/api/ambient/readings/batch` (đúng path), field `TemperatureC` → `ambientTemperature`, source string → int.
4. **ChargingStateEnum** — sửa `Full=4` thành đúng `Float=4`.
5. **Cross-source timestamp** — pin `time_iso` per tick để BMS + INA226 + DS18B20 có cùng `Time` (yêu cầu §1.6.6).
6. **6 scenario thiếu** — thêm `rapid_discharge`, `abnormal_charging`, `soh_degradation`, `high_ambient_temp`, `high_humidity`, `high_temp_humidity_combo`, `fire_detected`, `gas_leak` → đủ 13/15 AnomalyType + 4/6 EnvironmentalIncidentType.
7. **Battery Guid** — thêm `battery_asset_id` field bắt buộc cho `contract=current`; validator báo lỗi sớm nếu thiếu.
8. **(2026-06-13) Strict Sprint 1 compliance** — sau khi đối chiếu lại `tasksprint.md` Sprint 1 thật kỹ, sửa 6 điểm cho khớp:
   - Bỏ `sourceDeviceId` khỏi `current` mode `_bms_to_dict`/`_gw_to_dict` (`newiot.md §7.4` legacy không có field này; Sprint 3 production sẽ có equivalent qua `sensorSourceCode`+`sourceType`).
   - **Gate `_send_ambient`** (Sprint 5 — S5-FW-06) behind `CONTRACT_IOT2` — Sprint 1 chỉ có mock BMS ingest, KHÔNG có SHT31 ambient.
   - **Gate `_maybe_trigger_environmental`** (Sprint 6 — S6-FW-01/02) behind `CONTRACT_IOT2` — Sprint 1 KHÔNG có MQ-2 smoke / water leak incident.
   - `seed.yaml`: `ingest_interval_s` 15 → **5** (khớp S1-FW-07 "Loop chính mỗi 5s").
   - `seed.yaml`: `batch_size_per_battery` 3 → **1** (firmware Sprint 1 gửi 1 reading/pin/batch).
   - `seed.yaml`: `initial_soc` 78.5/65 → **30/25** cho 2 pin LiFePO4 12V — đưa voltage `OCV(SOC)` vào dải `12.0..13.0V` theo S1-FW-04 spec ("voltage 12.0–13.0V"). Trước đó voltage 13.15-13.31V (ngoài range).

   > Trong mode `iot2-production`, tất cả tính năng Sprint 2–7 vẫn hoạt động (heartbeat, provision, MQTT, ambient, environmental, firmware-check). User có thể tăng `initial_soc` trở lại nếu muốn test high-SOC scenarios.

9. **(2026-06-14) Sprint 2 alignment với firmware ESP32** — sau khi tôi implement Sprint 2 FW (S2-FW-01..04) cho `iot/firmware-esp32/`, sửa simulator để cùng schema với firmware:

   **HTTP route fix (real bug — đã 404 khi chạy với BE thật):**
   - **`/api/v1/iot-devices/{provision,heartbeat,firmware-check}` → `/api/iot-devices/...`** — backend thực dùng route KHÔNG có `/v1/` (đã verify trong `services/BatteryService/src/.../IotDevicesController.cs:[Route("api/iot-devices")]`). Sửa cả `src/http_client.py`, `README.md`, `guideline.md` (5 reference). Trước đó simulator gửi tới route `/v1/` → backend trả 404 → provision/heartbeat im lặng fail.

   **Heartbeat body schema (khớp `IotDeviceHeartbeatCommand` + firmware Sprint 2):**
   - **Bỏ `ConnectedSensorCount`** — KHÔNG có trong backend DTO và firmware ESP32 cũng không gửi. Artifact simulator-only, backend silent ignore.
   - **Bỏ `IpAddress`** — KHÔNG có trong backend DTO và firmware không gửi.
   - **Thêm `FreeMemoryPercent`** — Sprint 2 review pass thứ 2 thêm vào firmware vì `MemoryUsageMb` luôn = 0 trên board ESP32-S3 N8 (no PSRAM). Backend command có `decimal? FreeMemoryPercent`.
   - **Đổi `MemoryUsageMb` từ float → int** — backend field là `long?`. System.Text.Json strict-mode reject float cho integer type. Trước đó `round(x, 1)` = float `156.7` → fix sang `int(round(x))` = `157`.

   > Heartbeat body cuối khớp 1:1 với firmware Sprint 2: `FirmwareVersion`, `Temperature`, `MemoryUsageMb` (int), `FreeMemoryPercent`, `SignalStrengthDbm`, `LocalQueueDepth`, `UptimeSeconds`, `DeviceTimestamp`, `Cpu`=null, `DiskFreeMb`=null.

   **Provision flow — design choice khác (chấp nhận):**
   - Firmware ESP32: provision **1 lần và persist NVS flag `provd=1`** → idempotent skip lần boot sau.
   - Simulator: provision **mỗi lần start** (không persist state qua restart) — Python process restart = clean state.
   - Backend handler `DeviceLifecycleHandlers.cs::Handle(ProvisionIotDeviceCommand)` đã verified idempotent (re-provision device Active vẫn trả OK + config) → cả 2 approach đều work với BE.

10. **(2026-06-26) Full parity với firmware ESP32 "complete" (commit `ec68591`)** — sau khi đọc lại TOÀN BỘ `iot/firmware-esp32/src/` (13 module) + verify trực tiếp DTO backend, sửa các điểm lệch giữa simulator và firmware đã hoàn thiện:

    **🔴 Production ingest contract — bug đúng/sai (sẽ bị backend 400):**
    - Trước: `iot2-production` gửi `{ "DeviceTimestamp": ..., "Readings": [{ PascalCase }] }`.
    - Backend `BatchIngestSensorReadingsCommand` chỉ bind `List<SensorReadingItem> Items` — KHÔNG có wrapper → `Items` rỗng → 400 "Danh sách readings là bắt buộc". Firmware `core/payload.cpp::buildProductionBatchPayload` gửi `{ "items": [{ camelCase, deviceTimestamp per-item, sourceType, sensorSourceCode, sohPercent?, chargingState?, bmsErrorCode? }] }`.
    - **Đã viết lại** `_build_ingest_payload`/`_bms_to_dict`/`_gw_to_dict` khớp 1:1 firmware + backend DTO. Gateway (INA226/DS18B20) emit `0.0` cho field không đo (backend Voltage/Current/Temperature non-nullable decimal). Sửa 3 unit test assert contract cũ sai.

    **🟠 MQTT (S4) — khớp `net/mqtt_client.cpp` + `cmd/`:**
    - Telemetry topic `solar/{siteId}/{dev}/telemetry` (cả batch) → `solar/{dev}/{serial}/telemetry` **per-pin** (firmware `mqttPublishTelemetry` group-by-serial). Broker ACL `solar/%u/+/telemetry`.
    - Command schema `{action, value}` → `{cmdId, type, params.pollingSeconds|pollingIntervalSeconds}`; ack `{cmdId, status, message}` → `{cmdId, status, error?}` (firmware `cmd_logic.cpp` + `command_handler.cpp`). type case-insensitive + `_`/`-`. Thêm MQTT-first streak threshold=3 + reset khi reconnect (S4-FW-06).

    **🟠 Provision/Heartbeat/OTA-check:**
    - `_do_provision` giờ parse `CommonResponse.data` → áp dụng `pollingIntervalSeconds`/`heartbeatIntervalSeconds`/`siteId`/`ntpServer` (firmware `provision.cpp`). **siteId backend-assigned override seed** → ambient + environmental incident dùng đúng site.
    - Heartbeat **HTTP-only** (firmware `heartbeat.cpp::sendOnce` dùng `httpPostJson`, KHÔNG qua MQTT). Trước simulator ưu tiên MQTT.
    - firmware-check `6h → 1h` (firmware `OTA_CHECK_INTERVAL_MS = 3600000`).

    **🟡 Tính năng mới (firmware có, simulator thiếu):**
    - **OTA Sprint 7** (`src/ota.py`): `ota_should_update` (so version chuỗi tuyệt đối — `ota_decision.h`) + `OtaRunner` lifecycle `firmware-check → PUT firmware-update-log {Downloading→Installing→Success} → bump version`. `trigger_ota` cmd chạy thật 1 chu kỳ. (Không tải .bin/SHA/rollback partition — cần hardware thật.)
    - **LED state** Online/Queued/Offline/Provisioning (`led_palette.h`) trên dashboard.

    **⚠ Phát hiện BUG BACKEND (chặn iot2 ingest — KHÔNG phải lỗi simulator):**
    - Verify trực tiếp với BE dev đang chạy (`:4001`): legacy `current` ingest ✅ persist (sent=1); iot2 provision/heartbeat/firmware-check ✅ 200. NHƯNG iot2 **sensor-readings/batch ingest → 500**:
      `23505: duplicate key value violates unique constraint "PK_sensor_ingest_idempotency_records"`.
    - Root cause: `SensorIngestIdempotencyRecord.Id` **không được sinh** → mọi insert dùng `id = 00000000-...-000000000000`. Insert ĐẦU TIÊN ok, mọi insert sau trùng PK Guid.Empty. DB xác nhận có đúng 1 row id=Guid.Empty.
    - File: `backend/.../CQRS/Handler/SensorReading/BatchIngestSensorReadingsCommandHandler.cs` (~dòng 348) — `new SensorIngestIdempotencyRecord { ... }` thiếu `Id = Guid.NewGuid()` (hoặc cấu hình `ValueGeneratedOnAdd`).
    - **Firmware thật cũng dính** (luôn gửi `Idempotency-Key` cho ingest). Simulator gửi đúng UUID key + `X-Device-Code` theo contract → đã đúng; queue+backoff xử lý 500 mượt. Cần fix backend ở repo backend (ngoài scope task simulator này).

    > Tóm tắt verify (BE dev `:4001`): provision 200 ✅ · heartbeat 200 ✅ · firmware-check 200 ✅ · legacy ingest persist ✅ · iot2 ingest blocked-by-BE-bug ⚠ · 44/44 unit test ✅.

---

## Ràng buộc

- Không implement Energy/CO2/kWh dashboard (ADR-017 / `tasksprint §0`). INA226 chỉ cross-source validation.
- BMS reading luôn `SourceType=1`, IotGateway luôn `SourceType=2` (rủi ro số 11 trong risk register).
- Khi demo hội đồng KLTN, chuyển sang firmware ESP32 thật (Sprint S5+) — file này dùng cho S1→S4 + unit/integration test khi không có pin.
