# IoT Simulator — Hướng dẫn sử dụng đầy đủ

> Hướng dẫn từng bước để cài, cấu hình và chạy `iot-simulator` trong dự án
> **Solar Battery Maintenance Management System (GSU26SE55)**.
> Dành cho thành viên BE / FE / AI / QA cần demo trọn luồng IoT khi chưa có phần cứng ESP32.
>
> **Cập nhật: 2026-08-11.** Simulator đã được viết lại để bám sát firmware ESP32-S3 thật
> (`capstone/iot/firmware-esp32`) ở mọi thứ **đi ra dây**: endpoint, header, hình dạng JSON, thứ tự
> trạng thái, cách xử lý lỗi. 313 unit test. Đã kiểm end-to-end trên backend thật: provision →
> telemetry đa nguồn → heartbeat → ambient → sự cố môi trường → OTA (tải + xác minh SHA-256 thật)
> → lệnh downlink qua MQTT → LWT → mất mạng và đẩy bù → **17/17 loại cảnh báo**.

---

## Mục lục

- [1. Bức tranh tổng thể](#1-bức-tranh-tổng-thể)
- [2. Chuẩn bị](#2-chuẩn-bị)
- [3. Cài đặt — 3 bước](#3-cài-đặt--3-bước)
- [4. Chạy thử KHÔNG cần backend thật](#4-chạy-thử-không-cần-backend-thật)
- [5. Lấy API key từ backend thật](#5-lấy-api-key-từ-backend-thật)
- [6. Cấu hình `seed.yaml` và `.env`](#6-cấu-hình-seedyaml-và-env)
- [7. Chạy thật + đọc bảng trạng thái](#7-chạy-thật--đọc-bảng-trạng-thái)
- [8. Kiểm chứng trong database](#8-kiểm-chứng-trong-database)
- [9. Trạng thái bền vững — thứ hay gây bất ngờ nhất](#9-trạng-thái-bền-vững--thứ-hay-gây-bất-ngờ-nhất)
- [10. 19 kịch bản](#10-19-kịch-bản)
- [11. Demo cảnh báo bằng bộ dataset anomaly](#11-demo-cảnh-báo-bằng-bộ-dataset-anomaly)
- [12. Demo các luồng B1–B7](#12-demo-các-luồng-b1b7)
- [13. Bật MQTT](#13-bật-mqtt)
- [14. Khắc phục sự cố](#14-khắc-phục-sự-cố)
- [15. Thiết kế nội bộ](#15-thiết-kế-nội-bộ)
- [16. Bảo trì](#16-bảo-trì)

---

## 1. Bức tranh tổng thể

```
┌──────────────────────┐   HTTPS (X-Api-Key)   ┌──────────────────┐
│   iot-simulator      │ ────────────────────► │  ApiGateway:4001 │
│   (thay ESP32-S3)    │        + MQTT         └────────┬─────────┘
│                      │ ◄──── lệnh downlink            ▼
│  · mô hình pin       │                       ┌──────────────────┐
│  · hàng đợi offline  │                       │ BatteryService   │
│  · state bền vững    │                       │ · kiểm API key   │
│  · máy trạng thái OTA│                       │ · ghi hypertable │
└──────────────────────┘                       │ · quét ngưỡng    │
                                               └────────┬─────────┘
                                    TimescaleDB ◄───────┤
                                    alerts      ◄───────┤
                                    outbox → RabbitMQ ──┴──► TicketService
                                                             NotificationService
```

Simulator **thay đúng phần "Site / BMS / ESP32"** trong sơ đồ `newiot.md §4`. Backend xử lý y hệt
như khi nhận từ ESP32 thật — **không có nhánh code riêng cho mock**.

Cái nó bỏ, và bỏ có chủ ý: Wi-Fi/AP cấu hình tại chỗ, trang web setup, quét QR, Serial CLI, RS485
Modbus/JK-BMS, I2C/1-Wire, ghi OTA partition. Đó là phần **phần cứng**, không nằm trên đường
`thiết bị ↔ backend`.

---

## 2. Chuẩn bị

### 2.1. Phần mềm

| Công cụ | Phiên bản | Cài |
|---|---|---|
| Python | 3.10+ (đang test trên 3.14) | `brew install python@3.12` |
| Docker + Compose | bất kỳ | Docker Desktop |
| `psql` (tuỳ chọn) | bất kỳ | `brew install libpq && brew link --force libpq` |
| `curl` | có sẵn | — |

Không có `psql` cũng không sao: mọi câu lệnh trong tài liệu này đều có bản chạy qua
`docker exec solar-postgres psql …`.

### 2.2. Stack backend đang chạy

```bash
docker ps --format "table {{.Names}}\t{{.Ports}}" | grep solar
```

Cần tối thiểu:

| Container | Cổng host |
|---|---|
| `solar-apigateway` | **4001** |
| `solar-authservice` | 4002 |
| `solar-batteryservice` | 4006 |
| `solar-ticketservice` | 4007 |
| `solar-notificationservice` | 4008 |
| `solar-postgres` | **5433** (không phải 5432 — 5432 là TimescaleDB của track IoT) |
| `solar-mosquitto` | 21883 (chỉ cần khi bật MQTT) |

Chưa chạy thì `cd backend && docker compose up -d`.

### 2.3. Tài khoản admin

```
Email:    admin@yourdomain.com
Password: Admin123@
```

### 2.4. Kết nối database

| Trường | Giá trị |
|---|---|
| Host | `localhost` |
| Port | **5433** |
| User | `postgres` |
| Password | `Password12345@` |
| DB | `battery_db` |

Kiểm nhanh:

```bash
PGPASSWORD='Password12345@' psql -h localhost -p 5433 -U postgres -d battery_db -c "select 1;"
# hoặc không cần psql:
docker exec solar-postgres psql -U postgres -d battery_db -c "select 1;"
```

### 2.5. ⚠ Điều kiện phía backend — đọc kỹ, đây là nguồn của hầu hết ca "chạy mà không thấy gì"

**(a) Scope `EnvironmentalIngest` (bitmask 4).**
`EdgeDeviceDefault` = `SensorIngest(1) | DeviceHeartbeat(2) | FirmwareCheck(8)` = **11 — KHÔNG có
4**. Thiếu nó thì `POST /api/ambient/readings/batch` và `POST /api/environmental-incidents` trả
**403**, mà 403 là lỗi vĩnh viễn nên thiết bị **bỏ luôn**, không thử lại. Cấp key với
`apiKeyScopes = 15`.

Kiểm scope của thiết bị đang có:

```bash
docker exec solar-postgres psql -U postgres -d battery_db \
  -c "select device_code, api_key_scopes, status from iot_devices where device_code like 'ESP32-SIM-%';"
```

Cấp thêm scope cho thiết bị đã tồn tại (chỉ dev):

```bash
docker exec solar-postgres psql -U postgres -d battery_db \
  -c "update iot_devices set api_key_scopes = api_key_scopes | 4 where device_code = 'ESP32-SIM-001';"
```

**(b) `Mqtt__Enabled=true`** — chỉ khi dùng đường MQTT. Mặc định của backend là `false`. Broker
vẫn nhận publish bất kể backend có subscribe hay không, nên tắt bridge nghĩa là **mất toàn bộ
telemetry trong im lặng** — không có lỗi ở bất kỳ đâu.

**(c) Ngưỡng auto-decommission.** Hơn **50 số đo ngoài dải vật lý trong 1 giờ** thì backend tự
chuyển thiết bị sang `Decommissioned`, và thiết bị đó bị **loại khỏi bảng tra khoá API** ⇒ mọi
request sau trả **401**, không tự hồi phục. Kịch bản `normal` của simulator luôn nằm trong dải (có
test chặn), nhưng cần biết điều này trước khi nghịch giá trị.

**(d) Ngưỡng Tier 2** (chỉ cần cho case 13/14 của bộ dataset anomaly). Hai cột dưới mặc định
`NULL` ⇒ luật tương ứng **không bao giờ chạy**:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "UPDATE threshold_configs SET internal_resistance_max_milliohm = 50,
                                cell_voltage_delta_max_mv = 100 WHERE is_active;"
```

> **Ghi chú lịch sử:** bản guideline trước liệt kê hai lỗi backend bắt buộc phải vá
> (`SensorIngestIdempotencyRecord.Id` không sinh, và policy `EnvironmentalIngest` chưa đăng ký).
> Cả hai **đã được vá** — kiểm lại ngày 2026-08-11 bằng cách gửi thật: ingest kèm
> `Idempotency-Key` nhiều lần trả 200/201 bình thường, ambient và environmental-incident đều 201.
> Không cần làm gì thêm.

---

## 3. Cài đặt — 3 bước

### 3.1. Vào thư mục và cài

```bash
cd ~/Documents/capstone/iot-simulator
make install          # tạo .venv + cài requirements
```

### 3.2. Chạy test (không cần backend, không cần mạng)

```bash
make test             # 313 test, dưới 1 giây
```

Test đỏ ở bước này nghĩa là môi trường Python hỏng — sửa xong hãy đi tiếp.

### 3.3. Tạo `.env`

```bash
cp env.example.txt .env
```

Sửa hai dòng:

```env
IOT_BASE_URL=http://localhost:4001
IOT_TLS_VERIFY=false
```

`IOT_API_KEY` để trống cũng được — key sẽ khai theo từng thiết bị trong `seed.yaml`.

---

## 4. Chạy thử KHÔNG cần backend thật

Kèm sẵn một **backend giả kiểm hợp đồng nghiêm ngặt** (`tools/mock_backend.py`). Nó **cố ý khắt
khe hơn ASP.NET Core**: backend thật bind JSON không phân biệt hoa thường và bỏ qua trường lạ, nên
nó *che mất* chỗ payload sai; backend giả từ chối thẳng để lỗi lộ ra ngay.

Mở **hai cửa sổ terminal**:

```bash
# Cửa sổ 1 — backend giả (cổng 4099, KHÔNG trùng ApiGateway thật ở 4001)
make mock

# Cửa sổ 2 — simulator trỏ vào backend giả
make demo
```

Sẽ thấy: provision → nhận cấu hình + bảng pin từ backend → telemetry đa nguồn mỗi 5 giây →
heartbeat → ambient → bảng trạng thái cập nhật liên tục.

Xem backend giả đã nhận được gì:

```bash
curl -s localhost:4099/ | python3 -m json.tool
```

Trường `errors` **phải rỗng**. Có phần tử nào trong đó nghĩa là payload sai hợp đồng.

Thử luôn OTA trọn vòng (tải artifact thật + xác minh SHA-256 thật):

```bash
make mock-ota         # backend giả có offer bản 1.2.0
```

---

## 5. Lấy API key từ backend thật

> Bỏ qua mục này nếu `seed.yaml` sẵn có đã dùng được. Kiểm nhanh bằng
> `python -m src.anomaly check` ở [§11](#11-demo-cảnh-báo-bằng-bộ-dataset-anomaly).

### 5.1. Đăng nhập lấy token

```bash
TOKEN=$(curl -s -X POST http://localhost:4001/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@yourdomain.com","password":"Admin123@"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['tokens']['accessToken'])")
echo "${#TOKEN} ký tự"
```

### 5.2. Lấy `siteId`

```bash
curl -s "http://localhost:4001/api/sites?pageSize=20" -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool | head -40
```

### 5.3. Lấy danh sách pin

```bash
curl -s "http://localhost:4001/api/battery-assets?pageSize=50" -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool | head -60
```

Hoặc đọc thẳng từ DB — nhanh và đủ dùng:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT ba.serial_number, bt.name AS loai_pin, s.name AS site
FROM battery_assets ba
LEFT JOIN battery_types bt ON bt.id = ba.battery_type_id
LEFT JOIN sites s ON s.id = ba.site_id
ORDER BY ba.serial_number;"
```

### 5.4. Tạo thiết bị IoT

Route **thật** là `POST /api/admin/iot-devices` — **không có** `/v1/`.

```bash
curl -s -X POST http://localhost:4001/api/admin/iot-devices \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "deviceCode": "ESP32-SIM-001",
        "displayName": "ESP32-SIM-001 (simulator)",
        "siteId": "<siteId lấy ở 5.2>",
        "hardwareRevision": "ESP32-S3-DevKitC-1-N16R8",
        "heartbeatIntervalSeconds": 60,
        "apiKeyScopes": 15
      }' | python3 -m json.tool
```

⚠ `rawApiKey` backend **chỉ trả một lần**. Chép ngay vào `seed.yaml`.
⚠ `apiKeyScopes: 15` — thiếu là ambient và sự cố môi trường trả 403 (xem [§2.5a](#25--điều-kiện-phía-backend--đọc-kỹ-đây-là-nguồn-của-hầu-hết-ca-chạy-mà-không-thấy-gì)).

### 5.5. Cách tự động

```bash
export ADMIN_TOKEN=$TOKEN
make provision        # đọc seed.yaml → tạo device → in bảng device_code | rawApiKey
```

Script tự in cảnh báo về scope. Nếu backend của bạn dùng route khác:

```bash
python scripts/provision_devices.py --admin-path /api/v1/admin/iot-devices --insecure
```

---

## 6. Cấu hình `seed.yaml` và `.env`

### 6.1. `seed.yaml` chỉ là ĐƯỜNG LUI

Đây là điểm khác lớn nhất so với bản simulator cũ, và cũng là chỗ hay hiểu nhầm nhất.

`config/seed.yaml` đóng đúng vai trò của `firmware-esp32/include/config.h`: **chỉ dùng khi chưa có
gì trong state**. Sau lần provision đầu tiên, **backend là nguồn chân lý**:

| Backend quyết định | Ghi vào |
|---|---|
| `pollingIntervalSeconds`, `heartbeatIntervalSeconds` | `logs/state/<device>.nvs.json` |
| `siteId`, `ntpServer` | nt |
| 6 trường MQTT (`host`, `port`, `tls`, `topicPrefix`, `username`, `password`) | nt |
| `batteryMappings[]` (pin nào thiết bị được gửi) | nt |

```
lần chạy đầu:   seed.yaml ──► POST /provision ──► backend trả cấu hình thật
                                                        │
lần chạy sau:   logs/state/<device>.nvs.json ◄──────────┘   (tương đương NVS của ESP32)
```

### 6.2. Chọn contract

```yaml
backend:
  contract_version: iot2-production     # MẶC ĐỊNH
```

| Giá trị | Ý nghĩa |
|---|---|
| **`iot2-production`** | Contract Sprint 3 — **contract DUY NHẤT mà firmware ESP32 thật chạy**. Đủ provision / heartbeat / MQTT / ambient / sự cố / OTA / `Idempotency-Key`. Định danh pin bằng `batteryAssetSerial`. |
| `current` | Contract Sprint 1 cho backend đời cũ: chỉ `POST /api/sensor-readings/batch` với `items[].batteryAssetId` (Guid), header chỉ `X-Api-Key`. Mọi tính năng trên **tự tắt**. Bắt buộc điền `batteries[].battery_asset_id`. |

Đổi bằng `seed.yaml` hoặc `IOT_CONTRACT_VERSION=current`.

### 6.3. Khai thiết bị

```yaml
devices:
  - device_code: ESP32-SIM-001
    site_id_guid: b6d83be5-050c-47a0-9f73-3160f517be80   # đường lui, provision sẽ ghi đè
    site_label: solar-farm-long-an
    firmware_version: 1.0.0-sim
    hardware_revision: ESP32-S3-DevKitC-1-N16R8
    model: ESP32-WROOM-S3
    api_key: iotk_...                                     # rawApiKey lấy ở §5.4
    batteries:
      - serial: BAT-2026-001
        battery_asset_id: 54754d04-3c44-4a49-acf2-068cfde936bc
        unit_id: 1
        nominal_voltage: 12.8
        nominal_capacity_ah: 100
        initial_soc: 30.0
        initial_soh: 94.2
        cycle_count: 120
        chemistry: LiFePO4
    sensors:
      ina226: true          # nguồn dự phòng đo V/I  → sensorSourceCode "redundant"
      ds18b20: true         # nhiệt ngoài thân pin   → "external-temp"
      sht31: true           # → POST /api/ambient/readings/batch, mỗi 60 giây
      mq2: true             # → POST /api/environmental-incidents, GasLeak(3)
      water_leak: true      # → Flood(4)
    scenario: normal
```

### 6.4. ⚠ `battery_catalog` — bắt buộc hiểu, nếu không sẽ có cảnh báo giả

`POST /provision` trả `batteryMappings[]` gồm **mọi pin cùng site với thiết bị** (#IoT2-18: thiết
bị chỉ được gửi cho pin **cùng site**, sai site backend trả 403 cho cả batch).

Với `ESP32-SIM-001` ở Solar Farm Long An, backend giao cả pin **NMC 48V** và **LiFePO4 24V**. Nếu
không khai loại pin, simulator dựng mô hình mặc định 12,8V cho chúng ⇒ backend chấm
**Undervoltage Critical** liên tục vì ngưỡng NMC là 42–54,6V. Cảnh báo giả, và rất khó truy.

`seed.yaml` đã có sẵn khối `battery_catalog` khai đủ 10 pin thật với đúng điện áp danh định. **Khi
thêm pin mới vào DB, nhớ thêm vào đây.** Simulator sẽ in cảnh báo nếu backend giao một pin nó
không biết:

```
[ESP32-SIM-001] pin BAT-XXX do backend giao nhưng KHÔNG có trong seed/battery_catalog —
dùng mô hình LiFePO4 12.8V/100Ah mặc định. Nếu pin này khác loại, số đo sinh ra sẽ nằm ngoài
ngưỡng và tạo CẢNH BÁO GIẢ. Thêm nó vào `battery_catalog` để sửa.
```

### 6.5. Kiểm tra cấu hình

```bash
.venv/bin/python -c "
from src.config import load_config
c = load_config('config/seed.yaml')
print('contract :', c.backend.contract_version)
print('backend  :', c.backend.base_url)
print('devices  :', [d.device_code for d in c.devices])
print('catalog  :', len(c.battery_catalog), 'pin')
print('mqtt     :', c.mqtt.enabled, 'qos', c.mqtt.qos)
"
```

---

## 7. Chạy thật + đọc bảng trạng thái

### 7.1. Smoke test — gửi một batch rồi thoát

```bash
IOT_BASE_URL=http://localhost:4001 python -m src.main --once --no-dashboard --device ESP32-SIM-001
```

Dòng cuối phải là `sent=1 fail=0`.

### 7.2. Chạy đầy đủ với bảng trạng thái

```bash
IOT_BASE_URL=http://localhost:4001 make run
```

### 7.3. Đọc bảng trạng thái

| Cột | Ý nghĩa |
|---|---|
| **LED** | Đúng bảng màu + kiểu nháy của firmware — xem [§7.4](#74-đèn-trạng-thái) |
| Status | `provisioning` · `connecting` · `online` · `offline` · `halted` |
| FW | Phiên bản đang chạy (đổi sau khi OTA thành công) |
| OTA | `<số bản cài thành công>/<số lần kiểm tra>` kèm `rb<N>` nếu có rollback |
| MQTT | `up` / `down` / `off` |
| Sent · Fail | Số batch gửi thành công / thất bại |
| **Queue** | Hàng đợi offline (trần 200 batch) |
| **Drop** | Số batch bị BỎ — lỗi 4xx vĩnh viễn hoặc hàng đợi đầy |
| **Part** | Số lần backend **nhận thiếu** (`inserted < totalReceived`) |
| HB | `<thành công>/<tổng>` heartbeat |
| Amb · Inc | Số bản ghi ambient / sự cố môi trường |
| Cmd | `<ack ok>/<lệnh nhận>` |
| Backoff | Thời gian còn phải chờ trước lần thử lại |

**`Drop` hoặc `Part` khác 0 là tín hiệu cần điều tra**, kể cả khi `Fail` bằng 0:
- `Drop` — dữ liệu bị vứt, backend trả 4xx (sai mapping pin, sai scope, sai payload).
- `Part` — backend trả 2xx nhưng **chỉ nhận một phần**; xem log để biết bao nhiêu reading bị bỏ.

### 7.4. Đèn trạng thái

Giống hệt đèn WS2812 trên board thật — 8 trạng thái, 3 kiểu hiển thị. Quy ước:
**có nháy = cần người xử lý, sáng đều = cứ để yên.**

| Đèn | Màu | Kiểu | Nghĩa |
|---|---|---|---|
| Online | xanh lá | đều | mọi thứ bình thường |
| Queued | xanh lá | **nháy** | còn hàng đợi chưa đẩy hết |
| Offline | đỏ | đều | backend không với tới |
| Provisioning | tím | đều | đang gọi `/provision` |
| Setup | tím | **nháy** | thiếu `deviceCode`/`apiKey` hợp lệ |
| WifiSearching | cam | đều | mất kết nối, đang thử lại |
| Recovery | tím/cam | **xen kẽ** | mất kết nối ≥ 30 giây |

Thứ tự ưu tiên: trạng thái **mạng** đứng **trên** trạng thái hàng đợi — chưa có mạng thì hàng đợi
đầy là *hệ quả*, không phải nguyên nhân, mà đèn chỉ nói được một điều.

### 7.5. Các cờ dòng lệnh

```bash
python -m src.main --scenario overheat            # ép kịch bản cho MỌI thiết bị
python -m src.main --device ESP32-SIM-001 \
                   --device ESP32-SIM-002          # chỉ chạy vài thiết bị
python -m src.main --no-dashboard --log-file /tmp/sim.log
python -m src.main --once                          # gửi 1 batch rồi thoát
python -m src.main --clear-state                   # xoá state → thiết bị "mới bóc hộp"
python -m src.main --no-persist                    # không ghi state ra đĩa
```

---

## 8. Kiểm chứng trong database

### 8.1. Số đo vừa vào

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT ba.serial_number, sr.sensor_source_code, sr.source_type, count(*) AS n,
       round(min(sr.voltage),2) AS v_min, round(max(sr.voltage),2) AS v_max,
       round(avg(sr.temperature),1) AS t_tb
FROM sensor_readings sr JOIN battery_assets ba ON ba.id = sr.battery_asset_id
WHERE sr.time > now() - interval '5 minutes'
GROUP BY 1,2,3 ORDER BY 1,2;"
```

Mỗi pin bật đủ cảm biến phải có **ba dòng**: `primary` (BMS), `redundant` (INA226),
`external-temp` (DS18B20). Điện áp phải nằm trong dải của **đúng loại pin** đó.

### 8.2. Thiết bị có được ghi nhận không

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT device_code,
       CASE status WHEN 1 THEN 'Pending' WHEN 2 THEN 'Active' WHEN 3 THEN 'Offline'
            WHEN 4 THEN 'Disabled' WHEN 5 THEN 'Decommissioned' END AS trang_thai,
       last_seen_at, current_firmware_version, outlier_incident_count
FROM iot_devices WHERE device_code LIKE 'ESP32-SIM-%';"
```

Đọc bảng này cho đúng:

- **`Active (2)`** trong lúc simulator đang chạy, và `last_seen_at` cập nhật liên tục.
- **`Offline (3)`** là **bình thường** sau khi bạn dừng simulator quá 10 phút — backend tự chuyển,
  và tự đưa về `Active` sau hai tín hiệu liên tiếp khi chạy lại. Không cần can thiệp.
- **`Decommissioned (5)`** là vấn đề thật: thiết bị bị khoá vĩnh viễn, xem [§14.7](#147-thiết-bị-trả-401-với-mọi-request-kể-cả-provision).
- `outlier_incident_count` phải là **0** — khác 0 nghĩa là đang gửi giá trị ngoài dải vật lý, và
  quá 50 trong một giờ thì thiết bị bị vô hiệu hoá.

### 8.3. Ambient và sự cố môi trường

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT time, ambient_temperature_celsius AS nhiet, humidity_percent AS am, source_device_id
FROM ambient_readings WHERE time > now() - interval '10 minutes' ORDER BY time DESC LIMIT 10;"

docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT detected_at, incident_type, severity, status, reported_by, notes
FROM environmental_incidents ORDER BY detected_at DESC LIMIT 10;"
```

### 8.4. Cảnh báo

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT a.detected_at, coalesce(ba.serial_number,'(cấp site)') AS pin, a.anomaly_type,
       CASE a.severity WHEN 2 THEN 'Warning' WHEN 3 THEN 'Critical' END AS muc,
       a.threshold_value, a.actual_value, a.unit
FROM alerts a LEFT JOIN battery_assets ba ON ba.id = a.battery_asset_id
WHERE a.detected_at > now() - interval '15 minutes' AND NOT a.is_deleted
ORDER BY a.detected_at DESC;"
```

Bảng tra `anomaly_type`: 1 Overheat · 2 Overvoltage · 3 Undervoltage · 4 LowSoc ·
5 RapidDischarge · 6 AbnormalCharging · 7 DeviceOffline · 8 SohDegradation · 9 HighAmbientTemp ·
10 HighHumidity · 11 HighTempHumidityCombo · 12 HighInternalResistance · 13 CellImbalance ·
14 EnvironmentalIncident · 15 SensorMismatch · 16 Undertemp · 17 IotDataIntegrityViolation.

### 8.5. Theo dõi liên tục

```bash
while true; do
  docker exec solar-postgres psql -U postgres -d battery_db -t -c \
    "select now()::time, count(*) from sensor_readings where time > now() - interval '1 minute';"
  sleep 5
done
```

> Dùng `watch -n 5 '<lệnh>'` cũng được, nhưng **macOS không có sẵn `watch`**
> (`brew install watch` nếu muốn). Vòng lặp trên chạy được ngay, không cần cài gì.


---

## 9. Trạng thái bền vững — thứ hay gây bất ngờ nhất

Simulator lưu state vào `logs/state/<device_code>.nvs.json` — **tương đương NVS của ESP32**. Đây
là hành vi của thiết bị thật, không phải cache tiện tay.

Nó giữ:

| Khoá | Nội dung |
|---|---|
| `provd` | đã provision chưa — **lần chạy sau KHÔNG gọi lại `/provision`** |
| `pollIntS`, `hbIntS` | chu kỳ do backend cấp |
| `siteid`, `ntpsv` | site và NTP server |
| `mqhost`, `mqport`, `mqtls`, `mquser`, `mqpass`, `mqprefix` | credential broker |
| `batmap` | bảng ánh xạ pin |
| `runfw` | **phiên bản firmware đang chạy** (để OTA có ý nghĩa qua các lần chạy) |
| `otaPend`, `otaBootN`, `otaRb`, `otaBadVer`, … | máy trạng thái OTA / rollback |

**Hệ quả cần nhớ:** sửa `seed.yaml` xong chạy lại mà **không thấy thay đổi** là bình thường —
state đang thắng. Xoá state để bắt đầu lại:

```bash
make clean-state                     # xoá toàn bộ
python -m src.main --clear-state     # xoá rồi chạy luôn
python -m src.main --no-persist      # không ghi state (mỗi lần chạy là thiết bị mới)
```

Xem state đang có gì:

```bash
python3 -m json.tool logs/state/ESP32-SIM-001.nvs.json
```

---

## 10. 19 kịch bản

Đặt ở `devices[].scenario`, hoặc `--scenario`, hoặc **gửi lệnh MQTT lúc đang chạy** (xem §13).

| Nhóm | Kịch bản | Kết quả mong đợi |
|---|---|---|
| **Pin (BMS)** | `normal` | mọi giá trị trong ngưỡng |
| | `overheat` | nhiệt tăng dần → Overheat Warning rồi Critical |
| | `overvoltage` | điện áp vượt trần → Overvoltage Critical |
| | `undervoltage` | điện áp dưới sàn → Undervoltage Critical |
| | `low_soc` | SOC tụt dần → LowSoc Warning rồi Critical |
| | `rapid_discharge` | dòng xả ~−12A |
| | `abnormal_charging` | dòng sạc ~+15A |
| | `soh_degradation` | SOH tụt dần → SohDegradation |
| | `bms_error` | gắn `bmsErrorCode = "OVT-PROTECT"` vào số đo |
| **Chéo nguồn** | `sensor_mismatch` | INA226 lệch > 0,5V và DS18B20 lệch > 5°C → SensorMismatch |
| **Môi trường** | `high_ambient_temp` | ambient 45°C |
| | `high_humidity` | ẩm 92% |
| | `high_temp_humidity_combo` | 42°C + 88% |
| **Sự cố an toàn** | `gas_leak` · `smoke` | MQ-2 vượt ngưỡng → **`GasLeak(3)`** |
| | `water_leak` | → `Flood(4)` |
| | `fire_detected` | → `FireDetected(2)` ⚠ mở rộng riêng của simulator |
| **Vận hành** | `device_offline` | dừng hoạt động sau 60 giây (demo LWT + DeviceOffline) |
| | `clock_skew` | đẩy lệch đồng hồ +10 phút (kích kiểm tra #IoT2-15) |

⚠ **`smoke` và `gas_leak` đều cho ra `GasLeak(3)`**, không phải `Smoke(1)`. MQ-2 bản chất là cảm
biến GAS và firmware ánh xạ nó như vậy (quyết định NS-24 #664); `Smoke(1)` để dành cho cảm biến
khói quang học sau này.

⚠ Cảm biến an toàn (MQ-2, rò nước) có **warm-up 30 giây** và **hạ nhiệt 5 phút** giữa hai lần báo
— giống hệt firmware. Đừng sốt ruột trong 30 giây đầu.

---

## 11. Demo cảnh báo bằng bộ dataset anomaly

Cách nhanh nhất để dựng **đủ 17/17 loại cảnh báo** mà không phải chờ kịch bản chạy tới ngưỡng.

`config/anomaly-dataset.yaml` chứa 26 case; `python -m src.anomaly` đẩy đúng số đo cần thiết, qua
**cùng đường gửi** với lúc chạy bình thường.

```bash
make anomaly-list      # 26 case + điều kiện của từng case
make anomaly-check     # backend đã đủ điều kiện chưa (KHÔNG gửi gì)
make anomaly-dry       # in payload thật sẽ gửi
make anomaly           # chạy lượt thường (~90 giây)
make anomaly-verify    # in câu SQL kiểm chứng + dọn để demo lại
```

Trỏ sang backend khác: `make anomaly BACKEND=http://localhost:5001`.

### 11.1. Luôn chạy `anomaly-check` trước

Nó provision thật để lấy `siteId` + danh sách pin thiết bị được phép gửi, rồi báo case nào chạy
được, case nào bị chặn vì `#IoT2-18` (khác site), case nào cần chỉnh backend.

### 11.2. Bốn case KHÔNG chạy trong lượt thường

| Case | Lý do | Cách chạy |
|---|---|---|
| **24** HighAmbientTemp Warning | backend khử trùng cảnh báo ambient theo `(site, loại)` trong 1 giờ và **không phân biệt mức** — gửi cùng lượt với case 17 thì bị nuốt im lặng | `python -m src.anomaly run --case 24` |
| **25** HighHumidity Critical | cùng lý do, loại trừ case 18 | `python -m src.anomaly run --case 25` |
| **26** IotDataIntegrityViolation | 🔴 làm thiết bị bị vô hiệu hoá **vĩnh viễn** | `run --case 26 --include-dangerous` |
| **15** DeviceOffline | là điều kiện thời gian, không gửi được | ngừng gửi > 10 phút |

Bộ chạy tự in ra lý do và lệnh chạy riêng — không im lặng bỏ qua.

### 11.3. 🔴 Case 26 — đọc trước khi chạy

`IotDataIntegrityViolation` là loại **duy nhất có tác dụng phụ không hồi phục được**. Backend đặt
`Status = Decommissioned`, và thiết bị đó bị loại khỏi bảng tra khoá API ⇒ **mọi** request sau trả
**401**, kể cả provision.

Case này cố ý dùng **`ESP32-SIM-002`** để thiết bị demo chính không bị ảnh hưởng, và kèm sẵn lệnh
khôi phục (đã kiểm chứng chạy đúng):

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "UPDATE iot_devices SET status = 2, auto_decommissioned_at = NULL,
                          outlier_incident_count = 0, outlier_window_started_at = NULL
   WHERE device_code = 'ESP32-SIM-002';"
```

### 11.4. Ba luật khử trùng khác nhau — nguồn của mọi ca "chạy xong mà không thấy gì"

| Nhóm | Khoá khử trùng | Cửa sổ | Chạy lại thì sao |
|---|---|---|---|
| Cảnh báo theo ngưỡng pin | `(pin, loại)` | **30 phút** | chỉ tạo dòng `Merged`, không có `Open` mới |
| Cảnh báo ambient | `(site, loại)` — không phân biệt mức | **1 giờ** | bị bỏ qua im lặng |
| Sự cố môi trường | `(site, loại sự cố)` | **KHÔNG có cửa sổ** | còn một sự cố `Open`/`Acknowledged` là mọi lần báo sau trả 200 "reused", **vĩnh viễn** |

`make anomaly-verify` in sẵn ba câu SQL tương ứng để dọn.

⚠ Site Solar Farm Long An có sẵn hai sự cố mở từ **dữ liệu seed** (`Smoke` và `OverheatHazard`),
nên hai loại đó không demo được cho tới khi đóng chúng.

### 11.5. Vì sao phải chờ

Backend quét ngưỡng **mỗi 10 giây, chỉ nhìn lại 20 giây**, và cảnh báo chỉ nổ khi đủ **5 lần vi
phạm**. Bộ đếm đọc số lần **đã ghi vào DB trước lượt quét**, nên gửi một loạt trong một lượt thì
tất cả đều ra `1 < 5` và **không cảnh báo nào nổ**. Bộ chạy vì thế chia **2 đợt** (5 số đo → chờ
14 giây → 2 số đo nữa) và chờ **65 giây** trước khi gửi cặp chéo nguồn.

Không phải chậm vô cớ — bỏ hai khoảng chờ đó là demo ra kết quả sai.

---

## 12. Demo các luồng B1–B7

### B1 — Provision

```bash
make clean-state
IOT_BASE_URL=http://localhost:4001 python -m src.main --no-dashboard --device ESP32-SIM-001
```

Log phải thấy: `chưa provisioned — chạy provision flow...` → `áp bảng pin từ provision: …` →
`provisioned, polling=…s, heartbeat=…s, site=…`.

Chạy lại lần hai: **không** gọi `/provision` nữa (đọc từ state) — đúng như firmware.

### B2 — Dữ liệu bình thường

Xem [§8.1](#81-số-đo-vừa-vào).

### B3 — Cảnh báo → Ticket → Notification

```bash
# Terminal 1
IOT_BASE_URL=http://localhost:4001 python -m src.main --no-dashboard \
  --device ESP32-SIM-001 --scenario overheat

# Terminal 2 — theo dõi
while true; do
  docker exec solar-postgres psql -U postgres -d battery_db -t -c \
    "select detected_at, anomaly_type, severity from alerts
     where detected_at > now() - interval '10 minutes' order by detected_at desc limit 5;"
  sleep 5
done
```

Nhanh hơn nhiều: dùng bộ dataset ở [§11](#11-demo-cảnh-báo-bằng-bộ-dataset-anomaly).

Kiểm ticket:

```bash
docker exec solar-postgres psql -U postgres -d ticket_db -c \
  "select code, title, status, created_at from tickets
   where created_at > now() - interval '30 minutes' order by created_at desc;"
```

### B4 — Thiết bị ngoại tuyến

```bash
python -m src.main --no-dashboard --device ESP32-SIM-001 --scenario device_offline
# tự dừng gửi sau 60 giây; chờ > 10 phút rồi kiểm
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "select device_code, status, last_seen_at from iot_devices where device_code='ESP32-SIM-001';"
```

Chạy lại simulator → sau hai tín hiệu liên tiếp backend tự đưa về `Active` và đóng cảnh báo.

### B5 — Hiệu chuẩn

Backend có bảng `iot_device_calibrations` và tự áp `raw × Scale + Offset` khi ingest. Simulator
không cần biết — cứ gửi số đo thô, backend hiệu chỉnh.

### B6 — OTA

```bash
# Cách nhanh: backend giả có sẵn bản mới
make mock-ota                 # cửa sổ 1
make demo                     # cửa sổ 2
```

Log sẽ chạy đủ: `OTA x → y` → `PUT log status=2` (Downloading) → `SHA-256 OK` →
`PUT log status=3` (Installing) → `verify OK` → `PUT log status=4` (Success).

Với backend thật: admin tạo `IotFirmwareRelease` mới và gán target cho thiết bị, rồi chạy lại
simulator (hoặc gửi lệnh `trigger_ota` qua MQTT — xem §13).

Phiên bản đang chạy được **lưu bền vững**, nên lần sau backend không offer lại nữa.

### B7 — Mất mạng, không mất dữ liệu

```bash
# Terminal 1
IOT_BASE_URL=http://localhost:4001 python -m src.main --no-dashboard --device ESP32-SIM-001

# Terminal 2 — ngắt backend
docker stop solar-apigateway
# đợi 1–2 phút: log hiện "offline → đã xếp hàng (độ sâu=N)", backoff tăng dần
docker start solar-apigateway
# log hiện "đẩy bù OK — hàng đợi còn N-1", giảm dần về 0
```

Đã đo thật: hàng đợi lên 6 batch, backoff 1,8s → 14s, bật lại thì đẩy bù đủ, **không mất và không
trùng bản ghi nào** (nhờ `Idempotency-Key` sinh ngay lúc lấy mẫu).

---

## 13. Bật MQTT

### 13.1. ⚠ Đọc mục này trước — trạng thái THỰC TẾ của MQTT trên stack hiện tại

Đã đo trực tiếp ngày **2026-08-11** trên stack đang chạy:

| Điều kiện | Thực tế đo được |
|---|---|
| `Mqtt__Enabled` của BatteryService | **`true`** ✓ |
| Broker mà backend cầu nối tới | `mosquitto:1883` = **cổng host 21883** |
| `POST /provision` có trả 6 trường MQTT không? | **KHÔNG** — response không có khoá `mqtt*` nào |
| `iot_devices.mqtt_username` của `ESP32-SIM-001/002` | **rỗng** |
| Mosquitto 21883 có cho anonymous không? | **KHÔNG** (`allow_anonymous false`, rc = 0x87) |
| Có tài khoản `esp32-sim-001` trong `passwd` của broker không? | **KHÔNG** |

**Kết luận: với backend đang deploy, simulator chạy HTTPS-only.** Đó là trạng thái **hợp lệ**, và
log nói rõ:

```
[ESP32-SIM-001] chưa có cấu hình MQTT dùng được (chưa provision?) — chạy HTTPS-only
```

Mã nguồn backend (`IotDeviceProvisionResultDto` + `DeviceLifecycleHandlers`) **đã có** đủ sáu
trường và cả phần tự sinh credential khi thiếu (IOT3-27/29/42), nhưng **image đang chạy chưa có
phần đó**. Muốn dùng đường MQTT đúng như thiết kế thì phải build lại BatteryService từ mã nguồn
hiện tại rồi provision lại — lúc đó `mqtt_username`/`mqtt_password_plaintext` sẽ được sinh và đẩy
xuống broker, và simulator tự nhận credential qua provision mà **không cần khai gì trong seed**.

### 13.2. Cách demo MQTT NGAY, không cần build lại backend

Dùng broker **EMQX ở cổng 11883** (container `iot-emqx` của track IoT) — đã kiểm: **cho phép
anonymous** (rc = 0). Cách này chứng minh trọn đường MQTT của simulator: telemetry theo từng pin,
lệnh downlink, ack, LWT.

**Bước 1** — bật MQTT trong `seed.yaml` và khai broker (đường lui, vì provision không cấp):

```yaml
mqtt:
  enabled: true
  host: localhost
  port: 11883
  tls: false
  username: esp32-sim-001      # phải KHÁC rỗng thì cấu hình mới được coi là "dùng được"
  password: demo-pass
  topic_prefix: solar
  qos: 0
```

**Bước 2** — chạy simulator (dùng backend giả cho gọn, hoặc backend thật đều được):

```bash
make mock                                    # cửa sổ 1 — backend giả ở 4099
IOT_MQTT_ENABLED=true make demo              # cửa sổ 2
```

Log phải thấy `MQTT CONNECTED localhost:11883` và `subscribe solar/esp32-sim-001/cmd`.

> Backend giả cũng cấp được credential MQTT qua provision, giống hệt backend sau khi build lại:
> `python tools/mock_backend.py --port 4099 --mqtt-host localhost --mqtt-port 11883 --mqtt-pass demo`

**Bước 3** — nếu muốn dùng Mosquitto 21883 (broker mà backend thật cầu nối tới), phải mượn một
tài khoản đã có trong `passwd`. Xem danh sách:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "select device_code, mqtt_username, mqtt_password_plaintext from iot_devices
   where coalesce(mqtt_username,'') <> '' and coalesce(mqtt_password_plaintext,'') <> '';"
```

⚠ ACL của Mosquitto là `pattern write solar/%u/...` với `%u` = username. Mượn tài khoản của thiết
bị khác thì **topic cũng phải theo tên thiết bị đó**, nếu không broker chặn im lặng. Cách sạch hơn
là build lại backend (§13.1).

### 13.3. Topic

```
<prefix>/<batterySerial>/telemetry   thiết bị gửi — MỖI PIN MỘT MESSAGE
<prefix>/status                      "online" retain; LWT "offline" retain
<prefix>/cmd                         thiết bị nhận lệnh
<prefix>/cmd/ack                     thiết bị trả kết quả
```

`<prefix>` = `solar/<mã thiết bị CHỮ THƯỜNG>`, khớp `MqttBrokerEndpointProvider.TopicPrefixFor()`
của backend. Simulator tự hạ chữ thường và cảnh báo nếu tiền tố backend cấp lệch với mã thiết bị.

⚠ **QoS 0 là cố ý.** Firmware dùng PubSubClient v2.8 — thư viện không hỗ trợ publish QoS 1. Đặt
QoS 1 làm simulator "khoẻ hơn" thiết bị thật và che mất lớp lỗi rơi message.

### 13.4. Gửi lệnh downlink

macOS **không** có sẵn `mosquitto_pub`, nên dùng `paho-mqtt` đã có trong `.venv`.
Đổi `PORT` cho khớp broker bạn đang dùng (11883 EMQX / 21883 Mosquitto).

```bash
.venv/bin/python - <<'EOF'
import json, paho.mqtt.client as mqtt

HOST, PORT = "localhost", 11883
DEVICE = "esp32-sim-001"          # ⚠ CHỮ THƯỜNG
CMDS = [
    {"cmdId": "c1", "type": "set_interval", "params": {"pollingSeconds": 10}},
    {"cmdId": "c2", "type": "request_heartbeat"},
    {"cmdId": "c3", "type": "trigger_ota"},
    # ⚠ mở rộng RIÊNG của simulator — firmware thật trả "unknown"
    {"cmdId": "c4", "type": "set_scenario", "params": {"scenario": "overheat"}},
]

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cli")
c.connect(HOST, PORT, 30)
for cmd in CMDS:
    c.publish(f"solar/{DEVICE}/cmd", json.dumps(cmd), qos=1)
    print("đã gửi:", cmd["type"])
c.disconnect()
EOF
```

Ack có dạng `{"cmdId":"…","status":"ok|failed|unknown|rejected","error":"…"}`.
`rejected` xuất hiện khi `trigger_ota` bị từ chối (ví dụ đang xác minh bản vừa cài).

### 13.5. Xem telemetry, ack và LWT đang chảy trên broker

```bash
.venv/bin/python - <<'EOF'
import json, time, paho.mqtt.client as mqtt

HOST, PORT, DEVICE = "localhost", 11883, "esp32-sim-001"

def on_connect(c, u, f, rc, p=None):
    c.subscribe(f"solar/{DEVICE}/#", qos=1)

def on_message(c, u, m):
    if m.topic.endswith("/telemetry"):
        print(f"{m.topic} — {len(json.loads(m.payload)['items'])} reading")
    else:
        print(f"{m.topic} — {m.payload.decode()[:90]}")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tap")
c.on_connect, c.on_message = on_connect, on_message
c.connect(HOST, PORT, 30)
c.loop_start(); time.sleep(20); c.loop_stop()
EOF
```

Sẽ thấy **mỗi pin một message riêng** trên `solar/esp32-sim-001/<serial>/telemetry`, và
`solar/esp32-sim-001/status` mang `online` (retain).

Thử LWT: `kill -9` tiến trình simulator rồi chạy lại đoạn trên — `status` chuyển thành `offline`.
Đó là **Last Will** do broker phát hộ, đúng cơ chế phát hiện mất kết nối tức thì của S4-FW-02.

---

## 14. Khắc phục sự cố

### 14.1. Sửa `seed.yaml` nhưng chạy lại không thấy đổi

**State đang thắng** — đúng thiết kế (xem [§9](#9-trạng-thái-bền-vững--thứ-hay-gây-bất-ngờ-nhất)).

```bash
make clean-state
```

### 14.2. `Drop` tăng, hoặc log `BỎ batch — lỗi vĩnh viễn 4xx`

Backend từ chối dữ liệu. Xem mã lỗi trong log:

| Mã | Nguyên nhân thường gặp |
|---|---|
| 400 | payload sai, hoặc `deviceTimestamp` lệch quá 5 phút (đồng bộ giờ máy) |
| 401 | API key sai, **hoặc thiết bị đã bị `Decommissioned`** (xem 14.6) |
| 403 | thiếu scope, hoặc gửi cho pin **khác site** (#IoT2-18) |
| 409 | thiết bị `Disabled` |

### 14.3. Log `⚠ NHẬN THIẾU: x/y reading vào được`

Backend trả 2xx nhưng bỏ bớt. Hai nguyên nhân:

1. **Serial pin chưa được map cho thiết bị này** — kiểm bằng
   `python -m src.anomaly check` (in ra danh sách pin thiết bị được phép gửi).
2. **Giá trị ngoài dải vật lý** — kiểm `outlier_incident_count` ở [§8.2](#82-thiết-bị-có-được-ghi-nhận-không).

### 14.4. Cảnh báo Undervoltage/Overvoltage giả trên vài pin

Backend giao pin khác loại mà `battery_catalog` chưa khai. Xem
[§6.4](#64--battery_catalog--bắt-buộc-hiểu-nếu-không-sẽ-có-cảnh-báo-giả) — trong log sẽ có dòng
`… KHÔNG có trong seed/battery_catalog …`.

### 14.5. Kịch bản `normal` vẫn sinh `SohDegradation Warning` trên `BAT-2026-003`

**Đây không phải lỗi.** `BAT-2026-003` trong `battery_catalog` có `initial_soh: 82.0` và
`cycle_count: 1850` — một viên pin NMC đã dùng nhiều, và ngưỡng cảnh báo SOH của loại đó là
**85%**. Backend chấm đúng: pin này thật sự đã xuống cấp.

Kiểm ngưỡng:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
SELECT bt.name, tc.soh_warning_threshold, tc.soh_critical_threshold
FROM threshold_configs tc JOIN battery_types bt ON bt.id = tc.battery_type_id
WHERE tc.is_active;"
```

Muốn một lượt `normal` hoàn toàn sạch cảnh báo, nâng `initial_soh` của pin đó lên trên 85 trong
`battery_catalog` — nhưng cân nhắc: giá trị 82% đang phản ánh đúng tình trạng pin, và nó là cách
duy nhất để `--scenario normal` cho thấy luồng cảnh báo mà không cần dựng kịch bản.

### 14.6. Chạy dataset anomaly xong mà không thấy cảnh báo mới

Gần như chắc chắn là **khử trùng**. Xem [§11.4](#114-ba-luật-khử-trùng-khác-nhau--nguồn-của-mọi-ca-chạy-xong-mà-không-thấy-gì)
và chạy `make anomaly-verify` để lấy câu SQL dọn.

### 14.7. Thiết bị trả 401 với mọi request, kể cả provision

Đã bị `Decommissioned` (gửi > 50 số đo ngoài dải trong 1 giờ, hoặc đã chạy case 26). Không tự hồi
phục:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "UPDATE iot_devices SET status = 2, auto_decommissioned_at = NULL,
                          outlier_incident_count = 0, outlier_window_started_at = NULL
   WHERE device_code = 'ESP32-SIM-001';"
```

### 14.8. `make mock` báo "address already in use"

Cổng mặc định đã đổi sang **4099** để không đụng ApiGateway. Nếu vẫn kẹt:

```bash
make mock MOCK_PORT=4098
make demo MOCK_PORT=4098
```

### 14.9. MQTT không kết nối

```bash
docker exec solar-postgres psql -U postgres -d battery_db \
  -c "select device_code, mqtt_username from iot_devices where device_code='ESP32-SIM-001';"
```

- `mqtt_username` trống nghĩa là backend chưa cấp credential ⇒ simulator chạy HTTPS-only. Đó là
  trạng thái **hợp lệ**, không phải lỗi.
- Log `MQTT bị từ chối xác thực N lần liên tiếp` → sau 5 lần simulator tự gọi lại `/provision` để
  xin credential mới, có hạ nhiệt 15 phút.

### 14.10. HTTP 429 khi chạy dataset anomaly

Gateway giới hạn **60 request / 30 giây** cho lời gọi dùng `X-Api-Key`. Bộ chạy đã tôn trọng
`retryAfterSeconds` và tự thử lại tối đa 3 lần. Nếu vẫn dính, chạy tách nhóm case.

### 14.11. `make install` lỗi trên Python mới

```bash
rm -rf .venv && make install
```

Nếu vẫn lỗi, chỉ định phiên bản: `make install PY=python3.12`.

### 14.12. Token hết hạn (401 khi gọi `/api/admin/...`)

Lấy lại token theo [§5.1](#51-đăng-nhập-lấy-token).

---

## 15. Thiết kế nội bộ

### 15.1. Vòng đời một thiết bị mô phỏng

Bám sát `main.cpp` của firmware:

```
setup():
  state(NVS) → identity → cấu hình MQTT → bảng pin → đường lên → HTTP
  → cấu hình đã provision → OTA(begin: verify/rollback) → nguồn BMS
  → cảm biến → heartbeat → hàng đợi → MQTT

loop() mỗi 100ms:
  mqtt tick → kiểm sức khoẻ credential → ensureProvisioned
  → theo pollingInterval:
        đọc BMS đa nguồn → gom theo pin
        MQTT trước: publish <prefix>/<serial>/telemetry cho TỪNG pin
        fallback HTTPS CHỈ phần MQTT chưa đẩy được
        hỏng tạm thời → xếp hàng + backoff; hỏng vĩnh viễn (4xx) → BỎ
  → đẩy bù hàng đợi (1 batch/vòng, có backoff)
  → OTA tick (verify-mode chạy cả khi mất mạng)
  → heartbeat theo chu kỳ
  → SHT31 ambient mỗi 60 giây
  → MQ-2 / rò nước / cháy — chạy VÔ ĐIỀU KIỆN, kể cả offline
  → cập nhật đèn + log thống kê mỗi 60 giây
```

### 15.2. Chống mất và chống trùng dữ liệu

| Cơ chế | Chi tiết |
|---|---|
| Phân loại lỗi | 4xx (trừ 408/429) = vĩnh viễn → **BỎ**; 0/5xx/408/429 = tạm thời → xếp hàng + backoff 2s→5' ±20% |
| Đọc kết quả một phần | Đọc `{totalReceived, inserted, skipped}` trong 2xx và **la lên** |
| Publish một phần | Serial đã vào backend qua MQTT **không** bị gửi lại qua HTTPS |
| Mất mạng | Vẫn lấy mẫu + xếp hàng; khoá idempotency sinh **lúc lấy mẫu**, không phải lúc gửi |
| Hàng đợi | Trần 200 batch, đầy thì bỏ cái **cũ nhất**, đẩy bù 1 batch/vòng |
| Cảm biến an toàn | Chạy **vô điều kiện**; `detectedAt` là lúc **phát hiện**, không phải lúc gửi |

### 15.3. Mốc thời gian — chi tiết dễ sai nhất

Firmware sinh timestamp **độ phân giải giây** (`2026-06-13T08:15:42Z`), rồi vá mili-giây bằng
**chỉ số item trong batch** để không đụng khoá chính `(Time, BatteryAssetId)`:

```
items[0].time = 2026-06-13T08:15:42.000Z
items[1].time = 2026-06-13T08:15:42.001Z
items[2].time = 2026-06-13T08:15:42.002Z
```

Simulator làm y hệt. `time` và `deviceTimestamp` của cùng một item **luôn bằng nhau**.

### 15.4. Hai điểm cố ý khác firmware

| Điểm | Vì sao |
|---|---|
| `LocalQueueDepth` gửi **số thật** | Firmware vẫn hard-code `0` kèm chú thích "Sprint 3 sẽ có queue thật", trong khi hàng đợi đã tồn tại từ Sprint 3. Gửi 0 chỉ để "giống bug" là làm hỏng đúng tính năng mà trường này sinh ra. |
| Nguồn phụ **sao chép giá trị BMS** thay vì gửi `0.0` | Backend coi `voltage ∉ (0, 1000]` là outlier và **> 50 outlier/giờ thì khoá thiết bị**. DS18B20 chỉ đo nhiệt; gửi `voltage: 0.0` với chu kỳ 5 giây là 720 outlier/giờ. |

Danh sách đầy đủ ở `README.md §7.2`.

---

## 16. Bảo trì

### 16.1. Xoay API key

```bash
TOKEN=...   # xem §5.1
curl -s -X POST http://localhost:4001/api/admin/iot-devices/<deviceId>/rotate-key \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Chép `rawApiKey` mới vào `seed.yaml`, rồi `make clean-state` để thiết bị provision lại.

### 16.2. Xoá hàng đợi

```bash
rm -f logs/queue/*.jsonl                       # tất cả
rm -f logs/queue/ESP32-SIM-001.jsonl           # một thiết bị
```

### 16.3. Dọn sạch để demo lại từ đầu

```bash
make clean-state                               # phía simulator
python -m src.anomaly verify                   # in các câu SQL phía backend
```

### 16.4. Xoá hẳn mọi thứ

```bash
make clean          # xoá .venv + hàng đợi + state
```

### 16.5. Khi thêm pin hoặc thiết bị mới vào backend

1. Thêm pin vào `battery_catalog` trong `seed.yaml` với **đúng điện áp danh định**
   (xem [§6.4](#64--battery_catalog--bắt-buộc-hiểu-nếu-không-sẽ-có-cảnh-báo-giả)).
2. Nếu là thiết bị mới: tạo qua [§5.4](#54-tạo-thiết-bị-iot) với `apiKeyScopes: 15`, thêm vào
   `devices:` trong `seed.yaml`.
3. `make clean-state` rồi chạy lại.
4. `make test` — bộ test có bất biến kiểm dataset anomaly vẫn khớp ngưỡng backend.

---

## Tài liệu liên quan

| Tài liệu | Nội dung |
|---|---|
| `README.md` | Tổng quan, bảng đối chiếu firmware, chi tiết bộ dataset anomaly |
| `config/anomaly-dataset.yaml` | 26 case demo cảnh báo, kèm ngưỡng thật của backend |
| `config/seed.yaml` | Thiết bị, pin, danh mục pin, cấu hình MQTT |
| `capstone/iot/iot-backend-contract-gaps.md` | Audit các điểm lệch contract IoT ↔ Backend |
| `capstone/iot/firmware-esp32/src/**` | Firmware thật mà simulator bám theo |

---

## Hỗ trợ

Khi báo lỗi, kèm theo:

```bash
python -m src.main --once --no-dashboard --log-file /tmp/sim.log ; tail -40 /tmp/sim.log
python -m src.anomaly check
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "select device_code, status, api_key_scopes, last_seen_at, outlier_incident_count
   from iot_devices where device_code like 'ESP32-SIM-%';"
```
