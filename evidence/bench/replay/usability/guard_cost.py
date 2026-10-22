"""Per-POST cost of the code added to the live POST path (guards, tally, session_info), in-process."""
import json, sys, time, timeit
from pathlib import Path
from covmap.server import App, Config, Session
from covmap.scoring import Scorer
from covmap.casedata import load_case

ev_path = sys.argv[1]
evs = [json.loads(l) for l in open(ev_path) if l.strip()][:300]
app = App(Config(clock_tolerance_ms=5000.0))
s = Session(id="s-x", case="lidc-0001", case_dir=Path("."), meta={"created_epoch": time.time() - 30,
            "t_offset_ms": 0.0, "seq_offset": 0, "rules": None, "clock_lead_ms": 1e9}, log=Path("/dev/null"),
            meta_path=Path("/dev/null"))
s.scorer = Scorer(load_case("lidc-0001"))
batch = [{**e, "session": "s-x"} for e in evs[100:103]]
now = batch[-1]["t_ms"] + 10

def added():
    app._check_session_ids(s, batch)
    if any("from_sample" in e for e in batch):
        pass
    app._check_clock(s, batch, now)
    s.tally(batch)
    return s.info()

n = 20000
t = timeit.timeit(added, number=n) / n * 1e6
cov = s.scorer.coverage()
j0 = timeit.timeit(lambda: json.dumps(cov), number=500) / 500 * 1e6
cov2 = {**cov, "session_info": s.info()}
j1 = timeit.timeit(lambda: json.dumps(cov2), number=500) / 500 * 1e6
print(json.dumps({"added_code_us_per_post": round(t, 2), "json_dumps_us": [round(j0, 1), round(j1, 1)],
                  "response_bytes": [len(json.dumps(cov)), len(json.dumps(cov2))]}))
