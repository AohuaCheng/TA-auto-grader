"""Workspace paths: repo code in TA-auto-grader/, data dirs in parent Essays/."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT.parent

DEFAULT_DOWNLOAD_DIR = WORKSPACE_ROOT / "downloads"
DEFAULT_TEMPLATES_DIR = WORKSPACE_ROOT / "templates"


def download_root() -> Path:
    raw = (os.getenv("THU_DOWNLOAD_DIR") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return DEFAULT_DOWNLOAD_DIR


def templates_dir() -> Path:
    raw = (os.getenv("THU_TEMPLATES_DIR") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return DEFAULT_TEMPLATES_DIR
