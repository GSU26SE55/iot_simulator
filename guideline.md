# IoT Simulator — Guideline đầy đủ

> Hướng dẫn step-by-step để cài đặt, cấu hình và chạy được `iot-simulator` trong dự án **Solar Battery Maintenance Management System (GSU26SE55)**.
> Đối tượng: thành viên team BE / FE / AI / QA cần demo end-to-end pipeline IoT khi chưa có hardware ESP32 thật.
> Cập nhật: **2026-06-26** — simulator nâng full parity với firmware ESP32 "complete" (commit `ec68591`). Verify end-to-end LIVE cả 2 contract (`current` + `iot2-production`): provision → ingest multi-source → heartbeat → ambient → firmware-check/OTA → environmental incident. 44/44 unit test PASS.
>
> ⚠ **Hai fix backend bắt buộc cho `iot2-production`** (xem [§2.5](#25-bắt-buộc-cho-iot2-production--2-fix-backend--scope-environmentalingest)): nếu backend chạy bản chưa có 2 fix này thì iot2 ingest + ambient/incident sẽ **500**.

---

## Mục lục

- [1. Bức tranh tổng thể](#1-bức-tranh-tổng-thể)
- [2. Prerequisites](#2-prerequisites)
- [3. Setup môi trường — 5 bước](#3-setup-môi-trường--5-bước)
- [4. Lấy API key từ backend](#4-lấy-api-key-từ-backend-cách-curl-đầy-đủ)
- [5. Cấu hình seed.yaml + .env](#5-cấu-hình-seedyaml--env)
- [6. Smoke test + chạy thật](#6-smoke-test--chạy-thật)
- [7. Verify trong DB + qua API](#7-verify-trong-db--qua-api)
- [8. 19 scenario — bảng đầy đủ](#8-19-scenario--bảng-đầy-đủ)
- [9. Demo end-to-end các luồng B1–B7](#9-demo-end-to-end-các-luồng-b1b7)
- [10. Troubleshooting — các lỗi đã gặp](#10-troubleshooting--các-lỗi-đã-gặp)
- [11. Thiết kế nội bộ + 2 contract version](#11-thiết-kế-nội-bộ--2-contract-version)
- [12. Bảo trì — rotate key, đổi scenario runtime, reset queue](#12-bảo-trì--rotate-key-đổi-scenario-runtime-reset-queue)

---

## 1. Bức tranh tổng thể

```
┌────────────────────────────────────────────────────────────────────────────┐
│                       SOLAR BATTERY IOT PIPELINE                            │
└────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────┐
│  iot-simulator      │   (chạy local Python)
│  - SimulatedDevice  │   - Mỗi instance = 1 ESP32-S3 node mock
│  - MockBattery      │   - Sinh sensor reading vật lý theo scenario
│  - LocalQueue       │   - Buffer JSONL khi backend down
│  - Dashboard (rich) │   - Hiển thị state live
└──────────┬──────────┘
           │ HTTPS REST (X-Api-Key)
           ▼
┌─────────────────────┐
│  ApiGateway:4001    │   YARP reverse proxy
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  BatteryService:4006│   - Validate API key (X-Api-Key)
│                     │   - Apply calibration (nếu có)
│                     │   - INSERT sensor_readings (hypertable)
│                     │   - UPDATE battery_assets.last_sensor_reading_at
│                     │   - UPDATE iot_devices.last_seen_at
│                     │   - Trigger anomaly detection
└──────────┬──────────┘
           ├──────► TimescaleDB (battery_db / sensor_readings hypertable)
           ├──────► alerts table (khi reading vượt threshold)
           └──────► outbox_messages → RabbitMQ
                       ├──► TicketService (auto-tạo Ticket P1/P2/P3)
                       └──► NotificationService (push/email Customer/Staff)
```

**Simulator này thay thế ESP32 thật trong sơ đồ `newiot.md §4`.** Backend xử lý y hệt như khi nhận từ ESP32 thật — không có code path đặc biệt cho mock.

---

## 2. Prerequisites

### 2.1. Phần mềm

| Tool | Version | Cài đặt |
|---|---|---|
| Python | 3.10+ (test trên 3.14) | `brew install python@3.12` |
| Docker + Docker Compose | Bất kỳ | Docker Desktop |
| `psql` client | bất kỳ | `brew install libpq && brew link --force libpq` |
| `curl` + `jq` (optional, đọc JSON đẹp) | bất kỳ | `brew install jq` |

### 2.2. Backend stack đang chạy

Trước khi setup simulator, đảm bảo stack `solar-*` đang chạy:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep solar
```

Kết quả phải thấy ít nhất:

| Container | Port host |
|---|---|
| `solar-apigateway` | 4001 |
| `solar-authservice` | 4002 |
| `solar-batteryservice` | 4006 |
| `solar-ticketservice` | 4007 |
| `solar-notificationservice` | 4008 |
| `solar-postgres` | **5433** (không phải 5432 — port 5432 là TimescaleDB riêng cho IoT track) |
| `solar-mosquitto` | 21883 (MQTT, nếu cần) |

Nếu chưa chạy → vào `backend/` và `docker compose up -d`.

### 2.3. Tài khoản admin

Default seed (xem `AuthService/Persistence/Seeders/AuthDataSeeder.cs`):

```
Email:    admin@yourdomain.com
Password: Admin123@
```

### 2.4. Postgres credentials

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` (port của solar-postgres, KHÔNG phải 5432) |
| User | `postgres` |
| Password | `Password12345@` |
| DB BatteryService | `battery_db` |

### 2.5. (Bắt buộc cho `iot2-production`) — 2 fix backend + scope EnvironmentalIngest

> Mode `current` (legacy) chạy được ngay, KHÔNG cần phần này. Chỉ đọc khi dùng `contract_version: iot2-production`.

**(a) 2 fix backend** — phát hiện 2026-06-26 khi verify simulator. Nếu backend của bạn build từ branch chưa có 2 fix này → iot2 ingest + ambient/incident trả **500** (firmware ESP32 thật cũng dính y hệt):

| Bug | Triệu chứng | Fix (file backend) |
|---|---|---|
| `SensorIngestIdempotencyRecord.Id` không sinh (PK `ValueGeneratedNever`) | `POST /api/sensor-readings/batch` có `Idempotency-Key` → request thứ 2 trở đi **500** `duplicate key PK_sensor_ingest_idempotency_records` | `BatchIngestSensorReadingsCommandHandler.cs` — thêm `Id = Guid.NewGuid()` |
| Named policy `EnvironmentalIngest` chưa đăng ký | ambient + environmental-incidents → **500** `AuthorizationPolicy 'EnvironmentalIngest' was not found` | `AmbientReadingsController.cs` + `EnvironmentalIncidentsController.cs` — đổi sang `[IotApiKeyScopeRequirement(IotApiKeyScopeEnum.EnvironmentalIngest)]` |

Sau khi fix, rebuild service: `cd backend && docker compose up -d --build batteryservice`.

**(b) Scope `EnvironmentalIngest` cho device** — `EdgeDeviceDefault = 11` (SensorIngest 1 + DeviceHeartbeat 2 + FirmwareCheck 8) **KHÔNG** gồm `EnvironmentalIngest = 4`. Device chỉ gửi BMS ingest + heartbeat + firmware-check là đủ scope. Nhưng nếu device bật `sht31`/`mq2`/`water_leak` (ambient + environmental incident) thì API key PHẢI có thêm scope `EnvironmentalIngest`, nếu không → **401** (đúng, không phải 500).

Cấp scope khi tạo device (đặt `apiKeyScopes = 15` = 11 | 4):
```bash
# trong body POST /api/admin/iot-devices (Bước 4.4): thêm "apiKeyScopes": 15
```
Hoặc grant cho device đã tồn tại bằng SQL (dev only):
```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db \
  -c "update iot_devices set api_key_scopes = api_key_scopes | 4 where device_code='ESP32-SIM-001';"
```

---

## 3. Setup môi trường — 5 bước

### Bước 3.1 — Vào thư mục

```bash
cd /Users/alex/Documents/capstone/iot-simulator
```

### Bước 3.2 — Tạo venv + cài deps

```bash
make install
```

Tương đương:
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Verify:
```bash
.venv/bin/python -c "import requests, yaml, rich, paho.mqtt.client, dotenv; print('OK')"
```

### Bước 3.3 — Tạo file `.env`

```bash
cp env.example.txt .env
```

Mở `.env` sửa:

```ini
IOT_BASE_URL=http://localhost:4001
IOT_TLS_VERIFY=false
# IOT_API_KEY để trống — vì sẽ điền per-device trong seed.yaml
```

### Bước 3.4 — Chạy unit test (không cần backend)

```bash
make test
```

Phải thấy `Ran 44 tests in ~0.003s — OK` (3 file: `test_bms.py`, `test_payload.py`, `test_features.py`). Nếu fail → simulator code có vấn đề, đừng tiếp tục.

### Bước 3.5 — Kiểm tra backend alive

```bash
curl -s http://localhost:4001/health || curl -sI http://localhost:4001/api/auth/login
```

Phải trả 2xx/4xx (không phải connection refused). Nếu refused → quay lại 2.2.

---

## 4. Lấy API key từ backend — cách curl đầy đủ

> Đây là phần khó nhất. Lý do: backend chỉ trả `rawApiKey` **1 lần duy nhất** khi tạo device — copy ngay, mất là phải rotate-key.

### 4.1. Login → lấy `accessToken`

```bash
TOKEN=$(curl -s -X POST http://localhost:4001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@yourdomain.com","password":"Admin123@"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["accessToken"])')

echo $TOKEN | head -c 80    # xem 80 ký tự đầu
```

JWT này có hiệu lực **1 giờ** (3600s). Hết hạn → login lại.

### 4.2. Lấy `siteId` Guid

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:4001/api/sites?pageSize=10" | python3 -m json.tool
```

Copy `data.items[0].id`. Trong DB hiện tại:

| Site name | Guid |
|---|---|
| Solar Farm Long An | `b6d83be5-050c-47a0-9f73-3160f517be80` |

Set biến shell để tái dùng:

```bash
SITE_ID=b6d83be5-050c-47a0-9f73-3160f517be80
```

> Nếu chưa có site → `POST /api/sites` với body `{"name":"...","address":"...","status":1, "customerId": "<customer guid>"}`.

### 4.3. Lấy danh sách `battery_asset_id` Guid

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:4001/api/battery-assets?siteId=$SITE_ID&pageSize=20" \
  | python3 -m json.tool
```

Copy field `id` của mỗi pin. Trong DB hiện tại có 3 pin:

| Serial | BatteryAssetId Guid | Type |
|---|---|---|
| `BAT-2026-001` | `54754d04-3c44-4a49-acf2-068cfde936bc` | LiFePO4 12V 100Ah |
| `BAT-2026-002` | `2810f7d9-a11e-4ab0-85d0-d4e68cce8443` | LiFePO4 12V 100Ah |
| `BAT-2026-003` | `5e2116ec-4d54-4bc6-a2e8-543a86c48934` | NMC 48V 200Ah |

### 4.4. Tạo IoT device — endpoint `POST /api/admin/iot-devices`

Validation backend:
- `deviceCode`: 3–64 chars, regex `^[A-Z0-9-]+$` (chỉ HOA + số + gạch ngang)
- `displayName`: ≤200 chars, bắt buộc
- `siteId`: Guid không rỗng
- `hardwareRevision`: ≤64 chars, optional
- `heartbeatIntervalSeconds`: 10–3600, default 60
- `apiKeyScopes`: default `EdgeDeviceDefault` = **11** = sensor.ingest(1) + device.heartbeat(2) + firmware.check(8). ⚠ KHÔNG gồm `environmental.ingest`(4). Device dùng ambient/incident phải set `apiKeyScopes: 15` (xem [§2.5b](#25-bắt-buộc-cho-iot2-production--2-fix-backend--scope-environmentalingest)).
- `notes`: ≤1000 chars, optional

```bash
curl -s -X POST "http://localhost:4001/api/admin/iot-devices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"deviceCode\": \"ESP32-SIM-001\",
    \"displayName\": \"ESP32 Simulator 001\",
    \"siteId\": \"$SITE_ID\",
    \"hardwareRevision\": \"ESP32-S3-DevKitC-1-N16R8\",
    \"heartbeatIntervalSeconds\": 60
  }" | python3 -m json.tool
```

Response thành công (HTTP 201):

```json
{
  "data": {
    "id": "329e216d-fc8f-457b-a87c-60aa8c45e5f0",
    "deviceCode": "ESP32-SIM-001",
    "rawApiKey": "iotk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "apiKeyLastFour": "wxyz",
    "apiKeyScopes": 11,
    "status": 1,
    ...
  },
  "isSuccess": true,
  "statusCode": 201
}
```

**→ COPY NGAY `rawApiKey`. Reload trang là mất.**

Tạo device thứ 2 tương tự, đổi `deviceCode` thành `ESP32-SIM-002`.

### 4.5. Nếu lỡ mất `rawApiKey` — rotate key

```bash
DEVICE_ID=329e216d-fc8f-457b-a87c-60aa8c45e5f0   # từ response 4.4
curl -s -X POST "http://localhost:4001/api/admin/iot-devices/$DEVICE_ID/rotate-key" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Response trả `rawApiKey` mới (key cũ invalid ngay lập tức).

### 4.6. Cách tự động — script `provision_devices.py`

```bash
export ADMIN_TOKEN=$TOKEN
.venv/bin/python scripts/provision_devices.py --base-url http://localhost:4001
```

Script đọc `config/seed.yaml`, tạo tất cả device, in `rawApiKey` cho từng cái → bạn paste vào `seed.yaml`.

---

## 5. Cấu hình `seed.yaml` + `.env`

### 5.1. Sửa `config/seed.yaml`

Mở file và điền:

```yaml
backend:
  base_url: http://localhost:4001          # ApiGateway
  tls_verify: false
  contract_version: current                # đang dùng contract TODAY (Sprint 1 legacy NI §7.4)
  heartbeat_interval_s: 60
  ingest_interval_s: 5                     # Sprint 1 S1-FW-07: "mỗi 5s"
  batch_size_per_battery: 1                # Sprint 1 firmware: 1 reading/pin/batch
  retry_base_s: 2
  retry_max_s: 300
  retry_jitter_pct: 20

mqtt:
  enabled: false                            # Sprint IoT-2 Phase D — bật khi MQTT broker sẵn

devices:
  - device_code: ESP32-SIM-001
    site_id_guid: b6d83be5-050c-47a0-9f73-3160f517be80    # ← từ Bước 4.2
    api_key: iotk_<rawApiKey từ Bước 4.4>                 # ← key của ESP32-SIM-001
    batteries:
      # Sprint 1 S1-FW-04: voltage 12.0–13.0V → SOC 25-30% giữ OCV trong dải đó.
      - serial: BAT-2026-001
        battery_asset_id: 54754d04-3c44-4a49-acf2-068cfde936bc   # ← Bước 4.3
        unit_id: 1
        nominal_voltage: 12.8
        nominal_capacity_ah: 100
        initial_soc: 30.0
        initial_soh: 94.2
        cycle_count: 120
        chemistry: LiFePO4
      - serial: BAT-2026-002
        battery_asset_id: 2810f7d9-a11e-4ab0-85d0-d4e68cce8443
        unit_id: 2
        # ... các field khác
    sensors:
      ina226: true
      ds18b20: true
      sht31: true
      mq2: true
      water_leak: true
    scenario: normal           # ← đổi để demo anomaly: overheat, low_soc, ...
    sensor_drift:
      voltage_v: 0.05
      temperature_c: 1.2

  - device_code: ESP32-SIM-002
    # ... tương tự
```

### 5.2. Sửa `.env`

```ini
IOT_BASE_URL=http://localhost:4001
IOT_TLS_VERIFY=false
IOT_API_KEY=                  # để trống — đã set per-device trong seed.yaml
IOT_SEED_FILE=config/seed.yaml
IOT_QUEUE_DIR=logs/queue
IOT_LOG_LEVEL=INFO

# MQTT (tùy chọn, để mặc định nếu chưa dùng)
IOT_MQTT_ENABLED=false
IOT_MQTT_HOST=localhost
IOT_MQTT_PORT=21883
```

### 5.3. Validate config

```bash
.venv/bin/python -c "
from src.config import load_config
c = load_config('config/seed.yaml')
print(f'backend  : {c.backend.base_url} (contract={c.backend.contract_version})')
print(f'devices  : {len(c.devices)}')
for d in c.devices:
    print(f'  {d.device_code} → {len(d.batteries)} pin, scenario={d.scenario}, key=...{d.api_key[-4:] if d.api_key else \"MISSING\"}')
"
```

Kỳ vọng:
```
backend  : http://localhost:4001 (contract=current)
devices  : 2
  ESP32-SIM-001 → 2 pin, scenario=normal, key=...kstU
  ESP32-SIM-002 → 1 pin, scenario=low_soc, key=...MqmI
```

Nếu in `MISSING` hoặc lỗi `batteries[].battery_asset_id` → quay lại Bước 4.

---

## 6. Smoke test + chạy thật

### 6.1. Smoke test — gửi 1 batch rồi thoát

```bash
make once
```

Hoặc:
```bash
.venv/bin/python -m src.main --once --no-dashboard
```

Output mong đợi (kết quả thật vừa test):

```
INFO iot-sim — Khởi động 2 device, backend=http://localhost:4001, mqtt=False
INFO iot-sim.device — [ESP32-SIM-001] booting (contract=current, scenario=normal, batteries=2)
INFO iot-sim.device — [ESP32-SIM-002] booting (contract=current, scenario=low_soc, batteries=1)
INFO iot-sim.device — [ESP32-SIM-001] stopped
INFO iot-sim.device — [ESP32-SIM-002] stopped
INFO iot-sim — Done. Tổng: sent=2 fail=0 queue=1 ambient=0 incidents=0
```

- `sent=2` → cả 2 device gửi thành công
- `fail=0` → không lỗi
- `queue=1` → còn 1 batch chưa kịp gửi (sẽ flush ở lần chạy tiếp theo)

### 6.2. Chạy full với dashboard live

```bash
make run
```

Hiển thị bảng `rich`:

```
┌── IoT Simulator — Live State ─────────────────────────────────────────────────┐
│ Device         Status  Scenario  Batt  V      T°C   SOC%  Sent Fail Queue ...│
│ ESP32-SIM-001  online  normal    2     12.94  29.4  78.5  42   0    0       │
│ ESP32-SIM-002  online  low_soc   1     49.30  31.1  52.7  41   0    0       │
└──────────────────────────────────────────────────────────────────────────────┘
```

Ctrl+C để dừng.

### 6.3. CLI flags hữu dụng

```bash
# Ép scenario cho TẤT CẢ devices (override seed.yaml)
.venv/bin/python -m src.main --scenario overheat

# Chỉ chạy 1 device cụ thể
.venv/bin/python -m src.main --device ESP32-SIM-001

# Lệnh kết hợp + log ra file
.venv/bin/python -m src.main \
    --device ESP32-SIM-001 \
    --scenario sensor_mismatch \
    --no-dashboard \
    --log-file logs/run-$(date +%H%M%S).log
```

---

## 7. Verify trong DB + qua API

### 7.1. Đếm reading 5 phút gần đây

```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db \
  -c "SELECT COUNT(*) FROM sensor_readings WHERE time > NOW() - INTERVAL '5 min';"
```

Expectation: 2 device × 3 pin × 4 tick/phút × 5 phút ≈ **~100 rows** (nhiều hơn nếu flush queue cũ).

### 7.2. Aggregate per pin

```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c "
SELECT b.serial_number, COUNT(*) AS readings_5min,
       MAX(s.time) AS latest,
       ROUND(AVG(s.voltage)::numeric, 2) AS avg_v,
       ROUND(AVG(s.temperature)::numeric, 1) AS avg_t,
       ROUND(AVG(s.soc_percent)::numeric, 1) AS avg_soc
FROM sensor_readings s
JOIN battery_assets b ON b.id = s.battery_asset_id
WHERE s.time > NOW() - INTERVAL '5 min'
GROUP BY b.serial_number
ORDER BY b.serial_number;
"
```

### 7.3. Verify field denormalize

```bash
# iot_devices.last_seen_at — phải update mỗi tick
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c "
SELECT device_code, status, last_seen_at FROM iot_devices ORDER BY device_code;
"

# battery_assets.last_sensor_reading_at — phải update mỗi insert
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c "
SELECT serial_number, last_sensor_reading_at FROM battery_assets ORDER BY serial_number;
"
```

### 7.4. Xem alerts

```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c "
SELECT anomaly_type, severity, current_value, threshold_value, created_at
FROM alerts ORDER BY created_at DESC LIMIT 10;
"
```

Hoặc qua API (đẹp hơn):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:4001/api/alerts?pageSize=10" | python3 -m json.tool
```

### 7.5. Realtime watch (đặt 1 terminal riêng)

```bash
watch -n 5 "PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c \"
SELECT b.serial_number, COUNT(*), MAX(s.time)
FROM sensor_readings s JOIN battery_assets b ON b.id=s.battery_asset_id
WHERE s.time > NOW() - INTERVAL '1 min'
GROUP BY 1 ORDER BY 1;\""
```

---

## 8. 19 scenario — bảng đầy đủ

| Scenario | Backend trigger | Threshold cần vượt | Thời gian ước tính trigger |
|---|---|---|---|
| `normal` | (không) | — | — |
| `overheat` | `AnomalyType=1` Critical | `Temperature > VoltageMax` threshold (~50°C) | ~3–5 phút |
| `overvoltage` | `AnomalyType=2` Critical | `Voltage > 14.4V` | ~5 phút |
| `undervoltage` | `AnomalyType=3` Critical | `Voltage < 10.5V` | ~5 phút |
| `low_soc` | `AnomalyType=4` Warning→Critical | `SOC < 20%` (Warning), `<10%` (Critical) | ~5–10 phút |
| `rapid_discharge` | `AnomalyType=5` | `Current < -10A` liên tục | ngay tick đầu |
| `abnormal_charging` | `AnomalyType=6` | `Current > 10A` charging | ngay tick đầu |
| `device_offline` | `AnomalyType=7` Warning | Sau 60s ngừng heartbeat → backend job 2 phút detect | ~3 phút |
| `soh_degradation` | `AnomalyType=8` | `SOH < 80%` | ~5 phút |
| `high_ambient_temp` | `AnomalyType=9` | Ambient `T > 40°C` (SHT31) | sau 5 phút (chu kỳ SHT31) |
| `high_humidity` | `AnomalyType=10` | Ambient `humidity > 85%` | sau 5 phút |
| `high_temp_humidity_combo` | `AnomalyType=11` | T ≥ 42°C AND humidity ≥ 88% | sau 5 phút |
| `sensor_mismatch` | `AnomalyType=15` Warning | (contract iot2 only) BMS vs INA226 lệch > 0.5V | ngay tick đầu |
| `smoke` | `EnvironmentalIncident type=1` Critical | MQ-2 ADC > 2500 | sau 30s arm |
| `fire_detected` | `EnvironmentalIncident type=2` Critical | DS18B20 + MQ-2 đồng thời | sau 30s |
| `gas_leak` | `EnvironmentalIncident type=3` Critical | MQ-135 ADC > threshold | sau 30s |
| `water_leak` | `EnvironmentalIncident type=4` (Flood) Critical | Water sensor toggle HIGH | sau 30s |
| `bms_error` | reading có `bms_error_code = "OVT-PROTECT"` (chỉ iot2) | — | ngay tick đầu |
| `clock_skew` | Backend reject `clock_drift` (chỉ iot2) | `deviceTimestamp` lệch +10 phút | ngay tick đầu |

> Lưu ý: 5 scenario (`sensor_mismatch`, `bms_error`, `clock_skew`, INA226/DS18B20 readings) **chỉ hoạt động** ở `contract_version: iot2-production`. Mode `current` chỉ gửi 1 reading BMS/battery/tick.

---

## 9. Demo end-to-end các luồng B1–B7

### B1 — Provision flow (chỉ contract iot2)

```bash
# Mode iot2-production sẽ tự gọi POST /api/iot-devices/provision khi boot
.venv/bin/python -m src.main --no-dashboard 2>&1 | grep provision
```

### B2 — Dữ liệu bình thường (đã verify ở Bước 6)

### B3 — Anomaly → Alert → Ticket → Notification

```bash
# Terminal 1
.venv/bin/python -m src.main --scenario overheat --device ESP32-SIM-001

# Terminal 2 — watch alert
watch -n 3 "curl -s -H 'Authorization: Bearer $TOKEN' \
  'http://localhost:4001/api/alerts?pageSize=5' | python3 -m json.tool"

# Sau 3-5 phút sẽ thấy alert `Overheat / Critical`
# Backend tự publish event → TicketService consume → tạo ticket P1
# Verify ticket
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:4001/api/tickets?pageSize=5" | python3 -m json.tool
```

### B4 — DeviceOffline detection

```bash
# Cách 1: scenario device_offline (simulator chủ động ngừng heartbeat)
.venv/bin/python -m src.main --scenario device_offline

# Cách 2: Ctrl+C dừng simulator giữa chừng → đợi 5 phút (LastSeenAt timeout)
# Verify
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db \
  -c "SELECT device_code, status, last_seen_at, last_offline_at FROM iot_devices;"
# status=3 Offline + alert DeviceOffline xuất hiện
```

### B5 — Calibration (Sprint S7 — chưa implement đầy đủ)

### B6 — OTA firmware (Sprint 7 — đã mô phỏng, chỉ `iot2-production`)

Simulator poll `GET /api/iot-devices/firmware-check` mỗi 1h (và ngay tick đầu). Nếu backend có firmware release mới (target version ≠ current) → simulator chạy lifecycle `PUT /api/iot-devices/firmware-update-log/{id}`: `Downloading → Installing → Success` rồi **bump `firmware_version`** in-memory (heartbeat/firmware-check kế tiếp dùng version mới → backend hết offer). Không tải `.bin`/verify SHA/rollback partition (cần hardware thật).

```bash
# Trigger ngay 1 chu kỳ check (không cần đợi 1h) — qua MQTT command trigger_ota:
#   publish solar/ESP32-SIM-001/cmd  payload {"cmdId":"x","type":"trigger_ota"}
# Hoặc xem firmware-check chạy lúc boot:
IOT_CONTRACT_VERSION=iot2-production .venv/bin/python -m src.main --no-dashboard --device ESP32-SIM-001 2>&1 | grep -iE "ota|firmware"
# Để thấy update thật: admin tạo IotFirmwareRelease mới + gán target cho device, rồi chạy lại.
```

> Cột `OTA` trên dashboard (`updates/checks`) + `FW` (version hiện tại) phản ánh trạng thái OTA.

### B7 — Resilience mất mạng

```bash
# Terminal 1
make run

# Terminal 2 — tắt backend, đợi 1 phút
docker stop solar-batteryservice

# Dashboard hiện "Queue" tăng dần, "Backoff" tăng theo exponential

# Bật lại
docker start solar-batteryservice

# Log "flushed N batch (remaining=0)"
# Verify trong DB không có row trùng theo Idempotency-Key (chỉ iot2-production)
```

---

## 10. Troubleshooting — các lỗi đã gặp

### 10.1. `psql: role "postgres" does not exist`

→ Đang gọi sai postgres. Stack solar dùng `solar-postgres` ở **port 5433**, không phải 5432.

Fix:
```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db
```

### 10.2. Simulator log `queued (size=N)` liên tục, `sent=0 fail=N`

Backend trả lỗi. Kiểm tra log backend:
```bash
docker logs solar-batteryservice --tail 100 | grep -iE "error|exception"
```

Các nguyên nhân thường gặp:

| Status code | Nguyên nhân | Fix |
|---|---|---|
| `401` (ingest/heartbeat) | API key sai, hoặc thiếu scope tương ứng (`SensorIngest`/`DeviceHeartbeat`/`FirmwareCheck`) | Rotate key (Bước 4.5), check `apiKeyScopes` |
| `401` (ambient/incident) | Device key thiếu scope `EnvironmentalIngest` (EdgeDeviceDefault=11 không có) | Grant scope 4 → `apiKeyScopes=15` ([§2.5b](#25-bắt-buộc-cho-iot2-production--2-fix-backend--scope-environmentalingest)) |
| `400` field error | Validation fail | Đọc `listErrors` chi tiết — thường do field thiếu / sai range |
| `404` | Endpoint không tồn tại | Sai `IOT_BASE_URL` (phải là `http://localhost:4001`, không `https://...:7200`) |
| `500` `duplicate key PK_sensor_ingest_idempotency_records` | **Bug backend** — `SensorIngestIdempotencyRecord.Id` không sinh | Áp dụng fix backend ([§2.5a](#25-bắt-buộc-cho-iot2-production--2-fix-backend--scope-environmentalingest)) rồi rebuild `batteryservice` |
| `500` `AuthorizationPolicy 'EnvironmentalIngest' was not found` | **Bug backend** — named policy chưa đăng ký (ambient/incident) | Áp dụng fix backend ([§2.5a](#25-bắt-buộc-cho-iot2-production--2-fix-backend--scope-environmentalingest)) rồi rebuild |
| `500` `instance ... already being tracked {Time, BatteryAssetId}` | Gửi ≥2 reading cùng `(time, batteryAssetId)` trong 1 batch (multi-source) | Đã xử lý: simulator stagger `time` theo ms. Nếu vẫn gặp → kiểm tra mã staggering trong `device._tick_ingest` |

### 10.3. `make install` fail trên Python 3.13/3.14

`rich` hoặc `paho-mqtt` chưa support — thử:
```bash
.venv/bin/pip install --upgrade pip setuptools wheel
.venv/bin/pip install -r requirements.txt --no-cache-dir
```

### 10.4. Smoke test PASS nhưng `make run` không thấy gì trong DB

Check API key đúng cho device đang chạy:
```bash
curl -s -X POST http://localhost:4001/api/sensor-readings/batch \
  -H "X-Api-Key: iotk_<key trong seed.yaml>" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"batteryAssetId":"<guid>","time":"2026-06-13T10:00:00Z","voltage":12.9,"current":-1.0,"temperature":29.0,"socPercent":78.0,"cycleCount":120,"sourceDeviceId":"TEST"}]}' \
  -w "\nHTTP %{http_code}\n"
```

Nếu curl trả 200 nhưng simulator vẫn 0 sent → check `dashboard.last_error` field.

### 10.5. `mv` venv → shebang vỡ

Đã gặp khi move folder ra ngoài. Fix:
```bash
find .venv -mindepth 1 -delete
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 10.6. Token hết hạn (401 khi gọi `/api/admin/...`)

JWT chỉ sống 1 giờ. Login lại Bước 4.1.

---

## 11. Thiết kế nội bộ + 2 contract version

### 11.1. Vòng đời 1 SimulatedDevice (mô phỏng main loop ESP32)

```
boot
  ├── (iot2-production) POST /api/iot-devices/provision  ← 1 lần
  │     → áp dụng response: pollingIntervalSeconds, heartbeatIntervalSeconds,
  │       siteId (override seed → dùng cho ambient/incident), ntpServer
  └── loop:
       ├── mỗi `ingest_interval_s` (mặc định 5s; provision có thể đổi):
       │     read BMS từng pin
       │     [iot2-only] read INA226 (redundant) + DS18B20 (external-temp)
       │     gom theo pin; [iot2] stagger `time` +1ms/+2ms mỗi source (tránh PK trùng)
       │     build payload {"items":[...]} (current vs iot2 shape)
       │     [MQTT bật + iot2] publish per-pin solar/{dev}/{serial}/telemetry (streak fail≥3 → fallback HTTPS)
       │     POST /api/sensor-readings/batch  (HTTPS, kèm Idempotency-Key nếu iot2)
       │       ├── 2xx → sent++, flush queue cũ
       │       └── lỗi → enqueue + exponential backoff
       │
       ├── mỗi `heartbeat_interval_s` (60s) [iot2-only]:
       │     POST /api/iot-devices/heartbeat   (HTTP-only, khớp firmware)
       │
       ├── mỗi 5 phút (SHT31 enabled, iot2):
       │     POST /api/ambient/readings/batch  (cần scope EnvironmentalIngest)
       │
       ├── mỗi 1 giờ [iot2-only] — OTA (Sprint 7):
       │     GET /api/iot-devices/firmware-check
       │       → nếu có bản mới: PUT firmware-update-log {Downloading→Installing→Success}
       │         + bump firmware_version (mô phỏng flash; không tải .bin/SHA thật)
       │
       └── scenario triggers [iot2-only]:
             smoke/fire/gas/water_leak → POST /api/environmental-incidents (cần scope EnvironmentalIngest)
             device_offline → halt sau 60s
             clock_skew → deviceTimestamp lệch +10 phút
```

### 11.2. So sánh 2 contract version

| Aspect | `current` (Sprint 1 legacy) | `iot2-production` (Sprint 3 production — KHỚP firmware `buildProductionBatchPayload`) |
|---|---|---|
| Endpoint sensor batch | `POST /api/sensor-readings/batch` | cùng URL |
| Body wrapper | `{ "items": [...] }` | `{ "items": [...] }` — **cùng envelope** (backend chỉ bind `List<SensorReadingItem> Items`; KHÔNG có wrapper `DeviceTimestamp`/`Readings`) |
| Reading shape | `batteryAssetId` Guid + time + V/I/T/SOC/cycle | `batteryAssetSerial` + `time` + **`deviceTimestamp` per-item** + V/I/T/SOC/cycle + `sourceType` + `sensorSourceCode` + (optional) `sohPercent`/`chargingState`/`bmsErrorCode`. **camelCase** (không phải PascalCase). |
| Header | `X-Api-Key` only | thêm `X-Device-Code` + `Idempotency-Key` (UUIDv4) |
| Multi-source/tick (BMS+INA226+DS18B20) | KHÔNG (chỉ 1 BMS reading/pin) | CÓ. Backend PK hypertable vẫn `(Time, BatteryAssetId)` → simulator **stagger `time` theo ms** mỗi source trong 1 pin để khỏi trùng PK (backend khuyến nghị ms-resolution; `CrossSourceValidationService` ghép cặp theo cửa sổ 60s nên lệch ms vẫn pair đúng). `deviceTimestamp` giữ 1 mốc/tick. |
| Provision/heartbeat/OTA | KHÔNG gọi | `/api/iot-devices/{provision,heartbeat,firmware-check,firmware-update-log}`; provision response **được áp dụng** (polling/heartbeat interval + **siteId override seed**) |
| MQTT (nếu bật) | KHÔNG | telemetry **per-pin** `solar/{dev}/{serial}/telemetry`; command `{cmdId,type,params}` → ack `{cmdId,status,error?}` |
| Cross-source SensorMismatch | chưa demo được | demo được |
| Clock skew reject | chưa enforce | reject ở `#IoT2-15` |

**Khi nào dùng iot2:** mặc định seed là `current` (an toàn, chạy ngay). Đổi sang `iot2-production` để demo đầy đủ Sprint 2–7 (provision/heartbeat/MQTT/ambient/incident/OTA) — **cần backend có 2 fix + scope** ở [§2.5](#25-bắt-buộc-cho-iot2-production--2-fix-backend--scope-environmentalingest). Đổi bằng `seed.yaml: contract_version: iot2-production` hoặc `IOT_CONTRACT_VERSION=iot2-production`.

### 11.3. Local queue resilience

```
File: logs/queue/{deviceCode}.jsonl

Mỗi line:
{
  "endpoint": "/api/sensor-readings/batch",
  "key": "<uuidv4>",
  "payload": { ... }
}
```

- Khi POST fail → `LocalQueue.append()` → ghi vào file
- Khi POST OK lần kế tiếp → `_flush_queue()` đọc theo thứ tự, gửi từng cái
- Endpoint khác nhau (sensor / ambient / incident) đều resume đúng path

### 11.4. Exponential backoff

```
base_s = 2
max_s = 300
jitter = ±20%

attempt 1 fail → next_backoff = 4s ± 0.8s
attempt 2 fail → next_backoff = 8s ± 1.6s
...
attempt N → tối đa 300s
khi 200 OK → reset về base_s
```

---

## 12. Bảo trì — rotate key, đổi scenario runtime, reset queue

### 12.1. Rotate API key của 1 device

```bash
curl -s -X POST "http://localhost:4001/api/admin/iot-devices/$DEVICE_ID/rotate-key" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Cập nhật `seed.yaml: devices[].api_key`, restart simulator.

### 12.2. Downlink command — qua MQTT (chỉ khi `mqtt.enabled` + iot2)

Schema **khớp firmware ESP32** (`cmd_logic.cpp`): `{ "cmdId", "type", "params" }` → ack `{ "cmdId", "status": "ok|failed|unknown", "error"? }` trên `solar/{dev}/cmd/ack`. `type` case-insensitive, chấp nhận cả `_` và `-`.

```
Topic: solar/ESP32-SIM-001/cmd

# Đổi interval ingest (1..3600s):
{"cmdId":"c1", "type":"set_interval", "params":{"pollingSeconds":30}}

# Yêu cầu gửi heartbeat ngay:
{"cmdId":"c2", "type":"request_heartbeat"}

# Trigger 1 chu kỳ OTA check:
{"cmdId":"c3", "type":"trigger_ota"}

# (sim-only, firmware KHÔNG có) đổi scenario runtime để demo:
{"cmdId":"c4", "type":"set_scenario", "params":{"scenario":"overheat"}}
```

### 12.3. Reset queue

```bash
find logs/queue -name "*.jsonl" -delete
# hoặc xoá 1 device cụ thể
rm logs/queue/ESP32-SIM-001.jsonl
```

### 12.4. Decommission device khi không dùng

```bash
curl -s -X DELETE "http://localhost:4001/api/admin/iot-devices/$DEVICE_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Device chuyển status=4 Decommissioned, API key invalid.

### 12.5. Reset hoàn toàn data trong DB (chỉ dev)

```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c "
TRUNCATE sensor_readings, iot_device_heartbeats, ambient_readings,
         environmental_incidents, alerts, outbox_messages RESTART IDENTITY;
UPDATE iot_devices SET last_seen_at=NULL, last_offline_at=NULL;
UPDATE battery_assets SET last_sensor_reading_at=NULL;
"
```

---

## Tài liệu liên quan

- `README.md` — Quick start + coverage matrix
- `config/seed.yaml` — config gốc
- `../iot/tasksprint.md` — kế hoạch 8 sprint
- `../iot/newiot.md` — thiết kế ESP32+MQTT v2
- `../iot/overall.iot.md` — BOM + luồng B1–B7
- `../backend/docs/api-battery.md` — contract API thật

## Hỗ trợ

- Backend BatteryService owner: Thắng
- IoT track owner: (theo Bước phân công sprint)
- Issue tracker: GitHub Issues label `area:iot-simulator`
