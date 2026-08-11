"""Bảng trạng thái trực tiếp trên terminal (rich) — hiển thị mọi thiết bị + bộ đếm.

Cột đèn dùng ĐÚNG bảng màu + kiểu nháy của firmware (`ui/led_palette.h`), kể cả nhấp nháy, để
người demo nhìn dashboard là biết đèn trên board thật đang màu gì.
"""
from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .device import SimulatedDevice
from .led import LABELS, LedState, is_lit
from .timeutil import monotonic_ms

_STATUS_COLOR = {
    "booting": "cyan",
    "provisioning": "yellow",
    "connecting": "yellow",
    "online": "green",
    "offline": "red",
    "halted": "bright_black",
}

_LED_COLOR = {
    LedState.ONLINE: "green",
    LedState.QUEUED: "green",
    LedState.OFFLINE: "red",
    LedState.PROVISIONING: "magenta",
    LedState.SETUP: "magenta",
    LedState.WIFI_SEARCHING: "dark_orange",
    LedState.RECOVERY: "magenta",
    LedState.OFF: "white",
}


def _render_led(state: LedState, now_ms: int) -> str:
    color = _LED_COLOR.get(state, "white")
    glyph = "●" if is_lit(state, now_ms) else "○"
    return f"[{color}]{glyph}[/] {LABELS.get(state, '?')}"


def render_table(devices: list[SimulatedDevice]) -> Table:
    now_ms = monotonic_ms()
    t = Table(title="IoT Simulator — trạng thái trực tiếp", expand=True)
    t.add_column("Device", style="cyan", no_wrap=True)
    t.add_column("LED", no_wrap=True)
    t.add_column("Status")
    t.add_column("Scenario")
    t.add_column("FW", style="dim", no_wrap=True)
    t.add_column("OTA", justify="right", style="blue")
    t.add_column("MQTT", justify="center")
    t.add_column("Pin", justify="right")
    t.add_column("V", justify="right")
    t.add_column("T°C", justify="right")
    t.add_column("SOC%", justify="right")
    t.add_column("Sent", justify="right", style="green")
    t.add_column("Fail", justify="right", style="red")
    t.add_column("Queue", justify="right", style="yellow")
    t.add_column("Drop", justify="right", style="red")
    t.add_column("Part", justify="right", style="yellow")
    t.add_column("HB", justify="right")
    t.add_column("Amb", justify="right")
    t.add_column("Inc", justify="right")
    t.add_column("Cmd", justify="right")
    t.add_column("Backoff", justify="right")
    t.add_column("Last seen", style="dim")
    t.add_column("Last error", style="red")

    for d in devices:
        s = d.state
        mqtt_cell = ("[green]up[/]" if s.mqtt_connected
                     else ("[red]down[/]" if d.mqtt_cfg.enabled else "[dim]off[/]"))
        t.add_row(
            d.cfg.device_code,
            _render_led(s.led, now_ms),
            f"[{_STATUS_COLOR.get(s.status, 'white')}]{s.status}[/]",
            f"[{'green' if s.scenario == 'normal' else 'magenta'}]{s.scenario}[/]",
            s.firmware_version,
            f"{s.ota_updates}/{s.ota_checks}"
            + (f" rb{s.ota_rollbacks}" if s.ota_rollbacks else ""),
            mqtt_cell,
            str(s.battery_count),
            f"{s.last_voltage:.2f}",
            f"{s.last_temperature:.1f}",
            f"{s.last_soc:.1f}",
            str(s.sent_batches),
            str(s.failed_batches),
            str(s.queued_batches),
            str(s.dropped_batches),
            str(s.partial_ingests),
            f"{s.heartbeat_ok}/{s.heartbeat_ok + s.heartbeat_fail}",
            str(s.ambient_sent),
            str(s.incidents_sent),
            f"{s.cmd_ack_ok}/{s.cmd_received}",
            f"{s.backoff_s:.0f}s" if s.backoff_s else "-",
            s.last_seen.split("T")[-1][:8] if s.last_seen else "-",
            (s.last_error[:58] + "…") if len(s.last_error) > 58 else s.last_error,
        )
    return t


def run_dashboard(devices: list[SimulatedDevice], stop_event) -> None:
    console = Console()
    with Live(render_table(devices), console=console, refresh_per_second=2) as live:
        while not stop_event.is_set():
            live.update(render_table(devices))
            time.sleep(0.5)
