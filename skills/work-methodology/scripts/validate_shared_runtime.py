"""Validate the shared DevKit runtime and agent assets without third parties."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTS = ROOT / "agents"
METHODOLOGY = ROOT / "skills" / "work-methodology"
DOMAIN_SKILLS = (
    "astrbot-plugin-dev",
    "mcp-server-dev",
    "python-engineering",
    "oss-repo-ops",
    "bugkiller",
)
AGENT_MARKERS = {
    "2718lab-triage.md": ("read-only", "triage"),
    "2718lab-investigator.md": ("read-only", "investigation"),
    "2718lab-doc-writer.md": ("documentation",),
    "2718lab-verifier.md": ("read-only", "verification"),
    "2718lab-code-writer.md": ("code writer", "gpt-5.6-sol", "ultra"),
    "2718lab-risk-reviewer.md": (
        "read-only",
        "budget: 0",
        "dangerous user approval",
    ),
}
LEGACY_ALIASES = {
    "bugkiller-luna-triage.md": "2718lab-triage",
    "bugkiller-terra-investigator.md": "2718lab-investigator",
    "bugkiller-terra-doc-writer.md": "2718lab-doc-writer",
    "bugkiller-terra-verifier.md": "2718lab-verifier",
    "bugkiller-sol-code-writer.md": "2718lab-code-writer",
    "bugkiller-sol-escalation.md": "2718lab-risk-reviewer",
}


def read(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"missing file: {path.relative_to(ROOT)}")
        return ""
    return path.read_text(encoding="utf-8")


def valid_agent(content: str) -> bool:
    if not content.startswith("---\n"):
        return False
    end = content.find("\n---\n", 4)
    return (
        end >= 0
        and "name:" in content[4:end]
        and "description:" in content[4:end]
        and "\n# " in content[end + 5 :]
    )


def main() -> int:
    errors: list[str] = []

    for filename, markers in AGENT_MARKERS.items():
        content = read(AGENTS / filename, errors)
        if content and not valid_agent(content):
            errors.append(f"{filename} needs frontmatter and a Markdown body")
        if "Bugkiller" in content:
            errors.append(f"{filename} must not be owned by Bugkiller")
        for marker in markers:
            if content and marker not in content:
                errors.append(f"{filename} missing marker: {marker}")

    for filename, target in LEGACY_ALIASES.items():
        content = read(AGENTS / filename, errors)
        if content and (
            "Compatibility alias" not in content
            or target not in content
            or len(content.splitlines()) > 24
        ):
            errors.append(f"{filename} must remain a thin alias for {target}")

    for skill_name in DOMAIN_SKILLS:
        content = read(ROOT / "skills" / skill_name / "SKILL.md", errors)
        for marker in (
            "共享执行层",
            "work-methodology",
            "2718lab-tools",
            "2718lab-code-writer",
        ):
            if content and marker not in content:
                errors.append(f"{skill_name}/SKILL.md missing shared marker: {marker}")

    methodology = "\n".join(
        read(path, errors)
        for path in (
            METHODOLOGY / "SKILL.md",
            METHODOLOGY / "references" / "team-patterns.md",
            METHODOLOGY / "references" / "orchestration-runtime.md",
        )
    )
    for target in AGENT_MARKERS:
        name = target.removesuffix(".md")
        if name not in methodology:
            errors.append(f"shared methodology missing agent: {name}")
    for legacy in ("bugkiller-sol-code-writer", "bugkiller-terra-doc-writer"):
        if legacy in methodology:
            errors.append(f"shared methodology contains legacy role: {legacy}")
    for tool in (
        "workflow_detect_adapters",
        "workflow_approval_prepare",
        "workflow_approval_grant",
        "workflow_approval_deny",
        "workflow_approval_claim",
    ):
        if tool not in methodology:
            errors.append(f"shared methodology missing tool: {tool}")

    ui = read(AGENTS / "openai.yaml", errors)
    for marker in (
        "display_name: 2718lab DevKit",
        "shared engineering",
        "default_prompt:",
    ):
        if ui and marker not in ui:
            errors.append(f"agents/openai.yaml missing marker: {marker}")
    if "display_name: Bugkiller" in ui:
        errors.append("agents/openai.yaml still presents Bugkiller as the plugin")

    mcp_config = read(ROOT / ".mcp.json", errors)
    if mcp_config:
        env_vars = json.loads(mcp_config)["mcpServers"]["2718lab-tools"]["env_vars"]
        if env_vars[:2] != ["DEVKIT_HOME", "BUGKILLER_HOME"]:
            errors.append(
                ".mcp.json must prefer DEVKIT_HOME and retain legacy fallback"
            )

    if errors:
        print("Shared runtime asset validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Shared runtime assets valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
