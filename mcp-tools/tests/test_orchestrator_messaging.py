"""Integration tests for peer messaging without coordinator body relay."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from orchestrator.models import Task, Workflow, WorkflowKind  # noqa: E402
from orchestrator.service import OrchestratorService, ServiceError  # noqa: E402
from orchestrator.store import SQLiteStore  # noqa: E402


class OrchestratorMessagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(
            dir="D:/bun/tmp/codex/bugkiller-plugin/orch-04"
        )
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


if __name__ == "__main__":
    unittest.main()
