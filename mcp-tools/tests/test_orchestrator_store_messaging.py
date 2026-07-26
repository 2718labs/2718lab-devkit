"""Durable, recipient-scoped orchestration mailbox coverage."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import Task, TaskState, Workflow, WorkflowKind, WorkflowState
from orchestrator.store import (
    ArtifactNotOwnedError,
    CapabilityInvalidError,
    CorrelationConflictError,
    MailboxForbiddenError,
    MessageExpiredError,
    QuotaExceededError,
    SQLiteStore,
    TTLInvalidError,
)


class SQLiteStoreMessagingTests(unittest.TestCase):
    _NOW = "2026-07-24T00:00:00+00:00"

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._database = Path(self._temporary_directory.name) / "orchestrator.sqlite"
        self.store = SQLiteStore(self._database)
        self.workflow = self.store.create_workflow(
            Workflow(
                "workflow-mail",
                WorkflowKind.DAG,
                "mailbox workflow",
                "summary",
                WorkflowState.RUNNING,
                policy_version="policy-1",
                created_at=self._NOW,
                updated_at=self._NOW,
            )
        )
        self.sender = self.store.register_task(
            Task("sender", self.workflow.id, "sender", "owner", TaskState.RUNNING),
            contract_subscriptions=("contract-a",),
        )
        self.recipient = self.store.register_task(
            Task(
                "recipient", self.workflow.id, "recipient", "owner", TaskState.RUNNING
            ),
            contract_subscriptions=("contract-a",),
        )
        self.sender_lease = self.store.acquire_lease(
            self.sender.id, "sender-owner", "2026-07-24T01:00:00+00:00", now=self._NOW
        )
        self.recipient_lease = self.store.acquire_lease(
            self.recipient.id,
            "recipient-owner",
            "2026-07-24T01:00:00+00:00",
            now=self._NOW,
        )
        self.store.register_task_artifact(
            self.sender.id,
            "sender-owner",
            self.sender_lease.epoch,
            kind="result",
            content_hash="sha256:artifact-one",
            safe_path="evidence/artifact-one",
            size=10,
            redaction_version="v1",
            now=self._NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _peer_capability(self) -> str:
        peers = self.store.list_authorized_peers(self.workflow.id, self.sender.id)
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0].task_id, self.recipient.id)
        self.assertEqual(peers[0].relationship, "contract_subscriber")
        return peers[0].capability

    def _send(self, *, correlation_id: str = "corr-1", ttl_seconds: int = 60):
        return self.store.enqueue_message(
            self.workflow.id,
            self.sender.id,
            self.recipient.id,
            "sender-owner",
            self.sender_lease.epoch,
            capability=self._peer_capability(),
            correlation_id=correlation_id,
            artifact_hash="sha256:artifact-one",
            metadata={"summary": "redacted"},
            ttl_seconds=ttl_seconds,
            max_count=5,
            max_bytes=100,
            now=self._NOW,
        )

    def test_subscription_peer_message_is_idempotent_and_survives_reopen(self) -> None:
        message = self._send()
        repeated = self._send()
        self.assertEqual(repeated.delivery_id, message.delivery_id)
        self.assertEqual(message.artifact_hash, "sha256:artifact-one")
        self.assertNotIn("body", message.__dataclass_fields__)

        self.store.close()
        self.store = SQLiteStore(self._database)
        inbox = self.store.read_inbox(
            self.workflow.id,
            self.recipient.id,
            "recipient-owner",
            self.recipient_lease.epoch,
            now="2026-07-24T00:00:30+00:00",
        )
        self.assertEqual(
            tuple(entry.delivery_id for entry in inbox), (message.delivery_id,)
        )
        events = self.store.list_events(self.workflow.id)
        self.assertTrue(events)
        self.assertNotIn("redacted", events[-1].redacted_payload)

    def test_correlation_quota_expiry_and_recipient_only_ack(self) -> None:
        message = self._send(ttl_seconds=1)
        with self.assertRaises(CorrelationConflictError) as conflict:
            self.store.enqueue_message(
                self.workflow.id,
                self.sender.id,
                self.recipient.id,
                "sender-owner",
                self.sender_lease.epoch,
                capability=self._peer_capability(),
                correlation_id="corr-1",
                artifact_hash="sha256:other",
                metadata={},
                ttl_seconds=60,
                max_count=5,
                max_bytes=100,
                now=self._NOW,
            )
        self.assertEqual(conflict.exception.code, "CORRELATION_CONFLICT")
        with self.assertRaises(QuotaExceededError):
            self.store.enqueue_message(
                self.workflow.id,
                self.sender.id,
                self.recipient.id,
                "sender-owner",
                self.sender_lease.epoch,
                capability=self._peer_capability(),
                correlation_id="corr-2",
                artifact_hash="sha256:artifact-one",
                metadata={},
                ttl_seconds=60,
                max_count=1,
                max_bytes=100,
                now=self._NOW,
            )
        with self.assertRaises(MailboxForbiddenError):
            self.store.ack_message(
                self.workflow.id,
                self.sender.id,
                "sender-owner",
                self.sender_lease.epoch,
                message.delivery_id,
                now=self._NOW,
            )
        with self.assertRaises(MessageExpiredError):
            self.store.ack_message(
                self.workflow.id,
                self.recipient.id,
                "recipient-owner",
                self.recipient_lease.epoch,
                message.delivery_id,
                now="2026-07-24T00:00:02+00:00",
            )

    def test_dependency_edges_are_authorized_peers_in_both_directions(self) -> None:
        dependent = self.store.register_task(
            Task("dependent", self.workflow.id, "dependent", "owner"),
            dependencies=(self.sender.id,),
        )

        sender_peers = {
            peer.task_id: peer.relationship
            for peer in self.store.list_authorized_peers(
                self.workflow.id, self.sender.id
            )
        }
        dependent_peers = {
            peer.task_id: peer.relationship
            for peer in self.store.list_authorized_peers(self.workflow.id, dependent.id)
        }

        self.assertEqual(sender_peers[dependent.id], "dependency_edge")
        self.assertEqual(dependent_peers[self.sender.id], "dependency_edge")

    def test_message_rejects_invalid_capability_artifact_and_ttl(self) -> None:
        with self.assertRaises(TTLInvalidError):
            self._send(ttl_seconds=0)
        with self.assertRaises(CapabilityInvalidError):
            self.store.enqueue_message(
                self.workflow.id,
                self.sender.id,
                self.recipient.id,
                "sender-owner",
                self.sender_lease.epoch,
                capability="invalid",
                correlation_id="invalid-capability",
                artifact_hash="sha256:artifact-one",
                metadata={},
                ttl_seconds=60,
                max_count=5,
                max_bytes=100,
                now=self._NOW,
            )
        with self.assertRaises(ArtifactNotOwnedError):
            self.store.enqueue_message(
                self.workflow.id,
                self.sender.id,
                self.recipient.id,
                "sender-owner",
                self.sender_lease.epoch,
                capability=self._peer_capability(),
                correlation_id="unowned-artifact",
                artifact_hash="sha256:not-owned",
                metadata={},
                ttl_seconds=60,
                max_count=5,
                max_bytes=100,
                now=self._NOW,
            )


if __name__ == "__main__":
    unittest.main()
