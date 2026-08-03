"""Integration tests for peer messaging without coordinator body relay."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from temp_support import task_scratch  # noqa: E402

from orchestrator.models import Task, Workflow, WorkflowKind  # noqa: E402
from orchestrator.service import OrchestratorService, ServiceError  # noqa: E402
from orchestrator.store import SQLiteStore  # noqa: E402


class OrchestratorMessagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=task_scratch("orchestrator-messaging"))
        self.database = Path(self.tempdir.name) / "orchestrator.sqlite"
        self.store = SQLiteStore(self.database)
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.store.close)
        self.service = OrchestratorService(
            self.store,
            evidence_root="evidence-root",
            mailbox_count_limit=1,
            mailbox_byte_limit=64,
            max_ttl_seconds=60,
        )
        self.now = datetime(2026, 7, 24, tzinfo=UTC)

    def _register(
        self,
        task_id: str,
        *,
        dependencies: tuple[str, ...] = (),
        contracts: tuple[str, ...] = (),
        card: str | None = None,
    ) -> None:
        self.service.register_task(
            Task(
                id=task_id,
                workflow_id="messages",
                title=f"{task_id} title",
                owner_role="agent",
                dependencies=dependencies,
            ),
            card=card or f"{task_id} card",
            direct_contract_hashes=contracts,
        )

    def _workflow(self) -> None:
        self.service.create_workflow(
            Workflow(
                id="messages",
                kind=WorkflowKind.DAG,
                title="Peer messaging",
                product_summary="Deliver artifact references only.",
            )
        )
        self._register("sender", contracts=("sha256:shared-contract",))
        self._register(
            "dependent", dependencies=("sender",), card="DEPENDENT CARD SECRET"
        )
        self._register("subscriber", contracts=("sha256:shared-contract",))
        self._register("offline", contracts=("sha256:shared-contract",))
        self._register("unrelated", card="UNRELATED CARD SECRET")
        self.service.ready_wave("messages")

    def _claim(
        self, task_id: str, owner: str, *, host_target: str | None = None
    ) -> tuple[Task, object]:
        arguments = {
            "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
            "now": self.now.isoformat(),
        }
        if host_target is not None:
            arguments["host_target"] = host_target
        return self.service.claim_task(task_id, owner, **arguments)

    def _register_evidence(
        self, owner: str, epoch: int, *, artifact_hash: str = "sha256:evidence"
    ) -> None:
        return self.service.register_artifact(
            "messages",
            "sender",
            owner=owner,
            epoch=epoch,
            kind="evidence",
            content_hash=artifact_hash,
            safe_path="evidence-root/results/check.txt",
            size=16,
            redaction_version="r1",
            now=self.now.isoformat(),
        )

    def test_authorized_peers_artifact_boundary_online_instruction_and_metadata_safety(
        self,
    ) -> None:
        self._workflow()
        sender, sender_lease = self._claim("sender", "sender-owner")
        subscriber, subscriber_lease = self._claim(
            "subscriber", "subscriber-owner", host_target="/root/subscriber"
        )
        _, offline_lease = self._claim("offline", "offline-owner")

        peers = self.service.peers("messages", "sender")
        self.assertEqual(
            {
                ("dependent", "dependency_edge"),
                ("offline", "contract_subscriber"),
                ("subscriber", "contract_subscriber"),
            },
            {(peer["task_id"], peer["relationship"]) for peer in peers},
        )
        self.assertNotIn("unrelated", repr(peers))

        with self.assertRaises(ServiceError) as captured:
            self.service.register_artifact(
                "messages",
                sender.id,
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                kind="evidence",
                content_hash="sha256:outside",
                safe_path="outside-root/check.txt",
                size=1,
                redaction_version="r1",
                now=self.now.isoformat(),
            )
        self.assertEqual("EVIDENCE_PATH_INVALID", captured.exception.code)
        self._register_evidence(sender_lease.owner, sender_lease.epoch)

        with self.assertRaises(ServiceError) as captured:
            self.service.send_message(
                "messages",
                sender.id,
                "subscriber",
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                correlation_id="reject-body",
                artifact_hash="sha256:evidence",
                metadata={"body": "MESSAGE BODY MUST NEVER ENTER MAILBOX"},
                ttl_seconds=30,
                now=self.now.isoformat(),
            )
        self.assertEqual("METADATA_INVALID", captured.exception.code)

        delivery = self.service.send_message(
            "messages",
            sender.id,
            subscriber.id,
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="online-1",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff", "summary": "PRIVATE LOG SUMMARY"},
            ttl_seconds=30,
            now=self.now.isoformat(),
        )
        self.assertTrue(delivery["delivery_id"])
        instruction = delivery["direct_instruction"]
        self.assertEqual("collaboration.send_message", instruction["operation"])
        self.assertEqual("/root/subscriber", instruction["arguments"]["target"])
        wakeup = instruction["arguments"]["message"]
        self.assertNotIn("\n", wakeup)
        self.assertNotIn(": ", wakeup)
        self.assertNotIn(", ", wakeup)
        wakeup_payload = json.loads(wakeup)
        self.assertEqual(
            {
                "delivery_id",
                "workflow_id",
                "sender_task_id",
                "artifact_hash",
                "correlation_id",
            },
            set(wakeup_payload),
        )
        self.assertEqual(delivery["delivery_id"], wakeup_payload["delivery_id"])
        self.assertEqual("messages", wakeup_payload["workflow_id"])
        self.assertEqual(sender.id, wakeup_payload["sender_task_id"])
        self.assertEqual("sha256:evidence", wakeup_payload["artifact_hash"])
        self.assertEqual("online-1", wakeup_payload["correlation_id"])
        self.assertNotIn("PRIVATE LOG SUMMARY", repr(delivery))
        self.assertNotIn("MESSAGE BODY MUST NEVER ENTER MAILBOX", repr(delivery))
        self.assertNotIn(
            "DEPENDENT CARD SECRET",
            repr(self.service.context("messages", role="agent", task_id="sender")),
        )
        self.service.ack_message(
            "messages",
            subscriber.id,
            delivery["delivery_id"],
            owner=subscriber_lease.owner,
            epoch=subscriber_lease.epoch,
            now=self.now.isoformat(),
        )

        offline_delivery = self.service.send_message(
            "messages",
            sender.id,
            "offline",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="mailbox-only",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff"},
            ttl_seconds=30,
            now=self.now.isoformat(),
        )
        self.assertIsNone(offline_lease.host_target)
        self.assertIsNone(offline_delivery["direct_instruction"])

        with self.assertRaises(ServiceError) as captured:
            self.service.inbox(
                "messages",
                subscriber.id,
                owner="wrong-owner",
                epoch=subscriber_lease.epoch,
                now=self.now.isoformat(),
            )
        self.assertEqual("STALE_LEASE", captured.exception.code)

    def test_endpoint_bind_and_rebind_require_current_lease_and_change_delivery_target(
        self,
    ) -> None:
        self._workflow()
        sender, sender_lease = self._claim("sender", "sender-owner")
        _, recipient_lease = self._claim("subscriber", "subscriber-owner")
        self._register_evidence(sender_lease.owner, sender_lease.epoch)

        with self.assertRaises(ServiceError) as captured:
            self.service.bind_endpoint(
                "messages",
                "subscriber",
                owner=recipient_lease.owner,
                epoch=recipient_lease.epoch,
                host_target="/root/not valid",
                now=self.now.isoformat(),
            )
        self.assertEqual("HOST_TARGET_INVALID", captured.exception.code)
        self.assertFalse(
            self.store.is_task_online("subscriber", now=self.now.isoformat())
        )

        bound = self.service.bind_endpoint(
            "messages",
            "subscriber",
            owner=recipient_lease.owner,
            epoch=recipient_lease.epoch,
            host_target="/root/subscriber",
            now=self.now.isoformat(),
        )
        self.assertEqual("/root/subscriber", bound.host_target)
        host_agent_id = "019f9536-f7e5-7c01-8fff-e1d15fbc0ddd"
        rebound = self.service.bind_endpoint(
            "messages",
            "subscriber",
            owner=recipient_lease.owner,
            epoch=recipient_lease.epoch,
            host_target=host_agent_id,
            now=self.now.isoformat(),
        )
        self.assertEqual(recipient_lease.epoch, rebound.epoch)
        self.assertEqual(host_agent_id, rebound.host_target)

        delivery = self.service.send_message(
            "messages",
            sender.id,
            "subscriber",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="rebound-target",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff"},
            ttl_seconds=30,
            now=self.now.isoformat(),
        )
        self.assertEqual(
            host_agent_id,
            delivery["direct_instruction"]["arguments"]["target"],
        )

        with self.assertRaises(ServiceError) as captured:
            self.service.bind_endpoint(
                "messages",
                "subscriber",
                owner="wrong-owner",
                epoch=recipient_lease.epoch,
                host_target="/root/hijacker",
                now=self.now.isoformat(),
            )
        self.assertEqual("STALE_LEASE", captured.exception.code)

    def test_invalid_host_targets_have_stable_error_and_do_not_make_task_online(
        self,
    ) -> None:
        self._workflow()

        for invalid_target in (
            "subscriber",
            "/root/has whitespace",
            "/root/subscriber\n",
            f"/root/{'a' * 1024}",
        ):
            with self.subTest(host_target=invalid_target):
                with self.assertRaises(ServiceError) as captured:
                    self._claim(
                        "subscriber", "subscriber-owner", host_target=invalid_target
                    )
                self.assertEqual("HOST_TARGET_INVALID", captured.exception.code)
                self.assertFalse(
                    self.store.is_task_online("subscriber", now=self.now.isoformat())
                )

    def test_recipient_resolves_only_its_unexpired_mailbox_artifact(self) -> None:
        self._workflow()
        sender, sender_lease = self._claim("sender", "sender-owner")
        _, recipient_lease = self._claim("subscriber", "subscriber-owner")
        _, other_lease = self._claim("offline", "offline-owner")
        self._register_evidence(sender_lease.owner, sender_lease.epoch)
        delivery = self.service.send_message(
            "messages",
            sender.id,
            "subscriber",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="resolve-artifact",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff"},
            ttl_seconds=1,
            now=self.now.isoformat(),
        )

        artifact = self.service.resolve_artifact(
            "messages",
            "subscriber",
            owner=recipient_lease.owner,
            epoch=recipient_lease.epoch,
            delivery_id=delivery["delivery_id"],
            now=self.now.isoformat(),
        )
        self.assertEqual("evidence", artifact["kind"])
        self.assertEqual("sha256:evidence", artifact["content_hash"])
        self.assertEqual("evidence-root/results/check.txt", artifact["safe_path"])
        self.assertEqual(16, artifact["size"])
        self.assertEqual("r1", artifact["redaction_version"])

        with self.assertRaises(ServiceError) as captured:
            self.service.resolve_artifact(
                "messages",
                "offline",
                owner=other_lease.owner,
                epoch=other_lease.epoch,
                delivery_id=delivery["delivery_id"],
                now=self.now.isoformat(),
            )
        self.assertEqual("MAILBOX_FORBIDDEN", captured.exception.code)

        with self.assertRaises(ServiceError) as captured:
            self.service.resolve_artifact(
                "messages",
                "subscriber",
                owner=recipient_lease.owner,
                epoch=recipient_lease.epoch,
                delivery_id=delivery["delivery_id"],
                now=(self.now + timedelta(seconds=2)).isoformat(),
            )
        self.assertEqual("MESSAGE_EXPIRED", captured.exception.code)

    def test_durable_offline_mailbox_idempotency_recipient_only_ack_and_no_body_events(
        self,
    ) -> None:
        self._workflow()
        sender, sender_lease = self._claim("sender", "sender-owner")
        self._register_evidence(sender_lease.owner, sender_lease.epoch)
        first = self.service.send_message(
            "messages",
            sender.id,
            "offline",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="offline-1",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff", "summary": "artifact ready"},
            ttl_seconds=30,
            now=self.now.isoformat(),
        )
        duplicate = self.service.send_message(
            "messages",
            sender.id,
            "offline",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="offline-1",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff", "summary": "artifact ready"},
            ttl_seconds=30,
            now=self.now.isoformat(),
        )
        self.assertEqual(first["delivery_id"], duplicate["delivery_id"])
        self.assertIsNone(first["direct_instruction"])

        self.store.close()
        reopened_store = SQLiteStore(self.database)
        self.addCleanup(reopened_store.close)
        reopened_service = OrchestratorService(
            reopened_store, evidence_root="evidence-root"
        )
        _, offline_lease = reopened_service.claim_task(
            "offline",
            "offline-owner",
            expires_at=(self.now + timedelta(minutes=5)).isoformat(),
            now=self.now.isoformat(),
        )
        inbox = reopened_service.inbox(
            "messages",
            "offline",
            owner=offline_lease.owner,
            epoch=offline_lease.epoch,
            now=self.now.isoformat(),
        )
        self.assertEqual(
            (first["delivery_id"],),
            tuple(entry["delivery_id"] for entry in inbox["entries"]),
        )

        with self.assertRaises(ServiceError) as captured:
            reopened_service.ack_message(
                "messages",
                "offline",
                first["delivery_id"],
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                now=self.now.isoformat(),
            )
        self.assertEqual("MAILBOX_FORBIDDEN", captured.exception.code)
        reopened_service.ack_message(
            "messages",
            "offline",
            first["delivery_id"],
            owner=offline_lease.owner,
            epoch=offline_lease.epoch,
            now=self.now.isoformat(),
        )
        reopened_service.ack_message(
            "messages",
            "offline",
            first["delivery_id"],
            owner=offline_lease.owner,
            epoch=offline_lease.epoch,
            now=self.now.isoformat(),
        )
        self.assertEqual(
            (),
            reopened_service.inbox(
                "messages",
                "offline",
                owner=offline_lease.owner,
                epoch=offline_lease.epoch,
                now=self.now.isoformat(),
            )["entries"],
        )
        self.assertNotIn(
            "MESSAGE BODY MUST NEVER ENTER MAILBOX",
            repr(reopened_store.list_events("messages")),
        )
        self.assertNotIn(
            "MESSAGE BODY MUST NEVER ENTER MAILBOX",
            repr(reopened_service.status("messages")),
        )
        self.assertNotIn(
            "MESSAGE BODY MUST NEVER ENTER MAILBOX",
            repr(reopened_service.context("messages", role="coordinator")),
        )

    def test_ttl_count_and_byte_quotas_reject_without_expanding_permissions(
        self,
    ) -> None:
        self._workflow()
        sender, sender_lease = self._claim("sender", "sender-owner")
        self._register_evidence(sender_lease.owner, sender_lease.epoch)
        self.service.send_message(
            "messages",
            sender.id,
            "offline",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            correlation_id="count-1",
            artifact_hash="sha256:evidence",
            metadata={"kind": "handoff"},
            ttl_seconds=1,
            now=self.now.isoformat(),
        )
        with self.assertRaises(ServiceError) as captured:
            self.service.send_message(
                "messages",
                sender.id,
                "unrelated",
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                correlation_id="unrelated",
                artifact_hash="sha256:evidence",
                metadata={"kind": "handoff"},
                ttl_seconds=30,
                now=self.now.isoformat(),
            )
        self.assertEqual("PEER_FORBIDDEN", captured.exception.code)
        with self.assertRaises(ServiceError) as captured:
            self.service.send_message(
                "messages",
                sender.id,
                "offline",
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                correlation_id="ttl",
                artifact_hash="sha256:evidence",
                metadata={"kind": "handoff"},
                ttl_seconds=0,
                now=self.now.isoformat(),
            )
        self.assertEqual("TTL_INVALID", captured.exception.code)
        with self.assertRaises(ServiceError) as captured:
            self.service.send_message(
                "messages",
                sender.id,
                "offline",
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                correlation_id="count-2",
                artifact_hash="sha256:evidence",
                metadata={"kind": "handoff"},
                ttl_seconds=30,
                now=self.now.isoformat(),
            )
        self.assertEqual("QUOTA_EXCEEDED", captured.exception.code)

        self.service.register_artifact(
            "messages",
            "sender",
            owner=sender_lease.owner,
            epoch=sender_lease.epoch,
            kind="evidence",
            content_hash="sha256:oversize",
            safe_path="evidence-root/results/oversize.txt",
            size=65,
            redaction_version="r1",
            now=self.now.isoformat(),
        )
        with self.assertRaises(ServiceError) as captured:
            self.service.send_message(
                "messages",
                sender.id,
                "offline",
                owner=sender_lease.owner,
                epoch=sender_lease.epoch,
                correlation_id="byte-quota",
                artifact_hash="sha256:oversize",
                metadata={"kind": "handoff"},
                ttl_seconds=30,
                now=(self.now + timedelta(seconds=2)).isoformat(),
            )
        self.assertEqual("QUOTA_EXCEEDED", captured.exception.code)

        _, offline_lease = self._claim("offline", "offline-owner")
        self.assertEqual(
            (),
            self.service.inbox(
                "messages",
                "offline",
                owner=offline_lease.owner,
                epoch=offline_lease.epoch,
                now=(self.now + timedelta(seconds=2)).isoformat(),
            )["entries"],
        )


class RoleScopedEnvelopeServiceTests(unittest.TestCase):
    """Role envelopes keep coordination facts durable without transcript relay."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(
            dir=task_scratch("orchestrator-role-messaging")
        )
        self.database = Path(self.tempdir.name) / "orchestrator.sqlite"
        self.store = SQLiteStore(self.database)
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.store.close)
        self.service = OrchestratorService(
            self.store,
            evidence_root="evidence-root",
            mailbox_count_limit=8,
            mailbox_byte_limit=128,
            max_ttl_seconds=60,
        )
        self.now = datetime(2026, 8, 3, tzinfo=UTC)
        self.contract_hash = self._hash("contract")
        self.assignment_token = self._hash("assignment token")
        self.context_hash = self._hash("dispatch context")
        self.route_hash = self._hash("route provenance")

    @staticmethod
    def _hash(value: str) -> str:
        return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"

    def _register(self, task_id: str, role: str, *, card: str) -> None:
        self.service.register_task(
            Task(
                id=task_id,
                workflow_id="roles",
                title=f"{task_id} title",
                owner_role=role,
            ),
            card=card,
            direct_contract_hashes=(self.contract_hash,),
        )

    def _claim(self, task_id: str, owner: str):
        return self.service.claim_task(
            task_id,
            owner,
            expires_at=(self.now + timedelta(minutes=5)).isoformat(),
            now=self.now.isoformat(),
        )

    def _artifact(self, task_id: str, lease: object, label: str, *, size: int = 16) -> str:
        content_hash = self._hash(label)
        self.service.register_artifact(
            "roles",
            task_id,
            owner=lease.owner,
            epoch=lease.epoch,
            kind="evidence",
            content_hash=content_hash,
            safe_path=f"evidence-root/{task_id}/{label}.json",
            size=size,
            redaction_version="r1",
            now=self.now.isoformat(),
        )
        return content_hash

    def _workflow(self) -> None:
        self.service.create_workflow(
            Workflow(
                id="roles",
                kind=WorkflowKind.DAG,
                title="Role scoped durable messaging",
                product_summary="Reference-only role packets.",
            )
        )
        self._register("coordinator", "coordinator", card="COORDINATOR CARD SECRET")
        self._register("worker-one", "worker", card="WORKER ONE CARD SECRET")
        self._register("worker-two", "worker", card="WORKER TWO CARD SECRET")
        self.service.ready_wave("roles")
        self.coordinator, self.coordinator_lease = self._claim(
            "coordinator", "coordinator-owner"
        )
        self.worker_one, self.worker_one_lease = self._claim(
            "worker-one", "worker-one-owner"
        )
        self.worker_two, self.worker_two_lease = self._claim(
            "worker-two", "worker-two-owner"
        )
        self.coordinator_index = self._artifact(
            "coordinator", self.coordinator_lease, "index-evidence"
        )
        self.worker_one_result = self._artifact(
            "worker-one", self.worker_one_lease, "terminal-result"
        )
        self.worker_one_evidence = self._artifact(
            "worker-one", self.worker_one_lease, "worker-evidence"
        )
        self.worker_one_dependency = self._artifact(
            "worker-one", self.worker_one_lease, "dependency-evidence"
        )
        self.worker_two_result = self._artifact(
            "worker-two", self.worker_two_lease, "worker-two-terminal"
        )
        self.worker_two_evidence = self._artifact(
            "worker-two", self.worker_two_lease, "worker-two-evidence"
        )

    def _assignment(self, worker_id: str, lease: object, *, correlation_id: str, ttl: int = 30):
        self.assertTrue(hasattr(self.service, "send_role_envelope"))
        return self.service.send_role_envelope(
            "roles",
            "coordinator",
            worker_id,
            owner=self.coordinator_lease.owner,
            epoch=self.coordinator_lease.epoch,
            recipient_epoch=lease.epoch,
            direction="coordinator_to_worker",
            sender_role="coordinator",
            recipient_role="worker",
            assignment_token=self.assignment_token,
            dispatch_context_hash=self.context_hash,
            route_provenance_hash=self.route_hash,
            correlation_id=correlation_id,
            ttl_seconds=ttl,
            task_card_hash=self.store.get_task(worker_id).card_hash,
            contract_hashes=(self.contract_hash,),
            index_evidence_hashes=(self.coordinator_index,),
            now=self.now.isoformat(),
        )

    def _terminal(
        self,
        worker_id: str,
        lease: object,
        *,
        correlation_id: str,
        assignment_token: str | None = None,
        context_hash: str | None = None,
        route_hash: str | None = None,
        risk_items: tuple[dict[str, str], ...] = (),
    ):
        result_hash = (
            self.worker_one_result if worker_id == "worker-one" else self.worker_two_result
        )
        evidence_hash = (
            self.worker_one_evidence
            if worker_id == "worker-one"
            else self.worker_two_evidence
        )
        return self.service.send_role_envelope(
            "roles",
            worker_id,
            "coordinator",
            owner=lease.owner,
            epoch=lease.epoch,
            recipient_epoch=self.coordinator_lease.epoch,
            direction="worker_to_coordinator",
            sender_role="worker",
            recipient_role="coordinator",
            assignment_token=assignment_token or self.assignment_token,
            dispatch_context_hash=context_hash or self.context_hash,
            route_provenance_hash=route_hash or self.route_hash,
            correlation_id=correlation_id,
            ttl_seconds=30,
            terminal_result_hash=result_hash,
            evidence_hashes=(evidence_hash,),
            risk_items=risk_items,
            now=self.now.isoformat(),
        )

    def test_all_role_directions_are_durable_and_projected_only_to_the_recipient(self) -> None:
        self._workflow()
        assignment = self._assignment(
            "worker-one", self.worker_one_lease, correlation_id="assign-one"
        )
        self.assertTrue(hasattr(self.service, "role_inbox"))
        terminal = self.service.send_role_envelope(
            "roles",
            "worker-one",
            "coordinator",
            owner=self.worker_one_lease.owner,
            epoch=self.worker_one_lease.epoch,
            recipient_epoch=self.coordinator_lease.epoch,
            direction="worker_to_coordinator",
            sender_role="worker",
            recipient_role="coordinator",
            assignment_token=self.assignment_token,
            dispatch_context_hash=self.context_hash,
            route_provenance_hash=self.route_hash,
            correlation_id="terminal-one",
            ttl_seconds=30,
            terminal_result_hash=self.worker_one_result,
            evidence_hashes=(self.worker_one_evidence,),
            risk_items=(
                {
                    "code": "RISK_NETWORK",
                    "severity": "high",
                    "evidence_hash": self.worker_one_evidence,
                },
            ),
            now=self.now.isoformat(),
        )
        peer = self.service.send_role_envelope(
            "roles",
            "worker-one",
            "worker-two",
            owner=self.worker_one_lease.owner,
            epoch=self.worker_one_lease.epoch,
            recipient_epoch=self.worker_two_lease.epoch,
            direction="peer_to_peer",
            sender_role="worker",
            recipient_role="worker",
            assignment_token=self.assignment_token,
            dispatch_context_hash=self.context_hash,
            route_provenance_hash=self.route_hash,
            correlation_id="peer-one",
            ttl_seconds=30,
            dependency_hashes=(self.worker_one_dependency,),
            evidence_hashes=(self.worker_one_evidence,),
            coordinator_task_id="coordinator",
            coordinator_epoch=self.coordinator_lease.epoch,
            now=self.now.isoformat(),
        )

        worker_inbox = self.service.role_inbox(
            "roles",
            "worker-one",
            owner=self.worker_one_lease.owner,
            epoch=self.worker_one_lease.epoch,
            recipient_role="worker",
            now=self.now.isoformat(),
        )
        coordinator_inbox = self.service.role_inbox(
            "roles",
            "coordinator",
            owner=self.coordinator_lease.owner,
            epoch=self.coordinator_lease.epoch,
            recipient_role="coordinator",
            now=self.now.isoformat(),
        )
        peer_inbox = self.service.role_inbox(
            "roles",
            "worker-two",
            owner=self.worker_two_lease.owner,
            epoch=self.worker_two_lease.epoch,
            recipient_role="worker",
            now=self.now.isoformat(),
        )
        self.assertEqual((assignment["delivery_id"],), tuple(
            entry["delivery_id"] for entry in worker_inbox["entries"]
        ))
        self.assertEqual((terminal["delivery_id"],), tuple(
            entry["delivery_id"] for entry in coordinator_inbox["entries"]
        ))
        self.assertEqual((peer["delivery_id"],), tuple(
            entry["delivery_id"] for entry in peer_inbox["entries"]
        ))
        self.assertEqual("coordinator_to_worker", worker_inbox["entries"][0]["direction"])
        self.assertEqual("worker_to_coordinator", coordinator_inbox["entries"][0]["direction"])
        self.assertEqual("peer_to_peer", peer_inbox["entries"][0]["direction"])
        projected = repr((worker_inbox, coordinator_inbox, peer_inbox))
        self.assertNotIn(self.assignment_token, projected)
        self.assertNotIn("COORDINATOR CARD SECRET", projected)
        self.assertNotIn("WORKER ONE CARD SECRET", projected)
        self.assertNotIn("evidence-root/", projected)
        raw_role_rows = repr(tuple(
            str(row["payload_json"])
            for row in self.store._connection.execute(
                "SELECT payload_json FROM role_envelopes ORDER BY sequence"
            ).fetchall()
        ))
        safe_views = repr(
            (
                self.service.status("roles"),
                self.service.context("roles", role="coordinator"),
                self.store.list_events("roles"),
            )
        )
        peer_capability = next(
            peer_info["capability"]
            for peer_info in self.service.peers("roles", "worker-one")
            if peer_info["task_id"] == "worker-two"
        )
        for forbidden in (
            "assignment token",
            "COORDINATOR CARD SECRET",
            "WORKER ONE CARD SECRET",
            "evidence-root/",
        ):
            self.assertNotIn(forbidden, raw_role_rows)
            self.assertNotIn(forbidden, safe_views)
        self.assertNotIn(peer_capability, raw_role_rows)
        self.assertNotIn(peer_capability, projected)

        self.store.close()
        self.store = SQLiteStore(self.database)
        self.addCleanup(self.store.close)
        self.service = OrchestratorService(self.store, evidence_root="evidence-root")
        reopened = self.service.role_inbox(
            "roles",
            "worker-two",
            owner=self.worker_two_lease.owner,
            epoch=self.worker_two_lease.epoch,
            recipient_role="worker",
            now=self.now.isoformat(),
        )
        self.assertEqual((peer["delivery_id"],), tuple(
            entry["delivery_id"] for entry in reopened["entries"]
        ))

    def test_role_fences_reject_stale_or_wrong_context_before_any_projection(self) -> None:
        self._workflow()
        self._assignment("worker-one", self.worker_one_lease, correlation_id="assign-one")
        cases = (
            ("stale-sender", self.worker_one_lease.epoch - 1, self.coordinator_lease.epoch,
             self.assignment_token, self.context_hash, self.route_hash, "STALE_LEASE"),
            ("stale-recipient", self.worker_one_lease.epoch, self.coordinator_lease.epoch + 1,
             self.assignment_token, self.context_hash, self.route_hash, "STALE_LEASE"),
            ("wrong-token", self.worker_one_lease.epoch, self.coordinator_lease.epoch,
             self._hash("other assignment"), self.context_hash, self.route_hash,
             "ROLE_ENVELOPE_FORBIDDEN"),
            ("wrong-context", self.worker_one_lease.epoch, self.coordinator_lease.epoch,
             self.assignment_token, self._hash("other context"), self.route_hash,
             "ROLE_ENVELOPE_FORBIDDEN"),
            ("wrong-route", self.worker_one_lease.epoch, self.coordinator_lease.epoch,
             self.assignment_token, self.context_hash, self._hash("other route"),
             "ROLE_ENVELOPE_FORBIDDEN"),
        )
        for (
            correlation_id,
            sender_epoch,
            recipient_epoch,
            assignment_token,
            context_hash,
            route_hash,
            code,
        ) in cases:
            with self.subTest(correlation_id=correlation_id):
                with self.assertRaises(ServiceError) as captured:
                    self.service.send_role_envelope(
                        "roles",
                        "worker-one",
                        "coordinator",
                        owner=self.worker_one_lease.owner,
                        epoch=sender_epoch,
                        recipient_epoch=recipient_epoch,
                        direction="worker_to_coordinator",
                        sender_role="worker",
                        recipient_role="coordinator",
                        assignment_token=assignment_token,
                        dispatch_context_hash=context_hash,
                        route_provenance_hash=route_hash,
                        correlation_id=correlation_id,
                        ttl_seconds=30,
                        terminal_result_hash=self.worker_one_result,
                        evidence_hashes=(self.worker_one_evidence,),
                        now=self.now.isoformat(),
                    )
                self.assertEqual(code, captured.exception.code)

        with self.assertRaises(ServiceError) as captured:
            self.service.send_role_envelope(
                "roles",
                "coordinator",
                "worker-one",
                owner=self.coordinator_lease.owner,
                epoch=self.coordinator_lease.epoch,
                recipient_epoch=self.worker_one_lease.epoch,
                direction="coordinator_to_worker",
                sender_role="worker",
                recipient_role="worker",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                correlation_id="wrong-role",
                ttl_seconds=30,
                task_card_hash=self.store.get_task("worker-one").card_hash,
                contract_hashes=(self.contract_hash,),
                index_evidence_hashes=(self.coordinator_index,),
                now=self.now.isoformat(),
            )
        self.assertEqual("ROLE_ENVELOPE_INVALID", captured.exception.code)
        with self.assertRaises(ServiceError) as captured:
            self.service.send_role_envelope(
                "roles",
                "worker-one",
                "worker-two",
                owner=self.worker_one_lease.owner,
                epoch=self.worker_one_lease.epoch,
                recipient_epoch=self.worker_two_lease.epoch,
                direction="worker_to_coordinator",
                sender_role="worker",
                recipient_role="coordinator",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                correlation_id="wrong-task",
                ttl_seconds=30,
                terminal_result_hash=self.worker_one_result,
                evidence_hashes=(self.worker_one_evidence,),
                now=self.now.isoformat(),
            )
        self.assertEqual("ROLE_ENVELOPE_FORBIDDEN", captured.exception.code)

    def test_assignment_fences_reject_missing_swapped_cross_workflow_and_rolled_over_recipient(self) -> None:
        self._workflow()
        with self.assertRaises(ServiceError) as captured:
            self._terminal("worker-one", self.worker_one_lease, correlation_id="missing")
        self.assertEqual("ROLE_ENVELOPE_FORBIDDEN", captured.exception.code)
        self.assertEqual(0, self.store._connection.execute(
            "SELECT COUNT(*) FROM role_envelopes"
        ).fetchone()[0])
        with self.assertRaises(ServiceError) as captured:
            self.service.send_role_envelope(
                "roles",
                "worker-two",
                "worker-one",
                owner=self.worker_two_lease.owner,
                epoch=self.worker_two_lease.epoch,
                recipient_epoch=self.worker_one_lease.epoch,
                direction="peer_to_peer",
                sender_role="worker",
                recipient_role="worker",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                correlation_id="peer-missing",
                ttl_seconds=30,
                evidence_hashes=(self.worker_two_evidence,),
                dependency_hashes=(self.worker_two_evidence,),
                coordinator_task_id="coordinator",
                coordinator_epoch=self.coordinator_lease.epoch,
                now=self.now.isoformat(),
            )
        self.assertEqual("ROLE_ENVELOPE_FORBIDDEN", captured.exception.code)

        self._assignment("worker-one", self.worker_one_lease, correlation_id="assign-one")
        with self.assertRaises(ServiceError) as captured:
            self._terminal("worker-two", self.worker_two_lease, correlation_id="swapped")
        self.assertEqual("ROLE_ENVELOPE_FORBIDDEN", captured.exception.code)
        with self.assertRaises(ServiceError) as captured:
            self.service.send_role_envelope(
                "foreign",
                "coordinator",
                "worker-one",
                owner=self.coordinator_lease.owner,
                epoch=self.coordinator_lease.epoch,
                recipient_epoch=self.worker_one_lease.epoch,
                direction="coordinator_to_worker",
                sender_role="coordinator",
                recipient_role="worker",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                correlation_id="cross-workflow",
                ttl_seconds=30,
                task_card_hash=self.store.get_task("worker-one").card_hash,
                contract_hashes=(self.contract_hash,),
                index_evidence_hashes=(self.coordinator_index,),
                now=self.now.isoformat(),
            )
        self.assertEqual("ROLE_ENVELOPE_FORBIDDEN", captured.exception.code)

        rollover = self.now + timedelta(minutes=6)
        _, next_coordinator = self.service.claim_task(
            "coordinator",
            "next-coordinator-owner",
            expires_at=(rollover + timedelta(minutes=5)).isoformat(),
            now=rollover.isoformat(),
        )
        _, next_worker = self.service.claim_task(
            "worker-one",
            "next-worker-owner",
            expires_at=(rollover + timedelta(minutes=5)).isoformat(),
            now=rollover.isoformat(),
        )
        self.assertGreater(next_worker.epoch, self.worker_one_lease.epoch)
        with self.assertRaises(ServiceError) as captured:
            self.service.send_role_envelope(
                "roles",
                "coordinator",
                "worker-one",
                owner=next_coordinator.owner,
                epoch=next_coordinator.epoch,
                recipient_epoch=self.worker_one_lease.epoch,
                direction="coordinator_to_worker",
                sender_role="coordinator",
                recipient_role="worker",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                correlation_id="rolled-recipient",
                ttl_seconds=30,
                task_card_hash=self.store.get_task("worker-one").card_hash,
                contract_hashes=(self.contract_hash,),
                index_evidence_hashes=(self.coordinator_index,),
                now=rollover.isoformat(),
            )
        self.assertEqual("STALE_LEASE", captured.exception.code)
        self.assertEqual(
            (),
            self.service.role_inbox(
                "roles",
                "worker-one",
                owner=next_worker.owner,
                epoch=next_worker.epoch,
                recipient_role="worker",
                now=rollover.isoformat(),
            )["entries"],
        )

    def test_risk_boundaries_and_role_envelope_quotas_are_exact(self) -> None:
        self._workflow()
        too_small = OrchestratorService(
            self.store,
            evidence_root="evidence-root",
            mailbox_count_limit=8,
            mailbox_byte_limit=15,
            max_ttl_seconds=60,
        )
        self.service = too_small
        with self.assertRaises(ServiceError) as captured:
            self._assignment("worker-one", self.worker_one_lease, correlation_id="byte-boundary")
        self.assertEqual("QUOTA_EXCEEDED", captured.exception.code)
        self.service = OrchestratorService(
            self.store,
            evidence_root="evidence-root",
            mailbox_count_limit=1,
            mailbox_byte_limit=16,
            max_ttl_seconds=60,
        )
        self._assignment("worker-one", self.worker_one_lease, correlation_id="count-one")
        with self.assertRaises(ServiceError) as captured:
            self._assignment("worker-two", self.worker_two_lease, correlation_id="count-two")
        self.assertEqual("QUOTA_EXCEEDED", captured.exception.code)

        self.service = OrchestratorService(
            self.store,
            evidence_root="evidence-root",
            mailbox_count_limit=8,
            mailbox_byte_limit=128,
            max_ttl_seconds=60,
        )
        eight_risks = tuple(
            {
                "code": f"RISK_{index}",
                "severity": "high",
                "evidence_hash": self.worker_one_evidence,
            }
            for index in range(8)
        )
        terminal = self._terminal(
            "worker-one",
            self.worker_one_lease,
            correlation_id="eight-risks",
            risk_items=eight_risks,
        )
        self.assertTrue(terminal["delivery_id"])
        with self.assertRaises(ServiceError) as captured:
            self._terminal(
                "worker-one",
                self.worker_one_lease,
                correlation_id="nine-risks",
                risk_items=eight_risks + (eight_risks[0],),
            )
        self.assertEqual("ROLE_ENVELOPE_INVALID", captured.exception.code)

    def test_expiry_idempotency_limits_and_archive_errno_17_are_block_records_only(self) -> None:
        self._workflow()
        assignment = self._assignment(
            "worker-one", self.worker_one_lease, correlation_id="assign-one", ttl=1
        )
        repeated = self._assignment(
            "worker-one", self.worker_one_lease, correlation_id="assign-one", ttl=1
        )
        self.assertEqual(assignment["delivery_id"], repeated["delivery_id"])
        with self.assertRaises(ServiceError) as captured:
            self._assignment("worker-one", self.worker_one_lease, correlation_id="assign-one")
        self.assertEqual("CORRELATION_CONFLICT", captured.exception.code)
        self.assertEqual(
            (),
            self.service.role_inbox(
                "roles",
                "worker-one",
                owner=self.worker_one_lease.owner,
                epoch=self.worker_one_lease.epoch,
                recipient_role="worker",
                now=(self.now + timedelta(seconds=1)).isoformat(),
            )["entries"],
        )
        self.assertTrue(hasattr(self.service, "ack_role_envelope"))
        with self.assertRaises(ServiceError) as captured:
            self.service.ack_role_envelope(
                "roles",
                "worker-one",
                assignment["delivery_id"],
                owner=self.worker_one_lease.owner,
                epoch=self.worker_one_lease.epoch,
                recipient_role="worker",
                now=(self.now + timedelta(seconds=1)).isoformat(),
            )
        self.assertEqual("MESSAGE_EXPIRED", captured.exception.code)

        before = self.store.get_task("worker-one")
        before_lease = self.store.get_lease("worker-one")
        protected_tables = (
            "artifacts",
            "code_task_acceptances",
            "atlas_ingestion_outbox",
            "task_index_bindings",
            "task_index_query_receipts",
            "task_index_verification_artifacts",
            "task_cards",
            "leases",
        )
        protected_counts = {
            table: self.store._connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in protected_tables
        }
        receipt_count = self.store._connection.execute(
            "SELECT COUNT(*) FROM host_operation_receipts"
        ).fetchone()[0]
        self.assertTrue(hasattr(self.service, "record_host_archive_result"))
        with self.assertRaises(ServiceError) as captured:
            self.service.record_host_archive_result(
                "roles",
                "worker-one",
                owner=self.worker_one_lease.owner,
                epoch=self.worker_one_lease.epoch,
                operation_id="forged-archive",
                assignment_token=self._hash("forged assignment"),
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                coordinator_task_id="coordinator",
                coordinator_epoch=self.coordinator_lease.epoch,
                errno=17,
                now=self.now.isoformat(),
            )
        self.assertEqual("ROLE_ENVELOPE_FORBIDDEN", captured.exception.code)
        with self.assertRaises(ServiceError) as captured:
            self.service.record_host_archive_result(
                "roles",
                "worker-one",
                owner=self.worker_one_lease.owner,
                epoch=self.worker_one_lease.epoch,
                operation_id="invalid-errno",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                coordinator_task_id="coordinator",
                coordinator_epoch=self.coordinator_lease.epoch,
                errno=True,
                now=self.now.isoformat(),
            )
        self.assertEqual("ROLE_ENVELOPE_INVALID", captured.exception.code)
        self.assertEqual(receipt_count, self.store._connection.execute(
            "SELECT COUNT(*) FROM host_operation_receipts"
        ).fetchone()[0])
        receipt = self.service.record_host_archive_result(
            "roles",
            "worker-one",
            owner=self.worker_one_lease.owner,
            epoch=self.worker_one_lease.epoch,
            operation_id="archive-worker-one",
            assignment_token=self.assignment_token,
            dispatch_context_hash=self.context_hash,
            route_provenance_hash=self.route_hash,
            coordinator_task_id="coordinator",
            coordinator_epoch=self.coordinator_lease.epoch,
            errno=17,
            now=self.now.isoformat(),
        )
        self.assertEqual("HOST_ARCHIVE_OS_ERROR_17", receipt["status_code"])
        self.assertEqual("blocked", receipt["outcome"])
        self.assertEqual(before, self.store.get_task("worker-one"))
        self.assertEqual(before_lease, self.store.get_lease("worker-one"))
        self.assertEqual(
            protected_counts,
            {
                table: self.store._connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in protected_tables
            },
        )
        repeated_receipt = self.service.record_host_archive_result(
            "roles",
            "worker-one",
            owner=self.worker_one_lease.owner,
            epoch=self.worker_one_lease.epoch,
            operation_id="archive-worker-one",
            assignment_token=self.assignment_token,
            dispatch_context_hash=self.context_hash,
            route_provenance_hash=self.route_hash,
            coordinator_task_id="coordinator",
            coordinator_epoch=self.coordinator_lease.epoch,
            errno=17,
            now=(self.now + timedelta(seconds=2)).isoformat(),
        )
        self.assertEqual(receipt, repeated_receipt)
        self.assertEqual([], self.service.status("roles")["code_acceptances"])
        self.assertEqual(0, self.store._connection.execute(
            "SELECT COUNT(*) FROM atlas_ingestion_outbox"
        ).fetchone()[0])
        with self.assertRaises(ServiceError) as captured:
            self.service.record_host_archive_result(
                "roles",
                "worker-one",
                owner=self.worker_one_lease.owner,
                epoch=self.worker_one_lease.epoch,
                operation_id="archive-worker-one",
                assignment_token=self.assignment_token,
                dispatch_context_hash=self.context_hash,
                route_provenance_hash=self.route_hash,
                coordinator_task_id="coordinator",
                coordinator_epoch=self.coordinator_lease.epoch,
                errno=18,
                now=self.now.isoformat(),
            )
        self.assertEqual("HOST_OPERATION_CONFLICT", captured.exception.code)
        receipt_rows = repr(tuple(
            tuple(row)
            for row in self.store._connection.execute(
                "SELECT * FROM host_operation_receipts"
            ).fetchall()
        ))
        self.assertNotIn("assignment token", receipt_rows)
        self.assertNotIn("evidence-root/", receipt_rows)


if __name__ == "__main__":
    unittest.main()
