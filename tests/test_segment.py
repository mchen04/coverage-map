"""segment.py without inference: conv3d decomposition, stdout isolation, post-processing."""

import json
import subprocess

import numpy as np
import pytest

from covmap import dicom_io, organs, segment

torch = pytest.importorskip("torch")
F = torch.nn.functional


@pytest.mark.parametrize("cin,cout,shape,stride,pad", [
    (1, 4, (9, 10, 11), (1, 1, 1), (1, 1, 1)),
    (8, 6, (12, 9, 10), (2, 2, 2), (1, 1, 1)),
    (4, 5, (7, 8, 9), (1, 2, 1), (0, 1, 0)),
])
def test_conv3d_by_conv2d_matches_native(cin, cout, shape, stride, pad):
    g = torch.Generator().manual_seed(1)
    x = torch.randn(2, cin, *shape, generator=g)
    w = torch.randn(cout, cin, 3, 3, 3, generator=g)
    b = torch.randn(cout, generator=g)
    with torch.no_grad():
        ref = F.conv3d(x, w, b, stride, pad)
        out = segment.conv3d_by_conv2d(x, w, b, stride, pad, budget_bytes=4096)  # forces chunking
    assert out.shape == ref.shape
    assert float((out - ref).abs().max() / ref.abs().max()) < 1e-5


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


@pytest.mark.parametrize("with_header", [False, True])
def test_postprocess_reuse_on_synthetic_case(with_header, synthetic_series, tmp_path, monkeypatch):
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
                    ("lung_lower_lobe_left", np.s_[3:12, 26:38, 22:34]), ("adrenal_gland_left", np.s_[15:18, 30:38, 2:8])):
        ts[sl] = inv[cls]
        truth[sl] = organs.CLASS_TO_ORGAN[cls].id if cls in organs.CLASS_TO_ORGAN else 0
    img = nib.Nifti1Image(ts.transpose(2, 1, 0), dicom_io.nifti_affine_ras(geom))
    img = nib.as_closest_canonical(img)  # a different axis order than ct.nii.gz on purpose
    if with_header:
        from totalsegmentator.nifti_ext_header import add_label_map_to_nifti
        img = add_label_map_to_nifti(img, class_map["total"])
    nib.save(img, str(cdir / "seg" / "totalseg.nii.gz"))
    original_run = {"wall_s": 12.5, "kwargs": {"ml": True}}
    (cdir / "seg" / "segment.json").write_text(json.dumps({"totalsegmentator": original_run}))

    segment.segment_case("synth", reuse=True, min_voxels=500)
    out = segment.segment_case("synth", reuse=True, min_voxels=500)  # reuse twice: no nesting
    labels = np.load(cdir / "seg" / "labels.npy")
    assert labels.dtype == np.uint8 and np.array_equal(labels, truth)
    assert out["regrid"] == "permutation"
    assert out["organs"] == {"lung_left": int((truth == 1).sum()), "liver": int((truth == 9).sum())}
    doc = json.loads((cdir / "seg" / "organs.json").read_text())
    assert [o["key"] for o in doc["organs"]] == ["lung_left", "liver"]
    assert list(doc["organs"][0]) == ["id", "key", "name", "group", "windows", "color_rgb", "voxels",
                                      "k_range", "center_k", "center_sop", "bbox_rc", "centroid_lps",
                                      "totalseg_classes"]
    record = json.loads((cdir / "seg" / "segment.json").read_text())
    assert record["totalsegmentator"] == {"reused": True, "previous": original_run}
    assert record["grid"]["label_map_source"].startswith("nifti" if with_header else "class_map")
    assert set(record["hashes_sha256"]) == {"totalseg.nii.gz", "labels.npy", "organs.json",
                                            "meshes.bin", "meshes.json"}
    assert (cdir / "seg" / "meshes.bin").stat().st_size > 0
