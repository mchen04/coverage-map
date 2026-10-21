"""Stable organ catalog: display organs built from TotalSegmentator v2 ``total`` classes.

Ids and keys never change once published (labels.npy, organs.json and stored
coverage depend on them); append new organs with new ids. ``color_rgb`` is a
neutral legend color only; the coverage map colors by status.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

GROUPS = ("thorax", "abdomen", "pelvis", "skeleton", "head")
WINDOW_CLASSES = ("lung", "bone", "soft")


@dataclass(frozen=True)
class Organ:
    id: int
    key: str
    name: str
    group: str
    windows: tuple[str, ...]
    color_rgb: tuple[int, int, int]
    totalseg_classes: tuple[str, ...]

    def as_dict(self) -> dict:
        d = asdict(self)
        d["windows"] = list(self.windows)
        d["color_rgb"] = list(self.color_rgb)
        d["totalseg_classes"] = list(self.totalseg_classes)
        return d


def _seq(fmt: str, items) -> tuple[str, ...]:
    return tuple(fmt.format(i) for i in items)


LUNG, BONE, SOFT = ("lung",), ("bone",), ("soft",)

CATALOG: tuple[Organ, ...] = (
    Organ(1, "lung_left", "Left lung", "thorax", LUNG, (138, 166, 204),
          ("lung_upper_lobe_left", "lung_lower_lobe_left")),
    Organ(2, "lung_right", "Right lung", "thorax", LUNG, (110, 142, 186),
          ("lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right")),
    Organ(3, "trachea", "Trachea", "thorax", ("lung", "soft"), (120, 186, 196), ("trachea",)),
    Organ(4, "heart", "Heart", "thorax", SOFT, (186, 104, 104), ("heart",)),
    Organ(5, "aorta", "Aorta", "thorax", SOFT, (214, 84, 76), ("aorta",)),
    Organ(6, "inferior_vena_cava", "Inferior vena cava", "thorax", SOFT, (92, 106, 168),
          ("inferior_vena_cava",)),
    Organ(7, "esophagus", "Esophagus", "thorax", SOFT, (204, 150, 116), ("esophagus",)),
    Organ(8, "thyroid", "Thyroid", "thorax", SOFT, (164, 112, 160), ("thyroid_gland",)),
    Organ(9, "liver", "Liver", "abdomen", SOFT, (150, 86, 66), ("liver",)),
    Organ(10, "spleen", "Spleen", "abdomen", SOFT, (132, 76, 120), ("spleen",)),
    Organ(11, "pancreas", "Pancreas", "abdomen", SOFT, (222, 186, 110), ("pancreas",)),
    Organ(12, "gallbladder", "Gallbladder", "abdomen", SOFT, (106, 156, 86), ("gallbladder",)),
    Organ(13, "stomach", "Stomach", "abdomen", SOFT, (212, 150, 142), ("stomach",)),
    Organ(14, "kidney_left", "Left kidney", "abdomen", SOFT, (176, 96, 84),
          ("kidney_left", "kidney_cyst_left")),
    Organ(15, "kidney_right", "Right kidney", "abdomen", SOFT, (160, 84, 100),
          ("kidney_right", "kidney_cyst_right")),
    Organ(16, "duodenum", "Duodenum", "abdomen", SOFT, (226, 170, 136), ("duodenum",)),
    Organ(17, "small_bowel", "Small bowel", "abdomen", SOFT, (236, 196, 160), ("small_bowel",)),
    Organ(18, "colon", "Colon", "abdomen", SOFT, (184, 140, 96), ("colon",)),
    Organ(19, "urinary_bladder", "Urinary bladder", "pelvis", SOFT, (200, 190, 92),
          ("urinary_bladder",)),
    Organ(20, "cervical_spine", "Cervical spine", "skeleton", BONE, (232, 220, 190),
          _seq("vertebrae_C{}", range(1, 8))),
    Organ(21, "thoracic_spine", "Thoracic spine", "skeleton", BONE, (222, 208, 172),
          _seq("vertebrae_T{}", range(1, 13))),
    Organ(22, "lumbar_spine", "Lumbar spine", "skeleton", BONE, (210, 196, 158),
          _seq("vertebrae_L{}", range(1, 6))),
    Organ(23, "sacrum", "Sacrum", "skeleton", BONE, (198, 184, 146), ("sacrum", "vertebrae_S1")),
    Organ(24, "ribs_left", "Left ribs", "skeleton", BONE, (236, 230, 206),
          _seq("rib_left_{}", range(1, 13))),
    Organ(25, "ribs_right", "Right ribs", "skeleton", BONE, (226, 222, 214),
          _seq("rib_right_{}", range(1, 13))),
    Organ(26, "sternum", "Sternum and costal cartilages", "skeleton", BONE, (214, 214, 196),
          ("sternum", "costal_cartilages")),
    Organ(27, "shoulder_left", "Left shoulder girdle and humerus", "skeleton", BONE, (206, 198, 186),
          ("scapula_left", "clavicula_left", "humerus_left")),
    Organ(28, "shoulder_right", "Right shoulder girdle and humerus", "skeleton", BONE, (196, 192, 178),
          ("scapula_right", "clavicula_right", "humerus_right")),
    Organ(29, "hip_left", "Left hip bone", "skeleton", BONE, (218, 204, 182), ("hip_left",)),
    Organ(30, "hip_right", "Right hip bone", "skeleton", BONE, (206, 196, 168), ("hip_right",)),
    Organ(31, "femur_left", "Left femur", "skeleton", BONE, (224, 214, 194), ("femur_left",)),
    Organ(32, "femur_right", "Right femur", "skeleton", BONE, (212, 204, 182), ("femur_right",)),
    Organ(33, "brain", "Brain", "head", SOFT, (196, 166, 172), ("brain",)),
    Organ(34, "skull", "Skull", "head", BONE, (230, 224, 210), ("skull",)),
)

BY_ID = {o.id: o for o in CATALOG}
BY_KEY = {o.key: o for o in CATALOG}
CLASS_TO_ORGAN = {cls: o for o in CATALOG for cls in o.totalseg_classes}


def _validate() -> None:
    ids = [o.id for o in CATALOG]
    keys = [o.key for o in CATALOG]
    classes = [c for o in CATALOG for c in o.totalseg_classes]
    problems = []
    if len(set(ids)) != len(ids) or not all(0 < i < 256 for i in ids):
        problems.append("organ ids must be unique uint8 > 0")
    if len(set(keys)) != len(keys):
        problems.append("organ keys must be unique")
    if len(set(classes)) != len(classes):
        problems.append("a TotalSegmentator class may map to one organ only")
    problems += [f"{o.key}: bad group/windows" for o in CATALOG
                 if o.group not in GROUPS or not o.windows or not set(o.windows) <= set(WINDOW_CLASSES)]
    if problems:
        raise ValueError("invalid organ catalog: " + "; ".join(problems))


_validate()


def catalog_classes() -> list[str]:
    """Every TotalSegmentator class the catalog uses (for ``roi_subset``)."""
    return [c for o in CATALOG for c in o.totalseg_classes]


def label_lut(label_map: dict[int, str]) -> np.ndarray:
    """uint8 lookup table: TotalSegmentator label value -> catalog organ id (0 = unused)."""
    lut = np.zeros(max([255, *label_map]) + 1, dtype=np.uint8)
    for value, name in label_map.items():
        organ = CLASS_TO_ORGAN.get(name)
        if organ is not None:
            lut[int(value)] = organ.id
    return lut
