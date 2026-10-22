"""Scripted readings (``source: "simulated"``) with independently counted truth.

A scenario is first planned (``plan_scenario``), then executed: ``simulate`` turns the plan
into stack-viewport events; the orchestrator's browser runner executes the same plan in
the real viewer. Plan format::

    {"case", "scenario", "seed", "source": "simulated", "canvas_px": [800, 800],
     "rect_convention": ..., "passes": [{"window", "voi": {"wc", "ww"}, "dwell_ms",
     "steps": [{"k", "rects": [[r0, r1, c0, c1], ...]}]}], "truth": {...}}

Within a pass the reader visits the steps in order and, at each step, displays slice
``k`` once per rect for ``dwell_ms`` each. A rect is the half-open image-pixel rectangle
``[r0, r1) x [c0, c1)`` that fills the (square) canvas; full field is ``[0, rows, 0,
cols]``. Every other rect is square in mm and may extend past the image edge (a panned
view): what is shown of the image is the rect clipped to ``[0, rows) x [0, cols)``.

Truth never uses ``covmap.scoring``: it is counted from the plan directly on
``labels.npy`` (per slice and window class, the dwell each pixel received; a voxel is
seen at >= 80 ms in a class its organ accepts), then mapped to statuses with the SPEC
thresholds. Every scenario is checked to keep each organ's expected coverage at least
``MARGIN`` away from both thresholds and to give the intended statuses; a scenario that
cannot is reported as not applicable for that case.
"""

from __future__ import annotations

import copy
import json
import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from covmap.baseline import unviewed_ranges
from covmap.casedata import read_json, resolve_case_dir
from covmap.windows import PRESETS

MIN_DWELL_MS = 80.0
REVIEWED, SKIPPED, MARGIN = 0.90, 0.05, 0.03
DWELL_MS, FLICK_MS = 150.0, 15.0
CANVAS_PX = [800, 800]
T0_MS = 1000.0
PASS_WINDOWS = ("soft", "lung", "bone")
RECT_CONVENTION = ("half-open image-pixel rect [r0, r1) x [c0, c1) filling the square canvas; "
                   "full field = [0, rows, 0, cols]; other rects are square in mm and may extend "
                   "past the image (panned view); the displayed image region is the rect clipped "
                   "to the image")
BASE_SCENARIOS = ("full_read", "window_skip_lung", "window_skip_bone", "region_skip_left_lung",
                  "dwell_skip_lung", "slice_skip_caudal", "partial_lung")
ORGAN_SKIP_PREFIX = "organ_skip_"
ORGAN_SKIP_PREFERENCE = ("spleen", "kidney_left", "kidney_right", "gallbladder", "thyroid",
                         "urinary_bladder", "heart", "stomach", "liver", "esophagus", "aorta")
MAX_ORGAN_SKIPS = 2


class NotApplicable(ValueError):
    pass


def _status(cov: float) -> str:
    return "reviewed" if cov >= REVIEWED else "skipped" if cov < SKIPPED else "partial"


def _near_threshold(cov: float) -> bool:
    return any(abs(cov - t) < MARGIN for t in (REVIEWED, SKIPPED))


@dataclass
class Ctx:
    """Case data read directly from disk (independent of covmap.casedata's indexing)."""

    case_id: str
    volume: dict
    organs: list
    labels: np.ndarray
    hist: np.ndarray            # (K, 256) label counts per slice
    shape: tuple
    _cache: dict = field(default_factory=dict)

    @classmethod
    def load(cls, case) -> "Ctx":
        d = resolve_case_dir(case)
        vol = read_json(d / "volume.json")
        organs = read_json(d / "seg" / "organs.json")["organs"]
        labels = np.load(d / "seg" / "labels.npy")
        hist = np.stack([np.bincount(s.ravel(), minlength=256) for s in labels])
        return cls(vol["case"], vol, organs, labels, hist, labels.shape)

    @property
    def full(self) -> list[int]:
        return [0, self.shape[1], 0, self.shape[2]]

    @property
    def ps(self) -> tuple[float, float]:
        return float(self.volume["pixel_spacing"][0]), float(self.volume["pixel_spacing"][1])

    def by_windows(self, *windows) -> list[dict]:
        return [o for o in self.organs if list(o["windows"]) == list(windows)]

    def slices_of(self, ids) -> np.ndarray:
        return np.flatnonzero(self.hist[:, list(ids)].sum(axis=1) > 0)

    def z(self, ks) -> np.ndarray:
        v = self.volume
        return (v["origin_lps"][2] + np.asarray(ks) * v["slice_spacing"] * v["slice_dir"][2])

    def cranial_first(self, ks) -> list[int]:
        ks = np.asarray(ks)
        return [int(k) for k in ks[np.argsort(-self.z(ks), kind="stable")]]

    def wsets(self) -> dict:
        if "wsets" not in self._cache:
            g = defaultdict(list)
            for o in self.organs:
                g[tuple(sorted(o["windows"]))].append(o["id"])
            self._cache["wsets"] = {ws: np.array(ids) for ws, ids in g.items()}
        return self._cache["wsets"]


