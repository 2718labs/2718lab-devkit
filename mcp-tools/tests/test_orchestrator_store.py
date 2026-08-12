"""Persistence contract tests for the SQLite orchestration store."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import Task, TaskState, Workflow, WorkflowKind
from orchestrator.store import (
    SQLiteStore,
    StaleLeaseError,
    StoreError,
    StrictIndexError,
    VersionConflictError,
)


class SQLiteStoreTests(unittest.TestCase):
    _WORKSPACE_ID = "sha256:" + "1" * 64

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "orchestrator.sqlite3"
        self.store = SQLiteStore(self.database)
        self.workflow = Workflow("workflow-1", WorkflowKind.DAG, "Title", "Summary")
        self.store.create_workflow(self.workflow)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _task(self, task_id: str) -> Task:
        return Task(task_id, self.workflow.id, task_id, "terra")

    def _legacy_v12_wal_database(self, name: str) -> Path:
        """Build one complete v12-shaped WAL database without any v13 object."""
        database = Path(self.directory.name) / name
        seeded = SQLiteStore(database)
        try:
            seeded.create_workflow(
                Workflow("legacy-workflow", WorkflowKind.DAG, "legacy", "summary")
            )
        finally:
            seeded.close()
        connection = sqlite3.connect(database)
        try:
            connection.execute("DROP TRIGGER IF EXISTS atlas_finalizations_no_update")
            connection.execute("DROP TRIGGER IF EXISTS atlas_finalizations_no_delete")
            connection.execute("DROP TABLE IF EXISTS atlas_finalizations")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "UPDATE schema_metadata SET value = '12' WHERE key = 'schema_version'"
            )
            connection.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-wal-workflow",
                    "dag",
                    "legacy WAL",
                    "summary",
                    "new",
                    0,
                    "",
                    "2026-08-12T00:00:00+00:00",
                    "2026-08-12T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return database

    def _seed_finalization_identities(self) -> tuple[str, str, str]:
        acceptance_id = "sha256:" + "a" * 64
        self._seed_projected_outbox("finalization-task", acceptance_id)
        return (
            acceptance_id,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
        )

    def _seed_projected_outbox(self, task_id: str, acceptance_id: str) -> None:
        task = self.store.register_task(
            Task(task_id, self.workflow.id, "task", "terra")
        )
        with self.store._transaction() as cursor:  # noqa: SLF001 - contract fixture
            cursor.execute(
                """
                INSERT INTO code_task_acceptances (
                    acceptance_id, workflow_id, code_task_id, code_task_version,
                    input_snapshot_id, output_snapshot_id, indexed_diff_hash,
                    intent_id, language, framework, payload_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    acceptance_id,
                    self.workflow.id,
                    task.id,
                    0,
                    "input",
                    "output",
                    "diff",
                    "intent",
                    "python",
                    "pytest",
                    "{}",
                    acceptance_id,
                    "2026-08-12T00:00:00+00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO atlas_ingestion_outbox (
                    ingestion_key, acceptance_id, payload_json, payload_hash, state,
                    attempt_count, last_error_code, reason_codes_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    acceptance_id,
                    acceptance_id,
                    "{}",
                    acceptance_id,
                    "projected",
                    0,
                    "",
                    "[]",
                    "2026-08-12T00:00:00+00:00",
                    "2026-08-12T00:00:00+00:00",
                ),
            )

    @staticmethod
    def _sqlite_file_snapshot(
        database: Path,
    ) -> tuple[bytes, bytes | None, bytes | None]:
        sidecars = tuple(
            database.with_name(f"{database.name}{suffix}")
            for suffix in ("-wal", "-shm")
        )
        return (
            database.read_bytes(),
            *(path.read_bytes() if path.exists() else None for path in sidecars),
        )

    def test_prepared_store_requires_delete_journal_and_finalization_relation(self) -> None:
        """The v13 prepared-store contract has no WAL recovery dependency."""
        table_names = {
            str(row["name"])
            for row in self.store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        with self.subTest("schema version"):
            self.assertEqual(self.store.schema_version(), 13)
        with self.subTest("physical journal mode"):
            self.assertEqual(self.store.journal_mode(), "delete")
        with self.subTest("immutable finalization relation"):
            self.assertIn("atlas_finalizations", table_names)
        with self.subTest("foreign keys"):
            self.assertTrue(self.store.foreign_keys_enabled())

    def test_v12_wal_migration_preserves_rows_and_switches_to_delete(self) -> None:
        database = self._legacy_v12_wal_database("legacy-v12-wal.sqlite")

        migrated = SQLiteStore(database)
        try:
            self.assertEqual(13, migrated.schema_version())
            self.assertEqual("delete", migrated.journal_mode())
            self.assertEqual("legacy WAL", migrated.get_workflow("legacy-wal-workflow").title)
        finally:
            migrated.close()

        self.assertEqual(b"\x01\x01", database.read_bytes()[18:20])
        self.assertFalse(database.with_name(f"{database.name}-wal").exists())
        self.assertFalse(database.with_name(f"{database.name}-shm").exists())

    def test_v12_quiescent_paired_wal_migrates_to_delete(self) -> None:
        database = self._legacy_v12_wal_database("legacy-v12-quiescent-pair.sqlite")
        checkpoint = sqlite3.connect(database)
        try:
            checkpoint.execute("PRAGMA wal_autocheckpoint = 0")
            self.assertEqual((0, 0, 0), checkpoint.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            wal = database.with_name(f"{database.name}-wal")
            shm = database.with_name(f"{database.name}-shm")
            self.assertTrue(wal.exists())
            self.assertTrue(shm.exists())
        finally:
            checkpoint.close()

        migrated = SQLiteStore(database)
        try:
            self.assertEqual(13, migrated.schema_version())
            self.assertEqual("delete", migrated.journal_mode())
            self.assertEqual("legacy WAL", migrated.get_workflow("legacy-wal-workflow").title)
        finally:
            migrated.close()

    def test_v12_checkpoint_or_sidecar_failure_is_fail_closed_without_schema_mutation(
        self,
    ) -> None:
        database = self._legacy_v12_wal_database("legacy-v12-busy.sqlite")
        reader = sqlite3.connect(database)
        writer = sqlite3.connect(database)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM workflows").fetchall()
            writer.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "checkpoint-blocker",
                    "dag",
                    "blocked",
                    "summary",
                    "new",
                    0,
                    "",
                    "2026-08-12T00:00:00+00:00",
                    "2026-08-12T00:00:00+00:00",
                ),
            )
            writer.commit()
            with self.assertRaises(StoreError):
                SQLiteStore(database)
        finally:
            writer.close()
            reader.close()

        connection = sqlite3.connect(database)
        try:
            self.assertEqual(
                "12",
                connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'atlas_finalizations'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v12_outbox_identity_drift_fails_before_journal_mutation(self) -> None:
        database = self._legacy_v12_wal_database("legacy-v12-outbox-drift.sqlite")
        connection = sqlite3.connect(database)
        try:
            connection.execute("DROP TABLE atlas_ingestion_outbox")
            connection.execute(
                """
                CREATE TABLE atlas_ingestion_outbox (
                    ingestion_key TEXT NOT NULL,
                    acceptance_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'projected', 'quarantined')),
                    attempt_count INTEGER NOT NULL CHECK (attempt_count BETWEEN 0 AND 16),
                    last_error_code TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (ingestion_key = payload_hash)
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        keeper = sqlite3.connect(database)
        try:
            keeper.execute("SELECT name FROM sqlite_master").fetchall()
            before = self._sqlite_file_snapshot(database)
            with self.assertRaises(StoreError):
                SQLiteStore(database)
            self.assertEqual(before, self._sqlite_file_snapshot(database))
        finally:
            keeper.close()

    def test_malformed_paired_v12_wal_fails_without_any_physical_mutation(self) -> None:
        database = self._legacy_v12_wal_database("legacy-v12-paired-sidecar.sqlite")
        wal = database.with_name(f"{database.name}-wal")
        shm = database.with_name(f"{database.name}-shm")
        wal.write_bytes(b"7\x7f\x06\x82" + b"\x00" * 28)
        shm.write_bytes(b"\x00" * 32_768)
        before = self._sqlite_file_snapshot(database)

        with self.assertRaises(StoreError):
            SQLiteStore(database)

        self.assertEqual(before, self._sqlite_file_snapshot(database))

        invalid = self._legacy_v12_wal_database("legacy-v12-invalid-sidecar.sqlite")
        invalid.with_name(f"{invalid.name}-wal").write_bytes(b"not-a-wal")
        with self.assertRaises(StoreError):
            SQLiteStore(invalid)
        connection = sqlite3.connect(invalid)
        try:
            self.assertEqual(
                "12",
                connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'atlas_finalizations'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_finalization_certificate_is_canonical_validated_and_immutable(self) -> None:
        acceptance_id, continuity_key_hash, published_receipt_hash = (
            self._seed_finalization_identities()
        )
        finalization = self.store._build_atlas_finalization(  # noqa: SLF001
            acceptance_id=acceptance_id,
            ingestion_key=acceptance_id,
            payload_hash=acceptance_id,
            continuity_key_hash=continuity_key_hash,
            view_id="view-1",
            fence_epoch=1,
            pointer_version=1,
            published_receipt_hash=published_receipt_hash,
            atlas_receipt_digest="sha256:" + "d" * 64,
            created_at="2026-08-12T00:00:00+00:00",
        )
        with self.store._transaction() as cursor:  # noqa: SLF001 - internal seam
            stored = self.store._insert_atlas_finalization(cursor, finalization)  # noqa: SLF001

        self.assertEqual(finalization, stored)
        self.assertEqual(
            finalization,
            self.store._atlas_finalization_for_acceptance(acceptance_id),  # noqa: SLF001
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(  # noqa: SLF001 - trigger contract
                "UPDATE atlas_finalizations SET view_id = 'other' WHERE acceptance_id = ?",
                (acceptance_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(  # noqa: SLF001 - trigger contract
                "DELETE FROM atlas_finalizations WHERE acceptance_id = ?",
                (acceptance_id,),
            )

        with self.assertRaises(ValueError):
            self.store._insert_atlas_finalization(  # noqa: SLF001
                self.store._connection.cursor(),
                replace(finalization, finalization_hash="sha256:" + "e" * 64),
            )
        mismatched = self.store._build_atlas_finalization(  # noqa: SLF001
            acceptance_id=acceptance_id,
            ingestion_key=acceptance_id,
            payload_hash="sha256:" + "f" * 64,
            continuity_key_hash=continuity_key_hash,
            view_id="view-1",
            fence_epoch=1,
            pointer_version=1,
            published_receipt_hash=published_receipt_hash,
            atlas_receipt_digest="sha256:" + "d" * 64,
            created_at="2026-08-12T00:00:00+00:00",
        )
        with self.assertRaises(StoreError):
            with self.store._transaction() as cursor:  # noqa: SLF001
                self.store._insert_atlas_finalization(cursor, mismatched)  # noqa: SLF001

        self.store._connection.execute(  # noqa: SLF001 - corruption read contract
            "DROP TRIGGER atlas_finalizations_no_update"
        )
        self.store._connection.execute("PRAGMA foreign_keys = OFF")  # noqa: SLF001
        self.store._connection.execute(  # noqa: SLF001 - corruption read contract
            """
            UPDATE atlas_finalizations SET payload_hash = ?, finalization_hash = ?
            WHERE acceptance_id = ?
            """,
            (mismatched.payload_hash, finalization.finalization_hash, acceptance_id),
        )
        self.store._connection.execute("PRAGMA foreign_keys = ON")  # noqa: SLF001
        with self.assertRaises(StoreError):
            self.store._atlas_finalization_for_acceptance(acceptance_id)  # noqa: SLF001

    def test_prepared_connection_rejects_finalization_shape_or_trigger_drift(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER atlas_finalizations_no_update")
            connection.commit()
        finally:
            connection.close()
        prepared = sqlite3.connect(self.database)
        try:
            with self.assertRaises(StoreError):
                SQLiteStore.from_prepared_connection(prepared)
        finally:
            prepared.close()

    def test_prepared_connection_rejects_finalization_type_or_guard_drift(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA writable_schema = ON")
            connection.execute(
                """
                UPDATE sqlite_master
                SET sql = replace(sql, 'view_id TEXT NOT NULL', 'view_id BLOB NOT NULL')
                WHERE type = 'table' AND name = 'atlas_finalizations'
                """
            )
            connection.execute("PRAGMA schema_version = 101")
            connection.execute("PRAGMA writable_schema = OFF")
            connection.commit()
        finally:
            connection.close()
        prepared = sqlite3.connect(self.database)
        try:
            with self.assertRaises(StoreError):
                SQLiteStore.from_prepared_connection(prepared)
        finally:
            prepared.close()

    def test_prepared_connection_rejects_non_abort_finalization_trigger_guard(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER atlas_finalizations_no_update")
            connection.execute(
                """
                CREATE TRIGGER atlas_finalizations_no_update
                BEFORE UPDATE ON atlas_finalizations WHEN 0
                BEGIN
                    SELECT RAISE(ABORT, 'atlas finalization is immutable');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()
        prepared = sqlite3.connect(self.database)
        try:
            with self.assertRaises(StoreError):
                SQLiteStore.from_prepared_connection(prepared)
        finally:
            prepared.close()

    def test_cross_bound_finalization_is_rejected_on_read_and_prepared_open(self) -> None:
        acceptance_id, continuity_key_hash, published_receipt_hash = (
            self._seed_finalization_identities()
        )
        other_acceptance = "sha256:" + "e" * 64
        self._seed_projected_outbox("other-finalization-task", other_acceptance)
        cross_bound = self.store._build_atlas_finalization(  # noqa: SLF001
            acceptance_id=acceptance_id,
            ingestion_key=other_acceptance,
            payload_hash=other_acceptance,
            continuity_key_hash=continuity_key_hash,
            view_id="view-cross",
            fence_epoch=1,
            pointer_version=1,
            published_receipt_hash=published_receipt_hash,
            atlas_receipt_digest="sha256:" + "d" * 64,
            created_at="2026-08-12T00:00:00+00:00",
        )
        self.store._connection.execute("PRAGMA foreign_keys = OFF")  # noqa: SLF001
        self.store._connection.execute(  # noqa: SLF001 - persisted corruption fixture
            "DROP TRIGGER atlas_finalizations_require_projected_outbox"
        )
        self.store._connection.execute(  # noqa: SLF001 - persisted corruption fixture
            """
            INSERT INTO atlas_finalizations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cross_bound.schema_version,
                cross_bound.acceptance_id,
                cross_bound.ingestion_key,
                cross_bound.payload_hash,
                cross_bound.continuity_key_hash,
                cross_bound.view_id,
                cross_bound.fence_epoch,
                cross_bound.pointer_version,
                cross_bound.published_receipt_hash,
                cross_bound.atlas_receipt_digest,
                cross_bound.finalization_hash,
                cross_bound.created_at,
            ),
        )
        self.store._connection.execute("PRAGMA foreign_keys = ON")  # noqa: SLF001
        self.store._connection.execute(  # noqa: SLF001 - restore canonical schema fixture
            """
            CREATE TRIGGER atlas_finalizations_require_projected_outbox
            BEFORE INSERT ON atlas_finalizations
            WHEN NOT EXISTS (
                SELECT 1 FROM atlas_ingestion_outbox AS outbox
                WHERE outbox.acceptance_id = NEW.acceptance_id
                  AND outbox.ingestion_key = NEW.ingestion_key
                  AND outbox.payload_hash = NEW.payload_hash
                  AND outbox.state = 'projected'
            )
            BEGIN
                SELECT RAISE(
                    ABORT, 'atlas finalization requires projected exact outbox'
                );
            END
            """
        )

        with self.assertRaises(StoreError):
            self.store._atlas_finalization_for_acceptance(acceptance_id)  # noqa: SLF001
        prepared = sqlite3.connect(self.database)
        try:
            with self.assertRaises(StoreError):
                SQLiteStore.from_prepared_connection(prepared)
        finally:
            prepared.close()

    def test_strict_binding_persists_only_an_opaque_workspace_authority(self) -> None:
        task = self.store.register_task(
            self._task("strict"),
            strict_index=True,
            workspace_id=self._WORKSPACE_ID,
            input_snapshot_id="sha256:input",
            task_node_ids=("sha256:task-node",),
        )

        binding = self.store.get_index_binding(task.id)
        row = self.store._connection.execute(
            "SELECT workspace_root, workspace_id FROM task_index_bindings WHERE task_id = ?",
            (task.id,),
        ).fetchone()

        self.assertEqual(self._WORKSPACE_ID, binding.workspace_id)
        self.assertFalse(hasattr(binding, "workspace_root"))
        self.assertEqual("", row["workspace_root"])
        self.assertEqual(self._WORKSPACE_ID, row["workspace_id"])

    def test_store_rejects_path_alias_and_invalid_workspace_id_before_writes(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            self.store.register_task(
                self._task("path-alias"),
                strict_index=True,
                workspace_root="D:/workspace",
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )
        with self.assertRaises(TypeError):
            self.store.register_task(
                self._task("workspace-alias"),
                strict_index=True,
                workspace="D:/workspace",
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )
        for task_id, workspace_id in (
            ("invalid-id", "D:/workspace"),
            ("path-value", self.database.parent),
        ):
            with self.subTest(workspace_id=workspace_id):
                with self.assertRaises(StrictIndexError) as invalid:
                    self.store.register_task(
                        self._task(task_id),
                        strict_index=True,
                        workspace_id=workspace_id,  # type: ignore[arg-type]
                        input_snapshot_id="sha256:input",
                        task_node_ids=("sha256:task-node",),
                    )
                self.assertEqual("INDEX_UNAVAILABLE", invalid.exception.code)

        for task_id in ("path-alias", "workspace-alias", "invalid-id", "path-value"):
            with self.assertRaises(KeyError):
                self.store.get_task(task_id)
            self.assertIsNone(self.store.get_index_binding(task_id))

    def test_reopen_preserves_workflows_tasks_and_append_only_event_order(self) -> None:
        self.store.register_task(self._task("one"))
        first = self.store.append_event(
            self.workflow.id, "one", "registered", "{}", "hash-1"
        )
        second = self.store.append_event(
            self.workflow.id, "one", "ready", "{}", "hash-2"
        )
        self.store.close()
        self.store = SQLiteStore(self.database)

        self.assertEqual(self.store.get_workflow(self.workflow.id), self.workflow)
        self.assertEqual(self.store.get_task("one"), self._task("one"))
        self.assertEqual(
            [event.sequence for event in self.store.list_events(self.workflow.id)],
            [first.sequence, second.sequence],
        )

    def test_dependencies_are_unique_and_readable(self) -> None:
        self.store.register_task(self._task("one"))
        self.store.register_task(self._task("two"), dependencies=("one", "one"))

        self.assertEqual(self.store.dependencies_for("two"), ("one",))

    def test_artifact_content_hash_is_reused(self) -> None:
        first = self.store.put_artifact("log", "content-hash", "safe/log.txt", 3, "v1")
        second = self.store.put_artifact(
            "log", "content-hash", "safe/other.txt", 99, "v2"
        )

        self.assertEqual(second, first)

    def test_task_version_compare_and_swap_rejects_stale_writer(self) -> None:
        self.store.register_task(self._task("one"))
        updated = self.store.update_task_state(
            "one", TaskState.READY, expected_version=0
        )
        self.assertEqual(updated.version, 1)
        with self.assertRaises(VersionConflictError):
            self.store.update_task_state("one", TaskState.RUNNING, expected_version=0)

    def test_leases_renew_and_take_over_with_monotonic_epochs(self) -> None:
        self.store.register_task(self._task("one"))
        initial = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:10:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        renewed = self.store.renew_lease(
            "one",
            "terra-a",
            initial.epoch,
            "2026-07-24T00:20:00+00:00",
            now="2026-07-24T00:01:00+00:00",
        )
        takeover = self.store.acquire_lease(
            "one",
            "terra-b",
            "2026-07-24T00:30:00+00:00",
            now="2026-07-24T00:21:00+00:00",
        )

        self.assertEqual((initial.epoch, renewed.epoch, takeover.epoch), (1, 1, 2))
        self.assertEqual(takeover.owner, "terra-b")

    def test_same_owner_reclaim_after_expiry_receives_new_epoch(self) -> None:
        self.store.register_task(self._task("one"))
        initial = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:01:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        reclaimed = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:03:00+00:00",
            now="2026-07-24T00:02:00+00:00",
        )

        self.assertEqual((initial.epoch, reclaimed.epoch), (1, 2))

    def test_expired_lease_cannot_complete_before_takeover(self) -> None:
        self.store.register_task(self._task("one"))
        lease = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:01:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        with self.assertRaises(StaleLeaseError) as raised:
            self.store.complete_task(
                "one",
                TaskState.DONE,
                0,
                "terra-a",
                lease.epoch,
                now="2026-07-24T00:02:00+00:00",
            )

        self.assertEqual(raised.exception.code, "STALE_LEASE")
        self.assertEqual(self.store.get_task("one").state, TaskState.NEW)

    def test_stale_owner_receives_stale_lease_code(self) -> None:
        self.store.register_task(self._task("one"))
        first = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:01:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        takeover = self.store.acquire_lease(
            "one",
            "terra-b",
            "2026-07-24T00:03:00+00:00",
            now="2026-07-24T00:02:00+00:00",
        )

        with self.assertRaises(StaleLeaseError) as raised:
            self.store.complete_task("one", TaskState.DONE, 0, "terra-a", first.epoch)
        self.assertEqual(raised.exception.code, "STALE_LEASE")
        completed = self.store.complete_task(
            "one",
            TaskState.DONE,
            0,
            "terra-b",
            takeover.epoch,
            now="2026-07-24T00:02:30+00:00",
        )
        self.assertEqual(completed.state, TaskState.DONE)


if __name__ == "__main__":
    unittest.main()
