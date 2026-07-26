"""Integration tests for the durable orchestration service boundary."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from orchestrator.models import (  # noqa: E402
    Task,
    TaskState,
    Workflow,
    WorkflowKind,
    WorkflowState,
)
from orchestrator.service import OrchestratorService, ServiceError  # noqa: E402
from orchestrator.store import SQLiteStore  # noqa: E402


class OrchestratorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(
            dir="D:/bun/tmp/codex/bugkiller-plugin/orch-03-card"
        )
        self.store = SQLiteStore(Path(self.tempdir.name) / "orchestrator.sqlite")
        self.service = OrchestratorService(self.store)
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.store.close)

    def _workflow(
        self, workflow_id: str, *, policy_version: str = "policy-v1"
    ) -> Workflow:
        workflow = Workflow(
            id=workflow_id,
            kind=WorkflowKind.DAG,
            title="Prepare release",
            product_summary="Ship the isolated workflow service.",
            policy_version=policy_version,
        )
        return self.service.create_workflow(workflow)

    def _register(
        self,
        workflow_id: str,
        task_id: str,
        *,
        dependencies: tuple[str, ...] = (),
        write_scope: tuple[str, ...] = (),
        card: str | None = None,
        direct_contract_hashes: tuple[str, ...] = (),
        required_evidence: tuple[str, ...] = ("unit test",),
        input_hash: str = "",
    ) -> Task:
        return self.service.register_task(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                title=f"{task_id} title",
                owner_role="implementer",
                dependencies=dependencies,
                write_scope=write_scope,
            ),
            card=card or f"card for {task_id}",
            direct_contract_hashes=direct_contract_hashes,
            required_evidence=required_evidence,
            input_hash=input_hash,
        )

    def _claim(self, task_id: str, owner: str, *, now: datetime) -> tuple[Task, object]:
        return self.service.claim_task(
            task_id,
            owner,
            expires_at=(now + timedelta(minutes=5)).isoformat(),
            now=now.isoformat(),
        )

    def _complete(
        self, task: Task, lease: object, *, now: datetime, result_hash: str = ""
    ) -> Task:
        return self.service.complete_task(
            task.id,
            expected_version=task.version,
            owner=getattr(lease, "owner"),
            epoch=getattr(lease, "epoch"),
            result_hash=result_hash,
            now=now.isoformat(),
        )

    def test_dag_waves_unlock_downstream_exactly_once_and_report_write_conflicts(
        self,
    ) -> None:
        self._workflow("release")
        self._register("release", "design", write_scope=("docs/design.md",))
        self._register("release", "code", write_scope=("src/app.py",))
        self._register(
            "release",
            "verify",
            dependencies=("design", "code"),
            write_scope=("src/app.py",),
        )

        first_wave = self.service.ready_wave("release")
        self.assertEqual(("design", "code"), tuple(task.id for task in first_wave))
        self.assertEqual((), self.service.ready_wave("release"))
        self.assertEqual(
            (("code", "verify", ("src/app.py",)),),
            self.service.write_scope_conflicts("release"),
        )

        now = datetime(2026, 7, 24, tzinfo=UTC)
        for task in first_wave:
            running, lease = self._claim(task.id, f"owner-{task.id}", now=now)
            self._complete(running, lease, now=now)

        second_wave = self.service.ready_wave("release")
        self.assertEqual(("verify",), tuple(task.id for task in second_wave))
        self.assertEqual((), self.service.ready_wave("release"))

    def test_stale_completion_returns_stable_error_code(self) -> None:
        self._workflow("stale")
        self._register("stale", "task")
        task = self.service.ready_wave("stale")[0]
        started = datetime(2026, 7, 24, tzinfo=UTC)
        running, old_lease = self._claim(task.id, "first-owner", now=started)
        _, fresh_lease = self._claim(
            task.id, "second-owner", now=started + timedelta(minutes=6)
        )

        with self.assertRaises(ServiceError) as captured:
            self._complete(running, old_lease, now=started + timedelta(minutes=6))
        self.assertEqual("STALE_LEASE", captured.exception.code)
        self.assertEqual("second-owner", getattr(fresh_lease, "owner"))

    def test_cancel_prevents_completion_and_cancels_every_nonterminal_task(
        self,
    ) -> None:
        self._workflow("cancel")
        self._register("cancel", "active")
        self._register("cancel", "waiting", dependencies=("active",))
        ready = self.service.ready_wave("cancel")[0]
        now = datetime(2026, 7, 24, tzinfo=UTC)
        running, lease = self._claim(ready.id, "owner", now=now)

        cancelled = self.service.cancel_workflow("cancel")
        self.assertEqual(WorkflowState.CANCELLED, cancelled.state)
        self.assertEqual(
            {"active": TaskState.CANCELLED, "waiting": TaskState.CANCELLED},
            {task.id: task.state for task in self.service.status("cancel")["tasks"]},
        )
        with self.assertRaises(ServiceError) as captured:
            self._complete(running, lease, now=now)
        self.assertEqual("WORKFLOW_CANCELLED", captured.exception.code)
        self.assertEqual(
            WorkflowState.CANCELLED, self.service.cancel_workflow("cancel").state
        )

    def test_block_and_fail_use_the_same_lease_and_version_cas_boundary(self) -> None:
        self._workflow("terminal")
        self._register("terminal", "blocked")
        self._register("terminal", "failed")
        now = datetime(2026, 7, 24, tzinfo=UTC)
        blocked, blocked_lease = self._claim(
            self.service.ready_wave("terminal")[0].id, "blocker", now=now
        )
        failed, failed_lease = self._claim(
            self.service.status("terminal")["tasks"][1].id, "failer", now=now
        )

        self.assertEqual(
            TaskState.BLOCKED,
            self.service.block_task(
                blocked.id,
                expected_version=blocked.version,
                owner=getattr(blocked_lease, "owner"),
                epoch=getattr(blocked_lease, "epoch"),
                now=now.isoformat(),
            ).state,
        )
        self.assertEqual(
            TaskState.FAILED,
            self.service.fail_task(
                failed.id,
                expected_version=failed.version,
                owner=getattr(failed_lease, "owner"),
                epoch=getattr(failed_lease, "epoch"),
                now=now.isoformat(),
            ).state,
        )

    def test_context_projections_are_role_scoped_and_agent_never_receives_sibling_card(
        self,
    ) -> None:
        self._workflow("contexts")
        self._register(
            "contexts",
            "agent-a",
            write_scope=("mcp-tools/orchestrator/service.py",),
            card="Only this agent can see this card.",
            direct_contract_hashes=("sha256:contract-a",),
        )
        self._register(
            "contexts",
            "agent-b",
            card="SIBLING SECRET CARD BODY - never disclose",
            direct_contract_hashes=("sha256:contract-b",),
        )
        self.service.ready_wave("contexts")

        product = self.service.context("contexts", role="product")
        coordinator = self.service.context("contexts", role="coordinator")
        agent = self.service.context("contexts", role="agent", task_id="agent-a")

        self.assertEqual("Ship the isolated workflow service.", product["direction"])
        self.assertIn("current_wave", coordinator)
        self.assertIn("write_conflicts", coordinator)
        self.assertEqual("agent-a", agent["task"]["id"])
        self.assertEqual(("sha256:contract-a",), agent["direct_contract_hashes"])
        self.assertEqual(("mcp-tools/orchestrator/service.py",), agent["write_scope"])
        self.assertNotIn("SIBLING SECRET CARD BODY", repr(agent))
        self.assertNotIn("agent-b", repr(agent))

    def test_agent_card_context_is_durable_across_store_reopen_without_sibling_body(
        self,
    ) -> None:
        self._workflow("durable-context")
        self._register(
            "durable-context",
            "agent-a",
            card="DURABLE AGENT A CARD BODY",
            direct_contract_hashes=("sha256:agent-a-contract",),
            required_evidence=("agent-a proof",),
        )
        self._register(
            "durable-context",
            "agent-b",
            card="DURABLE SIBLING SECRET CARD BODY",
            direct_contract_hashes=("sha256:sibling-contract",),
            required_evidence=("sibling proof",),
        )
        database = Path(self.tempdir.name) / "orchestrator.sqlite"

        self.store.close()
        reopened_store = SQLiteStore(database)
        self.addCleanup(reopened_store.close)
        reopened_service = OrchestratorService(reopened_store)
        agent = reopened_service.context(
            "durable-context", role="agent", task_id="agent-a"
        )

        self.assertEqual("DURABLE AGENT A CARD BODY", agent["card"])
        self.assertEqual(("sha256:agent-a-contract",), agent["direct_contract_hashes"])
        self.assertEqual(("agent-a proof",), agent["required_evidence"])
        self.assertNotIn("DURABLE SIBLING SECRET CARD BODY", repr(agent))
        self.assertNotIn("sha256:sibling-contract", repr(agent))
        self.assertNotIn("sibling proof", repr(agent))
        self.assertNotIn("agent-b", repr(agent))

    def test_artifacts_and_completed_inputs_dedupe_by_content_hash_without_cross_policy_reuse(
        self,
    ) -> None:
        content_hash = "sha256:verified-result"
        self._workflow("dedupe-v1", policy_version="policy-v1")
        self._register("dedupe-v1", "first", input_hash=content_hash)
        first = self.service.ready_wave("dedupe-v1")[0]
        now = datetime(2026, 7, 24, tzinfo=UTC)
        running, lease = self._claim(first.id, "owner", now=now)
        artifact = self.service.register_artifact(
            running.id,
            owner=getattr(lease, "owner"),
            epoch=getattr(lease, "epoch"),
            kind="evidence",
            content_hash=content_hash,
            safe_path="evidence/result.txt",
            size=15,
            redaction_version="r1",
            now=now.isoformat(),
        )
        same_artifact = self.service.register_artifact(
            running.id,
            owner=getattr(lease, "owner"),
            epoch=getattr(lease, "epoch"),
            kind="evidence",
            content_hash=content_hash,
            safe_path="evidence/result.txt",
            size=15,
            redaction_version="r1",
            now=now.isoformat(),
        )
        self.assertEqual(content_hash, artifact.content_hash)
        self.assertEqual(artifact, same_artifact)

        completed = self._complete(running, lease, now=now, result_hash=content_hash)
        self.assertEqual(
            completed, self.service.completed_input("dedupe-v1", content_hash)
        )

        self._workflow("dedupe-v2", policy_version="policy-v2")
        self.assertIsNone(self.service.completed_input("dedupe-v2", content_hash))


if __name__ == "__main__":
    unittest.main()
