"""Repository-only persistence contracts for external bootstrap descriptors."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import (
    ExternalBootstrapBatch,
    ExternalBootstrapBatchItem,
    ExternalBootstrapState,
    ExternalDispatchGrant,
    ExternalSourceDescriptor,
)
from orchestrator.store import (
    ExternalBootstrapConflictError,
    ExternalDispatchGrantError,
    SQLiteStore,
)


def _hash(label: str) -> str:
    return "sha256:" + (label.encode("ascii").hex() * 64)[:64]


class ExternalBootstrapStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "orchestrator.sqlite3"
        self.store = SQLiteStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _records(
        self, *, assignment_hash: str | None = None, expires_at: str = "2026-08-04T00:00:00+00:00"
    ) -> tuple[ExternalSourceDescriptor, ExternalBootstrapBatch, ExternalDispatchGrant]:
        descriptor = ExternalSourceDescriptor(
            descriptor_hash=_hash("descriptor"),
            source_hash=_hash("source"),
            repository_hash=_hash("repository"),
            common_dir_hash=_hash("common"),
            project_hash=_hash("project"),
            task_root_hash=_hash("root"),
            ref_hash=_hash("ref"),
            commit_hash=_hash("commit"),
            tree_hash=_hash("tree"),
        )
        item = ExternalBootstrapBatchItem(
            item_index=0,
            workflow_id="workflow-1",
            task_id="task-1",
            lease_epoch=3,
            plan_hash=_hash("plan"),
            projection_hash=_hash("projection"),
            assignment_hash=assignment_hash or _hash("assignment"),
            predecessor_hash=_hash("predecessor"),
            quota_hash=_hash("quota"),
            route_hash=_hash("route"),
        )
        batch = ExternalBootstrapBatch(
            batch_hash=_hash("batch:" + expires_at),
            descriptor_hash=descriptor.descriptor_hash,
            idempotency_key="external-bootstrap-" + expires_at[:10],
            items=(item,),
            expires_at=expires_at,
        )
        grant = ExternalDispatchGrant(
            grant_id="grant-1",
            descriptor_hash=descriptor.descriptor_hash,
            batch_hash=batch.batch_hash,
            assignment_hash=item.assignment_hash,
            expires_at=expires_at,
        )
        return descriptor, batch, grant

    def test_admission_is_idempotent_hash_only_and_host_api_unavailable(self) -> None:
        descriptor, batch, grant = self._records()

        first = self.store.admit_external_bootstrap(descriptor, batch, grant)
        second = self.store.admit_external_bootstrap(descriptor, batch, grant)

        self.assertEqual(first, second)
        stored_descriptor, stored_batch, outbox, stored_grant = first
        self.assertEqual(stored_descriptor, descriptor)
        self.assertEqual(stored_batch, batch)
        self.assertEqual(outbox.state, ExternalBootstrapState.PENDING)
        self.assertEqual(outbox.availability, "HOST_API_UNAVAILABLE")
        self.assertEqual(stored_grant.state, ExternalBootstrapState.PENDING)
        self.assertEqual(stored_grant.availability, "HOST_API_UNAVAILABLE")
        persisted = "\n".join(
            str(row[0]) for row in self.store._connection.execute(
                "SELECT payload_json FROM external_bootstrap_descriptors "
                "UNION ALL SELECT payload_json FROM external_bootstrap_batches "
                "UNION ALL SELECT payload_json FROM external_bootstrap_outbox "
                "UNION ALL SELECT payload_json FROM external_dispatch_grants"
            )
        )
        self.assertNotIn("D:/", persisted)
        self.assertNotIn("bearer", persisted.lower())

    def test_schema_upgrade_exposes_external_bootstrap_tables(self) -> None:
        self.assertEqual(self.store.schema_version(), 8)
        table_names = {
            str(row[0])
            for row in self.store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "external_bootstrap_descriptors",
                "external_bootstrap_batches",
                "external_bootstrap_outbox",
                "external_dispatch_grants",
            }.issubset(table_names)
        )

    def test_conflicting_binding_rolls_back_every_new_row(self) -> None:
        descriptor, batch, grant = self._records()
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        conflict_descriptor, conflict_batch, conflict_grant = self._records(
            assignment_hash=_hash("other-assignment")
        )

        with self.assertRaises(ExternalBootstrapConflictError):
            self.store.admit_external_bootstrap(
                conflict_descriptor, conflict_batch, conflict_grant
            )

        self.assertEqual(self.store.external_bootstrap_counts(), (1, 1, 1, 1))
        self.assertEqual(self.store.get_external_dispatch_grant(grant.grant_id), grant)

    def test_invalid_grant_rolls_back_descriptor_batch_and_outbox(self) -> None:
        descriptor, batch, grant = self._records()
        invalid_grant = ExternalDispatchGrant(
            grant_id=grant.grant_id,
            descriptor_hash=descriptor.descriptor_hash,
            batch_hash=batch.batch_hash,
            assignment_hash=_hash("wrong-assignment"),
            expires_at=grant.expires_at,
        )

        with self.assertRaises(ExternalBootstrapConflictError):
            self.store.admit_external_bootstrap(descriptor, batch, invalid_grant)

        self.assertEqual(self.store.external_bootstrap_counts(), (0, 0, 0, 0))

    def test_second_grant_cannot_rebind_an_existing_assignment(self) -> None:
        descriptor, batch, grant = self._records()
        self.store.admit_external_bootstrap(descriptor, batch, grant)

        with self.assertRaises(ExternalBootstrapConflictError):
            self.store.admit_external_bootstrap(
                descriptor, batch, replace(grant, grant_id="grant-2")
            )

        self.assertEqual(self.store.external_bootstrap_counts(), (1, 1, 1, 1))

    def test_grant_consumption_is_one_shot_bound_and_expiry_checked(self) -> None:
        descriptor, batch, grant = self._records()
        self.store.admit_external_bootstrap(descriptor, batch, grant)

        consumed = self.store.consume_external_dispatch_grant(
            grant.grant_id,
            descriptor_hash=descriptor.descriptor_hash,
            batch_hash=batch.batch_hash,
            assignment_hash=grant.assignment_hash,
            now="2026-08-03T00:00:00+00:00",
        )
        self.assertIsNotNone(consumed.consumed_at)
        self.assertEqual(consumed.availability, "HOST_API_UNAVAILABLE")
        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                grant.grant_id,
                descriptor_hash=descriptor.descriptor_hash,
                batch_hash=batch.batch_hash,
                assignment_hash=grant.assignment_hash,
                now="2026-08-03T00:00:01+00:00",
            )

        expired_descriptor, expired_batch, expired_grant = self._records(
            expires_at="2026-08-02T00:00:00+00:00"
        )
        expired_grant = ExternalDispatchGrant(
            grant_id="expired-grant",
            descriptor_hash=expired_grant.descriptor_hash,
            batch_hash=expired_grant.batch_hash,
            assignment_hash=expired_grant.assignment_hash,
            expires_at=expired_grant.expires_at,
        )
        self.store.admit_external_bootstrap(expired_descriptor, expired_batch, expired_grant)
        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                expired_grant.grant_id,
                descriptor_hash=expired_descriptor.descriptor_hash,
                batch_hash=expired_batch.batch_hash,
                assignment_hash=expired_grant.assignment_hash,
                now="2026-08-03T00:00:00+00:00",
            )

    def test_concurrent_consumers_admit_exactly_one_winner(self) -> None:
        descriptor, batch, grant = self._records()
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def consume() -> None:
            store = SQLiteStore(self.database)
            try:
                barrier.wait()
                store.consume_external_dispatch_grant(
                    grant.grant_id,
                    descriptor_hash=descriptor.descriptor_hash,
                    batch_hash=batch.batch_hash,
                    assignment_hash=grant.assignment_hash,
                    now="2026-08-03T00:00:00+00:00",
                )
                outcomes.append("consumed")
            except ExternalDispatchGrantError:
                outcomes.append("rejected")
            finally:
                store.close()

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ("consumed", "rejected"))


if __name__ == "__main__":
    unittest.main()
