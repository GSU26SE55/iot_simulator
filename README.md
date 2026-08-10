# IoT Simulator — bản demo `iot → backend` không cần phần cứng

Mô phỏng **thiết bị ESP32-S3 chạy firmware thật** của repo `capstone/iot`, nhưng bỏ hết phần cứng:
không BMS, không pin, không RS485/I2C/1-Wire, không Wi-Fi riêng. Mọi thứ **đi ra dây** — endpoint,
header, hình dạng JSON, thứ tự trạng thái, cách xử lý lỗi — thì giống thiết bị thật 1:1.

Mục tiêu: chạy được **toàn bộ luồng `iot ↔ backend`** trên một chiếc laptop.

```
┌────────────────────┐        HTTPS  +  MQTT        ┌──────────────────┐
│  iot-simulator     │ ───────────────────────────► │  BatteryService  │
│  (thay ESP32-S3)   │ ◄─────────────────────────── │  + broker + DB   │
└────────────────────┘   provision · telemetry ·    └──────────────────┘
                          heartbeat · ambient ·
                          sự cố · OTA · lệnh downlink
```

Đối chiếu từng dòng với `capstone/iot/firmware-esp32/src/**` — mỗi module Python đều ghi rõ nó
mirror file C++ nào.

---

## 1. Chạy trong 2 phút

```bash
make install                      # tạo .venv + cài phụ thuộc

# (A) Chạy với backend THẬT
IOT_BASE_URL=http://localhost:4001 make run

# (B) Chạy với backend GIẢ kèm sẵn — không cần dựng gì thêm
make mock                         # cửa sổ 1: backend giả, kiểm hợp đồng nghiêm ngặt
make demo                         # cửa sổ 2: simulator trỏ vào backend giả
```

`make demo` sẽ: provision → nhận cấu hình + bảng pin từ backend → đẩy telemetry đa nguồn mỗi 5s →
heartbeat mỗi 60s → ambient mỗi 60s → và cập nhật bảng trạng thái trực tiếp trên terminal.

Lệnh khác:

```bash
make once            # gửi một batch rồi thoát (smoke test)
make test            # 283 unit test
make clean-state     # xoá state bền vững → thiết bị "mới bóc hộp", provision lại từ đầu
make mock-ota        # backend giả có offer OTA 1.2.0 (tải thật + xác minh SHA-256 thật)
```

---

## 2. Nó làm được đúng những gì `iot` làm với backend

| Luồng | Endpoint / topic | Trạng thái |
|---|---|---|
| **Provision** | `POST /api/iot-devices/provision` | ✅ đọc **đủ** response: chu kỳ, siteId, ntpServer, **6 trường MQTT**, **`batteryMappings[]`** |
| **Telemetry** | `POST /api/sensor-readings/batch` | ✅ contract production, đa nguồn (BMS + INA226 + DS18B20), `Idempotency-Key` |
| **Telemetry (MQTT)** | `<prefix>/<serial>/telemetry` | ✅ MQTT-first, **mỗi pin một message**, fallback HTTPS |
| **Heartbeat** | `POST /api/iot-devices/heartbeat` | ✅ đúng tập trường của `IotDeviceHeartbeatCommand`, đọc cảnh báo lệch giờ |
| **Ambient (SHT31)** | `POST /api/ambient/readings/batch` | ✅ chu kỳ 60s, `source=1` (số), không có `solarIrradiance` |
| **Sự cố môi trường** | `POST /api/environmental-incidents` | ✅ MQ-2 → `GasLeak(3)`, rò nước → `Flood(4)`, có warm-up + hạ nhiệt 5' |
| **OTA** | `GET firmware-check` · `PUT firmware-update-log/{id}` | ✅ tải artifact thật, xác minh SHA-256 thật, verify-mode 2', **rollback**, chống re-OTA loop |
| **Lệnh downlink** | `<prefix>/cmd` → `<prefix>/cmd/ack` | ✅ `set_interval` · `request_heartbeat` · `trigger_ota`, ack `ok/failed/unknown/rejected` |
| **Trạng thái** | `<prefix>/status` | ✅ `online` retain + **LWT `offline`** retain (phát hiện mất kết nối tức thì) |
| **Hàng đợi ngoại tuyến** | — | ✅ trần 200 batch, drop-oldest, đẩy bù 1 batch/vòng có backoff |
| **Xin lại credential** | `POST /provision` | ✅ broker từ chối xác thực 5 lần → re-provision, hạ nhiệt 15' |

