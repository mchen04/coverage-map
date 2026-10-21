"""Shared benchmark plumbing: host context, timing statistics, evidence output."""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path


def host_context() -> dict:
    load = os.getloadavg()
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    except OSError:
        cpu = platform.processor()
    return {"host": platform.node(), "cpu": cpu, "cores": os.cpu_count(), "macos": platform.mac_ver()[0],
            "loadavg_1_5_15": [round(x, 2) for x in load], "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def stats(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"n": len(xs), "median": round(statistics.median(xs), 4), "min": round(xs[0], 4),
            "max": round(xs[-1], 4), "runs": [round(x, 4) for x in xs]}


def write(out: Path, obj: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, indent=1, default=str))
