"""DICOM sorting, affine, HU, LPS->RAS NIfTI round trip and label re-gridding."""

import numpy as np
import pytest

from covmap import dicom_io


def _rotation(ax, ay, az):
    ax, ay, az = np.radians([ax, ay, az])
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    ry = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    return rz @ ry @ rx


_R = _rotation(20, -15, 35)
ORIENTATIONS = {
    "axial": dict(row_dir=(1, 0, 0), col_dir=(0, 1, 0)),
    "axial_normal_down": dict(row_dir=(-1, 0, 0), col_dir=(0, 1, 0)),
    "coronal": dict(row_dir=(1, 0, 0), col_dir=(0, 0, -1)),
    "oblique": dict(row_dir=tuple(_R[:, 0]), col_dir=tuple(_R[:, 1])),
}


@pytest.fixture(params=sorted(ORIENTATIONS))
def series(request, synthetic_series):
    return synthetic_series(**ORIENTATIONS[request.param], descending=request.param != "coronal")


def test_sorting_affine_and_hu(series):
    geom, datasets, checks = dicom_io.read_series(series["paths"], series_uid=series["series_uid"])
    assert geom["sop_uids"] == series["sop_uids"]
    assert (geom["n_slices"], geom["rows"], geom["cols"]) == series["shape"]
    assert np.allclose(geom["affine_lps"], series["affine_lps"], atol=1e-6)
    assert np.allclose(geom["slice_dir"], series["normal"], atol=1e-9)
    assert geom["slice_spacing"] == pytest.approx(2.0)
    assert geom["pixel_spacing"] == pytest.approx([0.8, 0.7])
    assert geom["default_window"] == {"wc": 40.0, "ww": 400.0}
    assert checks["max_spacing_deviation_mm"] < 1e-6

    # SPEC formula: origin + c*ps[1]*row_dir + r*ps[0]*col_dir + k*slice_spacing*slice_dir,
    # and slice k's origin is its ImagePositionPatient.
    for k, ds in enumerate(datasets):
        assert np.allclose(dicom_io.voxel_to_lps(geom, [k, 0, 0]), [float(x) for x in ds.ImagePositionPatient],
                           atol=1e-6)
    k, r, c = 5, 7, 3
    expect = (np.array(geom["origin_lps"]) + c * 0.7 * np.array(geom["row_dir"])
              + r * 0.8 * np.array(geom["col_dir"]) + k * 2.0 * np.array(geom["slice_dir"]))
    assert np.allclose(dicom_io.voxel_to_lps(geom, [k, r, c]), expect)

    vol, info = dicom_io.hu_volume(datasets)
    assert vol.dtype == np.int16
    assert np.array_equal(vol, series["hu"])
    assert len(info["rescale_pairs"]) > 1  # per-slice rescale exercised


def _sitk_world(img):
    """World LPS of every voxel of a SimpleITK image, as ``[z, y, x, 3]``."""
    size = img.GetSize()
    idx = np.stack(np.meshgrid(*[np.arange(n) for n in size[::-1]], indexing="ij"), axis=-1)[..., ::-1]
    d = np.array(img.GetDirection()).reshape(3, 3)
    return idx * np.array(img.GetSpacing()) @ d.T + np.array(img.GetOrigin())


