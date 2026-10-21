"""3D organ-map render timing via the CLI (`covmap render`, headless Chromium + vtk.js).

    python bench/bench_render.py --cases lidc-0001 pancreas-0051 --runs 3 --out bench.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.common import host_context, stats, write  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    work = Path(a.out).parent / "render-work"
    work.mkdir(parents=True, exist_ok=True)
    res = {"context": host_context(), "cases": {}}
    for case in a.cases:
        walls, inner, shas = [], [], set()
        for i in range(a.runs):
            png = work / f"{case}-{i}.png"
            t0 = time.perf_counter()
            p = subprocess.run([sys.executable, "-m", "covmap", "render", "--case", case, "--out", str(png)],
                               capture_output=True, text=True)
            walls.append(time.perf_counter() - t0)
            r = json.loads(p.stdout)
            if p.returncode != 0 or not r.get("ok"):
                raise SystemExit(f"render failed: {p.stdout[-1500:]} {p.stderr[-1500:]}")
            inner.append(r.get("timings_ms", {}))
            shas.add(r.get("sha256"))
        res["cases"][case] = {"wall_s": stats(walls), "timings_ms": inner, "distinct_png_sha256": sorted(shas),
                              "webgl": r.get("webgl_renderer")}
    res["context_end"] = host_context()
    write(Path(a.out), res)
    print(json.dumps({"ok": True, "out": a.out,
                      "wall_median_s": {c: v["wall_s"]["median"] for c, v in res["cases"].items()}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
