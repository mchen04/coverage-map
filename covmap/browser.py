from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
from pathlib import Path
from unittest import mock

from aiohttp import streams, web
from aiohttp.http import HttpVersion11, RawRequestMessage
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from covmap.casedata import organ_outline, read_json
from covmap.server import APP_KEY, Config, make_app

OUTLINES = "outlines.json"
_outlines: dict[str, dict] = {}


def write_outlines(case_dir: Path) -> Path:
    d = Path(case_dir)
    n_slices = read_json(d / "volume.json")["n_slices"]
    table = {}
    for organ in read_json(d / "seg" / "organs.json")["organs"]:
        slices = {}
        for k in range(n_slices):
            contours = organ_outline(d, organ["key"], k)
            if contours:
                slices[str(k)] = contours
        table[organ["key"]] = slices
    out = d / "seg" / OUTLINES
    out.write_text(json.dumps(table, separators=(",", ":")))
    return out


def precomputed_outline(case, key: str, k: int, tolerance: float = 0.5) -> list:
    d = Path(case)
    table = _outlines.get(str(d))
    if table is None:
        table = _outlines[str(d)] = read_json(d / "seg" / OUTLINES)
    if key not in table:
        raise KeyError(f"unknown organ {key!r}")
    n_slices = read_json(d / "volume.json")["n_slices"]
    if not 0 <= k < n_slices:
        raise IndexError(f"slice {k} outside 0..{n_slices - 1}")
    return table[key].get(str(k), [])


class InlineExecutor(concurrent.futures.Executor):
    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as e:
            future.set_exception(e)
        return future


class BrowserServer:
    def __init__(self, cfg: Config | None = None, **overrides):
        cfg = dataclasses.replace(cfg or Config(**overrides), outline=precomputed_outline)
        self.app = make_app(cfg)
        self.app[APP_KEY].pool = InlineExecutor()

    async def start(self) -> None:
        self.app.freeze()
        await self.app.startup()

    async def handle(self, method: str, path: str, body: bytes | None = None) -> tuple[int, dict, bytes]:
        headers = {"Host": "localhost"}
        if body:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        loop = asyncio.get_running_loop()
        payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2**16, loop=loop)
        if body:
            payload.feed_data(body)
        payload.feed_eof()
        raw = tuple((k.encode(), v.encode()) for k, v in headers.items())
        message = RawRequestMessage(method, path, HttpVersion11, CIMultiDictProxy(CIMultiDict(headers)),
                                    raw, False, None, False, False, URL(path))
        request = web.Request(message, payload, mock.Mock(), mock.Mock(), mock.Mock(), loop,
                              client_max_size=self.app._client_max_size)
        try:
            response = await self.app._handle(request)
        except web.HTTPException as e:
            response = e
        data = response.body if isinstance(response.body, bytes) else (response.text or "").encode()
        return response.status, dict(response.headers), data
