"""Task-local cache and temporary-file policy for the MCP server card."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_server_card_temp_and_cache_environment_stays_under_task_root() -> None:
    configured_task_root = Path(os.environ["CODEX_TASK_TEMP"])

    assert configured_task_root.is_absolute()
    task_root = configured_task_root.resolve()
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        assert task_root.is_relative_to(Path(runner_temp).resolve())
    elif os.name == "nt":
        assert task_root.drive.casefold() == "g:"

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


def test_hosted_workflows_and_fast_lane_docs_preserve_temp_root_boundary() -> None:
    workflows = {
        relative_path: (ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        )
    }

    for workflow in workflows.values():
        assert "$env:RUNNER_TEMP" in workflow
        assert "RUNNER_TEMP is required for hosted task-local storage" in workflow

    documents = (
        (ROOT / "mcp-tools/devkit_fastlane/FASTLANE_CONTRACT.md").read_text(
            encoding="utf-8"
        ),
        (
            ROOT / "mcp-tools/devkit_fastlane/references/efficiency-automation.md"
        ).read_text(encoding="utf-8"),
    )
    for required in (
        "当前 bootstrap/read-context",
        "默认 `G:\\2718lab\\_codex\\.codex-task-temp`",
        "C-drive temporary roots are forbidden",
        "RUNNER_TEMP",
        "host-private V2/V3 bootstrap",
        "external host embedding",
    ):
        assert any(required in document for document in documents), required
