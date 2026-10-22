"""C12 repro: two tabs on one session number from the same seq; the second tab posts lung reads."""
import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])  # repo tests dir (synth)
from aiohttp.test_utils import TestClient, TestServer
from covmap.server import Config, make_app
from synth import build_case, read_pass, end

root = Path(tempfile.mkdtemp())
cdir = build_case(root / "data" / "cases", "synth-c12")
vol = json.loads((cdir / "volume.json").read_text())

async def main():
    async with TestClient(TestServer(make_app(Config(orthanc="http://127.0.0.1:9", data_dir=root / "data",
                                                       ohif_dist=root / "nodist", web_dir=None)))) as c:
        sid = (await (await c.post("/api/sessions", json={"case": "synth-c12"})).json())["session"]
        stamp = lambda evs: [{**e, "session": sid} for e in evs]
        tab1 = stamp(read_pass(vol, range(0, 14), 0, 0, "soft"))           # seq 0-13, soft window
        tab2 = stamp(read_pass(vol, range(14, 40), 100, 0, "lung") + [end(vol, 100 + 26 * 150, 26)])  # seq 0-26
        r1 = await c.post(f"/api/sessions/{sid}/events", json={"events": tab1, "now_ms": 2200})
        r2 = await c.post(f"/api/sessions/{sid}/events", json={"events": tab2, "now_ms": 4100})
        b2 = await r2.json()
        r3 = None
        if r2.status == 409:  # resync: renumber after last_seq and resend
            n = b2["last_seq"] + 1
            r3 = (await c.post(f"/api/sessions/{sid}/events", json={"events": [{**e, "seq": e["seq"] + n} for e in tab2],
                                                                     "now_ms": 4100})).status
        cov = await (await c.get(f"/api/sessions/{sid}/coverage")).json()
        lungs = {o["key"]: o["coverage"] for o in cov["organs"] if o["key"].startswith("lung")}
        print(json.dumps({"tab1_status": r1.status, "tab2_status": r2.status, "tab2_renumbered_status": r3,
                          "tab2_body": {k: b2[k] for k in ("conflicts", "last_seq", "last_t_ms") if k in b2},
                          "ignored": cov["events"]["ignored"], "lung_coverage": lungs}))
asyncio.run(main())
