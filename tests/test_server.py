"""HTTP API (docs/SPEC.md) via aiohttp test servers, plus a real ``covmap serve`` process."""

import asyncio
import csv
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from covmap.server import Config, make_app
from covmap.simulate import plan_events, plan_scenario
from synth import build_case, end, read_pass, stack_view

CASE = "synth-srv"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("srv")
    data = root / "data"
    cdir = build_case(data / "cases", CASE)
    (cdir / "seg" / "meshes.json").write_text(json.dumps({"organs": [], "bounds_lps": [0] * 6}))
    (cdir / "seg" / "meshes.bin").write_bytes(np.arange(64, dtype=np.float32).tobytes())
    unseg = data / "cases" / "unsegmented"
    unseg.mkdir(parents=True)
    (unseg / "volume.json").write_text(json.dumps({"case": "unsegmented", "study_uid": "5.5", "series_uid": "5.6",
                                                   "n_slices": 3}))
    vol = json.loads((cdir / "volume.json").read_text())
    sdir = data / "samples" / CASE
    sdir.mkdir(parents=True)
    events = plan_events(plan_scenario(str(cdir), "window_skip_lung"), vol)
    (sdir / "skip-lungs.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (sdir / "skip-lungs.json").write_text(json.dumps({"label": "Lung window skipped", "source": "simulated",
                                                      "description": "test sample", "created": "2026-10-21"}))
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>ohif</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "assets" / "core.wasm").write_bytes(b"\0asm")
    (dist / "render").mkdir()  # installed into the dist by scripts/build_ohif.sh
    (dist / "render.html").write_text("<html>render</html>")
    (dist / "render" / "render.js").write_text("// render")
    webdir = root / "web"  # optional COVMAP_WEB_DIR fallback
    (webdir / "render").mkdir(parents=True)
    (webdir / "render.html").write_text("<html>shadowed</html>")
    (webdir / "render" / "extra.js").write_text("// extra")
    return SimpleNamespace(root=root, data=data, cdir=cdir, vol=vol, dist=dist, web=webdir,
                           sample_events=events)


def _cfg(env, **kw):
    base = dict(orthanc="http://127.0.0.1:9", data_dir=env.data, ohif_dist=env.dist, web_dir=env.web,
                demo_case="pancreas-0051")
    return Config(**{**base, **kw})


def run_client(cfg, fn):
    async def main():
        async with TestClient(TestServer(make_app(cfg))) as c:
            return await fn(c)
    return asyncio.run(main())


async def _json(resp, status=200):
    assert resp.status == status, (resp.status, await resp.text())
    return await resp.json()


# ---- cases ----------------------------------------------------------------------------
def test_health_cases_lookup(env):
    async def fn(c):
        h = await _json(await c.get("/api/health"))
        assert h == {**h, "ok": True, "orthanc": False, "cases": 2, "segmented": 1, "ohif_dist": True,
                     "demo": CASE}
        cases = await _json(await c.get("/api/cases"))
        synth = next(x for x in cases if x["case"] == CASE)
        assert synth["organs"] == 12 and synth["n_slices"] == 40 and synth["segmented"]
        assert synth["series_uid"] == env.vol["series_uid"]
        assert (await _json(await c.get("/api/lookup", params={"series": env.vol["series_uid"]}))) == {"case": CASE}
        assert (await _json(await c.get("/api/lookup", params={"study": env.vol["study_uid"]}))) == {"case": CASE}
        await _json(await c.get("/api/lookup", params={"series": "1.2.3"}), 404)
        await _json(await c.get("/api/lookup"), 400)
    run_client(_cfg(env), fn)


def test_case_files(env):
    async def fn(c):
        organs = await _json(await c.get(f"/api/cases/{CASE}/organs"))
        assert organs == json.loads((env.cdir / "seg" / "organs.json").read_text())
        assert (await _json(await c.get(f"/api/cases/{CASE}/volume")))["n_slices"] == 40
        assert (await _json(await c.get(f"/api/cases/{CASE}/meshes.json")))["organs"] == []
        r = await c.get(f"/api/cases/{CASE}/meshes.bin")
        assert r.status == 200 and r.headers["Content-Type"] == "application/octet-stream"
        assert await r.read() == (env.cdir / "seg" / "meshes.bin").read_bytes()
        await _json(await c.get("/api/cases/nope/organs"), 404)
        await _json(await c.get("/api/cases/unsegmented/organs"), 404)
        await _json(await c.get("/api/cases/bad%20id/volume"), 400)
        await _json(await c.get("/api/not-a-route"), 404)
    run_client(_cfg(env), fn)