# ---- truth by direct counting -------------------------------------------------------
def _clip(rect, shape):
    r0, r1, c0, c1 = rect
    r0, r1, c0, c1 = max(r0, 0), min(r1, shape[1]), max(c0, 0), min(c1, shape[2])
    return None if r0 >= r1 or c0 >= c1 else (r0, r1, c0, c1)


def count_truth(ctx: Ctx, passes: list[dict]) -> tuple[dict, np.ndarray]:
    """(expected coverage per organ key, viewed-image mask) for a plan's passes."""
    K, R, C = ctx.shape
    full = (0, R, 0, C)
    shown = defaultdict(lambda: defaultdict(list))  # k -> class -> [(clipped rect, dwell)]
    viewed = np.zeros(K, bool)
    for p in passes:
        cls = PRESETS[p["window"]]["class"]
        for st in p["steps"]:
            viewed[st["k"]] = True
            for rect in st["rects"]:
                r = _clip(rect, ctx.shape)
                if r is not None:
                    shown[st["k"]][cls].append((r, float(p["dwell_ms"])))
    seen = np.zeros(256)
    for k, by_cls in shown.items():
        for ws, ids in ctx.wsets().items():
            items = [x for cls in ws for x in by_cls.get(cls, ())]
            if not items:
                continue
            if all(r == full for r, _ in items):
                if sum(d for _, d in items) >= MIN_DWELL_MS:
                    seen[ids] += ctx.hist[k, ids]
                continue
            dwell = np.zeros((R, C))
            for (r0, r1, c0, c1), d in items:
                dwell[r0:r1, c0:c1] += d
            seen[ids] += np.bincount(ctx.labels[k][dwell >= MIN_DWELL_MS], minlength=256)[ids]
    total = ctx.hist.sum(axis=0)
    cov = {o["key"]: float(seen[o["id"]] / total[o["id"]]) if total[o["id"]] else 0.0 for o in ctx.organs}
    return cov, viewed


# ---- plan building ------------------------------------------------------------------
def _pass(window: str, ks, dwell: float, rects_of=None, full=None) -> dict:
    return {"window": window, "voi": {"wc": PRESETS[window]["wc"], "ww": PRESETS[window]["ww"]},
            "dwell_ms": float(dwell),
            "steps": [{"k": int(k), "rects": rects_of(k) if rects_of else [list(full)]} for k in ks]}


class _Planner:
    def __init__(self, ctx: Ctx, seed: int):
        self.ctx = ctx
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        self.down = True if seed == 0 else bool(self.rng.integers(2))

    def dwell(self, flick=False) -> float:
        if self.seed == 0:
            return FLICK_MS if flick else DWELL_MS
        return float(self.rng.integers(10, 21) if flick else self.rng.integers(130, 171))

    def order(self, ks) -> list[int]:
        """Alternate scroll direction per pass, as a reader does."""
        ks = self.ctx.cranial_first(ks)
        self.down = not self.down
        return ks if not self.down else ks[::-1]

    def passes(self, windows=PASS_WINDOWS, ks=None, special=None) -> list[dict]:
        """One pass per window over ``ks`` (default every slice), full field, normal dwell;
        ``special[window] = (ks, rects_of, flick)`` overrides a window's pass."""
        ctx, special = self.ctx, special or {}
        ks_all = range(ctx.shape[0]) if ks is None else ks
        out = []
        for w in windows:
            sks, rects_of, flick = special.get(w, (ks_all, None, False))
            out.append(_pass(w, self.order(sks), self.dwell(flick), rects_of, ctx.full))
        return out


def _lungs(ctx):
    lungs = ctx.by_windows("lung")
    if not lungs:
        raise NotApplicable("no lung organs in this case")
    return lungs


