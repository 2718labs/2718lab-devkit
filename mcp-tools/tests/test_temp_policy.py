"""Tests for task-scoped scratch storage."""

from __future__ import annotations

import pytest

from temp_support import task_scratch


def test_task_scratch_stays_under_configured_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "codex-task"
    monkeypatch.setenv("CODEX_TASK_TEMP", str(configured))

    scratch = task_scratch("atlas")

    assert scratch == (configured / "atlas").resolve()
    assert scratch.is_dir()


def test_task_scratch_rejects_escape(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_TASK_TEMP", str(tmp_path))

    with pytest.raises(ValueError, match="scratch name must be one safe path component"):
        task_scratch("../escape")