def test_outline(env):
    from covmap.casedata import load_case

    case = load_case(env.cdir)
    lid = case.organ("liver")["id"]
    k = int(np.argmax((case.labels == lid).sum(axis=(1, 2))))

    async def fn(c):
        o = await _json(await c.get(f"/api/cases/{CASE}/outline", params={"organ": "liver", "k": k}))
        assert o["organ"] == "liver" and o["k"] == k and len(o["contours"]) >= 1
        pts = np.array([p for cnt in o["contours"] for p in cnt])
        r, cc = np.nonzero(case.labels[k] == lid)
        assert pts[:, 0].min() >= r.min() - 0.5 and pts[:, 0].max() <= r.max() + 0.5
        assert pts[:, 1].min() >= cc.min() - 0.5 and pts[:, 1].max() <= cc.max() + 0.5
        lung_free = int(np.flatnonzero(~(case.labels == case.organ("lung_left")["id"]).any(axis=(1, 2)))[0])
        assert (await _json(await c.get(f"/api/cases/{CASE}/outline",
                                        params={"organ": "lung_left", "k": lung_free})))["contours"] == []
        await _json(await c.get(f"/api/cases/{CASE}/outline", params={"organ": "nope", "k": 1}), 404)
        await _json(await c.get(f"/api/cases/{CASE}/outline", params={"organ": "liver", "k": 999}), 400)
        await _json(await c.get(f"/api/cases/{CASE}/outline", params={"organ": "liver"}), 400)
    run_client(_cfg(env), fn)


# ---- sessions -------------------------------------------------------------------------
def test_session_lifecycle(env):
    vol, K = env.vol, 40

    async def fn(c):
        s = await _json(await c.post("/api/sessions", json={"case": CASE, "label": "t1"}))
        sid = s["session"]
        assert s["case"] == CASE and sid.startswith("s-")
        evs = read_pass(vol, range(K), 0, 0, "soft", source="ohif")
        cov = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": evs[:20], "now_ms": 20 * 150 + 50}))
        assert cov["provisional"] and cov["session"] == sid and cov["events"]["total"] == 20
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": evs[20:] + [end(vol, K * 150, K)]}))
        liver = next(o for o in cov["organs"] if o["key"] == "liver")
        assert liver["status"] == "reviewed"
        committed = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert not committed["provisional"] and committed["organs"] == cov["organs"]
        # malformed batch: 400 and nothing persisted
        bad = await c.post(f"/api/sessions/{sid}/events", json={"events": [{"v": 1, "kind": "view"}]})
        assert bad.status == 400 and "seq" in (await bad.json())["error"]
        bad = await c.post(f"/api/sessions/{sid}/events", data=b"{nope", headers={"Content-Type": "application/json"})
        assert bad.status == 400
        log = await (await c.get(f"/api/sessions/{sid}/events.jsonl")).text()
        assert len(log.splitlines()) == K + 1
        assert (env.data / "sessions" / f"{sid}.jsonl").read_text() == log
        # rules re-score the whole session and persist
        cov = await _json(await c.post(f"/api/sessions/{sid}/rules", json={"min_dwell_ms": 200}))
        assert cov["params"]["min_dwell_ms"] == 200 and cov["summary"]["reviewed"] == 0
        meta = json.loads((env.data / "sessions" / f"{sid}.json").read_text())
        assert meta["rules"] == {"min_dwell_ms": 200} and meta["label"] == "t1"
        await _json(await c.post(f"/api/sessions/{sid}/rules", json={"reviewed_threshold": 2}), 400)
        cov = await _json(await c.post(f"/api/sessions/{sid}/rules", json={"min_dwell_ms": None}))
        assert cov["params"]["min_dwell_ms"] == 80 and cov["summary"]["reviewed"] > 0
        # timeline
        tl = await _json(await c.get(f"/api/sessions/{sid}/timeline", params={"frames": 10}))
        assert len(tl["frames"]) == 10 and tl["frames"][-1]["statuses"]["liver"] == "reviewed"
        assert set(tl["frames"][0]) >= {"t_ms", "summary", "statuses"}
        await _json(await c.get(f"/api/sessions/{sid}/timeline", params={"frames": 0}), 400)
        # exports
        r = await c.get(f"/api/sessions/{sid}/export", params={"format": "jsonl"})
        assert r.headers["Content-Disposition"] == f'attachment; filename="{sid}.jsonl"'
        assert (await r.text()) == log
        r = await c.get(f"/api/sessions/{sid}/export", params={"format": "json"})
        assert "attachment" in r.headers["Content-Disposition"]
        assert json.loads(await r.text())["organs"] == committed["organs"]
        r = await c.get(f"/api/sessions/{sid}/export", params={"format": "csv"})
        rows = list(csv.DictReader(io.StringIO(await r.text())))
        assert r.headers["Content-Type"].startswith("text/csv") and len(rows) == 12
        assert set(rows[0]) >= {"organ", "status", "coverage", "reader", "flagged", "note", "short_dwell",
                                "wrong_window", "out_of_region", "never_displayed"}
        await _json(await c.get(f"/api/sessions/{sid}/export", params={"format": "xml"}), 400)
        await _json(await c.get("/api/sessions/s-nope/coverage"), 404)
        await _json(await c.post("/api/sessions", json={"case": "unsegmented"}), 409)
        await _json(await c.post("/api/sessions", json={"case": "nope"}), 404)
        await _json(await c.post("/api/sessions", json={}), 400)
        return sid, committed

    sid, committed = run_client(_cfg(env), fn)

    async def rehydrate(c):  # a fresh server rebuilds the session from disk
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert cov["organs"] == committed["organs"]
    run_client(_cfg(env), rehydrate)


