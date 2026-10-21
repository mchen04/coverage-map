"""Data locations shared by every command."""

import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("COVMAP_DATA", Path(__file__).resolve().parent.parent / "data"))


def case_dir(case_id: str) -> Path:
    return data_dir() / "cases" / case_id


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def ohif_dist() -> Path:
    return Path(os.environ.get("COVMAP_OHIF_DIST", data_dir().parent / "ohif-dist"))


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
