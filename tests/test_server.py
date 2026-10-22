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
from synth import build_case, end, finish, read_pass, stack_view

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
    # synthetic logs are posted much faster than real time: the live clock guard is tested
    # on its own (test_live_post_guards)
    base = dict(orthanc="http://127.0.0.1:9", data_dir=env.data, ohif_dist=env.dist, web_dir=env.web,
                demo_case="pancreas-0051", clock_tolerance_ms=None)
    return Config(**{**base, **kw})


def _to(sid, events):
    """Synthetic events as the client posts them: stamped with the session id."""
    return [{**e, "session": sid} for e in events]


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
        evs = _to(sid, read_pass(vol, range(K), 0, 0, "soft", source="ohif") + [end(vol, K * 150, K)])
        cov = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": evs[:20], "now_ms": 20 * 150 + 50}))
        assert cov["provisional"] and cov["session"] == sid and cov["events"]["total"] == 20
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": evs[20:]}))
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
        assert set(tl["frames"][0]) >= {"t_ms", "summary", "statuses", "view"}
        assert tl["frames"][-1]["view"] == {"sop": vol["sop_uids"][K - 1], "k": K - 1,
                                            "voi": {"wc": 40.0, "ww": 400.0}, "window": "soft"}
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
        live = _to(sid, [stack_view(env.vol, k, 0, 0, "lung", source="ohif"), end(env.vol, 500, 1, source="ohif")])
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
        batches = [_to(sid, [stack_view(vol, i % 40, i * 1000 + j * 100, i * 10 + j, viewport=f"vp{i}")
                             for j in range(10)]) for i in range(20)]
        resps = await asyncio.gather(*[c.post(f"/api/sessions/{sid}/events", json={"events": b}) for b in batches])
        assert all(r.status == 200 for r in resps)
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert cov["events"]["total"] == 200 and cov["events"]["views"] == 200
        log = (env.data / "sessions" / f"{sid}.jsonl").read_text().splitlines()
        assert len(log) == 200
    run_client(_cfg(env), fn)


def test_concurrent_partial_rules_keep_every_update(env):
    """A partial rules POST is merged into the session's current rules under the session
    lock, so overlapping updates of different keys are never lost."""
    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        for i in range(8):
            m, r = 100.0 + i, 0.8 + i / 100
            resps = await asyncio.gather(c.post(f"/api/sessions/{sid}/rules", json={"min_dwell_ms": m}),
                                         c.post(f"/api/sessions/{sid}/rules", json={"reviewed_threshold": r}))
            assert [x.status for x in resps] == [200, 200]
            p = (await _json(await c.get(f"/api/sessions/{sid}/coverage")))["params"]
            assert (p["min_dwell_ms"], p["reviewed_threshold"]) == (m, r), i
            meta = json.loads((env.data / "sessions" / f"{sid}.json").read_text())
            assert meta["rules"] == {"min_dwell_ms": m, "reviewed_threshold": r}, i
        # an invalid merge is still a 400 and changes nothing
        await _json(await c.post(f"/api/sessions/{sid}/rules", json={"skipped_threshold": 0.95}), 400)
        p = (await _json(await c.get(f"/api/sessions/{sid}/coverage")))["params"]
        assert p["skipped_threshold"] == 0.05 and p["min_dwell_ms"] == 107.0
    run_client(_cfg(env), fn)


