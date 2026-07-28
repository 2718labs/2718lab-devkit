"""Test helpers for task-scoped temporary storage."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def task_scratch(name: str) -> Path:
    """Create and return one safe scratch directory under the task temp root."""
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or any(character in name for character in "/\\\\:")
        or Path(name).is_absolute()
        or Path(name).name != name
    ):
        raise ValueError("scratch name must be one safe path component")

    base = Path(os.environ.get("CODEX_TASK_TEMP", tempfile.gettempdir())).resolve()
    scratch = (base / name).resolve()
    if scratch.parent != base:
        raise ValueError("scratch name must be one safe path component")
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch
