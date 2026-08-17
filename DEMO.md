# Hướng dẫn seed dữ liệu & 4 kịch bản demo

Tài liệu này mô tả cách đưa hệ thống về trạng thái sạch, chạy bộ case bằng IoT simulator,
và trình diễn bốn kịch bản demo.

Mọi con số trong tài liệu đã được kiểm chứng trên stack thật ngày 2026-08-17.

---

## 0. Cần có sẵn

| Thứ | Kiểm tra |
|---|---|
| Docker + Docker Compose | `docker --version` |
| Python 3.11+ | `python3 --version` |
| Bốn repo cùng thư mục cha | `backend/`, `iot_simulator/`, `frontend/`, `mobile/` |

Thứ tự làm lần đầu:

```
§2.1 khởi động backend
  → §2.9 tạo dữ liệu nền   (chỉ khi battery_assets trống)
  → §2.4 cài simulator
  → §2.2 dọn dữ liệu giao dịch
  → §2.3 giữ đúng một viên pin
  → §2.7 kiểm dữ liệu nền
  → §2.6 mở panel
  → §4  chạy demo
```

Những lần sau chỉ cần **§2.2 → §2.3 → §2.6**.

---

## 1. Ngưỡng của viên pin demo

`BAT-2026-REAL-001` — LiFePO4 24V 30Ah, site Solar Farm Long An.

| Chỉ số | Ngưỡng | Ghi chú |
|---|---|---|
| Nhiệt độ | **−10 … 55 °C** | |
| Điện áp | 20,0 – 29,2 V | pack 8S ⇒ 2,5–3,65 V mỗi cell |
| SOC | cảnh báo 20 % · nguy cấp 10 % | |
| SOH | cảnh báo 85 % · nguy cấp **80 %** | |
| Dòng | 15 A sạc / 30 A xả | |

Quy tắc mức nghiêm trọng: **vượt ngưỡng → Warning**, **vượt thêm 5 đơn vị → Critical**.
Ví dụ nhiệt: 57 °C là Warning (55 < 57 < 60), 67 °C là Critical (> 60).

> ⚠️ Hai con số **55 °C** và **80 %** phải khớp với hardcode trong `ai-module`
> (`CHEMISTRY_TEMP_PROFILES["LFP"]`, `EOL_SOH`). ai-module chưa có đường nhận ngưỡng động từ
> `threshold_configs`, nên DB được chỉnh theo AI — chiều ngược với thiết kế đúng, nhưng là chiều
> duy nhất khả thi hiện tại. Đổi ngưỡng ở một nơi mà quên nơi kia thì cùng một viên pin sẽ được
> hai hệ thống chấm khác nhau ngay trên màn hình demo.

---

## 2. Chuẩn bị

### 2.1 Khởi động backend

```bash
cd backend
docker compose -f docker-compose.yml up -d
```

Chờ tới khi gateway trả 200:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4001/health
```

> `solar-ai-module-grpc` báo `unhealthy` là **bình thường** — healthcheck gọi HTTP vào cổng gRPC
> nên luôn fail. Service vẫn chạy; kiểm bằng `docker logs solar-ai-module-grpc | grep listening`.
> Đừng restart: nạp lại model PyTorch mất 30–60 giây.

### 2.2 Dọn dữ liệu giao dịch

Xoá alert / ticket / số đo, **giữ nguyên** dữ liệu nền (site, pin, ngưỡng, thiết bị, KB, tài khoản):

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
BEGIN;
DELETE FROM alerts;
DELETE FROM environmental_incidents;
DELETE FROM sensor_readings;
DELETE FROM ambient_readings;
DELETE FROM noise_breach_events;
DELETE FROM sensor_ingest_idempotency_records;
DELETE FROM outbox_messages;
UPDATE battery_assets SET last_sensor_reading_at = NULL;
COMMIT;"

docker exec solar-postgres psql -U postgres -d ticket_db -c "
BEGIN;
DELETE FROM sla_pause_events;
DELETE FROM sla_timers;
DELETE FROM ticket_activities;
DELETE FROM ticket_battery_assets;
DELETE FROM alert_ticket_saga_states;
DELETE FROM outbox_messages;
DELETE FROM tickets;
COMMIT;"

docker exec solar-postgres psql -U postgres -d notification_db -c "
DELETE FROM notifications; DELETE FROM notification_groups;"
```