def test_session_info_everywhere_and_export_origin(env):
    vol = env.vol
    n = len(env.sample_events)
    out = {}

    async def fn(c):
        await _json(await c.get("/api/sessions/s-nope"), 404)
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE, "label": "info"})))["session"]
        info = await _json(await c.get(f"/api/sessions/{sid}"))
        created = info.pop("created")
        assert isinstance(created, str) and created[:4].isdigit()
        assert info == {"session": sid, "case": CASE, "label": "info", "from_sample": None, "sample_events": 0,
                        "finished": False, "events": 0, "last_seq": None, "last_t_ms": None, "last_now_ms": None,
                        "rules": {},
                        "t_offset_ms": 0.0, "seq_offset": 0, "last_view": None}
        evs = _to(sid, read_pass(vol, range(10), 0, 0) + [finish(vol, 1600, 10, confirmed=False)])
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": evs, "now_ms": 1700}))
        info = await _json(await c.get(f"/api/sessions/{sid}"))
        assert cov["session_info"] == info
        assert (info["events"], info["last_seq"], info["last_t_ms"], info["finished"]) == (11, 10, 1600.0, False)
        assert info["last_now_ms"] == 1700  # a takeover continues after it (C23)
        cov = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": _to(sid, [finish(vol, 1800, 11), end(vol, 1900, 12)])}))
        assert cov["session_info"]["finished"] and cov["session_info"]["last_seq"] == 12
        assert cov["session_info"]["last_now_ms"] == 1700  # kept (the highest) when a POST has no now_ms
        cov = await _json(await c.post(f"/api/sessions/{sid}/rules", json={"min_dwell_ms": 100}))
        assert cov["session_info"]["rules"] == {"min_dwell_ms": 100}
        info = await _json(await c.get(f"/api/sessions/{sid}"))
        assert (await _json(await c.get(f"/api/sessions/{sid}/coverage")))["session_info"] == info == cov["session_info"]
        r = await c.get(f"/api/sessions/{sid}/export", params={"format": "json"})
        assert json.loads(await r.text())["session_info"] == info
        r = await c.get(f"/api/sessions/{sid}/export", params={"format": "csv"})
        assert r.headers["Content-Disposition"] == f'attachment; filename="{sid}.csv"'
        # started from a sample: sample events counted apart, client numbering for the live part
        s = await _json(await c.post("/api/sessions", json={"case": CASE, "from_sample": "skip-lungs"}))
        sid2 = s["session"]
        info = await _json(await c.get(f"/api/sessions/{sid2}"))
        assert (info["from_sample"], info["sample_events"], info["events"]) == ("skip-lungs", n, n)
        assert (info["last_seq"], info["last_t_ms"], info["seq_offset"]) == (None, None, n)
        assert info["t_offset_ms"] == s["t_offset_ms"] and info["label"] == "Lung window skipped"
        live = _to(sid2, [stack_view(vol, 20, 0, 0, "lung", source="ohif"), end(vol, 500, 1, source="ohif")])
        live[0]["from_sample"] = "forged"  # only the server marks sample events
        cov = await _json(await c.post(f"/api/sessions/{sid2}/events", json={"events": live}))
        info = cov["session_info"]
        assert (info["sample_events"], info["events"], info["last_seq"], info["last_t_ms"]) == (n, n + 2, 1, 500.0)
        assert info["last_now_ms"] is None  # that POST carried no now_ms
        cov = await _json(await c.post(f"/api/sessions/{sid2}/events", json={"events": [], "now_ms": 650}))
        info = cov["session_info"]
        assert (info["last_t_ms"], info["last_now_ms"]) == (500.0, 650)  # client numbering, not the sample shift
        assert "from_sample" not in json.loads((env.data / "sessions" / f"{sid2}.jsonl").read_text().splitlines()[n])
        r = await c.get(f"/api/sessions/{sid2}/export", params={"format": "csv"})
        assert r.headers["Content-Disposition"] == f'attachment; filename="{sid2}-from-sample.csv"'
        r = await c.get(f"/api/sessions/{sid2}/export", params={"format": "json"})
        assert json.loads(await r.text())["session_info"] == info
        out.update({sid: await _json(await c.get(f"/api/sessions/{sid}")), sid2: info})
    run_client(_cfg(env), fn)

    async def rehydrate(c):  # rebuilt from the log and meta by a fresh server (now_ms is not logged)
        for sid, info in out.items():
            assert await _json(await c.get(f"/api/sessions/{sid}")) == {**info, "last_now_ms": None}
    run_client(_cfg(env), rehydrate)