Có bản đối chiếu chi tiết ở **§7 — Bảng đối chiếu firmware**.

---

## 3. Chọn contract

```yaml
backend:
  contract_version: iot2-production     # MẶC ĐỊNH
```

| Giá trị | Ý nghĩa |
|---|---|
| **`iot2-production`** | Contract Sprint 3 — **contract DUY NHẤT mà firmware ESP32 thật chạy**. Có đủ provision / heartbeat / MQTT / ambient / sự cố / OTA / `Idempotency-Key`. Định danh pin bằng `batteryAssetSerial`. |
| `current` | Contract Sprint 1 cho backend đời cũ: chỉ `POST /api/sensor-readings/batch` với `items[].batteryAssetId` (Guid), header chỉ `X-Api-Key`. Mọi tính năng trên **tự tắt**. Bắt buộc điền `batteries[].battery_asset_id`. |

Đổi bằng `seed.yaml` hoặc `IOT_CONTRACT_VERSION=current`.

---

## 4. Nguồn chân lý của cấu hình

`config/seed.yaml` đóng đúng vai trò của `firmware-esp32/include/config.h`: **chỉ là đường lui**.

```
lần chạy đầu:   seed.yaml ──► POST /provision ──► backend trả cấu hình thật
                                                       │
lần chạy sau:   logs/state/<device>.nvs.json ◄─────────┘   (tương đương NVS của ESP32)
```

Backend quyết định: `pollingIntervalSeconds`, `heartbeatIntervalSeconds`, `siteId`, `ntpServer`,
6 trường MQTT (`mqttBrokerHost/Port/UseTls/TopicPrefix/Username/Password`), và
`batteryMappings[]` (serial ↔ unitId ↔ sensorSourceCode).

Trạng thái bền vững cũng giữ **version firmware đang chạy** (để OTA có ý nghĩa qua các lần chạy)
và **máy trạng thái OTA** (`otaPend/otaBootN/otaRb/otaBadVer/…`).

```bash
make clean-state                       # hoặc: python -m src.main --clear-state
python -m src.main --no-persist        # không ghi state — mỗi lần chạy là thiết bị mới tinh
```

---

## 5. Kịch bản

Đặt ở `devices[].scenario`, hoặc `--scenario`, hoặc **gửi lệnh MQTT lúc đang chạy**:
`{"cmdId":"x","type":"set_scenario","params":{"scenario":"overheat"}}`.

