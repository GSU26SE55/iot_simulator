"""CLI entry — IoT Simulator.

Usage:
  python -m src.main                          # đọc config/seed.yaml + env
  python -m src.main --seed config/my.yaml
  python -m src.main --no-dashboard           # log thường thôi, không dashboard
  python -m src.main --once                   # gửi 1 batch rồi thoát (smoke test)
  python -m src.main --scenario overheat      # override scenario cho TẤT CẢ devices
  python -m src.main --device ESP32-SIM-001   # chỉ chạy 1 device

Env (xem env.example.txt):
  IOT_BASE_URL, IOT_TLS_VERIFY, IOT_API_KEY, IOT_MQTT_*, IOT_SEED_FILE, IOT_QUEUE_DIR, IOT_LOG_LEVEL
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .config import load_config
from .dashboard import run_dashboard
from .device import SimulatedDevice


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="iot-simulator", description=__doc__)
    p.add_argument("--seed", default=None, help="đường dẫn seed YAML (mặc định config/seed.yaml)")
    p.add_argument("--no-dashboard", action="store_true", help="tắt rich dashboard")
    p.add_argument("--once", action="store_true", help="gửi 1 batch + thoát")
    p.add_argument("--scenario", default=None, help="ép scenario cho mọi device (overheat|low_soc|sensor_mismatch|smoke|...)")
    p.add_argument("--device", action="append", default=None, help="chỉ chạy device code này (có thể lặp lại)")
    p.add_argument("--log-file", default=None, help="ghi log ra file thay vì stderr")
    return p.parse_args()


def setup_logging(level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = []
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=handlers,
    )


def main() -> int:
    args = parse_args()
    cfg = load_config(args.seed)
    setup_logging(cfg.log_level, args.log_file)
    log = logging.getLogger("iot-sim")

    # Filter devices
    devices_cfg = cfg.devices
    if args.device:
        wanted = set(args.device)
        devices_cfg = [d for d in devices_cfg if d.device_code in wanted]
        if not devices_cfg:
            log.error("Không tìm thấy device nào khớp --device %s", args.device)
            return 2
    if args.scenario:
        for d in devices_cfg:
            d.scenario = args.scenario

    log.info("Khởi động %d device, backend=%s, mqtt=%s",
             len(devices_cfg), cfg.backend.base_url, cfg.mqtt.enabled)

    devices = [
        SimulatedDevice(dev_cfg=d, backend_cfg=cfg.backend, mqtt_cfg=cfg.mqtt, queue_dir=Path(cfg.queue_dir))
        for d in devices_cfg
    ]

    stop_event = threading.Event()

    def _on_sigint(_signum, _frame):
        log.info("Nhận SIGINT, dừng…")
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    for d in devices:
        d.start()

    if args.once:
        # Đợi mỗi device gửi ít nhất 1 batch hoặc fail rõ ràng
        deadline = time.time() + 30
        while time.time() < deadline:
            if all(d.state.sent_batches >= 1 or d.state.failed_batches >= 1 for d in devices):
                break
            time.sleep(0.5)
        stop_event.set()
    elif args.no_dashboard:
        while not stop_event.is_set():
            time.sleep(1.0)
    else:
        try:
            run_dashboard(devices, stop_event)
        except KeyboardInterrupt:
            stop_event.set()

    for d in devices:
        d.stop()
    for d in devices:
        d.join(timeout=5)

    log.info("Done. Tổng: sent=%d fail=%d queue=%d ambient=%d incidents=%d",
             sum(d.state.sent_batches for d in devices),
             sum(d.state.failed_batches for d in devices),
             sum(d.state.queued_batches for d in devices),
             sum(d.state.ambient_sent for d in devices),
             sum(d.state.incidents_sent for d in devices))
    return 0


if __name__ == "__main__":
    sys.exit(main())
