"""App server (docs/SPEC.md "HTTP API"): case data, live scoring sessions, a streaming
read-only (GET/HEAD) reverse proxy to Orthanc for DICOMweb/WADO, and the static OHIF build
with SPA fallback. Only loopback Host headers are answered (DNS-rebinding guard).

Scoring runs in a thread pool (numpy releases the GIL) under a per-session lock, so
event batches of one session apply in order while proxy traffic keeps flowing.

Sessions persist as ``$COVMAP_DATA/sessions/<id>.jsonl`` (accepted events, one per line,
exactly what replay reads) plus ``<id>.json`` (case, label, rules, sample origin); a
session unknown to the running server is rebuilt from those files on first use. A
session created ``from_sample`` starts with the sample's events (each marked
``"from_sample": <name>``, original ``source`` kept); later client events are shifted by
``t_offset_ms`` / ``seq_offset`` (returned on creation) so they follow the sample in time.
"""

from __future__ import annotations

import asyncio
import csv
from collections import OrderedDict
import io
import json
import math
import mimetypes
import os
import re
import secrets
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

from covmap import paths
from covmap.casedata import organ_outline, read_json
from covmap.events import EventError, parse_post_body, read_jsonl, validate_events
from covmap.scoring import REASONS, Scorer, make_params, same_event, timeline

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailer", "trailers", "transfer-encoding", "upgrade"}
MAX_BODY = 256 * 1024 * 1024
MAX_LIVE_SESSIONS = int(os.environ.get("COVMAP_MAX_SESSIONS", "16"))
CHUNK = 1 << 16

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/json", ".map")


CLOCK_TOLERANCE_MS = 5000.0


def _env_tolerance() -> float | None:
    v = os.environ.get("COVMAP_CLOCK_TOLERANCE_MS")
    if v is None:
        return CLOCK_TOLERANCE_MS
    if v.strip().lower() in ("", "off", "none"):
        return None
    try:
        tol = float(v)
    except ValueError:
        tol = -1.0
    if not (math.isfinite(tol) and tol >= 0):
        raise ValueError(f"COVMAP_CLOCK_TOLERANCE_MS must be a number of ms >= 0 or 'off', not {v!r}")
    return tol


@dataclass
class Config:
    orthanc: str = "http://127.0.0.1:8042"
    data_dir: Path = field(default_factory=paths.data_dir)
    ohif_dist: Path = field(default_factory=paths.ohif_dist)
    # optional extra root for the standalone render page; by default /render.html and /render/*
    # come from the OHIF dist, where scripts/build_ohif.sh installs them
    web_dir: Path | None = field(default_factory=lambda: Path(os.environ["COVMAP_WEB_DIR"])
                                 if os.environ.get("COVMAP_WEB_DIR") else None)
    demo_case: str = field(default_factory=lambda: os.environ.get("COVMAP_DEMO_CASE", "pancreas-0051"))
    cross_origin_isolation: bool = field(default_factory=lambda: os.environ.get("COVMAP_COI") == "1")
    # live POSTs: a batch with an event t_ms (or now_ms) more than this far ahead of the
    # session's clock is refused (App._check_clock); None = no clock check
    clock_tolerance_ms: float | None = field(default_factory=lambda: _env_tolerance())

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra  # more fields of the JSON error body


def _json(obj, status=200, headers=None) -> web.Response:
    return web.json_response(obj, status=status, headers=headers,
                             dumps=lambda o: json.dumps(o, separators=(",", ":"), default=str))


def _safe_id(x: str | None, what: str) -> str:
    if not x or not ID_RE.match(x):
        raise ApiError(400, f"invalid {what} {x!r}")
    return x


def _write_jsonl(path: Path, events: list[dict], mode="a") -> None:
    with open(path, mode) as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")


def _append_batch(path: Path, events: list[dict]) -> None:
    """Append a batch to a session log, or nothing of it: a failed write (disk full, I/O) is cut
    back to the previous end, so the log never holds a torn line or half a batch."""
    size = path.stat().st_size if path.exists() else 0
    try:
        _write_jsonl(path, events)
    except BaseException:
        try:
            os.truncate(path, size)
        except OSError:
            pass
        raise


