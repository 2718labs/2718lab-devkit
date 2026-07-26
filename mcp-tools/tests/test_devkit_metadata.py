"""Release metadata consistency checks for the DevKit plugin surface."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_json(relative_path: str) -> dict[str, object]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class DevKitMetadataTests(unittest.TestCase):
    def test_public_tree_has_no_private_machine_details(self) -> None:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        forbidden = {
            "private marketplace": "pidan" + "-local-plugins",
            "task scratch root": "D:" + "\\bun\\tmp",
            "task scratch root (slash)": "D:" + "/bun/tmp",
            "long-term source checkout": "G:" + "\\2718lab",
            "long-term source checkout (slash)": "G:" + "/2718lab",
            "private home profile": "C:" + "\\Users\\pidan",
            "private home profile (slash)": "C:" + "/Users/pidan",
            "private key block": "-----BEGIN " + "PRIVATE KEY-----",
            "obsolete public repo owner": "https://github.com/2718lab" + "/",
        }
        findings: list[str] = []
        for relative_path in listed.stdout.splitlines():
            path = ROOT / relative_path
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for label, marker in forbidden.items():
                if marker in text:
                    findings.append(f"{relative_path}: {label}")

        self.assertEqual([], findings)

    def test_public_security_policy_and_ci_use_least_privilege(self) -> None:
        security = ROOT / "SECURITY.md"
        self.assertTrue(security.is_file())
        policy = security.read_text(encoding="utf-8")
        self.assertIn("privately", policy.lower())
        self.assertIn("GitHub Security Advisory", policy)

        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        self.assertEqual({"contents": "read"}, workflow["permissions"])

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[`SECURITY.md`](SECURITY.md)", readme)
        self.assertIn("[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)", readme)
        self.assertIn("面向 Codex 与 Claude Code 的工程基础设施插件", readme)
        self.assertIn(
            "claude plugin marketplace add 2718labs/2718lab-devkit",
            readme,
        )
        self.assertIn("socialify.git.ci/2718labs/2718lab-devkit", readme)
        self.assertIn("DBJD-CR/astrbot_plugin_helloworld", readme)

        community_files = (
            "CODE_OF_CONDUCT.md",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
        )
        for relative_path in community_files:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

        bug_report = yaml.safe_load(
            (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(
                encoding="utf-8"
            )
        )
        component = next(
            item for item in bug_report["body"] if item.get("id") == "component"
        )
        self.assertEqual(
            [
                "Project index",
                "Durable workflow orchestration",
                "Shared Agent roles",
            ],
            component["attributes"]["options"][:3],
        )
        self.assertIn(
            "Specialized Bugkiller workflow",
            component["attributes"]["options"],
        )

    def test_plugin_manifests_share_platform_first_positioning(self) -> None:
        codex = load_json(".codex-plugin/plugin.json")
        claude = load_json(".claude-plugin/plugin.json")
        marketplace = load_json(".claude-plugin/marketplace.json")
        public_repository = "https://github.com/2718labs/2718lab-devkit"
        self.assertEqual(codex["author"]["url"], "https://github.com/2718labs")
        self.assertEqual(codex["homepage"], public_repository)
        self.assertEqual(codex["repository"], public_repository)
        self.assertEqual(codex["interface"]["websiteURL"], public_repository)
        self.assertEqual(claude["author"]["url"], "https://github.com/2718labs")
        self.assertEqual(claude["homepage"], public_repository)
        self.assertEqual(claude["repository"], public_repository)
        self.assertEqual(claude["license"], "AGPL-3.0")
        self.assertEqual(codex["version"].split("+", 1)[0], "0.2.0")
        self.assertEqual(claude["version"], "0.2.0")
        self.assertEqual(marketplace["name"], "2718lab-devkit")
        self.assertEqual(marketplace["owner"]["url"], "https://github.com/2718labs")
        self.assertEqual(marketplace["version"], claude["version"])
        self.assertEqual(len(marketplace["plugins"]), 1)
        marketplace_plugin = marketplace["plugins"][0]
        self.assertEqual(marketplace_plugin["name"], claude["name"])
        self.assertEqual(marketplace_plugin["source"], "./")
        self.assertEqual(marketplace_plugin["version"], claude["version"])
        self.assertEqual(marketplace_plugin["repository"], public_repository)
        self.assertTrue(
            codex["version"] == claude["version"]
            or codex["version"].startswith(f"{claude['version']}+codex.")
        )
        codex_description = str(codex["description"])
        claude_description = str(claude["description"])
        self.assertIn("engineering infrastructure", codex_description)
        self.assertIn("project intelligence", codex_description)
        self.assertIn("workflow orchestration", codex_description)
        self.assertNotIn("Bugkiller", codex_description)
        self.assertIn("工程基础设施", claude_description)
        self.assertIn("项目索引", claude_description)
        self.assertIn("任务编排", claude_description)
        self.assertNotIn("Bugkiller", claude_description)

        interface = codex["interface"]
        self.assertIn("项目智能", str(interface["shortDescription"]))
        long_description = str(interface["longDescription"])
        self.assertIn("工程基础设施", long_description)
        self.assertIn("所有领域 Skill 共用", long_description)
        self.assertIn("多 Agent 角色", long_description)
        self.assertIn("Bugkiller", long_description)
        self.assertLess(
            long_description.index("工程基础设施"),
            long_description.index("Bugkiller"),
        )
        prompts = interface["defaultPrompt"]
        self.assertLessEqual(len(prompts), 4)
        self.assertNotIn("Bugkiller", prompts[0])
        self.assertTrue(any("Bugkiller" in prompt for prompt in prompts))

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project_description = pyproject["project"]["description"]
        self.assertIn("engineering infrastructure", project_description.lower())
        self.assertIn("project intelligence", project_description)
        self.assertIn("Codex and Claude Code", project_description)
        self.assertEqual(
            pyproject["project"]["urls"],
            {
                "Homepage": public_repository,
                "Repository": public_repository,
                "Issues": f"{public_repository}/issues",
                "Changelog": f"{public_repository}/blob/main/CHANGELOG.md",
            },
        )

        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        release = changelog.split("## [0.2.0]", 1)[1].split("## 0.1.0", 1)[0]
        self.assertIn("工程基础设施", release)
        self.assertIn("Bugkiller", release)

    def test_codex_hook_uses_supported_manifest_and_output_schema(self) -> None:
        hook = load_json("hooks/hooks.json")
        self.assertEqual({"hooks"}, set(hook))
        post_tool = hook["hooks"]["PostToolUse"][0]
        self.assertEqual("Edit|Write", post_tool["matcher"])

        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "metadata.yaml"
            metadata.write_text("version: 1.0\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "metadata_guard.py")],
                input=json.dumps({"tool_input": {"file_path": str(metadata)}}),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("systemMessage", output)
        self.assertNotIn("hookSpecificOutput", output)

    def test_mcp_uses_portable_codex_stdio_configuration(self) -> None:
        metadata = load_json(".mcp.json")
        self.assertEqual({"2718lab-tools"}, set(metadata["mcpServers"]))
        server = metadata["mcpServers"]["2718lab-tools"]
        self.assertEqual(server["command"], "python")
        self.assertEqual(server["args"], ["mcp-tools/server.py"])
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(
            server["env_vars"],
            ["DEVKIT_HOME", "BUGKILLER_HOME", "PLUGIN_DATA", "CODEX_HOME"],
        )
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", json.dumps(metadata))

    def test_readme_leads_with_platform_and_documents_real_capabilities(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        opening = "\n".join(text.splitlines()[:40])
        self.assertIn("工程基础设施插件", opening)
        self.assertIn("确定性项目索引", opening)
        self.assertIn("耐久任务编排", opening)
        self.assertNotIn("Bugkiller", opening)
        self.assertLess(
            text.index("## 核心能力"), text.index("## 专门工作流：Bugkiller")
        )

        for marker in (
            "lexical",
            "graph",
            "impact",
            "workflow_create",
            "workflow_register_task",
            "workflow_message_send",
            "worktree_checkpoint_create",
            "workflow_detect_adapters",
            "workflow_approval_prepare",
            "commit、push、PR",
            "2718lab-code-writer",
            "2718lab-investigator",
            "2718lab-verifier",
            "strict_index=true",
            "project_index_sync",
            "project_index_query",
            "trace_id",
            "worktree_checkpoint_create",
            'project_index_sync(bind_as="output")',
            'workflow_artifact_register(kind="verification", snapshot_id=...)',
            "workflow_complete",
            "astrbot-plugin-dev",
            "mcp-server-dev",
            "python-engineering",
            "oss-repo-ops",
            "work-methodology",
            "2718lab-new-plugin",
            "<marketplace-name>",
            "uv sync --frozen",
            "DEGRADED_SKILL_ONLY",
            "2718lab-tools",
        ):
            self.assertIn(marker, text)

    def test_work_index_records_completed_cards_and_active_runtime(self) -> None:
        text = (
            ROOT / "docs" / "superpowers" / "work" / "2026-07-24-bugkiller" / "index.md"
        ).read_text(encoding="utf-8")
        for card in ("ORCH-04A", "ORCH-04", "BK-03", "BK-04"):
            self.assertIn(f"`tasks/{card}.md` | done", text)
        self.assertIn("`tasks/BK-05.md` | done", text)
        self.assertIn("`tasks/BK-06.md` | done", text)


if __name__ == "__main__":
    unittest.main()