def _sc_full_read(pl: _Planner):
    yield (pl.passes(), {"*": "reviewed"},
           "Every slice in soft, lung and bone windows (one pass each), full field, "
           f"{DWELL_MS:.0f} ms per slice.")


def _sc_window_skip_lung(pl: _Planner):
    lungs = _lungs(pl.ctx)
    yield (pl.passes(("soft", "bone")), {**{o["key"]: "skipped" for o in lungs}, "*": "reviewed"},
           "Full stack in soft and bone windows only; the lung window is never used.")


def _sc_window_skip_bone(pl: _Planner):
    bones = pl.ctx.by_windows("bone")
    if not bones:
        raise NotApplicable("no bone organs in this case")
    yield (pl.passes(("soft", "lung")), {**{o["key"]: "skipped" for o in bones}, "*": "reviewed"},
           "Full stack in soft and lung windows only; the bone window is never used.")


def _sc_dwell_skip_lung(pl: _Planner):
    lungs = _lungs(pl.ctx)
    flick = (range(pl.ctx.shape[0]), None, True)
    yield (pl.passes(special={"lung": flick}), {**{o["key"]: "skipped" for o in lungs}, "*": "reviewed"},
           f"Lung-window pass flicks through the stack at ~{FLICK_MS:.0f} ms per slice "
           "(below the minimum dwell); soft and bone passes are normal.")


def _sc_partial_lung(pl: _Planner):
    ctx = pl.ctx
    lungs = _lungs(ctx)
    ks = ctx.cranial_first(ctx.slices_of([o["id"] for o in lungs]))
    for frac in (0.5, 0.45, 0.55, 0.4, 0.6, 0.35, 0.65, 0.3, 0.7):
        n = max(1, round(frac * len(ks)))
        yield (pl.passes(special={"lung": (ks[:n], None, False)}),
               {**{o["key"]: "partial" for o in lungs}, "*": "reviewed"},
               f"Lung window only over the cranial {n} of {len(ks)} lung slices "
               f"({frac:.0%}); soft and bone passes cover the full stack.")


def _sc_slice_skip_caudal(pl: _Planner):
    """The caudal block of slices closest to 30% of the stack (searched over 10-50%) for
    which every organ's covered fraction stays clear of the thresholds."""
    ctx = pl.ctx
    K = ctx.shape[0]
    order = ctx.cranial_first(range(K))
    sizes = sorted(range(max(1, round(0.1 * K)), round(0.5 * K) + 1), key=lambda n: (abs(n - 0.3 * K), n))
    for n_skip in sizes:
        kept, skipped = sorted(order[:K - n_skip]), sorted(order[K - n_skip:])
        yield (pl.passes(ks=kept), None,
               f"The caudal {n_skip} of {K} slices (k {skipped[0]}..{skipped[-1]}, {n_skip / K:.0%}) "
               "are never displayed; all other slices are read in soft, lung and bone windows.")


def _lung_sides(ctx):
    lungs = _lungs(ctx)
    left = [o for o in lungs if "left" in o["key"]]
    right = [o for o in lungs if "right" in o["key"]]
    if not left or not right:
        raise NotApplicable("needs both a left and a right lung organ")
    return left, right


def _best_split(h_right: np.ndarray, h_left: np.ndarray, right_low: bool) -> int:
    """Column s minimising lost right-lung + shown left-lung fraction for a view of the
    patient's right side: columns [.., s) if the right lung lies at low columns, else [s, ..)."""
    nr, nl = max(h_right.sum(), 1), max(h_left.sum(), 1)
    cr = np.concatenate([[0], np.cumsum(h_right)])  # cr[s] = right voxels in columns < s
    cl = np.concatenate([[0], np.cumsum(h_left)])
    if right_low:
        cost = (nr - cr) / nr + cl / nl
    else:
        cost = cr / nr + (nl - cl) / nl
    return int(np.argmin(cost))


