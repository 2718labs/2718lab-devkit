"""Static contracts for repository governance and review automation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_repository_contribution_governance_files_are_present() -> None:
    for relative_path in (
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        ".github/CODEOWNERS",
        "docs/governance/repository-automation.md",
    ):
        assert (ROOT / relative_path).is_file(), relative_path

    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "* @Ayleovelle" in owners


def test_gemini_repository_configuration_is_checked_in_without_credentials() -> None:
    config = (ROOT / ".gemini" / "config.yaml").read_text(encoding="utf-8")
    styleguide = (ROOT / ".gemini" / "styleguide.md").read_text(encoding="utf-8")

    for required in (
        "code_review:",
        "comment_severity_threshold: MEDIUM",
        "pull_request_opened:",
        "code_review: true",
    ):
        assert required in config
    assert "secret" not in config.casefold()
    assert "fail closed" in styleguide.casefold()
    assert "test" in styleguide.casefold()


def test_codeql_runs_for_python_on_pull_requests_and_main() -> None:
    workflow = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(
        encoding="utf-8"
    )

    for required in (
        "pull_request:",
        "push:",
        "branches: [main]",
        "language: [python]",
        "security-events: write",
    ):
        assert required in workflow
    action_refs = re.findall(r"github/codeql-action/[^@]+@([0-9a-f]{40})", workflow)
    assert len(action_refs) == 3
    assert "github/codeql-action/" in workflow
    assert "@v4" not in workflow


def test_checkout_is_pinned_to_the_node24_v5_release_everywhere() -> None:
    checkout_ref = "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8"
    for relative_path in (
        ".github/workflows/ci.yml",
        ".github/workflows/codeql.yml",
        ".github/workflows/release.yml",
    ):
        workflow = (ROOT / relative_path).read_text(encoding="utf-8")
        assert checkout_ref in workflow, relative_path
        assert (
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" not in workflow
        )


def test_other_node24_actions_are_pinned_everywhere() -> None:
    node24_actions = {
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405": (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        ),
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f": (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        ),
        "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294": (
            ".github/workflows/dependency-review.yml",
        ),
    }
    for action_ref, paths in node24_actions.items():
        for relative_path in paths:
            workflow = (ROOT / relative_path).read_text(encoding="utf-8")
            assert action_ref in workflow, relative_path


def test_dependency_review_checks_pull_request_dependency_changes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency-review.yml").read_text(
        encoding="utf-8"
    )

    for required in (
        "pull_request:",
        "fail-on-severity: high",
    ):
        assert required in workflow
    action_refs = re.findall(
        r"actions/dependency-review-action@([0-9a-f]{40})", workflow
    )
    assert action_refs == ["a1d282b36b6f3519aa1f3fc636f609c47dddb294"]
