"""Pi system stats: vcgencmd thermals/throttling, load, uptime.

Returns {"on_pi": false, ...nulls} on hosts without vcgencmd.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _vcgencmd(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["vcgencmd", *args], capture_output=True, text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _parse_temp(raw: str | None) -> float | None:
    # "temp=47.3'C"
    if raw and raw.startswith("temp="):
        try:
            return float(raw.removeprefix("temp=").removesuffix("'C"))
        except ValueError:
            return None
    return None


def get_stats() -> dict:
    temp_raw = _vcgencmd("measure_temp")
    throttled_raw = _vcgencmd("get_throttled")  # "throttled=0x50000"
    throttled_hex = None
    throttled = None
    if throttled_raw and "=" in throttled_raw:
        throttled_hex = throttled_raw.split("=", 1)[1]
        try:
            throttled = int(throttled_hex, 16) != 0
        except ValueError:
            pass

    load1 = None
    try:
        load1 = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass

    uptime_s = None
    try:
        uptime_s = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    return {
        "on_pi": temp_raw is not None,
        "temp_c": _parse_temp(temp_raw),
        "throttled": throttled,
        "throttled_hex": throttled_hex,
        "load1": load1,
        "uptime_s": uptime_s,
    }
