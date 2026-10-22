"""End-to-end run of the JSON CLI on both cases; every command is a fresh `python -m covmap` process.

Per case: fetch (re-verifies the download and derived files), orthanc-load, segment into a scratch
output directory (real TotalSegmentator inference, labels compared with the production `seg`),
scenarios, then for every applicable scenario simulate -> replay -> report against its truth, and
render the map of one skip scenario. Each command's argv, exit status, wall time and stdout JSON go
to <out>/<case>/NN-<step>.json; <out>/summary.json holds the verdict.

    python e2e/cli_e2e.py --out "$MINI_WORK/evidence/cli-e2e"
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from covmap.paths import case_dir  # noqa: E402

CASES = ["lidc-0001", "pancreas-0051"]
RENDER_SCENARIO = "window_skip_lung"


class Runner:
    def __init__(self, out: Path):
        self.out, self.steps = out, []

    def run(self, case: str, step: str, *args: str) -> dict:
        argv = [sys.executable, "-m", "covmap", *args]
        t0 = time.perf_counter()
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        wall = round(time.perf_counter() - t0, 3)
        try:
            doc = json.loads(p.stdout)
        except ValueError:
            doc = None
        rec = {"argv": ["python", "-m", "covmap", *args], "exit": p.returncode, "wall_s": wall,
               "stdout_is_one_json_object": isinstance(doc, dict), "stdout": doc if doc is not None else p.stdout,
               "stderr_tail": p.stderr[-2000:]}
        d = self.out / case
        d.mkdir(parents=True, exist_ok=True)
        n = sum(1 for s in self.steps if s["case"] == case)
        (d / f"{n:02d}-{step}.json").write_text(json.dumps(rec, indent=1) + "\n")
        self.steps.append({"case": case, "step": step, "exit": p.returncode, "wall_s": wall,
                           "json": rec["stdout_is_one_json_object"]})
        return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases", nargs="+", default=CASES)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    r, cases = Runner(out), {}
    t_all = time.perf_counter()

    for case in args.cases:
        res = cases[case] = {"scenarios": {}}
        r.run(case, "fetch", "fetch", "--case", case)
        r.run(case, "orthanc-load", "orthanc-load", "--case", case)
        scratch = case_dir(case) / "seg-e2e"
        seg = r.run(case, "segment", "segment", "--case", case, "--out-name", "seg-e2e")
        prod = json.loads((case_dir(case) / "seg" / "segment.json").read_text())["hashes_sha256"]
        got = seg["stdout"].get("hashes_sha256", {}) if isinstance(seg["stdout"], dict) else {}
        res["segment"] = {"device": seg["stdout"].get("device") if got else None,
                          "totalsegmentator_wall_s": (seg["stdout"].get("totalsegmentator") or {}).get("wall_s")
                          if got else None,
                          "labels_identical_to_production": got.get("labels.npy") == prod["labels.npy"],
                          "organs_identical_to_production": got.get("organs.json") == prod["organs.json"]}
        if scratch.is_dir() and scratch.name == "seg-e2e":
            shutil.rmtree(scratch)  # disposable: the record above keeps its hashes

        listed = r.run(case, "scenarios", "scenarios", "--case", case)["stdout"]
        names = listed.get("applicable", []) if isinstance(listed, dict) else []
        work = out / case / "work"
        work.mkdir(exist_ok=True)
        for name in names:
            ev = work / f"{name}.jsonl"
            r.run(case, f"simulate-{name}", "simulate", "--case", case, "--scenario", name, "--out", str(ev))
            cov = work / f"{name}.coverage.json"
            r.run(case, f"replay-{name}", "replay", "--case", case, "--events", str(ev), "--out", str(cov))
            rep = r.run(case, f"report-{name}", "report", "--case", case, "--coverage", str(cov),
                        "--truth", f"{ev}.truth.json")["stdout"]
            t = rep.get("truth", {}) if isinstance(rep, dict) else {}
            res["scenarios"][name] = {"exact_match": t.get("exact_match"), "false_skips": t.get("false_skips"),
                                      "missed_skips": t.get("missed_skips")}
        if not names:
            continue  # scenarios failed (exit != 0 above): nothing to render
        shot = RENDER_SCENARIO if RENDER_SCENARIO in names else names[0]
        ren = r.run(case, "render", "render", "--case", case, "--coverage", str(work / f"{shot}.coverage.json"),
                    "--out", str(out / case / f"map-{shot}.png"))
        res["render"] = {"scenario": shot, "exit": ren["exit"]}

    usage = r.run("cli", "usage-error", "replay", "--case", CASES[0])
    summary = {
        "cases": cases, "commands": len(r.steps),
        "all_exit_0": all(s["exit"] == 0 for s in r.steps if s["case"] != "cli"),
        "all_stdout_one_json_object": all(s["json"] for s in r.steps),
        "usage_error": {"exit": usage["exit"],
                        "error": usage["stdout"].get("error") if usage["stdout_is_one_json_object"] else None},
        "all_scenarios_exact": all(c["scenarios"] and all(v["exact_match"] for v in c["scenarios"].values())
                                   for c in cases.values()),
        "no_false_skips": not any(v["false_skips"] for c in cases.values() for v in c["scenarios"].values()),
        "segment_matches_production": all(c["segment"]["labels_identical_to_production"] for c in cases.values()),
        "wall_s": round(time.perf_counter() - t_all, 1), "steps": r.steps,
    }
    summary["ok"] = (summary["all_exit_0"] and summary["all_stdout_one_json_object"] and usage["exit"] == 2
                     and summary["all_scenarios_exact"] and summary["no_false_skips"]
                     and summary["segment_matches_production"])
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "steps"}, indent=1))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
