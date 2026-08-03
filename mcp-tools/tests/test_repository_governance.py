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


def test_dependency_review_checks_pull_request_dependency_changes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency-review.yml").read_text(
        encoding="utf-8"
    )

    for required in (
        "pull_request:",
        "actions/dependency-review-action@v4",
        "fail-on-severity: high",
    ):
        assert required in workflow
