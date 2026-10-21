"""DICOM series geometry, HU volume, NIfTI export and label re-gridding.

Conventions (docs/SPEC.md): arrays are indexed ``[k, r, c]``; ``affine_lps`` maps
``(c, r, k, 1)`` to LPS millimetres; ``k`` ascends along ``slice_dir`` =
``row_dir x col_dir``. NIfTI files hold the same voxels as ``[c, r, k]`` with a
RAS affine (LPS with x and y negated).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pydicom
from pydicom.multival import MultiValue

LPS_TO_RAS = np.diag([-1.0, -1.0, 1.0, 1.0])

# Positional tolerances (mm / relative) used to reject inconsistent stacks.
ORIENTATION_TOL = 1e-4
PIXEL_SPACING_TOL = 1e-4
SPACING_REL_TOL = 0.05
SPACING_ABS_TOL = 0.01


class SeriesError(ValueError):
    """The files do not form one regular, consistently oriented CT stack."""


def _vec(values, n: int) -> np.ndarray:
    v = np.asarray([float(x) for x in values], dtype=float)
    if v.shape != (n,):
        raise SeriesError(f"expected {n} values, got {list(values)!r}")
    return v


def _first(value):
    """First element of a possibly multi-valued DICOM attribute."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, MultiValue)):
        return value[0] if len(value) else None
    return value


