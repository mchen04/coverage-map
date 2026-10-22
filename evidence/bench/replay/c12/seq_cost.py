"""Per-POST cost of the seq-conflict check (App._check_seqs), in-process."""
import json, sys, timeit
from pathlib import Path
from covmap.server import App, Config, Session
from covmap.scoring import Scorer
from covmap.casedata import load_case

evs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
sc = Scorer(load_case("lidc-0001"))
sc.add(evs[:300], validated=False)
s = Session(id="s-x", case="lidc-0001", case_dir=Path("."), meta={"seq_offset": 0}, log=Path("/dev/null"),
            meta_path=Path("/dev/null"))
from covmap.events import validate_events
new = validate_events(evs[300:303])
resend = validate_events(evs[200:203])
n = 20000
out = {"accepted_events": len(sc.seqs),
       "new_batch_3_us": round(timeit.timeit(lambda: App._check_seqs(s, sc, new), number=n) / n * 1e6, 2),
       "resend_batch_3_us": round(timeit.timeit(lambda: App._check_seqs(s, sc, resend), number=n) / n * 1e6, 2)}
print(json.dumps(out))