Bốn bảng dễ bị bỏ sót, mỗi bảng gây một kiểu hỏng riêng:

| Bảng | Bỏ sót thì sao |
|---|---|
| `sensor_ingest_idempotency_records` | Payload cũ bị coi là trùng → **không ghi số đo, không sinh alert, không báo lỗi** |
| `noise_breach_events` | Bộ đếm chống nhiễu còn tồn → case Warning nổ sớm hơn đáng lẽ |
| `sla_timers` | Khoá ngoại chặn `DELETE FROM tickets` |
| `alerts` | Cửa sổ khử trùng 30 phút vẫn hiệu lực → case chạy sau bị gộp |

`ticket_audit_logs` / `ticket_audit_outbox` **không xoá được** — có trigger DB chặn (append-only
theo thiết kế). Chúng không hiện trên UI và không ảnh hưởng demo.

### 2.3 Giữ đúng một viên pin

Seeder **tự chạy lại mỗi lần container khởi động** và tạo lại 4 viên pin + 3 loại pin đã xoá.
Sau mỗi lần restart backend, chạy lại:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
BEGIN;
DELETE FROM noise_breach_events;
DELETE FROM battery_assets WHERE serial_number <> 'BAT-2026-REAL-001';
DELETE FROM battery_types  WHERE name <> 'LiFePO4 24V 30Ah';
COMMIT;"
```

`noise_breach_events` phải xoá **trước** — khoá ngoại `FK_noise_breach_events_battery_assets_battery_asset_id`
chặn việc xoá pin nếu còn bản ghi tham chiếu. Nếu §2.2 đã chạy thì bảng này rỗng sẵn; câu lệnh trên
để an toàn khi chỉ chạy riêng bước này sau một lần restart.

Bốn viên kia **không có `threshold_config`** — chạy case lên chúng sẽ im lặng, không alert.

### 2.4 Cài đặt simulator (chỉ làm một lần)

```bash
cd iot_simulator
make install                # tạo .venv + cài phụ thuộc
cp env.example.txt .env     # nếu chưa có .env
```

`make install` dựng `.venv` bằng `python3` của máy và cài `requests`, `PyYAML`, `rich`,
`paho-mqtt`, `python-dotenv`.

Ba biến trong `.env` cần đúng thì simulator mới gửi được dữ liệu:

| Biến | Giá trị demo | Ý nghĩa |
|---|---|---|
| `IOT_BASE_URL` | `http://localhost:4001` | Địa chỉ ApiGateway |
| `IOT_API_KEY` | khoá của `ESP32-SIM-001` | Xác thực thiết bị khi gửi số đo |
| `IOT_SEED_FILE` | `config/seed.yaml` | Khai báo thiết bị ↔ pin |

`IOT_API_KEY` phải **khớp chính xác** khoá trong DB. Lấy giá trị thật:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -tAc \
  "select api_key_plaintext from iot_devices where device_code='ESP32-SIM-001';"
```

Dán kết quả vào `.env` (và vào `config/seed.yaml` mục `devices[].api_key` nếu khác). Sai khoá thì
mọi lần gửi trả **401** và không có số đo nào vào DB.

Kiểm tra trước khi chạy — lệnh này **không gửi gì**, chỉ đối chiếu điều kiện:

```bash
make anomaly-check
```

### 2.5 Kiểm tra `seed.yaml` chỉ khai đúng một thiết bị

```bash
.venv/bin/python -c "
import yaml
d = yaml.safe_load(open('config/seed.yaml'))
for x in d['devices']:
    print(x['device_code'], '->', [b['serial'] for b in x.get('batteries', [])])"
