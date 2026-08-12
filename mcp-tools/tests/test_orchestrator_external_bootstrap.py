"""Repository-only persistence contracts for external bootstrap descriptors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
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


def _replace_schema_metadata_with_legacy_version(
    connection: sqlite3.Connection,
    version: str,
) -> None:
    connection.execute("DROP TABLE schema_metadata")
    connection.execute(
        """
        CREATE TABLE schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
        ("schema_version", version),
    )


class ExternalBootstrapStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "orchestrator.sqlite3"
        self.store = SQLiteStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _records(
        self,
        *,
        assignment_hash: str | None = None,
        batch_label: str | None = None,
        descriptor_label: str = "descriptor",
        expires_at: str = "2026-08-04T00:00:00+00:00",
        grant_id: str = "grant-1",
    ) -> tuple[ExternalSourceDescriptor, ExternalBootstrapBatch, ExternalDispatchGrant]:
        descriptor = ExternalSourceDescriptor(
            descriptor_hash=_hash(descriptor_label),
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
            index_hash=_hash("index"),
            workflow_hash=_hash("workflow"),
            task_hash=_hash("task"),
            lease_hash=_hash("lease"),
        )
        batch = ExternalBootstrapBatch(
            batch_hash=_hash(batch_label or "batch:" + expires_at),
            descriptor_hash=descriptor.descriptor_hash,
            idempotency_key="external-bootstrap-"
            + hashlib.sha256(
                (batch_label or "batch:" + expires_at).encode("ascii")
            ).hexdigest()[:17],
            items=(item,),
            expires_at=expires_at,
        )
        grant = ExternalDispatchGrant(
            grant_id=grant_id,
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
            str(row[0])
            for row in self.store._connection.execute(
                "SELECT payload_json FROM external_bootstrap_descriptors "
                "UNION ALL SELECT payload_json FROM external_bootstrap_batches "
                "UNION ALL SELECT payload_json FROM external_bootstrap_outbox "
                "UNION ALL SELECT payload_json FROM external_dispatch_grants"
            )
        )
        self.assertNotIn("D:/", persisted)
        self.assertNotIn("bearer", persisted.lower())

    def test_schema_upgrade_exposes_external_bootstrap_tables(self) -> None:
        self.assertEqual(self.store.schema_version(), 13)
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
                "external_dispatch_grant_bindings",
                "external_bootstrap_batch_commitments",
                "external_dispatch_grant_commitments",
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
        self.store.admit_external_bootstrap(
            expired_descriptor, expired_batch, expired_grant
        )
        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                expired_grant.grant_id,
                descriptor_hash=expired_descriptor.descriptor_hash,
                batch_hash=expired_batch.batch_hash,
                assignment_hash=expired_grant.assignment_hash,
                now="2026-08-03T00:00:00+00:00",
            )

    def test_offset_expiry_is_normalized_to_utc_before_consumption(self) -> None:
        descriptor, batch, grant = self._records(
            batch_label="offset-expiry",
            expires_at="2026-08-03T08:00:00+08:00",
            grant_id="offset-grant",
        )
        _, stored_batch, _, stored_grant = self.store.admit_external_bootstrap(
            descriptor, batch, grant
        )
        self.assertEqual("2026-08-03T00:00:00+00:00", stored_batch.expires_at)
        self.assertEqual("2026-08-03T00:00:00+00:00", stored_grant.expires_at)

        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                grant.grant_id,
                descriptor_hash=descriptor.descriptor_hash,
                batch_hash=batch.batch_hash,
                assignment_hash=grant.assignment_hash,
                now="2026-08-03T00:00:01+00:00",
            )

    def test_batch_larger_than_nine_items_is_rejected_before_writes(self) -> None:
        descriptor, batch, grant = self._records(batch_label="too-many-items")
        item = batch.items[0]
        too_many = replace(
            batch,
            items=tuple(
                replace(
                    item,
                    item_index=index,
                    assignment_hash=_hash(f"assignment-{index}"),
                )
                for index in range(10)
            ),
        )

        with self.assertRaises(ExternalBootstrapConflictError):
            self.store.admit_external_bootstrap(
                descriptor,
                too_many,
                replace(grant, assignment_hash=_hash("assignment-0")),
            )

        self.assertEqual(self.store.external_bootstrap_counts(), (0, 0, 0, 0))

    def test_cross_bound_grant_rows_cannot_be_consumed(self) -> None:
        primary_descriptor, primary_batch, primary_grant = self._records(
            batch_label="primary-batch", grant_id="primary-grant"
        )
        other_descriptor, _, _ = self._records(
            descriptor_label="other-descriptor",
            batch_label="other-descriptor-batch",
            grant_id="other-descriptor-grant",
        )
        _, other_batch, _ = self._records(
            assignment_hash=_hash("other-batch-assignment"),
            batch_label="other-batch",
            grant_id="other-batch-grant",
        )
        _, assignment_batch, _ = self._records(
            assignment_hash=_hash("other-assignment"),
            batch_label="other-assignment-batch",
            grant_id="other-assignment-grant",
        )
        self.store.admit_external_bootstrap(
            primary_descriptor, primary_batch, primary_grant
        )
        self.store.admit_external_bootstrap(
            other_descriptor,
            self._records(
                descriptor_label="other-descriptor",
                batch_label="other-descriptor-batch",
                grant_id="other-descriptor-grant",
            )[1],
            self._records(
                descriptor_label="other-descriptor",
                batch_label="other-descriptor-batch",
                grant_id="other-descriptor-grant",
            )[2],
        )
        self.store.admit_external_bootstrap(
            primary_descriptor,
            other_batch,
            self._records(
                assignment_hash=_hash("other-batch-assignment"),
                batch_label="other-batch",
                grant_id="other-batch-grant",
            )[2],
        )
        self.store.admit_external_bootstrap(
            primary_descriptor,
            assignment_batch,
            self._records(
                assignment_hash=_hash("other-assignment"),
                batch_label="other-assignment-batch",
                grant_id="other-assignment-grant",
            )[2],
        )

        corruptions = (
            ("descriptor_hash", other_descriptor.descriptor_hash),
            ("batch_hash", other_batch.batch_hash),
            ("assignment_hash", assignment_batch.items[0].assignment_hash),
        )
        for column, value in corruptions:
            with self.subTest(column=column):
                self.store._connection.execute(
                    f"UPDATE external_dispatch_grants SET {column} = ? WHERE grant_id = ?",
                    (value, primary_grant.grant_id),
                )
                row = self.store._connection.execute(
                    "SELECT descriptor_hash, batch_hash, assignment_hash "
                    "FROM external_dispatch_grants WHERE grant_id = ?",
                    (primary_grant.grant_id,),
                ).fetchone()
                with self.assertRaises(ExternalDispatchGrantError):
                    self.store.get_external_dispatch_grant(primary_grant.grant_id)
                with self.assertRaises(ExternalDispatchGrantError):
                    self.store.consume_external_dispatch_grant(
                        primary_grant.grant_id,
                        descriptor_hash=str(row["descriptor_hash"]),
                        batch_hash=str(row["batch_hash"]),
                        assignment_hash=str(row["assignment_hash"]),
                        now="2026-08-03T00:00:00+00:00",
                    )
                self.store._connection.execute(
                    f"UPDATE external_dispatch_grants SET {column} = ? WHERE grant_id = ?",
                    (
                        getattr(primary_grant, column),
                        primary_grant.grant_id,
                    ),
                )

    def test_batch_item_requires_and_retains_index_workflow_task_and_lease_hashes(
        self,
    ) -> None:
        required_hashes = {
            "index_hash",
            "workflow_hash",
            "task_hash",
            "lease_hash",
        }
        self.assertTrue(
            required_hashes.issubset(ExternalBootstrapBatchItem.__dataclass_fields__)
        )
        descriptor, batch, grant = self._records(batch_label="item-hash-bindings")
        item = ExternalBootstrapBatchItem(
            item_index=0,
            workflow_id="workflow-1",
            task_id="task-1",
            lease_epoch=3,
            plan_hash=_hash("plan"),
            projection_hash=_hash("projection"),
            assignment_hash=grant.assignment_hash,
            predecessor_hash=_hash("predecessor"),
            quota_hash=_hash("quota"),
            route_hash=_hash("route"),
            index_hash=_hash("index"),
            workflow_hash=_hash("workflow"),
            task_hash=_hash("task"),
            lease_hash=_hash("lease"),
        )
        batch = replace(batch, items=(item,))
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        payload = self.store._connection.execute(
            "SELECT payload_json FROM external_bootstrap_batch_items "
            "WHERE batch_hash = ? AND item_index = 0",
            (batch.batch_hash,),
        ).fetchone()["payload_json"]
        for field in required_hashes:
            self.assertIn(field, payload)

    def test_descriptor_payload_or_hash_mismatch_rejects_read_and_consumption(
        self,
    ) -> None:
        descriptor, batch, grant = self._records(batch_label="corrupt-descriptor")
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        self.store._connection.execute(
            """
            UPDATE external_bootstrap_descriptors
            SET payload_json = ?, payload_hash = ?
            WHERE descriptor_hash = ?
            """,
            ("{}", "sha256:" + "f" * 64, descriptor.descriptor_hash),
        )

        with self.assertRaises(ExternalDispatchGrantError):
            self.store.get_external_dispatch_grant(grant.grant_id)
        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                grant.grant_id,
                descriptor_hash=descriptor.descriptor_hash,
                batch_hash=batch.batch_hash,
                assignment_hash=grant.assignment_hash,
                now="2026-08-03T00:00:00+00:00",
            )
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT consumed_at FROM external_dispatch_grants WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()["consumed_at"]
        )

    def test_self_consistent_descriptor_mutation_is_rejected_by_commitment_chain(
        self,
    ) -> None:
        descriptor, batch, grant = self._records(
            batch_label="self-consistent-descriptor-mutation"
        )
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        payload_json = self.store._connection.execute(
            "SELECT payload_json FROM external_bootstrap_descriptors "
            "WHERE descriptor_hash = ?",
            (descriptor.descriptor_hash,),
        ).fetchone()["payload_json"]
        payload = json.loads(payload_json)
        payload["source_hash"] = _hash("tampered-source")
        tampered_payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        tampered_payload_hash = (
            "sha256:"
            + hashlib.sha256(tampered_payload_json.encode("utf-8")).hexdigest()
        )
        self.store._connection.execute(
            """
            UPDATE external_bootstrap_descriptors
            SET payload_json = ?, payload_hash = ?
            WHERE descriptor_hash = ?
            """,
            (
                tampered_payload_json,
                tampered_payload_hash,
                descriptor.descriptor_hash,
            ),
        )

        with self.assertRaises(ExternalDispatchGrantError):
            self.store.get_external_dispatch_grant(grant.grant_id)
        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                grant.grant_id,
                descriptor_hash=descriptor.descriptor_hash,
                batch_hash=batch.batch_hash,
                assignment_hash=grant.assignment_hash,
                now="2026-08-03T00:00:00+00:00",
            )
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT consumed_at FROM external_dispatch_grants WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()["consumed_at"]
        )

    def test_batch_payload_or_hash_mismatch_rejects_read_and_consumption(self) -> None:
        descriptor, batch, grant = self._records(batch_label="corrupt-batch")
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        self.store._connection.execute(
            """
            UPDATE external_bootstrap_batches
            SET payload_json = ?, payload_hash = ?
            WHERE batch_hash = ?
            """,
            ("{}", "sha256:" + "e" * 64, batch.batch_hash),
        )

        with self.assertRaises(ExternalDispatchGrantError):
            self.store.get_external_dispatch_grant(grant.grant_id)
        with self.assertRaises(ExternalDispatchGrantError):
            self.store.consume_external_dispatch_grant(
                grant.grant_id,
                descriptor_hash=descriptor.descriptor_hash,
                batch_hash=batch.batch_hash,
                assignment_hash=grant.assignment_hash,
                now="2026-08-03T00:00:00+00:00",
            )
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT consumed_at FROM external_dispatch_grants WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()["consumed_at"]
        )

    def test_v8_batch_physical_descriptor_hash_mismatch_rolls_back_before_bindings(
        self,
    ) -> None:
        descriptor, batch, grant = self._records(
            batch_label="v8-physical-descriptor-mismatch",
            grant_id="v8-physical-descriptor-mismatch-grant",
        )
        other_descriptor, other_batch, other_grant = self._records(
            descriptor_label="v8-other-descriptor",
            batch_label="v8-other-batch",
            grant_id="v8-other-grant",
        )
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        self.store.admit_external_bootstrap(other_descriptor, other_batch, other_grant)
        self.store.close()

        connection = sqlite3.connect(self.database)
        try:
            for table in (
                "external_dispatch_grant_bindings",
                "external_bootstrap_batch_commitments",
                "external_dispatch_grant_commitments",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            _replace_schema_metadata_with_legacy_version(connection, "8")
            connection.execute(
                """
                UPDATE external_bootstrap_batches
                SET descriptor_hash = ?
                WHERE batch_hash = ?
                """,
                (other_descriptor.descriptor_hash, batch.batch_hash),
            )
            connection.commit()
        finally:
            connection.close()

        reopened: SQLiteStore | None = None
        try:
            with self.assertRaises(ExternalBootstrapConflictError):
                reopened = SQLiteStore(self.database)
        finally:
            if reopened is not None:
                reopened.close()

        connection = sqlite3.connect(self.database)
        try:
            schema_version = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            persisted_descriptor_hash = connection.execute(
                """
                SELECT descriptor_hash FROM external_bootstrap_batches
                WHERE batch_hash = ?
                """,
                (batch.batch_hash,),
            ).fetchone()[0]
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual("8", schema_version)
        self.assertEqual(other_descriptor.descriptor_hash, persisted_descriptor_hash)
        self.assertFalse(
            {
                "external_dispatch_grant_bindings",
                "external_bootstrap_batch_commitments",
                "external_dispatch_grant_commitments",
            }
            & table_names
        )

    def test_v8_descriptor_item_and_grant_raw_mismatches_roll_back_before_bindings(
        self,
    ) -> None:
        for target in ("descriptor", "item", "grant"):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / "orchestrator.sqlite3"
                    store = SQLiteStore(database)
                    descriptor, batch, grant = self._records(
                        batch_label=f"v8-raw-{target}",
                        grant_id=f"v8-raw-{target}-grant",
                    )
                    store.admit_external_bootstrap(descriptor, batch, grant)
                    store.close()

                    connection = sqlite3.connect(database)
                    try:
                        for table in (
                            "external_dispatch_grant_bindings",
                            "external_bootstrap_batch_commitments",
                            "external_dispatch_grant_commitments",
                        ):
                            connection.execute(f"DROP TABLE IF EXISTS {table}")
                        _replace_schema_metadata_with_legacy_version(connection, "8")
                        if target == "descriptor":
                            payload_json = connection.execute(
                                """
                                SELECT payload_json FROM external_bootstrap_descriptors
                                WHERE descriptor_hash = ?
                                """,
                                (descriptor.descriptor_hash,),
                            ).fetchone()[0]
                            payload = json.loads(payload_json)
                            payload["descriptor_hash"] = _hash(
                                "v8-raw-descriptor-payload"
                            )
                            tampered_payload_json = json.dumps(
                                payload,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            )
                            connection.execute(
                                """
                                UPDATE external_bootstrap_descriptors
                                SET payload_json = ?, payload_hash = ?
                                WHERE descriptor_hash = ?
                                """,
                                (
                                    tampered_payload_json,
                                    "sha256:"
                                    + hashlib.sha256(
                                        tampered_payload_json.encode("utf-8")
                                    ).hexdigest(),
                                    descriptor.descriptor_hash,
                                ),
                            )
                        elif target == "item":
                            connection.execute(
                                """
                                UPDATE external_bootstrap_batch_items
                                SET assignment_hash = ?
                                WHERE batch_hash = ? AND item_index = 0
                                """,
                                (_hash("v8-raw-item-column"), batch.batch_hash),
                            )
                        else:
                            connection.execute(
                                """
                                UPDATE external_dispatch_grants
                                SET assignment_hash = ?
                                WHERE grant_id = ?
                                """,
                                (_hash("v8-raw-grant-column"), grant.grant_id),
                            )
                        connection.commit()
                    finally:
                        connection.close()

                    with self.assertRaises(ExternalBootstrapConflictError):
                        SQLiteStore(database)

                    connection = sqlite3.connect(database)
                    try:
                        schema_version = connection.execute(
                            "SELECT value FROM schema_metadata "
                            "WHERE key = 'schema_version'"
                        ).fetchone()[0]
                        table_names = {
                            row[0]
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type = 'table'"
                            )
                        }
                    finally:
                        connection.close()
                    self.assertEqual("8", schema_version)
                    self.assertFalse(
                        {
                            "external_dispatch_grant_bindings",
                            "external_bootstrap_batch_commitments",
                            "external_dispatch_grant_commitments",
                        }
                        & table_names
                    )

    def test_v8_expiry_column_payload_hash_mismatch_fails_open_without_rewrite(
        self,
    ) -> None:
        descriptor, batch, grant = self._records(
            batch_label="v8-raw-expiry",
            expires_at="2026-08-03T08:00:00+08:00",
            grant_id="v8-raw-expiry-grant",
        )
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        self.store.close()

        raw_expiry = "2026-08-03T08:00:00+08:00"
        connection = sqlite3.connect(self.database)
        try:
            payload_json, payload_hash = connection.execute(
                """
                SELECT payload_json, payload_hash FROM external_bootstrap_batches
                WHERE batch_hash = ?
                """,
                (batch.batch_hash,),
            ).fetchone()
            payload = json.loads(payload_json)
            payload["expires_at"] = raw_expiry
            raw_payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            for table in (
                "external_dispatch_grant_bindings",
                "external_bootstrap_batch_commitments",
                "external_dispatch_grant_commitments",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            _replace_schema_metadata_with_legacy_version(connection, "8")
            connection.execute(
                """
                UPDATE external_bootstrap_batches
                SET expires_at = ?, payload_json = ?
                WHERE batch_hash = ?
                """,
                (raw_expiry, raw_payload_json, batch.batch_hash),
            )
            connection.commit()
        finally:
            connection.close()

        reopened: SQLiteStore | None = None
        try:
            with self.assertRaises(ExternalBootstrapConflictError):
                reopened = SQLiteStore(self.database)
        finally:
            if reopened is not None:
                reopened.close()

        connection = sqlite3.connect(self.database)
        try:
            persisted_expiry, persisted_payload, persisted_hash, schema_version = (
                connection.execute(
                    """
                    SELECT batch.expires_at, batch.payload_json, batch.payload_hash,
                           schema.value
                    FROM external_bootstrap_batches AS batch
                    JOIN schema_metadata AS schema ON schema.key = 'schema_version'
                    WHERE batch.batch_hash = ?
                    """,
                    (batch.batch_hash,),
                ).fetchone()
            )
        finally:
            connection.close()
        self.assertEqual(raw_expiry, persisted_expiry)
        self.assertEqual(raw_payload_json, persisted_payload)
        self.assertEqual(payload_hash, persisted_hash)
        self.assertEqual("8", schema_version)

    def test_valid_v8_offset_expiry_is_retained_but_fails_closed_without_v10_commitments(
        self,
    ) -> None:
        descriptor, batch, grant = self._records(
            batch_label="v8-valid-expiry",
            expires_at="2026-08-03T08:00:00+08:00",
            grant_id="v8-valid-expiry-grant",
        )
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        self.store.close()

        raw_expiry = "2026-08-03T08:00:00+08:00"
        connection = sqlite3.connect(self.database)
        try:
            for table in (
                "external_dispatch_grant_bindings",
                "external_bootstrap_batch_commitments",
                "external_dispatch_grant_commitments",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            _replace_schema_metadata_with_legacy_version(connection, "8")
            for table, identity_column, identity in (
                ("external_bootstrap_batches", "batch_hash", batch.batch_hash),
                ("external_dispatch_grants", "grant_id", grant.grant_id),
            ):
                payload_json = connection.execute(
                    f"SELECT payload_json FROM {table} WHERE {identity_column} = ?",
                    (identity,),
                ).fetchone()[0]
                payload = json.loads(payload_json)
                payload["expires_at"] = raw_expiry
                raw_payload_json = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                raw_payload_hash = (
                    "sha256:"
                    + hashlib.sha256(raw_payload_json.encode("utf-8")).hexdigest()
                )
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET expires_at = ?, payload_json = ?, payload_hash = ?
                    WHERE {identity_column} = ?
                    """,
                    (raw_expiry, raw_payload_json, raw_payload_hash, identity),
                )
            connection.commit()
        finally:
            connection.close()

        reopened = SQLiteStore(self.database)
        try:
            self.assertEqual(13, reopened.schema_version())
            self.assertEqual(
                "2026-08-03T00:00:00+00:00",
                reopened._connection.execute(
                    """
                    SELECT expires_at FROM external_dispatch_grants
                    WHERE grant_id = ?
                    """,
                    (grant.grant_id,),
                ).fetchone()["expires_at"],
            )
            with self.assertRaises(ExternalDispatchGrantError):
                reopened.get_external_dispatch_grant(grant.grant_id)
            with self.assertRaises(ExternalDispatchGrantError):
                reopened.consume_external_dispatch_grant(
                    grant.grant_id,
                    descriptor_hash=descriptor.descriptor_hash,
                    batch_hash=batch.batch_hash,
                    assignment_hash=grant.assignment_hash,
                    now="2026-08-02T23:59:59+00:00",
                )
            self.assertIsNone(
                reopened._connection.execute(
                    "SELECT consumed_at FROM external_dispatch_grants WHERE grant_id = ?",
                    (grant.grant_id,),
                ).fetchone()["consumed_at"]
            )
        finally:
            reopened.close()

    def test_authentic_c53_v8_item_is_retained_but_fails_closed_without_v9_hashes(
        self,
    ) -> None:
        descriptor, batch, grant = self._records(
            batch_label="c53-v8-item",
            expires_at="2026-08-03T08:00:00+08:00",
            grant_id="c53-v8-grant",
        )
        self.store.admit_external_bootstrap(descriptor, batch, grant)
        self.store.close()

        raw_expiry = "2026-08-03T08:00:00+08:00"
        missing_v9_hashes = {
            "index_hash",
            "workflow_hash",
            "task_hash",
            "lease_hash",
        }
        connection = sqlite3.connect(self.database)
        try:
            for table in (
                "external_dispatch_grant_bindings",
                "external_bootstrap_batch_commitments",
                "external_dispatch_grant_commitments",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            _replace_schema_metadata_with_legacy_version(connection, "8")
            item_payload_json = connection.execute(
                """
                SELECT payload_json FROM external_bootstrap_batch_items
                WHERE batch_hash = ? AND item_index = 0
                """,
                (batch.batch_hash,),
            ).fetchone()[0]
            item_payload = json.loads(item_payload_json)
            for field in missing_v9_hashes:
                item_payload.pop(field)
            legacy_item_json = json.dumps(
                item_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            legacy_item_hash = (
                "sha256:" + hashlib.sha256(legacy_item_json.encode("utf-8")).hexdigest()
            )
            connection.execute(
                """
                UPDATE external_bootstrap_batch_items
                SET payload_json = ?, payload_hash = ?
                WHERE batch_hash = ? AND item_index = 0
                """,
                (legacy_item_json, legacy_item_hash, batch.batch_hash),
            )
            for table, identity_column, identity in (
                ("external_bootstrap_batches", "batch_hash", batch.batch_hash),
                ("external_dispatch_grants", "grant_id", grant.grant_id),
            ):
                payload_json = connection.execute(
                    f"SELECT payload_json FROM {table} WHERE {identity_column} = ?",
                    (identity,),
                ).fetchone()[0]
                payload = json.loads(payload_json)
                payload["expires_at"] = raw_expiry
                if table == "external_bootstrap_batches":
                    for field in missing_v9_hashes:
                        payload["items"][0].pop(field)
                legacy_payload_json = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                legacy_payload_hash = (
                    "sha256:"
                    + hashlib.sha256(legacy_payload_json.encode("utf-8")).hexdigest()
                )
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET expires_at = ?, payload_json = ?, payload_hash = ?
                    WHERE {identity_column} = ?
                    """,
                    (raw_expiry, legacy_payload_json, legacy_payload_hash, identity),
                )
            connection.commit()
        finally:
            connection.close()

        reopened = SQLiteStore(self.database)
        try:
            self.assertEqual(13, reopened.schema_version())
            persisted_item_json = reopened._connection.execute(
                """
                SELECT payload_json FROM external_bootstrap_batch_items
                WHERE batch_hash = ? AND item_index = 0
                """,
                (batch.batch_hash,),
            ).fetchone()["payload_json"]
            self.assertTrue(
                missing_v9_hashes.isdisjoint(json.loads(persisted_item_json))
            )
            with self.assertRaises(ExternalDispatchGrantError):
                reopened.get_external_dispatch_grant(grant.grant_id)
            with self.assertRaises(ExternalDispatchGrantError):
                reopened.consume_external_dispatch_grant(
                    grant.grant_id,
                    descriptor_hash=descriptor.descriptor_hash,
                    batch_hash=batch.batch_hash,
                    assignment_hash=grant.assignment_hash,
                    now="2026-08-03T00:00:00+00:00",
                )
            self.assertIsNone(
                reopened._connection.execute(
                    "SELECT consumed_at FROM external_dispatch_grants WHERE grant_id = ?",
                    (grant.grant_id,),
                ).fetchone()["consumed_at"]
            )
        finally:
            reopened.close()

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
