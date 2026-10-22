"""Shared fixtures. Keep this small; other synthetic helpers live in their own modules."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pytest


def _ds(v: float) -> str:
    return f"{v:.10g}"


def write_synthetic_series(directory: Path, *, rows: int = 24, cols: int = 20, n_slices: int = 12,
                           pixel_spacing=(0.8, 0.7), slice_spacing: float = 2.0,
                           row_dir=(1.0, 0.0, 0.0), col_dir=(0.0, 1.0, 0.0),
                           origin=(-10.0, -20.0, 30.0), descending: bool = True,
                           seed: int = 0, vary_rescale: bool = True, skip_k=(),
                           duplicate_k: int | None = None, tilt_mm: float = 0.0,
                           offsets=None) -> dict:
    """Write a CT series with pydicom and return its ground truth.

    Slice ``k`` sits at ``origin + k * slice_spacing * (row_dir x col_dir)``. Instance
    numbers run against ``k`` when ``descending`` and file names are shuffled, so a
    reader must sort by geometry. HU values encode ``(k, r, c)``; rescale slope and
    intercept vary per slice when ``vary_rescale``. ``offsets`` overrides the position of
    slice ``k`` along the normal (mm from the origin).
    """
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    row = np.array([float(_ds(x)) for x in row_dir])
    col = np.array([float(_ds(x)) for x in col_dir])
    normal = np.cross(row, col)
    normal /= np.linalg.norm(normal)
    origin = np.asarray(origin, float)
    study, series = generate_uid(), generate_uid()

    kk, rr, cc = np.meshgrid(np.arange(n_slices), np.arange(rows), np.arange(cols), indexing="ij")
    hu = (2 * ((kk * 37 + rr * 11 + cc * 3) % 1000) - 1000).astype(np.int16)  # even, -1000..998

    ks = [k for k in range(n_slices) if k not in set(skip_k)]
    if duplicate_k is not None:
        ks.append(duplicate_k)
    order = sorted(range(len(ks)), key=lambda i: ks[i], reverse=descending)
    names = [f"im{rng.randrange(10**8):08d}_{i}.dcm" for i in range(len(ks))]
    rng.shuffle(names)
    sops, paths = {}, []
    for inst, i in enumerate(order, start=1):
        k = ks[i]
        sop = generate_uid()
        slope, intercept = ((2.0, -1000.0) if k % 3 == 1 else (1.0, -1024.0 + 8 * (k % 2))) \
            if vary_rescale else (1.0, -1024.0)
        stored = ((hu[k].astype(np.float64) - intercept) / slope).astype(np.uint16)
        along = k * slice_spacing if offsets is None else offsets[k]
        pos = origin + along * normal + k * tilt_mm * row

        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = sop
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID, ds.SOPInstanceUID = CTImageStorage, sop
        ds.StudyInstanceUID, ds.SeriesInstanceUID = study, series
        ds.Modality, ds.PatientID, ds.InstanceNumber = "CT", "SYNTH-0001", inst
        ds.Rows, ds.Columns = rows, cols
        ds.PixelSpacing = [_ds(pixel_spacing[0]), _ds(pixel_spacing[1])]
        ds.ImageOrientationPatient = [_ds(x) for x in (*row, *col)]
        ds.ImagePositionPatient = [_ds(x) for x in pos]
        ds.SliceThickness = _ds(slice_spacing)
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 0
        ds.RescaleSlope, ds.RescaleIntercept = _ds(slope), _ds(intercept)
        ds.WindowCenter, ds.WindowWidth = "40", "400"
        ds.PixelData = stored.astype("<u2").tobytes()
        path = directory / names[inst - 1]
        ds.save_as(path, enforce_file_format=True)
        paths.append(path)
        sops[k] = sop

    affine = np.eye(4)
    affine[:3, 0] = row * pixel_spacing[1]
    affine[:3, 1] = col * pixel_spacing[0]
    affine[:3, 2] = normal * slice_spacing
    affine[:3, 3] = origin
    return {"paths": paths, "hu": hu, "affine_lps": affine, "normal": normal, "row": row, "col": col,
            "sop_uids": [sops[k] for k in range(n_slices) if k in sops], "series_uid": series,
            "study_uid": study, "shape": (n_slices, rows, cols)}


@pytest.fixture
def synthetic_series(tmp_path):
    """Factory: ``synthetic_series(name="s", **kwargs)`` writes a series under tmp_path."""
    def make(name: str = "series", **kwargs) -> dict:
        return write_synthetic_series(tmp_path / name, **kwargs)
    return make