def test_gap_endpoint(env):
    from covmap.casedata import load_case
    from covmap.scoring import Scorer

    vol = env.vol
    case = load_case(env.cdir)
    n_k = case.organ_slice_counts[case.key_index["liver"]]
    ks = [int(k) for k in np.flatnonzero(n_k)]
    skip = max(ks, key=lambda k: n_k[k])

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        gap = f"/api/sessions/{sid}/gap"
        await _json(await c.get(gap), 400)
        await _json(await c.get(gap, params={"organ": "nope"}), 404)
        await _json(await c.get(gap, params={"organ": "liver", "now_ms": "soon"}), 400)
        await _json(await c.get(gap, params={"organ": "liver", "now_ms": "inf"}), 400)
        await _json(await c.get("/api/sessions/s-nope/gap", params={"organ": "liver"}), 404)
        g = await _json(await c.get(gap, params={"organ": "liver"}))
        assert g == Scorer(case).gap("liver") and g["unseen_voxels"] == int(n_k.sum())
        assert g["ranges"] == [[ks[0], ks[-1]]] and g["window"] == "soft" and g["sop"] == vol["sop_uids"][g["k"]]
        evs = read_pass(vol, [k for k in ks if k != skip], 0, 0)
        t = len(evs) * 150.0
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": _to(sid, evs + [end(vol, t, len(evs))])}))
        g = await _json(await c.get(gap, params={"organ": "liver"}))
        assert (g["k"], g["ranges"], g["unseen_voxels"]) == (skip, [[skip, skip]], int(n_k[skip]))
        # on the gap slice now: the gap follows the provisional state at the last POST's now_ms
        await _json(await c.post(f"/api/sessions/{sid}/events",
                                 json={"events": _to(sid, [stack_view(vol, skip, t + 10, len(evs) + 1)]),
                                       "now_ms": t + 20}))
        assert (await _json(await c.get(gap, params={"organ": "liver"})))["k"] == skip
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": [], "now_ms": t + 300}))
        g = await _json(await c.get(gap, params={"organ": "liver"}))
        assert (g["k"], g["sop"], g["ranges"], g["unseen_voxels"]) == (None, None, [], 0)
        assert (await _json(await c.get(gap, params={"organ": "liver", "now_ms": t + 30})))["k"] == skip
        # sample session: ?now_ms is the client clock, shifted after the sample like POSTs
        s = await _json(await c.post("/api/sessions", json={"case": CASE, "from_sample": "skip-lungs"}))
        sid2, key = s["session"], "lung_left"
        k = max(range(n_k.size), key=lambda i: case.organ_slice_counts[case.key_index[key]][i])
        await _json(await c.post(f"/api/sessions/{sid2}/events",
                                 json={"events": _to(sid2, [stack_view(vol, k, 0, 0, "lung")]), "now_ms": 0}))
        g2 = f"/api/sessions/{sid2}/gap"
        at0 = await _json(await c.get(g2, params={"organ": key}))
        at500 = await _json(await c.get(g2, params={"organ": key, "now_ms": 500}))
        n_lung = int(case.organ_slice_counts[case.key_index[key]][k])
        assert at0["unseen_voxels"] - at500["unseen_voxels"] == n_lung > 0 and at0["k"] == k != at500["k"]
    run_client(_cfg(env), fn)


