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
    gray = render(case, out=tmp_path / "gray.png", width=400, height=400)  # CLI path: files served in-process
    assert (gray["width"], gray["height"]) == (400, 400)
    assert gray["organs_drawn"] == len(organs)
    assert gray["status_counts"] == {"skipped": len(organs)}
    assert gray["foreground_fraction"] > 0.01
    assert set(gray["timings_ms"]) >= {"browser_start", "page_load", "meshes", "render", "screenshot", "total"}
    assert gray["webgl_renderer"]
    # the same page and files through the app server give the same image
    via_server = render(case, out=tmp_path / "gray-server.png", width=400, height=400, server=server)
    assert via_server["sha256"] == gray["sha256"]

    keys = [o["key"] for o in organs]
    cov = {"organs": [{"key": k, "status": ("reviewed", "partial", "skipped")[i % 3]} for i, k in enumerate(keys)]}
    colored = render(case, coverage=cov, out=tmp_path / "colored.png", width=400, height=400)
    assert colored["status_counts"].get("reviewed", 0) == len(keys[0::3])
    assert colored["sha256"] != gray["sha256"]


def test_page_server_serves_only_the_render_files(server):
    import urllib.error
    import urllib.request

    from covmap.render import page_server

    case, _ = _segmented_case()
    with page_server(case) as base:
        for path in ("/render.html", "/render/render.js", f"/api/cases/{case}/organs",
                     f"/api/cases/{case}/meshes.json", f"/api/cases/{case}/meshes.bin"):
            with urllib.request.urlopen(base + path) as r:
                assert r.status == 200 and r.read()
        for path in ("/render/../index.html", "/index.html", f"/api/cases/{case}/volume",
                     "/api/cases/other-case/organs"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path)
            assert e.value.code == 404
    with pytest.raises(ValueError), page_server("../" + case):
        pass
