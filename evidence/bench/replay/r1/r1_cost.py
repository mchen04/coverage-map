"""Per-event cost added to Scorer.feed by last_view tracking (stack_view_of), in-process."""
import json
import sys
import timeit

from covmap.casedata import load_case
from covmap.events import validate_events
from covmap.scoring import Scorer

sc = Scorer(load_case("lidc-0001"))
evs = validate_events([json.loads(line) for line in open(sys.argv[1]) if line.strip()])
stack = [e for e in evs if e["kind"] == "view" and e["vp_type"] == "stack"][:200]
n = 200
t = timeit.timeit(lambda: [sc.stack_view_of(e) for e in stack], number=n) / (n * len(stack)) * 1e6
print(json.dumps({"stack_view_of_us_per_view": round(t, 3), "views": len(stack)}))