| Nhóm | Kịch bản |
|---|---|
| Pin (BMS) | `normal` · `overheat` · `overvoltage` · `undervoltage` · `low_soc` · `rapid_discharge` · `abnormal_charging` · `soh_degradation` · `bms_error` |
| Chéo nguồn | `sensor_mismatch` — INA226 lệch > 0,5V và DS18B20 lệch > 5°C, đủ vượt ngưỡng `SensorMismatch` |
| Môi trường | `high_ambient_temp` · `high_humidity` · `high_temp_humidity_combo` |
| Sự cố an toàn | `gas_leak` · `smoke` → `GasLeak(3)` · `water_leak` → `Flood(4)` · `fire_detected` → `FireDetected(2)` |
| Vận hành | `device_offline` (dừng sau 60s để demo LWT) · `clock_skew` (+10 phút, kích hoạt kiểm tra #IoT2-15) |

---

## 6. Cấu trúc mã nguồn

Mỗi file ghi rõ nó mirror file nào bên `capstone/iot/firmware-esp32/src/`.

```
src/
├── main.py            CLI                          ← (không có bên firmware)
├── device.py          vòng lặp chính               ← main.cpp
├── config.py          nạp seed                     ← include/config.h
│
├── timeutil.py        mốc thời gian ISO8601        ← net/time_sync.cpp
├── backoff.py         backoff + phân loại lỗi      ← net/backoff.{h,cpp}
├── policy.py          các quyết định thuần         ← core/{ingest,ota_check,reprovision}_policy.h
├── payload.py         dựng JSON batch              ← core/payload.{h,cpp} + core/reading_filter.h
├── ingest_result.py   đọc `inserted/skipped`       ← core/ingest_result.{h,cpp}
├── net_rules.py       kiểm định danh + topic       ← core/{identity_validation,net_config_rules}.h
├── battery_map.py     bảng ánh xạ pin              ← core/battery_map_codec.h + config/battery_map_runtime.cpp
├── nvs.py             state bền vững               ← config/nvs_store.cpp
├── link.py            trạng thái đường lên          ← net/wifi_manager.cpp
├── led.py             đèn trạng thái (8 màu/kiểu)  ← ui/{status_led,led_palette}.h
│
├── http_client.py     REST                         ← net/http_client.cpp
├── mqtt_config.py     cấu hình broker runtime      ← config/mqtt_config.cpp
├── mqtt_client.py     MQTT                         ← net/mqtt_client.cpp
├── provision.py       luồng provision              ← provision/provision.cpp
├── heartbeat.py       heartbeat                    ← telemetry/heartbeat.cpp
├── cmd.py             lệnh downlink                ← cmd/{cmd_logic,command_handler}.cpp
├── ota.py             OTA + rollback               ← ota/ota_update.cpp + ota/ota_decision.h
├── bms.py             mô hình pin                  ← bms/mock_bms.cpp
├── dashboard.py       bảng trạng thái terminal     ← (không có bên firmware)
├── sensors/
│   ├── redundant.py         INA226 + DS18B20       ← bms/mock_bms.cpp (multi-source)
│   ├── ambient.py           SHT31                  ← sensor/sht31.cpp
│   ├── environmental.py     reporter sự cố         ← sensor/environmental_incident.cpp
│   ├── incident_trigger.py  cạnh lên + hạ nhiệt    ← sensor/incident_trigger.h
│   ├── mq2.py               MQ-2                   ← sensor/mq2.cpp
│   ├── water_leak.py        rò nước                ← sensor/water_leak.cpp
│   └── fire_watch.py        báo cháy               ← ⚠ RIÊNG của simulator
└── anomaly.py         bộ chạy dataset anomaly      ← ⚠ RIÊNG của simulator (§11)

config/anomaly-dataset.yaml   20 case demo cảnh báo — xem §11
tools/mock_backend.py         backend giả, kiểm hợp đồng nghiêm ngặt
tests/                        283 unit test
```

---

## 7. Bảng đối chiếu firmware

### 7.1 Đã mô phỏng đúng

| Hạng mục | Chi tiết |
|---|---|
| Mốc thời gian | `%Y-%m-%dT%H:%M:%SZ` — **độ phân giải giây**, đúng `net::isoNow` |
| Mili-giây per-item | `time`/`deviceTimestamp` = mốc chung + `.{index:03d}` → khoá chính `(Time, BatteryAssetId)` không đụng |
| Nhóm MQTT | mỗi pin một message, index ms **đánh lại từ 0** trong mỗi nhóm |
| Header | `X-Api-Key` + `X-Device-Code` + `Accept`; `Idempotency-Key` **chỉ** cho ingest |
| Phân loại lỗi | 4xx (trừ 408/429) = vĩnh viễn → **BỎ**; 0/5xx/408/429 = tạm thời → xếp hàng + backoff 2s→5' ±20% |
| Nhận thiếu | đọc `{totalReceived, inserted, skipped}` trong 2xx và **la lên** |
| Publish một phần | serial đã vào backend qua MQTT **không** bị gửi lại qua HTTPS |
| Mất mạng | vẫn lấy mẫu + xếp hàng, khoá idempotency sinh **lúc lấy mẫu** |
| Hàng đợi | trần 200 batch, drop-oldest, đẩy bù 1 batch/vòng có backoff |
| Cảm biến an toàn | chạy **vô điều kiện** kể cả khi mất mạng; `detectedAt` = lúc **phát hiện** |
| OTA | Downloading→Installing→Success / Failed / Skipped / RolledBack; boot-counter; chặn version hỏng sau 3 lần |
| Broker | LWT `offline` retain, `online` retain, QoS **0**, trần gói **4096 byte** |
| Xác thực broker | đếm riêng lỗi xác thực (rc 4/5); 5 lần → re-provision, hạ nhiệt 15' |
| Định danh | từ chối giá trị có khoảng trắng/CR/LF (chống tiêm header `X-Api-Key`) |
| Đèn | 8 trạng thái + 3 kiểu nháy, trạng thái **mạng** ưu tiên trên trạng thái hàng đợi |

### 7.2 Cố ý khác — và vì sao

| Điểm khác | Lý do |
|---|---|
| **`LocalQueueDepth` gửi số THẬT** | Firmware vẫn hard-code `0` kèm chú thích "Sprint 3 sẽ có queue thật", trong khi hàng đợi đã tồn tại từ Sprint 3. Gửi 0 chỉ để "giống bug" là làm hỏng đúng tính năng mà trường này sinh ra. |
| **Nguồn phụ sao chép giá trị BMS** thay vì gửi `0.0` | Backend coi `voltage ∉ (0,1000]` là outlier và **>50 outlier/giờ thì tự khoá thiết bị**. DS18B20 chỉ đo nhiệt; gửi `voltage: 0.0` với chu kỳ 5s là 720 outlier/giờ. `mockGenerateMultiSource` của firmware sao chép giá trị BMS đúng vì lý do này. |
| **Đẩy hàng đợi không bị chặn theo trạng thái mạng** | Firmware có driver Wi-Fi tự dò lại mạng; simulator không có, "có mạng" chỉ suy ra được từ kết quả request. Chặn theo trạng thái mạng thì một khi mất kết nối sẽ không còn request nào để phát hiện lúc backend sống lại. Vẫn bị backoff ghìm nên không nện backend. |
| **Trạng thái đường lên khởi đầu là "có kết nối"** | Ảnh phản chiếu của firmware: Wi-Fi đã associate trước lần POST đầu tiên. |
| Lệnh `set_scenario` | Mở rộng riêng để demo; firmware trả `unknown`. |
| Kịch bản `fire_detected` | Mở rộng riêng; firmware **không có** đường báo cháy (chỉ MQ-2→GasLeak và rò nước→Flood). |

### 7.3 Không có (và không cần)

Wi-Fi/AP `SolarGW-xxxx`, trang cấu hình web + quét QR, Serial CLI, Modbus RS485/JK-BMS, I2C/1-Wire
thật, ghi OTA partition + rollback bootloader, TLS bằng CA cert nạp từ LittleFS.
Đây đều là phần **phần cứng / cấu hình tại chỗ**, không nằm trên đường `thiết bị ↔ backend`.

---

## 8. Điều kiện phía backend

Ba thứ dưới đây không phải lỗi của simulator, nhưng làm hỏng demo nếu bỏ qua
(nguồn: `capstone/iot/iot-backend-contract-gaps.md`):

1. **Scope `EnvironmentalIngest` (bitmask 4).** `EdgeDeviceDefault` = `SensorIngest | DeviceHeartbeat
   | FirmwareCheck` = **11, KHÔNG có 4**. Thiếu nó thì ambient + sự cố môi trường trả **403**, và
   403 là lỗi vĩnh viễn nên bị BỎ. Cấp key với scope tổng = **15**.
2. **`Mqtt__Enabled=true`.** Mặc định của backend là `false`. Broker vẫn nhận publish bất kể
   backend có subscribe hay không ⇒ tắt bridge là **mất telemetry hoàn toàn trong im lặng**.
3. **Ngưỡng outlier.** >50 reading ngoài dải vật lý trong một giờ ⇒ backend tự chuyển thiết bị sang
   `Decommissioned`, mọi request sau trả 409. Kịch bản `normal` của simulator luôn nằm trong dải
   (có test chặn), nhưng các kịch bản cực đoan chạy dài thì cần biết điều này.

---

## 9. Backend giả (kiểm hợp đồng nghiêm ngặt)

`tools/mock_backend.py` **khắt khe hơn ASP.NET Core một cách có chủ ý**: backend thật bind JSON
không phân biệt hoa thường và bỏ qua trường lạ, nên nó *che mất* chỗ payload sai. Mock này từ chối
thẳng để lỗi lộ ra ngay.

```bash
python3 tools/mock_backend.py --port 4001
python3 tools/mock_backend.py --port 4001 --offer-version 1.2.0        # thử OTA trọn vòng
python3 tools/mock_backend.py --port 4001 --mqtt-host localhost \
        --mqtt-port 1883 --mqtt-pass demo                              # cấp broker qua provision

curl -s localhost:4001/ | python3 -m json.tool     # tóm tắt + danh sách vi phạm hợp đồng
```

Nó kiểm: header bắt buộc, định dạng ISO8601, kiểu số nguyên của `MemoryUsageMb`, enum phải là số,
`Idempotency-Key` (trả lại kết quả cũ khi trùng), trùng khoá chính trong cùng batch, dải vật lý
(→ đếm vào `skipped`), `notes ≤ 1000`, `failureReason ≤ 500`, `detectedAt` không ở tương lai quá 5'.

---

## 10. Kiểm thử

```bash
make test          # 283 test, chạy < 0,3s, không cần mạng
```

| Tệp | Phủ |
|---|---|
| `test_payload.py` | hình dạng JSON hai contract, mili-giây per-item, tag chéo nguồn, bẫy outlier |
| `test_bms.py` | 9 kịch bản pin + bất biến "không sinh outlier" |
| `test_provision.py` | envelope, biên, 6 trường MQTT, `batteryMappings[]`, retry 30s, state qua khởi động lại |
| `test_resilience.py` | phân loại lỗi, backoff, trần hàng đợi, đẩy bù, GH-737/740/748, đèn |
| `test_ota.py` | trọn vòng OTA, checksum sai, rollback, boot-loop, chặn version hỏng |
| `test_sensors.py` | cạnh lên + hạ nhiệt, MQ-2→GasLeak, `detectedAt` lùi đúng, SHT31 60s |
| `test_mqtt_client.py` | topic, trần gói, đếm lỗi xác thực, **chống tự khoá luồng mạng** |
| `test_http_contract.py` | header, method, route, body — chạy trên client THẬT |
| `test_features.py` | lệnh downlink, heartbeat, đèn, luật định danh, re-provision |
| `test_anomaly.py` | dataset anomaly: tính nhất quán, chia đợt, ô thời gian, từng case vượt đúng ngưỡng |

Đã kiểm end-to-end: backend giả (0 vi phạm hợp đồng), broker MQTT thật (telemetry per-pin, ack
downlink, LWT `offline` sau khi `kill -9`), và ngắt backend giữa chừng (hàng đợi lên 6 batch,
backoff 1,8s→14s, đẩy bù đủ, **không mất và không trùng bản ghi nào**).

---

## 11. Demo cảnh báo (anomaly)

`config/anomaly-dataset.yaml` + `python -m src.anomaly` đẩy **đúng số đo cần thiết** để backend
dựng từng loại cảnh báo. Payload đi qua **cùng đường gửi** với lúc chạy bình thường
(`payload.py` + `http_client.py`) — không có nhánh riêng cho demo.

```bash
make anomaly-list      # 20 case + điều kiện của từng case
make anomaly-check     # backend đã đủ điều kiện chưa (KHÔNG gửi gì)
make anomaly-dry       # in payload thật sẽ gửi
make anomaly           # chạy toàn bộ (~90 giây)
make anomaly-verify    # in câu SQL kiểm chứng
```

### Kết quả đã kiểm trên backend thật

18/18 case gửi được đều nổ đúng loại, đúng mức, đúng giá trị; case 20 đúng như thiết kế là
**không** sinh cảnh báo nào:

| Cảnh báo | Mức | Pin | Giá trị |
|---|---|---|---|
| Overheat | Warning / Critical | BAT‑2026‑001 / ‑002 | 62 °C / 72 °C |
| Undertemp | Warning / Critical | BAT‑2026‑001 | −12 °C / −18 °C |
| Overvoltage · Undervoltage | Critical | BAT‑2026‑001 | 15,2 V · 9,5 V |
| LowSoc | Warning / Critical | BAT‑2026‑001 / ‑003 | 15 % / 8 % |
| RapidDischarge · AbnormalCharging | Critical | BAT‑2026‑REAL‑001 | −130 A · 45 A |
| SohDegradation | Warning / Critical | BAT‑2026‑001 / ‑004 | 82 % / 72 % |
| HighInternalResistance · CellImbalance | Critical | BAT‑2026‑001 | 65 mΩ · 135 mV |
| HighAmbientTemp · HighHumidity · HighTempHumidityCombo | Critical/Warning/Critical | site | 48 °C · 83 % · 39 °C |
| SensorMismatch | Warning | BAT‑2026‑001 | 0,6 V |

### Bốn chỗ tài liệu gốc ghi SAI so với backend đang chạy — đã sửa trong dataset

1. **Overheat Critical delta là +5 °C**, không phải +8 (`AnomalyRules.OverheatCriticalDeltaC`).
2. **Ambient: cảnh báo 38 / nguy cấp 42; kết hợp là temp ≥ 35 VÀ ẩm ≥ 75** (tài liệu ghi 40/45 và
   38/85). Kèm theo: 39 °C + 87 % sinh **ba** cảnh báo chứ không phải một — luật kết hợp là nhánh
   độc lập, không phải `else`.
3. **RapidDischarge/AbnormalCharging không thể demo trên pin NMC**: `current_max_charge/discharge`
   của NMC (và của LiFePO4 12V, NCA) đều `NULL` ⇒ luật không bao giờ chạy. Loại pin **duy nhất**
   trong DB có hai ngưỡng đó là **LiFePO4 24V 30Ah (`BAT-2026-REAL-001`)** — sạc 30 A, xả 100 A.
4. **Mốc thời gian cố định `2026-08-08` sẽ không bao giờ được xét**: bộ quét chỉ nhìn lại 20 giây,
   và `deviceTimestamp` lệch quá 5 phút thì backend trả 400 cho cả batch. Bộ chạy luôn tự đặt mốc
   "vừa xong".

### Ba ràng buộc vận hành khiến "gửi một phát" KHÔNG bao giờ đủ

Đây là phần khó nhất, và là lý do bộ chạy có cấu trúc như hiện tại:

- **Chống nhiễu đếm theo bản đã ghi vào DB.** Mỗi lượt quét đọc số breach đã persist *trước* lượt
  đó rồi cộng 1 cho số đo đang xét — breach của chính lượt này còn đang chờ ghi nên không được
  đếm. Gửi 6 số đo một lượt ⇒ cả 6 đều ra `1 < 5` ⇒ **bị bỏ qua hết**.
  ⇒ Bộ chạy chia **2 đợt**: đợt 1 đúng 5 số đo, chờ 14 giây cho chúng được ghi, rồi đợt 2 gửi
  thêm 2 — lúc này `5 + 1 = 6 ≥ 5` ngay ở lượt quét đầu tiên của đợt 2.
- **Khoá chính `(Time, BatteryAssetId)`.** Hai case cùng pin chạy trong cùng một giây sẽ đụng
  khoá; backend đếm vào `skipped` và case sau mất dữ liệu trong im lặng.
  ⇒ Bộ chạy cấp cho mỗi case một **giây riêng theo từng pin**.
- **Ghép cặp chéo nguồn quét cả cửa sổ 60 giây.** Backend ghép MỌI số đo `primary` với MỌI số đo
  `IotGateway` của cùng pin trong 60 giây. Các case phía trên vừa đẩy 15,2 V / 9,5 V / 62 °C /
  −18 °C lên cùng pin đó, nên gửi cặp chéo nguồn ngay sau sẽ ghép nhầm — **đã quan sát được 57
  cảnh báo SensorMismatch giả**.
  ⇒ Bộ chạy chờ **65 giây** rồi mới gửi cặp chéo nguồn (`--fast` để bỏ qua, chấp nhận rác).

Ngoài ra API Gateway giới hạn **60 request / 30 giây** cho lời gọi dùng `X-Api-Key`; bộ chạy gộp
cả đợt vào một batch và tôn trọng `retryAfterSeconds` khi bị 429.

### Điều kiện phải bật trước ở backend

```bash
# case 13/14 (Tier 2) — hai cột này mặc định NULL ⇒ luật không bao giờ chạy
docker exec solar-postgres psql -U postgres -d battery_db -c \
  "UPDATE threshold_configs SET internal_resistance_max_milliohm = 50,
                                cell_voltage_delta_max_mv = 100 WHERE is_active;"
```

⚠ **Chạy lại trong 30 phút sẽ không thấy cảnh báo mới** — backend gộp cảnh báo trùng
(`AnomalyEngine__DedupWindowMinutes=30`). `make anomaly-verify` in sẵn câu SQL dọn để demo lại
từ đầu.

---

## 12. Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `IOT_BASE_URL` | `https://localhost:7200` | gốc backend |
| `IOT_TLS_VERIFY` | `true` | tắt khi dùng chứng chỉ tự ký |
| `IOT_CONTRACT_VERSION` | `iot2-production` | `current` cho backend đời cũ |
| `IOT_API_KEY` | — | key mặc định cho thiết bị chưa khai trong seed |
| `IOT_MQTT_ENABLED` | `false` | bật đường MQTT-first |
| `IOT_MQTT_HOST/PORT/TLS/USERNAME/PASSWORD` | — | đường lui khi backend tắt MQTT |
| `IOT_SEED_FILE` | `config/seed.yaml` | |
| `IOT_QUEUE_DIR` | `logs/queue` | |
| `IOT_STATE_DIR` | `logs/state` | tương đương NVS |
| `IOT_PERSIST_STATE` | `true` | `false` = thiết bị mới tinh mỗi lần chạy |
| `IOT_OTA_ENABLED` | `true` | |
| `IOT_LOG_LEVEL` | `INFO` | |

---

## 13. Tạo thiết bị trên backend

```bash
export ADMIN_TOKEN=<JWT admin>
make provision          # đọc seed.yaml → POST /api/v1/admin/iot-devices → in rawApiKey
```

`rawApiKey` backend **chỉ trả một lần** — chép ngay vào `seed.yaml` hoặc `.env`.
Nhớ cấp scope tổng **15** (xem §8).