```

Kết quả mong đợi:

```
ESP32-SIM-001 -> ['BAT-2026-REAL-001']
```

Nếu còn `ESP32-SIM-002`, hãy chú thích khối đó lại. Nó trỏ vào **cùng viên pin**, nên vừa lỗi xác
thực (thiết bị đã bị xoá khỏi DB) vừa bơm số đo trùng lên đúng viên pin đang quan sát.

### 2.6 Mở bảng điều khiển

```bash
make anomaly-panel          # → http://localhost:8099
```

Kiểm tra trạng thái sạch:

```bash
curl -s http://localhost:8099/api/state | python3 -m json.tool
# alerts: []  breaches: []  incidents: []  tickets: []  sagas: []
```

### 2.7 Dữ liệu nền tối thiểu để case chạy được

Sau khi dọn theo §2.2–2.3, `battery_db` phải còn **đúng** những dòng sau. Thiếu bất kỳ dòng nào là
một nhóm case im lặng không chạy:

| Bảng | Số dòng | Thiếu thì mất case nào |
|---|---|---|
| `sites` | 1 | Toàn bộ case môi trường (17–19, 21–25) |
| `battery_types` | 1 | — |
| `threshold_configs` | 1 | **Mọi case số đo pin** — không có ngưỡng thì không luật nào chạy |
| `ambient_threshold_configs` | 1 | Case ambient (17, 18, 19, 24, 25) |
| `battery_assets` | 1 | Toàn bộ case pin |
| `iot_devices` | 1 | Toàn bộ — không thiết bị thì không gửi được số đo |
| `customer_accounts` | ≥1 | Ticket auto không có chủ sở hữu |

Kiểm nhanh:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
select 'sites' t, count(*) n from sites
union all select 'battery_types', count(*) from battery_types
union all select 'threshold_configs', count(*) from threshold_configs
union all select 'ambient_threshold_configs', count(*) from ambient_threshold_configs
union all select 'battery_assets', count(*) from battery_assets
union all select 'iot_devices', count(*) from iot_devices;"
```

**Cột ngưỡng NULL = luật không bao giờ chạy.** Bốn cột dưới đây quyết định bốn case Tier 2 có sinh
cảnh báo hay không — trên máy đang chạy chúng đã có giá trị:

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
select current_max_charge, current_max_discharge,
       internal_resistance_max_milliohm, cell_voltage_delta_max_mv,
       noise_suppression_enabled, noise_suppression_count
from threshold_configs;"
```

| Cột | Giá trị | Case phụ thuộc |
|---|---|---|
| `current_max_discharge` | 30,00 | 09 RapidDischarge |
| `current_max_charge` | 15,00 | 10 AbnormalCharging |
| `internal_resistance_max_milliohm` | 50,000 | 13 HighInternalResistance |
| `cell_voltage_delta_max_mv` | 100,000 | 14 CellImbalance |
| `noise_suppression_count` | 5 | Mọi case Warning — cần đủ 5 lần vi phạm |

Cột nào `NULL` thì case tương ứng gửi số đo thành công nhưng **không sinh cảnh báo**, và không có
thông báo lỗi nào.

### 2.8 Case không chạy được với dữ liệu tối thiểu

`make anomaly-check` sẽ báo `✗` cho những case này — **bình thường**, không phải hỏng:

| Case | Lý do |
|---|---|
| 15 DeviceOffline | Phải chạy tay: dừng simulator > 10 phút |
| 26 IotDataIntegrityViolation | Cần `ESP32-SIM-002` — thiết bị đã bị xoá ở §2.3 |
| 16 SensorMismatch | Cần hai nguồn đo cùng gửi trong một cửa sổ 60 giây |

Bốn case demo ở §4 **không** nằm trong nhóm này.

### 2.9 Nếu DB trắng hoàn toàn — tạo lại dữ liệu nền

`BAT-2026-REAL-001` **không nằm trong seeder của backend** — nó được tạo thủ công cho demo. Nếu
`battery_assets` trống (hoặc chỉ còn các viên `BAT-2026-001…004` do seeder sinh), chạy khối SQL
dưới đây. GUID cố ý đặt cứng để khớp với `config/seed.yaml` — đổi GUID thì phải sửa cả hai nơi.

```bash
docker exec solar-postgres psql -U postgres -d battery_db -c "
BEGIN;

