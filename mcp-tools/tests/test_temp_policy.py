"""Task-local cache and temporary-file policy for the MCP server card."""

from __future__ import annotations

import os
from pathlib import Path


def test_server_card_temp_and_cache_environment_stays_under_task_root() -> None:
    configured_task_root = Path(os.environ["CODEX_TASK_TEMP"])

    assert configured_task_root.is_absolute()
    task_root = configured_task_root.resolve()
    if os.name == "nt":
        assert task_root.drive.casefold() == "d:"
    else:
        workspace_root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        assert task_root.is_relative_to(workspace_root)

    for name in (
        "CODEX_TASK_TEMP",
        "TEMP",
        "TMP",
        "TMPDIR",
        "PYTHONPYCACHEPREFIX",
        "UV_CACHE_DIR",
    ):
        configured_value = Path(os.environ[name])
        assert configured_value.is_absolute(), name
        assert configured_value.resolve().is_relative_to(task_root), name


def test_server_does_not_restore_legacy_data_or_temp_ownership() -> None:
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    source = server_path.read_text(encoding="utf-8")

    assert "BUGKILLER_HOME" not in source
    assert "_resolve_data_root" not in source
    assert "tempfile" not in source
    assert "C:\\\\" not in source
