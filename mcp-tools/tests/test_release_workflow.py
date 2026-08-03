"""Static contracts for the tag-driven release package."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_workflow_builds_and_retains_rc2_prerelease_package() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    for required in (
        "tags:",
        '- "v*.*.*"',
        "mcp-tools/pyproject.toml",
        "extensions/astrbot/pyproject.toml",
        "build_main_artifact.py",
        "mcp-tools/tests/test_primary_artifact.py",
        "mcp-tools/devkit_fastlane",
        "uv build --no-create-gitignore --out-dir",
        "uv==0.11.28",
        "zipfile.ZipFile",
        "tarfile.open",
        "devkit_astrbot/__init__.py",
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "persist-credentials: false",
        "${ARTIFACT}.sha256",
        '"${ASTRBOT_DIR}"/*.whl',
        '"${ASTRBOT_DIR}"/*.tar.gz',
        "prerelease=",
        "release_args+=(--prerelease)",
    ):
        assert required in workflow


def test_release_workflow_runs_mcp_runtime_on_its_windows_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    continuous_integration = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "  mcp-runtime:\n"
        "    name: Re-run MCP runtime\n"
        "    needs: metadata\n"
        "    runs-on: windows-latest"
    ) in workflow
    mcp_runtime = workflow.split("\n  quality:", maxsplit=1)[0]
    ci_mcp_runtime = continuous_integration.split("\n  astrbot-extension:", maxsplit=1)[
        0
    ]
    for required in (
        "Configure MCP task-local runtime storage",
        '"CODEX_TASK_TEMP=$root"',
        "\"UV_CACHE_DIR=$(Join-Path $root 'uv-cache')\"",
        "      - name: Check MCP runtime\n        shell: bash\n        run: |",
        "needs: [metadata, mcp-runtime, quality, fast-lane]",
    ):
        assert required in workflow
    fixture_repo = (
        "D:\\bun\\tmp\\codex\\2718-devkit\\worktrees\\atlas12b-team-efficiency"
    )
    assert fixture_repo in mcp_runtime
    assert fixture_repo in ci_mcp_runtime