@pytest.mark.parametrize("name", sorted(ORIENTATIONS))
def test_matches_simpleitk_dicom_and_nifti_roundtrip(name, synthetic_series, tmp_path):
    """SimpleITK (GDCM) is an independent oracle for DICOM geometry and for our NIfTI."""
    sitk = pytest.importorskip("SimpleITK")
    s = synthetic_series(**ORIENTATIONS[name], vary_rescale=False)
    geom, datasets, _ = dicom_io.read_series(s["paths"])
    vol, _ = dicom_io.hu_volume(datasets)
    nii = tmp_path / "ct.nii.gz"
    dicom_io.write_nifti(vol, geom, nii)

    inv = np.linalg.inv(np.asarray(geom["affine_lps"]))
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(s["paths"][0].parent)))
    for img in (reader.Execute(), sitk.ReadImage(str(nii))):
        world = _sitk_world(img)
        crk = world.reshape(-1, 3) @ inv[:3, :3].T + inv[:3, 3]
        assert np.allclose(crk, np.rint(crk), atol=1e-3)
        c, r, k = np.rint(crk).astype(int).T
        assert ((k >= 0) & (k < vol.shape[0]) & (r >= 0) & (r < vol.shape[1])
                & (c >= 0) & (c < vol.shape[2])).all()
        values = sitk.GetArrayFromImage(img).reshape(-1)
        assert np.array_equal(values.astype(np.int32), vol[k, r, c].astype(np.int32))


def test_nifti_is_ras_of_lps(synthetic_series, tmp_path):
    nib = pytest.importorskip("nibabel")
    s = synthetic_series(**ORIENTATIONS["oblique"])
    geom, datasets, _ = dicom_io.read_series(s["paths"])
    vol, _ = dicom_io.hu_volume(datasets)
    path = tmp_path / "ct.nii.gz"
    dicom_io.write_nifti(vol, geom, path)
    img = nib.load(str(path))
    assert img.get_data_dtype() == np.int16
    assert img.header["sform_code"] == 1 and img.header["qform_code"] == 1
    data = np.asanyarray(img.dataobj)
    assert data.shape == (vol.shape[2], vol.shape[1], vol.shape[0])
    rng = np.random.default_rng(0)
    for _ in range(50):
        k, r, c = (int(rng.integers(n)) for n in vol.shape)
        ras = img.affine @ [c, r, k, 1]
        assert np.allclose([-ras[0], -ras[1], ras[2]], dicom_io.voxel_to_lps(geom, [k, r, c]), atol=1e-4)
        assert data[c, r, k] == vol[k, r, c]
    assert np.allclose(img.get_qform(), img.affine, atol=1e-4)


def _labels(shape, seed=1):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 6, size=shape).astype(np.uint8)


@pytest.mark.parametrize("name", sorted(ORIENTATIONS))
def test_regrid_permutations_exact(name, synthetic_series):
    nib = pytest.importorskip("nibabel")
    from nibabel.orientations import apply_orientation, inv_ornt_aff

    s = synthetic_series(**ORIENTATIONS[name])
    geom, _, _ = dicom_io.read_series(s["paths"])
    labels = _labels(s["shape"])
    nifti = labels.transpose(2, 1, 0)
    aff = dicom_io.nifti_affine_ras(geom)

    out, info = dicom_io.regrid_to_dicom(nifti, aff, geom)
    assert info["mode"] == "permutation" and np.array_equal(out, labels)

    can = nib.as_closest_canonical(nib.Nifti1Image(nifti, aff))
    out, info = dicom_io.regrid_to_dicom(np.asanyarray(can.dataobj), can.affine, geom)
    assert info["mode"] == "permutation" and np.array_equal(out, labels)

    ornt = np.array([[2, -1], [0, 1], [1, -1]])
    data = apply_orientation(nifti, ornt)
    out, info = dicom_io.regrid_to_dicom(data, aff @ inv_ornt_aff(ornt, nifti.shape), geom)
    assert info["mode"] == "permutation" and np.array_equal(out, labels)
    assert {a["sign"] for a in info["axes"]} == {-1, 1}


