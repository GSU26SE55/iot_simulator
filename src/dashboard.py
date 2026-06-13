"""Live CLI dashboard với rich. Hiển thị state mọi device + counters."""
from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .device import SimulatedDevice


def _status_color(status: str) -> str:
    return {
        "provisioning": "yellow",
        "online":       "green",
        "offline":      "red",
        "halted":       "bright_black",
    }.get(status, "white")


def _scenario_color(scenario: str) -> str:
    if scenario == "normal":
        return "green"
    return "magenta"


def render_table(devices: list[SimulatedDevice]) -> Table:
    t = Table(title="IoT Simulator — Live State", expand=True)
    t.add_column("Device", style="cyan", no_wrap=True)
    t.add_column("Status")
    t.add_column("Scenario")
    t.add_column("Batt", justify="right")
    t.add_column("V", justify="right")
    t.add_column("T°C", justify="right")
    t.add_column("SOC%", justify="right")
    t.add_column("Sent", justify="right", style="green")
    t.add_column("Fail", justify="right", style="red")
    t.add_column("Queue", justify="right", style="yellow")
    t.add_column("Amb", justify="right")
    t.add_column("Inc", justify="right")
    t.add_column("Backoff", justify="right")
    t.add_column("Last seen", style="dim")
    t.add_column("Last error", style="red")

    for d in devices:
        s = d.state
        t.add_row(
            d.cfg.device_code,
            f"[{_status_color(s.status)}]{s.status}[/]",
            f"[{_scenario_color(s.scenario)}]{s.scenario}[/]",
            str(len(d.cfg.batteries)),
            f"{s.last_voltage:.2f}",
            f"{s.last_temperature:.1f}",
            f"{s.last_soc:.1f}",
            str(s.sent_batches),
            str(s.failed_batches),
            str(s.queued_batches),
            str(s.ambient_sent),
            str(s.incidents_sent),
            f"{s.backoff_s:.0f}s" if s.backoff_s else "-",
            s.last_seen.split("T")[-1][:8] if s.last_seen else "-",
            (s.last_error[:60] + "…") if len(s.last_error) > 60 else s.last_error,
        )
    return t


def run_dashboard(devices: list[SimulatedDevice], stop_event) -> None:
    console = Console()
    with Live(render_table(devices), console=console, refresh_per_second=2) as live:
        while not stop_event.is_set():
            live.update(render_table(devices))
            time.sleep(0.5)