-- 1. Loại pin + ngưỡng. Hai con số 55 / 80 phải khớp hardcode ai-module (xem §1).
INSERT INTO battery_types (id, name, chemistry, nominal_voltage, nominal_capacity_ah,
                           created_at, is_deleted)
VALUES ('bbbb0001-0000-4000-8000-000000000024', 'LiFePO4 24V 30Ah', 1, 24.00, 30.00, now(), false)
ON CONFLICT (id) DO NOTHING;

INSERT INTO threshold_configs (id, battery_type_id, temperature_max, temperature_min,
                               voltage_max, voltage_min,
                               soc_warning_threshold, soc_critical_threshold,
                               soh_warning_threshold, soh_critical_threshold,
                               current_max_charge, current_max_discharge,
                               internal_resistance_max_milliohm, cell_voltage_delta_max_mv,
                               noise_suppression_enabled, noise_suppression_count,
                               is_active, created_at, is_deleted)
VALUES (gen_random_uuid(), 'bbbb0001-0000-4000-8000-000000000024',
        55.00, -10.00, 29.20, 20.00, 20.00, 10.00, 85.00, 80.00,
        15.00, 30.00, 50.000, 100.000, true, 5, true, now(), false)
ON CONFLICT DO NOTHING;

-- 2. Viên pin. customer_id phải là ID THẬT trong auth_db (xem lệnh bên dưới).
INSERT INTO battery_assets (id, serial_number, battery_type_id, customer_id, site_id,
                            install_date, warranty_status, status,
                            cascade_risk_score, electrical_topology, created_at, is_deleted)
VALUES ('aaaa0001-0000-4000-8000-000000000024', 'BAT-2026-REAL-001',
        'bbbb0001-0000-4000-8000-000000000024',
        '370a64c1-2ee7-445e-94a2-b1d1281c4ea9',
        '886564d0-8e8b-4b51-81be-e0c3f895c38f',
        '2026-01-03', 1, 1, 0.0, 1, now(), false)
ON CONFLICT (id) DO NOTHING;

COMMIT;"
```

`customer_id` và `site_id` phải trỏ tới bản ghi có thật. Lấy giá trị đúng trên máy bạn:

```bash
# ID tài khoản Customer — nguồn sự thật là auth_db
docker exec solar-postgres psql -U postgres -d auth_db -tAc \
  "select id from accounts where email='customer.demo@solarbattery.local';"

# Site
docker exec solar-postgres psql -U postgres -d battery_db -tAc \
  "select id from sites limit 1;"
