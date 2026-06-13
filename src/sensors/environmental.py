"""MQ-2 (smoke), MQ-135 (gas), DS18B20 spike (fire detect), water leak — POST /api/environmental-incidents.

Payload đúng theo api-battery.md §POST /api/environmental-incidents:
  {
    "siteId":      "<Guid>",
    "incidentType": <int 1..5,99>,
    "severity":     <int 1..3>,        # mặc định 3 Critical nếu không truyền
    "detectedAt":   "ISO8601 UTC ≤ now + 5min",
    "reportedBy":   "ESP32-SIM-001",   # device code (≤ 256 chars)
    "notes":        "...mô tả..."      # ≤ 1000 chars
  }

Auth: API Key + scope EnvironmentalIngest.
"""
from __future__ import annotations

from dataclasses import dataclass

# EnvironmentalIncidentTypeEnum (api-battery.md §enums)
INC_SMOKE = 1
INC_FIRE_DETECTED = 2
INC_GAS_LEAK = 3
INC_FLOOD = 4
INC_OVERHEAT_HAZARD = 5
INC_OTHER = 99

# AlertSeverityEnum
SEV_INFO = 1
SEV_WARNING = 2
SEV_CRITICAL = 3


@dataclass
class EnvironmentalIncident:
    site_id_guid: str
    incident_type: int
    severity: int
    detected_at: str
    reported_by: str
    notes: str


def make_smoke_incident(device_code: str, site_id_guid: str, detected_at: str, adc_value: float) -> EnvironmentalIncident:
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_SMOKE,
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes=f"MQ-2 smoke ADC={adc_value:.0f} vượt threshold 2500 (cabinet B)",
    )


def make_fire_incident(device_code: str, site_id_guid: str, detected_at: str, temp_c: float) -> EnvironmentalIncident:
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_FIRE_DETECTED,
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes=f"DS18B20 + MQ-2 đồng thời trigger (T={temp_c:.1f}°C, smoke)",
    )


def make_gas_leak_incident(device_code: str, site_id_guid: str, detected_at: str, adc_value: float) -> EnvironmentalIncident:
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_GAS_LEAK,
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes=f"MQ-135 gas sensor ADC={adc_value:.0f} vượt threshold",
    )


def make_water_leak_incident(device_code: str, site_id_guid: str, detected_at: str) -> EnvironmentalIncident:
    return EnvironmentalIncident(
        site_id_guid=site_id_guid,
        incident_type=INC_FLOOD,                                       # backend enum: Flood=4
        severity=SEV_CRITICAL,
        detected_at=detected_at,
        reported_by=device_code,
        notes="Water leak sensor GPIO2 toggled HIGH — phát hiện nước sàn cabinet",
    )