def test_samples_and_from_sample(env):
    async def fn(c):
        samples = await _json(await c.get(f"/api/cases/{CASE}/samples"))
        assert samples == [{"name": "skip-lungs", "label": "Lung window skipped", "source": "simulated",
                            "description": "test sample", "created": "2026-10-21",
                            "events": len(env.sample_events)}]
        s = await _json(await c.post("/api/sessions", json={"case": CASE, "from_sample": "skip-lungs"}))
        n = len(env.sample_events)
        assert s["prefilled_events"] == n and s["seq_offset"] == n
        assert s["t_offset_ms"] == env.sample_events[-1]["t_ms"] + 1
        sid = s["session"]
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        st = {o["key"]: o["status"] for o in cov["organs"]}
        assert st["lung_left"] == st["lung_right"] == "skipped" and st["liver"] == "reviewed"
        assert cov["events"]["sources"] == {"simulated": n}
        # the client's own clock and seq restart at 0; the server shifts them after the sample
        k = int(next(o for o in json.loads((env.cdir / "seg" / "organs.json").read_text())["organs"]
                     if o["key"] == "lung_left")["center_k"])
        live = [stack_view(env.vol, k, 0, 0, "lung", source="ohif"), end(env.vol, 500, 1, source="ohif")]
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": live}))
        assert cov["events"]["total"] == n + 2 and cov["events"]["ignored"]["other"] == 0
        assert next(o for o in cov["organs"] if o["key"] == "lung_left")["seen_voxels"] > 0
        lines = [json.loads(x) for x in (env.data / "sessions" / f"{sid}.jsonl").read_text().splitlines()]
        assert all(e["from_sample"] == "skip-lungs" and e["source"] == "simulated" for e in lines[:n])
        assert lines[n]["seq"] == n and lines[n]["t_ms"] == s["t_offset_ms"] and lines[n]["source"] == "ohif"
        await _json(await c.post("/api/sessions", json={"case": CASE, "from_sample": "nope"}), 404)
        await _json(await c.get("/api/cases/nope/samples"), 404)
    run_client(_cfg(env), fn)


def test_concurrent_posts(env):
    vol = env.vol

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        batches = [[stack_view(vol, i % 40, i * 1000 + j * 100, i * 10 + j, viewport=f"vp{i}") for j in range(10)]
                   for i in range(20)]
        resps = await asyncio.gather(*[c.post(f"/api/sessions/{sid}/events", json={"events": b}) for b in batches])
        assert all(r.status == 200 for r in resps)
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert cov["events"]["total"] == 200 and cov["events"]["views"] == 200
        log = (env.data / "sessions" / f"{sid}.jsonl").read_text().splitlines()
        assert len(log) == 200
    run_client(_cfg(env), fn)


