"""Validate current Bugkiller routing and durable-handoff assets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "bugkiller" / "SKILL.md"
REFERENCES = ROOT / "skills" / "bugkiller" / "references"
AGENTS = ROOT / "agents"
PROFILE = ROOT / "skills" / "code-atlas" / "assets" / "host-profiles.json"
AGENT_MARKERS = {
    "bugkiller-sol-coordinator.md": ("Sol", "final acceptance", "Terra High"),
    "bugkiller-terra-investigator.md": ("Terra High", "gpt-5.6-terra", "high"),
    "bugkiller-terra-doc-writer.md": ("Terra High", "documentation-only"),
    "bugkiller-terra-verifier.md": ("Terra High", "read-only", "verification"),
    "bugkiller-sol-escalation.md": ("Sol High", "gpt-5.6-sol", "exceptional"),
}
DEPRECATED_AGENTS = (
    "-".join(("bugkiller", "sol", "code", "writer")) + ".md",
    "-".join(("bugkiller", "luna", "triage")) + ".md",
)
HANDOFF_SEQUENCE = (
    "workflow_artifact_register",
    "workflow_message_send",
    "workflow_inbox",
    "workflow_artifact_resolve",
    "workflow_message_ack",
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


def _mapping(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def validate_profiles(errors: list[str]) -> None:
    try:
        payload = json.loads(PROFILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(errors, f"invalid host profile asset: {exc}")
        return
    root = _mapping(payload)
    hosts = _mapping(root.get("hosts") if root else None)
    codex = _mapping(hosts.get("codex") if hosts else None)
    codex_roles = _mapping(codex.get("roles") if codex else None)
    expected = {
        "normal": ("gpt-5.6-terra", "high"),
        "complex": ("gpt-5.6-terra", "max"),
        "exceptional": ("gpt-5.6-sol", "high"),
    }
    code = _mapping(codex_roles.get("code") if codex_roles else None)
    for route, (model, reasoning) in expected.items():
        value = _mapping(code.get(route) if code else None)
        if value is None or value.get("model") != model or value.get("reasoning") != reasoning:
            fail(errors, f"host profile missing Codex {route} route")
    luna = _mapping(codex_roles.get("luna") if codex_roles else None)
    if luna is None or luna.get("status") != "unavailable":
        fail(errors, "host profile must declare Luna unavailable")
    if set(hosts or {}) != {"codex"}:
        fail(errors, "host profile must expose Codex only")


def main() -> int:
    errors: list[str] = []
    skill = text(SKILL, errors)
    if skill:
        if not valid_frontmatter(skill) or not has_markdown_body(skill):
            fail(errors, "SKILL.md needs YAML frontmatter and a Markdown body")
        if len(skill.splitlines()) > 120:
            fail(errors, "SKILL.md must remain at or below 120 lines")
        for marker in (
            "Terra High",
            "Terra Max",
            "Sol High",
            "Luna is unavailable",
            "workflow_artifact_register",
            "workflow_message_ack",
        ):
            if marker not in skill:
                fail(errors, f"SKILL.md missing routing marker: {marker}")

    reference_text = "\n".join(text(path, errors) for path in REFERENCES.glob("*.md"))
    for marker in (*HANDOFF_SEQUENCE, "TTL", "does not grant", "candidate commit"):
        if marker not in reference_text:
            fail(errors, f"references missing policy marker: {marker}")
    workflow = text(REFERENCES / "workflow.md", errors)
    require_ordered_markers(workflow, HANDOFF_SEQUENCE, errors, "workflow.md")
    roles = text(REFERENCES / "roles.md", errors)
    for marker in (
        "Sol coordinator",
        "gpt-5.6-terra",
        "Terra High",
        "Terra Max",
        "Sol High",
        "Luna",
    ):
        if marker not in roles:
            fail(errors, f"roles.md missing routing marker: {marker}")

    for filename, markers in AGENT_MARKERS.items():
        content = text(AGENTS / filename, errors)
        if content and (not valid_frontmatter(content) or not has_markdown_body(content)):
            fail(errors, f"{filename} needs YAML frontmatter and a Markdown body")
        for marker in markers:
            if content and marker not in content:
                fail(errors, f"{filename} missing marker: {marker}")
    for filename in DEPRECATED_AGENTS:
        if (AGENTS / filename).exists():
            fail(errors, "obsolete routing agent asset must be removed")

    ui_metadata = text(AGENTS / "openai.yaml", errors)
    if not ui_metadata.startswith("interface:\n"):
        fail(errors, "openai.yaml needs an interface UI metadata mapping")
    for marker in ("display_name:", "short_description:", "default_prompt:"):
        if marker not in ui_metadata:
            fail(errors, f"openai.yaml missing interface field: {marker}")
    if "agents:" in ui_metadata or "model:" in ui_metadata or ".toml" in ui_metadata:
        fail(errors, "openai.yaml must remain UI metadata only")

    validate_profiles(errors)
    if errors:
        print("Bugkiller asset validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Bugkiller assets valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