@dataclass
class Session:
    """A reading session. The object lives as long as the server; its scorer and event
    list are loaded on demand and may be dropped (LRU) - always use them under ``lock``."""

    id: str
    case: str
    case_dir: Path
    meta: dict
    log: Path
    meta_path: Path
    events: list | None = None
    scorer: Scorer | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # event-log tallies for session_info (kept with ``events``); see ``tally``
    stats: dict = field(default_factory=lambda: {"events": 0, "sample_events": 0, "last_seq": None,
                                                 "last_t_ms": None})
    last_now: float | None = None  # now_ms of the latest POST (server time base), for the gap target
    last_now_ms: float | None = None  # highest now_ms POSTed, client numbering (session_info)

    @property
    def params(self) -> dict:
        return self.meta.get("rules") or {}

    def save_meta(self) -> None:
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.meta, indent=1))
        tmp.replace(self.meta_path)

    def shift(self, events: list[dict]) -> list[dict]:
        dt, ds = self.meta.get("t_offset_ms", 0.0), self.meta.get("seq_offset", 0)
        if not dt and not ds:
            return events
        return [{**e, "t_ms": e["t_ms"] + dt, "seq": e["seq"] + ds} for e in events]

    def created_epoch(self) -> float:
        """Server wall-clock time the session was created (s since the epoch)."""
        t = self.meta.get("created_epoch")
        if t is None:  # sessions from before created_epoch: the second-resolution stamp
            try:
                t = time.mktime(time.strptime(self.meta["created"][:19], "%Y-%m-%dT%H:%M:%S"))
            except (KeyError, TypeError, ValueError):
                t = 0.0
        return float(t)

    def tally(self, events: list[dict], reset: bool = False) -> None:
        """Count logged events; ``last_seq`` / ``last_t_ms`` are the client's own numbering
        (as POSTed, before the sample shift) of its latest live event, None before any."""
        st = self.stats
        if reset:
            st.update(events=0, sample_events=0, last_seq=None, last_t_ms=None)
        dt, ds = self.meta.get("t_offset_ms", 0.0), self.meta.get("seq_offset", 0)
        for e in events:
            st["events"] += 1
            if "from_sample" in e:
                st["sample_events"] += 1
                continue
            seq, t = e["seq"] - ds, e["t_ms"] - dt
            st["last_seq"] = seq if st["last_seq"] is None else max(st["last_seq"], seq)
            st["last_t_ms"] = t if st["last_t_ms"] is None else max(st["last_t_ms"], t)

    def info(self) -> dict:
        """session_info (docs/SPEC.md "HTTP API"); call with the scorer loaded."""
        m, st = self.meta, self.stats
        fin = self.scorer.finished if self.scorer is not None else None
        return {"session": self.id, "case": self.case, "label": m.get("label"),
                "from_sample": m.get("from_sample"), "sample_events": st["sample_events"],
                "created": m.get("created"), "finished": bool(fin and fin.get("confirmed")),
                "events": st["events"], "last_seq": st["last_seq"], "last_t_ms": st["last_t_ms"],
                "last_now_ms": self.last_now_ms, "rules": dict(self.params), "t_offset_ms": m.get("t_offset_ms", 0.0),
                "seq_offset": m.get("seq_offset", 0),
                "last_view": self.scorer.last_view if self.scorer is not None else None}


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sessions: dict[str, Session] = {}
        self.live: OrderedDict[str, None] = OrderedDict()  # LRU of sessions with a loaded scorer
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="covmap-score")
        self.client: aiohttp.ClientSession | None = None

    async def run(self, fn, *args, **kw):
        return await asyncio.get_running_loop().run_in_executor(self.pool, lambda: fn(*args, **kw))

    # ---- cases ---------------------------------------------------------------------
    def case_dir(self, case: str) -> Path:
        d = self.cfg.cases_dir / _safe_id(case, "case")
        if not (d / "volume.json").exists():
            raise ApiError(404, f"unknown case {case!r}")
        return d

    def cases(self) -> list[dict]:
        out = []
        if not self.cfg.cases_dir.is_dir():
            return out
        for d in sorted(self.cfg.cases_dir.iterdir()):
            vf = d / "volume.json"
            if not vf.exists():
                continue
            try:
                v = read_json(vf)
            except (OSError, ValueError):
                continue
            organs = d / "seg" / "organs.json"
            n_org = len(read_json(organs)["organs"]) if organs.exists() else 0
            out.append({"case": d.name, "study_uid": v.get("study_uid"), "series_uid": v.get("series_uid"),
                        "n_slices": v.get("n_slices"), "organs": n_org,
                        "segmented": (d / "seg" / "labels.npy").exists() and organs.exists()})
        return out

    def demo(self) -> dict | None:
        cases = self.cases()
        pick = next((c for c in cases if c["case"] == self.cfg.demo_case), None) or \
            next((c for c in cases if c["segmented"]), None) or (cases[0] if cases else None)
        if pick is None:
            return None
        return {"case": pick["case"], "study_uid": pick["study_uid"],
                "url": f"/coverage?StudyInstanceUIDs={pick['study_uid']}"}

    # ---- sessions ------------------------------------------------------------------
    async def create_session(self, body: dict) -> dict:
        case = body.get("case")
        if not isinstance(case, str):
            raise ApiError(400, "body needs 'case'")
        cdir = self.case_dir(case)
        if not (cdir / "seg" / "labels.npy").exists():
            raise ApiError(409, f"case {case!r} is not segmented yet")
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ApiError(400, "'label' must be a string")
        prefill, sample_meta = [], None
        sample = body.get("from_sample")
        if sample is not None:
            _safe_id(sample, "sample")
            sdir = self.cfg.samples_dir / case
            if not (sdir / f"{sample}.jsonl").exists():
                raise ApiError(404, f"no sample {sample!r} for case {case!r}")
            try:
                prefill = await self.run(read_jsonl, sdir / f"{sample}.jsonl")
            except EventError as e:
                raise ApiError(500, f"sample {sample!r} is invalid: {e}")
            mp = sdir / f"{sample}.json"
            sample_meta = read_json(mp) if mp.exists() else {}
        sid = time.strftime("s-%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
        meta = {"session": sid, "case": case, "label": label or (sample_meta or {}).get("label"),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "created_epoch": time.time(), "rules": None,
                "from_sample": sample, "sample": sample_meta, "t_offset_ms": 0.0, "seq_offset": 0}
        if prefill:
            prefill = [{**e, "session": sid, "from_sample": sample} for e in prefill]
            meta["t_offset_ms"] = max(e["t_ms"] for e in prefill) + 1.0
            meta["seq_offset"] = max(e["seq"] for e in prefill) + 1
        s = Session(sid, case, cdir, meta, self.cfg.sessions_dir / f"{sid}.jsonl",
                    self.cfg.sessions_dir / f"{sid}.json")
        async with s.lock:
            await self.run(_write_jsonl, s.log, prefill, "w")
            s.save_meta()
            self.sessions[sid] = s
            await self._ready(s)
        return {"session": sid, "case": case, "label": meta["label"], "from_sample": sample,
                "prefilled_events": len(prefill), "t_offset_ms": meta["t_offset_ms"],
                "seq_offset": meta["seq_offset"]}

    async def session(self, sid: str) -> Session:
        """The session object (scorer not necessarily loaded; see ``_ready``)."""
        _safe_id(sid, "session")
        s = self.sessions.get(sid)
        if s is None:
            meta_path = self.cfg.sessions_dir / f"{sid}.json"
            if not meta_path.exists():
                raise ApiError(404, f"unknown session {sid!r}")
            meta = read_json(meta_path)
            s = self.sessions.setdefault(sid, Session(
                sid, meta["case"], self.case_dir(meta["case"]), meta,
                self.cfg.sessions_dir / f"{sid}.jsonl", meta_path))
        return s

    async def _ready(self, s: Session) -> Scorer:
        """Load the session's events and scorer if needed (caller holds ``s.lock``) and
        drop the scorers of the least recently used idle sessions beyond the limit."""
        if s.scorer is None:
            s.events = await self.run(read_jsonl, s.log) if s.log.exists() else []
            s.tally(s.events, reset=True)
            s.scorer = await self.run(self._rescore, s, s.params)
            # coronal/sagittal plane indexes in the background (idempotent, built once per case)
            self.pool.submit(s.scorer.case.prepare_planes)
        self.live[s.id] = None
        self.live.move_to_end(s.id)
        for sid in list(self.live)[:-MAX_LIVE_SESSIONS]:
            other = self.sessions[sid]
            if not other.lock.locked():
                other.scorer = other.events = None
                del self.live[sid]
        return s.scorer

    @staticmethod
    def _rescore(s: Session, rules: dict, events: list[dict] | None = None) -> Scorer:
        sc = Scorer(s.case_dir, s.id, **rules)
        events = s.events if events is None else events
        if events:
            sc.add(events, validated=True)
        return sc

    @staticmethod
    def _check_session_ids(s: Session, events: list[dict]) -> None:
        """Every event's ``session`` (when present) must be the session of the URL."""
        foreign = sorted({str(e["session"]) for e in events if e.get("session") not in (None, s.id)})
        if foreign:
            raise ApiError(400, f"events of session(s) {foreign[:3]} posted to session {s.id!r}; "
                                "nothing was applied")

    def _check_clock(self, s: Session, events: list[dict], now: float | None) -> float | None:
        """Live-POST clock guard (docs/SPEC.md "Reading events"), in the client's clock
        (before the sample shift). The client may start its clock before the session exists
        (OHIF starts it when the viewer opens), so the first POST carrying ``now_ms`` pins
        ``lead = max(0, now_ms - elapsed)``, elapsed = server time since the session was
        created. A batch is refused (400, nothing applied) when an event's ``t_ms`` is more
        than ``clock_tolerance_ms`` ahead of ``elapsed + lead`` or of the POST's own
        ``now_ms``, or when ``now_ms`` is ahead of ``elapsed + lead``: leftover events of an
        earlier clock would otherwise freeze the scorer clock (time never runs backwards).
        Returns the lead to pin once the batch is accepted (None: nothing to pin)."""
        tol = self.cfg.clock_tolerance_ms
        if tol is None or (not events and now is None):
            return None
        elapsed = (time.time() - s.created_epoch()) * 1000.0
        pinned = s.meta.get("clock_lead_ms")
        lead = pinned if pinned is not None else (max(0.0, now - elapsed) if now is not None else 0.0)
        limit = elapsed + lead + tol
        ev_limit = limit if now is None else min(limit, now + tol)
        ahead = [e for e in events if e["t_ms"] > ev_limit]
        if ahead or (now is not None and now > limit):
            what = (f"event seq {ahead[0]['seq']} t_ms {ahead[0]['t_ms']}" if ahead else f"now_ms {now}")
            raise ApiError(400, f"{what} is ahead of session {s.id!r}'s clock (accepted up to "
                                f"{ev_limit if ahead else limit:.0f} ms: time since the session was created"
                                f" + client lead {lead:.0f} ms + {tol:.0f} ms, events also <= now_ms + "
                                f"{tol:.0f} ms); nothing was applied")
        return lead if pinned is None and now is not None else None

    @staticmethod
    def _check_seqs(s: Session, scorer: Scorer, events: list[dict]) -> None:
        """409 (nothing applied) when an event reuses a seq already accepted in this session
        (or earlier in the batch) for a different event, e.g. two tabs numbering from the
        same seq; an identical resend passes (the scorer ignores it as before). Compared in
        the log numbering (after the sample shift); ``conflicts`` are client seqs."""
        accepted, batch, bad = scorer.seqs, {}, set()
        for e in events:
            first = accepted.get(e["seq"]) or batch.setdefault(e["seq"], e)
            if first is not e and not same_event(first, e):
                bad.add(e["seq"])
        if bad:
            ds, st = s.meta.get("seq_offset", 0), s.stats
            conflicts = [q - ds for q in sorted(bad)]
            raise ApiError(409, f"seq {conflicts[:5]} already used in session {s.id!r} by different events "
                                "(another tab or client on this session?); nothing was applied: continue "
                                "after last_seq", conflicts=conflicts, last_seq=st["last_seq"],
                           last_t_ms=st["last_t_ms"])

    async def add_events(self, s: Session, body) -> dict:
        try:
            events, now = parse_post_body(body)
        except EventError as e:
            raise ApiError(400, str(e))
        self._check_session_ids(s, events)
        if any("from_sample" in e for e in events):  # only the server marks sample events
            events = [{k: v for k, v in e.items() if k != "from_sample"} for e in events]
        async with s.lock:
            scorer = await self._ready(s)
            lead = self._check_clock(s, events, now)
            events = s.shift(events)
            self._check_seqs(s, scorer, events)
            # identical resends (a retry, the unload beacon repeating a POST in flight) carry
            # nothing new: neither scored nor logged again
            fresh: dict[int, dict] = {}
            for e in events:
                if e["seq"] not in scorer.seqs:
                    fresh.setdefault(e["seq"], e)
            events = list(fresh.values())
            now_ms = now
            if now is not None:
                now += s.meta.get("t_offset_ms", 0.0)
            # a batch with new events older than ones already applied (concurrent POSTs, an unload
            # beacon overtaking a tick) is scored in log order, as replay and restarts score it
            late = scorer.last_t is not None and any(e["t_ms"] < scorer.last_t for e in events)
            try:
                if late:
                    scorer = await self.run(self._rescore, s, s.params, s.events + events)
                    cov = await self.run(scorer.coverage, now)
                    s.scorer = scorer
                else:
                    cov = await self.run(scorer.add, events, now, validated=True)
                if events:  # the log is the record: a batch it does not hold is not applied
                    await self.run(_append_batch, s.log, events)
            except EventError as e:
                raise ApiError(400, str(e))
            except Exception:
                s.scorer = None  # may hold part of the refused batch: rebuild from the log next time
                self.live.pop(s.id, None)
                raise
            if events:
                s.events.extend(events)
                s.tally(events)
            if lead is not None:
                s.meta["clock_lead_ms"] = round(lead, 1)
                await self.run(s.save_meta)
            s.last_now = now
            if now_ms is not None:
                s.last_now_ms = now_ms if s.last_now_ms is None else max(s.last_now_ms, now_ms)
            cov["session_info"] = s.info()
        return cov

    async def coverage(self, s: Session) -> dict:
        """Coverage as the reader last saw it: while views are open (no end, hidden or
        confirmed finish since), provisional at the latest POST's now_ms like that POST's
        response; else (or before any now_ms since the server started) committed."""
        async with s.lock:
            sc = await self._ready(s)
            now = s.last_now if sc.segmenter.open else None
            cov = await self.run(sc.coverage, now)
            cov["session_info"] = s.info()
            return cov

    async def info(self, s: Session) -> dict:
        async with s.lock:
            await self._ready(s)
            return s.info()

    async def recent(self, case: str, limit: int) -> list[dict]:
        """The case's latest readings, newest first, from their meta and logs (no scoring)."""
        _safe_id(case, "case")
        metas = []
        for path in self.cfg.sessions_dir.glob("*.json"):
            try:
                meta = read_json(path)
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("case") == case and isinstance(meta.get("session"), str):
                metas.append(meta)
        metas.sort(key=lambda m: m.get("created_epoch") or 0.0, reverse=True)
        out = []
        for meta in metas[:limit]:
            s = await self.session(meta["session"])
            async with s.lock:
                if s.events is not None and s.scorer is not None:
                    st, fin = dict(s.stats), s.scorer.finished
                else:  # not loaded, or its scorer is dropped after a refused batch: the log is the record
                    events = await self.run(read_jsonl, s.log) if s.log.exists() else []
                    tmp = Session(s.id, s.case, s.case_dir, s.meta, s.log, s.meta_path)
                    tmp.tally(events)
                    st = tmp.stats
                    # as the scorer: finished once a finish was confirmed
                    fin = next((e for e in events if e.get("kind") == "finish" and e.get("confirmed")), None)
                out.append({"session": s.id, "label": s.meta.get("label"), "from_sample": s.meta.get("from_sample"),
                            "created": s.meta.get("created"), "events": st["events"], "last_t_ms": st["last_t_ms"],
                            "finished": bool(fin and fin.get("confirmed"))})
        return out

    async def gap(self, s: Session, key: str, now: float | None) -> dict:
        async with s.lock:
            scorer = await self._ready(s)
            if key not in scorer.case.key_index:
                raise ApiError(404, f"unknown organ {key!r} for case {s.case!r}")
            return await self.run(scorer.gap, key, s.last_now if now is None else now)

    async def events(self, s: Session) -> list[dict]:
        async with s.lock:
            await self._ready(s)
            return list(s.events)

    async def set_rules(self, s: Session, body) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be an object")
        async with s.lock:
            # merge into the current rules under the lock: overlapping partial updates
            # (different keys) must not overwrite each other
            rules = dict(s.params)
            for k in ("min_dwell_ms", "reviewed_threshold", "skipped_threshold"):
                if k in body:
                    rules[k] = body[k]
            rules = {k: v for k, v in rules.items() if v is not None}
            try:
                make_params(**rules)
            except (TypeError, ValueError) as e:
                raise ApiError(400, str(e))
            await self._ready(s)
            scorer = await self.run(self._rescore, s, rules)
            before = s.meta.get("rules")
            s.meta["rules"] = rules or None
            try:
                s.save_meta()  # stored first: live rules never differ from what a restart reads
            except OSError:
                s.meta["rules"] = before
                raise
            s.scorer = scorer
            cov = await self.run(s.scorer.coverage)
            cov["session_info"] = s.info()
            return cov


# ---- handlers -------------------------------------------------------------------------
APP_KEY = web.AppKey("covmap", App)


def _app(request) -> App:
    return request.app[APP_KEY]


async def health(request):
    a = _app(request)
    orthanc = False
    try:
        async with a.client.get(a.cfg.orthanc.rstrip("/") + "/system",
                                timeout=aiohttp.ClientTimeout(total=1.5)) as r:
            orthanc = r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        pass
    cases = a.cases()
    return _json({"ok": True, "orthanc": orthanc, "cases": len(cases),
                  "segmented": sum(c["segmented"] for c in cases),
                  "ohif_dist": (a.cfg.ohif_dist / "index.html").exists(),
                  "sessions": len(a.sessions), "demo": (a.demo() or {}).get("case")})


async def list_cases(request):
    return _json(_app(request).cases())


async def lookup(request):
    series, study = request.query.get("series"), request.query.get("study")
    if not series and not study:
        raise ApiError(400, "need ?series= or ?study=")
    for c in _app(request).cases():
        if (series and c["series_uid"] == series) or (study and c["study_uid"] == study):
            return _json({"case": c["case"]})
    raise ApiError(404, "no case for that series/study")


def _case_file(rel: str, content_type: str | None = None):
    async def handler(request):
        f = _app(request).case_dir(request.match_info["case"]) / rel
        if not f.exists():
            raise ApiError(404, f"{rel} not available for this case")
        return web.FileResponse(f, headers={"Content-Type": content_type or "application/json",
                                            "Cache-Control": "no-cache"})
    return handler


async def samples(request):
    a = _app(request)
    case = request.match_info["case"]
    a.case_dir(case)
    sdir = a.cfg.samples_dir / case
    out = []
    for f in sorted(sdir.glob("*.jsonl")) if sdir.is_dir() else []:
        mp = f.with_suffix(".json")
        meta = read_json(mp) if mp.exists() else {}
        with open(f) as fh:
            n = sum(1 for line in fh if line.strip())
        out.append({"name": f.stem, "label": meta.get("label", f.stem), "source": meta.get("source"),
                    "description": meta.get("description"), "created": meta.get("created"), "events": n})
    return _json(out)


async def outline(request):
    a = _app(request)
    case = request.match_info["case"]
    d = a.case_dir(case)
    key = request.query.get("organ")
    try:
        k = int(request.query.get("k", ""))
    except ValueError:
        raise ApiError(400, "need integer ?k=")
    if not key:
        raise ApiError(400, "need ?organ=<key>")
    try:
        contours = await a.run(organ_outline, d, key, k)
    except FileNotFoundError:
        raise ApiError(409, "case is not segmented")
    except KeyError as e:
        raise ApiError(404, str(e.args[0]))
    except IndexError as e:
        raise ApiError(400, str(e))
    return _json({"case": case, "organ": key, "k": k, "contours": contours},
                 headers={"Cache-Control": "max-age=3600"})


async def demo(request):
    d = _app(request).demo()
    if d is None:
        raise ApiError(404, "no case available")
    return _json(d)


async def root(request):
    d = _app(request).demo()
    if d is None or not d.get("study_uid"):
        return await static(request)
    raise web.HTTPFound(d["url"])


async def create_session(request):
    body = await _body(request)
    if not isinstance(body, dict):
        raise ApiError(400, "body must be an object {case, label?, from_sample?}")
    return _json(await _app(request).create_session(body))


async def post_events(request):
    a = _app(request)
    s = await a.session(request.match_info["id"])
    return _json(await a.add_events(s, await _body(request)))


async def get_coverage(request):
    a = _app(request)
    return _json(await a.coverage(await a.session(request.match_info["id"])))


async def list_sessions(request):
    case = request.query.get("case")
    if not case:
        raise ApiError(400, "need ?case=<id>")
    try:
        limit = min(max(int(request.query.get("limit", "10")), 1), 50)
    except ValueError:
        raise ApiError(400, "'limit' must be an integer")
    return _json({"case": case, "sessions": await _app(request).recent(case, limit)})


async def get_session(request):
    a = _app(request)
    return _json(await a.info(await a.session(request.match_info["id"])))


async def get_gap(request):
    a = _app(request)
    s = await a.session(request.match_info["id"])
    key = request.query.get("organ")
    if not key:
        raise ApiError(400, "need ?organ=<key>")
    now = request.query.get("now_ms")
    if now is not None:
        try:
            now = float(now)
        except ValueError:
            raise ApiError(400, "now_ms must be a number")
        if not math.isfinite(now):
            raise ApiError(400, "now_ms must be finite")
        now += s.meta.get("t_offset_ms", 0.0)  # client clock -> session time, as for POSTs
    return _json(await a.gap(s, key, now))


async def get_events(request):
    a = _app(request)
    s = await a.session(request.match_info["id"])
    if not s.log.exists():
        return web.Response(body=b"", content_type="application/x-ndjson")
    return web.FileResponse(s.log, headers={"Content-Type": "application/x-ndjson",
                                            "Cache-Control": "no-cache"})


async def post_rules(request):
    a = _app(request)
    s = await a.session(request.match_info["id"])
    return _json(await a.set_rules(s, await _body(request)))


async def get_timeline(request):
    a = _app(request)
    s = await a.session(request.match_info["id"])
    try:
        frames = int(request.query.get("frames", "40"))
    except ValueError:
        raise ApiError(400, "frames must be an integer")
    if not 1 <= frames <= 1000:
        raise ApiError(400, "frames must be 1..1000")
    events = await a.events(s)
    return _json(await a.run(timeline, s.case_dir, events, frames, **s.params))


def _csv(cov: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["organ", "name", "status", "coverage", "seen_voxels", "voxels", "reader", "flagged",
                "note", *REASONS])
    for o in cov["organs"]:
        w.writerow([o["key"], o["name"], o["status"], o["coverage"], o["seen_voxels"], o["voxels"],
                    o.get("reader") or "", int(bool(o.get("flagged"))), o.get("note") or "",
                    *[o["unseen"][r] for r in REASONS]])
    return buf.getvalue()


async def export(request):
    a = _app(request)
    s = await a.session(request.match_info["id"])
    fmt = request.query.get("format", "json")
    disp = 'attachment; filename="{}"'
    if fmt == "jsonl":
        body = s.log.read_bytes() if s.log.exists() else b""
        return web.Response(body=body, content_type="application/x-ndjson",
                            headers={"Content-Disposition": disp.format(f"{s.id}.jsonl")})
    if fmt not in ("json", "csv"):
        raise ApiError(400, "format must be jsonl, json or csv")
    cov = await a.coverage(s)
    if fmt == "json":
        return web.Response(text=json.dumps(cov, indent=1), content_type="application/json",
                            headers={"Content-Disposition": disp.format(f"{s.id}.coverage.json")})
    name = f"{s.id}-from-sample.csv" if s.meta.get("from_sample") else f"{s.id}.csv"
    return web.Response(text=_csv(cov), content_type="text/csv",
                        headers={"Content-Disposition": disp.format(name)})


async def api_not_found(request):
    raise ApiError(404, f"no API route {request.method} {request.path}")


async def _body(request):
    if not request.can_read_body:
        return {}
    try:
        return await request.json(loads=json.loads)
    except json.JSONDecodeError as e:
        raise ApiError(400, f"invalid JSON body: {e.msg}")


# ---- proxy ----------------------------------------------------------------------------
# Read-only (GET/HEAD) DICOMweb and WADO-URI; OHIF never writes (dicomUploadEnabled false).
DICOMWEB_PREFIX, WADO_PATH = "/dicom-web/", "/wado"


def _upstream_target(request) -> str:
    """Orthanc path + query: the fixed prefix plus the raw tail of this request. A dot
    segment ('.' / '..') in the tail at any level of percent-decoding, with '\\' read as
    '/', is refused, so no request can leave the prefix on Orthanc (its root REST API has
    /patients, /tools/shutdown, ...)."""
    path, sep, query = request.raw_path.partition("?")
    if path == WADO_PATH:
        prefix, tail = WADO_PATH, ""
    elif path.startswith(DICOMWEB_PREFIX):
        prefix, tail = DICOMWEB_PREFIX, path[len(DICOMWEB_PREFIX):]
    else:  # e.g. an encoded prefix that only matched the route after decoding
        raise ApiError(400, "proxied paths must start with /dicom-web/ or be /wado")
    level = tail
    for _ in range(4):
        if any(seg in (".", "..") for seg in level.replace("\\", "/").split("/")):
            raise ApiError(400, "dot segments are not allowed in proxied paths")
        decoded = urllib.parse.unquote(level)
        if decoded == level:
            break
        level = decoded
    else:
        raise ApiError(400, "too many levels of percent-encoding in proxied path")
    return prefix + tail + sep + query


async def proxy(request):
    a = _app(request)
    url = URL(a.cfg.orthanc.rstrip("/") + _upstream_target(request), encoded=True)
    headers = CIMultiDict((k, v) for k, v in request.headers.items()
                          if k.lower() not in HOP_BY_HOP | {"host", "content-length"})
    try:
        async with a.client.request(request.method, url, headers=headers, allow_redirects=False) as up:
            resp = web.StreamResponse(status=up.status, reason=up.reason)
            for k, v in up.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    resp.headers.add(k, v)
            await resp.prepare(request)
            try:
                if request.method != "HEAD":
                    async for chunk in up.content.iter_chunked(CHUNK):
                        await resp.write(chunk)
                await resp.write_eof()
            except ConnectionResetError:  # the browser went away mid-stream
                pass
            return resp
    except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as e:
        return _json({"ok": False, "error": f"orthanc unreachable at {a.cfg.orthanc}: {type(e).__name__}"},
                     status=502)


# ---- static ---------------------------------------------------------------------------
def _resolve_under(root: Path, rel: str) -> Path | None:
    try:
        root = root.resolve()
        f = (root / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if f != root and root not in f.parents:
        return None
    return f if f.is_file() else None


def _static_headers(a: App, f: Path) -> dict:
    h = {"Cache-Control": "no-cache" if f.suffix == ".html" else "public, max-age=3600"}
    if a.cfg.cross_origin_isolation:
        h.update({"Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "require-corp"})
    return h


async def render_static(request):
    a = _app(request)
    rel = "render.html" if request.path == "/render.html" else "render/" + request.match_info["tail"]
    roots = [a.cfg.ohif_dist] + ([a.cfg.web_dir] if a.cfg.web_dir else [])
    for root in roots:
        f = _resolve_under(root, rel)
        if f is not None:
            return web.FileResponse(f, headers=_static_headers(a, f))
    # never the SPA fallback: a missing render page must be a clear 404 for `covmap render`
    raise web.HTTPNotFound(text=f"{rel} not found under {', '.join(map(str, roots))} (run scripts/build_ohif.sh)")


async def static(request):
    a = _app(request)
    dist = a.cfg.ohif_dist
    f = _resolve_under(dist, request.path) if request.path != "/" else None
    if f is not None:
        return web.FileResponse(f, headers=_static_headers(a, f))
    index = dist / "index.html"
    accept = request.headers.get("Accept", "")
    looks_like_file = "." in request.path.rsplit("/", 1)[-1]
    if looks_like_file and "text/html" not in accept:
        raise web.HTTPNotFound(text="not found")
    if not index.exists():
        return web.Response(status=503, content_type="text/plain",
                            text=f"OHIF build not found at {dist} (see /api/health)")
    return web.FileResponse(index, headers=_static_headers(a, index))


# ---- app ------------------------------------------------------------------------------
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_HOST_RE = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[^\[\]:/]+))(?::\d{1,5})?$")


def host_allowed(host: str | None) -> bool:
    """Loopback Host headers only, any port (127.0.0.1, localhost, [::1]; the ssh tunnel
    sends 127.0.0.1:8765). A page that rebinds its own DNS name to 127.0.0.1 still sends that
    name, so it can neither read the API nor drive the Orthanc proxy. No Host at all (not a
    browser) is allowed."""
    if host is None:
        return True
    m = _HOST_RE.match(host)
    return bool(m) and (m["v6"] or m["name"]).lower() in LOOPBACK_HOSTS


@web.middleware
async def loopback_host_only(request, handler):
    if not host_allowed(request.headers.get("Host")):
        return _json({"ok": False, "error": "Host not allowed (use 127.0.0.1, localhost or [::1])"},
                     status=403)
    return await handler(request)


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except ApiError as e:
        return _json({"ok": False, "error": str(e), **e.extra}, status=e.status)
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - API clients always get JSON
        if not request.path.startswith("/api/"):
            raise
        return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)


def make_app(cfg: Config | None = None, **overrides) -> web.Application:
    cfg = cfg or Config(**overrides)
    app = web.Application(client_max_size=MAX_BODY, middlewares=[loopback_host_only, errors])
    state = App(cfg)
    app[APP_KEY] = state

    async def startup(_):
        state.client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=64, keepalive_timeout=30),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=300),
            auto_decompress=False)

    async def cleanup(_):
        if state.client is not None:
            await state.client.close()
        state.pool.shutdown(wait=False, cancel_futures=True)

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    r = app.router
    r.add_get("/api/health", health)
    r.add_get("/api/cases", list_cases)
    r.add_get("/api/lookup", lookup)
    r.add_get("/api/demo", demo)
    r.add_get("/api/cases/{case}/organs", _case_file("seg/organs.json"))
    r.add_get("/api/cases/{case}/volume", _case_file("volume.json"))
    r.add_get("/api/cases/{case}/meshes.json", _case_file("seg/meshes.json"))
    r.add_get("/api/cases/{case}/meshes.bin", _case_file("seg/meshes.bin", "application/octet-stream"))
    r.add_get("/api/cases/{case}/samples", samples)
    r.add_get("/api/cases/{case}/outline", outline)
    r.add_post("/api/sessions", create_session)
    r.add_get("/api/sessions", list_sessions)
    r.add_get("/api/sessions/{id}", get_session)
    r.add_post("/api/sessions/{id}/events", post_events)
    r.add_get("/api/sessions/{id}/coverage", get_coverage)
    r.add_get("/api/sessions/{id}/gap", get_gap)
    r.add_get("/api/sessions/{id}/events.jsonl", get_events)
    r.add_post("/api/sessions/{id}/rules", post_rules)
    r.add_get("/api/sessions/{id}/timeline", get_timeline)
    r.add_get("/api/sessions/{id}/export", export)
    r.add_route("*", "/api/{tail:.*}", api_not_found)
    r.add_get("/dicom-web/{tail:.*}", proxy)  # GET and HEAD only; other methods: 405
    r.add_get(WADO_PATH, proxy)
    r.add_get("/render.html", render_static)
    r.add_get("/render/{tail:.*}", render_static)
    r.add_get("/", root)
    r.add_get("/{tail:.*}", static)
    return app


def serve(host: str = "127.0.0.1", port: int = 8765, orthanc: str = "http://127.0.0.1:8042") -> dict:
    """Run the server until interrupted (prints one JSON line once it is listening)."""
    app = make_app(orthanc=orthanc)
    cfg = app[APP_KEY].cfg

    def announce(*_):  # run_app calls its print hook after the port is bound, never on a failed bind
        print(json.dumps({"ok": True, "cmd": "serve", "url": f"http://{host}:{port}/",
                          "data": str(cfg.data_dir), "ohif_dist": str(cfg.ohif_dist),
                          "ohif_dist_present": (cfg.ohif_dist / "index.html").exists(),
                          "orthanc": cfg.orthanc, "pid": os.getpid()}), flush=True)

    web.run_app(app, host=host, port=port, print=announce, shutdown_timeout=5, access_log=None,
                handle_signals=True)
    return {"ok": True, "cmd": "serve", "stopped": True}
