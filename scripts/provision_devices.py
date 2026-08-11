"""Helper script: gọi admin endpoint backend để tạo IotDevice + Battery seed + lấy raw API key.

Chạy LẦN ĐẦU khi setup môi trường dev — sau đó copy `rawApiKey` trả về vào `config/seed.yaml`
hoặc env `IOT_API_KEY` (backend chỉ trả 1 lần, không lấy lại được — xem NI §7.2).

Usage:
  export ADMIN_TOKEN=<JWT của admin>
  python scripts/provision_devices.py --base-url https://localhost:7200 --insecure

Script đọc devices từ seed.yaml (chỉ field device_code, model, site_id, batteries) → tạo mới
hoặc skip nếu đã tồn tại. Output: bảng device_code | rawApiKey.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
import urllib3
import yaml


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision IoT devices vào backend")
    p.add_argument("--base-url", default=os.getenv("IOT_BASE_URL", "https://localhost:7200"))
    p.add_argument("--seed", default="config/seed.yaml")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN", ""))
    p.add_argument("--admin-path", default="/api/admin/iot-devices",
                   help="route admin tạo device; đổi nếu backend của bạn có tiền tố khác "
                        "(vd /api/v1/admin/iot-devices)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.admin_token:
        print("[ERR] Thiếu --admin-token hoặc env ADMIN_TOKEN (JWT admin)", file=sys.stderr)
        return 2

    seed_path = Path(args.seed)
    if not seed_path.exists():
        print(f"[ERR] Không tìm thấy seed file: {seed_path}", file=sys.stderr)
        return 2

    raw = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    devices = raw.get("devices", [])
    if not devices:
        print("[ERR] Seed YAML không có device nào", file=sys.stderr)
        return 2

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = requests.Session()
    session.verify = not args.insecure
    session.headers.update({
        "Authorization": f"Bearer {args.admin_token}",
        "Content-Type": "application/json",
    })

    print("⚠ SCOPE: key cấp cho thiết bị PHẢI có `EnvironmentalIngest` (bitmask 4).")
    print("  `EdgeDeviceDefault` của backend = SensorIngest|DeviceHeartbeat|FirmwareCheck = 11,")
    print("  KHÔNG có 4 ⇒ ambient (SHT31) và environmental-incident (MQ-2 / rò nước) sẽ trả 403,")
    print("  và 403 là lỗi vĩnh viễn nên thiết bị BỎ luôn. Cấp scope tổng = 15.\n")
    print(f"{'DeviceCode':<24} {'Status':<10} RawApiKey (copy ngay — chỉ hiện 1 lần)")
    print("-" * 100)
    for d in devices:
        code = d["device_code"]
        # Schema khớp CreateIotDeviceCommand (BatteryService.Application).
        # Validation backend: deviceCode 3-64 chars [A-Z0-9-]+, displayName ≤200, siteId Guid,
        # hardwareRevision ≤64, heartbeatIntervalSeconds 10-3600.
        body = {
            "deviceCode": code,
            "displayName": f"{code} (simulator)",
            "siteId": d.get("site_id_guid"),
            "hardwareRevision": d.get("hardware_revision", "ESP32-S3-DevKitC-1")[:64],
            "heartbeatIntervalSeconds": 60,
            "notes": "Created by iot-simulator/scripts/provision_devices.py",
        }
        try:
            r = session.post(f"{args.base_url.rstrip('/')}{args.admin_path}",
                             json=body, timeout=15)
        except requests.RequestException as ex:
            print(f"{code:<24} NETERR    {ex}")
            continue
        if r.status_code in (200, 201):
            try:
                data = r.json().get("data") or r.json()
                key = data.get("rawApiKey") or data.get("RawApiKey", "(missing)")
                print(f"{code:<24} CREATED    {key}")
            except ValueError:
                print(f"{code:<24} ?          {r.text[:80]}")
        elif r.status_code == 409:
            print(f"{code:<24} EXISTS     (đã tồn tại — dùng rotate-key nếu cần)")
        else:
            print(f"{code:<24} {r.status_code:<10} {r.text[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
