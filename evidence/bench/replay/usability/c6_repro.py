"""C6 repro: leftover events of an earlier client clock posted into a new session."""
import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])  # repo tests dir (synth)
from aiohttp.test_utils import TestClient, TestServer
from covmap.server import Config, make_app
from synth import build_case, read_pass, end, stack_view

root = Path(tempfile.mkdtemp())
cdir = build_case(root / "data" / "cases", "synth-c6")
vol = json.loads((cdir / "volume.json").read_text())

async def main():
    async with TestClient(TestServer(make_app(Config(orthanc="http://127.0.0.1:9", data_dir=root / "data",
                                                       ohif_dist=root / "nodist", web_dir=None)))) as c:
        sid = (await (await c.post("/api/sessions", json={"case": "synth-c6"})).json())["session"]
        other = "s-previous"
        stale = [{**e, "session": other} for e in read_pass(vol, range(3), 60000, 200)]
        r1 = await c.post(f"/api/sessions/{sid}/events", json={"events": stale, "now_ms": 100})
        left = [{**e, "session": sid} for e in read_pass(vol, range(3), 60000, 200)]
        r2 = await c.post(f"/api/sessions/{sid}/events", json={"events": left, "now_ms": 100})
        read = [{**e, "session": sid} for e in read_pass(vol, range(40), 0, 0, dwell=700.0) + [end(vol, 28000, 40)]]
        r3 = await c.post(f"/api/sessions/{sid}/events", json={"events": read, "now_ms": 28100})
        cov = await r3.json()
        liver = next(o for o in cov["organs"] if o["key"] == "liver")
        print(json.dumps({"foreign_session_status": r1.status, "leftover_clock_status": r2.status,
                          "read_status": r3.status, "liver_status": liver["status"],
                          "liver_coverage": liver["coverage"], "voxel_coverage": cov["summary"]["voxel_coverage"]}))
asyncio.run(main())