def _sc_region_skip_left_lung(pl: _Planner):
    ctx = pl.ctx
    left, right = _lung_sides(ctx)
    lid, rid = [o["id"] for o in left], [o["id"] for o in right]
    K, R, C = ctx.shape
    ps_r, ps_c = ctx.ps
    W = round(R * ps_r / ps_c)  # columns spanned by a square-in-mm view of full image height
    lab = ctx.labels
    h_r = np.isin(lab, rid).sum(axis=(0, 1))
    h_l = np.isin(lab, lid).sum(axis=(0, 1))
    cols = np.arange(C)
    right_low = (h_r * cols).sum() / max(h_r.sum(), 1) < (h_l * cols).sum() / max(h_l.sum(), 1)
    s_global = _best_split(h_r, h_l, right_low)

    def rect(s):
        return [0, R, s - W, s] if right_low else [0, R, s, s + W]

    ks = range(K)
    desc = ("Lung-window pass shows only the patient's right side of the gap between the lungs "
            "(panned square view ending at the split column{}); soft and bone passes are full field.")
    intended = {**{o["key"]: "skipped" for o in left}, **{o["key"]: "reviewed" for o in right}, "*": "reviewed"}
    yield (pl.passes(special={"lung": (ks, lambda k: [rect(s_global)], False)}), intended,
           desc.format(f" c={s_global}"))
    per_k = {}
    for k in ks:
        hr = np.isin(lab[k], rid).sum(axis=0)
        hl = np.isin(lab[k], lid).sum(axis=0)
        per_k[k] = _best_split(hr, hl, right_low) if hr.any() and hl.any() else s_global
    yield (pl.passes(special={"lung": (ks, lambda k: [rect(per_k[k])], False)}), intended,
           desc.format(", split per slice"))


def _quadtree(blocked: np.ndarray, min_size: int) -> list[list[int]]:
    """Squares of a quadtree over the image that avoid ``blocked`` pixels."""
    R, C = blocked.shape
    P = np.zeros((R + 1, C + 1), np.int64)
    P[1:, 1:] = np.cumsum(np.cumsum(blocked, axis=0), axis=1)
    S = 1 << max(0, math.ceil(math.log2(max(R, C))))
    out, stack = [], [(0, 0, S)]
    while stack:
        r0, c0, s = stack.pop()
        if r0 >= R or c0 >= C:
            continue
        r1, c1 = min(r0 + s, R), min(c0 + s, C)
        if P[r1, c1] - P[r0, c1] - P[r1, c0] + P[r0, c0] == 0:
            out.append([r0, r0 + s, c0, c0 + s])
        elif s > min_size:
            h = s // 2
            stack += [(r0 + h, c0 + h, h), (r0 + h, c0, h), (r0, c0 + h, h), (r0, c0, h)]
    return sorted(out)


def _strips(blocked: np.ndarray, ps) -> list[list[int]]:
    """Up to four panned full-size square views showing everything outside the bbox of ``blocked``."""
    R, C = blocked.shape
    rows, cols = np.nonzero(blocked)
    r_lo, r_hi, c_lo, c_hi = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
    W, H = round(R * ps[0] / ps[1]), round(C * ps[1] / ps[0])
    out = []
    if c_lo > 0:
        out.append([0, R, int(c_lo) - W, int(c_lo)])
    if c_hi < C:
        out.append([0, R, int(c_hi), int(c_hi) + W])
    if r_lo > 0:
        out.append([int(r_lo) - H, int(r_lo), 0, C])
    if r_hi < R:
        out.append([int(r_hi), int(r_hi) + H, 0, C])
    return out


def _sc_organ_skip(pl: _Planner, key: str):
    from scipy.ndimage import binary_dilation

    ctx = pl.ctx
    organ = next((o for o in ctx.organs if o["key"] == key), None)
    if organ is None:
        raise NotApplicable(f"organ {key!r} not in this case")
    if list(organ["windows"]) != ["soft"]:
        raise NotApplicable(f"{key} is not a soft-window-only organ")
    ks = set(int(k) for k in ctx.slices_of([organ["id"]]))
    isotropic = abs(ctx.ps[0] - ctx.ps[1]) < 1e-6
    strategies = [("strips", 3, None), ("quadtree", 2, 32), ("quadtree", 2, 16), ("quadtree", 1, 8)]
    for name, dil, size in strategies:
        if name == "quadtree" and not isotropic:
            continue
        rects = {}
        for k in ks:
            blocked = binary_dilation(ctx.labels[k] == organ["id"], iterations=dil)
            rects[k] = _strips(blocked, ctx.ps) if name == "strips" else _quadtree(blocked, size)
        soft = (range(ctx.shape[0]), lambda k: rects[k] if k in rects else [ctx.full], False)
        label = "panned square views around its bounding box" if name == "strips" else \
            f"square tiles (quadtree, >= {size} px)"
        yield (pl.passes(special={"soft": soft}), {key: "skipped", "*": "reviewed"},
               f"On the {len(ks)} slices containing {organ['name']}, the soft-window pass shows "
               f"only {label} covering the image minus the organ dilated by {dil} px; "
               "lung and bone passes are full field.")


