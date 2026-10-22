import json

import pytest

from covmap.report import SIMULATED_NOTE, compare, report
from covmap.scoring import replay_file
from covmap.simulate import simulate
from synth import build_case


def _cov(statuses, complete=True, sources=None):
    return {"case": "c", "session": "s", "params": {"min_dwell_ms": 80, "reviewed_threshold": 0.9,
                                                    "skipped_threshold": 0.05},
            "organs": [{"key": k, "name": k, "status": s, "coverage": {"reviewed": 1.0, "partial": 0.5,
                                                                       "skipped": 0.0}[s],
                        "unseen": {"short_dwell": 0.0, "wrong_window": 0.0, "out_of_region": 0.0,
                                   "never_displayed": 0.0}} for k, s in statuses.items()],
            "summary": {}, "events": {"sources": sources or {"ohif": 3}},
            "baseline": {"model": "per-image-viewed", "images": 4, "viewed": 4 if complete else 3,
                         "unviewed_ranges": [] if complete else [[3, 3]], "complete": complete}}


def test_tp_fp_fn():
    cov = _cov({"a": "skipped", "b": "partial", "c": "reviewed", "d": "skipped", "e": "reviewed"})
    truth = {"scenario": "x", "expected": {"a": "skipped", "b": "skipped", "c": "skipped", "*": "reviewed"},
             "baseline_expected_complete": True}
    r = compare(cov, truth)
    assert (r["tp"], r["fp"], r["fn"], r["tn"]) == (2, 1, 1, 1)
    assert r["false_skips"] == ["d"] and r["missed_skips"] == ["c"]
    assert not r["exact_match"] and {m["key"] for m in r["mismatches"]} == {"b", "c", "d"}
    assert r["baseline"]["matches_expected"] and not r["baseline"]["flagged"]


def test_exact_match_and_missing_organ():
    cov = _cov({"a": "skipped", "b": "reviewed"})
    assert compare(cov, {"expected": {"a": "skipped", "*": "reviewed"}})["exact_match"]
    r = compare(cov, {"expected": {"a": "skipped", "zz": "skipped", "*": "reviewed"}})
    assert not r["exact_match"] and r["missing_organs"] == ["zz"]


@pytest.fixture(scope="module")
def case_dir(tmp_path_factory):
    return build_case(tmp_path_factory.mktemp("r"), "synth-r")


def _run(case_dir, tmp_path, name):
    out = tmp_path / f"{name}.jsonl"
    simulate(str(case_dir), name, out)
    replay_file(str(case_dir), out, out=tmp_path / "cov.json")
    return report(str(case_dir), tmp_path / "cov.json", truth=f"{out}.truth.json", out=tmp_path / "rep.json")


def test_baseline_catches_slice_skips(case_dir, tmp_path):
    rep = _run(case_dir, tmp_path, "slice_skip_caudal")
    b = rep["truth"]["baseline"]
    assert b["flagged"] and not b["complete"] and b["matches_expected"]
    assert b["catches"] and set(b["catches"]) <= {m["key"] for m in rep["truth"]["per_organ"] if m["expected"] != "reviewed"}
    assert b["coverage_map_catches_all_baseline_catches"] and b["false_alarms"] == []
    assert rep["simulated"] and rep["notes"][0] == SIMULATED_NOTE
    assert json.loads((tmp_path / "rep.json").read_text())["truth"]["exact_match"]


def test_baseline_cannot_catch_window_skips(case_dir, tmp_path):
    rep = _run(case_dir, tmp_path, "window_skip_lung")
    t = rep["truth"]
    assert t["exact_match"] and t["tp"] == 2 and t["fp"] == 0
    assert not t["baseline"]["flagged"] and t["baseline"]["catches"] == []
    assert sorted(t["baseline"]["cannot_catch"]) == ["lung_left", "lung_right"]


def test_report_without_truth(case_dir, tmp_path):
    out = tmp_path / "full.jsonl"
    simulate(str(case_dir), "full_read", out)
    replay_file(str(case_dir), out, out=tmp_path / "c.json")
    rep = report(str(case_dir), tmp_path / "c.json")
    assert "truth" not in rep and rep["summary"]["reviewed"] == len(rep["organs"])
    assert all(o["top_unseen_reason"] is None for o in rep["organs"])


def test_truth_for_other_case_rejected(case_dir, tmp_path):
    out = tmp_path / "full.jsonl"
    simulate(str(case_dir), "full_read", out)
    replay_file(str(case_dir), out, out=tmp_path / "c.json")
    t = json.loads((tmp_path / "full.jsonl.truth.json").read_text())
    t["case"] = "other"
    with pytest.raises(ValueError, match="truth is for case"):
        report(str(case_dir), tmp_path / "c.json", truth=t)


def test_cli_roundtrip(tmp_path, monkeypatch, capsys):
    from covmap.cli import main

    build_case(tmp_path / "cases", "synth-cli")
    monkeypatch.setenv("COVMAP_DATA", str(tmp_path))

    def run(*argv):
        rc = main(list(argv))
        return rc, json.loads(capsys.readouterr().out)

    rc, ls = run("scenarios", "--case", "synth-cli")
    assert rc == 0 and "region_skip_left_lung" in ls["applicable"]
    ev = str(tmp_path / "ev.jsonl")
    rc, sim = run("simulate", "--case", "synth-cli", "--scenario", "region_skip_left_lung", "--out", ev)
    assert rc == 0 and sim["ok"] and sim["source"] == "simulated"
    rc, cov = run("replay", "--case", "synth-cli", "--events", ev, "--out", str(tmp_path / "cov.json"))
    assert rc == 0 and cov["ok"] and cov["baseline"]["complete"]
    rc, rep = run("report", "--case", "synth-cli", "--coverage", str(tmp_path / "cov.json"),
                  "--truth", ev + ".truth.json")
    assert rc == 0 and rep["truth"]["exact_match"] and rep["truth"]["false_skips"] == []
    rc, bad = run("simulate", "--case", "synth-cli", "--scenario", "organ_skip_nothing", "--out", ev)
    assert rc == 2 and bad["ok"] is False
    rc, err = run("replay", "--case", "missing-case", "--events", ev)
    assert rc == 1 and err["ok"] is False and "missing" in err["error"]
    for argv in ([], ["replay", "--case", "synth-cli"], ["segment", "--case", "x", "--device", "cuda"], ["bogus"]):
        rc, usage = run(*argv)  # argparse usage errors are JSON on stdout too
        assert rc == 2 and usage["ok"] is False and usage["error"].startswith("usage: covmap")
