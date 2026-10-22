"""covmap render: the app's vtk.js organ map in headless Chromium (needs the built app)."""

from __future__ import annotations

import json

import pytest

from covmap import paths

pytestmark = [pytest.mark.browser, pytest.mark.data]


def _segmented_case() -> tuple[str, list[dict]]:
    for d in sorted((paths.data_dir() / "cases").glob("*")):
        organs = d / "seg" / "organs.json"
        if organs.exists() and (d / "seg" / "meshes.bin").exists():
            return d.name, json.loads(organs.read_text())["organs"]
    pytest.skip("no segmented case in $COVMAP_DATA")


@pytest.fixture(scope="module")
def server():
    if not (paths.ohif_dist() / "render.html").exists():
        pytest.skip("render page not built (scripts/build_ohif.sh)")
    from covmap.render import covmap_server

    with covmap_server() as base:
        yield base


def test_render_default_gray_and_with_coverage(server, tmp_path):
    from covmap.render import render

    case, organs = _segmented_case()
    gray = render(case, out=tmp_path / "gray.png", width=400, height=400, server=server)
    assert (gray["width"], gray["height"]) == (400, 400)
    assert gray["organs_drawn"] == len(organs)
    assert gray["status_counts"] == {"skipped": len(organs)}
    assert gray["foreground_fraction"] > 0.01
    assert set(gray["timings_ms"]) >= {"browser_start", "page_load", "meshes", "render", "screenshot", "total"}
    assert gray["webgl_renderer"]

    keys = [o["key"] for o in organs]
    cov = {"organs": [{"key": k, "status": ("reviewed", "partial", "skipped")[i % 3]} for i, k in enumerate(keys)]}
    colored = render(case, coverage=cov, out=tmp_path / "colored.png", width=400, height=400, server=server)
    assert colored["status_counts"].get("reviewed", 0) == len(keys[0::3])
    assert colored["sha256"] != gray["sha256"]