def test_live_post_guards(env, monkeypatch):
    """Live POSTs refuse events of another session and events running ahead of the
    session's clock (400, nothing applied); sample sessions take the client's own clock."""
    from covmap import server

    vol = env.vol
    log = lambda sid: env.data / "sessions" / f"{sid}.jsonl"  # noqa: E731

    async def refused(c, sid, body, needle):
        r = await c.post(f"/api/sessions/{sid}/events", json=body)
        assert r.status == 400 and needle in (await r.json())["error"], (r.status, await r.text())

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        other = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        evs = read_pass(vol, range(5), 0, 0)
        # another session's events, alone or mixed into the batch
        await refused(c, sid, {"events": _to(other, evs)}, other)
        await refused(c, sid, {"events": _to(sid, evs[:2]) + _to(other, evs[2:])}, "nothing was applied")
        # leftover events of an earlier clock posted with this session's now_ms
        await refused(c, sid, {"events": _to(sid, read_pass(vol, range(5), 60000, 0)), "now_ms": 100}, "ahead")
        # without now_ms the clock is the time since the session was created
        await refused(c, sid, {"events": _to(sid, read_pass(vol, range(5), 20000, 0))}, "ahead")
        info = await _json(await c.get(f"/api/sessions/{sid}"))
        assert info["events"] == 0 and (not log(sid).exists() or log(sid).read_text() == "")
        meta = json.loads((env.data / "sessions" / f"{sid}.json").read_text())
        assert "clock_lead_ms" not in meta
        # the client started its clock 7 s before the session existed (viewer load): its
        # first now_ms pins that lead, so its own events are accepted ...
        cov = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": _to(sid, read_pass(vol, range(5), 7000, 0)), "now_ms": 7800}))
        assert cov["events"]["total"] == 5
        lead = json.loads((env.data / "sessions" / f"{sid}.json").read_text())["clock_lead_ms"]
        assert 7000 < lead <= 7800
        await _json(await c.post(f"/api/sessions/{sid}/events",
                                 json={"events": _to(sid, [end(vol, 7900, 5)]), "now_ms": 8000}))
        # ... and later leftovers (or a now_ms) ahead of elapsed + lead + 5 s are not
        await refused(c, sid, {"events": _to(sid, [stack_view(vol, 9, 40000, 6)])}, "ahead")
        await refused(c, sid, {"events": [], "now_ms": 40000}, "now_ms 40000")
        assert (await _json(await c.get(f"/api/sessions/{sid}")))["events"] == 6
        assert len(log(sid).read_text().splitlines()) == 6
        # events without a session field are taken as this session's
        bare = {k: v for k, v in stack_view(vol, 9, 8100, 6).items() if k != "session"}
        assert (await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": [bare]})))["events"]["total"] == 7
        # sample sessions: the client's clock restarts at 0, the server shifts it after the sample
        s = await _json(await c.post("/api/sessions", json={"case": CASE, "from_sample": "skip-lungs"}))
        assert s["t_offset_ms"] > 10000
        live = _to(s["session"], [stack_view(vol, 20, 0, 0, "lung", source="ohif"), end(vol, 400, 1, source="ohif")])
        cov = await _json(await c.post(f"/api/sessions/{s['session']}/events", json={"events": live, "now_ms": 450}))
        assert cov["events"]["total"] == len(env.sample_events) + 2 and cov["events"]["ignored"]["other"] == 0
    run_client(_cfg(env, clock_tolerance_ms=5000.0), fn)

    async def off(c):  # clock check off (synthetic logs posted faster than real time)
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        cov = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": _to(sid, read_pass(vol, range(3), 10**6, 0))}))
        assert cov["events"]["total"] == 3
        await refused(c, sid, {"events": _to("s-else", read_pass(vol, range(3), 0, 3))}, "s-else")
    run_client(_cfg(env, clock_tolerance_ms=None), off)

    for value, want in ((None, 5000.0), ("off", None), ("", None), ("2500", 2500.0)):
        if value is None:
            monkeypatch.delenv("COVMAP_CLOCK_TOLERANCE_MS", raising=False)
        else:
            monkeypatch.setenv("COVMAP_CLOCK_TOLERANCE_MS", value)
        assert Config().clock_tolerance_ms == want == server._env_tolerance()
    monkeypatch.setenv("COVMAP_CLOCK_TOLERANCE_MS", "-1")
    with pytest.raises(ValueError):
        Config()


def test_reused_seq_with_other_content_is_409(env):
    """Two tabs on one session number from the same seq: the second tab's batch is refused
    whole (409, nothing applied, client numbering to resync); identical resends pass."""
    vol = env.vol
    log = lambda sid: (env.data / "sessions" / f"{sid}.jsonl").read_text().splitlines()  # noqa: E731

    async def conflict(c, sid, events, want):
        r = await c.post(f"/api/sessions/{sid}/events", json={"events": events})
        body = await r.json()
        assert r.status == 409 and body["conflicts"] == want, (r.status, body)
        return body

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        tab1 = _to(sid, read_pass(vol, range(10, 15), 0, 0, "lung"))  # seq 0-4
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": tab1}))
        # a resend (e.g. after a lost response, or the unload beacon) of accepted events, with a
        # new one: only the new one is scored and logged
        more = _to(sid, [stack_view(vol, 15, 750, 5, "lung")])
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": tab1[3:] + more + more}))
        assert cov["events"]["total"] == 6 and cov["events"]["ignored"]["other"] == 0
        assert cov["events"]["ignored"]["conflict"] == 0 and len(log(sid)) == 6
        state = lambda cov: ({k: v for k, v in cov.items() if k != "timing_ms"}, log(sid))  # noqa: E731
        before = state(await _json(await c.get(f"/api/sessions/{sid}/coverage")))
        # a second tab numbering from seq 3: refused whole, nothing applied
        tab2 = _to(sid, read_pass(vol, range(30, 34), 800, 3, "soft"))  # seq 3-6
        body = await conflict(c, sid, tab2, [3, 4, 5])
        assert (body["last_seq"], body["last_t_ms"]) == (5, 750.0) and "nothing was applied" in body["error"]
        assert state(await _json(await c.get(f"/api/sessions/{sid}/coverage"))) == before
        # a stamp change is not a content change; the same seq twice in one batch is checked too
        await _json(await c.post(f"/api/sessions/{sid}/events",
                                 json={"events": [{k: v for k, v in tab1[0].items() if k != "session"}]}))
        twice = _to(sid, [stack_view(vol, 20, 900, 6), stack_view(vol, 21, 950, 6)])
        await conflict(c, sid, twice, [6])
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": twice[:1] * 2}))
        # the renumbered second tab is accepted
        cov = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": [{**e, "seq": e["seq"] + 4} for e in tab2]}))
        assert cov["session_info"]["last_seq"] == 10 and cov["events"]["ignored"]["conflict"] == 0
        # sample session: conflicts and last_seq in the client numbering
        s2 = (await _json(await c.post("/api/sessions", json={"case": CASE, "from_sample": "skip-lungs"})))["session"]
        await _json(await c.post(f"/api/sessions/{s2}/events", json={"events": _to(s2, [stack_view(vol, 5, 0, 0)])}))
        body = await conflict(c, s2, _to(s2, [stack_view(vol, 6, 10, 0)]), [0])
        assert (body["last_seq"], body["last_t_ms"]) == (0, 0.0)
        return sid, tab1, tab2

    sid, tab1, tab2 = run_client(_cfg(env), fn)

    async def rehydrate(c):  # rebuilt from the log by a fresh server: same checks
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": tab1}))
        await conflict(c, sid, tab2[:1], [3])
    run_client(_cfg(env), rehydrate)


def test_coverage_and_exports_as_the_reader_saw_them(env):
    """While a view is open, GET coverage and both exports are the latest POST's response
    (provisional at its now_ms); after a confirmed finish they are committed and equal a
    replay of the exported log. session_info.last_view restores the slice and window."""
    from covmap.scoring import replay_file

    vol = env.vol
    strip = lambda cov: {k: v for k, v in cov.items() if k != "timing_ms"}  # noqa: E731

    def csv_rows(text):
        return {r["organ"]: (r["status"], float(r["coverage"]), int(r["seen_voxels"]))
                for r in csv.DictReader(io.StringIO(text))}

    async def exports(c, sid):
        j = json.loads(await (await c.get(f"/api/sessions/{sid}/export", params={"format": "json"})).text())
        rows = csv_rows(await (await c.get(f"/api/sessions/{sid}/export", params={"format": "csv"})).text())
        assert rows == {o["key"]: (o["status"], o["coverage"], o["seen_voxels"]) for o in j["organs"]}
        return j

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        assert (await _json(await c.get(f"/api/sessions/{sid}")))["last_view"] is None
        evs = _to(sid, read_pass(vol, range(5, 15), 0, 0))  # slice 14 still on screen
        resp = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": evs, "now_ms": 2000}))
        assert resp["provisional"] and resp["t_ms"] == 2000
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert strip(cov) == strip(resp) and strip(await exports(c, sid)) == strip(resp)
        assert cov["session_info"]["last_view"] == {"sop": vol["sop_uids"][14], "k": 14,
                                                    "voi": {"wc": 40.0, "ww": 400.0}, "window": "soft"}
        # confirmed finish closes the open view at its t_ms: committed == what was shown
        fin = await _json(await c.post(f"/api/sessions/{sid}/events",
                                       json={"events": _to(sid, [finish(vol, 2000, 10)]), "now_ms": 2100}))
        assert [o["seen_voxels"] for o in fin["organs"]] == [o["seen_voxels"] for o in resp["organs"]]
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert not cov["provisional"] and cov["organs"] == fin["organs"] and cov["events"]["open"] == 0
        exported = await exports(c, sid)
        log = env.root / f"{sid}.export.jsonl"
        log.write_bytes(await (await c.get(f"/api/sessions/{sid}/export", params={"format": "jsonl"})).read())
        rep = replay_file(env.cdir, log)
        assert exported["organs"] == rep["organs"] and exported["summary"] == rep["summary"]
        # a session left open
        sid2 = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        await _json(await c.post(f"/api/sessions/{sid2}/events",
                                 json={"events": _to(sid2, [stack_view(vol, 20, 0, 0)]), "now_ms": 900}))
        assert (await _json(await c.get(f"/api/sessions/{sid2}/coverage")))["t_ms"] == 900
        return sid2

    sid2 = run_client(_cfg(env), fn)

    async def restarted(c):  # no now_ms known after a server restart: committed
        url = f"/api/sessions/{sid2}/coverage"
        cov = await _json(await c.get(url))
        assert not cov["provisional"] and cov["events"]["open"] == 1
        assert cov["session_info"]["last_view"]["k"] == 20
        await _json(await c.post(f"/api/sessions/{sid2}/events", json={"events": [], "now_ms": 1000}))
        assert (await _json(await c.get(url)))["t_ms"] == 1000
        await _json(await c.post(f"/api/sessions/{sid2}/events", json={"events": []}))  # no now_ms: committed
        assert not (await _json(await c.get(url)))["provisional"]
    run_client(_cfg(env), restarted)


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
HITS = web.AppKey("hits", list)  # (method, raw path) of every request the fake Orthanc got


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

    async def orthanc_root(request):  # the rest of Orthanc's REST API (/patients, /tools/shutdown...)
        if request.path.startswith("/dicom-web/"):
            raise web.HTTPNotFound()
        return web.json_response({"root": request.path})

    hits = []

    @web.middleware
    async def record(request, handler):
        hits.append((request.method, request.raw_path))
        return await handler(request)

    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[record])
    app[HITS] = hits
    app.router.add_get("/system", system)
    app.router.add_get("/dicom-web/studies/1/series/2/instances/3/frames/1", frames)
    app.router.add_route("*", "/dicom-web/studies", echo)
    app.router.add_get("/wado", wado)
    app.router.add_route("*", "/{tail:.*}", orthanc_root)
    return app


