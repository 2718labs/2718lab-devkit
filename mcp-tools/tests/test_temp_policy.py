"""Tests for task-scoped scratch storage."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from temp_support import task_scratch


def test_task_scratch_uses_task_temp_and_rejects_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    base = Path(os.environ["CODEX_TASK_TEMP"])

    scratch = task_scratch("safe-name")

    assert scratch == base / "safe-name"
    assert scratch.is_dir()
    with pytest.raises(ValueError):
        task_scratch("../escape")
