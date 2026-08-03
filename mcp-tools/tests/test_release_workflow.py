"""Static contracts for the maintainer-dispatched release package."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_workflow_requires_a_main_dispatch_before_it_creates_a_tag() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    for required in (
        "workflow_dispatch:",
        "channel:",
        'test "${GITHUB_REF}" = "refs/heads/main"',
        'git check-ref-format "refs/tags/${RELEASE_TAG}"',
        "git merge-base --is-ancestor",
        "git tag -a",
        'git push "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"',
        "gh release create",
        "gh release upload",
        "--draft=false",
        "--verify-tag",
        "mcp-tools/pyproject.toml",
        "build_main_artifact.py",
        "mcp-tools/tests/test_primary_artifact.py",
        "mcp-tools/devkit_fastlane",
        "uv==0.11.28",
        "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "${ARTIFACT}.sha256",
        "prerelease=",
        "release_args+=(--prerelease)",
    ):
        assert required in workflow
    for forbidden in (
        "tags:",
        "extensions/astrbot",
        "uv build --no-create-gitignore --out-dir",
        "*.whl",
        "*.tar.gz",
        "if: github.ref == 'refs/heads/main'",
    ):
        assert forbidden not in workflow
    assert workflow.count("git fetch --no-tags origin +refs/heads/main") >= 2
    assert 'git cat-file -t "refs/tags/${RELEASE_TAG}"' in workflow
    assert "release recovery requires a draft release" in workflow


def test_release_publish_keeps_write_credentials_out_of_the_build_and_rechecks_tag() -> (
    None
):
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    publish = workflow.split("\n  publish:\n", maxsplit=1)[1]
    tag_step = publish.split(
        "\n      - name: Create immutable annotated tag", maxsplit=1
    )[1]
    tag_step = tag_step.split(
        "\n      - name: Create or resume draft GitHub Release", maxsplit=1
    )[0]
    release_step = publish.split(
        "\n      - name: Create or resume draft GitHub Release", maxsplit=1
    )[1]

    assert "persist-credentials: false" in publish
    assert 'test "${RELEASE_TAG_STATE}" = "resume"' in tag_step
    assert (
        tag_step.count(
            'git fetch --no-tags origin "refs/tags/${RELEASE_TAG}:refs/tags/${RELEASE_TAG}"'
        )
        == 1
    )
    assert tag_step.index(
        'git fetch --no-tags origin "refs/tags/${RELEASE_TAG}:refs/tags/${RELEASE_TAG}"'
    ) > tag_step.index(
        'git push "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"'
    )
    assert "RELEASE_COMMIT: ${{ needs.metadata.outputs.commit }}" in release_step
    assert 'git cat-file -t "refs/tags/${RELEASE_TAG}"' in release_step
    assert (
        'test "$(git rev-parse "refs/tags/${RELEASE_TAG}^{}")" = "${RELEASE_COMMIT}"'
        in release_step
    )


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
    mcp_runtime = workflow.split("\n  primary-artifact:", maxsplit=1)[0]
    ci_mcp_runtime = continuous_integration.split("\n  fast-lane:", maxsplit=1)[0]
    for required in (
        "Configure MCP task-local runtime storage",
        '"CODEX_TASK_TEMP=$root"',
        "\"UV_CACHE_DIR=$(Join-Path $root 'uv-cache')\"",
        "      - name: Check MCP runtime\n        shell: bash\n        run: |",
        "needs: [metadata, mcp-runtime, primary-artifact, fast-lane]",
    ):
        assert required in workflow
    for workflow_segment in (mcp_runtime, ci_mcp_runtime):
        assert "$env:RUNNER_TEMP" in workflow_segment
        assert "D:\\bun\\tmp\\codex" not in workflow_segment


def test_ci_workflow_is_mcp_only_and_uses_runner_local_fast_lane_storage() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "astrbot-extension" not in workflow
    assert "extensions/astrbot" not in workflow
    assert "needs: [mcp-runtime, fast-lane]" in workflow
    assert "Join-Path $env:RUNNER_TEMP '2718lab-devkit-task\\fast-lane'" in workflow
    assert "D:\\bun\\tmp\\codex\\2718lab-devkit-ci\\fast-lane" not in workflow


def test_workflow_test_tool_versions_are_pinned() -> None:
    continuous_integration = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "pytest==9.1.1" in continuous_integration
    assert "pytest==9.1.1" in release