async def _raw(server, target: str, method: str = "GET", host: str | None = None) -> tuple[int, bytes]:
    """Send a request line verbatim (no client-side path normalization); (status, body)."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    host = f"{server.host}:{server.port}" if host is None else host
    writer.write(f"{method} {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                 f"Content-Length: 0\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


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
                r = await c.get("/wado?requestType=WADO&studyUID=1")
                assert r.status == 200 and r.headers["Content-Type"] == "application/dicom"
                assert await r.read() == b"DICM" * 10
                assert (await c.get("/dicom-web/nothing-here")).status == 404
                r = await c.head("/dicom-web/studies")
                assert r.status == 200
                results = await asyncio.gather(*[c.get("/dicom-web/studies/1/series/2/instances/3/frames/1")
                                                 for _ in range(6)])
                assert [len(await x.read()) for x in results] == [len(body)] * 6
                # the real browser through the ssh tunnel / other loopback spellings
                for host in ("127.0.0.1:8765", "localhost:8765", "LOCALHOST", "[::1]:8765"):
                    status, body = await _raw(c.server, "/dicom-web/studies?limit=1", host=host)
                    assert status == 200 and json.loads(body)["path"] == "/dicom-web/studies", host
                    status, _ = await _raw(c.server, "/api/health", host=host)
                    assert status == 200, host
        finally:
            await up.close()
        assert all(m in ("GET", "HEAD") and (p.startswith(("/dicom-web/", "/system")) or p.split("?")[0] == "/wado")
                   for m, p in up.app[HITS]), up.app[HITS]
        async with TestClient(TestServer(make_app(_cfg(env, orthanc="http://127.0.0.1:9")))) as c:
            r = await c.get("/dicom-web/studies")
            assert r.status == 502 and not (await r.json())["ok"]
    asyncio.run(main())


def test_proxy_only_forwards_reads_under_its_prefixes(env):
    """The DICOMweb / WADO proxy is GET/HEAD only, anchored to /dicom-web/ and /wado, never
    lets a dot segment (raw, percent-encoded, double-encoded, or with a backslash) through to
    Orthanc's root REST API, and refuses foreign Host headers (DNS rebinding)."""
    async def main():
        up = TestServer(_upstream_app())
        await up.start_server()
        hits = up.app[HITS]
        try:
            async with TestClient(TestServer(make_app(_cfg(env, orthanc=str(up.make_url("")).rstrip("/"))))) as c:
                for method in ("POST", "PUT", "DELETE", "PATCH"):
                    assert (await c.request(method, "/dicom-web/studies", data=b"x")).status == 405, method
                    assert (await c.request(method, "/wado?requestType=WADO", data=b"x")).status == 405, method
                assert (await c.post("/tools/shutdown")).status == 405
                assert hits == []
                for target in ("/dicom-web/../patients/x", "/dicom-web/%2e%2e/patients/x",
                               "/dicom-web/%2E%2E/patients/x", "/dicom-web/..%2Fpatients/x",
                               "/dicom-web/..%2fpatients/x", "/dicom-web/..%5Cpatients/x",
                               "/dicom-web/studies/..%5C..%5Ctools/shutdown", "/dicom-web/studies/%2e%2e/%2e%2e/system",
                               "/dicom-web/%252e%252e/patients/x", "/dicom-web/./studies", "/dicom-web/studies/.",
                               "/dicom-web/studies/..", "/dicom-web/.%2e/patients", "/dicom-web/%2e./patients",
                               "/dicom%2Dweb/studies"):
                    for method, want in (("GET", 400), ("HEAD", 400), ("POST", 405)):
                        status, _ = await _raw(c.server, target, method)
                        assert status == want, (method, target, status)
                assert hits == []
                for target in ("/wadoXYZ", "/wado/../patients", "/wado/x", "/wado%2F..%2Fpatients"):
                    status, body = await _raw(c.server, target)  # not proxied: the SPA's index.html
                    assert status == 200 and body == b"<html>ohif</html>", target
                assert hits == []
                for host in ("evil.example", "evil.example:8765", "127.0.0.1.evil.example:8765",
                             "localhost.evil.example", "10.0.0.5:8765", "[::2]:8765", "127.0.0.1:x"):
                    status, _ = await _raw(c.server, "/dicom-web/studies", host=host)
                    assert status == 403, host
                    status, _ = await _raw(c.server, "/api/health", host=host)
                    assert status == 403, host
                assert hits == []
                assert (await c.get("/dicom-web/studies")).status == 200  # still proxies normal reads
                assert hits == [("GET", "/dicom-web/studies")]
        finally:
            await up.close()
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
            evs = _to(sid, read_pass(vol, range(i * 10, i * 10 + 10), 0, 0, "soft") + [end(vol, 1500, 10)])
            expected[sid] = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": evs}))
        assert list(app.live) == [sids[-1]] and app.sessions[sids[0]].scorer is None
        for sid in sids:  # evicted sessions rebuild from their logs with identical results
            cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
            assert cov["organs"] == expected[sid]["organs"] and cov["events"]["total"] == 11
        more = _to(sids[0], [stack_view(vol, 35, 2000, 11), end(vol, 2200, 12)])
        cov = await _json(await c.post(f"/api/sessions/{sids[0]}/events", json={"events": more}))
        assert cov["events"]["total"] == 13
    run_client(_cfg(env), fn)


