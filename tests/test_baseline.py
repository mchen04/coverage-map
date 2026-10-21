import numpy as np
import pytest

from covmap.baseline import (MODEL, PerImageBaseline, implied_organ_coverage, unviewed_ranges,
                             viewed_from_ranges)
from covmap.casedata import load_case
from covmap.scoring import Scorer
from synth import build_case, end, read_pass, stack_view, view_krc


@pytest.mark.parametrize("viewed,ranges", [
    ([1, 1, 1], []), ([0, 0, 0], [[0, 2]]), ([0, 1, 1, 0], [[0, 0], [3, 3]]),
    ([1, 0, 0, 1, 0, 1], [[1, 2], [4, 4]]), ([], []),
])
def test_unviewed_ranges_roundtrip(viewed, ranges):
    v = np.array(viewed, bool)
    assert unviewed_ranges(v) == ranges
    assert viewed_from_ranges(len(viewed), ranges).tolist() == v.tolist()


def test_summary():
    b = PerImageBaseline(10)
    for k in (0, 1, 2, 6, 7, 8, 9):
        b.mark(k)
    assert b.summary() == {"model": MODEL, "images": 10, "viewed": 7, "unviewed_ranges": [[3, 5]],
                           "complete": False}


def test_implied_organ_coverage():
    counts = np.array([[10, 10, 0, 0], [0, 5, 5, 10], [0, 0, 0, 0]])
    cov = implied_organ_coverage(counts, np.array([1, 1, 0, 0], bool))
    assert cov.tolist() == [1.0, 0.25, 0.0]


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    return load_case(build_case(tmp_path_factory.mktemp("b"), "synth-b"))


def test_any_window_zoom_or_duration_counts_as_viewed(case):
    vol, K = case.volume, case.shape[0]
    ks = [k for k in range(K) if k not in (3, 4, 5, 20)]
    evs = read_pass(vol, ks[:10], 0, 0, "brain", dwell=1)
    evs += read_pass(vol, ks[10:], 1000, 100, "soft", dwell=0, rect=(0, 4, 0, 4))
    evs.append(end(vol, 5000, 999))
    cov = Scorer(case).add(evs)
    assert cov["baseline"] == {"model": MODEL, "images": K, "viewed": K - 4,
                               "unviewed_ranges": [[3, 5], [20, 20]], "complete": False}


def test_only_known_axial_images_count(case):
    vol = case.volume
    K, R, C = case.shape
    coronal = [(-0.5, 10, -0.5), (-0.5, 10, C - 0.5), (K - 0.5, 10, C - 0.5), (K - 0.5, 10, -0.5)]
    evs = [stack_view(vol, 2, 0, 0, sop="9.9.9"), view_krc(vol, coronal, 10, 1),
           stack_view(vol, 5, 20, 2, series="1.1"), end(vol, 30, 3)]
    assert Scorer(case).add(evs)["baseline"]["viewed"] == 0
