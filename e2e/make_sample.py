"""Record a preloaded sample reading session through the real OHIF coverage build.

The sample is a deliberately incomplete reading so a visitor immediately sees all three
states: a soft-tissue pass over the cranial three quarters of the slices, a lung-window pass
over the cranial half, and no bone-window pass. The browser-scripted reader drives real OHIF; the coverage
extension records what was displayed (events carry source: ohif, the recording instrument); the
session's event log is saved as the sample, whose metadata says source: browser-scripted.

    python e2e/make_sample.py --case pancreas-0051 --base http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from covmap import paths  # noqa: E402
from e2e.run_reading import run  # noqa: E402

SOFT = {"wc": 40.0, "ww": 400.0}
LUNG = {"wc": -600.0, "ww": 1500.0}


def sample_plan(case: str, dwell_ms: float) -> dict:
    vol = json.loads((paths.case_dir(case) / "volume.json").read_text())
    full = [0, vol["rows"], 0, vol["cols"]]
    n = vol["n_slices"]
    ks = list(range(n))
    # slice index grows along slice_dir; cranial = larger patient z (LPS +z is superior)
    cranial_first = ks if vol["slice_dir"][2] < 0 else ks[::-1]
    soft = sorted(cranial_first[: (3 * n) // 4])
    half = sorted(cranial_first[: n - n // 2])
    return {
        "case": case, "scenario": "sample_partial_reading",
        "passes": [
            {"window": "soft", "voi": SOFT, "dwell_ms": dwell_ms, "steps": [{"k": k, "rects": [full]} for k in soft]},
            {"window": "lung", "voi": LUNG, "dwell_ms": dwell_ms, "steps": [{"k": k, "rects": [full]} for k in half]},
        ],
        "truth": {"case": case, "scenario": "sample_partial_reading", "source": "browser-scripted",
                  "description": "sample only; statuses come from scoring, no truth claimed"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--name", default="partial-reading")
    ap.add_argument("--dwell-ms", type=float, default=150.0)
    a = ap.parse_args()
    plan = sample_plan(a.case, a.dwell_ms)
    work = paths.data_dir().parent / "evidence" / "samples"
    res = run(a.case, plan["scenario"], a.base, "coverage", work, plan=plan)
    dst = paths.data_dir() / "samples" / a.case
    dst.mkdir(parents=True, exist_ok=True)
    events = Path(res["events_path"]).read_text()
    (dst / f"{a.name}.jsonl").write_text(events)
    meta = {"label": "Sample: partial reading", "source": "browser-scripted",
            "description": ("Recorded by a scripted reader driving the real OHIF viewer: soft-tissue "
                            "window over the upper three quarters of the slices, lung window over the "
                            "upper half, no bone window. Shows reviewed, partly reviewed and skipped "
                            "organs."),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "events": events.count("\n"),
            "session": res["session"]}
    (dst / f"{a.name}.json").write_text(json.dumps(meta, indent=1))
    summary = res["coverage"]["summary"]
    print(json.dumps({"ok": True, "case": a.case, "sample": str(dst / f"{a.name}.jsonl"),
                      "events": meta["events"], "summary": summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