def _builder(scenario: str):
    if scenario.startswith(ORGAN_SKIP_PREFIX):
        key = scenario[len(ORGAN_SKIP_PREFIX):]
        return lambda pl: _sc_organ_skip(pl, key)
    fn = globals().get(f"_sc_{scenario}")
    if scenario not in BASE_SCENARIOS or fn is None:
        raise NotApplicable(f"unknown scenario {scenario!r}")
    return fn


def _check(cov: dict, intended: dict | None) -> list[str]:
    problems = [f"{k} coverage {c:.3f} within {MARGIN} of a threshold" for k, c in cov.items()
                if _near_threshold(c)]
    if intended is not None:
        for k, c in cov.items():
            want = intended.get(k, intended.get("*"))
            if want is not None and _status(c) != want:
                problems.append(f"{k} would be {_status(c)} ({c:.3f}), intended {want}")
    return problems


def _plan(ctx: Ctx, scenario: str, seed: int) -> dict:
    problems = []
    for passes, intended, description in _builder(scenario)(_Planner(ctx, seed)):
        cov, viewed = count_truth(ctx, passes)
        bad = _check(cov, intended)
        if bad:
            problems.append("; ".join(bad[:4]))
            continue
        expected = {k: _status(c) for k, c in cov.items() if _status(c) != "reviewed"}
        n_views = sum(len(st["rects"]) for p in passes for st in p["steps"])
        duration = sum(len(st["rects"]) * p["dwell_ms"] for p in passes for st in p["steps"])
        truth = {
            "case": ctx.case_id, "scenario": scenario, "source": "simulated", "seed": seed,
            "intended_skips": sorted(expected), "expected": {**expected, "*": "reviewed"},
            "expected_coverage": {k: round(c, 6) for k, c in cov.items()},
            "baseline_expected_complete": bool(viewed.all()),
            "baseline_expected_unviewed_ranges": unviewed_ranges(viewed),
            "params": {"min_dwell_ms": MIN_DWELL_MS, "reviewed_threshold": REVIEWED,
                       "skipped_threshold": SKIPPED, "margin": MARGIN},
            "description": description,
            "plan": {"passes": len(passes), "views": n_views, "duration_ms": duration},
        }
        return {"case": ctx.case_id, "scenario": scenario, "seed": seed, "source": "simulated",
                "canvas_px": CANVAS_PX, "rect_convention": RECT_CONVENTION,
                "shape": list(ctx.shape), "passes": passes, "truth": truth}
    raise NotApplicable(f"{scenario}: no variant keeps every organ clear of the thresholds"
                        + (f" ({problems[-1]})" if problems else ""))


_ctx_cache: dict = {}
_plans: dict = {}
_lock = threading.Lock()


def _ctx(case) -> Ctx:
    d = resolve_case_dir(case).resolve()
    files = [d / "volume.json", d / "seg" / "organs.json", d / "seg" / "labels.npy"]
    for f in files:
        if not f.exists():
            raise FileNotFoundError(f"case not segmented, missing {f}")
    key = (str(d), tuple(f.stat().st_mtime_ns for f in files))
    with _lock:
        if key not in _ctx_cache:
            _ctx_cache.clear()
            _plans.clear()
            _ctx_cache[key] = Ctx.load(d)
        return _ctx_cache[key]


def plan_scenario(case_id, scenario: str, seed: int = 0) -> dict:
    """JSON-able plan + truth for a scenario (raises NotApplicable)."""
    ctx = _ctx(case_id)
    key = (ctx.case_id, scenario, seed)
    if key not in _plans:
        try:
            _plans[key] = _plan(ctx, scenario, seed)
        except NotApplicable as e:
            _plans[key] = e
    if isinstance(_plans[key], NotApplicable):
        raise NotApplicable(str(_plans[key]))
    return copy.deepcopy(_plans[key])


def _organ_skip_candidates(ctx: Ctx) -> list[str]:
    soft = [o["key"] for o in ctx.by_windows("soft")]
    pref = [k for k in ORGAN_SKIP_PREFERENCE if k in soft]
    return pref + [k for k in soft if k not in pref]


