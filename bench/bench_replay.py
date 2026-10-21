"""Replay/scoring timing with a correctness fingerprint.

For each case and scenario it writes the simulated event log once, then measures:
  cli_cold_s    `python -m covmap replay` in a fresh process (imports + case load + scoring)
  warm_ms       in-process replay_file(), case already cached, median of --reps
  live_post_ms  per-POST scoring cost: events fed in 400 ms batches to one Scorer with
                provisional scoring at each batch (what the server does for the live panel)
The fingerprint (per-organ status + coverage rounded to 1e-6) must be identical between
rounds for a speed change to count.

    python bench/bench_replay.py --cases lidc-0001 pancreas-0051 --out bench.json [--reps 5]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.common import host_context, stats, write  # noqa: E402


def fingerprint(cov: dict) -> tuple[str, dict]:
    fp = {o["key"]: [o["status"], round(o["coverage"], 6)] for o in cov["organs"]}
    return hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:16], fp


def live_batches(events: list[dict], batch_ms: float = 400.0) -> list[tuple[list[dict], float]]:
    out, cur, edge = [], [], None
    for e in events:
        if edge is None:
            edge = e["t_ms"] + batch_ms
        while e["t_ms"] >= edge:
            out.append((cur, edge))
            cur, edge = [], edge + batch_ms
        cur.append(e)
    if cur:
        out.append((cur, edge))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--scenarios", nargs="*", default=["full_read", "window_skip_lung", "region_skip_left_lung"])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--work", default=None, help="where event logs go (default: next to --out)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from covmap.scoring import Scorer, replay_file
    from covmap.simulate import list_scenarios, simulate

    work = Path(a.work or Path(a.out).parent / "replay-work")
    work.mkdir(parents=True, exist_ok=True)
    res = {"context": host_context(), "cases": {}}
    for case in a.cases:
        applicable = {s["name"] for s in list_scenarios(case)["scenarios"] if s.get("applicable", True)}
        cres = {}
        for sc in a.scenarios:
            if sc not in applicable:
                continue
            ev = work / f"{case}.{sc}.jsonl"
            simulate(case, sc, out=str(ev))
            events = [json.loads(line) for line in ev.read_text().splitlines() if line.strip()]
            t0 = time.perf_counter()
            p = subprocess.run([sys.executable, "-m", "covmap", "replay", "--case", case, "--events", str(ev)],
                               capture_output=True, text=True)
            cold = time.perf_counter() - t0
            cov = json.loads(p.stdout)
            fp, fp_full = fingerprint(cov)
            replay_file(case, str(ev))  # warm the case cache
            warm = []
            for _ in range(a.reps):
                t = time.perf_counter()
                c2 = replay_file(case, str(ev))
                warm.append((time.perf_counter() - t) * 1000)
            assert fingerprint(c2)[0] == fp, "warm replay differs from CLI replay"
            sc_obj = Scorer(case)
            post = []
            for batch, now in live_batches(events):
                t = time.perf_counter()
                sc_obj.add(batch, now_ms=now)
                post.append((time.perf_counter() - t) * 1000)
            cres[sc] = {"events": len(events), "cli_exit": p.returncode, "cli_cold_s": round(cold, 3),
                        "warm_ms": stats(warm), "live_posts": len(post),
                        "live_post_ms": {"median": stats(post)["median"], "p95": sorted(post)[int(0.95 * (len(post) - 1))],
                                         "max": max(post)},
                        "fingerprint": fp, "statuses": {k: v[0] for k, v in fp_full.items()}}
        res["cases"][case] = cres
    res["context_end"] = host_context()
    write(Path(a.out), res)
    print(json.dumps({"ok": True, "out": a.out, "summary": {
        c: {s: [v["cli_cold_s"], v["warm_ms"]["median"], v["live_post_ms"]["p95"], v["fingerprint"]]
            for s, v in r.items()} for c, r in res["cases"].items()}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
