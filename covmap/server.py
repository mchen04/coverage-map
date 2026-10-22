"""App server (docs/SPEC.md "HTTP API"): case data, live scoring sessions, a streaming
reverse proxy to Orthanc for DICOMweb/WADO, and the static OHIF build with SPA fallback.

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
import mimetypes
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from covmap import paths
from covmap.casedata import organ_outline, read_json
from covmap.events import EventError, parse_post_body, read_jsonl, validate_events
from covmap.scoring import REASONS, Scorer, make_params, timeline

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailer", "trailers", "transfer-encoding", "upgrade"}
MAX_BODY = 256 * 1024 * 1024
MAX_LIVE_SESSIONS = int(os.environ.get("COVMAP_MAX_SESSIONS", "16"))
CHUNK = 1 << 16

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/json", ".map")


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
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rules": None,
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
    def _rescore(s: Session, rules: dict) -> Scorer:
        sc = Scorer(s.case_dir, s.id, **rules)
        if s.events:
            sc.add(s.events, validated=True)
        return sc

    async def add_events(self, s: Session, body) -> dict:
        try:
            events, now = parse_post_body(body)
        except EventError as e:
            raise ApiError(400, str(e))
        events = s.shift(events)
        if now is not None:
            now += s.meta.get("t_offset_ms", 0.0)
        async with s.lock:
            scorer = await self._ready(s)
            try:
                cov = await self.run(scorer.add, events, now, validated=True)
            except EventError as e:
                raise ApiError(400, str(e))
            if events:
                await self.run(_write_jsonl, s.log, events)
                s.events.extend(events)
        return cov

    async def coverage(self, s: Session) -> dict:
        async with s.lock:
            return await self.run((await self._ready(s)).coverage)

    async def events(self, s: Session) -> list[dict]:
        async with s.lock:
            await self._ready(s)
            return list(s.events)

    async def set_rules(self, s: Session, body) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be an object")
        rules = dict(s.params)
        for k in ("min_dwell_ms", "reviewed_threshold", "skipped_threshold"):
            if k in body:
                rules[k] = body[k]
        rules = {k: v for k, v in rules.items() if v is not None}
        try:
            make_params(**rules)
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e))
        async with s.lock:
            await self._ready(s)
            s.scorer = await self.run(self._rescore, s, rules)
            s.meta["rules"] = rules or None
            s.save_meta()
            return await self.run(s.scorer.coverage)


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
    return web.Response(text=_csv(cov), content_type="text/csv",
                        headers={"Content-Disposition": disp.format(f"{s.id}.csv")})


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
async def proxy(request):
    a = _app(request)
    url = a.cfg.orthanc.rstrip("/") + request.raw_path
    headers = CIMultiDict((k, v) for k, v in request.headers.items()
                          if k.lower() not in HOP_BY_HOP | {"host", "content-length"})
    data = await request.read() if request.body_exists else None
    try:
        async with a.client.request(request.method, url, headers=headers, data=data,
                                    allow_redirects=False) as up:
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
@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except ApiError as e:
        return _json({"ok": False, "error": str(e)}, status=e.status)
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - API clients always get JSON
        if not request.path.startswith("/api/"):
            raise
        return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)


def make_app(cfg: Config | None = None, **overrides) -> web.Application:
    cfg = cfg or Config(**overrides)
    app = web.Application(client_max_size=MAX_BODY, middlewares=[errors])
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
    r.add_post("/api/sessions/{id}/events", post_events)
    r.add_get("/api/sessions/{id}/coverage", get_coverage)
    r.add_get("/api/sessions/{id}/events.jsonl", get_events)
    r.add_post("/api/sessions/{id}/rules", post_rules)
    r.add_get("/api/sessions/{id}/timeline", get_timeline)
    r.add_get("/api/sessions/{id}/export", export)
    r.add_route("*", "/api/{tail:.*}", api_not_found)
    r.add_route("*", "/dicom-web/{tail:.*}", proxy)
    r.add_route("*", "/wado{tail:.*}", proxy)
    r.add_get("/render.html", render_static)
    r.add_get("/render/{tail:.*}", render_static)
    r.add_get("/", root)
    r.add_get("/{tail:.*}", static)
    return app


def serve(host: str = "127.0.0.1", port: int = 8765, orthanc: str = "http://127.0.0.1:8042") -> dict:
    """Run the server until interrupted (prints one JSON line when listening)."""
    app = make_app(orthanc=orthanc)
    cfg = app[APP_KEY].cfg

    async def announce(_):
        print(json.dumps({"ok": True, "cmd": "serve", "url": f"http://{host}:{port}/",
                          "data": str(cfg.data_dir), "ohif_dist": str(cfg.ohif_dist),
                          "ohif_dist_present": (cfg.ohif_dist / "index.html").exists(),
                          "orthanc": cfg.orthanc, "pid": os.getpid()}), flush=True)

    app.on_startup.append(announce)
    web.run_app(app, host=host, port=port, print=None, shutdown_timeout=5, access_log=None,
                handle_signals=True)
    return {"ok": True, "cmd": "serve", "stopped": True}
