"""segment.py without inference: conv3d decomposition, stdout isolation, post-processing."""

import json
import subprocess

import numpy as np
import pytest

from covmap import dicom_io, organs, segment

torch = pytest.importorskip("torch")
F = torch.nn.functional


@pytest.mark.parametrize("mode", sorted(segment.CONV3D_FUNCS))
@pytest.mark.parametrize("cin,cout,shape,stride,pad", [
    (1, 4, (9, 10, 11), (1, 1, 1), (1, 1, 1)),
    (8, 6, (12, 9, 10), (2, 2, 2), (1, 1, 1)),
    (4, 5, (7, 8, 9), (1, 2, 1), (0, 1, 0)),
])
def test_conv3d_modes_match_native(mode, cin, cout, shape, stride, pad):
    g = torch.Generator().manual_seed(1)
    x = torch.randn(2, cin, *shape, generator=g)
    w = torch.randn(cout, cin, 3, 3, 3, generator=g)
    b = torch.randn(cout, generator=g)
    with torch.no_grad():
        ref = F.conv3d(x, w, b, stride, pad)
        out = segment.CONV3D_FUNCS[mode](x, w, b, stride, pad, 4096)  # forces chunking
        if mode == "sum2d_t":  # same conv2d calls and summation order as sum2d
            assert torch.equal(out, segment.conv3d_by_conv2d(x, w, b, stride, pad, 4096))
    assert out.shape == ref.shape
    assert float((out - ref).abs().max() / ref.abs().max()) < 1e-5


def test_conv3d_config(monkeypatch):
    monkeypatch.delenv(segment.CONV3D_ENV, raising=False)
    monkeypatch.delenv(segment.CONV3D_BUDGET_ENV, raising=False)
    assert segment.conv3d_config("mps") == {"mode": "sum2d_t", "budget_mb": 256, "source": "default"}
    monkeypatch.setenv(segment.CONV3D_ENV, "fold2d")
    monkeypatch.setenv(segment.CONV3D_BUDGET_ENV, "64")
    assert segment.conv3d_config("cpu") == {"mode": "fold2d", "budget_mb": 64, "source": "environment"}
    monkeypatch.setenv(segment.CONV3D_ENV, "chunk3d")
    with pytest.raises(ValueError):
        segment.conv3d_config("mps")  # would run the broken MPS Conv3d
    monkeypatch.setenv(segment.CONV3D_ENV, "bogus")
    with pytest.raises(ValueError):
        segment.conv3d_config("cpu")


def test_lowmem_patch_is_scoped():
    conv = torch.nn.Conv3d(2, 3, 3, padding=1)
    dil = torch.nn.Conv3d(2, 3, 3, padding=2, dilation=2)
    x = torch.randn(1, 2, 6, 7, 8)
    with torch.no_grad():
        ref, ref_dil = conv(x), dil(x)
        with segment.lowmem_cpu_conv3d() as rec:
            out, out_dil = conv(x), dil(x)
    assert rec["calls"] == {"decomposed": 1, "native": 1}
    assert torch.allclose(out, ref, atol=1e-5) and torch.equal(out_dil, ref_dil)
    assert torch.nn.Conv3d._conv_forward.__name__ == "_conv_forward"


@pytest.mark.skipif(not torch.backends.mps.is_available(), reason="needs Apple MPS")
def test_mps_refuses_native_conv3d():
    # native MPS Conv3d is numerically wrong, so a layer the decomposed path cannot take must fail
    dil = torch.nn.Conv3d(2, 3, 3, padding=2, dilation=2).to("mps")
    with torch.no_grad(), segment.decomposed_conv3d(segment.CONV3D_DEFAULTS["mps"], device="mps"):
        with pytest.raises(RuntimeError, match="--device cpu"):
            dil(torch.randn(1, 2, 6, 7, 8, device="mps"))


def test_select_device_auto(monkeypatch):
    monkeypatch.delenv(segment.CONV3D_ENV, raising=False)
    if torch.backends.mps.is_available():
        dev, conv, info = segment.select_device("auto")
        assert dev == "mps" and conv["mode"] == "sum2d_t" and info["requested"] == "auto"
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    dev, conv, info = segment.select_device("auto")
    assert dev == "cpu" and info["auto_fallback"] == "MPS unavailable"
    with pytest.raises(ValueError):
        segment.select_device("cuda")


