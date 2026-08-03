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
