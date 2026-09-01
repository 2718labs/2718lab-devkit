"""Durable, recipient-scoped orchestration mailbox coverage."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from hashlib import sha256
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
    RoleEnvelopeInvalidError,
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


class SQLiteStoreRoleEnvelopeTests(unittest.TestCase):
    """Store-level role-envelope fences cannot be bypassed through the service."""

    _NOW = "2026-08-03T00:00:00+00:00"

    @staticmethod
    def _hash(value: str) -> str:
        return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._database = Path(self._temporary_directory.name) / "orchestrator.sqlite"
        self.store = SQLiteStore(self._database)
        self.workflow = self.store.create_workflow(
            Workflow(
                "workflow-role-mail",
                WorkflowKind.DAG,
                "role mailbox workflow",
                "summary",
                WorkflowState.RUNNING,
                policy_version="policy-1",
                created_at=self._NOW,
                updated_at=self._NOW,
            )
        )
        self.coordinator = self.store.register_task(
            Task(
                "coordinator",
                self.workflow.id,
                "coordinator",
                "coordinator",
                TaskState.RUNNING,
                card_hash=self._hash("COORDINATOR SOURCE BODY MUST NOT PROJECT"),
            ),
            card_body="COORDINATOR SOURCE BODY MUST NOT PROJECT",
            contract_subscriptions=(self._hash("contract"),),
        )
        self.worker_one = self.store.register_task(
            Task(
                "worker-one",
                self.workflow.id,
                "worker one",
                "worker",
                TaskState.RUNNING,
                card_hash=self._hash("WORKER SOURCE BODY MUST NOT PROJECT"),
            ),
            card_body="WORKER SOURCE BODY MUST NOT PROJECT",
            contract_subscriptions=(self._hash("contract"),),
        )
        self.worker_two = self.store.register_task(
            Task(
                "worker-two",
                self.workflow.id,
                "worker two",
                "worker",
                TaskState.RUNNING,
                card_hash=self._hash("SECOND WORKER SOURCE BODY MUST NOT PROJECT"),
            ),
            card_body="SECOND WORKER SOURCE BODY MUST NOT PROJECT",
            contract_subscriptions=(self._hash("contract"),),
        )
        self.coordinator_lease = self.store.acquire_lease(
            "coordinator",
            "coordinator-owner",
            "2026-08-03T01:00:00+00:00",
            now=self._NOW,
        )
        self.worker_one_lease = self.store.acquire_lease(
            "worker-one", "worker-one-owner", "2026-08-03T01:00:00+00:00", now=self._NOW
        )
        self.worker_two_lease = self.store.acquire_lease(
            "worker-two", "worker-two-owner", "2026-08-03T01:00:00+00:00", now=self._NOW
        )
        self.index_hash = self._hash("index")
        self.store.register_task_artifact(
            "coordinator",
            "coordinator-owner",
            self.coordinator_lease.epoch,
            kind="evidence",
            content_hash=self.index_hash,
            safe_path="evidence/index.json",
            size=16,
            redaction_version="r1",
            now=self._NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _assignment_kwargs(
        self, *, correlation_id: str = "assignment-one"
    ) -> dict[str, object]:
        return {
            "recipient_epoch": self.worker_one_lease.epoch,
            "direction": "coordinator_to_worker",
            "sender_role": "coordinator",
            "recipient_role": "worker",
            "assignment_token": self._hash("assignment token"),
            "dispatch_context_hash": self._hash("dispatch context"),
            "route_provenance_hash": self._hash("route provenance"),
            "correlation_id": correlation_id,
            "ttl_seconds": 30,
            "task_card_hash": self.worker_one.card_hash,
            "contract_hashes": (self._hash("contract"),),
            "index_evidence_hashes": (self.index_hash,),
            "max_count": 4,
            "max_bytes": 64,
            "now": self._NOW,
        }

    def _role_row_count(self) -> int:
        return int(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM role_envelopes"
            ).fetchone()[0]
        )

    def test_sensitive_fields_each_reject_without_any_role_row_or_event(self) -> None:
        self.assertTrue(hasattr(self.store, "enqueue_role_envelope"))
        cases = (
            ("assignment_token", "Bearer SHOULD NEVER PERSIST"),
            ("dispatch_context_hash", "ENV=TOP_SECRET"),
            ("route_provenance_hash", "route=RAW_SECRET"),
            ("task_card_hash", "RAW TRANSCRIPT MUST NEVER PERSIST"),
            ("contract_hashes", ("RAW CONTRACT SOURCE BODY",)),
            ("index_evidence_hashes", ("C:\\absolute\\source-body.txt",)),
        )
        for field, value in cases:
            with self.subTest(field=field):
                arguments = self._assignment_kwargs()
                arguments[field] = value
                with self.assertRaises(RoleEnvelopeInvalidError) as captured:
                    self.store.enqueue_role_envelope(
                        self.workflow.id,
                        "coordinator",
                        "worker-one",
                        "coordinator-owner",
                        self.coordinator_lease.epoch,
                        **arguments,
                    )
                self.assertEqual("ROLE_ENVELOPE_INVALID", captured.exception.code)
                self.assertEqual(0, self._role_row_count())
                self.assertEqual((), self.store.list_events(self.workflow.id))

    def test_terminal_sensitive_fields_each_reject_without_a_new_row_or_event(
        self,
    ) -> None:
        self.store.enqueue_role_envelope(
            self.workflow.id,
            "coordinator",
            "worker-one",
            "coordinator-owner",
            self.coordinator_lease.epoch,
            **self._assignment_kwargs(correlation_id="terminal-prerequisite"),
        )
        terminal_result = self._hash("terminal result")
        worker_evidence = self._hash("worker evidence")
        for content_hash, safe_path in (
            (terminal_result, "evidence/terminal-result.json"),
            (worker_evidence, "evidence/worker-evidence.json"),
        ):
            self.store.register_task_artifact(
                "worker-one",
                "worker-one-owner",
                self.worker_one_lease.epoch,
                kind="evidence",
                content_hash=content_hash,
                safe_path=safe_path,
                size=16,
                redaction_version="r1",
                now=self._NOW,
            )
        baseline_rows = self._role_row_count()
        baseline_events = len(self.store.list_events(self.workflow.id))
        base = {
            "recipient_epoch": self.coordinator_lease.epoch,
            "direction": "worker_to_coordinator",
            "sender_role": "worker",
            "recipient_role": "coordinator",
            "assignment_token": self._hash("assignment token"),
            "dispatch_context_hash": self._hash("dispatch context"),
            "route_provenance_hash": self._hash("route provenance"),
            "ttl_seconds": 30,
            "terminal_result_hash": terminal_result,
            "evidence_hashes": (worker_evidence,),
            "risk_items": (),
            "max_count": 4,
            "max_bytes": 64,
            "now": self._NOW,
        }
        cases = (
            ("terminal_result_hash", "RAW TERMINAL SOURCE BODY"),
            ("evidence_hashes", ("C:\\absolute\\worker-evidence.txt",)),
            (
                "risk_items",
                (
                    {
                        "code": "RISK_RAW",
                        "severity": "high",
                        "evidence_hash": "Bearer risk evidence must not persist",
                    },
                ),
            ),
        )
        for field, value in cases:
            with self.subTest(field=field):
                arguments = dict(base)
                arguments["correlation_id"] = f"terminal-sensitive-{field}"
                arguments[field] = value
                with self.assertRaises(RoleEnvelopeInvalidError) as captured:
                    self.store.enqueue_role_envelope(
                        self.workflow.id,
                        "worker-one",
                        "coordinator",
                        "worker-one-owner",
                        self.worker_one_lease.epoch,
                        **arguments,
                    )
                self.assertEqual("ROLE_ENVELOPE_INVALID", captured.exception.code)
                self.assertEqual(baseline_rows, self._role_row_count())
                self.assertEqual(
                    baseline_events, len(self.store.list_events(self.workflow.id))
                )

    def test_peer_sensitive_fields_each_reject_without_a_new_row_or_event(self) -> None:
        self.store.enqueue_role_envelope(
            self.workflow.id,
            "coordinator",
            "worker-one",
            "coordinator-owner",
            self.coordinator_lease.epoch,
            **self._assignment_kwargs(correlation_id="peer-prerequisite"),
        )
        worker_evidence = self._hash("peer worker evidence")
        worker_dependency = self._hash("peer worker dependency")
        for content_hash, safe_path in (
            (worker_evidence, "evidence/peer-worker-evidence.json"),
            (worker_dependency, "evidence/peer-worker-dependency.json"),
        ):
            self.store.register_task_artifact(
                "worker-one",
                "worker-one-owner",
                self.worker_one_lease.epoch,
                kind="evidence",
                content_hash=content_hash,
                safe_path=safe_path,
                size=16,
                redaction_version="r1",
                now=self._NOW,
            )
        capability = next(
            peer.capability
            for peer in self.store.list_authorized_peers(self.workflow.id, "worker-one")
            if peer.task_id == "worker-two"
        )
        baseline_rows = self._role_row_count()
        baseline_events = len(self.store.list_events(self.workflow.id))
        base = {
            "recipient_epoch": self.worker_two_lease.epoch,
            "direction": "peer_to_peer",
            "sender_role": "worker",
            "recipient_role": "worker",
            "assignment_token": self._hash("assignment token"),
            "dispatch_context_hash": self._hash("dispatch context"),
            "route_provenance_hash": self._hash("route provenance"),
            "ttl_seconds": 30,
            "evidence_hashes": (worker_evidence,),
            "dependency_hashes": (worker_dependency,),
            "coordinator_task_id": "coordinator",
            "coordinator_epoch": self.coordinator_lease.epoch,
            "capability": capability,
            "max_count": 4,
            "max_bytes": 64,
            "now": self._NOW,
        }
        cases = (
            ("evidence_hashes", ("RAW PEER EVIDENCE BODY",)),
            ("dependency_hashes", ("C:\\absolute\\dependency-source.txt",)),
        )
        for field, value in cases:
            with self.subTest(field=field):
                arguments = dict(base)
                arguments["correlation_id"] = f"peer-sensitive-{field}"
                arguments[field] = value
                with self.assertRaises(RoleEnvelopeInvalidError) as captured:
                    self.store.enqueue_role_envelope(
                        self.workflow.id,
                        "worker-one",
                        "worker-two",
                        "worker-one-owner",
                        self.worker_one_lease.epoch,
                        **arguments,
                    )
                self.assertEqual("ROLE_ENVELOPE_INVALID", captured.exception.code)
                self.assertEqual(baseline_rows, self._role_row_count())
                self.assertEqual(
                    baseline_events, len(self.store.list_events(self.workflow.id))
                )

    def test_wrong_peer_capability_fails_without_a_new_role_envelope(self) -> None:
        assignment = self.store.enqueue_role_envelope(
            self.workflow.id,
            "coordinator",
            "worker-one",
            "coordinator-owner",
            self.coordinator_lease.epoch,
            **self._assignment_kwargs(),
        )
        worker_evidence = self._hash("worker evidence")
        self.store.register_task_artifact(
            "worker-one",
            "worker-one-owner",
            self.worker_one_lease.epoch,
            kind="evidence",
            content_hash=worker_evidence,
            safe_path="evidence/worker-evidence.json",
            size=16,
            redaction_version="r1",
            now=self._NOW,
        )
        self.store.list_authorized_peers(self.workflow.id, "worker-one")
        event_count = len(self.store.list_events(self.workflow.id))
        with self.assertRaises(CapabilityInvalidError) as captured:
            self.store.enqueue_role_envelope(
                self.workflow.id,
                "worker-one",
                "worker-two",
                "worker-one-owner",
                self.worker_one_lease.epoch,
                recipient_epoch=self.worker_two_lease.epoch,
                direction="peer_to_peer",
                sender_role="worker",
                recipient_role="worker",
                assignment_token=self._hash("assignment token"),
                dispatch_context_hash=self._hash("dispatch context"),
                route_provenance_hash=self._hash("route provenance"),
                correlation_id="peer-one",
                ttl_seconds=30,
                evidence_hashes=(worker_evidence,),
                dependency_hashes=(worker_evidence,),
                coordinator_task_id="coordinator",
                coordinator_epoch=self.coordinator_lease.epoch,
                capability="wrong-peer-capability",
                max_count=4,
                max_bytes=64,
                now=self._NOW,
            )
        self.assertEqual("CAPABILITY_INVALID", captured.exception.code)
        self.assertEqual(
            assignment.delivery_id,
            self.store._role_envelope_from_row(
                self.store._connection.execute(
                    "SELECT * FROM role_envelopes WHERE delivery_id = ?",
                    (assignment.delivery_id,),
                ).fetchone()
            ).delivery_id,
        )
        self.assertEqual(1, self._role_row_count())
        self.assertEqual(event_count, len(self.store.list_events(self.workflow.id)))


class SQLiteStoreRoleEnvelopeSchemaTests(unittest.TestCase):
    """The v6 upgrade creates the same unique envelope hash surface as a fresh v7 DB."""

    def test_v6_upgrade_and_fresh_v7_have_the_partial_unique_envelope_hash_index(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        legacy_database = Path(temporary.name) / "legacy.sqlite"
        fresh_database = Path(temporary.name) / "fresh.sqlite"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute(
                "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_metadata (key, value) VALUES ('schema_version', '6')"
            )
            connection.execute(
                """
                CREATE TABLE messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL,
                    sender_task_id TEXT NOT NULL,
                    recipient_task_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    redacted_metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE (workflow_id, sender_task_id, recipient_task_id, correlation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE atlas_ingestion_outbox (
                    ingestion_key TEXT PRIMARY KEY,
                    acceptance_id TEXT NOT NULL UNIQUE
                        REFERENCES code_task_acceptances(acceptance_id),
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'projected', 'quarantined')),
                    attempt_count INTEGER NOT NULL
                        CHECK (attempt_count BETWEEN 0 AND 16),
                    last_error_code TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (ingestion_key = payload_hash)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX idx_atlas_outbox_pending
                    ON atlas_ingestion_outbox(state, created_at, ingestion_key)
                """
            )
            connection.commit()
        finally:
            connection.close()
        legacy = SQLiteStore(legacy_database)
        fresh = SQLiteStore(fresh_database)
        self.addCleanup(legacy.close)
        self.addCleanup(fresh.close)
        self.assertEqual(13, legacy.schema_version())
        expected_index = "idx_role_envelopes_envelope_hash"
        for store in (legacy, fresh):
            indexes = {
                str(row["name"]): int(row["unique"])
                for row in store._connection.execute(
                    "PRAGMA index_list(role_envelopes)"
                ).fetchall()
            }
            self.assertEqual(1, indexes[expected_index])
            sql = store._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (expected_index,),
            ).fetchone()[0]
            self.assertIn("WHERE envelope_hash IS NOT NULL", str(sql))


if __name__ == "__main__":
    unittest.main()
