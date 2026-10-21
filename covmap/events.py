"""Reading events (docs/SPEC.md "Reading events"): validation, ordering, display intervals.

Display kinds: ``view`` opens a display state for its viewport that lasts until the next
display event for the same viewport, a ``hidden`` event for it (``"*"`` = every viewport)
or an ``end`` event. Reader-layer kinds (``annotation``, ``finish``) never open or close
display states. Events are applied in ``(t_ms, seq)`` order.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

DISPLAY_KINDS = ("view", "hidden", "end")
READER_KINDS = ("annotation", "finish")
KINDS = DISPLAY_KINDS + READER_KINDS
VP_TYPES = ("stack", "volume")
ACTIONS = ("done", "na", "flag", "clear")
ALL_VIEWPORTS = "*"


class EventError(ValueError):
    """A malformed event or request body."""


def _num(ev: dict, key: str, where: str, *, required=True, minimum=None) -> float | None:
    x = ev.get(key)
    if x is None:
        if required:
            raise EventError(f"{where}: missing '{key}'")
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise EventError(f"{where}: '{key}' must be a finite number, got {x!r}")
    if minimum is not None and x < minimum:
        raise EventError(f"{where}: '{key}' must be >= {minimum}, got {x!r}")
    return float(x)


def _str(ev: dict, key: str, where: str, *, required=True, nullable=False) -> str | None:
    x = ev.get(key)
    if x is None:
        if required and not nullable:
            raise EventError(f"{where}: missing '{key}'")
        return None
    if not isinstance(x, str) or not x:
        raise EventError(f"{where}: '{key}' must be a non-empty string, got {x!r}")
    return x


def _vec(x: Any, n: int, label: str, where: str) -> list[float]:
    if not isinstance(x, (list, tuple)) or len(x) != n:
        raise EventError(f"{where}: '{label}' must be a list of {n} numbers")
    out = []
    for v in x:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise EventError(f"{where}: '{label}' has a non-finite or non-numeric value {v!r}")
        out.append(float(v))
    return out


def validate_event(ev: Any, where: str = "event") -> dict:
    """Check one event against the schema; return a normalized shallow copy."""
    if not isinstance(ev, dict):
        raise EventError(f"{where}: must be a JSON object")
    if ev.get("v") != 1:
        raise EventError(f"{where}: unsupported or missing version 'v' (expected 1), got {ev.get('v')!r}")
    kind = ev.get("kind")
    if kind not in KINDS:
        raise EventError(f"{where}: 'kind' must be one of {', '.join(KINDS)}, got {kind!r}")
    seq = ev.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise EventError(f"{where}: 'seq' must be a non-negative integer, got {seq!r}")
    out = dict(ev)
    out["t_ms"] = _num(ev, "t_ms", where, minimum=0)
    _str(ev, "source", where)
    _str(ev, "session", where, required=False)
    _str(ev, "case", where, required=False)

    if kind == "view":
        _str(ev, "viewport", where)
        if ev.get("vp_type") not in VP_TYPES:
            raise EventError(f"{where}: 'vp_type' must be 'stack' or 'volume', got {ev.get('vp_type')!r}")
        _str(ev, "series", where, nullable=True)
        _str(ev, "sop", where, required=ev["vp_type"] == "stack", nullable=ev["vp_type"] == "volume")
        corners = ev.get("corners_lps")
        if not isinstance(corners, (list, tuple)) or len(corners) != 4:
            raise EventError(f"{where}: 'corners_lps' must be 4 points [TL, TR, BR, BL]")
        out["corners_lps"] = [_vec(p, 3, "corners_lps", where) for p in corners]
        if ev.get("normal") is not None:
            out["normal"] = _vec(ev["normal"], 3, "normal", where)
        voi = ev.get("voi")
        if not isinstance(voi, dict):
            raise EventError(f"{where}: 'voi' must be an object {{wc, ww}}")
        out["voi"] = {"wc": _num(voi, "wc", where + ".voi"), "ww": _num(voi, "ww", where + ".voi")}
        if ev.get("canvas_px") is not None:
            _vec(ev["canvas_px"], 2, "canvas_px", where)
    elif kind == "hidden":
        _str(ev, "viewport", where)
    elif kind == "end":
        _str(ev, "viewport", where, required=False)
    elif kind == "annotation":
        _str(ev, "organ", where)
        if ev.get("action") not in ACTIONS:
            raise EventError(f"{where}: 'action' must be one of {', '.join(ACTIONS)}, got {ev.get('action')!r}")
        note = ev.get("note")
        if note is not None and not isinstance(note, str):
            raise EventError(f"{where}: 'note' must be a string")
    elif kind == "finish":
        if not isinstance(ev.get("confirmed"), bool):
            raise EventError(f"{where}: 'confirmed' must be true or false")
        rem = ev.get("remaining", [])
        if not isinstance(rem, list) or not all(isinstance(k, str) for k in rem):
            raise EventError(f"{where}: 'remaining' must be a list of organ keys")
        out["remaining"] = list(rem)
    return out


def validate_events(events: Any, where: str = "events") -> list[dict]:
    if not isinstance(events, list):
        raise EventError(f"{where}: must be a list")
    return [validate_event(e, f"{where}[{i}]") for i, e in enumerate(events)]


def order_key(ev: dict) -> tuple[float, int]:
    return ev["t_ms"], ev["seq"]


def sort_events(events: Iterable[dict]) -> list[dict]:
    return sorted(events, key=order_key)


def read_jsonl(path) -> list[dict]:
    """Validated events from a JSONL file."""
    with open(path) as f:
        return parse_lines(f.read().splitlines(), str(path))


def parse_lines(lines: Iterable[str], name: str = "jsonl") -> list[dict]:
    """Validated events from JSONL lines (blank lines skipped); errors name the line."""
    out = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise EventError(f"{name}:{i}: invalid JSON ({e.msg})") from None
        out.append(validate_event(obj, f"{name}:{i}"))
    return out


def parse_post_body(body: Any) -> tuple[list[dict], float | None]:
    """``{"events": [...], "now_ms": x?}`` -> (validated events, now_ms)."""
    if not isinstance(body, dict):
        raise EventError("body must be a JSON object {events, now_ms?}")
    events = validate_events(body.get("events", []))
    now = _num(body, "now_ms", "body", required=False, minimum=0)
    return events, now


@dataclass(slots=True)
class Interval:
    viewport: str
    start_ms: float
    end_ms: float
    payload: Any

    @property
    def dwell_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)


class Segmenter:
    """Incremental per-viewport display-state machine.

    ``feed`` returns the intervals the event closes; open states are kept until closed.
    The caller attaches a payload (e.g. the resolved plane) to each ``view``.
    """

    def __init__(self):
        self.open: dict[str, tuple[float, Any]] = {}

    def _close(self, viewport: str, t: float) -> list[Interval]:
        if viewport == ALL_VIEWPORTS:
            closed = [Interval(vp, s, max(t, s), p) for vp, (s, p) in self.open.items()]
            self.open.clear()
            return closed
        if viewport in self.open:
            s, p = self.open.pop(viewport)
            return [Interval(viewport, s, max(t, s), p)]
        return []

    def feed(self, ev: dict, payload: Any = None) -> list[Interval]:
        kind, t = ev["kind"], ev["t_ms"]
        if kind == "view":
            closed = self._close(ev["viewport"], t)
            self.open[ev["viewport"]] = (t, payload)
            return closed
        if kind == "hidden":
            return self._close(ev["viewport"], t)
        if kind == "end":
            return self._close(ALL_VIEWPORTS, t)
        return []

    def provisional(self, now_ms: float) -> list[Interval]:
        """Open states as intervals ending at ``now_ms`` (does not close them)."""
        return [Interval(vp, s, max(now_ms, s), p) for vp, (s, p) in self.open.items()]


def segment(events: Iterable[dict], now_ms: float | None = None):
    """(closed intervals, provisional open intervals) for validated events; payload = the view event."""
    seg = Segmenter()
    closed: list[Interval] = []
    for ev in sort_events(events):
        closed += seg.feed(ev, ev if ev["kind"] == "view" else None)
    return closed, (seg.provisional(now_ms) if now_ms is not None else [])
