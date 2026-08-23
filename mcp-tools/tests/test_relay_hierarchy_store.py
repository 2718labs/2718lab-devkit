"""Durable Scheduler Topology V1 coverage at the RelayStore boundary."""

from __future__ import annotations

import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.service import RelayService
from devkit_relay.store import RelayStore, RelayStoreError
from devkit_runtime.relay_runtime import RelayRuntime


class _NoDeliveryBroker:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def is_available(self) -> bool:
        return True

    def prepare_capability(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("migrated historical replay must not redeliver")


def _task(task_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "kind": "implementation",
        "stage": "a1_writer",
        "title": task_id,
        "objective": task_id,
        "priority": 50,
        "dependencies": [],
        "write_scope": [{"path": f"mcp-tools/{task_id}.py", "kind": "file"}],
        "route": {
            "route_class": "terra_max",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
        },
        "constraints": [],
        "acceptance_criteria": [],
        "atlas_packet_ids": [],
        "required_evidence": [],
        "design_for_task_id": None,
        "prewarm_for_task_id": None,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
        "split_policy": None,
        "split_parent_task_id": None,
        "split_depth": 0,
        "split_verdict": None,
    }


def _plan() -> dict[str, object]:
    tasks = [_task(f"writer-{index}") for index in range(4)]
    plan: dict[str, object] = {
        "schema": "2718lab-devkit/relay-plan-v3",
        "workflow_id": "topology-store-v1",
        "workspace_binding": {
            "workspace_id": "sha256:" + "b" * 64,
            "input_snapshot_id": "sha256:" + "c" * 64,
            "atlas_packet_ids": [],
        },
        "project_binding": {
            "schema": "2718lab-devkit/project-binding-v1",
            "mode": "indexed",
        },
        "base_commit": "d" * 40,
        "capacity": 3,
        "runtime_policy_id": "2718lab-devkit/relay-runtime-policy-v1",
        "tasks": tasks,
        "dependencies": [],
        "conflicts": [],
        "queues": {
            "writer_ready": [task["task_id"] for task in tasks],
            "design_ready": [],
            "prewarm_ready": [],
            "bootstrap_index": [],
            "review_integration": [],
            "terminal": [],
            "unsplittable": [],
        },
        "scheduler_topology": {
            "schema": "2718lab-devkit/scheduler-topology-v1",
            "max_writers_per_scheduler": 3,
            "max_parallel_writers": 9,
            "groups": [
                {
                    "scheduler_id": "scheduler-alpha",
                    "coordinator_lease_id": "lease-alpha",
                    "worktree_identity": "worktree-alpha",
                    "writer_task_ids": ["writer-0", "writer-1", "writer-2"],
                    "prewarm_task_ids": [],
                },
                {
                    "scheduler_id": "scheduler-beta",
                    "coordinator_lease_id": "lease-beta",
                    "worktree_identity": "worktree-beta",
                    "writer_task_ids": ["writer-3"],
                    "prewarm_task_ids": [],
                },
            ],
        },
    }
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def _downgrade_to_legacy(database: Path, version: str) -> None:
    if version not in {"5", "6", "7", "8"}:
        raise AssertionError("unsupported legacy fixture")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE IF EXISTS relay_v3_start_delivery_journal")
    connection.execute("DROP TABLE relay_v3_start_attempts")
    if version != "8":
        connection.execute(
            "CREATE TEMP TABLE relay_v3_runs_migration AS SELECT * FROM relay_v3_runs"
        )
        connection.execute("DROP TABLE relay_v3_runs")
        connection.execute(
            """
            CREATE TABLE relay_v3_runs (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL UNIQUE,
                plan_hash TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                input_snapshot_id TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                integration_head TEXT NOT NULL,
                integration_version INTEGER NOT NULL CHECK (integration_version >= 0),
                capacity INTEGER NOT NULL CHECK (typeof(capacity) = 'integer')
                    CHECK (capacity BETWEEN 1 AND 3),
                schedule_version INTEGER NOT NULL CHECK (schedule_version >= 0),
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO relay_v3_runs SELECT * FROM relay_v3_runs_migration"
        )
        connection.execute("DROP TABLE relay_v3_runs_migration")
        connection.execute("DROP TABLE relay_v3_finalization_outcomes")
        connection.execute("DROP TABLE relay_v3_finalization_journal")
        connection.execute(
            """
            CREATE TABLE relay_v3_finalization_journal (
                finalization_id TEXT PRIMARY KEY,
                reservation_epoch INTEGER NOT NULL CHECK (reservation_epoch >= 1),
                integration_proof_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                expectation_key TEXT NOT NULL,
                expectation_version INTEGER NOT NULL CHECK (expectation_version >= 1),
                expectation_hash TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                base_oid TEXT NOT NULL,
                final_oid TEXT NOT NULL,
                fence_hash TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('prepared', 'committed', 'aborted')),
                result_hash TEXT,
                journal_version INTEGER NOT NULL CHECK (journal_version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (state = 'committed' AND result_hash IS NOT NULL)
                    OR (state IN ('prepared', 'aborted') AND result_hash IS NULL)
                ),
                UNIQUE (integration_proof_id, reservation_epoch)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE relay_v3_finalization_outcomes (
                finalization_id TEXT PRIMARY KEY,
                fence_hash TEXT NOT NULL UNIQUE,
                integration_proof_id TEXT NOT NULL,
                expectation_key TEXT NOT NULL,
                expectation_version INTEGER NOT NULL CHECK (expectation_version >= 1),
                expectation_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX relay_v3_finalizations_by_proof
            ON relay_v3_finalization_journal(
                integration_proof_id, expectation_hash, state
            )
            """
        )
    if version in {"5", "6"}:
        connection.execute("DROP INDEX relay_v3_scheduler_slots_by_group")
        connection.execute("DROP TABLE relay_v3_scheduler_writer_slots")
        connection.execute("DROP TABLE relay_v3_scheduler_groups")
    if version == "5":
        connection.execute("DROP INDEX relay_v3_cleanup_by_run_state")
        connection.execute("DROP TABLE relay_v3_cleanup_ledger")
    connection.execute(
        "UPDATE relay_v3_schema_metadata SET value = ? WHERE key = 'schema_version'",
        (version,),
    )
    connection.commit()
    connection.close()


def _schema_rows(database: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return list(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        )
    finally:
        connection.close()


def _plan_with_prepared_prewarm() -> dict[str, object]:
    submitted = deepcopy(_plan())
    prewarm = _task("prewarm-0")
    prewarm.update(
        {
            "kind": "prewarm",
            "stage": "a3_prewarm",
            "write_scope": [],
            "route": {
                "route_class": "luna_medium",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
            },
            "prewarm_for_task_id": "writer-0",
        }
    )
    tasks = submitted["tasks"]
    assert isinstance(tasks, list)
    tasks.append(prewarm)
    tasks.sort(key=lambda item: str(item["task_id"]))
    topology = submitted["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["prewarm_task_ids"] = ["prewarm-0"]
    submitted["plan_hash"] = canonical_hash(
        {key: value for key, value in submitted.items() if key != "plan_hash"}
    )
    return submitted


def test_start_attempt_abort_is_idempotent_and_restores_writer_prewarm_semantics(
    tmp_path: Path,
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    created = store.start_create(
        _plan_with_prepared_prewarm(), idempotency_key="abort-prepared-start"
    )

    assert set(created) == {
        "schema",
        "workflow_id",
        "run_id",
        "schedule_version",
        "host_actions",
    }
    assert (
        store.start_create(
            _plan_with_prepared_prewarm(), idempotency_key="abort-prepared-start"
        )
        == created
    )
    attempt = store.start_attempt("abort-prepared-start")
    assert attempt["state"] == "prepared"

    receipt = store.abort_start_attempt(
        str(attempt["attempt_id"]),
        error_code="RELAY_HOST_ACTION_REJECTED",
    )
    fingerprint = store.database_fingerprint()

    assert (
        store.abort_start_attempt(
            str(attempt["attempt_id"]),
            error_code="RELAY_HOST_ACTION_REJECTED",
        )
        == receipt
    )
    assert store.database_fingerprint() == fingerprint
    status = store.status("topology-store-v1")
    states = {str(task["task_id"]): task["state"] for task in status["tasks"]}
    assert states["writer-0"] == "ready"
    assert states["prewarm-0"] == "prepared"
    assert status["outstanding_action_ids"] == []
    assert all(lease["state"] != "active" for lease in status["leases"])
    with pytest.raises(RelayStoreError, match="RELAY_STATE_STALE"):
        store.mark_start_admitted(str(attempt["attempt_id"]))


def test_admitted_and_delivered_start_attempts_cannot_be_aborted(
    tmp_path: Path,
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    created = store.start_create(_plan(), idempotency_key="admitted-start")
    attempt = store.start_attempt("admitted-start")
    actions = created["host_actions"]
    assert isinstance(actions, list)
    facts = []
    for action in actions:
        assert isinstance(action, dict)
        lease = action["lease"]
        assert isinstance(lease, dict)
        facts.append(
            {
                "action_id": action["action_id"],
                "workflow_id": action["workflow_id"],
                "task_id": action["task_id"],
                "epoch": lease["epoch"],
                "endpoint": f"bridge/{action['action_id']}",
                "issued_at": 2_000_000_000,
                "expires_at": 2_000_003_600,
                "key_id": "sha256:" + "8" * 64,
                "bundle_hash": "sha256:" + "9" * 64,
            }
        )

    admitted = store.initialize_start_delivery(str(attempt["attempt_id"]), facts)
    assert admitted["state"] == "admitted"
    with pytest.raises(RelayStoreError, match="RELAY_STATE_STALE"):
        store.abort_start_attempt(
            str(attempt["attempt_id"]), error_code="RELAY_HOST_ACTION_REJECTED"
        )

    for fact in facts:
        store.record_start_action_delivery(
            str(attempt["attempt_id"]),
            str(fact["action_id"]),
            {
                "action_id": fact["action_id"],
                "endpoint": fact["endpoint"],
                "bundle_hash": fact["bundle_hash"],
            },
        )
    delivered = store.mark_start_delivered(str(attempt["attempt_id"]))
    assert delivered["state"] == "delivered"
    with pytest.raises(RelayStoreError, match="RELAY_STATE_STALE"):
        store.abort_start_attempt(
            str(attempt["attempt_id"]), error_code="RELAY_HOST_ACTION_REJECTED"
        )


def test_schema_eight_migrates_start_attempt_journal_in_one_open(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay.sqlite3"
    seeded = RelayStore(database)
    seeded.start_create(_plan(), idempotency_key="v8-journal-seed")
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE IF EXISTS relay_v3_start_delivery_journal")
    connection.execute("DROP TABLE relay_v3_start_attempts")
    connection.execute(
        "UPDATE relay_v3_schema_metadata SET value = '8' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()

    migrated = RelayStore(database)

    assert migrated.status("topology-store-v1")["workflow_id"] == "topology-store-v1"
    assert (
        migrated._require_connection()
        .execute(
            "SELECT value FROM relay_v3_schema_metadata WHERE key = 'schema_version'"
        )
        .fetchone()[0]
        == "10"
    )
    assert (
        migrated._require_connection()
        .execute("SELECT COUNT(*) FROM relay_v3_start_attempts")
        .fetchone()[0]
        == 1
    )


def test_v8_shape_drift_fails_before_start_attempt_migration_mutates_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE relay_v3_start_delivery_journal")
    connection.execute("DROP TABLE relay_v3_start_attempts")
    connection.execute("ALTER TABLE relay_v3_actions ADD COLUMN forged TEXT")
    connection.execute(
        "UPDATE relay_v3_schema_metadata SET value = '8' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore(database)

    unchanged = sqlite3.connect(database)
    assert (
        unchanged.execute(
            "SELECT value FROM relay_v3_schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        == "8"
    )
    assert (
        unchanged.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'relay_v3_start_attempts'"
        ).fetchone()[0]
        == 0
    )
    unchanged.close()


def test_v10_missing_start_attempt_table_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE relay_v3_start_attempts")
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore(database)

    unchanged = sqlite3.connect(database)
    assert (
        unchanged.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'relay_v3_start_attempts'"
        ).fetchone()[0]
        == 0
    )
    unchanged.close()


def test_v10_start_attempt_constraint_drift_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "relay.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE relay_v3_start_attempts")
    connection.execute(
        """
        CREATE TABLE relay_v3_start_attempts (
            attempt_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            run_id TEXT NOT NULL,
            action_ids_json TEXT NOT NULL,
            compensation_json TEXT NOT NULL,
            state TEXT NOT NULL,
            error_code TEXT,
            receipt_json TEXT,
            attempt_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (idempotency_key)
                REFERENCES relay_v3_idempotency(idempotency_key)
                ON DELETE RESTRICT,
            FOREIGN KEY (run_id) REFERENCES relay_v3_runs(run_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore(database)


def test_start_create_persists_topology_groups_and_writer_slots(tmp_path: Path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")

    created = store.start_create(_plan(), idempotency_key="topology-create")

    connection = store._require_connection()
    groups = [
        tuple(row)
        for row in connection.execute(
            """
            SELECT scheduler_id, coordinator_lease_id, worktree_identity,
                   prewarm_task_ids_json
            FROM relay_v3_scheduler_groups
            ORDER BY scheduler_id
            """
        )
    ]
    slots = [
        tuple(row)
        for row in connection.execute(
            """
            SELECT scheduler_id, task_id, slot
            FROM relay_v3_scheduler_writer_slots
            ORDER BY scheduler_id, slot
            """
        )
    ]

    assert created["workflow_id"] == "topology-store-v1"
    assert groups == [
        ("scheduler-alpha", "lease-alpha", "worktree-alpha", "[]"),
        ("scheduler-beta", "lease-beta", "worktree-beta", "[]"),
    ]
    assert slots == [
        ("scheduler-alpha", "writer-0", 1),
        ("scheduler-alpha", "writer-1", 2),
        ("scheduler-alpha", "writer-2", 3),
        ("scheduler-beta", "writer-3", 1),
    ]


def test_start_create_rejects_raw_worktree_path_without_mutation(
    tmp_path: Path,
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    malformed = _plan()
    topology = malformed["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["worktree_identity"] = "G:/raw/worktree"
    before = store.database_fingerprint()

    with pytest.raises(RelayStoreError) as raised:
        store.start_create(malformed, idempotency_key="topology-raw-path")

    assert raised.value.code == "RELAY_TOPOLOGY_INVALID"
    assert store.database_fingerprint() == before


def test_start_create_rejects_topology_not_bound_into_plan_hash(tmp_path: Path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    malformed = _plan()
    topology = malformed["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["coordinator_lease_id"] = "lease-rebound"

    with pytest.raises(RelayStoreError) as raised:
        store.start_create(malformed, idempotency_key="topology-hash")

    assert raised.value.code == "RELAY_TOPOLOGY_INVALID"


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "relay.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE relay_v3_schema_metadata SET value = '11' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError) as raised:
        RelayStore.open_readwrite(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"


@pytest.mark.parametrize("old_version", ["5", "6", "7", "8", "9"])
def test_real_legacy_schema_versions_migrate_atomically_to_ten_in_rw_factory(
    tmp_path: Path, old_version: str
) -> None:
    database = tmp_path / f"relay-{old_version}.sqlite3"
    seeded = RelayStore(database)
    seeded.start_create(_plan(), idempotency_key=f"legacy-{old_version}-seed")
    seeded.close()
    if old_version != "9":
        _downgrade_to_legacy(database, old_version)

    store = RelayStore.open_readwrite(database)

    assert store.status("topology-store-v1")["workflow_id"] == "topology-store-v1"
    assert (
        store._require_connection()
        .execute(
            "SELECT value FROM relay_v3_schema_metadata WHERE key = 'schema_version'"
        )
        .fetchone()[0]
        == "10"
    )
    assert (
        store._require_connection()
        .execute("SELECT COUNT(*) FROM relay_v3_start_attempts")
        .fetchone()[0]
        == 1
    )
    store.close()


def test_real_v8_historical_idempotency_key_replays_after_v10_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay-v8-replay.sqlite3"
    request = {
        "mode": "create",
        "plan": _plan(),
        "idempotency_key": "historical-v8-replay",
    }
    seeded_store = RelayStore(database)
    seeded_relay = RelayService(
        seeded_store, capability_secret=b"historical-replay-secret"
    )
    expected = seeded_relay.start(request)
    seeded_store.close()
    _downgrade_to_legacy(database, "8")

    migrated = RelayStore.open_readwrite(database)
    broker = _NoDeliveryBroker()
    replayed = RelayRuntime(
        RelayService(migrated, capability_secret=b"historical-replay-secret"),
        capability_broker=broker,
    ).start(request)

    assert replayed == expected
    assert broker.calls == 0
    assert migrated.start_attempt("historical-v8-replay")["state"] == "delivered"


@pytest.mark.parametrize("claimed_version", ["5", "6", "7", "8", "9", "10"])
def test_metadata_only_claims_fail_closed_without_creating_authority_objects(
    tmp_path: Path, claimed_version: str
) -> None:
    database = tmp_path / f"metadata-only-{claimed_version}.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE relay_v3_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO relay_v3_schema_metadata (key, value) VALUES ('schema_version', ?)",
        (claimed_version,),
    )
    connection.commit()
    connection.close()
    before = _schema_rows(database)

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore.open_readwrite(database)

    assert _schema_rows(database) == before


@pytest.mark.parametrize("claimed_version", ["5", "6", "7", "8", "9", "10"])
def test_each_schema_version_rejects_extra_authority_before_migration_ddl(
    tmp_path: Path, claimed_version: str
) -> None:
    database = tmp_path / f"extra-authority-{claimed_version}.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    if claimed_version not in {"9", "10"}:
        _downgrade_to_legacy(database, claimed_version)
    elif claimed_version == "9":
        connection = sqlite3.connect(database)
        connection.execute("DROP TABLE IF EXISTS relay_v3_start_delivery_journal")
        connection.execute(
            "UPDATE relay_v3_schema_metadata SET value = '9' "
            "WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE VIEW relay_v3_forged_authority AS SELECT run_id FROM relay_v3_runs"
    )
    connection.commit()
    connection.close()
    before = _schema_rows(database)

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore.open_readwrite(database)

    assert _schema_rows(database) == before


@pytest.mark.parametrize(
    "extra_ddl",
    [
        "CREATE INDEX relay_v3_forged_index ON relay_v3_runs(workflow_id)",
        "CREATE VIEW relay_v3_forged_view AS SELECT run_id FROM relay_v3_runs",
        """
        CREATE TRIGGER relay_v3_forged_trigger AFTER INSERT ON relay_v3_runs
        BEGIN SELECT 1; END
        """,
    ],
)
def test_v9_rejects_extra_index_view_or_trigger(tmp_path: Path, extra_ddl: str) -> None:
    database = tmp_path / "extra-v9-authority.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute(extra_ddl)
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore.open_readwrite(database)


def test_v9_rejects_index_ddl_drift_and_extra_metadata(tmp_path: Path) -> None:
    database = tmp_path / "v9-index-drift.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX relay_v3_tasks_by_state")
    connection.execute(
        "CREATE INDEX relay_v3_tasks_by_state ON relay_v3_tasks(run_id, state)"
    )
    connection.execute(
        "INSERT INTO relay_v3_schema_metadata (key, value) VALUES ('forged', '9')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError, match="RELAY_SCHEMA_INCOMPATIBLE"):
        RelayStore.open_readwrite(database)


def test_pretend_v8_with_legacy_capacity_check_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "pretend-v8.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE relay_v3_runs_v7 (
            run_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL UNIQUE,
            plan_hash TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            input_snapshot_id TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            integration_head TEXT NOT NULL,
            integration_version INTEGER NOT NULL CHECK (integration_version >= 0),
            capacity INTEGER NOT NULL CHECK (typeof(capacity) = 'integer')
                CHECK (capacity BETWEEN 1 AND 3),
            schedule_version INTEGER NOT NULL CHECK (schedule_version >= 0),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO relay_v3_runs_v7 SELECT * FROM relay_v3_runs")
    connection.execute("DROP TABLE relay_v3_runs")
    connection.execute("ALTER TABLE relay_v3_runs_v7 RENAME TO relay_v3_runs")
    connection.execute(
        "UPDATE relay_v3_schema_metadata SET value = '8' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError) as raised:
        RelayStore(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"


def test_v8_capacity_check_comment_cannot_forge_schema_compatibility(
    tmp_path: Path,
) -> None:
    database = tmp_path / "comment-forged-capacity.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE relay_v3_runs_forged (
            run_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL UNIQUE,
            plan_hash TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            input_snapshot_id TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            integration_head TEXT NOT NULL,
            integration_version INTEGER NOT NULL CHECK (integration_version >= 0),
            capacity INTEGER NOT NULL CHECK (typeof(capacity) = 'integer')
                /* CHECK (capacity BETWEEN 1 AND 9) */,
            schedule_version INTEGER NOT NULL CHECK (schedule_version >= 0),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO relay_v3_runs_forged SELECT * FROM relay_v3_runs")
    connection.execute("DROP TABLE relay_v3_runs")
    connection.execute("ALTER TABLE relay_v3_runs_forged RENAME TO relay_v3_runs")
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError) as raised:
        RelayStore(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"


@pytest.mark.parametrize(
    ("table", "schema"),
    [
        pytest.param(
            "relay_v3_finalization_journal",
            """
            finalization_id TEXT PRIMARY KEY,
            reservation_epoch INTEGER NOT NULL CHECK (reservation_epoch >= 1),
            integration_proof_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            expectation_key TEXT NOT NULL,
            expectation_version INTEGER NOT NULL CHECK (expectation_version >= 1),
            expectation_hash TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            final_oid TEXT NOT NULL,
            fence_hash TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK (state IN ('prepared', 'committed', 'aborted')),
            result_hash TEXT,
            journal_version INTEGER NOT NULL CHECK (journal_version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (state = 'committed' AND result_hash IS NOT NULL)
                OR (state IN ('prepared', 'aborted') AND result_hash IS NULL)
            ),
            UNIQUE (integration_proof_id, reservation_epoch)
            """,
            id="journal",
        ),
        pytest.param(
            "relay_v3_finalization_outcomes",
            """
            finalization_id TEXT PRIMARY KEY,
            fence_hash TEXT NOT NULL UNIQUE,
            integration_proof_id TEXT NOT NULL,
            expectation_key TEXT NOT NULL,
            expectation_version INTEGER NOT NULL CHECK (expectation_version >= 1),
            expectation_hash TEXT NOT NULL,
            result_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
            """,
            id="outcomes",
        ),
    ],
)
def test_v8_missing_finalization_foreign_key_fails_closed(
    tmp_path: Path, table: str, schema: str
) -> None:
    database = tmp_path / f"missing-foreign-key-{table}.sqlite3"
    seeded = RelayStore(database)
    seeded.close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    forged_table = f"{table}_forged"
    connection.execute(f"CREATE TABLE {forged_table} ({schema})")
    connection.execute(f"INSERT INTO {forged_table} SELECT * FROM {table}")
    connection.execute(f"DROP TABLE {table}")
    connection.execute(f"ALTER TABLE {forged_table} RENAME TO {table}")
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError) as raised:
        RelayStore(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"


def test_v8_foreign_key_violation_fails_closed_during_constructor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "foreign-key-v8.sqlite3"
    store = RelayStore(database)
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    relay.start_create(_plan(), idempotency_key="foreign-key-seed")
    store.close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM relay_v3_runs")
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError) as raised:
        RelayStore(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"


def test_schema_seven_migrates_capacity_check_before_real_service_store_create(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay.sqlite3"
    seeded = RelayStore(database)
    seeded_relay = RelayService(seeded, capability_secret=b"hierarchy-test-secret")
    seeded_relay.start_create(_plan(), idempotency_key="seeded-v7-capacity-three")
    seeded.close()
    _downgrade_to_legacy(database, "7")

    store = RelayStore.open_readwrite(database)
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    assert store.status("topology-store-v1")["run"]["capacity"] == 3
    plan = _plan()
    plan["workflow_id"] = "topology-store-v1-capacity-four"
    plan["capacity"] = 4
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )

    relay.start_create(plan, idempotency_key="migrated-capacity-four")

    assert store.status("topology-store-v1-capacity-four")["run"]["capacity"] == 4


def test_real_service_to_store_never_dispatches_a_prewarm_for_unsplittable_writer(
    tmp_path: Path,
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    plan = _plan()
    tasks = plan["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["split_verdict"] = "UNSPLITTABLE_SCOPE_CONFLICT"
    prewarm = _task("prewarm-blocked")
    prewarm.update(
        {
            "kind": "prewarm",
            "stage": "a3_prewarm",
            "write_scope": [],
            "route": {
                "route_class": "luna_medium",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
            },
            "prewarm_for_task_id": "writer-0",
        }
    )
    tasks.append(prewarm)
    tasks.sort(key=lambda task: str(task["task_id"]))
    topology = plan["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["writer_task_ids"] = ["writer-0", "writer-1", "writer-2"]
    queues = plan["queues"]
    assert isinstance(queues, dict)
    queues["writer_ready"] = ["writer-0", "writer-1", "writer-2", "writer-3"]
    queues["prewarm_ready"] = []
    queues["unsplittable"] = ["writer-0"]
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )

    created = relay.start_create(plan, idempotency_key="unsplittable-prewarm")

    assert {action["task_id"] for action in created["host_actions"]} == {
        "writer-0",
        "writer-1",
        "writer-2",
    }


def test_unsplittable_writers_keep_slots_and_are_serialized_by_scope(
    tmp_path: Path,
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    plan = _plan()
    tasks = plan["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["split_verdict"] = "UNSPLITTABLE_SCOPE_CONFLICT"
    tasks[1]["split_verdict"] = "UNSPLITTABLE_SCOPE_CONFLICT"
    tasks[0]["write_scope"] = [{"path": "mcp-tools/shared.py", "kind": "file"}]
    tasks[1]["write_scope"] = [{"path": "mcp-tools/shared.py", "kind": "file"}]
    prewarm = _task("prewarm-withheld")
    prewarm.update(
        {
            "kind": "prewarm",
            "stage": "a3_prewarm",
            "write_scope": [],
            "route": {
                "route_class": "luna_medium",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
            },
            "prewarm_for_task_id": "writer-0",
        }
    )
    tasks.append(prewarm)
    tasks.sort(key=lambda task: str(task["task_id"]))
    topology = plan["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["writer_task_ids"] = ["writer-0", "writer-1", "writer-2"]
    groups[0]["prewarm_task_ids"] = []
    queues = plan["queues"]
    assert isinstance(queues, dict)
    queues["writer_ready"] = ["writer-0", "writer-1", "writer-2", "writer-3"]
    queues["prewarm_ready"] = []
    queues["unsplittable"] = ["writer-0", "writer-1"]
    plan["conflicts"] = [
        {
            "from_task_id": "writer-0",
            "kind": "write_scope_conflict",
            "to_task_id": "writer-1",
        }
    ]
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )

    created = relay.start_create(plan, idempotency_key="unsplittable-serial")

    assert [action["task_id"] for action in created["host_actions"]] == [
        "writer-0",
        "writer-2",
        "writer-3",
    ]
    slots = [
        tuple(row)
        for row in store._require_connection().execute(
            """
            SELECT task_id, slot FROM relay_v3_scheduler_writer_slots
            WHERE scheduler_id = 'scheduler-alpha' ORDER BY slot
            """
        )
    ]
    assert slots == [("writer-0", 1), ("writer-1", 2), ("writer-2", 3)]


def test_writer_budget_does_not_consume_the_bounded_reader_budget(
    tmp_path: Path,
) -> None:
    store = RelayStore(
        tmp_path / "relay.sqlite3", host_writer_capacity=1, host_reader_capacity=1
    )
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    plan = _plan()
    plan["capacity"] = 1
    tasks = plan["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["priority"] = 100
    first_prewarm = _task("prewarm-reader-one")
    second_prewarm = _task("prewarm-reader-two")
    for prewarm, target, priority in (
        (first_prewarm, "writer-0", 90),
        (second_prewarm, "writer-1", 80),
    ):
        prewarm.update(
            {
                "kind": "prewarm",
                "stage": "a3_prewarm",
                "priority": priority,
                "write_scope": [],
                "route": {
                    "route_class": "luna_medium",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "medium",
                },
                "prewarm_for_task_id": target,
            }
        )
        tasks.append(prewarm)
    tasks.sort(key=lambda task: str(task["task_id"]))
    topology = plan["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["prewarm_task_ids"] = [
        "prewarm-reader-one",
        "prewarm-reader-two",
    ]
    queues = plan["queues"]
    assert isinstance(queues, dict)
    queues["prewarm_ready"] = ["prewarm-reader-one", "prewarm-reader-two"]
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )

    created = relay.start_create(plan, idempotency_key="bounded-reader-budget")

    assert [action["task_id"] for action in created["host_actions"]] == [
        "writer-0",
        "prewarm-reader-one",
    ]


def test_real_service_to_store_emits_a_read_only_slot_for_group_bound_prewarm(
    tmp_path: Path,
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    plan = _plan()
    tasks = plan["tasks"]
    assert isinstance(tasks, list)
    prewarm = _task("prewarm-readonly")
    prewarm.update(
        {
            "kind": "prewarm",
            "stage": "a3_prewarm",
            "write_scope": [],
            "route": {
                "route_class": "luna_medium",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
            },
            "prewarm_for_task_id": "writer-0",
        }
    )
    tasks.append(prewarm)
    tasks.sort(key=lambda task: str(task["task_id"]))
    topology = plan["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["prewarm_task_ids"] = ["prewarm-readonly"]
    queues = plan["queues"]
    assert isinstance(queues, dict)
    queues["prewarm_ready"] = ["prewarm-readonly"]
    plan["capacity"] = 5
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )

    created = relay.start_create(plan, idempotency_key="read-only-prewarm")
    action = next(
        item
        for item in created["host_actions"]
        if item["task_id"] == "prewarm-readonly"
    )

    assert "host_scheduler_topology" not in action
    assert set(action["relay_host_scheduler_slot"]) == {
        "schema",
        "plan_hash",
        "topology_hash",
        "group_binding_hash",
        "scheduler_id",
        "coordinator_lease_id",
        "worktree_identity",
        "writer_slot",
        "read_only",
    }
    assert action["relay_host_scheduler_slot"]["writer_slot"] is None
    assert action["relay_host_scheduler_slot"]["read_only"] is True


@pytest.mark.parametrize("capacity", range(4, 10))
def test_real_service_to_store_persists_full_v3_capacity_range_and_emits_bound_slot(
    tmp_path: Path, capacity: int
) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    plan = _plan()
    plan["capacity"] = capacity
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )

    created = relay.start_create(plan, idempotency_key="host-fence")

    assert len(created["host_actions"]) == 4
    action = next(
        item for item in created["host_actions"] if item["task_id"] == "writer-0"
    )
    assert "host_scheduler_topology" not in action
    slot = action["relay_host_scheduler_slot"]
    assert slot == {
        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
        "plan_hash": plan["plan_hash"],
        "topology_hash": canonical_hash(plan["scheduler_topology"]),
        "group_binding_hash": canonical_hash(
            {
                "scheduler_id": "scheduler-alpha",
                "coordinator_lease_id": "lease-alpha",
                "worktree_identity": "worktree-alpha",
                "writer_task_ids": ["writer-0", "writer-1", "writer-2"],
                "prewarm_task_ids": [],
            }
        ),
        "scheduler_id": "scheduler-alpha",
        "coordinator_lease_id": "lease-alpha",
        "worktree_identity": "worktree-alpha",
        "writer_slot": 1,
        "read_only": False,
    }
    assert store.status("topology-store-v1")["run"]["capacity"] == capacity