def list_scenarios(case_id) -> dict:
    ctx = _ctx(case_id)
    out = []

    def entry(name):
        try:
            p = plan_scenario(case_id, name)
        except NotApplicable as e:
            return {"name": name, "applicable": False, "reason": str(e)}
        t = p["truth"]
        return {"name": name, "applicable": True, "description": t["description"],
                "intended_skips": t["intended_skips"], "expected": t["expected"],
                "baseline_expected_complete": t["baseline_expected_complete"], "views": t["plan"]["views"]}

    for name in BASE_SCENARIOS:
        out.append(entry(name))
    chosen = 0
    for key in _organ_skip_candidates(ctx):
        if chosen >= MAX_ORGAN_SKIPS:
            break
        e = entry(ORGAN_SKIP_PREFIX + key)
        out.append(e)
        chosen += e["applicable"]
    return {"case": ctx.case_id, "source": "simulated", "scenarios": out,
            "applicable": [e["name"] for e in out if e["applicable"]]}


# ---- plan -> events -----------------------------------------------------------------
def _world(vol: dict, k: float, r: float, c: float) -> list[float]:
    """SPEC: world = origin + c*ps_col*row_dir + r*ps_row*col_dir + k*slice_spacing*slice_dir."""
    o, rd, cd, sd = (np.asarray(vol[x], float) for x in ("origin_lps", "row_dir", "col_dir", "slice_dir"))
    ps_r, ps_c = vol["pixel_spacing"]
    return (o + c * ps_c * rd + r * ps_r * cd + k * vol["slice_spacing"] * sd).tolist()


def rect_corners(vol: dict, k: int, rect) -> list[list[float]]:
    """Canvas corners (TL, TR, BR, BL) in LPS for a half-open pixel rect on slice k.
    Pixel centres are integers, so the rect's edges sit at r0 - 0.5 ... r1 - 0.5. The full
    field of a non-square image is widened to the square canvas around its centre."""
    r0, r1, c0, c1 = rect
    ps_r, ps_c = vol["pixel_spacing"]
    rc, cc = (r0 + r1) / 2 - 0.5, (c0 + c1) / 2 - 0.5
    hr, hc = (r1 - r0) / 2, (c1 - c0) / 2
    if list(rect) == [0, vol["rows"], 0, vol["cols"]]:
        half = max(hr * ps_r, hc * ps_c)
        hr, hc = half / ps_r, half / ps_c
    return [_world(vol, k, rc - hr, cc - hc), _world(vol, k, rc - hr, cc + hc),
            _world(vol, k, rc + hr, cc + hc), _world(vol, k, rc + hr, cc - hc)]


def plan_events(plan: dict, volume: dict, session: str | None = None) -> list[dict]:
    session = session or f"sim-{plan['scenario']}-{plan['seed']}"
    normal = np.cross(volume["row_dir"], volume["col_dir"]).tolist()
    base = {"v": 1, "session": session, "case": plan["case"], "source": "simulated"}
    out, t = [], T0_MS
    for p in plan["passes"]:
        for st in p["steps"]:
            k = st["k"]
            for rect in st["rects"]:
                out.append({**base, "seq": len(out), "t_ms": t, "kind": "view", "viewport": "default",
                            "vp_type": "stack", "series": volume["series_uid"], "sop": volume["sop_uids"][k],
                            "corners_lps": rect_corners(volume, k, rect), "normal": normal,
                            "voi": dict(p["voi"]), "canvas_px": list(CANVAS_PX)})
                t += p["dwell_ms"]
    out.append({**base, "seq": len(out), "t_ms": t, "kind": "end", "viewport": "*"})
    return out


def simulate(case_id, scenario: str, out, seed: int = 0) -> dict:
    """Write the scenario's events (JSONL, ``source: simulated``) and ``<out>.truth.json``."""
    try:
        plan = plan_scenario(case_id, scenario, seed)
    except NotApplicable as e:
        return {"ok": False, "applicable": False, "case": str(case_id), "scenario": scenario,
                "error": str(e)}
    volume = _ctx(case_id).volume
    events = plan_events(plan, volume)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    truth_path = Path(str(out) + ".truth.json")
    with open(truth_path, "w") as f:
        json.dump(plan["truth"], f, indent=1)
    t = plan["truth"]
    return {"case": plan["case"], "scenario": scenario, "source": "simulated", "seed": seed,
            "out": str(out), "truth": str(truth_path), "events": len(events),
            "duration_ms": events[-1]["t_ms"] - events[0]["t_ms"], "intended_skips": t["intended_skips"],
            "expected": t["expected"], "baseline_expected_complete": t["baseline_expected_complete"],
            "description": t["description"]}
