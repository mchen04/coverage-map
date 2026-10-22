---
title: Coverage Map
emoji: 🩻
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Research-only CT reading-coverage map in the OHIF viewer
---

# Coverage Map

Coverage Map shows which organs a reader actually looked at while reading a CT scan. Scroll
through the scan and watch each organ on the 3D model turn green once it has been shown in the
right window for long enough.

> **Research use only. Not a medical device.** Coverage measures what the viewer displayed, not
> what a reader perceived or concluded.

## Try it

| Action | Input |
|---|---|
| Scroll slices | Mouse wheel |
| Soft-tissue, lung, bone window | `1`, `2`, `3` |
| Jump to the next gap | `N`, or click a gray or yellow organ in the 3D map |
| Finish the reading | `F` |

The Space sleeps after 48 hours without visitors. The first visit after that wakes it, which
takes a little while. Reading sessions reset when it restarts.

Source code: [github.com/mchen04/coverage-map](https://github.com/mchen04/coverage-map)

## Data

Both CTs are public, deidentified datasets from The Cancer Imaging Archive (TCIA), used under
CC BY 3.0:

- LIDC-IDRI, LIDC-IDRI-0001 (chest CT). [doi:10.7937/K9/TCIA.2015.LO9QL9SX](https://doi.org/10.7937/K9/TCIA.2015.LO9QL9SX)
- Pancreas-CT, PANCREAS_0051 (abdominal CT). [doi:10.7937/K9/TCIA.2016.tNB1kqBU](https://doi.org/10.7937/K9/TCIA.2016.tNB1kqBU)
- Clark K, et al. The Cancer Imaging Archive (TCIA). [doi:10.1007/s10278-013-9622-7](https://doi.org/10.1007/s10278-013-9622-7)

Organ outlines come from TotalSegmentator.
