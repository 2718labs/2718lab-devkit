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
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
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


def test_ci_and_release_static_checks_cover_continuity_runtime() -> None:
    workflows = (
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"),
    )

    for workflow in workflows:
        assert "devkit_continuity" in workflow


def test_workflow_test_tool_versions_are_pinned() -> None:
    continuous_integration = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "pytest==9.1.1" in continuous_integration
    assert "pytest==9.1.1" in release


def test_fast_lane_workflows_use_current_routing_contract_without_quota_paths() -> None:
    workflows = (
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"),
    )

    for workflow in workflows:
        assert "scripts/team_efficiency.py" in workflow
        assert "scripts/fastlane_routing.py" in workflow
        assert "tests/test_team_efficiency.py" in workflow
        assert "tests/test_fastlane_routing.py" in workflow
        assert "quota" not in workflow.casefold()

    assert "uv lock --check" in workflows[0]
    assert "uv lock --check" in workflows[1]
    assert "unzip -t" in workflows[0]
    assert "sha256sum" in workflows[0]
    assert "if-no-files-found: error" in workflows[0]
    assert "persist-credentials: false" in workflows[1]
    assert "gh release create" in workflows[1]
    assert "$env:RUNNER_TEMP" in workflows[0]
    assert "$env:RUNNER_TEMP" in workflows[1]


def test_scheduler_topology_v1_documentation_preserves_host_owned_gates() -> None:
    documents = {
        "design": (
            ROOT
            / "docs"
            / "superpowers"
            / "specs"
            / "2026-08-23-devkit-1.1.0-scheduling-design.md"
        ).read_text(encoding="utf-8"),
        "plan": (
            ROOT
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-08-23-devkit-1.1.0-scheduling.md"
        ).read_text(encoding="utf-8"),
        "contract": (ROOT / "mcp-tools" / "devkit_fastlane" / "FASTLANE_CONTRACT.md").read_text(
            encoding="utf-8"
        ),
        "automation": (
            ROOT
            / "mcp-tools"
            / "devkit_fastlane"
            / "references"
            / "efficiency-automation.md"
        ).read_text(encoding="utf-8"),
        "patterns": (
            ROOT
            / "mcp-tools"
            / "devkit_fastlane"
            / "references"
            / "team-patterns.md"
        ).read_text(encoding="utf-8"),
        "readme": (ROOT / "README.md").read_text(encoding="utf-8"),
        "readme_zh": (ROOT / "README.zh-CN.md").read_text(encoding="utf-8"),
        "pr_template": (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        ),
    }

    for name in ("design", "plan", "contract", "automation", "patterns"):
        text = documents[name]
        assert "2718lab-devkit/scheduler-topology-v1" in text
        assert "A/B/C" in text
        assert "1:3" in text
        assert "UNSPLITTABLE" in text
        assert "declared-child" in text
        assert "opaque identity" in text
        assert "host capability" in text
        assert "lease" in text

    for name in ("readme", "readme_zh", "pr_template"):
        assert "2718lab-devkit/scheduler-topology-v1" in documents[name]

    for required in (
        "does not restore account-usage quota",
        "does not restore D-drive temporary roots",
        "does not restore a parent model ceiling",
    ):
        assert required in documents["design"]
