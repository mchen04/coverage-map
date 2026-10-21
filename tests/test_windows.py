import itertools

import pytest

from covmap.windows import CLASSES, OTHER, PRESETS, RANGES, classify, qualifies


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_presets_land_in_their_class(name):
    p = PRESETS[name]
    assert classify(p["wc"], p["ww"]) == p["class"]


def test_preset_values():
    assert {k: (v["ww"], v["wc"]) for k, v in PRESETS.items()} == {
        "soft": (400, 40), "lung": (1500, -600), "bone": (2500, 480), "liver": (150, 90),
        "mediastinum": (350, 50), "brain": (80, 40)}


@pytest.mark.parametrize("wc,ww,cls", [
    (-900, 800, "lung"), (-300, 2500, "lung"), (-900, 799.9, OTHER), (-300, 2500.1, OTHER),
    (-900.1, 1500, OTHER), (-299.9, 1500, OTHER),
    (150, 1000, "bone"), (1000, 4500, "bone"), (149.9, 2500, OTHER), (1000.1, 2500, OTHER),
    (480, 999.9, OTHER), (480, 4500.1, OTHER),
    (-50, 100, "soft"), (150, 700, "soft"), (-50.1, 400, OTHER), (150.1, 400, OTHER),
    (40, 99.9, OTHER), (40, 700.1, OTHER),
    (40, 80, OTHER), (1024, 4096, OTHER), (0, 0, OTHER),
    (float("nan"), 400, OTHER), (40, float("inf"), OTHER),
])
def test_boundaries_inclusive(wc, ww, cls):
    assert classify(wc, ww) == cls


def test_ranges_are_disjoint():
    ww_grid = range(0, 5001, 25)
    wc_grid = range(-1200, 1201, 25)
    for ww, wc in itertools.product(ww_grid, wc_grid):
        hits = [c for c, ((w0, w1), (c0, c1)) in RANGES.items() if w0 <= ww <= w1 and c0 <= wc <= c1]
        assert len(hits) <= 1, (ww, wc, hits)


def test_qualifies():
    assert qualifies("lung", ["lung", "soft"]) and qualifies("soft", ["lung", "soft"])
    assert not qualifies("bone", ["lung", "soft"])
    assert not any(qualifies(OTHER, [c]) for c in CLASSES)
