"""Scripted scenarios: every applicable scenario must reproduce its independently counted
truth through the real scorer (exact status match, zero false skips)."""

import ast
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

import covmap.simulate as simulate_mod
from covmap.report import report
from covmap.scoring import replay_file
from covmap.simulate import (BASE_SCENARIOS, MARGIN, list_scenarios, plan_events, plan_scenario,
                             simulate)
from synth import build_case

GEOMETRIES = {"standard": (40, 96, 96), "flipped": (36, 80, 96), "mirrored": (40, 96, 96)}


@pytest.fixture(scope="module", params=sorted(GEOMETRIES))
def case_dir(request, tmp_path_factory):
    return build_case(tmp_path_factory.mktemp("sim"), f"synth-{request.param}",
                      shape=GEOMETRIES[request.param], orientation=request.param)


def test_truth_is_independent_of_scoring():
    tree = ast.parse(Path(simulate_mod.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("scoring" in m for m in imported if m), imported
    code = "import sys, covmap.simulate; print('covmap.scoring' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                          cwd=Path(simulate_mod.__file__).parents[1]).stdout.strip() == "False"


def test_all_scenarios_applicable_on_synthetic(case_dir):
    ls = list_scenarios(str(case_dir))
    names = ls["applicable"]
    assert set(BASE_SCENARIOS) <= set(names)
    organ_skips = [n for n in names if n.startswith("organ_skip_")]
    assert len(organ_skips) == 2


@pytest.mark.parametrize("seed", [0, 7])
def test_scenarios_match_truth_exactly(case_dir, tmp_path, seed):
    results = {}
    for name in list_scenarios(str(case_dir))["applicable"]:
        out = tmp_path / f"{name}.jsonl"
        sim = simulate(str(case_dir), name, out, seed=seed)
        assert sim.get("ok", True), sim
        cov = replay_file(str(case_dir), out, out=tmp_path / f"{name}.cov.json")
        rep = report(str(case_dir), tmp_path / f"{name}.cov.json", truth=f"{out}.truth.json")
        t = rep["truth"]
        results[name] = (t["exact_match"], t["false_skips"], t["mismatches"])
        assert t["exact_match"], (name, t["mismatches"])
        assert t["false_skips"] == [] and t["fp"] == 0, name
        assert t["baseline"]["matches_expected"] and t["baseline"]["ranges_match_expected"], name
        assert t["baseline"]["coverage_map_catches_all_baseline_catches"], name
        truth = json.loads(Path(f"{out}.truth.json").read_text())
        for key, c in truth["expected_coverage"].items():
            measured = next(o for o in cov["organs"] if o["key"] == key)
            assert measured["coverage"] == pytest.approx(c, abs=1e-6), (name, key)
    assert all(r[0] for r in results.values())


def test_expected_reasons(case_dir, tmp_path):
    def unseen(name, key):
        out = tmp_path / f"{name}.jsonl"
        simulate(str(case_dir), name, out)
        cov = replay_file(str(case_dir), out)
        return next(o for o in cov["organs"] if o["key"] == key)["unseen"]

    assert unseen("dwell_skip_lung", "lung_left")["short_dwell"] == pytest.approx(1.0)
    assert unseen("window_skip_lung", "lung_right")["wrong_window"] == pytest.approx(1.0)
    assert unseen("window_skip_bone", "sternum")["wrong_window"] == pytest.approx(1.0)
    # left lung was on screen in-region only under soft/bone: wrong_window precedes out_of_region
    assert unseen("region_skip_left_lung", "lung_left")["wrong_window"] > 0.97


def test_plan_format(case_dir):
    vol = json.loads((case_dir / "volume.json").read_text())
    R, C = vol["rows"], vol["cols"]
    ps_r, ps_c = vol["pixel_spacing"]
    for name in list_scenarios(str(case_dir))["applicable"]:
        plan = plan_scenario(str(case_dir), name)
        assert plan["source"] == "simulated" and plan["truth"]["scenario"] == name
        for p in plan["passes"]:
            assert p["window"] in ("soft", "lung", "bone") and set(p["voi"]) == {"wc", "ww"}
            assert p["dwell_ms"] > 0
            for st in p["steps"]:
                assert 0 <= st["k"] < vol["n_slices"] and st["rects"]
                for r0, r1, c0, c1 in st["rects"]:
                    assert r1 > r0 and c1 > c0
                    if [r0, r1, c0, c1] != [0, R, 0, C]:
                        assert math.isclose((r1 - r0) * ps_r, (c1 - c0) * ps_c), (name, [r0, r1, c0, c1])
        exp = plan["truth"]["expected_coverage"]
        assert all(min(abs(c - 0.9), abs(c - 0.05)) >= MARGIN for c in exp.values()), name


def test_events_are_labeled_and_ordered(case_dir):
    plan = plan_scenario(str(case_dir), "full_read")
    vol = json.loads((case_dir / "volume.json").read_text())
    evs = plan_events(plan, vol)
    assert all(e["source"] == "simulated" for e in evs)
    assert [e["seq"] for e in evs] == list(range(len(evs)))
    assert all(a["t_ms"] < b["t_ms"] for a, b in zip(evs, evs[1:]))
    assert evs[-1]["kind"] == "end" and evs[0]["vp_type"] == "stack" and evs[0]["canvas_px"] == [800, 800]
    assert len(evs) == 3 * vol["n_slices"] + 1


def test_slice_skip_caudal_skips_the_caudal_end(case_dir):
    plan = plan_scenario(str(case_dir), "slice_skip_caudal")
    vol = json.loads((case_dir / "volume.json").read_text())
    (a, b), = plan["truth"]["baseline_expected_unviewed_ranges"]
    K = vol["n_slices"]
    z = lambda k: vol["origin_lps"][2] + k * vol["slice_spacing"] * vol["slice_dir"][2]
    skipped = range(a, b + 1)
    shown = [k for k in range(K) if k not in skipped]
    assert max(z(k) for k in skipped) < min(z(k) for k in shown)  # caudal = lowest z
    assert 0.2 <= len(skipped) / K <= 0.4
    assert not plan["truth"]["baseline_expected_complete"]


def test_not_applicable(tmp_path):
    d = build_case(tmp_path, "no-lungs", organs=["liver", "spleen", "heart"])
    ls = {e["name"]: e for e in list_scenarios(str(d))["scenarios"]}
    for name in ("window_skip_lung", "region_skip_left_lung", "dwell_skip_lung", "partial_lung",
                 "window_skip_bone"):
        assert not ls[name]["applicable"] and ls[name]["reason"], name
    res = simulate(str(d), "window_skip_lung", tmp_path / "x.jsonl")
    assert res["ok"] is False and not (tmp_path / "x.jsonl").exists()
    assert simulate(str(d), "nonsense", tmp_path / "y.jsonl")["ok"] is False