def test_late_batch_scored_in_log_order(env):
    """A batch older than events already applied (an unload beacon overtaking a tick) is scored
    as replay and a restart score the log: live coverage equals the record."""
    from covmap.scoring import replay_file

    vol = env.vol
    first = read_pass(vol, range(0, 12), 0, 0)
    rest = read_pass(vol, range(12, 18), 12 * 150, 12)
    last = [end(vol, 18 * 150 + 40, 18)]

    async def fn(c):
        out = {}
        for name, order in (("in_order", [first, rest, last]), ("late", [first, last, rest])):
            sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
            for b in order:
                await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": _to(sid, b)}))
            out[name] = (sid, await _json(await c.get(f"/api/sessions/{sid}/coverage")))
        return out

    out = run_client(_cfg(env), fn)
    key = lambda cov: [(o["key"], o["seen_voxels"], o["status"]) for o in cov["organs"]]  # noqa: E731
    sid, late = out["late"]
    assert late["events"]["open"] == 0 and not late["provisional"]
    replayed = replay_file(env.cdir, env.data / "sessions" / f"{sid}.jsonl")
    assert key(late) == key(replayed) == key(out["in_order"][1])

    async def restarted(c):  # a fresh server rebuilds the session from its log
        return await _json(await c.get(f"/api/sessions/{sid}/coverage"))
    assert key(run_client(_cfg(env), restarted)) == key(late)


