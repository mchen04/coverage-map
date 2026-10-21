"""Segmentation timing with a correctness gate.

Runs `covmap segment` in a fresh process into seg dir `<out-name>` (never the production
`seg`), reads its timing record, and compares labels with the reference segmentation
(per-organ Dice). A configuration only counts as a gain when it is faster AND keeps the
same organ set with per-organ Dice >= --min-dice (default 0.90) and mean Dice >= 0.95.

    python bench/bench_segment.py --case lidc-0001 --out-name seg-bench-mps --device mps \
        --ref seg --out bench.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from bench.common import host_context, write  # noqa: E402
from covmap import paths  # noqa: E402


def dice(a: np.ndarray, b: np.ndarray, ids: list[int]) -> dict[int, float]:
    out = {}
    for i in ids:
        x, y = a == i, b == i
        s = int(x.sum()) + int(y.sum())
        out[i] = 1.0 if s == 0 else round(2.0 * int((x & y).sum()) / s, 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out-name", required=True)
    ap.add_argument("--ref", default="seg")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--roi-subset", action="store_true")
    ap.add_argument("--threads", type=int)
    ap.add_argument("--min-dice", type=float, default=0.90)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.out_name == "seg":
        raise SystemExit("refusing to overwrite the production segmentation")

    cmd = [sys.executable, "-m", "covmap", "segment", "--case", a.case, "--device", a.device,
           "--out-name", a.out_name]
    if a.fast:
        cmd.append("--fast")
    if a.roi_subset:
        cmd.append("--roi-subset")
    if a.threads:
        cmd += ["--threads", str(a.threads)]
    ctx = host_context()
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    res = {"case": a.case, "cmd": " ".join(cmd), "exit": p.returncode, "wall_s": round(wall, 2),
           "context": ctx, "context_end": host_context()}
    try:
        res["segment"] = json.loads(p.stdout)
    except json.JSONDecodeError:
        res["stdout_tail"] = p.stdout[-2000:]
        res["stderr_tail"] = p.stderr[-2000:]
    if p.returncode == 0:
        cdir = paths.case_dir(a.case)
        ref = np.load(cdir / a.ref / "labels.npy")
        new = np.load(cdir / a.out_name / "labels.npy")
        ref_org = {o["id"]: o["key"] for o in json.loads((cdir / a.ref / "organs.json").read_text())["organs"]}
        new_org = {o["id"]: o["key"] for o in json.loads((cdir / a.out_name / "organs.json").read_text())["organs"]}
        d = dice(ref, new, sorted(ref_org))
        per = {ref_org[i]: v for i, v in d.items()}
        mean = round(float(np.mean(list(per.values()))), 4) if per else None
        res["correctness"] = {
            "same_organ_set": set(ref_org.values()) == set(new_org.values()),
            "missing": sorted(set(ref_org.values()) - set(new_org.values())),
            "extra": sorted(set(new_org.values()) - set(ref_org.values())),
            "dice": per, "dice_mean": mean, "dice_min": min(per.values()) if per else None,
        }
        c = res["correctness"]
        c["pass"] = bool(c["same_organ_set"] and c["dice_min"] is not None
                         and c["dice_min"] >= a.min_dice and mean >= 0.95) if a.out_name != a.ref else True
    write(Path(a.out), res)
    print(json.dumps({"ok": p.returncode == 0, "out": a.out, "wall_s": res["wall_s"],
                      "totalsegmentator_s": res.get("segment", {}).get("timings_s", {}).get("totalsegmentator"),
                      "correctness_pass": res.get("correctness", {}).get("pass"),
                      "dice_min": res.get("correctness", {}).get("dice_min")}))
    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
