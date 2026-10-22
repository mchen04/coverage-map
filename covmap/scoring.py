"""Organ coverage scoring (docs/SPEC.md "Scoring").

Every display state (a ``view`` until the next display event of its viewport) is
resolved to a plane (axis + index), a visible region on that plane, and a window class.
Closed states are merged by (plane, region, class) and applied to per-voxel accumulators
over all labeled organ voxels:

* ``qdwell`` - qualifying dwell (ms): states whose plane and region contain the voxel and
  whose window class is in the organ's ``windows``;
* ``flags``  - QUAL / NONQUAL: displayed in-region under a qualifying / non-qualifying
  window; PLANE: its plane was displayed at all (region or not).

A voxel is seen when it had a qualifying display (QUAL) and ``qdwell >= min_dwell_ms``
(so ``min_dwell_ms = 0`` means any qualifying display); unseen voxels get the first matching
reason of short_dwell (QUAL), wrong_window (NONQUAL), out_of_region (PLANE),
never_displayed. Open states are credited provisionally up to ``now_ms`` on temporary
copies of the touched voxels only. Voxel centres are at integer indices; a voxel is
inside the visible region when its centre is (inclusive edges).

Per-organ outputs come from ``table``: counts of voxels per organ and per voxel state
``flags | seen << 3`` (16 states). It is built by one full pass at the first coverage
call and then kept up to date by per-plane deltas, so a live POST costs work
proportional to the planes it touches, not to the size of the volume.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from covmap.baseline import PerImageBaseline
from covmap.casedata import Case, load_case
from covmap.events import EventError, Segmenter, read_jsonl, sort_events, validate_events
from covmap.windows import CLASSES, OTHER, classify

DEFAULTS = {"min_dwell_ms": 80.0, "reviewed_threshold": 0.90, "skipped_threshold": 0.05}
REASONS = ("short_dwell", "wrong_window", "out_of_region", "never_displayed")
QUAL, NONQUAL, PLANE = 1, 2, 4
# flags -> reason code 1..4 (index into REASONS + 1); 0 is reserved for "seen"
_REASON_CODE = np.array([4, 1, 2, 1, 3, 1, 2, 1], np.uint8)
SEEN = 8  # state bit next to the flags: QUAL and qdwell >= min_dwell_ms
N_STATES = 16
# state -> reason-code column (0 seen, 1..4 REASONS), as a 16 x 5 one-hot matrix
_STATE_TO_CODE = np.zeros((N_STATES, 5), np.int64)
_STATE_TO_CODE[np.arange(N_STATES), np.concatenate([_REASON_CODE, np.zeros(8, np.uint8)])] = 1
OBLIQUE_COS = math.cos(math.radians(5.0))
EPS = 1e-4  # voxel units: tolerance for corners landing exactly on a voxel centre


def make_params(min_dwell_ms=None, reviewed_threshold=None, skipped_threshold=None) -> dict:
    p = dict(DEFAULTS)
    for k, v in (("min_dwell_ms", min_dwell_ms), ("reviewed_threshold", reviewed_threshold),
                 ("skipped_threshold", skipped_threshold)):
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{k} must be a non-negative number, got {v!r}")
            p[k] = float(v)
    if p["skipped_threshold"] > p["reviewed_threshold"] or p["reviewed_threshold"] > 1:
        raise ValueError("need 0 <= skipped_threshold <= reviewed_threshold <= 1")
    return p


def status_of(coverage: float, params: dict) -> str:
    if coverage >= params["reviewed_threshold"]:
        return "reviewed"
    if coverage < params["skipped_threshold"]:
        return "skipped"
    return "partial"


@dataclass(frozen=True)
class Plane:
    """A resolved display state: plane ``axis`` (0 axial k, 1 coronal r, 2 sagittal c),
    ``index`` along it, visible ``region`` in in-plane voxel coords, window class."""

    axis: int
    index: int
    region: tuple
    cls: str


def _region(u, v, nu: int, nv: int) -> tuple:
    """Visible region from the 4 corners' in-plane coords: ("all",), ("none",),
    ("box", u0, u1, v0, v1) inclusive voxel bounds, or ("quad", u0, v0, ..., u3, v3).
    Plain float arithmetic on the 4 corners (per-event numpy calls on 4-element arrays
    cost ~8x more), in the same IEEE operation order as the array formulation."""
    us = [float(x) for x in u]
    vs = [float(x) for x in v]
    aligned = True
    for i in range(4):
        eu, ev = us[(i + 1) % 4] - us[i], vs[(i + 1) % 4] - vs[i]
        if not min(abs(eu), abs(ev)) <= 1e-3 * float(np.hypot(eu, ev)) + 1e-6:
            aligned = False
            break
    lo_u, hi_u = max(0, math.ceil(min(us) - EPS)), min(nu - 1, math.floor(max(us) + EPS))
    lo_v, hi_v = max(0, math.ceil(min(vs) - EPS)), min(nv - 1, math.floor(max(vs) + EPS))
    if lo_u > hi_u or lo_v > hi_v:
        return ("none",)
    if aligned:
        if lo_u == 0 and lo_v == 0 and hi_u == nu - 1 and hi_v == nv - 1:
            return ("all",)
        return ("box", lo_u, hi_u, lo_v, hi_v)
    area = 0.0
    for i in range(4):  # shoelace, summed in index order
        area += us[i] * vs[(i + 1) % 4] - us[(i + 1) % 4] * vs[i]
    if abs(0.5 * area) < 1e-6:
        return ("none",)
    return ("quad",) + tuple(round(x, 6) for p in zip(us, vs) for x in p)


def _inside(region: tuple, u: np.ndarray, v: np.ndarray) -> np.ndarray | None:
    """Boolean mask of in-plane voxels inside the region (None = all of them)."""
    kind = region[0]
    if kind == "all":
        return None
    if kind == "none":
        return np.zeros(u.shape, bool)
    if kind == "box":
        _, u0, u1, v0, v1 = region
        return (u >= u0) & (u <= u1) & (v >= v0) & (v <= v1)
    pts = np.asarray(region[1:], float).reshape(4, 2)
    area = np.sum(pts[:, 0] * np.roll(pts[:, 1], -1) - np.roll(pts[:, 0], -1) * pts[:, 1])
    sign = 1.0 if area > 0 else -1.0
    uf, vf = u.astype(np.float64), v.astype(np.float64)
    mask = np.ones(u.shape, bool)
    for i in range(4):
        (pu, pv), (qu, qv) = pts[i], pts[(i + 1) % 4]
        cross = (qu - pu) * (vf - pv) - (qv - pv) * (uf - pu)
        mask &= sign * cross >= -EPS * math.hypot(qu - pu, qv - pv)
    return mask


def resolve_view(case: Case, ev: dict, pts: np.ndarray | None = None) -> Plane | str:
    """Plane of a validated ``view`` event, or the ignore reason
    (``oblique`` / ``unknown_image`` / ``other``). ``pts``: the corners already mapped to
    continuous voxel (k, r, c) (Scorer.add maps a whole batch with one solve)."""
    series = ev.get("series")
    if series is not None and case.series_uid and series != case.series_uid:
        return "unknown_image"
    world = np.asarray(ev["corners_lps"], float)
    if pts is None:
        pts = case.geom.world_to_voxel(world)
    if ev["vp_type"] == "stack":
        k = case.sop_index.get(ev.get("sop"))
        if k is None:
            return "unknown_image"
        axis, index = 0, k
    else:
        n = np.cross(world[1] - world[0], world[3] - world[0])
        if np.linalg.norm(n) < 1e-9:
            if ev.get("normal") is None:
                return "other"
            n = np.asarray(ev["normal"], float)
        norm = np.linalg.norm(n)
        if norm < 1e-12:
            return "other"
        cos = np.abs(case.geom.plane_normals @ (n / norm))
        axis = int(np.argmax(cos))
        if cos[axis] < OBLIQUE_COS:
            return "oblique"
        pos = float(pts[:, axis].mean())
        if not -0.5 <= pos < case.shape[axis] - 0.5:
            return "other"
        index = int(math.floor(pos + 0.5))
    a1, a2 = [a for a in range(3) if a != axis]
    region = _region(pts[:, a1], pts[:, a2], case.shape[a1], case.shape[a2])
    return Plane(axis, index, region, classify(ev["voi"]["wc"], ev["voi"]["ww"]))


class Scorer:
    """Incremental coverage scorer for one reading session of one case."""

    def __init__(self, case, session: str | None = None, *, min_dwell_ms=None,
                 reviewed_threshold=None, skipped_threshold=None):
        self.case = load_case(case)
        self.params = make_params(min_dwell_ms, reviewed_threshold, skipped_threshold)
        self.session = session
        c = self.case
        n_org = len(c.organs)
        self.qdwell = np.zeros(c.n_vox, np.float32)
        self.flags = np.zeros(c.n_vox, np.uint8)
        self.table: np.ndarray | None = None  # (n_org, 16) voxel counts per state, see module doc
        self._flushed = False  # states were applied while there was no table
        self._qual = {cls: np.array([cls in o["windows"] for o in c.organs], bool)
                      for cls in CLASSES + (OTHER,)}
        # per-organ flag bits a state of class cls sets on its in-region voxels
        self._bits = {cls: np.where(q, QUAL, NONQUAL).astype(np.uint8) for cls, q in self._qual.items()}
        self.segmenter = Segmenter()
        self.pending: dict[Plane, float] = {}
        self.baseline = PerImageBaseline(c.shape[0])
        self.ignored = {"oblique": 0, "unknown_image": 0, "other": 0}
        self.sources: Counter = Counter()
        self.n_total = self.n_views = self.n_states = 0
        self.seqs: set[int] = set()
        self.last_t: float | None = None
        self.reader = {k: {"reader": None, "flagged": False, "note": None} for k in c.organ_keys}
        self.finished: dict | None = None

    # ---- events -------------------------------------------------------------------
    def add(self, events, now_ms: float | None = None, *, validated: bool = False) -> dict:
        """Apply a batch of events (sorted by (t_ms, seq)); return coverage, provisional
        at ``now_ms`` when given. A malformed batch raises EventError and changes nothing."""
        t0 = time.perf_counter()
        evs = events if validated else validate_events(events)
        foreign = {e["case"] for e in evs if e.get("case") not in (None, self.case.case_id)}
        if foreign:
            raise EventError(f"events for case {sorted(foreign)} sent to a session of case "
                             f"{self.case.case_id!r}")
        evs = sort_events(evs)
        # all view corners -> voxel coords with one solve (same LAPACK result per column)
        views = [i for i, e in enumerate(evs) if e["kind"] == "view"]
        pts = {}
        if views:
            world = np.asarray([evs[i]["corners_lps"] for i in views], float)
            pts = dict(zip(views, self.case.geom.world_to_voxel(world)))
        for i, ev in enumerate(evs):
            self.feed(ev, pts.get(i))
        return self.coverage(now_ms, _t0=t0)

    def feed(self, ev: dict, _pts: np.ndarray | None = None) -> None:
        """Apply one validated event (callers keep (t_ms, seq) order)."""
        self.n_total += 1
        if ev["seq"] in self.seqs:  # resent event
            self.ignored["other"] += 1
            return
        self.seqs.add(ev["seq"])
        self.sources[ev["source"]] += 1
        if self.session is None:
            self.session = ev.get("session")
        if self.last_t is not None and ev["t_ms"] < self.last_t:
            ev = {**ev, "t_ms": self.last_t}  # late batch: never run time backwards
        self.last_t = ev["t_ms"]
        kind = ev["kind"]
        payload = None
        if kind == "view":
            self.n_views += 1
            plane = resolve_view(self.case, ev, _pts)
            if isinstance(plane, str):
                self.ignored[plane] += 1
            else:
                payload = plane
                if plane.axis == 0:
                    self.baseline.mark(plane.index)
        elif kind == "annotation":
            self._annotate(ev)
        elif kind == "finish":
            self.finished = {"t_ms": ev["t_ms"], "confirmed": ev["confirmed"],
                             "remaining": list(ev.get("remaining", []))}
        for iv in self.segmenter.feed(ev, payload):
            if iv.payload is not None:
                self.pending[iv.payload] = self.pending.get(iv.payload, 0.0) + iv.dwell_ms
                self.n_states += 1

    def _annotate(self, ev: dict) -> None:
        r = self.reader.get(ev["organ"])
        if r is None:
            self.ignored["other"] += 1
            return
        action, note = ev["action"], ev.get("note")
        if action == "clear":
            r.update(reader=None, flagged=False, note=None)
            return
        if action == "flag":
            r["flagged"] = True
        else:
            r["reader"] = action
        if note:
            r["note"] = note

    # ---- accumulation -------------------------------------------------------------
    def _apply(self, plane: Plane, dwell: float, oi, u, v, f, q) -> None:
        """Credit one state to a plane's local arrays (oi organ ordinals as intp, u/v
        in-plane coords, f flags, q qdwell; f and q are modified in place). Boxes only
        touch the u-range of the box (u is sorted within a plane). Per-organ lookups
        instead of boolean scatter: out-of-region / non-qualifying voxels get bit 0 and
        +0.0 ms, which leaves them exactly unchanged."""
        region = plane.region
        if region[0] == "none":
            return
        if region[0] == "box":
            _, u0, u1, v0, v1 = region
            # keys in u's dtype: a Python int key makes numpy cast the whole plane to int64
            t = u.dtype.type
            lo, hi = int(u.searchsorted(t(u0), "left")), int(u.searchsorted(t(u1), "right"))
            if lo == hi:
                return
            oi, f, q, vv = oi[lo:hi], f[lo:hi], q[lo:hi], v[lo:hi]
            inside = (vv >= v0) & (vv <= v1)
        else:
            inside = _inside(region, u, v)
        bits = self._bits[plane.cls][oi]
        if inside is not None:
            bits *= inside
        f |= bits
        if dwell > 0:
            add = np.where(self._qual[plane.cls], np.float32(dwell), np.float32(0))[oi]
            if inside is not None:
                add *= inside
            q += add

    def _state(self, f, q) -> np.ndarray:
        """Voxel state ``flags | seen << 3``; seen = a qualifying display (QUAL) with
        qdwell >= min_dwell_ms. qdwell only grows together with QUAL, so for min_dwell_ms > 0
        the dwell test implies QUAL; at 0 seen is exactly QUAL (never "everything", which
        would also break the incremental table that starts from all-zero states)."""
        m = self.params["min_dwell_ms"]
        if m > 0:
            return f | ((q >= m).view(np.uint8) << 3)
        return f | ((f & QUAL) << 3)

    def _delta(self, oi, s_old, s_new) -> np.ndarray | None:
        """(n_org, 16) change of the state table when voxels of organs ``oi`` go s_old -> s_new."""
        ch = s_old != s_new
        if not ch.any():
            return None
        n = len(self.case.organs) * N_STATES
        oc = oi[ch] * N_STATES
        return (np.bincount(oc + s_new[ch], minlength=n) - np.bincount(oc + s_old[ch], minlength=n)
                ).reshape(-1, N_STATES)

    def _group(self, states) -> dict:
        groups: dict[tuple[int, int], list] = {}
        for plane, dwell in states:
            groups.setdefault((plane.axis, plane.index), []).append((plane, dwell))
        return groups

    def _flush(self) -> None:
        """Apply the closed states, plane by plane (all states of a plane in one go),
        keeping the state table up to date when there is one."""
        if not self.pending:
            return
        c = self.case
        groups = self._group(self.pending.items())
        self.pending.clear()
        track = self.table is not None
        self._flushed |= not track
        for (axis, index), states in groups.items():
            sel, u, v = c.plane(axis, index)
            if u.size == 0:
                continue
            oi = c.vord[sel].astype(np.intp)
            f, q = self.flags[sel], self.qdwell[sel]  # views (axial) or copies
            s_old = self._state(f, q) if track else None
            f |= PLANE
            for plane, dwell in states:
                self._apply(plane, dwell, oi, u, v, f, q)
            if not isinstance(sel, slice):
                self.flags[sel], self.qdwell[sel] = f, q
            if track and (d := self._delta(oi, s_old, self._state(f, q))) is not None:
                self.table += d

    # replay heuristic: with many pending planes and no table yet, one chunked pass over all
    # voxels after the flush is cheaper than per-plane deltas
    FULL_PASS_MIN_PLANES = 64

    def _current_table(self) -> np.ndarray:
        """Flush, then the committed state table (built on first use)."""
        if self.table is None and not self._flushed and \
                len({(p.axis, p.index) for p in self.pending}) < self.FULL_PASS_MIN_PLANES:
            n = len(self.case.organs)  # nothing applied yet: every voxel is in state 0
            self.table = np.zeros((n, N_STATES), np.int64)
            self.table[:, 0] = self.case.organ_voxels
        self._flush()
        if self.table is None:
            self.table = self._full_table()
        return self.table

    @property
    def seen_counts(self) -> np.ndarray:
        """Committed seen voxels per organ."""
        return self._current_table()[:, SEEN:].sum(axis=1)

    def _full_table(self) -> np.ndarray:
        """State table by one pass over all voxels, in cache-sized chunks."""
        n = len(self.case.organs) * N_STATES
        out = np.zeros(n, np.int64)
        vord, step = self.case.vord, 1 << 18
        for a in range(0, vord.size, step):
            b = a + step
            key = vord[a:b].astype(np.intp) * N_STATES + self._state(self.flags[a:b], self.qdwell[a:b])
            out += np.bincount(key, minlength=n)
        return out.reshape(-1, N_STATES)

    def _provisional(self, now_ms: float) -> np.ndarray | None:
        """(n_org, 16) table change from crediting the open states up to ``now_ms``
        (computed on copies; nothing is modified), or None."""
        open_states = [(iv.payload, iv.dwell_ms) for iv in self.segmenter.provisional(now_ms)
                       if iv.payload is not None]
        if not open_states:
            return None
        c = self.case
        groups = self._group(open_states)
        if len({axis for axis, _ in groups}) == 1:  # planes of one axis are disjoint
            total = None
            for (axis, index), states in groups.items():
                sel, u, v = c.plane(axis, index)
                oi = c.vord[sel].astype(np.intp)
                f0, q0 = self.flags[sel], self.qdwell[sel]
                f, q = f0 | PLANE, q0.copy()
                for plane, dwell in states:
                    self._apply(plane, dwell, oi, u, v, f, q)
                d = self._delta(oi, self._state(f0, q0), self._state(f, q))
                if d is not None:
                    total = d if total is None else total + d
            return total
        # planes of different axes intersect: work on the union of their voxels
        def gidx(sel):
            return np.arange(sel.start, sel.stop) if isinstance(sel, slice) else sel

        planes = {key: c.plane(*key) for key in groups}
        union = np.unique(np.concatenate([gidx(p[0]) for p in planes.values()]))
        f0, q0 = self.flags[union], self.qdwell[union]
        f, q = f0 | PLANE, q0.copy()
        for key, states in groups.items():
            sel, u, v = planes[key]
            pos = np.searchsorted(union, gidx(sel))
            oi = c.vord[sel].astype(np.intp)
            fl, ql = f[pos], q[pos]
            for plane, dwell in states:
                self._apply(plane, dwell, oi, u, v, fl, ql)
            f[pos], q[pos] = fl, ql
        return self._delta(c.vord[union].astype(np.intp), self._state(f0, q0), self._state(f, q))

    # ---- outputs ------------------------------------------------------------------
    def coverage(self, now_ms: float | None = None, *, _t0: float | None = None) -> dict:
        """Coverage JSON (SPEC); committed unless ``now_ms`` is given."""
        t0 = time.perf_counter() if _t0 is None else _t0
        c = self.case
        states = self._current_table()
        if now_ms is not None and (d := self._provisional(now_ms)) is not None:
            states = states + d
        table = states @ _STATE_TO_CODE  # (n_org, 5): seen, then REASONS
        organs = []
        for i, o in enumerate(c.organs):
            nv = int(c.organ_voxels[i])
            seen = int(table[i, 0])
            cov = seen / nv if nv else 0.0
            organs.append({
                "id": o["id"], "key": o["key"], "name": o.get("name", o["key"]),
                "windows": list(o["windows"]), "coverage": round(cov, 6),
                "status": status_of(cov, self.params), "seen_voxels": seen, "voxels": nv,
                "unseen": {r: round(table[i, j + 1] / nv, 6) if nv else 0.0 for j, r in enumerate(REASONS)},
                **self.reader[o["key"]]})
        total = int(c.organ_voxels.sum())
        summary = self._summary([o["status"] for o in organs],
                                int(table[:, 0].sum()) / total if total else 0.0)
        return {
            "case": c.case_id, "session": self.session, "provisional": now_ms is not None,
            "t_ms": now_ms if now_ms is not None else self.last_t,
            "params": dict(self.params), "organs": organs, "summary": summary,
            "baseline": self.baseline.summary(),
            "events": {"total": self.n_total, "views": self.n_views, "ignored": dict(self.ignored),
                       "sources": dict(self.sources), "states": self.n_states,
                       "open": len(self.segmenter.open)},
            "timing_ms": {"score": round((time.perf_counter() - t0) * 1000, 3)},
        }

    def _summary(self, statuses: list[str], voxel_coverage: float) -> dict:
        keys = self.case.organ_keys
        rd = [self.reader[k] for k in keys]
        return {
            "reviewed": statuses.count("reviewed"), "partial": statuses.count("partial"),
            "skipped": statuses.count("skipped"), "organs": len(statuses),
            "voxel_coverage": round(voxel_coverage, 6),
            "attested": sum(r["reader"] == "done" and s != "reviewed" for r, s in zip(rd, statuses)),
            "not_applicable": sum(r["reader"] == "na" for r in rd),
            "flagged": sum(r["flagged"] for r in rd),
            "remaining": [k for k, r, s in zip(keys, rd, statuses)
                          if s != "reviewed" and r["reader"] not in ("done", "na")],
            "finished": dict(self.finished) if self.finished else None,
        }

    def snapshot(self, now_ms: float | None = None) -> dict:
        """Cheap status snapshot (no unseen reasons) from the incremental seen counts."""
        seen = self.seen_counts
        if now_ms is not None and (d := self._provisional(now_ms)) is not None:
            seen += d[:, SEEN:].sum(axis=1)
        nv = self.case.organ_voxels
        cov = np.divide(seen, nv, out=np.zeros(seen.size), where=nv > 0)
        statuses = [status_of(x, self.params) for x in cov]
        total = int(nv.sum())
        return {"t_ms": now_ms if now_ms is not None else self.last_t,
                "summary": self._summary(statuses, int(seen.sum()) / total if total else 0.0),
                "statuses": dict(zip(self.case.organ_keys, statuses)),
                "coverage": {k: round(float(x), 4) for k, x in zip(self.case.organ_keys, cov)}}


def timeline(case, events: list[dict], frames: int = 40, **params) -> dict:
    """Coverage snapshots at ``frames`` evenly spaced times over the session, computed in
    one ordered pass (each frame credits open states up to its time)."""
    t0 = time.perf_counter()
    frames = max(1, min(int(frames), 1000))
    evs = sort_events(events)
    sc = Scorer(case, **params)
    if not evs:
        return {"frames": [], "timing_ms": {"timeline": 0.0}}
    t_first, t_last = evs[0]["t_ms"], evs[-1]["t_ms"]
    times = np.linspace(t_first, t_last, frames) if frames > 1 else np.array([t_last])
    out, i = [], 0
    for t in times:
        while i < len(evs) and evs[i]["t_ms"] <= t:
            sc.feed(evs[i])
            i += 1
        out.append(sc.snapshot(float(t)))
    return {"case": sc.case.case_id, "session": sc.session, "params": sc.params, "frames": out,
            "timing_ms": {"timeline": round((time.perf_counter() - t0) * 1000, 3)}}


def replay_file(case_id, events_path, out=None, min_dwell_ms=None, reviewed_threshold=None,
                skipped_threshold=None) -> dict:
    """Score an event log (committed states only; states still open at the end of the log
    are reported under ``events.open`` but not credited)."""
    t0 = time.perf_counter()
    case = load_case(case_id)
    t1 = time.perf_counter()
    evs = read_jsonl(events_path)
    t2 = time.perf_counter()
    cov = Scorer(case, min_dwell_ms=min_dwell_ms, reviewed_threshold=reviewed_threshold,
                 skipped_threshold=skipped_threshold).add(evs, validated=True)
    t3 = time.perf_counter()
    cov["timing_ms"].update(load=round((t1 - t0) * 1000, 3), parse=round((t2 - t1) * 1000, 3),
                            replay=round((t3 - t2) * 1000, 3), total=round((t3 - t0) * 1000, 3))
    cov["events_path"] = str(events_path)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(cov, f, indent=1)
        cov["out"] = str(out)
    return cov
