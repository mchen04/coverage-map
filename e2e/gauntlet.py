"""Gauntlet: the same controlled readings scored by every tool, against known truth.

Tools compared per (case, scenario):
  coverage_sim      Coverage Map scoring of the scenario's simulated event log (labeled simulated)
  coverage_browser  Coverage Map live session from a real OHIF reading (browser-scripted input)
  per_image_model   implemented model of described Synapse/eRAD per-image viewed/unviewed
                    behaviour (covmap/baseline.py), fed the same real OHIF events
  stock_ohif        stock OHIF v3.13.13 built-in viewed-slice tracker (viewedDataService),
                    read from the unmodified stock build after the same reading

Truth is the scenario plan's intent with expected statuses from independent voxel counting
(covmap.simulate). Slice-level tools cannot name organs, so each organ gets the coverage they
imply (fraction of its voxels on slices they call viewed) and is flagged by the same reviewed /
skipped thresholds as Coverage Map, the rule `covmap report` uses for its baseline. The looser
"any voxel on an unviewed slice" attribution is kept per row as `any_overlap` for reference.

    python e2e/gauntlet.py --cases lidc-0001 pancreas-0051 --out $MINI_WORK/evidence/gauntlet/round-1 \
        [--scenarios a b] [--no-browser] [--no-stock]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from covmap import paths  # noqa: E402


def organ_slices(case: str) -> dict[str, set[int]]:
    labels = np.load(paths.case_dir(case) / "seg" / "labels.npy", mmap_mode="r")
    organs = json.loads((paths.case_dir(case) / "seg" / "organs.json").read_text())["organs"]
    per_k = [set(np.unique(labels[k]).tolist()) for k in range(labels.shape[0])]
    out = {}
    for o in organs:
        out[o["key"]] = {k for k, ids in enumerate(per_k) if o["id"] in ids}
    return out


def expected_skips(truth: dict, keys: list[str]) -> dict[str, str]:
    exp = truth["expected"]
    default = exp.get("*", "reviewed")
    return {k: exp.get(k, default) for k in keys if exp.get(k, default) != "reviewed"}


def coverage_flags(cov: dict) -> dict[str, str]:
    return {o["key"]: o["status"] for o in cov["organs"] if o["status"] != "reviewed"}


def slice_tool_flags(case: str, unviewed_ranges: list[list[int]], params: dict) -> set[str]:
    """Organs a slice-level tool flags: implied coverage from its viewed slices below reviewed."""
    from covmap.baseline import implied_organ_coverage, viewed_from_ranges
    from covmap.casedata import load_case
    from covmap.report import _status

    c = load_case(case)
    implied = implied_organ_coverage(c.organ_slice_counts, viewed_from_ranges(c.shape[0], unviewed_ranges))
    return {k for k, x in zip(c.organ_keys, implied) if _status(float(x), params) != "reviewed"}


def slice_tool_judge(case, truth_skips, keys, unviewed_ranges, params, slices) -> dict:
    r = judge(truth_skips, slice_tool_flags(case, unviewed_ranges, params), keys, exact=False)
    unviewed = {k for a, b in unviewed_ranges for k in range(a, b + 1)}
    r["any_overlap"] = sorted(key for key, ks in slices.items() if ks & unviewed)
    return r


def judge(truth_skips: dict[str, str], flagged: set[str] | dict, keys: list[str], exact: bool) -> dict:
    """Per-tool verdict. exact=True also compares partial vs skipped."""
    flagged_set = set(flagged)
    caught = sorted(k for k in truth_skips if k in flagged_set)
    missed = sorted(k for k in truth_skips if k not in flagged_set)
    false_alarms = sorted(k for k in flagged_set if k not in truth_skips)
    r = {"caught": caught, "missed": missed, "false_alarms": false_alarms,
         "scenario_alert": bool(flagged_set)}
    if exact and isinstance(flagged, dict):
        r["status_mismatch"] = sorted(k for k in caught if flagged[k] != truth_skips[k])
        r["exact"] = not missed and not false_alarms and not r["status_mismatch"]
    return r


def run_sim(case: str, scenario: str, out: Path) -> dict:
    from covmap.report import report
    from covmap.scoring import replay_file
    from covmap.simulate import simulate

    ev = out / f"{case}.{scenario}.sim.jsonl"
    t0 = time.perf_counter()
    simulate(case, scenario, out=str(ev))
    cov_path = out / f"{case}.{scenario}.sim.coverage.json"
    cov = replay_file(case, str(ev), out=str(cov_path))
    rep = report(case, str(cov_path), truth=str(ev) + ".truth.json")
    return {"coverage": cov, "report": rep, "s": round(time.perf_counter() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--scenarios", nargs="*")
    ap.add_argument("--out", required=True)
    ap.add_argument("--coverage-base", default="http://127.0.0.1:8765")
    ap.add_argument("--stock-base", default="http://127.0.0.1:8766")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-stock", action="store_true")
    a = ap.parse_args()

    from covmap.simulate import list_scenarios, plan_scenario
    from e2e.run_reading import run as run_reading

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in a.cases:
        slices = organ_slices(case)
        keys = sorted(slices)
        listed = list_scenarios(case)["scenarios"]
        names = [s["name"] for s in listed if s.get("applicable", True)]
        if a.scenarios:
            names = [n for n in names if n in a.scenarios]
        for name in names:
            plan = plan_scenario(case, name)
            truth = plan["truth"]
            skips = expected_skips(truth, keys)
            row = {"case": case, "scenario": name, "truth_skips": skips,
                   "truth_baseline_complete": truth.get("baseline_expected_complete")}
            sim = run_sim(case, name, out)
            row["coverage_sim"] = judge(skips, coverage_flags(sim["coverage"]), keys, exact=True)
            params = sim["coverage"]["params"]
            row["per_image_model_sim"] = slice_tool_judge(
                case, skips, keys, sim["coverage"]["baseline"]["unviewed_ranges"], params, slices)
            if not a.no_browser:
                br = run_reading(case, name, a.coverage_base, "coverage", out / "browser", plan=plan)
                cov = br["coverage"]
                row["coverage_browser"] = judge(skips, coverage_flags(cov), keys, exact=True)
                row["per_image_model"] = slice_tool_judge(
                    case, skips, keys, cov["baseline"]["unviewed_ranges"], params, slices)
                row["browser_run"] = {k: br[k] for k in ("run", "open", "events_path", "session", "console_errors")}
                row["browser_stock_tracker_in_coverage_build"] = br["stock_viewed_tracker"]
                (out / "browser" / f"{case}.{name}.coverage.json").write_text(json.dumps(cov, indent=1))
            if not a.no_browser and not a.no_stock:
                st = run_reading(case, name, a.stock_base, "viewer", out / "stock", plan=plan)
                tracker = st["stock_viewed_tracker"]
                row["stock_ohif"] = slice_tool_judge(case, skips, keys, tracker["unviewed_ranges"], params, slices)
                row["stock_ohif"]["tracker"] = tracker
                row["stock_run"] = {k: st[k] for k in ("run", "open", "console_errors")}
            rows.append(row)
            (out / "rows.json").write_text(json.dumps(rows, indent=1, default=str))
            print(json.dumps({"case": case, "scenario": name,
                              "sim_exact": row["coverage_sim"].get("exact"),
                              "browser_exact": row.get("coverage_browser", {}).get("exact"),
                              "stock_caught": row.get("stock_ohif", {}).get("caught"),
                              "per_image_caught": row.get("per_image_model", row["per_image_model_sim"])["caught"]}),
                  flush=True)
    summary = summarize(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({"ok": True, "out": str(out), **summary["totals"]}))
    return 0


def summarize(rows: list[dict]) -> dict:
    tools = ["coverage_sim", "coverage_browser", "per_image_model", "per_image_model_sim", "stock_ohif"]
    totals = {}
    for t in tools:
        rs = [r[t] for r in rows if t in r]
        if not rs:
            continue
        totals[t] = {"rows": len(rs), "caught": sum(len(x["caught"]) for x in rs),
                     "missed": sum(len(x["missed"]) for x in rs),
                     "false_alarms": sum(len(x["false_alarms"]) for x in rs)}
        if t.startswith("coverage"):
            totals[t]["exact_rows"] = sum(1 for x in rs if x.get("exact"))
    # dominance: every skip a baseline caught, Coverage Map (browser, else sim) caught too
    dom = []
    for r in rows:
        ours = set(r.get("coverage_browser", r["coverage_sim"])["caught"])
        for t in ("per_image_model", "per_image_model_sim", "stock_ohif"):
            if t in r:
                lost = sorted(set(r[t]["caught"]) - ours)
                if lost:
                    dom.append({"case": r["case"], "scenario": r["scenario"], "tool": t, "baseline_only": lost})
    return {"totals": totals, "baseline_catches_missed_by_coverage_map": dom, "rows": len(rows)}


if __name__ == "__main__":
    raise SystemExit(main())
