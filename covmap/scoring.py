"""Organ coverage scoring (docs/SPEC.md "Scoring").

Every display state (a ``view`` until the next display event of its viewport) is
resolved to a plane (axis + index), a visible region on that plane, and a window class.
Closed states are merged by (plane, region, class) and applied to per-voxel accumulators
over all labeled organ voxels:

* ``qdwell`` - qualifying dwell (ms): states whose plane and region contain the voxel and
  whose window class is in the organ's ``windows``;
* ``flags``  - QUAL / NONQUAL: displayed in-region under a qualifying / non-qualifying
  window; PLANE: its plane was displayed at all (region or not).

A voxel is seen when ``qdwell >= min_dwell_ms``; unseen voxels get the first matching
reason of short_dwell (QUAL), wrong_window (NONQUAL), out_of_region (PLANE),
never_displayed. Open states are credited provisionally up to ``now_ms`` on temporary
copies of the touched voxels only. Voxel centres are at integer indices; a voxel is
inside the visible region when its centre is (inclusive edges).
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


def _region(u: np.ndarray, v: np.ndarray, nu: int, nv: int) -> tuple:
    """Visible region from the 4 corners' in-plane coords: ("all",), ("none",),
    ("box", u0, u1, v0, v1) inclusive voxel bounds, or ("quad", u0, v0, ..., u3, v3)."""
    eu, ev = np.roll(u, -1) - u, np.roll(v, -1) - v
    aligned = bool(np.all(np.minimum(abs(eu), abs(ev)) <= 1e-3 * np.hypot(eu, ev) + 1e-6))
    lo_u, hi_u = max(0, math.ceil(u.min() - EPS)), min(nu - 1, math.floor(u.max() + EPS))
    lo_v, hi_v = max(0, math.ceil(v.min() - EPS)), min(nv - 1, math.floor(v.max() + EPS))
    if lo_u > hi_u or lo_v > hi_v:
        return ("none",)
    if aligned:
        if lo_u == 0 and lo_v == 0 and hi_u == nu - 1 and hi_v == nv - 1:
            return ("all",)
        return ("box", lo_u, hi_u, lo_v, hi_v)
    area = 0.5 * float(np.sum(u * np.roll(v, -1) - np.roll(u, -1) * v))
    if abs(area) < 1e-6:
        return ("none",)
    return ("quad",) + tuple(round(float(x), 6) for p in zip(u, v) for x in p)


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


def resolve_view(case: Case, ev: dict) -> Plane | str:
    """Plane of a validated ``view`` event, or the ignore reason
    (``oblique`` / ``unknown_image`` / ``other``)."""
    series = ev.get("series")
    if series is not None and case.series_uid and series != case.series_uid:
        return "unknown_image"
    world = np.asarray(ev["corners_lps"], float)
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
        self.seen_counts = np.zeros(n_org, np.int64)
        self._qual = {cls: np.array([cls in o["windows"] for o in c.organs] + [False], bool)
                      for cls in CLASSES + (OTHER,)}
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
        for ev in sort_events(evs):
            self.feed(ev)
        return self.coverage(now_ms, _t0=t0)

    def feed(self, ev: dict) -> None:
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
            plane = resolve_view(self.case, ev)
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
    def _masks(self, plane: Plane):
        """(sel, qualifying-in-region mask, non-qualifying-in-region mask) over the plane's
        voxels; for boxes ``sel`` is narrowed to the box's u-range (u is sorted per plane)."""
        sel, u, v = self.case.plane(plane.axis, plane.index)
        region = plane.region
        if region[0] == "box":
            _, u0, u1, v0, v1 = region
            lo, hi = int(np.searchsorted(u, u0, "left")), int(np.searchsorted(u, u1, "right"))
            sel = slice(sel.start + lo, sel.start + hi) if isinstance(sel, slice) else sel[lo:hi]
            v = v[lo:hi]
            inside = (v >= v0) & (v <= v1)
        else:
            inside = _inside(region, u, v)
        qual = self._qual[plane.cls][self.case.vord[sel]]
        if inside is None:
            return sel, qual, ~qual
        return sel, inside & qual, inside & ~qual

    def _flush(self) -> None:
        min_d = self.params["min_dwell_ms"]
        vord = self.case.vord
        for axis, index in {(p.axis, p.index) for p in self.pending}:
            self.flags[self.case.plane(axis, index)[0]] |= PLANE
        for plane, dwell in self.pending.items():
            sel, mq, mn = self._masks(plane)
            if mq.size == 0:
                continue
            f = self.flags[sel]  # view (axial) or copy (coronal/sagittal)
            f[mn] |= NONQUAL
            f[mq] |= QUAL
            if not isinstance(sel, slice):
                self.flags[sel] = f
            if dwell > 0 and mq.any():
                q = self.qdwell[sel]
                before = q[mq]
                after = before + np.float32(dwell)
                q[mq] = after
                if not isinstance(sel, slice):
                    self.qdwell[sel] = q
                newly = (before < min_d) & (after >= min_d)
                if newly.any():
                    self.seen_counts += np.bincount(vord[sel][mq][newly], minlength=self.seen_counts.size)
        self.pending.clear()

    def _provisional(self, now_ms: float):
        """Open states credited to ``now_ms`` on copies: (union idx, qdwell, flags) or None."""
        open_states = [(iv.payload, iv.dwell_ms) for iv in self.segmenter.provisional(now_ms)
                       if iv.payload is not None]
        if not open_states:
            return None

        def gidx(sel):
            return np.arange(sel.start, sel.stop) if isinstance(sel, slice) else sel

        parts = []
        for plane, dwell in open_states:
            sel, mq, mn = self._masks(plane)
            parts.append((gidx(self.case.plane(plane.axis, plane.index)[0]), gidx(sel), mq, mn, dwell))
        union = np.unique(np.concatenate([p[0] for p in parts]))
        q, f = self.qdwell[union].copy(), self.flags[union].copy()
        for whole, sub, mq, mn, dwell in parts:
            f[np.searchsorted(union, whole)] |= PLANE
            pos = np.searchsorted(union, sub)
            f[pos[mn]] |= NONQUAL
            f[pos[mq]] |= QUAL
            q[pos[mq]] += np.float32(dwell)
        return union, q, f

    # ---- outputs ------------------------------------------------------------------
    def coverage(self, now_ms: float | None = None, *, _t0: float | None = None) -> dict:
        """Coverage JSON (SPEC); committed unless ``now_ms`` is given."""
        t0 = time.perf_counter() if _t0 is None else _t0
        self._flush()
        c, min_d = self.case, self.params["min_dwell_ms"]
        codes = _REASON_CODE[self.flags]
        codes[self.qdwell >= min_d] = 0
        prov = self._provisional(now_ms) if now_ms is not None else None
        if prov is not None:
            union, q, f = prov
            pc = _REASON_CODE[f]
            pc[q >= min_d] = 0
            codes[union] = pc
        n_org = len(c.organs)
        table = np.bincount(c.vord.astype(np.int64) * 5 + codes, minlength=n_org * 5).reshape(n_org, 5)
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
        self._flush()
        seen = self.seen_counts.copy()
        if now_ms is not None and (prov := self._provisional(now_ms)) is not None:
            union, q, _ = prov
            min_d = self.params["min_dwell_ms"]
            newly = (q >= min_d) & (self.qdwell[union] < min_d)
            seen += np.bincount(self.case.vord[union[newly]], minlength=seen.size)
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