# ---- demo and static ------------------------------------------------------------------
def test_demo_root_and_static(env):
    async def fn(c):
        d = await _json(await c.get("/api/demo"))
        assert d == {"case": CASE, "study_uid": env.vol["study_uid"],
                     "url": f"/coverage?StudyInstanceUIDs={env.vol['study_uid']}"}
        r = await c.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == d["url"]
        r = await c.get("/assets/app.js")
        assert r.status == 200 and "javascript" in r.headers["Content-Type"]
        r = await c.get("/assets/core.wasm")
        assert r.headers["Content-Type"] == "application/wasm"
        r = await c.get("/coverage?StudyInstanceUIDs=1.2.3", headers={"Accept": "text/html"})
        assert r.status == 200 and await r.text() == "<html>ohif</html>"
        r = await c.get("/viewer/1.2.840.10008.1", headers={"Accept": "text/html,*/*"})
        assert await r.text() == "<html>ohif</html>"
        assert (await c.get("/assets/missing.js")).status == 404
        assert (await c.get("/../../etc/passwd", headers={"Accept": "text/html"})).status in (200, 404)
        assert await (await c.get("/render.html")).text() == "<html>render</html>"  # dist wins
        assert await (await c.get("/render/render.js")).text() == "// render"
        assert await (await c.get("/render/extra.js")).text() == "// extra"  # COVMAP_WEB_DIR fallback
        assert (await c.get("/render/nope.js", headers={"Accept": "text/html"})).status == 404
    run_client(_cfg(env), fn)

    async def dist_only(c):
        assert await (await c.get("/render/render.js")).text() == "// render"
        assert (await c.get("/render/extra.js")).status == 404
    run_client(_cfg(env, web_dir=None), dist_only)


def test_demo_case_env(env):
    async def fn(c):
        assert (await _json(await c.get("/api/demo")))["case"] == CASE
    run_client(_cfg(env, demo_case=CASE), fn)

    async def none(c):
        await _json(await c.get("/api/demo"), 404)
        r = await c.get("/", headers={"Accept": "text/html"})
        assert r.status == 200 and await r.text() == "<html>ohif</html>"
    run_client(_cfg(env, data_dir=env.root / "empty"), none)


def test_starts_without_dist_or_cases(env, tmp_path):
    async def fn(c):
        h = await _json(await c.get("/api/health"))
        assert h["ok"] and h["ohif_dist"] is False and h["cases"] == 0
        r = await c.get("/viewer", headers={"Accept": "text/html"})
        assert r.status == 503 and "OHIF build not found" in await r.text()
        assert await _json(await c.get("/api/cases")) == []
    run_client(_cfg(env, data_dir=tmp_path / "nodata", ohif_dist=tmp_path / "nodist"), fn)


# ---- proxy ----------------------------------------------------------------------------
BIG = np.random.default_rng(0).integers(0, 256, 5 * 1024 * 1024 + 123, dtype=np.uint8).tobytes()
BOUNDARY = "covmap-test-boundary-123"


