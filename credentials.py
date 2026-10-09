"""Load login credentials from a dedicated local file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CREDENTIALS_FILE = Path("credentials.local.env")


def load_credentials(credentials_file: Path | None = None) -> Path | None:
    """Load credentials.local.env if it exists. Returns the path loaded."""
    path = credentials_file or DEFAULT_CREDENTIALS_FILE
    if path.exists():
        load_dotenv(path, override=True)
        return path
    return None


def get_credentials() -> tuple[str | None, str | None]:
    username = (os.getenv("THU_USERNAME") or "").strip() or None
    password = (os.getenv("THU_PASSWORD") or "").strip() or None
    return username, password


def credentials_ready() -> bool:
    username, password = get_credentials()
    placeholders = {"", "your_student_id", "YOUR_STUDENT_ID", "your_password_here"}
    return bool(
        username
        and password
        and username not in placeholders
        and password not in placeholders
    )
