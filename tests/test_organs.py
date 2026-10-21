"""Organ catalog sanity: stable ids, windows per SPEC, classes exist in TotalSegmentator."""

import numpy as np
import pytest

from covmap import organs

# Published ids must never change (labels.npy and stored coverage depend on them).
STABLE_IDS = {
    "lung_left": 1, "lung_right": 2, "trachea": 3, "heart": 4, "aorta": 5, "inferior_vena_cava": 6,
    "esophagus": 7, "thyroid": 8, "liver": 9, "spleen": 10, "pancreas": 11, "gallbladder": 12,
    "stomach": 13, "kidney_left": 14, "kidney_right": 15, "duodenum": 16, "small_bowel": 17,
    "colon": 18, "urinary_bladder": 19, "cervical_spine": 20, "thoracic_spine": 21,
    "lumbar_spine": 22, "sacrum": 23, "ribs_left": 24, "ribs_right": 25, "sternum": 26,
    "shoulder_left": 27, "shoulder_right": 28, "hip_left": 29, "hip_right": 30, "femur_left": 31,
    "femur_right": 32, "brain": 33, "skull": 34,
}


def test_ids_and_keys_stable_and_unique():
    assert {o.key: o.id for o in organs.CATALOG} == STABLE_IDS
    assert len(organs.BY_ID) == len(organs.BY_KEY) == len(organs.CATALOG)
    assert all(0 < o.id < 256 for o in organs.CATALOG)


def test_windows_follow_spec():
    for o in organs.CATALOG:
        if o.key.startswith("lung_"):
            assert o.windows == ("lung",), o.key
        elif o.key == "trachea":
            assert o.windows == ("lung", "soft")
        elif o.group == "skeleton" or o.key == "skull":
            assert o.windows == ("bone",), o.key
        else:
            assert o.windows == ("soft",), o.key


def test_groups_names_colors():
    assert {o.group for o in organs.CATALOG} == set(organs.GROUPS)
    colors = [o.color_rgb for o in organs.CATALOG]
    assert len(set(colors)) == len(colors)
    for o in organs.CATALOG:
        assert o.name and len(o.color_rgb) == 3 and all(0 <= v <= 255 for v in o.color_rgb)
        d = o.as_dict()
        assert d["windows"] == list(o.windows) and d["totalseg_classes"] == list(o.totalseg_classes)


def test_classes_exist_in_installed_totalsegmentator():
    pytest.importorskip("totalsegmentator")
    from totalsegmentator.map_to_binary import class_map

    total = set(class_map["total"].values())
    used = organs.catalog_classes()
    assert len(used) == len(set(used))
    assert sorted(set(used) - total) == []
    expected_groups = {
        "lung_left": {"lung_upper_lobe_left", "lung_lower_lobe_left"},
        "lung_right": {"lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right"},
        "ribs_left": {f"rib_left_{i}" for i in range(1, 13)},
        "ribs_right": {f"rib_right_{i}" for i in range(1, 13)},
        "thoracic_spine": {f"vertebrae_T{i}" for i in range(1, 13)},
        "sternum": {"sternum", "costal_cartilages"},
        "shoulder_left": {"scapula_left", "clavicula_left", "humerus_left"},
    }
    for key, classes in expected_groups.items():
        assert set(organs.BY_KEY[key].totalseg_classes) == classes


def test_label_lut_maps_total_classes():
    pytest.importorskip("totalsegmentator")
    from totalsegmentator.map_to_binary import class_map

    lut = organs.label_lut(class_map["total"])
    inv = {v: k for k, v in class_map["total"].items()}
    assert lut.dtype == np.uint8 and lut[0] == 0
    assert lut[inv["liver"]] == organs.BY_KEY["liver"].id
    assert lut[inv["lung_middle_lobe_right"]] == organs.BY_KEY["lung_right"].id
    assert lut[inv["rib_left_7"]] == organs.BY_KEY["ribs_left"].id
    assert lut[inv["adrenal_gland_left"]] == 0  # not in the catalog -> background