def _upstream_app():
    async def system(request):
        return web.json_response({"Name": "fake-orthanc"})

    async def echo(request):
        body = await request.read()
        return web.json_response({"method": request.method, "path": request.path, "query": request.query_string,
                                  "accept": request.headers.get("Accept"), "host": request.headers.get("Host"),
                                  "content_type": request.headers.get("Content-Type"), "len": len(body),
                                  "sha": hashlib.sha256(body).hexdigest()},
                                 headers={"X-Upstream": "1"})

    async def frames(request):
        resp = web.StreamResponse(headers={
            "Content-Type": f'multipart/related; type="application/octet-stream"; boundary={BOUNDARY}'})
        await resp.prepare(request)
        payload = (f"--{BOUNDARY}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + BIG + \
            f"\r\n--{BOUNDARY}--\r\n".encode()
        for i in range(0, len(payload), 100_000):
            await resp.write(payload[i:i + 100_000])
        await resp.write_eof()
        return resp

    async def wado(request):
        return web.Response(body=b"DICM" * 10, content_type="application/dicom")

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/system", system)
    app.router.add_get("/dicom-web/studies/1/series/2/instances/3/frames/1", frames)
    app.router.add_route("*", "/dicom-web/studies", echo)
    app.router.add_get("/wado", wado)
    return app


def test_proxy(env):
    async def main():
        up = TestServer(_upstream_app())
        await up.start_server()
        try:
            cfg = _cfg(env, orthanc=str(up.make_url("")).rstrip("/"))
            async with TestClient(TestServer(make_app(cfg))) as c:
                assert (await _json(await c.get("/api/health")))["orthanc"] is True
                r = await c.get("/dicom-web/studies?limit=5&includefield=00081030",
                                headers={"Accept": "application/dicom+json"})
                e = await _json(r)
                assert e["query"] == "limit=5&includefield=00081030" and e["accept"] == "application/dicom+json"
                assert e["path"] == "/dicom-web/studies" and r.headers["X-Upstream"] == "1"
                r = await c.get("/dicom-web/studies/1/series/2/instances/3/frames/1",
                                headers={"Accept": 'multipart/related; type="application/octet-stream"'})
                assert r.status == 200 and f"boundary={BOUNDARY}" in r.headers["Content-Type"]
                body = await r.read()
                assert BIG in body and body.startswith(f"--{BOUNDARY}".encode())
                post = BIG[:3_000_000]
                e = await _json(await c.post("/dicom-web/studies", data=post,
                                             headers={"Content-Type": "application/dicom"}))
                assert e["len"] == len(post) and e["sha"] == hashlib.sha256(post).hexdigest()
                assert e["content_type"] == "application/dicom" and e["method"] == "POST"
                r = await c.get("/wado?requestType=WADO&studyUID=1")
                assert r.status == 200 and r.headers["Content-Type"] == "application/dicom"
                assert await r.read() == b"DICM" * 10
                assert (await c.get("/dicom-web/nothing-here")).status == 404
                r = await c.head("/dicom-web/studies")
                assert r.status == 200
                results = await asyncio.gather(*[c.get("/dicom-web/studies/1/series/2/instances/3/frames/1")
                                                 for _ in range(6)])
                assert [len(await x.read()) for x in results] == [len(body)] * 6
        finally:
            await up.close()
        async with TestClient(TestServer(make_app(_cfg(env, orthanc="http://127.0.0.1:9")))) as c:
            r = await c.get("/dicom-web/studies")
            assert r.status == 502 and not (await r.json())["ok"]
    asyncio.run(main())


# ---- real process ---------------------------------------------------------------------
def test_serve_process_starts_and_stops_cleanly(env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "covmap", "serve", "--port", str(port), "--orthanc", "http://127.0.0.1:9"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "COVMAP_DATA": str(env.data), "COVMAP_OHIF_DIST": str(env.dist)})
    try:
        line = json.loads(proc.stdout.readline())
        assert line["ok"] and line["cmd"] == "serve" and line["ohif_dist_present"]
        deadline = time.time() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    h = json.load(r)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        assert h["ok"] and h["cases"] == 2
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_scorer_eviction_and_reload(env, monkeypatch):
    import covmap.server as server

    monkeypatch.setattr(server, "MAX_LIVE_SESSIONS", 1)
    vol = env.vol

    async def fn(c):
        app = c.server.app[server.APP_KEY]
        sids = [(await _json(await c.post("/api/sessions", json={"case": CASE})))["session"] for _ in range(3)]
        expected = {}
        for i, sid in enumerate(sids):
            evs = read_pass(vol, range(i * 10, i * 10 + 10), 0, 0, "soft") + [end(vol, 1500, 10)]
            expected[sid] = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": evs}))
        assert list(app.live) == [sids[-1]] and app.sessions[sids[0]].scorer is None
        for sid in sids:  # evicted sessions rebuild from their logs with identical results
            cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
            assert cov["organs"] == expected[sid]["organs"] and cov["events"]["total"] == 11
        more = [stack_view(vol, 35, 2000, 11), end(vol, 2200, 12)]
        cov = await _json(await c.post(f"/api/sessions/{sids[0]}/events", json={"events": more}))
        assert cov["events"]["total"] == 13
    run_client(_cfg(env), fn)
