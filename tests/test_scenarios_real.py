"""Every applicable scripted scenario on the real public cases reproduces its independently
counted truth through simulate -> replay -> report (needs segmented cases in $COVMAP_DATA)."""

import json

import pytest

from covmap import paths
from covmap.report import report
from covmap.scoring import replay_file
from covmap.simulate import list_scenarios, simulate

CASES = ["lidc-0001", "pancreas-0051"]


@pytest.mark.data
@pytest.mark.parametrize("case_id", CASES)
def test_real_case_scenarios(case_id, tmp_path):
    seg = paths.case_dir(case_id) / "seg"
    if not ((seg / "labels.npy").exists() and (seg / "organs.json").exists()):
        pytest.skip(f"{case_id} not segmented in {paths.data_dir()}")
    ls = list_scenarios(case_id)
    assert "full_read" in ls["applicable"] and "slice_skip_caudal" in ls["applicable"]
    assert any(n.startswith("organ_skip_") for n in ls["applicable"]), ls
    matrix = {}
    for name in ls["applicable"]:
        out = tmp_path / f"{name}.jsonl"
        sim = simulate(case_id, name, out)
        assert sim.get("ok", True), sim
        cov = replay_file(case_id, out, out=tmp_path / f"{name}.cov.json")
        rep = report(case_id, tmp_path / f"{name}.cov.json", truth=f"{out}.truth.json")
        t = rep["truth"]
        matrix[name] = {"exact": t["exact_match"], "false_skips": t["false_skips"],
                        "baseline_complete": cov["baseline"]["complete"],
                        "replay_ms": cov["timing_ms"]["replay"]}
        assert t["exact_match"], (name, t["mismatches"])
        assert t["false_skips"] == [], name
        assert t["baseline"]["matches_expected"] and t["baseline"]["ranges_match_expected"], name
    print(json.dumps({case_id: matrix}, indent=1))
