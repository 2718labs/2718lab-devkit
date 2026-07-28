"""Contract tests for the official FastMCP stdio runtime."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class MCPContractTests(unittest.TestCase):
    EXPECTED_TOOLS = {
        "check_release",
        "validate_astrbot_plugin",
        "validate_mcp_server",
        "check_python_project",
        "workflow_create",
        "workflow_register_task",
        "workflow_ready",
        "workflow_claim",
        "workflow_endpoint_bind",
        "workflow_complete",
        "workflow_status",
        "workflow_context",
        "workflow_artifact_register",
        "workflow_peers",
        "workflow_message_send",
        "workflow_inbox",
        "workflow_artifact_resolve",
        "workflow_message_ack",
        "workflow_cancel",
        "bugkiller_route",
        "bugkiller_detect_adapters",
        "bugkiller_approval_prepare",
        "bugkiller_approval_grant",
        "bugkiller_approval_deny",
        "bugkiller_approval_claim",
        "project_index_sync",
        "project_index_status",
        "project_index_query",
        "worktree_checkpoint_create",
        "worktree_checkpoint_status",
        "worktree_checkpoint_restore",
    }

    def test_official_fastmcp_registers_the_complete_tool_contract(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())

        self.assertEqual("2718lab-tools", server.mcp.name)
        self.assertEqual(self.EXPECTED_TOOLS, {tool.name for tool in tools})
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("from mcp.server.fastmcp import FastMCP", source)
        self.assertNotIn("from fastmcp import", source)
        self.assertNotIn("print(", source)

    def test_tool_annotations_expose_read_and_host_confirmation_boundaries(
        self,
    ) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        for name in {
            "workflow_status",
            "workflow_context",
            "workflow_peers",
            "workflow_inbox",
            "workflow_artifact_resolve",
            "bugkiller_route",
            "bugkiller_detect_adapters",
            "check_release",
            "validate_astrbot_plugin",
            "validate_mcp_server",
            "check_python_project",
            "project_index_status",
            "project_index_query",
            "worktree_checkpoint_status",
        }:
            self.assertTrue(tools[name].annotations.readOnlyHint, name)
        for name in {"bugkiller_approval_grant", "bugkiller_approval_claim"}:
            self.assertTrue(tools[name].annotations.destructiveHint, name)
            self.assertFalse(tools[name].annotations.openWorldHint, name)
        self.assertIn(
            "user confirmation", tools["bugkiller_approval_grant"].description.lower()
        )
        self.assertTrue(
            tools["worktree_checkpoint_restore"].annotations.destructiveHint
        )

    def test_strict_index_tool_schemas_extend_existing_workflow_contracts(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        register = tools["workflow_register_task"].inputSchema["properties"]
        for name in {
            "strict_index",
            "workspace_root",
            "input_snapshot_id",
            "task_node_ids",
            "contract_node_ids",
        }:
            self.assertIn(name, register)
        artifact = tools["workflow_artifact_register"].inputSchema["properties"]
        self.assertIn("snapshot_id", artifact)

    def test_project_index_query_schema_exposes_supported_modes(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        mode_schema = tools["project_index_query"].inputSchema["properties"]["mode"]
        self.assertEqual(["lexical", "graph", "impact"], mode_schema["enum"])
        self.assertEqual("lexical", mode_schema["default"])

    def test_project_index_wrappers_use_durable_default_runtime_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "plan.md").write_text(
                "# Plan\n\n- [ ] verify index\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ, {"BUGKILLER_HOME": str(root / "data")}, clear=True
            ):
                synced = server.project_index_sync(str(workspace))
                snapshot_id = synced["data"]["snapshot_id"]
                status = server.project_index_status(str(workspace), snapshot_id)
                queried = server.project_index_query(
                    str(workspace), snapshot_id, "verify index", max_nodes=10
                )
                missing_checkpoint = server.worktree_checkpoint_status("sha256:missing")
                partial_lease = server.project_index_sync(
                    str(workspace), workflow_id="wf", bind_as="output"
                )

                self.assertTrue(synced["ok"])
                self.assertEqual(snapshot_id, status["data"]["snapshot_id"])
                self.assertTrue(queried["ok"])
                self.assertTrue(queried["data"]["trace_id"].startswith("sha256:"))
                self.assertEqual("NOT_FOUND", missing_checkpoint["error"]["code"])
                self.assertEqual("INVALID_REQUEST", partial_lease["error"]["code"])
                self.assertTrue((root / "data" / "project-index.sqlite3").is_file())

    def test_host_bridge_tools_expose_lease_scoped_input_contracts(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        claim_schema = tools["workflow_claim"].inputSchema
        self.assertIn("host_target", claim_schema["properties"])
        self.assertNotIn("host_target", claim_schema["required"])

        bind_schema = tools["workflow_endpoint_bind"].inputSchema
        self.assertTrue(
            {
                "workflow_id",
                "task_id",
                "owner",
                "lease_epoch",
                "host_target",
            }.issubset(bind_schema["required"])
        )
        resolve_schema = tools["workflow_artifact_resolve"].inputSchema
        self.assertTrue(
            {
                "workflow_id",
                "recipient_task_id",
                "owner",
                "lease_epoch",
                "delivery_id",
            }.issubset(resolve_schema["required"])
        )

    def test_data_root_priority_and_rejections_never_use_plugin_source_or_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "BUGKILLER_HOME": str(root / "explicit"),
                    "PLUGIN_DATA": str(root / "plugin-data"),
                    "CODEX_HOME": str(root / "codex"),
                },
                clear=True,
            ):
                self.assertEqual(
                    (root / "explicit").resolve(), server._resolve_data_root()
                )
            with patch.dict(
                os.environ, {"CODEX_HOME": str(root / "codex")}, clear=True
            ):
                self.assertEqual(
                    (root / "codex" / "bugkiller").resolve(),
                    server._resolve_data_root(),
                )
            with patch.dict(
                os.environ, {"BUGKILLER_HOME": str(server.PLUGIN_ROOT)}, clear=True
            ):
                with self.assertRaises(server.RuntimeContractError) as rejected:
                    server._resolve_data_root()
                self.assertEqual("DATA_ROOT_INVALID", rejected.exception.code)
            cache_root = root / ".codex" / "plugins" / "cache" / "bugkiller"
            with patch.dict(
                os.environ, {"BUGKILLER_HOME": str(cache_root)}, clear=True
            ):
                with self.assertRaises(server.RuntimeContractError):
                    server._resolve_data_root()

    def test_data_root_falls_back_to_codex_data_and_persists_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            expected = (home / ".codex" / "data" / "2718lab-devkit").resolve()
            with (
                patch("server.Path.home", return_value=home),
                patch.dict(os.environ, {}, clear=True),
            ):
                missing = server.workflow_status("missing")

                self.assertEqual("NOT_FOUND", missing["error"]["code"])
                self.assertEqual(expected, server._resolve_data_root())
                self.assertTrue((expected / "orchestrator.sqlite3").is_file())

    def test_workflow_wrappers_are_json_safe_and_return_stable_errors(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"BUGKILLER_HOME": temporary}, clear=True),
        ):
            created = server.workflow_create(
                "wf", "dag", "title", "summary", "policy-v1"
            )
            registered = server.workflow_register_task(
                "wf",
                "task-a",
                "Task A",
                "implementer",
                "Task A card",
                [],
                ["mcp-tools/server.py"],
                ["sha256:contract"],
                ["contract test"],
                "",
            )
            ready = server.workflow_ready("wf")
            missing = server.workflow_status("missing")

            self.assertTrue(created["ok"])
            self.assertEqual("wf", created["data"]["id"])
            self.assertTrue(registered["ok"])
            self.assertEqual("task-a", ready["data"][0]["id"])
            self.assertEqual("NOT_FOUND", missing["error"]["code"])
            self.assertNotIn("traceback", repr(missing).lower())

    def test_workflow_claim_injects_the_project_index_runtime(self) -> None:
        sentinel_index = object()
        observed_index_services: list[object | None] = []

        class FakeService:
            @staticmethod
            def claim_task(
                task_id: str,
                owner: str,
                *,
                expires_at: str,
                host_target: str | None = None,
                now: str | None = None,
            ) -> tuple[str, str]:
                return task_id, owner

        @contextmanager
        def project_index_runtime():
            yield sentinel_index, object()

        @contextmanager
        def orchestrator_runtime(index_service=None):
            observed_index_services.append(index_service)
            yield object(), FakeService()

        with (
            patch.object(server, "_project_index_runtime", project_index_runtime),
            patch.object(server, "_orchestrator_runtime", orchestrator_runtime),
        ):
            claimed = server.workflow_claim(
                "strict-task",
                "writer",
                "2099-01-01T00:00:00+00:00",
            )

        self.assertTrue(claimed["ok"])
        self.assertEqual([sentinel_index], observed_index_services)

    def test_workflow_complete_injects_the_project_index_runtime(self) -> None:
        sentinel_index = object()
        observed_index_services: list[object | None] = []

        class FakeService:
            @staticmethod
            def complete_task(
                task_id: str,
                *,
                expected_version: int,
                owner: str,
                epoch: int,
                result_hash: str = "",
                now: str | None = None,
            ) -> tuple[str, str]:
                return task_id, owner

        @contextmanager
        def project_index_runtime():
            yield sentinel_index, object()

        @contextmanager
        def orchestrator_runtime(index_service=None):
            observed_index_services.append(index_service)
            yield object(), FakeService()

        with (
            patch.object(server, "_project_index_runtime", project_index_runtime),
            patch.object(server, "_orchestrator_runtime", orchestrator_runtime),
        ):
            completed = server.workflow_complete(
                "strict-task",
                2,
                "writer",
                1,
            )

        self.assertTrue(completed["ok"])
        self.assertEqual([sentinel_index], observed_index_services)

    def test_bugkiller_tools_return_data_only_and_approval_journal_never_executes(
        self,
    ) -> None:
        manifest = {
            "action": "commit",
            "repo_realpath": "D:/worktrees/task",
            "origin_fingerprint": "sha256:origin",
            "base_head": "abc123",
            "status_hash": "sha256:status",
            "diff_hash": "sha256:diff",
            "test_hash": "sha256:tests",
            "risk_hash": "sha256:risk",
            "commit_message": "fix: durable MCP contract",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"BUGKILLER_HOME": temporary}, clear=True),
        ):
            route = server.bugkiller_route(["credentials"])
            prepared = server.bugkiller_approval_prepare(
                manifest, "2099-01-01T00:00:00+00:00"
            )
            granted = server.bugkiller_approval_grant(prepared["data"]["id"])
            claimed = server.bugkiller_approval_claim(prepared["data"]["id"], manifest)

            self.assertEqual("HUMAN_GATE", route["data"]["target_state"])
            self.assertEqual("PREPARED", prepared["data"]["state"])
            self.assertEqual("GRANTED", granted["data"]["state"])
            self.assertEqual("CLAIMED", claimed["data"]["state"])
            self.assertFalse(any(Path(temporary).rglob(".git")))

    def test_workflow_runtime_covers_artifact_mailbox_context_completion_and_cancel(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"BUGKILLER_HOME": temporary}, clear=True),
        ):
            self.assertTrue(
                server.workflow_create("mail", "dag", "mail", "summary")["ok"]
            )
            for task_id in ("sender", "recipient"):
                registered = server.workflow_register_task(
                    "mail",
                    task_id,
                    task_id,
                    "implementer",
                    f"card for {task_id}",
                    [],
                    [f"mcp-tools/{task_id}.py"],
                    ["sha256:shared-contract"],
                    ["contract test"],
                    "",
                )
                self.assertTrue(registered["ok"])
            self.assertEqual(2, len(server.workflow_ready("mail")["data"]))
            sender_claim = server.workflow_claim(
                "sender", "sender-owner", "2099-01-01T00:00:00+00:00"
            )
            recipient_claim = server.workflow_claim(
                "recipient", "recipient-owner", "2099-01-01T00:00:00+00:00"
            )
            sender_task, sender_lease = sender_claim["data"]
            _, recipient_lease = recipient_claim["data"]

            endpoint = server.workflow_endpoint_bind(
                "mail",
                "recipient",
                "recipient-owner",
                recipient_lease["epoch"],
                "/root/recipient",
            )

            artifact = server.workflow_artifact_register(
                "mail",
                "sender",
                "sender-owner",
                sender_lease["epoch"],
                "result",
                "sha256:message-artifact",
                "evidence/message.json",
                20,
                "r1",
            )
            peers = server.workflow_peers("mail", "sender")
            forbidden_send = server.workflow_message_send(
                "mail",
                "sender",
                "recipient",
                "wrong-owner",
                sender_lease["epoch"],
                "corr-forbidden",
                "sha256:message-artifact",
                {"summary": "redacted"},
                60,
            )
            sent = server.workflow_message_send(
                "mail",
                "sender",
                "recipient",
                "sender-owner",
                sender_lease["epoch"],
                "corr-1",
                "sha256:message-artifact",
                {"summary": "redacted"},
                60,
            )
            forbidden_inbox = server.workflow_inbox(
                "mail", "recipient", "sender-owner", recipient_lease["epoch"]
            )
            inbox = server.workflow_inbox(
                "mail", "recipient", "recipient-owner", recipient_lease["epoch"]
            )
            delivery_id = sent["data"]["delivery_id"]
            resolved_artifact = server.workflow_artifact_resolve(
                "mail",
                "recipient",
                "recipient-owner",
                recipient_lease["epoch"],
                delivery_id,
            )
            acknowledged = server.workflow_message_ack(
                "mail",
                "recipient",
                "recipient-owner",
                recipient_lease["epoch"],
                delivery_id,
            )
            context = server.workflow_context("mail", "agent", "sender")
            completed = server.workflow_complete(
                "sender",
                sender_task["version"],
                "sender-owner",
                sender_lease["epoch"],
                "sha256:message-artifact",
            )
            cancelled = server.workflow_cancel("mail")

            self.assertTrue(artifact["ok"])
            self.assertTrue(endpoint["ok"])
            self.assertEqual("/root/recipient", endpoint["data"]["host_target"])
            self.assertEqual("recipient", peers["data"][0]["task_id"])
            self.assertEqual("STALE_LEASE", forbidden_send["error"]["code"])
            self.assertEqual("STALE_LEASE", forbidden_inbox["error"]["code"])
            self.assertEqual(delivery_id, inbox["data"]["entries"][0]["delivery_id"])
            self.assertEqual(
                "/root/recipient",
                sent["data"]["direct_instruction"]["arguments"]["target"],
            )
            self.assertEqual(
                "sha256:message-artifact", resolved_artifact["data"]["content_hash"]
            )
            self.assertEqual(
                "evidence/message.json", resolved_artifact["data"]["safe_path"]
            )
            self.assertEqual("acknowledged", acknowledged["data"]["delivery_state"])
            self.assertEqual("sender", context["data"]["task"]["id"])
            self.assertEqual("done", completed["data"]["state"])
            self.assertEqual("cancelled", cancelled["data"]["state"])
            self.assertEqual("recipient-owner", recipient_lease["owner"])


if __name__ == "__main__":
    unittest.main()
