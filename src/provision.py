"""Luồng provision — mirror `firmware-esp32/src/provision/provision.cpp` (S2-FW-02 + IOT3-42/49).

`POST /api/iot-devices/provision` → `CommonResponse<IotDeviceProvisionResultDto>`:

    { "isSuccess": true, "statusCode": 200, "data": {
        "deviceId", "deviceCode", "siteId",
        "heartbeatIntervalSeconds", "pollingIntervalSeconds", "ntpServer",
        "mqttBrokerHost", "mqttBrokerPort", "mqttUseTls",
        "mqttTopicPrefix", "mqttUsername", "mqttPassword",       ← IOT3-42
        "batteryMappings": [ { "batteryAssetSerial", "unitId", "sensorSourceCode" } ],  ← IOT3-49
        "supportedSensors": [...] } }

Ba thứ bản simulator cũ BỎ QUA và nay đã đọc đủ:
  1. **6 trường MQTT** — credential broker do backend cấp lúc chạy. Thiếu chúng thì mỗi thiết bị
     phải chép tay mật khẩu vào seed, và admin xoay key là simulator câm.
  2. **`batteryMappings[]`** — tập pin backend thực sự giao cho thiết bị. Không đọc thì thiết bị
     gửi theo bảng cứng, backend **vẫn trả 201** nhưng lặng lẽ bỏ reading (GH-748).
  3. **Ghi trạng thái đã provision** — firmware chỉ provision ở lần boot ĐẦU; các lần sau nạp từ
     NVS. Provision lại mỗi lần chạy là mô phỏng SAI luồng mà backend nhìn thấy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import nvs as nvskeys
from .battery_map import (BatteryMapEntry, BatteryMapError,
                          MAX_BATTERY_MAP_ENTRIES, describe_battery_map_error,
                          validate_battery_map_entry)

log = logging.getLogger("iot-sim.provision")

# provision.h — giá trị mặc định của firmware.
DEFAULT_POLLING_S = 5
DEFAULT_HEARTBEAT_S = 60
DEFAULT_NTP_SERVER = "time.google.com"

# Ràng buộc do firmware áp (provision.cpp §3 "Sanity bounds").
POLLING_MIN_S, POLLING_MAX_S = 1, 600
HEARTBEAT_MIN_S, HEARTBEAT_MAX_S = 10, 3600

# main.cpp — provision fail thì thử lại sau 30s.
PROVISION_RETRY_MS = 30000


@dataclass
class ProvisionedConfig:
    """`provision::ProvisionedConfig`."""

    provisioned: bool = False
    polling_interval_s: int = DEFAULT_POLLING_S
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_S
    site_id: str = ""
    ntp_server: str = DEFAULT_NTP_SERVER


@dataclass
class ProvisionParseResult:
    """Kết quả đọc thân response — tách khỏi I/O để test được."""

    ok: bool = False
    error: str = ""
    polling_interval_s: int = DEFAULT_POLLING_S
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_S
    site_id: str = ""
    ntp_server: str = DEFAULT_NTP_SERVER
    data: dict = field(default_factory=dict)


def _clamp_polling(value) -> int:
    """Khớp `provision.cpp`: sai kiểu/thiếu → default 5; <1 → 5; >600 → 600 (KHÔNG về default)."""
    try:
        sec = int(value)
    except (TypeError, ValueError):
        return DEFAULT_POLLING_S
    if sec < POLLING_MIN_S:
        return DEFAULT_POLLING_S
    if sec > POLLING_MAX_S:
        return POLLING_MAX_S
    return sec


def _clamp_heartbeat(value) -> int:
    """Khớp `provision.cpp`: thiếu → 60; <10 → 60; >3600 → 3600."""
    try:
        sec = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_S
    if sec < HEARTBEAT_MIN_S:
        return DEFAULT_HEARTBEAT_S
    if sec > HEARTBEAT_MAX_S:
        return HEARTBEAT_MAX_S
    return sec


def parse_provision_response(body) -> ProvisionParseResult:
    """Đọc `CommonResponse<IotDeviceProvisionResultDto>`.

    Firmware TỪ CHỐI khi `isSuccess` không phải true hoặc `data` rỗng — giữ nguyên: một response
    2xx nhưng `isSuccess=false` là backend nói "không, thiết bị này chưa được phép", coi là thành
    công sẽ khiến thiết bị chạy tiếp với cấu hình mặc định và gửi rác lên backend.
    """
    out = ProvisionParseResult()
    if not isinstance(body, dict):
        out.error = "response không phải JSON object"
        return out
    if body.get("isSuccess") is not True:
        out.error = f"backend isSuccess=false msg={body.get('message', 'unknown')}"
        return out
    data = body.get("data")
    if not isinstance(data, dict):
        out.error = "response không có data"
        return out

    out.data = data
    out.polling_interval_s = _clamp_polling(data.get("pollingIntervalSeconds"))
    out.heartbeat_interval_s = _clamp_heartbeat(data.get("heartbeatIntervalSeconds"))
    site = data.get("siteId")
    out.site_id = site if isinstance(site, str) else ""
    ntp = data.get("ntpServer")
    out.ntp_server = ntp if isinstance(ntp, str) and ntp else DEFAULT_NTP_SERVER
    out.ok = True
    return out


def parse_battery_mappings(data: dict) -> tuple[list[BatteryMapEntry], int, bool]:
    """Đọc `batteryMappings[]` — trả `(entries, số mục bị bỏ, backend CÓ gửi mảng này không)`.

    Mục hỏng bị BỎ kèm log (không vứt cả bảng), và số mục vượt trần 8 pin cũng tính là bỏ —
    im lặng cắt bớt ở đây nghĩa là vài pin biến mất khỏi telemetry mà không ai biết.
    """
    arr = data.get("batteryMappings")
    if not isinstance(arr, list):
        return [], 0, False

    entries: list[BatteryMapEntry] = []
    skipped = 0
    synthesised_unit_ids = 0
    for item in arr:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if len(entries) >= MAX_BATTERY_MAP_ENTRIES:
            skipped += 1
            continue
        serial = item.get("batteryAssetSerial") or ""
        unit_id = item.get("unitId")
        code = item.get("sensorSourceCode") or "primary"
        err = validate_battery_map_entry(serial, unit_id, code)
        if err is not BatteryMapError.OK:
            log.warning("BỎ mapping serial='%s' unitId=%s — %s", serial, unit_id,
                        describe_battery_map_error(err))
            skipped += 1
            continue
        # ⚠ Backend thật trả `unitId: null` — xem ghi chú ở `validate_battery_map_entry`.
        # Cấp số thứ tự thay thế để bảng vẫn dùng được; simulator không cần địa chỉ Modbus.
        if unit_id in (None, 0):
            unit_id = len(entries) + 1
            synthesised_unit_ids += 1
        entries.append(BatteryMapEntry(serial=str(serial), unit_id=int(unit_id),
                                       sensor_source_code=str(code)))
    if synthesised_unit_ids:
        log.info("%d mapping không có `unitId` (backend trả null) — đã cấp số thứ tự thay thế; "
                 "simulator không dùng địa chỉ Modbus nên không ảnh hưởng dữ liệu gửi đi",
                 synthesised_unit_ids)
    return entries, skipped, True


def parse_mqtt_settings(data: dict) -> dict | None:
    """Đọc 6 trường MQTT. Trả None khi backend tắt MQTT (`mqttBrokerHost` rỗng/thiếu).

    Backend cam kết "cả sáu trường hoặc không trường nào" (`IotDeviceProvisionResultDto`), nên
    `mqttBrokerHost` là cờ duy nhất cần xét. Trường hợp rỗng KHÔNG được xoá cấu hình đã có: thiết
    bị từng được cấp credential mà backend tạm tắt MQTT thì xoá đi là lần bật lại phải làm tay.
    """
    host = data.get("mqttBrokerHost") or ""
    if not host:
        return None
    return {
        "host": str(host),
        "port": data.get("mqttBrokerPort", 0),
        "use_tls": bool(data.get("mqttUseTls", False)),
        "prefix": data.get("mqttTopicPrefix") or "",
        "username": data.get("mqttUsername") or "",
        "password": data.get("mqttPassword") or "",
    }


class ProvisionRunner:
    """Bọc luồng provision + persist, giữ trạng thái `ProvisionedConfig` cho vòng lặp chính."""

    def __init__(self, device_code: str, http, store, mqtt_cfg, battery_map,
                 apply_battery_map: bool = True):
        self.device_code = device_code
        self._http = http
        self._nvs = store
        self._mqtt_cfg = mqtt_cfg
        self._battery_map = battery_map
        self._apply_battery_map = apply_battery_map
        self.mqtt_config_changed = False   # cờ để main loop gọi mqtt.apply_config()

    # ── nạp trạng thái đã lưu ─────────────────────────────────────────────────────────────
    def load_provisioned(self) -> ProvisionedConfig:
        """`provision::loadProvisioned` — có kiểm biên phòng khi state hỏng."""
        cfg = ProvisionedConfig()
        cfg.provisioned = self._nvs.get_bool(nvskeys.KEY_PROVISIONED, False)

        poll_ms = self._nvs.get_int(nvskeys.KEY_POLL_MS, DEFAULT_POLLING_S * 1000)
        hb_ms = self._nvs.get_int(nvskeys.KEY_HB_MS, DEFAULT_HEARTBEAT_S * 1000)
        if poll_ms < 1000 or poll_ms > 600000:
            poll_ms = DEFAULT_POLLING_S * 1000
        if hb_ms < 10000 or hb_ms > 3600000:
            hb_ms = DEFAULT_HEARTBEAT_S * 1000
        cfg.polling_interval_s = poll_ms // 1000
        cfg.heartbeat_interval_s = hb_ms // 1000

        cfg.site_id = self._nvs.get_string(nvskeys.KEY_SITE_ID, "")
        cfg.ntp_server = self._nvs.get_string(nvskeys.KEY_NTP, "") or DEFAULT_NTP_SERVER
        return cfg

    def clear_provision_flag(self) -> None:
        """`provision::clearProvisionFlag` — buộc chạy lại provision ở vòng tới (IOT3-44)."""
        self._nvs.put_bool(nvskeys.KEY_PROVISIONED, False)

    # ── chạy provision ────────────────────────────────────────────────────────────────────
    def run(self, firmware_version: str, hardware_revision: str,
            device_timestamp_iso: str) -> tuple[bool, ProvisionedConfig, str]:
        """`provision::runProvisionFlow`. Trả `(ok, config, thông điệp lỗi)`."""
        res = self._http.provision(hardware_revision=hardware_revision,
                                   device_timestamp_iso=device_timestamp_iso)
        if not res.ok:
            msg = f"HTTP {res.status_code}: {res.body[:160]}"
            log.warning("[%s] provision FAIL — %s", self.device_code, msg)
            return False, ProvisionedConfig(), msg

        parsed = parse_provision_response(res.json)
        if not parsed.ok:
            log.warning("[%s] provision FAIL — %s", self.device_code, parsed.error)
            return False, ProvisionedConfig(), parsed.error

        # 1) Lưu trạng thái (tương đương ghi NVS).
        self._nvs.put_int(nvskeys.KEY_POLL_MS, parsed.polling_interval_s * 1000)
        self._nvs.put_int(nvskeys.KEY_HB_MS, parsed.heartbeat_interval_s * 1000)
        self._nvs.put_string(nvskeys.KEY_SITE_ID, parsed.site_id)
        self._nvs.put_string(nvskeys.KEY_NTP, parsed.ntp_server)
        self._nvs.put_string(nvskeys.KEY_FW_VER, firmware_version)
        self._nvs.put_bool(nvskeys.KEY_PROVISIONED, True)

        cfg = ProvisionedConfig(
            provisioned=True,
            polling_interval_s=parsed.polling_interval_s,
            heartbeat_interval_s=parsed.heartbeat_interval_s,
            site_id=parsed.site_id,
            ntp_server=parsed.ntp_server,
        )

        # 2) IOT3-42 — 6 trường MQTT.
        self.mqtt_config_changed = self.apply_mqtt(parsed.data)

        # 3) IOT3-49 — bảng ánh xạ pin.
        self.apply_battery_mappings(parsed.data)

        log.info("[%s] provisioned, polling=%ds, heartbeat=%ds, site=%s, ntp=%s",
                 self.device_code, cfg.polling_interval_s, cfg.heartbeat_interval_s,
                 cfg.site_id or "(rỗng)", cfg.ntp_server)
        return True, cfg, ""

    def apply_mqtt(self, data: dict) -> bool:
        settings = parse_mqtt_settings(data)
        if settings is None:
            log.info("[%s] MQTT chưa bật ở backend — HTTPS-only (giữ nguyên cấu hình cũ)",
                     self.device_code)
            return False
        changed = self._mqtt_cfg.apply_from_provision(
            host=settings["host"], port=settings["port"], use_tls=settings["use_tls"],
            prefix=settings["prefix"], user=settings["username"],
            password=settings["password"])
        self._mqtt_cfg.warn_if_prefix_mismatch()
        return changed

    def apply_battery_mappings(self, data: dict) -> bool:
        entries, skipped, present = parse_battery_mappings(data)
        if not present:
            log.info("[%s] response không có batteryMappings[] — giữ bảng pin đang dùng",
                     self.device_code)
            return False
        if skipped:
            log.warning("[%s] ⚠ %d mapping bị BỎ QUA — số liệu của những pin đó sẽ KHÔNG được gửi",
                        self.device_code, skipped)
        if not self._apply_battery_map:
            log.info("[%s] apply_battery_map=false — bỏ qua batteryMappings[] của backend, "
                     "dùng bảng pin trong seed", self.device_code)
            return False
        return self._battery_map.apply_from_provision(entries)
