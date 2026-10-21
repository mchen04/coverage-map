"""Per-organ surface meshes for vtk.js (docs/SPEC.md "Meshes").

Each organ mask is block-averaged to roughly ``voxel_mm`` resolution, lightly
smoothed and contoured with marching cubes at 0.5. Vertices are mapped to LPS mm
with the volume affine and clamped to the volume's physical extent; triangles are
wound so normals point outward. The finest block size whose meshes fit the case's
triangle budget is found by binary search over the distinct block-factor triples.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

MAX_TRIANGLES = 250_000
BASE_VOXEL_MM = 1.5
SMOOTH_SIGMA = 0.5  # in (downsampled) voxels


def _block_factors(spacing_krc, voxel_mm: float) -> np.ndarray:
    return np.maximum(1, np.rint(voxel_mm / np.asarray(spacing_krc, float))).astype(int)


def _candidate_factors(spacing_krc, voxel_mm: float, max_mm: float = 16.0, max_aspect: float = 2.0):
    """Block-factor triples, finest first: no block finer than ``voxel_mm`` along an axis
    whose voxels are finer than that, block edges at most ``max_mm`` and aspect <= ``max_aspect``."""
    sp = np.asarray(spacing_krc, float)
    floor = np.where(sp < voxel_mm, 0.66 * voxel_mm, 0.0)
    found = set()
    for fk in range(1, int(max_mm // sp[0]) + 2):
        for t in np.arange(min(sp[1:]), max_mm + 1e-9, 0.1):
            f = np.array([fk, max(1, round(t / sp[1])), max(1, round(t / sp[2]))])
            dims = f * sp
            if (dims >= floor).all() and dims.max() <= max(max_mm, sp.max()) and dims.max() <= max_aspect * dims.min():
                found.add(tuple(int(x) for x in f))
    if not found:
        found.add(tuple(int(x) for x in _block_factors(sp, voxel_mm)))
    return sorted(found, key=lambda f: (float(np.prod(np.asarray(f) * sp)), f))


def organ_surface(mask: np.ndarray, offset_krc, factors, sigma: float = SMOOTH_SIGMA):
    """Marching-cubes surface of a cropped boolean mask.

    Returns vertices as continuous ``(k, r, c)`` indices of the full volume and
    triangles, or ``None`` if nothing survives at this resolution.
    """
    from scipy import ndimage
    from skimage import measure

    f = np.asarray(factors, int)
    # One empty block on each side closes the surface; pad the high side to whole blocks.
    lo = f
    hi = f + (-np.asarray(mask.shape)) % f
    m = np.pad(mask.astype(np.float32), list(zip(lo, hi)))
    shp = np.asarray(m.shape) // f
    down = m.reshape(shp[0], f[0], shp[1], f[1], shp[2], f[2]).mean(axis=(1, 3, 5))
    if sigma > 0:
        down = ndimage.gaussian_filter(down, sigma)
    if down.max() <= 0.5:
        return None
    verts, faces, _, _ = measure.marching_cubes(down, level=0.5, allow_degenerate=False)
    # block i spans padded indices [i*f, i*f + f - 1]; its centre is i*f + (f-1)/2
    verts = verts * f + (f - 1) / 2.0 - lo + np.asarray(offset_krc, float)
    return verts, faces.astype(np.uint32)


def _signed_volume(points: np.ndarray, tris: np.ndarray) -> float:
    a, b, c = (points[tris[:, i]].astype(np.float64) for i in range(3))
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _to_world(verts_krc: np.ndarray, geometry: dict) -> np.ndarray:
    shape = np.array([geometry["n_slices"], geometry["rows"], geometry["cols"]], float)
    v = np.clip(verts_krc, -0.5, shape - 0.5)  # stay inside the volume's physical box
    a = np.asarray(geometry["affine_lps"], float)
    return v[:, ::-1] @ a[:3, :3].T + a[:3, 3]


def build_meshes(labels: np.ndarray, geometry: dict, organ_ids: list[tuple[int, str]],
                 max_triangles: int = MAX_TRIANGLES, voxel_mm: float = BASE_VOXEL_MM):
    """Meshes for ``[(id, key), ...]``; returns ``(meshes, info)``.

    ``meshes`` is a list of ``{"id", "key", "points" float32 (n,3) LPS, "tris" uint32 (m,3)}``.
    """
    from scipy import ndimage

    spacing_krc = (geometry["slice_spacing"], geometry["pixel_spacing"][0], geometry["pixel_spacing"][1])
    boxes = ndimage.find_objects(labels)
    crops = {}
    for oid, key in organ_ids:
        if oid > len(boxes) or boxes[oid - 1] is None:
            continue
        box = boxes[oid - 1]
        crops[oid] = (key, labels[box] == oid, [s.start for s in box])

    # Distinct block-factor triples from fine to coarse; binary-search the finest that fits.
    candidates = _candidate_factors(spacing_krc, voxel_mm)

    attempts, results = [], {}
    lo, hi, best = 0, len(candidates) - 1, None
    while lo <= hi:
        mid = 0 if not attempts else (lo + hi) // 2
        results[mid] = _mesh_all(crops, np.asarray(candidates[mid]), geometry)
        total = sum(len(m["tris"]) for m in results[mid])
        attempts.append({"factors_krc": list(candidates[mid]), "n_tris": total})
        if total <= max_triangles:
            best, hi = mid, mid - 1
        else:
            lo = mid + 1
    if best is None:
        raise RuntimeError(f"could not fit meshes into {max_triangles} triangles: {attempts}")
    meshes = results[best]
    total = sum(len(m["tris"]) for m in meshes)
    info = {"attempts": attempts, "factors_krc": list(candidates[best]), "n_tris_total": total,
            "n_points_total": sum(len(m["points"]) for m in meshes), "max_triangles": max_triangles}
    return meshes, info


def _mesh_all(crops: dict, factors: np.ndarray, geometry: dict) -> list[dict]:
    """One outward-wound LPS mesh per cropped organ mask at the given block factors."""
    meshes = []
    for oid, (key, mask, offset) in crops.items():
        surf = organ_surface(mask, offset, factors)
        f = factors
        while surf is None:  # thin/tiny organ vanished: no smoothing, then finer blocks
            surf = organ_surface(mask, offset, f, sigma=0.0)
            if surf is None and f.max() == 1:
                break
            f = np.maximum(1, f // 2)
        if surf is None:
            continue
        verts, tris = surf
        pts = _to_world(verts, geometry)
        if _signed_volume(pts, tris) < 0:
            tris = tris[:, [0, 2, 1]]
        meshes.append({"id": oid, "key": key, "points": pts.astype(np.float32),
                       "tris": np.ascontiguousarray(tris, dtype=np.uint32)})
    return meshes


def write_meshes(meshes: list[dict], seg_dir: Path) -> dict:
    seg_dir = Path(seg_dir)
    entries, p_off, t_off = [], 0, 0
    for m in meshes:
        entries.append({"id": int(m["id"]), "key": m["key"], "points_offset": p_off,
                        "n_points": len(m["points"]), "tris_offset": t_off, "n_tris": len(m["tris"])})
        p_off += len(m["points"])
        t_off += len(m["tris"])
    if meshes:
        allp = np.concatenate([m["points"] for m in meshes])
        lo, hi = allp.min(axis=0), allp.max(axis=0)
        bounds = [float(v) for pair in zip(lo, hi) for v in pair]
    else:
        bounds = [0.0] * 6
    meta = {"organs": entries, "bounds_lps": [round(b, 3) for b in bounds]}
    with open(seg_dir / "meshes.bin", "wb") as f:
        for m in meshes:
            f.write(np.ascontiguousarray(m["points"], dtype="<f4").tobytes())
        for m in meshes:
            f.write(np.ascontiguousarray(m["tris"], dtype="<u4").tobytes())
    (seg_dir / "meshes.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


def write_case_meshes(labels: np.ndarray, geometry: dict, organ_ids: list[tuple[int, str]],
                      seg_dir: Path, max_triangles: int = MAX_TRIANGLES) -> tuple[dict, dict]:
    meshes, info = build_meshes(labels, geometry, organ_ids, max_triangles=max_triangles)
    return write_meshes(meshes, seg_dir), info


def load_meshes(seg_dir: Path) -> dict:
    """Read meshes.json + meshes.bin into ``{key: {"id", "points" (n,3), "tris" (m,3)}}``."""
    seg_dir = Path(seg_dir)
    meta = json.loads((seg_dir / "meshes.json").read_text())
    raw = (seg_dir / "meshes.bin").read_bytes()
    n_pts = sum(o["n_points"] for o in meta["organs"])
    n_tri = sum(o["n_tris"] for o in meta["organs"])
    if len(raw) != 4 * 3 * (n_pts + n_tri):
        raise ValueError(f"meshes.bin is {len(raw)} bytes, expected {12 * (n_pts + n_tri)}")
    pts = np.frombuffer(raw, dtype="<f4", count=n_pts * 3).reshape(-1, 3)
    tris = np.frombuffer(raw, dtype="<u4", offset=12 * n_pts).reshape(-1, 3)
    out = {}
    for o in meta["organs"]:
        p = pts[o["points_offset"]:o["points_offset"] + o["n_points"]]
        t = tris[o["tris_offset"]:o["tris_offset"] + o["n_tris"]]
        if t.size and int(t.max()) >= len(p):
            raise ValueError(f"{o['key']}: triangle index {int(t.max())} >= {len(p)} points")
        out[o["key"]] = {"id": o["id"], "points": p, "tris": t}
    return {"meta": meta, "organs": out}
