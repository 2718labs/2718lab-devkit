"""Validate the specialized Bugkiller overlay without third-party packages."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "bugkiller" / "SKILL.md"
REFERENCES = ROOT / "skills" / "bugkiller" / "references"
SHARED_AGENTS = (
    "2718lab-triage",
    "2718lab-investigator",
    "2718lab-doc-writer",
    "2718lab-verifier",
    "2718lab-code-writer",
    "2718lab-risk-reviewer",
)
STRICT_WORKFLOW_SEQUENCE = (
    "project_index_sync",
    "strict_index=true",
    "project_index_query",
    "trace_id",
    "worktree_checkpoint_create",
    'project_index_sync(bind_as="output")',
    "project_index_query",
    "trace_id",
    'workflow_artifact_register(kind="verification", snapshot_id=...)',
    "workflow_complete",
)


def read(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"missing file: {path.relative_to(ROOT)}")
        return ""
    return path.read_text(encoding="utf-8")


def valid_frontmatter(content: str) -> bool:
    if not content.startswith("---\n"):
        return False
    end = content.find("\n---\n", 4)
    return end >= 0 and "name:" in content[4:end] and "description:" in content[4:end]


def require_ordered(
    content: str, markers: tuple[str, ...], errors: list[str], source: str
) -> None:
    cursor = 0
    for marker in markers:
        position = content.find(marker, cursor)
        if position < 0:
            errors.append(f"{source} missing or out-of-order marker: {marker}")
        else:
            cursor = position + len(marker)


def main() -> int:
    errors: list[str] = []
    skill = read(SKILL, errors)
    if skill:
        if not valid_frontmatter(skill) or "\n# Bugkiller\n" not in skill:
            errors.append("SKILL.md needs valid frontmatter and a Markdown body")
        if len(skill.splitlines()) > 120:
            errors.append("SKILL.md must remain at or below 120 lines")
        for marker in (
            "specialized defect workflow",
            "shared execution layer",
            "共享执行层",
            "work-methodology",
            "2718lab-tools",
            "gpt-5.6-sol",
            "ultra",
            "DEGRADED_SKILL_ONLY",
        ):
            if marker not in skill:
                errors.append(f"SKILL.md missing marker: {marker}")
        for agent in SHARED_AGENTS:
            if agent not in skill:
                errors.append(f"SKILL.md missing shared agent: {agent}")
        if "bugkiller-sol-code-writer" in skill:
            errors.append("SKILL.md must route through the shared code writer")

    reference_text = "\n".join(read(path, errors) for path in REFERENCES.glob("*.md"))
    for marker in (
        "DEGRADED_SKILL_ONLY",
        "workflow_artifact_register",
        "workflow_message_send",
        "collaboration.send_message",
        "workflow_inbox",
        "workflow_message_ack",
        "TTL",
        "does not grant",
    ):
        if marker not in reference_text:
            errors.append(f"references missing policy marker: {marker}")

    roles = read(REFERENCES / "roles.md", errors)
    for marker in (
        "spawn",
        "model choices",
        "explicitly select Luna",
        "explicitly select Terra",
        "DEGRADED_TRIAGE",
        "gpt-5.6-sol",
        "ultra",
        *SHARED_AGENTS,
    ):
        if marker not in roles:
            errors.append(f"roles.md missing routing marker: {marker}")
    for legacy in ("bugkiller-sol-code-writer", "bugkiller-terra-doc-writer"):
        if legacy in roles:
            errors.append(f"roles.md contains legacy role: {legacy}")

    workflow = read(REFERENCES / "workflow.md", errors)
    require_ordered(
        workflow,
        STRICT_WORKFLOW_SEQUENCE,
        errors,
        "workflow.md",
    )

    if errors:
        print("Bugkiller asset validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Bugkiller assets valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