def test_regrid_resampled_nearest(synthetic_series):
    s = synthetic_series(**ORIENTATIONS["oblique"], rows=24, cols=20)
    geom, _, _ = dicom_io.read_series(s["paths"])
    n_k, n_r, n_c = s["shape"]
    coarse = _labels((n_c // 2, n_r // 2, n_k), seed=3)  # NIfTI order (c, r, k), 2x in-plane
    aff = dicom_io.nifti_affine_ras(geom).copy()
    aff[:3, 0] *= 2
    aff[:3, 1] *= 2
    # coarse voxel centres sit between fine voxels 0 and 1, 2 and 3, ...
    aff[:3, 3] += 0.5 * (aff[:3, 0] + aff[:3, 1]) / 2
    out, info = dicom_io.regrid_to_dicom(coarse, aff, geom)
    assert info["mode"] == "resampled_nearest"
    kk, rr, cc = np.meshgrid(np.arange(n_k), np.arange(n_r), np.arange(n_c), indexing="ij")
    assert np.array_equal(out, coarse[cc // 2, rr // 2, kk])


def test_rejects_gap(synthetic_series):
    s = synthetic_series(skip_k=(5,))
    with pytest.raises(dicom_io.SeriesError, match="gap"):
        dicom_io.read_series(s["paths"])


def test_rejects_duplicate_position(synthetic_series):
    s = synthetic_series(duplicate_k=4)
    with pytest.raises(dicom_io.SeriesError, match="duplicate slice position"):
        dicom_io.read_series(s["paths"])


def test_rejects_cumulative_drift(synthetic_series):
    # every step is within 5% of the mean, but slice 6 sits 0.44 mm off the uniform grid
    steps = [2.08] * 6 + [1.92] * 5
    s = synthetic_series(n_slices=12, slice_spacing=2.0, offsets=np.concatenate([[0], np.cumsum(steps)]))
    with pytest.raises(dicom_io.SeriesError, match="uniform grid"):
        dicom_io.read_series(s["paths"])


def test_fetch_rederives_missing_or_edited_volume_json(synthetic_series, tmp_path, monkeypatch):
    import json
    import shutil
    import zipfile

    from covmap.fetch import fetch_case, manifest_sha256, sha256_file

    s = synthetic_series()
    monkeypatch.setenv("COVMAP_DATA", str(tmp_path / "data"))
    cdir = tmp_path / "data" / "cases" / "synth"
    (cdir / "source").mkdir(parents=True)
    with zipfile.ZipFile(cdir / "source.zip", "w") as z:
        for p in s["paths"]:
            z.write(p, p.name)
            shutil.copy(p, cdir / "source" / p.name)
    files = {p.name: sha256_file(p) for p in (cdir / "source").glob("*.dcm")}
    (cdir / "provenance.json").write_text(json.dumps({
        "series_uid": s["series_uid"], "files": files, "manifest_sha256": manifest_sha256(files),
        "zip": {"file": "source.zip", "sha256": sha256_file(cdir / "source.zip")}}))

    first = fetch_case("synth", series_uid=s["series_uid"])
    assert first["download"].startswith("skipped") and first["derived_rebuilt"]
    vol = cdir / "volume.json"
    good = vol.read_text()
    assert not fetch_case("synth", series_uid=s["series_uid"])["derived_rebuilt"]
    for damage in (vol.unlink, lambda: vol.write_text(good.replace('"n_slices": 12', '"n_slices": 11'))):
        damage()
        assert fetch_case("synth", series_uid=s["series_uid"])["derived_rebuilt"]
        assert vol.read_text() == good
        prov = json.loads((cdir / "provenance.json").read_text())
        assert prov["derived"]["volume_json_sha256"] == sha256_file(vol)


def test_rejects_gantry_tilt(synthetic_series):
    s = synthetic_series(tilt_mm=0.5)
    with pytest.raises(dicom_io.SeriesError, match="tilt"):
        dicom_io.read_series(s["paths"])


def test_rejects_mixed_series(synthetic_series):
    a = synthetic_series("a")
    b = synthetic_series("b")
    with pytest.raises(dicom_io.SeriesError, match="2 series"):
        dicom_io.read_series(a["paths"] + b["paths"][:2])
    with pytest.raises(dicom_io.SeriesError, match="expected"):
        dicom_io.read_series(a["paths"], series_uid=b["series_uid"])
