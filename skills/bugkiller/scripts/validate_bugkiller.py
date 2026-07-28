"""Validate the self-contained Bugkiller plugin assets without third-party packages."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "bugkiller" / "SKILL.md"
REFERENCES = ROOT / "skills" / "bugkiller" / "references"
AGENTS = ROOT / "agents"
AGENT_MARKERS = {
    "bugkiller-luna-triage.md": ("Luna", "read-only", "DEGRADED_TRIAGE"),
    "bugkiller-terra-investigator.md": ("Terra", "read-only", "investigation"),
    "bugkiller-terra-doc-writer.md": ("Terra", "documentation"),
    "bugkiller-terra-verifier.md": ("Terra", "read-only", "verification"),
    "bugkiller-sol-code-writer.md": ("Sol", "code writer", "gpt-5.6-sol", "ultra"),
    "bugkiller-sol-escalation.md": ("Sol", "read-only", "budget: 0"),
}
NON_CODE_AGENTS = (
    "bugkiller-luna-triage.md",
    "bugkiller-terra-investigator.md",
    "bugkiller-terra-doc-writer.md",
    "bugkiller-terra-verifier.md",
)
CODE_WRITE_PROHIBITIONS = (
    "never write code",
    "must not write code",
    "do not write code",
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


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def text(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        fail(errors, f"missing file: {path.relative_to(ROOT)}")
        return ""
    return path.read_text(encoding="utf-8")


def valid_frontmatter(content: str) -> bool:
    if not content.startswith("---\n"):
        return False
    end = content.find("\n---\n", 4)
    if end < 0:
        return False
    frontmatter = content[4:end]
    return "name:" in frontmatter and "description:" in frontmatter


def has_markdown_body(content: str) -> bool:
    end = content.find("\n---\n", 4)
    return end >= 0 and "\n# " in content[end + 5 :]


def require_ordered_markers(
    content: str,
    markers: tuple[str, ...],
    errors: list[str],
    source: str,
) -> None:
    cursor = 0
    for marker in markers:
        position = content.find(marker, cursor)
        if position < 0:
            fail(errors, f"{source} missing or out-of-order marker: {marker}")
            continue
        cursor = position + len(marker)


def main() -> int:
    errors: list[str] = []
    skill = text(SKILL, errors)
    if skill:
        if not valid_frontmatter(skill):
            fail(errors, "SKILL.md needs name and description YAML frontmatter")
        if not has_markdown_body(skill):
            fail(errors, "SKILL.md needs a Markdown body")
        if len(skill.splitlines()) > 120:
            fail(errors, "SKILL.md must remain at or below 120 lines")
        if "references/" not in skill:
            fail(errors, "SKILL.md must route detailed guidance to references")
        for marker in ("gpt-5.6-sol", "ultra"):
            if marker not in skill:
                fail(errors, f"SKILL.md missing code-routing marker: {marker}")
        if not any(
            marker in skill
            for marker in (
                "Luna and Terra never write code",
                "Luna/Terra never write code",
            )
        ):
            fail(errors, "SKILL.md must state that Luna/Terra never write code")
        if "Terra writer is the only workspace writer" in skill:
            fail(errors, "SKILL.md still authorizes the deprecated Terra code writer")

    reference_text = "\n".join(text(path, errors) for path in REFERENCES.glob("*.md"))
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
            fail(errors, f"references missing policy marker: {marker}")
    roles = text(REFERENCES / "roles.md", errors)
    for marker in (
        "spawn",
        "model choices",
        "explicitly select Luna",
        "explicitly select Terra",
        "DEGRADED_TRIAGE",
        "bugkiller-terra-doc-writer",
        "bugkiller-sol-code-writer",
        "gpt-5.6-sol",
        "ultra",
    ):
        if marker not in roles:
            fail(errors, f"roles.md missing runtime-routing marker: {marker}")
    if not any(
        marker in roles
        for marker in (
            "Luna and Terra never write code",
            "Luna/Terra never write code",
        )
    ):
        fail(errors, "roles.md must state that Luna/Terra never write code")
    if "only patch writer" in roles:
        fail(errors, "roles.md still authorizes the deprecated Terra code writer")

    workflow = text(REFERENCES / "workflow.md", errors)
    require_ordered_markers(
        workflow,
        STRICT_WORKFLOW_SEQUENCE,
        errors,
        "workflow.md",
    )

    for filename, markers in AGENT_MARKERS.items():
        content = text(AGENTS / filename, errors)
        if content and not valid_frontmatter(content):
            fail(errors, f"{filename} needs YAML frontmatter plus Markdown body")
        if content and not has_markdown_body(content):
            fail(errors, f"{filename} needs a Markdown body")
        for marker in markers:
            if content and marker not in content:
                fail(errors, f"{filename} missing marker: {marker}")

    deprecated_writer = AGENTS / "bugkiller-terra-writer.md"
    if deprecated_writer.exists():
        fail(
            errors, "deprecated agent asset must be removed: bugkiller-terra-writer.md"
        )
    for filename in NON_CODE_AGENTS:
        content = text(AGENTS / filename, errors).lower()
        if content and not any(marker in content for marker in CODE_WRITE_PROHIBITIONS):
            fail(errors, f"{filename} must explicitly prohibit code writes")

    ui_metadata = text(AGENTS / "openai.yaml", errors)
    if not ui_metadata.startswith("interface:\n"):
        fail(errors, "openai.yaml needs an interface UI metadata mapping")
    for marker in ("display_name:", "short_description:", "default_prompt:"):
        if marker not in ui_metadata:
            fail(errors, f"openai.yaml missing interface field: {marker}")
    if "agents:" in ui_metadata:
        fail(errors, "openai.yaml must not enumerate plugin agents")
    if "model:" in ui_metadata or ".toml" in ui_metadata:
        fail(
            errors,
            "openai.yaml must not declare model slugs or global TOML installation",
        )

    writer = text(AGENTS / "bugkiller-sol-code-writer.md", errors)
    sol = text(AGENTS / "bugkiller-sol-escalation.md", errors)
    if writer and "dispatch" not in writer:
        fail(
            errors,
            "Sol code writer must identify gpt-5.6-sol and ultra as dispatch parameters",
        )
    if writer and "do not automatically request reviewer" not in writer:
        fail(errors, "Sol code writer must prohibit automatic reviewer escalation")
    if sol and ("dangerous user approval" not in sol or "one call" not in sol):
        fail(errors, "dangerous Sol reviewer must require user approval and one call")

    if errors:
        print("Bugkiller asset validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Bugkiller assets valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
