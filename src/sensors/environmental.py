"""Báo sự cố môi trường — mirror `firmware-esp32/src/sensor/environmental_incident.cpp`
(S6-FW-01/02 + GH-736/741).

`POST /api/environmental-incidents` — REST, KHÔNG qua MQTT (bridge của backend chỉ subscribe
telemetry/heartbeat/status/cmd-ack, không có topic incident).

Payload (khớp `environmental_incident.cpp`, đúng thứ tự trường):
    { "siteId": "<Guid>", "incidentType": <int>, "severity": <int>,
      "reportedBy": "<deviceCode>", "detectedAt": "<ISO8601 UTC>", "notes"?: "..." }

⚠ Yêu cầu triển khai: API key của thiết bị PHẢI có scope `EnvironmentalIngest` (bitmask 4).
`EdgeDeviceDefault` của backend = SensorIngest|DeviceHeartbeat|FirmwareCheck = 11, **KHÔNG có 4**
(audit `iot-backend-contract-gaps.md` #2) ⇒ thiếu scope thì endpoint này trả 403, và 403 là lỗi
VĨNH VIỄN nên sự cố bị BỎ. Vì thế khi gặp 401/403 module này in hẳn gợi ý ra log.

⚠ KHÔNG xếp hàng đợi: firmware không queue incident (chỉ giữ `pending` + backoff rồi thử lại).
Bản simulator cũ đẩy incident vào `local queue` — sai hành vi, và còn làm nghẽn hàng đợi telemetry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, IntEnum

from ..backoff import is_transient_failure

log = logging.getLogger("iot-sim.env-incident")

ENDPOINT = "/api/environmental-incidents"

# Backend giới hạn: notes ≤ 1000, reportedBy ≤ 256.
MAX_NOTES_CHARS = 1000
MAX_REPORTED_BY_CHARS = 256


class IncidentType(IntEnum):
    """`EnvironmentalIncidentTypeEnum` của backend."""

    SMOKE = 1
    FIRE_DETECTED = 2
    GAS_LEAK = 3
    FLOOD = 4
    OVERHEAT_HAZARD = 5
    OTHER = 99


class IncidentSeverity(IntEnum):
    """`AlertSeverityEnum` của backend."""

    INFO = 1
    WARNING = 2
    CRITICAL = 3


# Hằng phẳng giữ tương thích ngược cho test/script cũ.
INC_SMOKE = int(IncidentType.SMOKE)
INC_FIRE_DETECTED = int(IncidentType.FIRE_DETECTED)
INC_GAS_LEAK = int(IncidentType.GAS_LEAK)
INC_FLOOD = int(IncidentType.FLOOD)
INC_OVERHEAT_HAZARD = int(IncidentType.OVERHEAT_HAZARD)
INC_OTHER = int(IncidentType.OTHER)

SEV_INFO = int(IncidentSeverity.INFO)
SEV_WARNING = int(IncidentSeverity.WARNING)
SEV_CRITICAL = int(IncidentSeverity.CRITICAL)


class IncidentReportResult(Enum):
    """`sensor::IncidentReportResult` — quyết định giữ `pending` hay BỎ."""

    SUCCESS = 0
    TRANSIENT = 1    # thử lại sau (chưa provision, chưa có giờ, 5xx, lỗi mạng)
    PERMANENT = 2    # gửi lại vẫn hỏng (sai scope, payload sai) → dừng


@dataclass
class EnvironmentalIncident:
    """Bản ghi sự cố trước khi tuần tự hoá."""

    site_id_guid: str
    incident_type: int
    severity: int
    detected_at: str
    reported_by: str
    notes: str

    def to_payload(self) -> dict:
        """Đúng thứ tự trường của firmware; `notes` chỉ gửi khi có."""
        body: dict = {
            "siteId": self.site_id_guid,
            "incidentType": int(self.incident_type),
            "severity": int(self.severity),
            "reportedBy": self.reported_by[:MAX_REPORTED_BY_CHARS],
            "detectedAt": self.detected_at,
        }
        if self.notes:
            body["notes"] = self.notes[:MAX_NOTES_CHARS]
        return body


class EnvironmentalIncidentReporter:
    """`sensor::envIncidentReport` — reporter DÙNG CHUNG cho mọi cảm biến sự cố."""

    def __init__(self, http, device_code: str):
        self._http = http
        self.device_code = device_code
        self.site_id = ""
        self.report_ok_count = 0
        self.report_fail_count = 0
        self.dropped_count = 0

    def set_site_id(self, site_id_guid: str | None) -> None:
        """`envIncidentSetSiteId` — siteId đến từ provision response."""
        self.site_id = site_id_guid or ""
        if self.site_id:
            log.info("[%s] siteId set: %s", self.device_code, self.site_id)

    def report(self, incident_type: IncidentType | int, severity: IncidentSeverity | int,
               notes: str, detected_at_iso: str) -> IncidentReportResult:
        # Backend đòi `siteId` (Guid). Chưa provision xong thì gửi chắc chắn 400 → bỏ công vô ích,
        # nhưng là lỗi TẠM THỜI vì provision xong sẽ gửi được.
        if not self.site_id:
            log.info("[%s] siteId chưa có (provision chưa xong?) — hoãn báo sự cố",
                     self.device_code)
            self.report_fail_count += 1
            return IncidentReportResult.TRANSIENT
        if not detected_at_iso:
            self.report_fail_count += 1
            return IncidentReportResult.TRANSIENT

        incident = EnvironmentalIncident(
            site_id_guid=self.site_id,
            incident_type=int(incident_type),
            severity=int(severity),
            detected_at=detected_at_iso,
            reported_by=self.device_code,
            notes=notes or "",
        )
        res = self._http.environmental_incident(incident.to_payload())

        # 201 Created (mới) hoặc 200 OK (dedup dùng lại) đều là thành công.
        if res.ok:
            self.report_ok_count += 1
            log.warning("[%s] ĐÃ BÁO sự cố type=%d severity=%d (%d) [%dms]", self.device_code,
                        int(incident_type), int(severity), res.status_code, res.duration_ms)
            return IncidentReportResult.SUCCESS

        self.report_fail_count += 1
        # Dùng lại ĐÚNG bảng phân loại của đường telemetry (GH-741). Trước đây gộp mọi lỗi thành
        # "thử lại" nên một lỗi 403 sinh ~180 request/phút, vô hạn, không bao giờ tự khỏi.
        if is_transient_failure(res.status_code):
            log.warning("[%s] báo sự cố FAIL code=%d (tạm thời, sẽ thử lại) resp=%s",
                        self.device_code, res.status_code, res.body[:120])
            return IncidentReportResult.TRANSIENT

        self.dropped_count += 1
        log.error("[%s] BỎ báo sự cố code=%d (vĩnh viễn) resp=%s", self.device_code,
                  res.status_code, res.body[:120])
        if res.status_code in (401, 403):
            log.error("[%s]   → nhiều khả năng API key THIẾU scope EnvironmentalIngest (bitmask 4), "
                      "hoặc thiết bị đã bị revoke. EdgeDeviceDefault của backend KHÔNG có scope này.",
                      self.device_code)
        return IncidentReportResult.PERMANENT


class PendingReport:
    """Chốt sự cố đã phát hiện + cổng thử lại có backoff — dùng chung cho mọi cảm biến an toàn.

    Firmware lặp lại y hệt khối này trong `mq2.cpp` và `water_leak.cpp`; gom về một chỗ để ba
    cảm biến của simulator không thể trôi khỏi nhau.

    Hai bất biến quan trọng (GH-736 + GH-741):
      * Giữ mốc `detected_ms` của lúc PHÁT HIỆN, để `detectedAt` gửi lên backend là thời điểm
        thật chứ không phải thời điểm gửi được.
      * Sự cố MỚI phải được báo NGAY, không chờ hết backoff của lần trước — bắt một xung khí mới
        rồi đợi 5 phút vì lần trước backend lỗi là biến sự cố mạng thành sự cố an toàn.
    """

    def __init__(self, backoff):
        self.pending = False
        self.detected_ms = 0
        self.next_report_ms = 0
        self._backoff = backoff

    def arm(self, now_ms: int) -> None:
        self.pending = True
        self.detected_ms = now_ms
        self._backoff.reset()
        self.next_report_ms = now_ms   # báo ngay

    def on_success(self, now_ms: int) -> None:
        self.pending = False
        self._backoff.reset()
        self.next_report_ms = now_ms

    def on_permanent(self, now_ms: int) -> None:
        self.pending = False
        self._backoff.reset()
        self.next_report_ms = now_ms

    def on_transient(self, now_ms: int) -> int:
        wait_ms = self._backoff.record_failure(now_ms)
        self.next_report_ms = now_ms + wait_ms
        return wait_ms


# ─────────────────── Builder giữ cho test/script (không đổi contract) ───────────────────────
def make_gas_leak_incident(device_code: str, site_id_guid: str, detected_at: str,
                           adc_value: float, threshold: int = 2000,
                           gpio: int = 1) -> EnvironmentalIncident:
    """MQ-2 → `GasLeak (3)`.

    ⚠ ĐÂY LÀ ÁNH XẠ CỦA FIRMWARE, không phải `Smoke`: MQ-2 bản chất là cảm biến GAS
    (LPG/propane/methane/khói khí cháy). `Smoke (1)` được để dành cho cảm biến khói quang học
    sau này. Xem `sensor/mq2.cpp` — quyết định NS-24 (#664, E4, Q10=B).
    """
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_GAS_LEAK,
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes=f"MQ-2 raw={int(adc_value)} > thr={threshold} (GPIO{gpio})",
    )


def make_water_leak_incident(device_code: str, site_id_guid: str, detected_at: str,
                             gpio: int = 2) -> EnvironmentalIncident:
    """Cảm biến rò nước → `Flood (4)`."""
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_FLOOD,
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes=f"water leak GPIO{gpio}",
    )


def make_fire_incident(device_code: str, site_id_guid: str, detected_at: str,
                       temp_c: float) -> EnvironmentalIncident:
    """`FireDetected (2)` — PHẦN MỞ RỘNG RIÊNG CỦA SIMULATOR.

    Firmware hiện KHÔNG có đường báo cháy (chỉ MQ-2→GasLeak và rò nước→Flood). Giữ lại vì enum
    này có thật ở backend và rất hữu ích để demo luồng cảnh báo, nhưng phải hiểu rõ: thiết bị thật
    hôm nay KHÔNG phát ra loại sự cố này.
    """
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_FIRE_DETECTED,
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes=f"MQ-2 vượt ngưỡng đồng thời nhiệt pin {temp_c:.1f}°C (mô phỏng)",
    )
