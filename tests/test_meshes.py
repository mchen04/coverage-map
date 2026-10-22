"""Mesh binary format round trip, world placement, closure and outward winding."""

import json

import numpy as np
import pytest

from covmap import meshes


def _rotation(ax, ay, az):
    from scipy.spatial.transform import Rotation

    return Rotation.from_euler("xyz", [ax, ay, az], degrees=True).as_matrix()


def _geometry(shape, spacing=(2.5, 0.8, 0.7), rot=(10, -20, 30), origin=(-40.0, 12.0, 100.0)):
    r = _rotation(*rot)
    row, col = r[:, 0], r[:, 1]
    normal = np.cross(row, col)
    a = np.eye(4)
    a[:3, 0], a[:3, 1], a[:3, 2], a[:3, 3] = row * spacing[2], col * spacing[1], normal * spacing[0], origin
    return {"n_slices": shape[0], "rows": shape[1], "cols": shape[2], "slice_spacing": spacing[0],
            "pixel_spacing": [spacing[1], spacing[2]], "affine_lps": a.tolist(),
            "sop_uids": [f"1.2.{k}" for k in range(shape[0])]}


def _phantom(shape=(24, 60, 64)):
    """Sphere (id 9) in the middle, a box (id 21) touching the volume edge."""
    geom = _geometry(shape)
    a = np.asarray(geom["affine_lps"])
    kk, rr, cc = np.meshgrid(*[np.arange(n) for n in shape], indexing="ij")
    world = np.stack([cc, rr, kk], -1) @ a[:3, :3].T + a[:3, 3]
    center = a[:3, :3] @ [32, 30, 12] + a[:3, 3]  # voxel (c, r, k) = (32, 30, 12)
    labels = np.zeros(shape, np.uint8)
    radius = 15.0
    labels[np.linalg.norm(world - center, axis=-1) <= radius] = 9
    labels[:6, 5:20, 0:12] = 21
    return labels, geom, center, radius


def _edges_closed(tris):
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return bool((counts == 2).all())


def test_roundtrip_world_bounds_and_winding(tmp_path):
    labels, geom, center, radius = _phantom()
    meta, info = meshes.write_case_meshes(labels, geom, [(9, "liver"), (21, "thoracic_spine")], tmp_path)
    loaded = meshes.load_meshes(tmp_path)
    assert json.loads((tmp_path / "meshes.json").read_text()) == loaded["meta"] == meta
    assert [o["key"] for o in meta["organs"]] == ["liver", "thoracic_spine"]
    n_pts = sum(o["n_points"] for o in meta["organs"])
    n_tri = sum(o["n_tris"] for o in meta["organs"])
    assert (tmp_path / "meshes.bin").stat().st_size == 12 * (n_pts + n_tri)
    assert meta["organs"][1]["points_offset"] == meta["organs"][0]["n_points"]
    assert meta["organs"][1]["tris_offset"] == meta["organs"][0]["n_tris"]
    assert info["n_tris_total"] == n_tri <= meshes.MAX_TRIANGLES

    a = np.asarray(geom["affine_lps"])
    inv = np.linalg.inv(a)
    all_pts = np.concatenate([m["points"] for m in loaded["organs"].values()])
    lo, hi = all_pts.min(0), all_pts.max(0)
    assert np.allclose(meta["bounds_lps"], [v for pair in zip(lo, hi) for v in pair], atol=1e-3)
    crk = all_pts.astype(float) @ inv[:3, :3].T + inv[:3, 3]
    extent = np.array([geom["cols"], geom["rows"], geom["n_slices"]])
    assert (crk >= -0.5 - 1e-3).all() and (crk <= extent - 0.5 + 1e-3).all()

    for key, m in loaded["organs"].items():
        assert m["points"].dtype == np.float32 and m["tris"].dtype == np.uint32
        assert _edges_closed(m["tris"]), key
        assert meshes._signed_volume(m["points"], m["tris"]) > 0, key

    sphere = loaded["organs"]["liver"]
    vol = meshes._signed_volume(sphere["points"], sphere["tris"])
    assert vol == pytest.approx(4 / 3 * np.pi * radius**3, rel=0.15)
    p = sphere["points"].astype(float)
    t = sphere["tris"]
    normals = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    outward = np.einsum("ij,ij->i", normals, p[t].mean(axis=1) - center)
    assert (outward > 0).mean() > 0.98
    dist = np.linalg.norm(p - center, axis=1)
    assert np.abs(dist - radius).max() < 3.0


def test_triangle_budget_adapts(tmp_path):
    labels, geom, _, _ = _phantom()
    full, _ = meshes.build_meshes(labels, geom, [(9, "liver"), (21, "box")])
    n_full = sum(len(m["tris"]) for m in full)
    budget = n_full // 4
    small, info = meshes.build_meshes(labels, geom, [(9, "liver"), (21, "box")], max_triangles=budget)
    assert 0 < sum(len(m["tris"]) for m in small) <= budget
    assert len(info["attempts"]) > 1


def test_missing_organ_is_skipped(tmp_path):
    labels, geom, _, _ = _phantom()
    meta, _ = meshes.write_case_meshes(labels, geom, [(9, "liver"), (5, "aorta")], tmp_path)
    assert [o["key"] for o in meta["organs"]] == ["liver"]