def test_serve_on_a_busy_port_prints_only_the_failure(env):
    """Nothing claims success before the port is bound: one JSON object, ok false, exit 1."""
    repo = Path(__file__).resolve().parents[1]
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        p = subprocess.run(
            [sys.executable, "-m", "covmap", "serve", "--port", str(busy.getsockname()[1]),
             "--orthanc", "http://127.0.0.1:9"],
            cwd=repo, capture_output=True, text=True, timeout=60,
            env={**os.environ, "COVMAP_DATA": str(env.data), "COVMAP_OHIF_DIST": str(env.dist)})
    out = json.loads(p.stdout)  # exactly one object
    assert p.returncode == 1 and out["ok"] is False and "address already in use" in out["error"]


def test_failed_batch_is_not_half_applied(env, monkeypatch):
    """An unexpected scorer error refuses the batch whole: the session is rebuilt from its log,
    so a retry neither double-counts nor logs a half-applied state."""
    from covmap.scoring import Scorer

    vol = env.vol
    feed = Scorer.feed

    def failing(self, ev, *a, **kw):
        if ev["seq"] == 3:
            raise RuntimeError("boom")
        return feed(self, ev, *a, **kw)

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": _to(sid, read_pass(vol, [5], 0, 0))}))
        batch = _to(sid, read_pass(vol, [6, 7, 8], 150, 1))
        with monkeypatch.context() as m:
            m.setattr(Scorer, "feed", failing)
            r = await c.post(f"/api/sessions/{sid}/events", json={"events": batch})
            assert r.status == 500
        listed = await _json(await c.get(f"/api/sessions?case={CASE}"))  # the reading list still answers
        assert [x["events"] for x in listed["sessions"] if x["session"] == sid] == [1]
        cov = await _json(await c.get(f"/api/sessions/{sid}/coverage"))
        assert cov["events"]["total"] == 1  # nothing of the refused batch
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": batch}))
        assert cov["events"]["total"] == 4 and cov["events"]["ignored"].get("other", 0) == 0
    run_client(_cfg(env), fn)