@pytest.mark.skipif(not torch.backends.mps.is_available(), reason="no MPS")
def test_mps_self_test_uses_decomposed_conv(monkeypatch):
    monkeypatch.delenv(segment.CONV3D_ENV, raising=False)
    info = segment.check_device("mps")
    assert info["self_test"]["rel_err"] <= 1e-4 and info["self_test"]["decomposed_calls"] == 3
    assert torch.nn.Conv3d._conv_forward.__name__ == "_conv_forward"


def test_stdout_to_stderr_covers_child_processes(capfd):
    with segment.stdout_to_stderr():
        print("python-level")
        subprocess.run(["echo", "child-level"], check=True)
    print("after")
    out, err = capfd.readouterr()
    assert out == "after\n"
    assert "python-level" in err and "child-level" in err


def test_organ_stats_on_known_labels():
    geom = {"affine_lps": np.diag([0.5, 0.5, 2.0, 1.0]).tolist(), "sop_uids": [f"s{k}" for k in range(10)]}
    labels = np.zeros((10, 20, 30), np.uint8)
    liver = organs.BY_KEY["liver"].id
    labels[2:7, 4:9, 10:20] = liver          # 5*5*10 = 250 voxels
    labels[0, 0, 0] = organs.BY_KEY["spleen"].id
    found, below = segment.organ_stats(labels, geom, min_voxels=100)
    assert [o["key"] for o in found] == ["liver"] and below == {"spleen": 1}
    o = found[0]
    assert o["voxels"] == 250 and o["k_range"] == [2, 6] and o["bbox_rc"] == [4, 8, 10, 19]
    assert o["center_k"] == 4 and o["center_sop"] == "s4"
    assert np.allclose(o["centroid_lps"], [14.5 * 0.5, 6 * 0.5, 4 * 2.0])


def _reuse_case(synthetic_series, tmp_path, monkeypatch, with_header=False):
    """A synthetic case with a TotalSegmentator-style seg file and the producing run's record."""
    nib = pytest.importorskip("nibabel")
    pytest.importorskip("totalsegmentator")
    from totalsegmentator.map_to_binary import class_map

    monkeypatch.setenv("COVMAP_DATA", str(tmp_path / "data"))
    s = synthetic_series(rows=40, cols=36, n_slices=20, row_dir=(-1, 0, 0), col_dir=(0, 1, 0))
    geom, datasets, _ = dicom_io.read_series(s["paths"])
    vol, _ = dicom_io.hu_volume(datasets)
    cdir = tmp_path / "data" / "cases" / "synth"
    (cdir / "seg").mkdir(parents=True)
    (cdir / "volume.json").write_text(json.dumps({"case": "synth", **geom}))
    dicom_io.write_nifti(vol, geom, cdir / "ct.nii.gz")

    inv = {v: k for k, v in class_map["total"].items()}
    truth = np.zeros(s["shape"], np.uint8)          # [k, r, c] in catalog ids
    ts = np.zeros(s["shape"], np.uint8)             # [k, r, c] in TotalSegmentator values
    for cls, sl in (("liver", np.s_[2:12, 5:25, 3:20]), ("lung_upper_lobe_left", np.s_[12:18, 2:20, 18:34]),
                    ("lung_lower_lobe_left", np.s_[3:12, 26:38, 22:34]),
                    ("adrenal_gland_left", np.s_[15:18, 30:38, 2:8])):
        ts[sl] = inv[cls]
        truth[sl] = organs.CLASS_TO_ORGAN[cls].id if cls in organs.CLASS_TO_ORGAN else 0
    img = nib.Nifti1Image(ts.transpose(2, 1, 0), dicom_io.nifti_affine_ras(geom))
    img = nib.as_closest_canonical(img)  # a different axis order than ct.nii.gz on purpose
    if with_header:
        from totalsegmentator.nifti_ext_header import add_label_map_to_nifti
        img = add_label_map_to_nifti(img, class_map["total"])
    nib.save(img, str(cdir / "seg" / "totalseg.nii.gz"))
    producing = {
        "options": {"task": "total", "fast": False, "roi_subset": False, "device": "cpu", "reuse": False},
        "versions": {"TotalSegmentator": "2.4.0", "covmap": "0.0.1-old"},
        "input": {"ct_nii": "/elsewhere/ct.nii.gz", "ct_nii_sha256": segment._sha256(cdir / "ct.nii.gz")},
        "totalsegmentator": {"wall_s": 12.5, "kwargs": {"ml": True, "fast": False, "roi_subset": None}},
        "hashes_sha256": {"totalseg.nii.gz": segment._sha256(cdir / "seg" / "totalseg.nii.gz")},
    }
    (cdir / "seg" / "segment.json").write_text(json.dumps(producing))
    return cdir, truth, producing


