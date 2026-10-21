"""Display-window (VOI) classes and presets (docs/SPEC.md "Window classes").

A displayed VOI ``(wc, ww)`` in HU falls into at most one class; ranges are inclusive.
Anything outside them is ``other`` and qualifies no organ.
"""

from __future__ import annotations

import math

CLASSES = ("lung", "bone", "soft")
OTHER = "other"

# class -> ((ww_min, ww_max), (wc_min, wc_max))
RANGES = {
    "lung": ((800.0, 2500.0), (-900.0, -300.0)),
    "bone": ((1000.0, 4500.0), (150.0, 1000.0)),
    "soft": ((100.0, 700.0), (-50.0, 150.0)),
}

PRESETS = {
    "soft": {"label": "Soft tissue", "wc": 40, "ww": 400, "class": "soft"},
    "lung": {"label": "Lung", "wc": -600, "ww": 1500, "class": "lung"},
    "bone": {"label": "Bone", "wc": 480, "ww": 2500, "class": "bone"},
    "liver": {"label": "Liver", "wc": 90, "ww": 150, "class": "soft"},
    "mediastinum": {"label": "Mediastinum", "wc": 50, "ww": 350, "class": "soft"},
    "brain": {"label": "Brain", "wc": 40, "ww": 80, "class": OTHER},
}


def classify(wc: float, ww: float) -> str:
    """Window class of a VOI: ``lung``, ``bone``, ``soft`` or ``other``."""
    wc, ww = float(wc), float(ww)
    if not (math.isfinite(wc) and math.isfinite(ww)):
        return OTHER
    for cls, ((w0, w1), (c0, c1)) in RANGES.items():
        if w0 <= ww <= w1 and c0 <= wc <= c1:
            return cls
    return OTHER


def qualifies(window_class: str, organ_windows) -> bool:
    return window_class in organ_windows


def preset_voi(name: str) -> dict:
    p = PRESETS[name]
    return {"wc": p["wc"], "ww": p["ww"]}
