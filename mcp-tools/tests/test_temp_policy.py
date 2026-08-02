"""Task-local cache and temporary-file policy for the MCP server card."""

from __future__ import annotations

import os
from pathlib import Path


def test_server_card_temp_and_cache_environment_stays_under_d_task_root() -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()

    assert task_root.drive.casefold() == "d:"
    for name in (
        "CODEX_TASK_TEMP",
        "TEMP",
        "TMP",
        "TMPDIR",
        "PYTHONPYCACHEPREFIX",
        "UV_CACHE_DIR",
    ):
        value = Path(os.environ[name]).resolve()
        assert value.is_relative_to(task_root), name


def test_server_does_not_restore_legacy_data_or_temp_ownership() -> None:
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    source = server_path.read_text(encoding="utf-8")

    assert "BUGKILLER_HOME" not in source
    assert "_resolve_data_root" not in source
    assert "tempfile" not in source
    assert "C:\\\\" not in source