def read_series(paths, series_uid: str | None = None) -> tuple[dict, list[pydicom.Dataset], dict]:
    """Read DICOM files, sort them along the slice normal and validate the stack.

    Returns ``(geometry, datasets_in_k_order, checks)`` where ``geometry`` has the
    volume.json fields (without ``case``) and ``checks`` records the measured
    deviations. Raises :class:`SeriesError` on mixed series, inconsistent
    orientation or spacing, duplicate positions, gaps or gantry tilt.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise SeriesError("no DICOM files")
    datasets = [pydicom.dcmread(p) for p in paths]

    uids = {str(ds.SeriesInstanceUID) for ds in datasets}
    if len(uids) != 1:
        raise SeriesError(f"files belong to {len(uids)} series: {sorted(uids)}")
    uid = uids.pop()
    if series_uid is not None and uid != series_uid:
        raise SeriesError(f"SeriesInstanceUID {uid} != expected {series_uid}")
    studies = {str(ds.StudyInstanceUID) for ds in datasets}
    if len(studies) != 1:
        raise SeriesError(f"files belong to {len(studies)} studies")
    modalities = {str(getattr(ds, "Modality", "")) for ds in datasets}
    if modalities != {"CT"}:
        raise SeriesError(f"expected CT only, got modalities {sorted(modalities)}")
    sops = [str(ds.SOPInstanceUID) for ds in datasets]
    if len(set(sops)) != len(sops):
        raise SeriesError("duplicate SOPInstanceUIDs in series")
    if len(datasets) < 2:
        raise SeriesError("need at least 2 slices to define slice spacing")

    ref = datasets[0]
    rows, cols = int(ref.Rows), int(ref.Columns)
    iop = _vec(ref.ImageOrientationPatient, 6)
    ps = _vec(ref.PixelSpacing, 2)
    for ds, p in zip(datasets, paths):
        if (int(ds.Rows), int(ds.Columns)) != (rows, cols):
            raise SeriesError(f"{p.name}: matrix {ds.Rows}x{ds.Columns} != {rows}x{cols}")
        if np.abs(_vec(ds.ImageOrientationPatient, 6) - iop).max() > ORIENTATION_TOL:
            raise SeriesError(f"{p.name}: ImageOrientationPatient differs from the first slice")
        if np.abs(_vec(ds.PixelSpacing, 2) - ps).max() > PIXEL_SPACING_TOL:
            raise SeriesError(f"{p.name}: PixelSpacing differs from the first slice")

    row_dir, col_dir = iop[:3], iop[3:]
    if abs(np.linalg.norm(row_dir) - 1) > 1e-3 or abs(np.linalg.norm(col_dir) - 1) > 1e-3:
        raise SeriesError(f"ImageOrientationPatient is not unit length: {iop.tolist()}")
    if abs(float(row_dir @ col_dir)) > 1e-3:
        raise SeriesError(f"ImageOrientationPatient is not orthogonal: {iop.tolist()}")
    row_dir = row_dir / np.linalg.norm(row_dir)
    col_dir = col_dir / np.linalg.norm(col_dir)
    normal = np.cross(row_dir, col_dir)
    normal /= np.linalg.norm(normal)

    ipp = np.array([_vec(ds.ImagePositionPatient, 3) for ds in datasets])
    proj = ipp @ normal
    order = np.argsort(proj, kind="stable")
    proj, ipp = proj[order], ipp[order]
    datasets = [datasets[i] for i in order]

    steps = np.diff(proj)
    if steps.min() <= SPACING_ABS_TOL:
        i = int(np.argmin(steps))
        raise SeriesError(
            f"duplicate slice position {proj[i]:.3f} mm along the normal "
            f"(SOPs {datasets[i].SOPInstanceUID}, {datasets[i + 1].SOPInstanceUID})")
    spacing = float((proj[-1] - proj[0]) / (len(proj) - 1))
    deviation = np.abs(steps - spacing)
    max_dev = float(deviation.max())
    if max_dev > max(SPACING_ABS_TOL, SPACING_REL_TOL * spacing):
        i = int(np.argmax(deviation))
        kind = "gap (missing slice?)" if steps[i] > spacing else "irregular spacing"
        raise SeriesError(
            f"{kind} between k={i} and k={i + 1}: step {steps[i]:.4f} mm vs mean "
            f"{spacing:.4f} mm (max deviation {max_dev:.4f} mm)")

    # Every slice origin must lie on the line origin + t * normal (no gantry tilt),
    # otherwise the affine below would misplace voxels.
    rel = ipp - ipp[0]
    inplane = rel - np.outer(rel @ normal, normal)
    max_inplane = float(np.linalg.norm(inplane, axis=1).max())
    if max_inplane > 0.5 * float(ps.min()):
        raise SeriesError(
            f"slice origins drift {max_inplane:.3f} mm off the slice normal "
            "(gantry tilt or sheared stack is not supported)")

    affine = np.eye(4)
    affine[:3, 0] = row_dir * ps[1]
    affine[:3, 1] = col_dir * ps[0]
    affine[:3, 2] = normal * spacing
    affine[:3, 3] = ipp[0]

    wc, ww = _first(getattr(datasets[0], "WindowCenter", None)), _first(getattr(datasets[0], "WindowWidth", None))
    default_window = {"wc": float(wc), "ww": float(ww)} if wc is not None and ww is not None else None

    geometry = {
        "study_uid": str(datasets[0].StudyInstanceUID),
        "series_uid": uid,
        "rows": rows, "cols": cols, "n_slices": len(datasets),
        "sop_uids": [str(ds.SOPInstanceUID) for ds in datasets],
        "origin_lps": ipp[0].tolist(),
        "row_dir": row_dir.tolist(), "col_dir": col_dir.tolist(), "slice_dir": normal.tolist(),
        "pixel_spacing": ps.tolist(), "slice_spacing": spacing,
        "affine_lps": affine.tolist(),
        "default_window": default_window,
    }
    checks = {
        "max_spacing_deviation_mm": max_dev,
        "max_inplane_offset_mm": max_inplane,
        "min_step_mm": float(steps.min()), "max_step_mm": float(steps.max()),
        "input_order_reversed": bool(order[0] > order[-1]),
    }
    return geometry, datasets, checks


def hu_volume(datasets: list[pydicom.Dataset]) -> tuple[np.ndarray, dict]:
    """Stack slices (already in k order) into int16 HU ``[k, r, c]``.

    Rescale slope/intercept are applied per slice since they may differ.
    """
    first = datasets[0]
    vol = np.empty((len(datasets), int(first.Rows), int(first.Columns)), dtype=np.int16)
    clipped = 0
    rescales = set()
    for k, ds in enumerate(datasets):
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
        intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
        rescales.add((slope, intercept))
        hu = np.rint(ds.pixel_array.astype(np.float64) * slope + intercept)
        lo, hi = np.iinfo(np.int16).min, np.iinfo(np.int16).max
        clipped += int(np.count_nonzero((hu < lo) | (hu > hi)))
        vol[k] = np.clip(hu, lo, hi)
    return vol, {"clipped_voxels": clipped, "rescale_pairs": sorted(rescales)}


def affine_lps(geometry: dict) -> np.ndarray:
    return np.asarray(geometry["affine_lps"], dtype=float)


def nifti_affine_ras(geometry: dict) -> np.ndarray:
    """RAS affine of the NIfTI array ``vol.transpose(2, 1, 0)`` (voxel order c, r, k)."""
    return LPS_TO_RAS @ affine_lps(geometry)


def write_nifti(vol_krc: np.ndarray, geometry: dict, path: Path) -> None:
    import nibabel as nib

    affine = nifti_affine_ras(geometry)
    img = nib.Nifti1Image(vol_krc.transpose(2, 1, 0), affine)
    img.set_sform(affine, code=1)
    img.set_qform(affine, code=1)
    img.header.set_xyzt_units("mm")
    img.header.set_slope_inter(1.0, 0.0)
    nib.save(img, str(path))


def voxel_to_lps(geometry: dict, krc: np.ndarray) -> np.ndarray:
    """World LPS for voxel coordinates given as ``[..., (k, r, c)]``."""
    krc = np.asarray(krc, dtype=float)
    crk = krc[..., ::-1]
    a = affine_lps(geometry)
    return crk @ a[:3, :3].T + a[:3, 3]


def grid_transform(src_affine_ras: np.ndarray, geometry: dict) -> np.ndarray:
    """4x4 matrix mapping DICOM-grid indices ``(k, r, c, 1)`` to source array indices."""
    swap = np.zeros((4, 4))
    swap[0, 2] = swap[1, 1] = swap[2, 0] = swap[3, 3] = 1.0  # (k, r, c) <-> (c, r, k)
    # LPS_TO_RAS is its own inverse: src index = inv(A_ras) @ RAS = inv(A_ras) @ L2R @ LPS.
    src_from_lps = np.linalg.inv(np.asarray(src_affine_ras, float)) @ LPS_TO_RAS
    return src_from_lps @ affine_lps(geometry) @ swap


def regrid_to_dicom(data: np.ndarray, src_affine_ras: np.ndarray, geometry: dict,
                    tol: float = 1e-3) -> tuple[np.ndarray, dict]:
    """Nearest-neighbour map a labels array (any NIfTI orientation) onto ``[k, r, c]``.

    When the source grid is the DICOM grid up to axis permutation/flip (the usual
    case: TotalSegmentator writes in its input space), this is an exact gather;
    the detected permutation is returned so callers can record it. Otherwise it
    falls back to nearest-neighbour resampling (out-of-volume = 0).
    """
    shape = (int(geometry["n_slices"]), int(geometry["rows"]), int(geometry["cols"]))
    m = grid_transform(src_affine_ras, geometry)
    lin, off = m[:3, :3], m[:3, 3]
    rounded = np.rint(lin)
    is_perm = (np.allclose(lin, rounded, atol=tol) and np.allclose(off, np.rint(off), atol=tol)
               and all(np.count_nonzero(rounded[i]) == 1 for i in range(3))
               and all(np.count_nonzero(rounded[:, j]) == 1 for j in range(3)))
    if is_perm:
        idx = []
        mapping = []
        for s in range(3):  # source axis s is driven by exactly one target axis t
            t = int(np.flatnonzero(rounded[s])[0])
            sign = int(rounded[s, t])
            o = int(np.rint(off[s]))
            ax = sign * np.arange(shape[t]) + o
            if ax.min() < 0 or ax.max() >= data.shape[s]:
                is_perm = False
                break
            bshape = [1, 1, 1]
            bshape[t] = shape[t]
            idx.append(ax.reshape(bshape))
            mapping.append({"src_axis": s, "dst_axis": "krc"[t], "sign": sign, "offset": o})
        if is_perm:
            return np.ascontiguousarray(data[tuple(idx)]), {"mode": "permutation", "axes": mapping}

    # Nearest source voxel centre, slice by slice; targets beyond the source extent get 0.
    out = np.zeros(shape, dtype=data.dtype)
    rr, cc = np.meshgrid(np.arange(shape[1]), np.arange(shape[2]), indexing="ij")
    plane = lin[:, 1:] @ np.stack([rr.ravel(), cc.ravel()]) + off[:, None]
    limits = np.asarray(data.shape)[:, None]
    for k in range(shape[0]):
        idx = np.floor(plane + lin[:, :1] * k + 0.5).astype(np.int64)
        ok = ((idx >= 0) & (idx < limits)).all(axis=0)
        out[k].reshape(-1)[ok] = data[idx[0, ok], idx[1, ok], idx[2, ok]]
    return out, {"mode": "resampled_nearest", "matrix": m.tolist()}
