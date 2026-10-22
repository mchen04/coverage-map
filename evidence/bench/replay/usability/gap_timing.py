"""Gap-target cost on the real cases: every organ, first call (builds the organ index) and repeats,
committed and provisional (open states credited up to now_ms), after a partial read."""
import json, statistics, sys, time
from covmap.casedata import load_case
from covmap.scoring import Scorer

events_dir, out = sys.argv[1], sys.argv[2]
res = {}
for case_id in ("lidc-0001", "pancreas-0051"):
    t = time.perf_counter(); case = load_case(case_id); load_s = time.perf_counter() - t
    for scen in ("window_skip_lung", "full_read"):
        evs = [json.loads(l) for l in open(f"{events_dir}/{case_id}.{scen}.jsonl") if l.strip()]
        part = [e for e in evs[: len(evs) * 2 // 3] if e["kind"] != "end"]
        now = part[-1]["t_ms"] + 300
        sc = Scorer(case)
        sc.add(part, now_ms=now)
        rows = {}
        for o in case.organs:
            key = o["key"]
            t = time.perf_counter(); g = sc.gap(key); first = (time.perf_counter() - t) * 1000
            rep, prov = [], []
            for _ in range(5):
                t = time.perf_counter(); sc.gap(key); rep.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter(); gp = sc.gap(key, now_ms=now); prov.append((time.perf_counter() - t) * 1000)
            rows[key] = {"voxels": int(case.organ_voxels[case.key_index[key]]), "first_ms": round(first, 3),
                         "repeat_ms": round(statistics.median(rep), 3), "provisional_ms": round(statistics.median(prov), 3),
                         "k": g["k"], "ranges": len(g["ranges"]), "unseen": g["unseen_voxels"],
                         "unseen_provisional": gp["unseen_voxels"]}
        big = max(rows, key=lambda k: rows[k]["voxels"])
        res[f"{case_id}/{scen}"] = {
            "organs": len(rows), "events_fed": len(part), "open_states": len(sc.segmenter.open),
            "max_first_ms": max(r["first_ms"] for r in rows.values()),
            "max_repeat_ms": max(r["repeat_ms"] for r in rows.values()),
            "max_provisional_ms": max(r["provisional_ms"] for r in rows.values()),
            "largest_organ": {big: rows[big]}, "per_organ": rows}
        print(case_id, scen, {k: v for k, v in res[f"{case_id}/{scen}"].items() if k != "per_organ"}, flush=True)
json.dump(res, open(out, "w"), indent=1)
