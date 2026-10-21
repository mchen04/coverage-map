"""Per-image viewed/unviewed baseline (docs/SPEC.md "Coverage JSON", ``baseline``).

What it models: the per-image read-tracking behaviour described for PACS viewers such as
Synapse/eRAD, where every acquired image of the series is marked "viewed" once it has
been displayed and the reader is warned about images never displayed. Here an image
(acquisition slice k) counts as viewed when it was displayed in any viewport, for any
duration, under any window and any zoom/pan: at least one ``view`` event names its SOP
instance, or a volume viewport showed the axial plane through that slice. Nothing else is
modelled: no dwell, no window, no visible region, no organ knowledge.

This is an implemented model of that described behaviour, written for this research
tool; it is not, and was not tested against, the proprietary products.

``implied_organ_coverage`` translates the baseline into organ terms for comparisons: it
credits every organ voxel on a viewed image as reviewed, i.e. the most the per-image
model can claim about an organ.
"""

from __future__ import annotations

import numpy as np

MODEL = "per-image-viewed"


def unviewed_ranges(viewed: np.ndarray) -> list[list[int]]:
    """Inclusive ``[a, b]`` index ranges of unviewed images."""
    miss = np.concatenate([[False], ~np.asarray(viewed, bool), [False]])
    edges = np.flatnonzero(np.diff(miss.astype(np.int8)))
    return [[int(a), int(b) - 1] for a, b in zip(edges[::2], edges[1::2])]


def viewed_from_ranges(n_images: int, ranges) -> np.ndarray:
    viewed = np.ones(n_images, bool)
    for a, b in ranges:
        viewed[int(a):int(b) + 1] = False
    return viewed


class PerImageBaseline:
    def __init__(self, n_images: int):
        self.viewed = np.zeros(n_images, bool)

    def mark(self, k: int) -> None:
        self.viewed[k] = True

    def summary(self) -> dict:
        n = int(self.viewed.size)
        seen = int(self.viewed.sum())
        return {"model": MODEL, "images": n, "viewed": seen,
                "unviewed_ranges": unviewed_ranges(self.viewed), "complete": seen == n}


def implied_organ_coverage(organ_slice_counts: np.ndarray, viewed: np.ndarray) -> np.ndarray:
    """Fraction of each organ's voxels lying on viewed images (organ x slice counts input)."""
    counts = np.asarray(organ_slice_counts)
    total = counts.sum(axis=1)
    on_viewed = counts[:, np.asarray(viewed, bool)].sum(axis=1)
    return np.divide(on_viewed, total, out=np.zeros(len(total)), where=total > 0)