```

> ⚠️ **`customer_accounts` trong `ticket_db` phải cùng ID với `auth_db`.** Read-model của
> TicketService từng lệch ID, và hậu quả là Customer tạo ticket trên **chính viên pin của mình**
> vẫn bị **403 “you do not have access to it”**. Đối chiếu và sửa:
>
> ```bash
> docker exec solar-postgres psql -U postgres -d ticket_db -c "
> update customer_accounts
> set id = (select id from dblink('dbname=auth_db','select id from accounts where email=''customer.demo@solarbattery.local''') as t(id uuid))
> where email = 'customer.demo@solarbattery.local';"
> ```
>
> Không có `dblink` thì lấy ID bằng lệnh trên rồi `update` thủ công.

Thiết bị IoT thì **không tạo bằng SQL** — khoá API phải sinh qua endpoint admin để backend băm và
lưu đúng định dạng:

```bash
make provision      # cần ADMIN_TOKEN trong .env
```

Lệnh trả `rawApiKey` **một lần duy nhất**. Chép ngay vào `.env` (`IOT_API_KEY`) và
`config/seed.yaml` (`devices[].api_key`).

### 2.10 Tài khoản

| Vai trò | Email | Mật khẩu |
|---|---|---|
| Manager | `manager.demo@solarbattery.local` | `Password123@` |
| Staff | `staff.tier2@solarbattery.local` | `Password123@` |
| Customer | `customer.demo@solarbattery.local` | `Password123@` |

---

## 3. Cách simulator sinh dữ liệu

### 3.1 Chế độ thời gian thực

Panel mặc định bật ô **“chạy thời gian thực”**. Mỗi case gửi 12 số đo nền cách nhau **5 giây thật**,
rồi mới bắn số đo lỗi — tổng khoảng **40 giây**.

Số đo nền do `MockBattery` sinh, cùng mô hình simulator chạy hằng ngày: nội suy OCV theo SOC,
sụt áp qua điện trở nội, sinh nhiệt I²R, dao động sin và nhiễu ngẫu nhiên. Warm-up chạy **đúng
scenario của case**, nên trị số bò dần về phía ngưỡng thay vì phẳng lì rồi nhảy vọt.

Ví dụ thật của case 02 (Overheat):

```
03:34:05  31,8 °C      03:34:30  47,1 °C
03:34:10  34,8 °C      03:34:35  50,7 °C
03:34:15  37,5 °C      03:34:40  54,2 °C   ← dừng sát ngưỡng 55
03:34:20  41,2 °C      03:34:13  67,0 °C   ← số đo lỗi của case
03:34:25  44,5 °C
```

Warm-up **tự dừng ngay trước ngưỡng**. Nếu để nó vượt qua, chính số đo nền sẽ sinh alert Warning
và chiếm chỗ khử trùng, khiến alert Critical của case bị gộp — chạy xong không có ticket nào, nhìn
hệt như luồng saga hỏng.

Tắt ô đó thì gửi gộp trong ~2 giây: nhanh hơn, nhưng người xem không thấy số nhích dần.

### 3.2 Ba mốc thời gian chi phối nhịp demo

| Tham số | Giá trị | Ảnh hưởng |
|---|---|---|
| Chu kỳ quét | 10 s | Alert hiện sau ~10–20 s |
| Khử trùng | **30 phút** | Cùng `(pin, loại lỗi)` trong 30' → **bị gộp** |
| Cửa sổ bằng chứng | ±2 phút | Log hiển thị trong ticket |

Khử trùng là ràng buộc lớn nhất. Từ 2026-08-17, alert **nghiêm trọng hơn** alert đang mở sẽ tạo
mới thay vì bị gộp — nên Warning rồi Critical vẫn ra hai luồng riêng. Nhưng hai case **cùng mức**
trên cùng viên pin thì vẫn gộp.

### 3.3 Lệnh dòng lệnh

```bash
make anomaly-list     # 26 case + điều kiện từng case
make anomaly-check    # kiểm tra backend đủ điều kiện chưa (KHÔNG gửi gì)
make anomaly-dry      # in payload thật sẽ gửi, KHÔNG gửi
make anomaly          # chạy cả lượt
make anomaly-verify   # câu SQL kiểm chứng + dọn để demo lại
```

---

## 4. Bốn kịch bản demo

Tổng thời lượng khoảng **17 phút**, chưa tính thời gian chờ giữa các màn.

Bốn case **khác loại lỗi nhau** nên không đụng khử trùng — chạy thẳng một mạch, không cần dọn DB
giữa chừng.

---

### Case 1 — Lỗi do pin · tự sinh qua saga (~4')

> *“Viên pin quá nhiệt. Không ai báo cáo — hệ thống tự phát hiện.”*

1. Panel → **Run case 02**
2. Chờ ~40 giây, mở tab chi tiết pin — nhiệt độ nhích dần từng nấc 5 giây
3. Alert `Overheating` · **Critical** · ngưỡng 55 / thực đo 67
4. Ticket `[Auto] BAT-2026-REAL-001 - Battery Overheating` · Priority High
5. Mở ticket → tab **Evidence**:

```
31,8 → 34,8 → 37,5 → 41,2 → 44,5 → 47,1 → 50,7 → 54,2 → 67,0 °C
```

> *“Số đo nền do mô hình pin thật sinh ra. Chỉ vào việc dòng tăng song song với nhiệt: đó là hệ
> quả vật lý của sinh nhiệt I²R, không phải số ngẫu nhiên. Ba dòng đầu vẫn trong ngưỡng, hệ thống
> vẫn giữ — số đo bình thường cũng là bằng chứng.”*

6. Tab **AI Verify** → điểm và lý do

**Đường đi:** Alert → outbox → saga MassTransit → `CreateTicketFromAlertConsumer`

---

### Case 2 — Lỗi do môi trường · tự sinh qua consumer (~3')

⏳ Chờ ít nhất 2 phút sau Case 1.

> *“Không phải lỗi nào cũng nằm ở viên pin.”*

1. Panel → **Run case 21** (rò khí, cảm biến MQ-2)
2. Ticket `Environmental incident at Solar Farm Long An` · **P1 Critical** · `ImpactScope = Site` ·
   SLA chạy ngay
3. Tab Info hiển thị **Sensor evidence**:

```
MQ-2                    3100 > 2000
Measured 3100 against a limit of 2000 — over threshold.
```

Nói chủ động trước khi bị hỏi:

> *“Ticket này không có bảng log pin — đúng thiết kế. Sự cố ở tủ điện, không gắn viên pin nào, nên
> bằng chứng là cảm biến MQ-2. Và nó đi đường hoàn toàn khác Case 1: không qua saga, mà qua một
> consumer riêng.”*

Comment trong mã nguồn nêu thẳng lý do phải có đường đó:

> *“Trước fix: pin hơi nóng quá ngưỡng → auto-ticket đầy đủ; nhà kho đang cháy → chỉ notify.”*

⚠️ Chọn **đúng case 21** (GasLeak). Nếu dữ liệu seed còn sự cố loại 1 (Smoke) hoặc 5
(OverheatHazard) đang mở, hai loại đó bị khử trùng và trả **200** kèm *“Existing active incident
reused”* — không phải 201.

---

### Case 3 — AI có thể sai, người quyết định (~4')

⏳ Chờ 2 phút.

1. Đăng nhập **Customer** → tạo ticket tay
2. **Chọn `IncidentDetectedAt` vào một thời điểm không có số đo bất thường** — đây là điểm mấu chốt
3. AI verify chấm thấp → `Suspicious`
4. Mở Evidence: bảng **có đủ số đo, đều trong ngưỡng**

> *“Đây là lý do bảng giữ lại cả dòng không vi phạm. Nếu chỉ hiện dòng vượt ngưỡng, Manager thấy
> bảng trống và không phân biệt được ‘không có dữ liệu’ với ‘có dữ liệu và nó bình thường’. Vế sau
> mới là căn cứ để bác ticket.”*

5. Manager **override** phán quyết AI → gán Staff

> *“AI chỉ dán nhãn và trưng bằng chứng. Phán quyết AI không bao giờ tự chuyển trạng thái ticket.”*

**Lưu ý khi diễn:** form tạo ticket mặc định điền thời điểm hiện tại. Muốn diễn đúng ý đồ phải
**chủ động chọn mốc**: chọn mốc có số đo bất thường thì AI xác nhận khách hàng nói đúng (điểm cao);
chọn mốc bình thường thì AI nghi ngờ (điểm thấp). Cả hai đều là kịch bản hay, nhưng phải chọn có
chủ đích.

---

### Case 4 — AI phát hiện trùng, Manager gộp (~5')

⚠️ Dùng lại ticket auto của Case 1 — **không xoá DB** giữa hai màn.

Kịch bản đời thực: máy phát hiện lúc 06:00 và mở ticket; khách hàng không để ý, 07:00 mới tự khai
và chọn đúng mốc sự cố 06:00.

1. Customer tạo ticket trên **đúng `BAT-2026-REAL-001`**, category **Overheat**, chọn
   `IncidentDetectedAt` **trùng mốc của ticket auto**:

   > *“The battery at the station is unusually hot, it feels very hot to the touch, I am worried
   > about fire”*

2. AI verify gắn badge **“Nghi trùng với TKT-xxxx”**, lý do `same battery, same category`
3. Manager đối chiếu → bấm **Merge**, chọn ticket auto làm master

| Ticket | Sau khi gộp |
|---|---|
| Thủ công (nguồn) | `ClosedRejected` · `CloseReason = MergedDuplicate` · SLA **dừng** |
| Auto (đích) | giữ nguyên · nhận đính kèm từ nguồn |

Cả hai ticket đều ghi vết kiểm toán hai chiều:

```
TKT-0001  StatusChanged  Demo Manager   "Source ticket TKT-0002 was merged into this ticket"
TKT-0002  StatusChanged  Demo Manager   "Merged into master ticket TKT-0001"
```

> *“AI dán nhãn, Manager xác nhận rồi mới gộp — AI không bao giờ tự gộp. Ticket không biến mất: nó
> đóng lại có lý do rõ ràng và link về ticket gốc. SLA của ticket trùng dừng lại, không đếm oan.”*

**Ba điều kiện gộp** — sai một cái là lỗi 409:

- Ticket nguồn phải `Open`
- Cùng `CustomerId`
- **Chung ít nhất một viên pin** ← chính vì thế ticket môi trường ở Case 2 không gộp được với
  ticket pin

**Chiều gộp:** thủ công là nguồn (bị đóng), auto là đích (giữ lại). Ngược chiều sẽ đóng mất ticket
đang giữ SLA.

---

## 5. Cách AI dò trùng

Hai thuật toán, chọn theo nguồn của cặp ticket — vì so văn bản hỏng theo hai kiểu ngược nhau:

| Cặp | Cách chấm | Lý do |
|---|---|---|
| Máy ↔ máy | Jaccard + **bắt buộc** cùng category | Mô tả sinh từ cùng template, khác mỗi con số. Đo được: Overheat 67 °C và Undertemp −18 °C ra 0,73 — hai lỗi ngược hẳn nhau |
| Có người viết | **Cấu trúc**: cùng category +0,45 · gần thời gian +0,30 · Jaccard ×0,25 | Cùng sự cố nhưng khác cách diễn đạt. Đo được: khách hàng vs máy ra 0,06, dưới ngưỡng 0,45 rất xa |

Cùng category một mình đã đủ vượt ngưỡng nghi trùng — **có chủ đích**. Ứng viên vốn đã lọc còn
ticket **đang mở** trên **cùng viên pin**, nên cùng loại lỗi nữa thì gần như chắc chắn cùng một sự
cố. Khách hàng phát hiện muộn rồi mới báo là chuyện thường, nên **không** đặt điều kiện thời gian
bắt buộc: nghi sai thì Manager bỏ qua trong một giây, còn bỏ sót thì hai ticket song song cho một
sự cố và không ai biết chúng liên quan.

---

## 6. Câu hỏi nên chuẩn bị

**“Warning có sinh ticket không?”**
Không, bất kể bao nhiêu lần. Chỉ **Critical** mới sinh ticket. Warning để theo dõi xu hướng.

**“Nếu pin đang Warning rồi vọt lên Critical thì sao?”**
Ra **hai luồng alert riêng**, và Critical sinh ticket. Trước 2026-08-17 thì Critical bị gộp vào
Warning cũ trong cửa sổ 30 phút, không phát sự kiện, không có ticket — kịch bản đời thực rất nguy
hiểm: pin nóng nhẹ 10:00 → Warning; 10:05 vọt 67 °C → **không ai được gọi**. Điều kiện khử trùng
nay có thêm `Severity >= newSeverity`.

**“AI ở đây là mô hình gì?”**
Học máy nằm ở hai luồng có dữ liệu chuỗi thời gian: **Mamba (SSM) + FiLM** dự đoán SOH và
**Isolation Forest** phát hiện bất thường, mỗi loại có bộ trọng số riêng cho NASA/NMC và LFP
(Severson 141 cell).

Verify ticket, dò trùng, gợi ý Staff và gợi ý KB dùng **luật tất định** — tái lập được, giải thích
được từng điểm, test được. Nhóm đã đo thử thay bằng mô hình:

- Embedding (`all-MiniLM-L6-v2`) cho dò trùng: cosine **0,97** giữa hai lỗi ngược nhau, tệ hơn
  Jaccard 0,73.
- Isolation Forest cho verify: model cần vector 57 chiều trích từ chuỗi 30 timestep, mà ticket chỉ
  có một snapshot tại thời điểm khai báo.

Dùng mô hình sai giả định đầu vào là dự đoán sai trong im lặng.

**“Sao `ticket_ai_suggestions` trống?”**
Mô hình SOH cần tối thiểu 30 số đo (`Ai__MinReadings`). Case demo chỉ có ~10 — ngưỡng an toàn cố ý.

**“Log này có phải chế không?”**
Không. `MockBattery` là mô hình vật lý. Chỉ vào việc dòng tăng song song với nhiệt.

---

## 7. Cạm bẫy đã gặp thật

| Hiện tượng | Nguyên nhân | Xử lý |
|---|---|---|
| Bấm Run mà không thấy gì | Chế độ thời gian thực mất 40 giây, không có phản hồi trực quan | Đợi đủ rồi mới làm mới trang |
| Alert có nhưng **không có ticket** | Alert cùng loại đang mở trong cửa sổ 30 phút → `status = Merged` | Bấm **“Dọn alert + ticket”** trên panel |
| Alert *Connection lost* tự hiện | Simulator ngừng gửi > 5 phút | Nhiễu môi trường test, tự đóng khi chạy case tiếp |
| Mobile lỗi **404** ở màn hình pin | `/bms-switch` đòi gateway `Active`; thiết bị thành `Offline` sau 5 phút im lặng | Chạy một case bất kỳ, thiết bị tự về Active |
| Mọi API ticket trả **502** | `TicketDataSeeder` crash-loop sau khi reset DB | Rebuild `ticketservice` |
| Customer tạo ticket bị **403** | `customer_accounts` trong `ticket_db` lệch ID với `auth_db` | Cập nhật read-model theo `auth_db` |
| Panel hiện 5 viên pin | Seeder chạy lại khi restart container | Xoá lại theo §2.3 |

---

## 8. Kiểm chứng nhanh bằng SQL

```bash
# Alert — severity 2 = Warning, 3 = Critical; status 3 = Merged (bị gộp, không phát sự kiện)
docker exec solar-postgres psql -U postgres -d battery_db -c "
select anomaly_type, severity, threshold_value, actual_value, status,
       merged_into_alert_id is not null as bi_gop
from alerts order by detected_at;"

# Ticket + kết quả AI
docker exec solar-postgres psql -U postgres -d ticket_db -c "
select t.code, t.origin, t.status, t.ai_verify_score,
       coalesce((select code from tickets d where d.id = t.suspected_duplicate_of_ticket_id),'-') as nghi_trung,
       coalesce(t.duplicate_reason,'-') as ly_do
from tickets t order by t.code;"

# Sự kiện đã phát — thiếu BatteryAnomalyDetectedEvent nghĩa là alert bị gộp
docker exec solar-postgres psql -U postgres -d battery_db -c "
select type, count(*) from outbox_messages group by type;"
```

Đối chiếu nhanh trong log backend:

```
ThresholdCheck: scanned=5, created=1, merged=0, outbox=1   ← đúng
ThresholdCheck: scanned=5, created=0, merged=1, outbox=0   ← alert bị gộp, sẽ KHÔNG có ticket
```
