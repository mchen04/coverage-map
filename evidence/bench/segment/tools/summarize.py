"""Summarize S2 segmentation bench rounds: per-case/config medians, gate, bit-identity vs production seg."""
import json, statistics, sys, os
from pathlib import Path
sys.path.insert(0, os.getcwd())
from covmap.paths import case_dir
ev = Path("evidence/bench/segment")  # the synced checkout copy (authoritative)
rounds = sys.argv[1:] or sorted(p.name.split("-")[1] for p in ev.glob("round-*"))
prod = {}
for case in ("lidc-0001", "pancreas-0051"):
    rec = json.loads((case_dir(case) / "seg" / "segment.json").read_text())
    prod[case] = rec["hashes_sha256"]
out = {}
for r in rounds:
    rows = {}
    for f in sorted((ev / f"round-{r}").glob("*.json")):
        if f.name.startswith("summary"):
            continue
        d = json.loads(f.read_text())
        label = f.stem.rsplit("-r", 1)[0][len(d["case"]) + 1:]
        seg = d.get("segment", {})
        ts = seg.get("totalsegmentator", {}).get("wall_s") or seg.get("timings_s", {}).get("totalsegmentator_s")
        c = d.get("correctness", {})
        h = seg.get("hashes_sha256", {})
        row = rows.setdefault((d["case"], label), {"wall_s": [], "ts_s": [], "pass": [], "dice_min": [], "dice_mean": [],
                                                    "labels_identical": [], "totalseg_identical": [], "load1": [], "files": []})
        row["wall_s"].append(d["wall_s"]); row["ts_s"].append(round(ts, 2) if ts else None)
        row["pass"].append(c.get("pass")); row["dice_min"].append(c.get("dice_min")); row["dice_mean"].append(c.get("dice_mean"))
        row["labels_identical"].append(h.get("labels.npy") == prod[d["case"]]["labels.npy"])
        row["totalseg_identical"].append(h.get("totalseg.nii.gz") == prod[d["case"]]["totalseg.nii.gz"])
        row["load1"].append(d["context"]["loadavg_1_5_15"][0]); row["files"].append(f.name)
        conv = seg.get("totalsegmentator", {}).get("conv3d", {})
        row["conv3d"] = {k: conv.get(k) for k in ("mode", "device", "budget_mb", "source")}
        row["torch_threads"] = seg.get("totalsegmentator", {}).get("torch_threads")
        # main-process peak RSS excludes MPS (GPU) memory; the driver allocation is read after the run (a lower bound)
        for key, src in (("rss_gb", "process_max_rss_gb"), ("mps_driver_gb", "mps_driver_allocated_gb")):
            v = seg.get("totalsegmentator", {}).get(src)
            if v is not None:
                row[key] = max(row.get(key, 0.0), v)
    summ = {}
    for (case, label), row in sorted(rows.items()):
        ts = [x for x in row["ts_s"] if x]
        summ[f"{case}/{label}"] = {**row, "n": len(row["wall_s"]), "median_wall_s": statistics.median(row["wall_s"]),
                                   "median_ts_s": statistics.median(ts) if ts else None}
        print(f"round {r} {case:14s} {label:12s} n={len(row['wall_s'])} wall med {statistics.median(row['wall_s']):7.1f} "
              f"runs {row['wall_s']} ts med {summ[f'{case}/{label}']['median_ts_s']} pass {row['pass']} "
              f"dice_min {row['dice_min']} mean {row['dice_mean']} labels_identical {row['labels_identical']} "
              f"load1 {row['load1']} conv {row['conv3d']} thr {row['torch_threads']} rss {row.get('rss_gb')} "
              f"mps {row.get('mps_driver_gb')}", file=sys.stderr)
    out[r] = {"round": r, "production_hashes": prod, "rows": summ}
json.dump(out, sys.stdout, indent=1)
