from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from covmap.browser import BrowserServer, precomputed_outline, write_outlines
from covmap.casedata import organ_outline, read_json
from covmap.server import Config, make_app
from covmap.simulate import plan_events, plan_scenario
from synth import build_case

CASE = "synth-browser"


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    root = tmp_path_factory.mktemp("browser") / "data"
    cdir = build_case(root / "cases", CASE)
    write_outlines(cdir)
    return root


def _cfg(data):
    return Config(data_dir=data, ohif_dist=data / "no-dist", demo_case=CASE, clock_tolerance_ms=None)


def _readings(data):
    cdir = data / "cases" / CASE
    events = plan_events(plan_scenario(str(cdir), "window_skip_lung"), read_json(cdir / "volume.json"))
    half = len(events) // 2
    return events[:half], events[half:]


def _strip(body, sid: str):
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "timing_ms"}
    return json.loads(json.dumps(body).replace(sid, "SID"))


async def _over_http(data):
    out = []
    async with TestClient(TestServer(make_app(_cfg(data)))) as c:
        sid = (await (await c.post("/api/sessions", json={"case": CASE})).json())["session"]
        for chunk in _readings(data):
            chunk = [{**e, "session": sid} for e in chunk]
            r = await c.post(f"/api/sessions/{sid}/events", json={"events": chunk, "now_ms": chunk[-1]["t_ms"]})
            out.append((r.status, _strip(await r.json(), sid)))
        for path in (f"/api/sessions/{sid}/gap?organ=liver", f"/api/sessions/{sid}/timeline?frames=5",
                     "/api/cases", "/api/lookup", f"/api/sessions/{sid}/rules", "/api/nope"):
            r = await c.get(path)
            out.append((r.status, _strip(await r.json(), sid)))
    return out


async def _in_browser(data):
    out = []
    srv = BrowserServer(_cfg(data))
    await srv.start()

    async def call(method, path, body=None):
        status, headers, raw = await srv.handle(method, path, json.dumps(body).encode() if body is not None else None)
        assert headers["Content-Type"].startswith("application/json")
        return status, json.loads(raw)

    sid = (await call("POST", "/api/sessions", {"case": CASE}))[1]["session"]
    for chunk in _readings(data):
        chunk = [{**e, "session": sid} for e in chunk]
        status, body = await call("POST", f"/api/sessions/{sid}/events", {"events": chunk, "now_ms": chunk[-1]["t_ms"]})
        out.append((status, _strip(body, sid)))
    for path in (f"/api/sessions/{sid}/gap?organ=liver", f"/api/sessions/{sid}/timeline?frames=5",
                 "/api/cases", "/api/lookup", f"/api/sessions/{sid}/rules", "/api/nope"):
        status, body = await call("GET", path)
        out.append((status, _strip(body, sid)))
    return out


def test_browser_server_answers_like_the_http_server(data):
    http = asyncio.run(_over_http(data))
    browser = asyncio.run(_in_browser(data))
    assert [s for s, _ in browser] == [200, 200, 200, 200, 200, 400, 404, 404]
    assert browser == http


def test_precomputed_outlines_match_live_contours(data):
    cdir = data / "cases" / CASE
    n_slices = read_json(cdir / "volume.json")["n_slices"]
    organs = read_json(cdir / "seg" / "organs.json")["organs"]
    drawn = 0
    for organ in organs:
        for k in range(n_slices):
            live = organ_outline(cdir, organ["key"], k)
            assert precomputed_outline(cdir, organ["key"], k) == live
            drawn += bool(live)
    assert drawn > 0
    with pytest.raises(KeyError):
        precomputed_outline(cdir, "no_such_organ", 0)
    with pytest.raises(IndexError):
        precomputed_outline(cdir, organs[0]["key"], n_slices)


def test_browser_outline_route_serves_the_precomputed_table(data):
    async def main():
        srv = BrowserServer(_cfg(data))
        await srv.start()
        cdir = data / "cases" / CASE
        key = read_json(cdir / "seg" / "organs.json")["organs"][0]["key"]
        k = next(int(k) for k in read_json(cdir / "seg" / "outlines.json")[key])
        status, _, raw = await srv.handle("GET", f"/api/cases/{CASE}/outline?organ={key}&k={k}")
        assert status == 200 and json.loads(raw)["contours"] == organ_outline(cdir, key, k)
        status, _, _ = await srv.handle("GET", f"/api/cases/{CASE}/outline?organ=nope&k={k}")
        assert status == 404
    asyncio.run(main())
