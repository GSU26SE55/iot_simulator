"""CLI — IoT Simulator.

    python -m src.main                            # đọc config/seed.yaml + env
    python -m src.main --seed config/my.yaml
    python -m src.main --no-dashboard             # chỉ log, không bảng trạng thái
    python -m src.main --once                     # gửi 1 batch rồi thoát (smoke test)
    python -m src.main --scenario overheat        # ép scenario cho TẤT CẢ thiết bị
    python -m src.main --device ESP32-SIM-001     # chỉ chạy 1 thiết bị (lặp lại được)
    python -m src.main --clear-state              # xoá state bền vững (như lệnh `clear` trên CLI
                                                  #   Serial của firmware: erase NVS) rồi chạy

Env (xem env.example.txt): IOT_BASE_URL, IOT_TLS_VERIFY, IOT_API_KEY, IOT_CONTRACT_VERSION,
IOT_MQTT_*, IOT_SEED_FILE, IOT_QUEUE_DIR, IOT_STATE_DIR, IOT_PERSIST_STATE, IOT_LOG_LEVEL.
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
except ImportError:  # pragma: no cover - phụ thuộc môi trường
    pass

from .config import CONTRACT_IOT2, load_config
from .control_server import start_control_server
from .dashboard import run_dashboard
from .device import SimulatedDevice


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="iot-simulator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", default=None, help="đường dẫn seed YAML (mặc định config/seed.yaml)")
    p.add_argument("--no-dashboard", action="store_true", help="tắt bảng trạng thái rich")
    p.add_argument("--once", action="store_true", help="gửi 1 batch rồi thoát")
    p.add_argument("--scenario", default=None,
                   help="ép scenario cho mọi thiết bị (overheat|low_soc|sensor_mismatch|gas_leak|…)")
    p.add_argument("--device", action="append", default=None,
                   help="chỉ chạy thiết bị có mã này (lặp lại được)")
    p.add_argument("--log-file", default=None, help="ghi log ra file thay vì stderr")
    p.add_argument("--clear-state", action="store_true",
                   help="xoá state bền vững của thiết bị (provision, credential MQTT, bảng pin, "
                        "trạng thái OTA) trước khi chạy — tương đương `clear` trên Serial CLI")
    p.add_argument("--no-persist", action="store_true",
                   help="không ghi state ra đĩa (mỗi lần chạy là một thiết bị mới tinh)")
    p.add_argument("--control-port", type=int, default=8787,
                   help="cổng HTTP control server cục bộ để chỉnh thông số lúc đang chạy "
                        "(dùng với control.html); 0 = tắt (mặc định 8787)")
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


def _clear_state(state_dir: Path, device_codes: list[str], log: logging.Logger) -> None:
    for code in device_codes:
        path = state_dir / f"{code}.nvs.json"
        if path.exists():
            path.unlink()
            log.info("đã xoá state: %s", path)


def main() -> int:
    args = parse_args()
    cfg = load_config(args.seed)
    setup_logging(cfg.log_level, args.log_file)
    log = logging.getLogger("iot-sim")

    devices_cfg = cfg.devices
    if args.device:
        wanted = set(args.device)
        devices_cfg = [d for d in devices_cfg if d.device_code in wanted]
        if not devices_cfg:
            log.error("Không tìm thấy thiết bị nào khớp --device %s", args.device)
            return 2
    if args.scenario:
        for d in devices_cfg:
            d.scenario = args.scenario

    state_dir = Path(cfg.state_dir)
    if args.clear_state:
        _clear_state(state_dir, [d.device_code for d in devices_cfg], log)

    persist = cfg.persist_state and not args.no_persist

    production = cfg.backend.contract_version == CONTRACT_IOT2
    log.info("Khởi động %d thiết bị · backend=%s · contract=%s · mqtt=%s · persist_state=%s",
             len(devices_cfg), cfg.backend.base_url, cfg.backend.contract_version,
             cfg.mqtt.enabled, persist)
    if not production:
        log.warning("contract_version='current' — đây là contract Sprint 1 của backend đời cũ. "
                    "Provision / heartbeat / MQTT / ambient / environmental-incident / OTA đều "
                    "TẮT vì backend đời đó không có. Firmware thật chỉ chạy 'iot2-production'.")

    devices = [
        SimulatedDevice(dev_cfg=d, backend_cfg=cfg.backend, mqtt_cfg=cfg.mqtt,
                        queue_dir=Path(cfg.queue_dir), state_dir=state_dir,
                        persist_state=persist, battery_catalog=cfg.battery_catalog)
        for d in devices_cfg
    ]

    stop_event = threading.Event()

    def _on_sigint(_signum, _frame):
        log.info("Nhận tín hiệu dừng…")
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    for d in devices:
        d.start()

    if args.control_port:
        try:
            start_control_server(devices, port=args.control_port)
            log.info("control server sẵn sàng — mở tools/control.html để chỉnh thông số")
        except OSError as ex:
            log.warning("không khởi động được control server tại cổng %d: %s",
                        args.control_port, ex)

    if args.once:
        deadline = time.time() + 60
        while time.time() < deadline and not stop_event.is_set():
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

    log.info("Xong. Tổng: sent=%d fail=%d queue=%d drop=%d partial=%d hb=%d amb=%d inc=%d "
             "ota_ok=%d ota_rb=%d",
             sum(d.state.sent_batches for d in devices),
             sum(d.state.failed_batches for d in devices),
             sum(d.state.queued_batches for d in devices),
             sum(d.state.dropped_batches for d in devices),
             sum(d.state.partial_ingests for d in devices),
             sum(d.state.heartbeat_ok for d in devices),
             sum(d.state.ambient_sent for d in devices),
             sum(d.state.incidents_sent for d in devices),
             sum(d.state.ota_updates for d in devices),
             sum(d.state.ota_rollbacks for d in devices))
    return 0


if __name__ == "__main__":
    sys.exit(main())
