"""Replay/scoring timing with a correctness fingerprint.

For each case and scenario it writes the simulated event log once, then measures:
  cli_cold_s    `python -m covmap replay` in a fresh process (imports + case load + scoring)
  warm_ms       in-process replay_file(), case already cached, median of --reps
  live_post_ms  per-POST scoring cost: events fed in 400 ms batches to one Scorer with
                provisional scoring at each batch (what the server does for the live panel)
The fingerprint (per-organ status + coverage rounded to 1e-6) must be identical between
rounds for a speed change to count. ``live_fingerprint`` hashes the per-organ status +
coverage returned by every live POST (provisional values included), so live-path changes
are checked too; ``live_final_matches_replay`` checks the session's last POST against replay.

``--http`` additionally drives a real ``python -m covmap serve`` process (scratch data dir
with the cases symlinked, so no sessions land in $COVMAP_DATA) at a browser-like cadence:
one POST of the events of each ``--http-cadence-ms`` window, on a keep-alive connection,
paced in real time; it records the HTTP round trip and the server's own scoring time.

    python bench/bench_replay.py --cases lidc-0001 pancreas-0051 --out bench.json [--reps 5]
        [--cold-reps 3] [--http [--http-scenarios full_read] [--http-cadence-ms 400]]
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import signal
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


def http_live(case: str, events: list[dict], port: int, cadence_ms: float) -> dict:
    """POST each cadence window's events to a live session of the real server, paced in real time."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)

    def call(method, path, body=None):
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        assert r.status == 200, (r.status, data[:300])
        return json.loads(data)

    sid = call("POST", "/api/sessions", {"case": case, "label": "bench"})["session"]
    rtt, score, sizes, cov = [], [], [], None
    start = time.perf_counter()
    for i, (batch, now) in enumerate(live_batches(events, cadence_ms)):
        delay = start + (i + 1) * cadence_ms / 1000 - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        body = {"events": [{**e, "session": sid} for e in batch], "now_ms": now}
        t = time.perf_counter()
        cov = call("POST", f"/api/sessions/{sid}/events", body)
        rtt.append((time.perf_counter() - t) * 1000)
        score.append(cov["timing_ms"]["score"])
        sizes.append(len(batch))
    conn.close()
    pct = lambda xs: sorted(xs)[int(0.95 * (len(xs) - 1))]  # noqa: E731
    return {"session": sid, "posts": len(rtt), "events_per_post_median": stats(sizes)["median"],
            "rtt_ms": {"median": stats(rtt)["median"], "p95": round(pct(rtt), 3), "max": round(max(rtt), 3)},
            "server_score_ms": {"median": stats(score)["median"], "p95": round(pct(score), 3)},
            "fingerprint": fingerprint(cov)[0]}


def start_server(cases: list[str], work: Path, port: int):
    from covmap import paths

    data = work / "http-data"
    shutil.rmtree(data, ignore_errors=True)
    (data / "cases").mkdir(parents=True)
    for c in cases:
        os.symlink(paths.case_dir(c), data / "cases" / c)
    p = subprocess.Popen([sys.executable, "-m", "covmap", "serve", "--port", str(port)],
                         env=dict(os.environ, COVMAP_DATA=str(data)), stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline()  # printed once listening, or the failure (e.g. the port is taken)
    try:
        started = json.loads(line)
    except ValueError:
        started = {}
    if not started.get("ok") or started.get("pid") != p.pid:
        try:
            p.wait(timeout=10)
            rest = p.stdout.read()  # the failure object spans several lines
        except subprocess.TimeoutExpired:
            p.kill()
            rest = ""
        raise RuntimeError(f"server did not start: {' '.join((line + rest).split())[:300]}")
    for _ in range(100):
        if p.poll() is not None:
            raise RuntimeError(f"server exited with {p.returncode}")
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/api/health")
            if c.getresponse().status == 200:
                return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError("server did not start")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--scenarios", nargs="*", default=["full_read", "window_skip_lung", "region_skip_left_lung"])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--work", default=None, help="where event logs go (default: next to --out)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cold-reps", type=int, default=1, help="fresh-process CLI replays per scenario")
    ap.add_argument("--http", action="store_true", help="also measure live POSTs through the real server")
    ap.add_argument("--http-scenarios", nargs="*", default=["full_read"])
    ap.add_argument("--http-cadence-ms", type=float, default=400.0)
    ap.add_argument("--http-port", type=int, default=8797)
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
            colds = []
            for _ in range(a.cold_reps):
                t0 = time.perf_counter()
                p = subprocess.run([sys.executable, "-m", "covmap", "replay", "--case", case, "--events", str(ev)],
                                   capture_output=True, text=True)
                colds.append(time.perf_counter() - t0)
            cold = stats(colds)["median"]
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
            post, live_fps = [], hashlib.sha256()
            for batch, now in live_batches(events):
                t = time.perf_counter()
                lc = sc_obj.add(batch, now_ms=now)
                post.append((time.perf_counter() - t) * 1000)
                live_fps.update(json.dumps(fingerprint(lc)[1], sort_keys=True).encode())
            cres[sc] = {"events": len(events), "cli_exit": p.returncode, "cli_cold_s": round(cold, 3),
                        "cli_cold_runs": [round(x, 3) for x in sorted(colds)],
                        "live_fingerprint": live_fps.hexdigest()[:16],
                        "live_final_matches_replay": fingerprint(lc)[0] == fp,
                        "warm_ms": stats(warm), "live_posts": len(post),
                        "live_post_ms": {"median": stats(post)["median"], "p95": sorted(post)[int(0.95 * (len(post) - 1))],
                                         "max": max(post)},
                        "fingerprint": fp, "statuses": {k: v[0] for k, v in fp_full.items()}}
        res["cases"][case] = cres
    if a.http:
        srv = start_server(a.cases, work, a.http_port)
        try:
            res["http"] = {"cadence_ms": a.http_cadence_ms, "context": host_context(), "cases": {}}
            for case in a.cases:
                for sc in a.http_scenarios:
                    if sc not in res["cases"][case]:
                        continue
                    ev = work / f"{case}.{sc}.jsonl"
                    events = [json.loads(line) for line in ev.read_text().splitlines() if line.strip()]
                    h = http_live(case, events, a.http_port, a.http_cadence_ms)
                    h["fingerprint_matches_replay"] = h["fingerprint"] == res["cases"][case][sc]["fingerprint"]
                    res["http"]["cases"].setdefault(case, {})[sc] = h
        finally:
            srv.send_signal(signal.SIGTERM)
            res["http"]["server_exit"] = srv.wait(30)
    res["context_end"] = host_context()
    write(Path(a.out), res)
    ok = res.get("http", {}).get("server_exit", 0) == 0  # a clean stop of the server the bench measured
    print(json.dumps({"ok": ok, "out": a.out, "summary": {
        c: {s: [v["cli_cold_s"], v["warm_ms"]["median"], v["live_post_ms"]["p95"], v["fingerprint"]]
            for s, v in r.items()} for c, r in res["cases"].items()}}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