def test_failed_log_append_is_not_applied(env, monkeypatch):
    """A batch the log cannot take (disk full) is not applied live either: the log is cut back
    to its last whole batch, and the client's retry is applied and logged once."""
    import covmap.server as server

    vol = env.vol

    def disk_full(path, events, mode="a"):
        with open(path, mode) as f:
            f.write('{"seq": 1, "tor')
        time.sleep(0.3)  # the log is read meanwhile
        raise OSError(28, "No space left on device")

    async def fn(c):
        sid = (await _json(await c.post("/api/sessions", json={"case": CASE})))["session"]
        await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": _to(sid, read_pass(vol, [5], 0, 0))}))
        batch = _to(sid, read_pass(vol, [6, 7, 8], 150, 1))
        with monkeypatch.context() as m:
            m.setattr(server, "_write_jsonl", disk_full)
            post = asyncio.ensure_future(c.post(f"/api/sessions/{sid}/events", json={"events": batch}))
            await asyncio.sleep(0.1)
            during = [await (await c.get(f"/api/sessions/{sid}/{u}")).read()
                      for u in ("events.jsonl", "export?format=jsonl")]
            assert (await post).status == 500
        assert during[0] == during[1] and len(during[0].splitlines()) == 1  # not the refused batch
        assert (await _json(await c.get(f"/api/sessions/{sid}/coverage")))["events"]["total"] == 1
        cov = await _json(await c.post(f"/api/sessions/{sid}/events", json={"events": batch}))
        return sid, cov

    sid, cov = run_client(_cfg(env), fn)
    log = [json.loads(x) for x in (env.data / "sessions" / f"{sid}.jsonl").read_text().splitlines()]
    assert cov["events"]["total"] == 4 and [e["seq"] for e in log] == [0, 1, 2, 3]


def test_recent_readings_of_a_case(env):
    vol = env.vol

    async def fn(c):
        sids = [(await _json(await c.post("/api/sessions", json={"case": CASE, "label": f"r{i}"})))["session"]
                for i in range(3)]
        # confirmed, then the Finish sheet opened again and backed out of: still finished (T1)
        evs = read_pass(vol, [3, 4], 0, 0) + [finish(vol, 400, 2), finish(vol, 450, 3, confirmed=False)]
        await _json(await c.post(f"/api/sessions/{sids[1]}/events", json={"events": _to(sids[1], evs)}))
        await _json(await c.get(f"/api/sessions/{sids[1]}/coverage"))
        return sids, await _json(await c.get(f"/api/sessions?case={CASE}&limit=3")), \
            await c.get("/api/sessions"), await _json(await c.get("/api/sessions?case=unsegmented"))

    sids, listed, missing, other = run_client(_cfg(env), fn)
    assert listed["case"] == CASE and [x["session"] for x in listed["sessions"]] == sids[::-1]
    newest, read, _ = listed["sessions"]
    assert newest == {**newest, "label": "r2", "events": 0, "last_t_ms": None, "finished": False}
    assert read == {**read, "label": "r1", "events": 4, "last_t_ms": 450, "finished": True}
    assert missing.status == 400 and other["sessions"] == []

    async def cold(c):  # a fresh server reads the same from the logs, without scoring
        return await _json(await c.get(f"/api/sessions?case={CASE}&limit=3"))
    assert run_client(_cfg(env), cold)["sessions"] == listed["sessions"]