@pytest.mark.parametrize("with_header", [False, True])
def test_postprocess_reuse_on_synthetic_case(with_header, synthetic_series, tmp_path, monkeypatch):
    cdir, truth, producing = _reuse_case(synthetic_series, tmp_path, monkeypatch, with_header)
    original_run = producing["totalsegmentator"]

    segment.segment_case("synth", reuse=True, min_voxels=500)
    out = segment.segment_case("synth", reuse=True, min_voxels=500)  # reuse twice: no nesting
    labels = np.load(cdir / "seg" / "labels.npy")
    assert labels.dtype == np.uint8 and np.array_equal(labels, truth)
    assert out["regrid"] == "permutation"
    assert out["totalsegmentator"] == {"reused": True}
    assert out["organs"] == {"lung_left": int((truth == 1).sum()), "liver": int((truth == 9).sum())}
    doc = json.loads((cdir / "seg" / "organs.json").read_text())
    assert [o["key"] for o in doc["organs"]] == ["lung_left", "liver"]
    assert list(doc["organs"][0]) == ["id", "key", "name", "group", "windows", "color_rgb", "voxels",
                                      "k_range", "center_k", "center_sop", "bbox_rc", "centroid_lps",
                                      "totalseg_classes"]
    record = json.loads((cdir / "seg" / "segment.json").read_text())
    assert record["totalsegmentator"] == {"reused": True, "previous": original_run}
    # the producing run's provenance is carried forward; this run is only a marker
    assert doc["label_source"] == record["label_source"] == "TotalSegmentator 2.4.0 total"
    for key in ("options", "versions", "input"):
        assert record[key] == producing[key]
    assert record["reuse"]["reused_by_versions"]["covmap"] == segment.__version__
    assert record["reuse"]["request"]["reuse"] is True and record["reuse"]["reused_at"]
    assert record["grid"]["label_map_source"].startswith("nifti" if with_header else "class_map")
    assert set(record["hashes_sha256"]) == {"totalseg.nii.gz", "labels.npy", "organs.json",
                                            "meshes.bin", "meshes.json"}
    assert (cdir / "seg" / "meshes.bin").stat().st_size > 0


def test_reuse_refuses_other_ct_flags_or_no_record(synthetic_series, tmp_path, monkeypatch):
    cdir, _, producing = _reuse_case(synthetic_series, tmp_path, monkeypatch)
    rec = cdir / "seg" / "segment.json"

    def refused(match, **kw):
        with pytest.raises(segment.SegmentError, match=match):
            segment.segment_case("synth", reuse=True, min_voxels=500, **kw)
        assert not (cdir / "seg" / "labels.npy").exists()  # nothing written

    refused("fast=True", fast=True)
    refused("roi_subset=True", roi_subset=True)
    rec.write_text(json.dumps({**producing, "input": {"ct_nii_sha256": "0" * 64}}))
    refused("not the CT")
    # another totalseg.nii.gz under this record (interrupted rerun or hand copy)
    rec.write_text(json.dumps({**producing, "hashes_sha256": {"totalseg.nii.gz": "0" * 64}}))
    refused("not the file")
    # record options say fast=False, but TotalSegmentator was actually called with fast=True
    rec.write_text(json.dumps({**producing, "totalsegmentator": {"kwargs": {"fast": True, "roi_subset": None}}}))
    refused("fast=False")
    rec.write_text(json.dumps({k: v for k, v in producing.items() if k not in ("options", "totalsegmentator")}))
    refused("unknown")  # nothing records the producing flags
    rec.unlink()
    refused("no record")
