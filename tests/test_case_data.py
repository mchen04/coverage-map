"""Real public cases (skipped unless fetched and segmented in $COVMAP_DATA).

HU and laterality checks prove the labels sit on the right voxels: a slice-order
flip puts liver labels into the chest, a left/right flip swaps liver and spleen.
"""

import json

import numpy as np
import pytest

from covmap import dicom_io, organs
from covmap.fetch import manifest_sha256, sha256_file
from covmap.paths import case_dir

pytestmark = pytest.mark.data

EXPECTED = {
    "lidc-0001": ["lung_left", "lung_right", "heart", "aorta", "thoracic_spine", "ribs_left", "ribs_right"],
    "pancreas-0051": ["liver", "spleen", "kidney_left", "kidney_right", "pancreas", "lumbar_spine"],
}


@pytest.fixture(scope="module", params=sorted(EXPECTED))
def case(request):
    cdir = case_dir(request.param)
    seg = cdir / "seg"
    if not (seg / "labels.npy").exists() or not (cdir / "volume.json").exists():
        pytest.skip(f"{request.param} not fetched+segmented in {cdir}")
    volume = json.loads((cdir / "volume.json").read_text())
    geom, datasets, _ = dicom_io.read_series(sorted((cdir / "source").glob("*.dcm")), volume["series_uid"])
    hu, _ = dicom_io.hu_volume(datasets)
    return {
        "id": request.param, "dir": cdir, "volume": volume, "geom": geom, "hu": hu,
        "labels": np.load(seg / "labels.npy"),
        "organs": json.loads((seg / "organs.json").read_text()),
    }


def test_provenance_hashes(case):
    prov = json.loads((case["dir"] / "provenance.json").read_text())
    assert prov["license_name"].startswith("Creative Commons Attribution")
    assert prov["image_url"].startswith("https://services.cancerimagingarchive.net/nbia-api/services/v1/")
    assert sha256_file(case["dir"] / prov["zip"]["file"]) == prov["zip"]["sha256"]
    files = {p.name: sha256_file(p) for p in (case["dir"] / "source").glob("*.dcm")}
    assert files == prov["files"] and manifest_sha256(files) == prov["manifest_sha256"]
    assert prov["n_files"] == case["volume"]["n_slices"]


def test_volume_json_matches_dicom(case):
    v, g = case["volume"], case["geom"]
    assert {k: v[k] for k in g} == g
    assert v["case"] == case["id"]


def test_labels_grid_and_catalog(case):
    labels, v = case["labels"], case["volume"]
    assert labels.dtype == np.uint8
    assert labels.shape == (v["n_slices"], v["rows"], v["cols"])
    counts = np.bincount(labels.ravel(), minlength=256)
    assert set(np.flatnonzero(counts)) - {0} <= set(organs.BY_ID)
    doc = case["organs"]
    assert doc["case"] == case["id"]
    for o in doc["organs"]:
        assert organs.BY_ID[o["id"]].key == o["key"]
        assert o["voxels"] == counts[o["id"]] >= 2000
        k0, k1 = o["k_range"]
        assert k0 <= o["center_k"] <= k1
        assert o["center_sop"] == v["sop_uids"][o["center_k"]]
        ks = np.flatnonzero((labels == o["id"]).any(axis=(1, 2)))
        assert (ks[0], ks[-1]) == (k0, k1)


def test_expected_organs_present(case):
    found = {o["key"] for o in case["organs"]["organs"]}
    missing = [k for k in EXPECTED[case["id"]] if k not in found]
    assert not missing, f"{case['id']} missing {missing}; found {sorted(found)}"


def _hu(case, key):
    return case["hu"][case["labels"] == organs.BY_KEY[key].id]


def test_hu_alignment(case):
    found = {o["key"] for o in case["organs"]["organs"]}
    for key in ("lung_left", "lung_right"):
        if key in found:
            assert (_hu(case, key) < -500).mean() > 0.8, key
    bone_ids = [organs.BY_KEY[k].id for k in found if organs.BY_KEY[k].windows == ("bone",)]
    assert bone_ids
    bone = case["hu"][np.isin(case["labels"], bone_ids)]
    assert (bone > 150).mean() > 0.5
    for key in found & {"thoracic_spine", "lumbar_spine"}:
        assert (_hu(case, key) > 150).mean() > 0.5, key
    if "liver" in found:
        assert 20 <= np.median(_hu(case, "liver")) <= 200


def test_laterality(case):
    """Patient-left organs have larger LPS x than their right-side counterparts."""
    c = {o["key"]: o["centroid_lps"] for o in case["organs"]["organs"]}
    pairs = [("lung_right", "lung_left"), ("kidney_right", "kidney_left"), ("liver", "spleen"),
             ("ribs_right", "ribs_left")]
    checked = 0
    for right, left in pairs:
        if right in c and left in c:
            assert c[right][0] < c[left][0], (right, left)
            checked += 1
    assert checked >= 2


def test_meshes_inside_volume(case):
    from covmap.meshes import load_meshes

    loaded = load_meshes(case["dir"] / "seg")
    keys = [o["key"] for o in case["organs"]["organs"]]
    assert sorted(loaded["organs"]) == sorted(keys)
    assert sum(len(m["tris"]) for m in loaded["organs"].values()) <= 250_000
    inv = np.linalg.inv(np.asarray(case["volume"]["affine_lps"]))
    v = case["volume"]
    extent = np.array([v["cols"], v["rows"], v["n_slices"]])
    for key, m in loaded["organs"].items():
        crk = m["points"].astype(float) @ inv[:3, :3].T + inv[:3, 3]
        assert (crk >= -0.5 - 1e-3).all() and (crk <= extent - 0.5 + 1e-3).all(), key
