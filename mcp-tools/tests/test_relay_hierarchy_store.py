"""Durable Scheduler Topology V1 coverage at the RelayStore boundary."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.store import RelayStore, RelayStoreError


def _task(task_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "kind": "implementation",
        "priority": 50,
        "dependencies": [],
        "write_scope": [{"path": f"mcp-tools/{task_id}.py", "kind": "file"}],
        "route": {
            "route_class": "terra_max",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
        },
    }


def _plan() -> dict[str, object]:
    tasks = [_task(f"writer-{index}") for index in range(4)]
    plan: dict[str, object] = {
        "workflow_id": "topology-store-v1",
        "workspace_binding": {
            "workspace_id": "sha256:" + "b" * 64,
            "input_snapshot_id": "sha256:" + "c" * 64,
        },
        "base_commit": "d" * 40,
        "capacity": 3,
        "tasks": tasks,
        "queues": {"ready": [task["task_id"] for task in tasks]},
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


def test_schema_six_migrates_to_seven_but_unknown_version_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE relay_v3_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO relay_v3_schema_metadata (key, value) VALUES ('schema_version', '6')"
    )
    connection.commit()
    connection.close()

    store = RelayStore(database)
    assert (
        store._require_connection()
        .execute(
            "SELECT value FROM relay_v3_schema_metadata WHERE key = 'schema_version'"
        )
        .fetchone()[0]
        == "7"
    )
    store.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE relay_v3_schema_metadata SET value = '8' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RelayStoreError) as raised:
        RelayStore(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"
