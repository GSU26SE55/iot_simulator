"""Bộ chạy dataset anomaly — đẩy số đo có chủ đích lên backend để demo từng loại cảnh báo.

    python -m src.anomaly list                 # liệt kê case + điều kiện
    python -m src.anomaly check                # kiểm tra backend đã sẵn sàng (KHÔNG gửi gì)
    python -m src.anomaly run                  # chạy toàn bộ case gửi được
    python -m src.anomaly run --case 1,2,5     # chạy vài case
    python -m src.anomaly run --dry-run        # in payload thật, không gửi
    python -m src.anomaly verify               # in câu SQL kiểm chứng kết quả

Toàn bộ đường gửi dùng LẠI mã sản xuất của simulator (`payload.py` + `http_client.py`), nên
payload đi ra dây giống hệt lúc chạy bình thường — không có nhánh riêng cho demo.

Bốn ràng buộc của backend quyết định thiết kế file này (đã đối chiếu trực tiếp trên mã nguồn
BatteryService, không chép từ tài liệu):

  1. **Bộ quét chỉ nhìn lại 20 giây** (`ScanIntervalSeconds=10`, lookback = 2× tick).
     Số đo mang mốc thời gian cũ hơn sẽ KHÔNG BAO GIỜ được quét ⇒ mọi mốc đều phải là "vừa xong".
  2. **`deviceTimestamp` lệch quá 5 phút ⇒ 400 cho CẢ batch** (#IoT2-15).
  3. **Chống nhiễu**: cảnh báo chỉ nổ khi đủ `NoiseSuppressionCount` (=5) lần vi phạm, và mỗi lần
     được khử trùng theo `reading.Time` ⇒ các lần lặp BẮT BUỘC khác mốc thời gian.
     Ngoại lệ nổ ngay: Overheat **Critical**, và EnvironmentalIncident.
  4. **Chỉ số đo `primary` được xét ngưỡng** (NS-08). Nguồn phụ chỉ dùng để ghép cặp chéo nguồn.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .bms import MockBattery
from .config import CONTRACT_IOT2, SimulatorConfig, load_config
from .http_client import IotHttpClient
from .payload import (SOURCE_CODE_PRIMARY, ChargingState, SensorReading, SourceType,
                      build_production_batch_payload)
from .provision import parse_battery_mappings, parse_provision_response
from .sensors.environmental import (EnvironmentalIncident, IncidentSeverity, IncidentType)
from .timeutil import ISO_FORMAT

log = logging.getLogger("iot-sim.anomaly")

DEFAULT_DATASET = "config/anomaly-dataset.yaml"

# Hai loại case tạo cảnh báo NGAY trong handler của endpoint, không qua bộ quét ngưỡng định kỳ
# ⇒ không cần chia đợt chống nhiễu, và không gắn với một viên pin nào.
SINGLE_SHOT_KINDS = {"ambient", "environmental_incident"}

# `AnomalyTypeEnum` của backend — dùng cho câu SQL kiểm chứng.
ANOMALY_TYPE_IDS = {
    "Overheat": 1, "Overvoltage": 2, "Undervoltage": 3, "LowSoc": 4, "RapidDischarge": 5,
    "AbnormalCharging": 6, "DeviceOffline": 7, "SohDegradation": 8, "HighAmbientTemp": 9,
    "HighHumidity": 10, "HighTempHumidityCombo": 11, "HighInternalResistance": 12,
    "CellImbalance": 13, "EnvironmentalIncident": 14, "SensorMismatch": 15, "Undertemp": 16,
    "IotDataIntegrityViolation": 17,
}


# ─────────────────────────────────────── kết quả ────────────────────────────────────────────
@dataclass
class CaseResult:
    case_id: int
    anomaly: str
    severity: str
    status: str            # OK · FAIL · BLOCKED · MANUAL · DRY
    sent: int = 0
    inserted: int = 0
    skipped: int = 0
    http_codes: list[int] = field(default_factory=list)
    detail: str = ""


# ─────────────────────────────────────── tiện ích ───────────────────────────────────────────
def _iso(dt: datetime) -> str:
    return dt.strftime(ISO_FORMAT)


def _num(value, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def build_reading(spec: dict, serial: str) -> SensorReading:
    """Dựng `SensorReading` từ một mục `reading:` trong dataset.

    Trường vắng mặt dùng mặc định an toàn (nằm trong ngưỡng), để mỗi case chỉ cần khai đúng
    những gì nó muốn đẩy vượt ngưỡng — khai thừa dễ kích nhầm luật khác và làm lẫn kết quả.
    """
    soh = spec.get("soh_percent")
    bms_error = spec.get("bms_error_code") or ""
    charging = spec.get("charging_state")
    ir = spec.get("internal_resistance_milliohm")
    cvd = spec.get("cell_voltage_delta_mv")

    return SensorReading(
        battery_asset_id=spec.get("battery_asset_id", "") or "",
        serial=serial,
        voltage=_num(spec.get("voltage")),
        current=_num(spec.get("current")),
        temperature=_num(spec.get("temperature")),
        soc_percent=_num(spec.get("soc_percent")),
        cycle_count=int(spec.get("cycle_count") or 0),
        sensor_source_code=str(spec.get("sensor_source_code") or SOURCE_CODE_PRIMARY),
        source_type=SourceType(int(spec.get("source_type") or 1)),
        charging_state=ChargingState(int(charging)) if charging is not None else ChargingState.IDLE,
        soh_percent=_num(soh),
        bms_error_code=bms_error,
        has_soh=soh is not None,
        has_charging_state=charging is not None,
        has_bms_error=bool(bms_error),
        internal_resistance_milliohm=None if ir is None else float(ir),
        cell_voltage_delta_mv=None if cvd is None else float(cvd),
    )


# ─────────────────────────────────────── bộ chạy ────────────────────────────────────────────
class AnomalyRunner:
    def __init__(self, sim_cfg: SimulatorConfig, dataset: dict, dry_run: bool = False):
        self.sim_cfg = sim_cfg
        self.dataset = dataset
        self.dry_run = dry_run
        self.defaults = dataset.get("defaults", {}) or {}
        self._clients: dict[str, IotHttpClient] = {}
        self._provision: dict[str, dict] = {}   # device_code → {site_id, serials}
        # Mốc gốc của cả lần chạy + số ô thời gian đã cấp cho từng pin (xem `_base_time`).
        self._run_start = datetime.now(timezone.utc)
        self._slots: dict[str, int] = {}

    # ── cấp mốc thời gian không đụng nhau ──────────────────────────────────────────────────
    def _base_time(self, key: str) -> datetime:
        """Cấp cho mỗi case một GIÂY riêng, tính theo từng pin.

        Khoá chính của bảng số đo là `(Time, BatteryAssetId)`. Hai case dùng chung một pin mà
        chạy trong cùng một giây sẽ đụng khoá — backend đếm chúng vào `skipped` với thông báo
        "N readings already existed", và case sau mất dữ liệu trong im lặng. Đây là lỗi ĐÃ QUAN
        SÁT ĐƯỢC khi chạy thật, không phải phòng xa.

        Lùi về quá khứ chứ không tiến tới tương lai: backend từ chối số đo có `Time` quá
        `now + 5 phút`, còn quá khứ thì hợp lệ miễn còn trong cửa sổ quét 20 giây.
        """
        used = self._slots.get(key, 0)
        self._slots[key] = used + 1
        if used >= 15:
            log.warning("pin %s đã dùng %d ô thời gian — các case sau lùi quá cửa sổ quét 20s và "
                        "có thể KHÔNG được quét. Chạy tách nhóm case cho pin này.", key, used + 1)
        return self._run_start - timedelta(seconds=used)

    # ── hạ tầng ────────────────────────────────────────────────────────────────────────────
    def _device_cfg(self, device_code: str):
        for d in self.sim_cfg.devices:
            if d.device_code == device_code:
                return d
        raise KeyError(f"Không tìm thấy thiết bị '{device_code}' trong seed.yaml")

    def client(self, device_code: str) -> IotHttpClient:
        if device_code not in self._clients:
            dev = self._device_cfg(device_code)
            self._clients[device_code] = IotHttpClient(
                base_url=self.sim_cfg.backend.base_url,
                device_code=dev.device_code,
                api_key=dev.api_key,
                tls_verify=self.sim_cfg.backend.tls_verify,
                firmware_version=dev.firmware_version,
                contract_version=self.sim_cfg.backend.contract_version,
                timeout_s=self.sim_cfg.backend.http_timeout_s,
            )
        return self._clients[device_code]

    def provision(self, device_code: str) -> dict:
        """Provision thật để lấy `siteId` + danh sách pin thiết bị được phép gửi.

        Bắt buộc: `POST /api/ambient/readings/batch` đòi `siteId`, và #IoT2-18 chặn thiết bị gửi
        cho pin khác site (403 cho CẢ batch). Đoán mò hai thứ này là hỏng demo.
        """
        if device_code in self._provision:
            return self._provision[device_code]

        dev = self._device_cfg(device_code)
        res = self.client(device_code).provision(
            hardware_revision=dev.hardware_revision,
            device_timestamp_iso=_iso(datetime.now(timezone.utc)))
        if not res.ok:
            raise RuntimeError(f"provision '{device_code}' thất bại: HTTP {res.status_code} "
                               f"— {res.body[:200]}")
        parsed = parse_provision_response(res.json)
        if not parsed.ok:
            raise RuntimeError(f"provision '{device_code}' bị từ chối: {parsed.error}")
        entries, _skipped, _present = parse_battery_mappings(parsed.data)
        info = {
            "site_id": parsed.site_id,
            "serials": [e.serial for e in entries],
            "polling_s": parsed.polling_interval_s,
        }
        self._provision[device_code] = info
        return info

    # ── các kiểu case ──────────────────────────────────────────────────────────────────────
    def run_case(self, case: dict) -> CaseResult:
        result = CaseResult(case_id=int(case.get("id", 0)), anomaly=str(case.get("anomaly", "?")),
                            severity=str(case.get("severity") or "-"), status="OK")
        return self.run_case_into(case, result)

    def run_case_into(self, case: dict, result: CaseResult) -> CaseResult:
        """Chạy trọn một case vào `result` cho sẵn. Một case hỏng KHÔNG được dừng cả bộ."""
        kind = str(case.get("kind") or "sensor_reading")
        try:
            if kind == "manual":
                result.status = "MANUAL"
                result.detail = "chạy tay — xem `instructions`"
                return result
            if kind == "ambient":
                return self._run_ambient(case, result)
            if kind == "environmental_incident":
                return self._run_incident(case, result)
            return self._run_readings(case, result, cross_source=(kind == "cross_source"))
        except Exception as ex:                       # noqa: BLE001
            result.status = "FAIL"
            result.detail = str(ex)
            return result

    def _repeat(self, case: dict) -> int:
        return max(1, int(case.get("repeat", self.defaults.get("repeat", 7))))

    def waves_for(self, case: dict) -> list[int]:
        """Chia số lần lặp thành các ĐỢT — chìa khoá để cảnh báo nổ CHẮC CHẮN.

        Cơ chế thật của backend (`AnomalyDetectionService`):
          · Mỗi lượt quét đọc số breach ĐÃ GHI VÀO DB **trước** lượt đó, rồi cộng 1 cho số đo
            đang xét. Breach của chính lượt này còn đang chờ ghi ⇒ KHÔNG được đếm.
          · Hệ quả: gửi 6 số đo trong MỘT lượt thì cả 6 đều ra `effectiveCount = 0 + 1 = 1`,
            nhỏ hơn ngưỡng 5 ⇒ **bị bỏ qua hết**, chỉ ghi lại breach. Cảnh báo chỉ nổ nếu đúng
            những số đo đó còn nằm trong cửa sổ 20 giây để được quét THÊM một lượt nữa — điều
            không có gì bảo đảm. Đây là lý do lần chạy thật đầu tiên chỉ 4/14 loại nổ cảnh báo.

        Cách làm chắc chắn: đợt 1 gửi ĐÚNG `noise_count` số đo, chờ hơn một nhịp quét cho chúng
        được ghi, rồi đợt 2 gửi thêm vài số đo — lúc này `effectiveCount = 5 + 1 = 6 ≥ 5` ngay
        trong lượt quét ĐẦU TIÊN của đợt 2.

        Case được backend miễn luật chống nhiễu (Overheat Critical, ambient) chỉ cần một đợt.
        """
        repeat = self._repeat(case)
        if repeat <= 1 or case.get("bypass_noise"):
            return [repeat]
        noise_count = int((self.dataset.get("meta", {}) or {}).get("noise_suppression_count", 5))
        first = min(noise_count, repeat - 1)
        return [first, repeat - first]

    def _send(self, send_fn, result: CaseResult, what: str):
        """Gửi một request, TÔN TRỌNG `retryAfterSeconds` khi bị giới hạn tần suất.

        API Gateway giới hạn 60 request / 30 giây cho lời gọi không JWT
        (`RateLimiting__AnonymousPermitLimit=60`, `WindowSeconds=30`) — thiết bị IoT xác thực
        bằng `X-Api-Key` nên rơi vào hạn mức này. Coi 429 là thất bại sẽ làm hỏng demo giữa
        chừng, đúng như lần chạy thật đầu tiên: 8/19 case chết vì 429.
        """
        for attempt in range(3):
            res = send_fn()
            result.http_codes.append(res.status_code)
            if res.status_code != 429:
                return res
            wait = 30.0
            data = (res.json or {}).get("data") if isinstance(res.json, dict) else None
            if isinstance(data, dict) and data.get("retryAfterSeconds"):
                wait = min(float(data["retryAfterSeconds"]) + 1.0, 60.0)
            if attempt == 2:
                return res
            print(f"    ⏳ backend giới hạn tần suất ({what}) — chờ {wait:.0f}s rồi thử lại "
                  f"(lần {attempt + 2}/3)")
            _time.sleep(wait)
        return res

    def precheck(self, case: dict, result: CaseResult) -> bool:
        """Kiểm quyền gửi TRƯỚC khi đụng vào mạng. Trả False nếu case bị chặn."""
        device_code = str(case.get("device_code") or self.defaults.get("device_code"))
        serial = str(case.get("battery") or "")
        if not serial:
            result.status = "FAIL"
            result.detail = "case thiếu `battery`"
            return False
        info = self.provision(device_code)
        if info["serials"] and serial not in info["serials"]:
            result.status = "BLOCKED"
            result.detail = (f"backend KHÔNG giao pin {serial} cho {device_code} "
                             f"(#IoT2-18: khác site ⇒ 403 cả batch). Pin được phép: "
                             f"{', '.join(info['serials'])}")
            return False
        return True

    # Anomaly của case → scenario tương ứng trong `MockBattery`. Warm-up chạy CHÍNH scenario
    # đó chứ không phải `normal`: mỗi scenario có một biến drift tăng dần theo `dt_s`, nên số
    # đo nền bò dần về phía ngưỡng đúng như pin đang hỏng thật. Chạy `normal` rồi nhảy thẳng
    # sang giá trị lỗi cho ra đường 30°C → 72°C trong 5 giây — phi vật lý, nhìn là biết dựng.
    #
    # Anomaly không có scenario tương ứng (Undertemp, CellImbalance, HighInternalResistance…)
    # rơi về `normal`: thà nền phẳng còn hơn bò theo một hướng sai.
    _WARMUP_SCENARIOS = {
        "Overheat": "overheat",
        "Overvoltage": "overvoltage",
        "Undervoltage": "undervoltage",
        "LowSoc": "low_soc",
        "RapidDischarge": "rapid_discharge",
        "AbnormalCharging": "abnormal_charging",
        "SohDegradation": "soh_degradation",
    }

    def send_warmup(self, case: dict, result: CaseResult, count: int = 12,
                    interval_s: float = 20.0) -> bool:
        """Gửi `count` số đo BÌNH THƯỜNG ngay TRƯỚC mốc của case, làm nền cho bằng chứng.

        Số đo của case là hằng số cứng trong dataset — cố ý, vì mỗi case chỉ được đẩy DUY NHẤT
        một trường ra ngoài ngưỡng, khai thừa là kích nhầm luật khác. Nhưng hệ quả là bảng
        "bằng chứng cảnh báo" chỉ có một dòng với con số tròn trịa, đọc lên không khác gì dữ
        liệu bịa: không thấy pin trước đó ra sao, không thấy nó vọt lên hay nhảy đột ngột.

        Ở đây dùng `MockBattery` — chính mô hình mà simulator chạy hằng ngày: OCV nội suy theo
        SOC, sụt áp qua điện trở nội, sinh nhiệt I²R, dao động sin và nhiễu ngẫu nhiên. Số đo
        nền vì thế lệch nhau từng dòng đúng như pin thật.

        Quan trọng hơn: warm-up chạy CHÍNH scenario của case (xem `_WARMUP_SCENARIOS`), nên
        các trị số bò dần về phía ngưỡng thay vì phẳng lì rồi nhảy vọt. Với case Overheat,
        `_scenario_temp_offset` cộng thêm `dt_s * 0.15` mỗi bước, cho ra đường 30 → 34 → 38 →
        46 → 53 → 61°C trước khi case đẩy 72°C — đọc lên là một viên pin đang nóng lên, không
        phải một con số rơi từ trên trời.

        Mốc thời gian LÙI VỀ TRƯỚC mốc của case (`_base_time` cấp cho case một giây riêng), để
        thứ tự đọc là: bình thường → vượt ngưỡng, đúng chiều diễn biến của một sự cố thật.
        """
        device_code = str(case.get("device_code") or self.defaults.get("device_code"))
        serial = str(case.get("battery") or "")
        if not serial:
            return True

        dev = self._device_cfg(device_code)
        bat_cfg = next((b for b in dev.batteries if b.serial == serial), None)
        if bat_cfg is None:
            # Pin không nằm trong seed của thiết bị → bỏ warm-up, case vẫn chạy bình thường.
            return True

        battery = MockBattery(bat_cfg)
        scenario = self._WARMUP_SCENARIOS.get(str(case.get("anomaly") or ""), "normal")
        # Mốc gốc của case; warm-up nằm TRƯỚC nó nên trừ dần theo `interval_s`.
        case_base = self._base_time(serial)

        readings: list[SensorReading] = []
        stamps: list[datetime] = []
        for i in range(count, 0, -1):
            r = battery.step(dt_s=interval_s, t_global=float(count - i) * interval_s,
                             scenario=scenario)
            r.serial = serial
            r.battery_asset_id = bat_cfg.battery_asset_id or ""
            readings.append(r)
            stamps.append(case_base - timedelta(seconds=i * interval_s))

        if self.dry_run:
            result.detail = (result.detail
                             or f"warm-up {count} số đo nền (MockBattery, scenario={scenario})")
            return True

        # Gửi từng mốc một: `build_production_batch_payload` đóng CHUNG một `deviceTimestamp`
        # cho cả batch, mà warm-up cần các mốc cách nhau `interval_s` để dựng nên đường diễn
        # biến — gộp một batch thì mọi dòng dồn về cùng một giây và mất hẳn ý nghĩa đó.
        client = self.client(device_code)
        ok = True
        for reading, stamp in zip(readings, stamps):
            payload = build_production_batch_payload([reading], _iso(stamp), device_code)
            if payload is None:
                continue
            res = client.ingest(payload, str(uuid.uuid4()))
            if not res.ok:
                ok = False
                log.warning("warm-up thất bại HTTP %s — case vẫn chạy tiếp", res.status_code)
                break
        return ok

    def send_wave(self, case: dict, result: CaseResult, count: int) -> bool:
        """Gửi MỘT đợt gồm `count` lần lặp trong DUY NHẤT một batch. Trả False nếu hỏng.

        Gộp cả đợt vào một request vì hai lẽ: gateway giới hạn 60 request / 30 giây cho lời gọi
        dùng `X-Api-Key` (đã chết thật ở lần chạy đầu vì gửi từng số đo một), và
        `patch_item_timestamp` vốn đã cấp cho mỗi item trong batch một mili-giây riêng nên `Time`
        vẫn khác nhau — đúng thứ bộ đếm chống nhiễu cần (nó khử trùng theo `reading.Time`).
        """
        device_code = str(case.get("device_code") or self.defaults.get("device_code"))
        serial = str(case.get("battery") or "")
        specs = case.get("readings") or [case.get("reading") or {}]

        readings: list[SensorReading] = []
        for _ in range(count):
            readings.extend(build_reading(spec, serial) for spec in specs)

        iso = _iso(self._base_time(serial))
        payload = build_production_batch_payload(readings, iso, device_code)
        if payload is None:
            result.status = "FAIL"
            result.detail = "dựng payload thất bại"
            return False

        if self.dry_run:
            import json
            result.status = "DRY"
            result.sent += len(readings)
            if not result.detail:
                result.detail = json.dumps(payload, ensure_ascii=False)
            return True

        client = self.client(device_code)
        res = self._send(lambda: client.ingest(payload, str(uuid.uuid4())), result, "ingest")
        result.sent += len(readings)
        data = (res.json or {}).get("data") if isinstance(res.json, dict) else None
        if isinstance(data, dict):
            result.inserted += int(data.get("inserted") or 0)
            result.skipped += int(data.get("skipped") or 0)
        if not res.ok:
            result.status = "FAIL"
            result.detail = f"HTTP {res.status_code}: {res.body[:200]}"
            return False
        if isinstance(res.json, dict) and res.json.get("message"):
            msg = str(res.json["message"])
            if "already existed" in msg or "outlier" in msg.lower():
                result.detail = msg
        return True

    def rebase_clock(self) -> None:
        """Đặt lại mốc gốc + bảng ô thời gian — gọi trước mỗi đợt mới.

        Đợt sau cách đợt trước hơn 10 giây, nên phải lấy mốc "vừa xong" của chính lúc đó; dùng
        lại mốc cũ sẽ đẩy số đo ra ngoài cửa sổ quét 20 giây và chúng không bao giờ được xét.
        """
        self._run_start = datetime.now(timezone.utc)
        self._slots = {}

    def _run_readings(self, case: dict, result: CaseResult, cross_source: bool) -> CaseResult:
        """Chạy trọn một case (dùng cho `--case` đơn lẻ và cho case chéo nguồn)."""
        if not self.precheck(case, result):
            return result
        waves = self.waves_for(case)
        gap = float((self.dataset.get("meta", {}) or {}).get("wave_gap_s", 14))
        for i, count in enumerate(waves):
            if i > 0 and not self.dry_run:
                print(f"    ⏳ chờ {gap:.0f}s cho đợt {i} được ghi nhận rồi gửi đợt {i + 1}")
                _time.sleep(gap)
                self.rebase_clock()
            if not self.send_wave(case, result, count):
                return result
        return result

    def _run_ambient(self, case: dict, result: CaseResult) -> CaseResult:
        device_code = str(case.get("device_code") or self.defaults.get("device_code"))
        info = self.provision(device_code)
        site_id = info["site_id"]
        if not site_id:
            result.status = "FAIL"
            result.detail = "provision không trả siteId — không gửi được ambient"
            return result

        spec = case.get("reading") or {}
        repeat = self._repeat(case)
        base = self._base_time(f"ambient:{site_id}")

        items = []
        for k in range(repeat):
            items.append({
                "siteId": site_id,
                # Ambient cũng có khoá theo (site, time) → mỗi lần lặp một mili-giây riêng.
                "time": _iso(base).replace("Z", f".{k % 1000:03d}Z"),
                "ambientTemperature": _num(spec.get("ambient_temperature")),
                "humidity": _num(spec.get("humidity")),
                "source": 1,                              # IotSensor — backend chỉ nhận SỐ
                "sourceDeviceId": device_code,
            })

        if self.dry_run:
            import json
            result.status = "DRY"
            result.sent = len(items)
            result.detail = json.dumps({"items": items}, ensure_ascii=False)
            return result

        client = self.client(device_code)
        res = self._send(lambda: client.ambient_ingest({"items": items}), result, "ambient")
        result.sent = len(items)
        if not res.ok:
            result.status = "FAIL"
            result.detail = f"HTTP {res.status_code}: {res.body[:200]}"
            return result
        result.inserted = len(items)
        return result

    def _run_incident(self, case: dict, result: CaseResult) -> CaseResult:
        """`POST /api/environmental-incidents` — cảnh báo CẤP SITE (`AnomalyType = 14`).

        Khác mọi case còn lại ở ba điểm, và cả ba đều quyết định cách đọc kết quả:

          1. **Không đi qua bộ quét ngưỡng.** Alert được tạo NGAY trong handler của endpoint, nên
             không cần chờ 10 giây và cũng không cần chia đợt.
          2. **Được miễn luật chống nhiễu** (`AnomalyDetectionService`) — một lần báo là đủ.
          3. **Khử trùng theo `(site, incidentType)` KHÔNG CÓ CỬA SỔ THỜI GIAN.** Chỉ cần tồn tại
             một sự cố `Open` hoặc `Acknowledged` cùng cặp đó là backend **dùng lại** nó và trả
             **200 "Existing active incident reused"** — KHÔNG tạo alert mới, dù đã một tháng.
             Vì thế bộ chạy phân biệt rõ 201 (tạo mới) với 200 (dùng lại) thay vì báo "thành công"
             chung chung: 200 ở đây nghĩa là **demo không sinh cảnh báo nào**.
        """
        device_code = str(case.get("device_code") or self.defaults.get("device_code"))
        info = self.provision(device_code)
        site_id = info["site_id"]
        if not site_id:
            result.status = "FAIL"
            result.detail = "provision không trả siteId — không gửi được sự cố"
            return result

        spec = case.get("incident") or {}
        incident = EnvironmentalIncident(
            site_id_guid=site_id,
            incident_type=int(spec.get("incident_type", IncidentType.GAS_LEAK)),
            severity=int(spec.get("severity", IncidentSeverity.CRITICAL)),
            # `DetectedAt` không được vượt quá now + 5 phút; lùi 1 giây cho chắc chắn.
            detected_at=_iso(datetime.now(timezone.utc) - timedelta(seconds=1)),
            reported_by=device_code,
            notes=str(spec.get("notes") or ""),
        )
        payload = incident.to_payload()

        if self.dry_run:
            import json
            result.status = "DRY"
            result.sent = 1
            result.detail = json.dumps(payload, ensure_ascii=False)
            return result

        client = self.client(device_code)
        res = self._send(lambda: client.environmental_incident(payload), result, "incident")
        result.sent = 1
        if not res.ok:
            result.status = "FAIL"
            result.detail = f"HTTP {res.status_code}: {res.body[:200]}"
            return result

        if res.status_code == 200:
            # Backend dùng lại sự cố đang mở ⇒ KHÔNG có alert mới. Báo rõ, đừng để người demo
            # tưởng đã xong rồi đi tìm cảnh báo không tồn tại.
            result.status = "SKIPPED"
            result.detail = ("backend DÙNG LẠI sự cố đang mở cùng (site, loại) — không tạo cảnh "
                            "báo mới. Giải quyết sự cố cũ rồi chạy lại: xem `anomaly verify`")
            return result

        result.inserted = 1
        return result


# ─────────────────────────────────────── lệnh CLI ───────────────────────────────────────────
def _load_dataset(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Không tìm thấy dataset: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _select(cases: list[dict], wanted: str | None) -> list[dict]:
    if not wanted:
        return cases
    ids = {int(x) for x in wanted.replace(" ", "").split(",") if x}
    picked = [c for c in cases if int(c.get("id", 0)) in ids]
    missing = ids - {int(c.get("id", 0)) for c in picked}
    if missing:
        raise ValueError(f"Không có case: {sorted(missing)}")
    return picked


def cmd_list(dataset: dict) -> int:
    cases = dataset.get("cases", []) or []
    print(f"{'ID':>3}  {'Anomaly':<24} {'Severity':<9} {'Kind':<14} {'Battery':<18} Tiêu đề")
    print("─" * 118)
    for c in cases:
        print(f"{c.get('id', ''):>3}  {str(c.get('anomaly', '')):<24} "
              f"{str(c.get('severity') or '-'):<9} {str(c.get('kind') or 'sensor_reading'):<14} "
              f"{str(c.get('battery') or '-'):<18} {c.get('title', '')}")
    danger = [c for c in cases if c.get("dangerous")]
    if danger:
        print("\n🔴 CASE CÓ TÁC DỤNG PHỤ NẶNG (bị giữ lại trừ khi có --include-dangerous):")
        for c in danger:
            print(f"  · case {c['id']} ({c['anomaly']}): {c.get('title', '')}")
            for ln in str(c.get("danger", "")).strip().splitlines():
                print("      " + ln)

    conflict = [c for c in cases if c.get("conflicts_with")]
    if conflict:
        print("\n⊘ CASE LOẠI TRỪ LẪN NHAU (backend khử trùng theo (site, loại), không phân biệt mức):")
        for c in conflict:
            print(f"  · case {c['id']} ({c['anomaly']} {c.get('severity')}) ⟷ case "
                  f"{c['conflicts_with']} — chạy riêng từng cái")

    blocked = [c for c in cases if c.get("requires")]
    if blocked:
        print("\n⚠ CASE CẦN CHỈNH BACKEND TRƯỚC:")
        for c in blocked:
            print(f"  · case {c['id']} ({c['anomaly']}): {str(c['requires']).strip()}")
    manual = [c for c in cases if c.get("kind") == "manual"]
    for c in manual:
        print(f"\n▶ case {c['id']} ({c['anomaly']}) — chạy tay:\n"
              + "\n".join("    " + ln for ln in str(c.get("instructions", "")).strip().splitlines()))
    return 0


def cmd_check(runner: AnomalyRunner, dataset: dict) -> int:
    cases = dataset.get("cases", []) or []
    devices = sorted({str(c.get("device_code") or runner.defaults.get("device_code"))
                      for c in cases if c.get("kind") != "manual"})

    print("═══ KIỂM TRA ĐIỀU KIỆN (không gửi số đo nào) ═══\n")
    print(f"Backend      : {runner.sim_cfg.backend.base_url}")
    print(f"Contract     : {runner.sim_cfg.backend.contract_version}")
    if runner.sim_cfg.backend.contract_version != CONTRACT_IOT2:
        print("\n✗ contract_version phải là `iot2-production`. Contract `current` không có "
              "provision/ambient nên KHÔNG chạy được dataset này.")
        return 2

    ok = True
    for code in devices:
        try:
            info = runner.provision(code)
        except Exception as ex:                        # noqa: BLE001
            print(f"\n✗ {code}: {ex}")
            ok = False
            continue
        print(f"\n✓ {code}")
        print(f"    siteId  : {info['site_id']}")
        print(f"    polling : {info['polling_s']}s")
        print(f"    pin được phép gửi ({len(info['serials'])}): {', '.join(info['serials']) or '(trống)'}")

    print("\n─── Từng case ───")
    for c in cases:
        cid, anomaly = c.get("id"), c.get("anomaly")
        kind = c.get("kind") or "sensor_reading"
        if kind == "manual":
            print(f"  {cid:>3} {anomaly:<24} CHẠY TAY")
            continue
        serial = c.get("battery")
        code = str(c.get("device_code") or runner.defaults.get("device_code"))
        info = runner._provision.get(code)
        if info is None:
            print(f"  {cid:>3} {anomaly:<24} ✗ thiết bị {code} không provision được")
            ok = False
            continue
        # Ambient và sự cố môi trường là cảnh báo CẤP SITE — không gắn với viên pin nào, nên
        # KHÔNG áp luật #IoT2-18. Thiếu nhánh này thì `serial` = None và case bị báo "BỊ CHẶN" oan.
        if kind in SINGLE_SHOT_KINDS:
            what = "ambient" if kind == "ambient" else "sự cố môi trường"
            print(f"  {cid:>3} {anomaly:<24} ✓ gửi được ({what}, siteId có sẵn)")
            continue
        if info["serials"] and serial not in info["serials"]:
            print(f"  {cid:>3} {anomaly:<24} ✗ BỊ CHẶN — backend không giao pin {serial} "
                  f"cho {code} (#IoT2-18)")
            ok = False
            continue
        flag = "  ⚠ cần chỉnh backend" if c.get("requires") else ""
        print(f"  {cid:>3} {anomaly:<24} ✓ gửi được (pin {serial}){flag}")

    print("\n─── Ngưỡng phía backend (chạy để tự kiểm) ───")
    print("  docker exec solar-postgres psql -U postgres -d battery_db -c \"\n"
          "    SELECT bt.name, tc.current_max_charge, tc.current_max_discharge,\n"
          "           tc.internal_resistance_max_milliohm, tc.cell_voltage_delta_max_mv,\n"
          "           tc.noise_suppression_enabled, tc.noise_suppression_count\n"
          "    FROM threshold_configs tc JOIN battery_types bt ON bt.id=tc.battery_type_id\n"
          "    WHERE tc.is_active;\"")
    print("\n  Cột NULL nghĩa là luật tương ứng KHÔNG BAO GIỜ chạy (xem `requires` của case 9/10/13/14).")
    return 0 if ok else 1


def _new_result(case: dict) -> CaseResult:
    return CaseResult(case_id=int(case.get("id", 0)), anomaly=str(case.get("anomaly", "?")),
                      severity=str(case.get("severity") or "-"), status="OK")


def _filter_dangerous(cases: list[dict], include_dangerous: bool) -> tuple[list[dict], list[dict]]:
    """Tách case có tác dụng phụ NẶNG ra khỏi lượt chạy thường.

    Hiện chỉ có `IotDataIntegrityViolation`: nó làm backend **vô hiệu hoá vĩnh viễn** thiết bị,
    và sau đó mọi request của thiết bị đó trả 401 vì tra khoá API loại thẳng thiết bị
    `Decommissioned` (`IotApiKeyService.LookupDeviceByRawKeyAsync`). Không có đường tự hồi phục.
    Chạy nhầm nó giữa buổi demo là mất luôn thiết bị, nên phải bật tường minh bằng cờ.
    """
    if include_dangerous:
        return cases, []
    safe = [c for c in cases if not c.get("dangerous")]
    held = [c for c in cases if c.get("dangerous")]
    return safe, held


def _resolve_conflicts(cases: list[dict]) -> tuple[list[dict], list[tuple[dict, int]]]:
    """Loại case bị KHỬ TRÙNG bởi một case khác trong cùng lượt chạy.

    Backend khử trùng cảnh báo ambient theo `(site, loại)` — KHÔNG phân biệt mức nghiêm trọng —
    trong 1 giờ. Nên `HighAmbientTemp Warning` và `HighAmbientTemp Critical` không thể cùng nổ
    trong một lượt: cái gửi sau bị nuốt IM LẶNG (`hasRecent` → `continue`, không log, không lỗi).

    Thà bỏ qua và nói rõ lý do còn hơn gửi rồi để người demo đi tìm một cảnh báo không bao giờ có.
    """
    ids = {int(c.get("id", 0)) for c in cases}
    kept: list[dict] = []
    dropped: list[tuple[dict, int]] = []
    for c in cases:
        other = c.get("conflicts_with")
        if other is not None and int(other) in ids:
            dropped.append((c, int(other)))
            continue
        kept.append(c)
    return kept, dropped


def _print_case_header(case: dict) -> None:
    print(f"▶ case {case.get('id')}: {case.get('anomaly')} / {case.get('severity') or '-'} "
          f"— {case.get('title', '')}")


def _print_case_result(r: CaseResult) -> None:
    if r.status == "OK":
        codes = ",".join(str(x) for x in sorted(set(r.http_codes)))
        line = f"    ✓ gửi {r.sent} reading · backend nhận {r.inserted}, bỏ {r.skipped} · HTTP {codes}"
        print(line)
        if r.skipped:
            print(f"    ⚠ {r.detail or 'có reading bị bỏ — xem message của backend'}")
    elif r.status == "DRY":
        print(f"    payload mẫu: {r.detail[:400]}")
    elif r.status == "SKIPPED":
        # KHÔNG dùng dấu ✗: đây không phải lỗi, mà là backend cố ý khử trùng.
        print(f"    ⊘ {r.detail}")
    else:
        print(f"    ✗ {r.status}: {r.detail}")


def cmd_run(runner: AnomalyRunner, dataset: dict, wanted: str | None, fast: bool = False,
            include_dangerous: bool = False, warmup: bool = True) -> int:
    cases = _select(dataset.get("cases", []) or [], wanted)
    if runner.sim_cfg.backend.contract_version != CONTRACT_IOT2 and not runner.dry_run:
        print("✗ contract_version phải là `iot2-production`.")
        return 2

    meta = dataset.get("meta", {}) or {}
    gap = float(meta.get("wave_gap_s", 14))
    isolation = float(meta.get("cross_source_isolation_s", 65))

    cases, held = _filter_dangerous(cases, include_dangerous)
    cases, conflicting = _resolve_conflicts(cases)

    print(f"Backend  : {runner.sim_cfg.backend.base_url}")
    print(f"Dataset  : {len(cases)} case" + ("  (DRY-RUN — không gửi gì)" if runner.dry_run else ""))
    print(f"Bộ quét  : mỗi {meta.get('scan_interval_s', '?')}s · nhìn lại "
          f"{meta.get('scan_lookback_s', '?')}s · gộp cảnh báo trùng trong "
          f"{meta.get('alert_dedup_window_min', '?')} phút\n")

    manual = [c for c in cases if (c.get("kind") or "") == "manual"]
    cross = [c for c in cases if (c.get("kind") or "") == "cross_source"]
    normal = [c for c in cases if c not in manual and c not in cross]

    results: list[CaseResult] = []
    plans: list[tuple[dict, CaseResult, list[int] | None]] = []

    # ── Đợt 1 ─────────────────────────────────────────────────────────────────────────────
    if normal:
        print("─── ĐỢT 1 " + "─" * 58)
    runner.rebase_clock()
    for c in normal:
        r = _new_result(c)
        _print_case_header(c)
        # Hai loại này KHÔNG đi qua bộ quét ngưỡng nên không cần chia đợt, và cũng không gắn với
        # một viên pin cụ thể — bỏ qua luôn bước kiểm quyền theo `battery`.
        if (c.get("kind") or "") in SINGLE_SHOT_KINDS:
            runner.run_case_into(c, r)
            plans.append((c, r, None))
        elif runner.precheck(c, r):
            # Nền TRƯỚC số đo gây lỗi: bảng bằng chứng cần thấy pin đang xấu dần, không phải
            # một con số lỗi đứng trơ trọi. Hỏng warm-up không chặn case — nó chỉ là bối cảnh.
            if warmup:
                runner.send_warmup(c, r)
            waves = runner.waves_for(c)
            if runner.send_wave(c, r, waves[0]):
                plans.append((c, r, waves))
            else:
                plans.append((c, r, None))
        _print_case_result(r)
        results.append(r)
        print()

    # ── Đợt 2 — chỉ những case cần vượt ngưỡng chống nhiễu ────────────────────────────────
    pending = [(c, r, w) for c, r, w in plans if w and len(w) > 1 and r.status == "OK"]
    if pending and not runner.dry_run:
        print(f"─── ĐỢT 2 (sau {gap:.0f}s, để đợt 1 kịp được ghi nhận) " + "─" * 26)
        print(f"⏳ chờ {gap:.0f}s…")
        _time.sleep(gap)
        runner.rebase_clock()
        for c, r, waves in pending:
            _print_case_header(c)
            runner.send_wave(c, r, waves[1])
            _print_case_result(r)
            print()
    elif pending and runner.dry_run:
        for c, r, waves in pending:
            runner.send_wave(c, r, waves[1])

    # ── Chéo nguồn — phải TÁCH KHỎI mọi số đo khác của cùng pin ───────────────────────────
    for c in cross:
        r = _new_result(c)
        if not runner.dry_run and normal and not fast:
            print("─── CHÉO NGUỒN " + "─" * 54)
            print(f"⏳ chờ {isolation:.0f}s trước khi gửi cặp chéo nguồn.")
            print("   Lý do: backend ghép MỌI số đo `primary` với MỌI số đo `IotGateway` của cùng")
            print("   pin trong cửa sổ 60 giây. Các case phía trên vừa đẩy hàng loạt số đo lệch")
            print("   nhau (15,2V / 9,5V / 62°C / −18°C…) lên cùng pin đó; gửi cặp chéo nguồn ngay")
            print("   lúc này sẽ ghép nhầm và sinh HÀNG CHỤC cảnh báo SensorMismatch giả — đã quan")
            print("   sát được 57 cảnh báo giả khi chạy thật. Dùng --fast để bỏ qua (chấp nhận rác).")
            _time.sleep(isolation)
            runner.rebase_clock()
        _print_case_header(c)
        runner.run_case_into(c, r)
        _print_case_result(r)
        results.append(r)
        print()

    # ── Chạy tay ──────────────────────────────────────────────────────────────────────────
    for c in manual:
        r = _new_result(c)
        r.status = "MANUAL"
        _print_case_header(c)
        print("    CHẠY TAY:")
        for ln in str(c.get("instructions", "")).strip().splitlines():
            print("      " + ln)
        results.append(r)
        print()

    ok = sum(1 for r in results if r.status in ("OK", "DRY"))
    bad = [r for r in results if r.status in ("FAIL", "BLOCKED")]
    skipped = [r for r in results if r.status == "SKIPPED"]
    print("═" * 70)
    print(f"Tổng: {ok} chạy được · {len(bad)} lỗi/bị chặn · {len(skipped)} bị backend khử trùng "
          f"· {len(manual)} chạy tay")
    for r in bad:
        print(f"  ✗ case {r.case_id} ({r.anomaly}): {r.detail}")
    for r in skipped:
        print(f"  ⊘ case {r.case_id} ({r.anomaly}): {r.detail}")

    if conflicting:
        print("\n⊘ BỎ QUA vì trùng khoá khử trùng với case khác trong CÙNG lượt chạy:")
        for c, other in conflicting:
            print(f"  · case {c['id']} ({c['anomaly']} {c.get('severity')}) trùng "
                  f"(site, loại) với case {other}")
        print("  Backend khử trùng cảnh báo ambient theo (site, loại) trong 1 giờ, KHÔNG phân biệt")
        print("  mức nghiêm trọng — gửi cả hai thì cái sau bị nuốt im lặng. Chạy riêng sau khi")
        print("  giải quyết cảnh báo cũ:")
        print("    python -m src.anomaly verify        # lấy câu SQL giải quyết cảnh báo")
        print(f"    python -m src.anomaly run --case "
              f"{','.join(str(c['id']) for c, _ in conflicting)}")

    if held:
        print("\n⚠ GIỮ LẠI case có tác dụng phụ NẶNG (cần bật tường minh):")
        for c in held:
            print(f"  · case {c['id']} ({c['anomaly']}): {c.get('title', '')}")
            for ln in str(c.get("danger", "")).strip().splitlines():
                print("      " + ln)
        print(f"  Chạy bằng: python -m src.anomaly run --case "
              f"{','.join(str(c['id']) for c in held)} --include-dangerous")

    if not runner.dry_run and ok:
        wait = int(meta.get("scan_interval_s", 10)) * 2 + 5
        print(f"\nChờ ~{wait}s cho bộ quét chạy đủ, rồi kiểm chứng:")
        print("  python -m src.anomaly verify")
    return 0 if not bad else 1


def cmd_verify(dataset: dict) -> int:
    """In sẵn câu SQL kiểm chứng — không tự chạy vì bộ chạy không có quyền vào DB."""
    print("═══ KIỂM CHỨNG KẾT QUẢ ═══\n")
    print("1) Cảnh báo vừa sinh (5 phút gần nhất):\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  SELECT a.detected_at, ba.serial_number,")
    print("         CASE a.anomaly_type " + " ".join(
        f"WHEN {v} THEN '{k}'" for k, v in sorted(ANOMALY_TYPE_IDS.items(), key=lambda x: x[1])) + " END AS anomaly,")
    print("         CASE a.severity WHEN 1 THEN 'Info' WHEN 2 THEN 'Warning' WHEN 3 THEN 'Critical' END AS severity,")
    print("         a.threshold_value, a.actual_value, a.unit, a.status, a.ticket_id")
    print("  FROM alerts a LEFT JOIN battery_assets ba ON ba.id = a.battery_asset_id")
    print("  WHERE a.detected_at > now() - interval '5 minutes' AND NOT a.is_deleted")
    print("  ORDER BY a.detected_at DESC;\"\n")

    print("2) Bộ đếm chống nhiễu (vì sao cảnh báo CHƯA nổ):\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  SELECT ba.serial_number, n.anomaly_type, count(*) AS breaches, max(n.time) AS latest")
    print("  FROM noise_breach_events n JOIN battery_assets ba ON ba.id = n.battery_asset_id")
    print("  WHERE n.time > now() - interval '30 minutes'")
    print("  GROUP BY 1,2 ORDER BY 4 DESC;\"\n")
    print("   → cần >= 5 breach cùng (pin, loại) mới nổ cảnh báo (trừ Overheat Critical).\n")

    print("3) Số đo vừa vào:\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  SELECT ba.serial_number, sr.time, sr.voltage, sr.temperature, sr.soc_percent,")
    print("         sr.soh_percent, sr.sensor_source_code, sr.bms_error_code")
    print("  FROM sensor_readings sr JOIN battery_assets ba ON ba.id = sr.battery_asset_id")
    print("  WHERE sr.time > now() - interval '5 minutes'")
    print("  ORDER BY sr.time DESC LIMIT 30;\"\n")

    print("4) Ticket sinh từ cảnh báo (nếu Saga V2 bật):\n")
    print("docker exec solar-postgres psql -U postgres -d ticket_db -c \"")
    print("  SELECT code, title, status, created_at FROM tickets")
    print("  WHERE created_at > now() - interval '10 minutes' ORDER BY created_at DESC;\"\n")

    print("5) Sự cố môi trường (case 21–23, cảnh báo cấp site):\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  SELECT i.detected_at,")
    print("         CASE i.incident_type WHEN 1 THEN 'Smoke' WHEN 2 THEN 'FireDetected'")
    print("              WHEN 3 THEN 'GasLeak' WHEN 4 THEN 'Flood' WHEN 5 THEN 'OverheatHazard'")
    print("              WHEN 99 THEN 'Other' END AS loai,")
    print("         CASE i.status WHEN 1 THEN 'Open' WHEN 2 THEN 'Acknowledged'")
    print("              WHEN 3 THEN 'Resolved' WHEN 4 THEN 'FalseAlarm' END AS trang_thai,")
    print("         i.reported_by, s.name AS site")
    print("  FROM environmental_incidents i JOIN sites s ON s.id = i.site_id")
    print("  ORDER BY i.detected_at DESC LIMIT 15;\"\n")

    print("6) Thiết bị (case 15 ngoại tuyến · case 26 vi phạm toàn vẹn dữ liệu):\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  SELECT device_code,")
    print("         CASE status WHEN 1 THEN 'Pending' WHEN 2 THEN 'Active' WHEN 3 THEN 'Offline'")
    print("              WHEN 4 THEN 'Disabled' WHEN 5 THEN 'Decommissioned' END AS trang_thai,")
    print("         last_seen_at, outlier_incident_count, auto_decommissioned_at")
    print("  FROM iot_devices WHERE device_code LIKE 'ESP32-SIM-%';\"\n")

    print("═" * 78)
    print("DỌN ĐỂ DEMO LẠI — ba luật khử trùng khác nhau, cần ba câu khác nhau")
    print("═" * 78)

    print("\n(a) Cảnh báo theo NGƯỠNG PIN — gộp `(pin, loại)` trong 30 phút")
    print("    (`AnomalyEngine__DedupWindowMinutes`). Chạy lại trong cửa sổ đó chỉ tạo dòng")
    print("    `Merged`, không có cảnh báo `Open` mới.\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  DELETE FROM alerts              WHERE detected_at > now() - interval '1 hour';")
    print("  DELETE FROM noise_breach_events WHERE time        > now() - interval '1 hour';\"")

    print("\n(b) Cảnh báo AMBIENT — khử trùng `(site, loại)` trong 1 giờ, KHÔNG phân biệt mức.")
    print("    Đây là lý do case 24/25 không thể chạy cùng lượt với case 17/18.")
    print("    Câu (a) ở trên đã xoá luôn nhóm này (alerts có `battery_asset_id IS NULL`).")

    print("\n(c) SỰ CỐ MÔI TRƯỜNG — khử trùng `(site, loại)` **KHÔNG có cửa sổ thời gian**.")
    print("    Còn một sự cố `Open`/`Acknowledged` là mọi lần báo sau đều bị dùng lại (HTTP 200).")
    print("    Đóng chúng lại thay vì xoá — đóng đúng nghiệp vụ hơn:\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  UPDATE environmental_incidents SET status = 3, resolved_at = now()")
    print("  WHERE status IN (1, 2);\"")
    print("\n    ⚠ Site Solar Farm Long An có sẵn hai sự cố mở từ dữ liệu seed")
    print("      (incident_type = 1 Smoke, và 5 OverheatHazard) — chúng chặn đúng hai loại đó.")

    print("\n(d) KHÔI PHỤC thiết bị bị vô hiệu hoá sau case 26 — BẮT BUỘC nếu đã chạy case đó.")
    print("    Thiết bị `Decommissioned` bị loại khỏi bảng tra khoá API ⇒ mọi request trả 401,")
    print("    và không có đường tự hồi phục.\n")
    print("docker exec solar-postgres psql -U postgres -d battery_db -c \"")
    print("  UPDATE iot_devices SET status = 2, auto_decommissioned_at = NULL,")
    print("                         outlier_incident_count = 0, outlier_window_started_at = NULL")
    print("  WHERE device_code = 'ESP32-SIM-002';\"")

    print("\n⚠ Mọi câu trên tác động THẬT vào DB. Chỉ dùng trên môi trường dev/demo.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.anomaly", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["list", "check", "run", "verify"])
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--seed", default=None, help="seed YAML (mặc định config/seed.yaml)")
    p.add_argument("--case", default=None, help="danh sách id, vd 1,2,5")
    p.add_argument("--dry-run", action="store_true", help="in payload thật, KHÔNG gửi")
    p.add_argument("--include-dangerous", action="store_true",
                   help="cho phép chạy case có tác dụng phụ NẶNG (vô hiệu hoá thiết bị). "
                        "Không có cờ này thì chúng bị giữ lại kể cả khi chọn bằng --case")
    p.add_argument("--no-warmup", action="store_true",
                   help="bỏ bước gửi số đo nền trước mỗi case. Mặc định CÓ gửi: thiếu nó thì "
                        "bảng bằng chứng chỉ có đúng một dòng số tròn trịa, không thấy pin xấu "
                        "dần nên đọc lên không khác gì dữ liệu bịa")
    p.add_argument("--fast", action="store_true",
                   help="bỏ khoảng chờ tách cặp chéo nguồn — nhanh hơn nhưng sinh cảnh báo "
                        "SensorMismatch giả (xem giải thích lúc chạy)")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s — %(message)s")

    dataset = _load_dataset(args.dataset)
    if args.command == "list":
        return cmd_list(dataset)
    if args.command == "verify":
        return cmd_verify(dataset)

    sim_cfg = load_config(args.seed)
    runner = AnomalyRunner(sim_cfg, dataset, dry_run=args.dry_run)
    if args.command == "check":
        return cmd_check(runner, dataset)
    return cmd_run(runner, dataset, args.case, fast=args.fast,
                   include_dangerous=args.include_dangerous, warmup=not args.no_warmup)


if __name__ == "__main__":
    sys.exit(main())
