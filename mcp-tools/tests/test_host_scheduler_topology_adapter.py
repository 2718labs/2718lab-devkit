"""Private Host reconstruction from one Relay scheduler slot."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_runtime.fastlane_host_intent import (
    parse_host_scheduler_topology_projection,
    parse_relay_host_scheduler_slot,
)


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _slot() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
        "plan_hash": _hash("plan"),
        "topology_hash": _hash("topology"),
        "group_binding_hash": _hash("group"),
        "scheduler_id": "scheduler-a",
        "coordinator_lease_id": "lease-a",
        "worktree_identity": "worktree-a",
        "writer_slot": 1,
        "read_only": False,
    }


def _fact(
    adapter: object,
    *,
    scheduler_id: str,
    group_binding_hash: str,
    writer_task_ids: tuple[str, ...],
    prewarm_task_ids: tuple[str, ...] = (),
) -> object:
    return adapter.HostSchedulerTopologyFact(
        plan_hash=_hash("plan"),
        topology_hash=_hash("topology"),
        group_binding_hash=group_binding_hash,
        scheduler_id=scheduler_id,
        coordinator_lease_id=f"lease-{scheduler_id}",
        worktree_identity=f"wt-{scheduler_id}",
        writer_task_ids=writer_task_ids,
        prewarm_task_ids=prewarm_task_ids,
        attested_capacity=3,
        attestation_hash=_hash(f"attestation-{scheduler_id}"),
    )


def test_private_adapter_constructs_topology_only_from_bound_slot_and_host_fact() -> None:
    from devkit_runtime import host_scheduler_topology_adapter as adapter

    parsed = parse_relay_host_scheduler_slot(_slot())
    fact = adapter.HostSchedulerTopologyFact(
        plan_hash=_hash("plan"),
        topology_hash=_hash("topology"),
        group_binding_hash=_hash("group"),
        scheduler_id="scheduler-a",
        coordinator_lease_id="lease-a",
        worktree_identity="worktree-a",
        writer_task_ids=("writer-a",),
        prewarm_task_ids=("prewarm-a",),
        attested_capacity=1,
        attestation_hash=_hash("attestation"),
    )

    topology = adapter.construct_host_scheduler_topology(parsed, (fact,))
    projection = parse_host_scheduler_topology_projection(topology)

    assert projection.schema == "2718lab-devkit/host-scheduler-topology-v1"
    assert projection.relay_plan_hash == _hash("plan")
    assert projection.relay_topology_hash == _hash("topology")
    assert projection.groups[0].relay_group_binding_hash == _hash("group")
    assert projection.groups[0].attested_capacity == 1
    assert projection.groups[0].attestation_hash == _hash("attestation")


@pytest.mark.parametrize("field", ["plan_hash", "topology_hash", "group_binding_hash"])
def test_adapter_rejects_any_rebound_relay_hash_mismatch(field: str) -> None:
    from devkit_runtime import host_scheduler_topology_adapter as adapter

    parsed = parse_relay_host_scheduler_slot(_slot())
    fact = _fact(
        adapter,
        scheduler_id="scheduler-a",
        group_binding_hash=_hash("group"),
        writer_task_ids=("writer-a",),
        prewarm_task_ids=("prewarm-a",),
    )
    fact = replace(
        fact,
        coordinator_lease_id="lease-a",
        worktree_identity="worktree-a",
    )

    assert (
        adapter.construct_host_scheduler_topology(
            parsed,
            (replace(fact, **{field: _hash(f"rebound-{field}")}),),
        )
        == adapter.NO_SAFE_WORK
    )


def test_prewarm_slot_is_read_only_and_does_not_raise_the_nine_writer_limit() -> None:
    from devkit_runtime import host_scheduler_topology_adapter as adapter

    slot = {
        **_slot(),
        "coordinator_lease_id": "lease-scheduler-a",
        "worktree_identity": "wt-scheduler-a",
        "writer_slot": None,
        "read_only": True,
    }
    parsed = parse_relay_host_scheduler_slot(slot)
    facts = (
        _fact(
            adapter,
            scheduler_id="scheduler-a",
            group_binding_hash=_hash("group"),
            writer_task_ids=("writer-a-1", "writer-a-2", "writer-a-3"),
            prewarm_task_ids=("prewarm-a",),
        ),
        _fact(
            adapter,
            scheduler_id="scheduler-b",
            group_binding_hash=_hash("group-b"),
            writer_task_ids=("writer-b-1", "writer-b-2", "writer-b-3"),
        ),
        _fact(
            adapter,
            scheduler_id="scheduler-c",
            group_binding_hash=_hash("group-c"),
            writer_task_ids=("writer-c-1", "writer-c-2", "writer-c-3"),
        ),
    )

    topology = adapter.construct_host_scheduler_topology(parsed, facts)
    projection = parse_host_scheduler_topology_projection(topology)

    assert projection is not adapter.NO_SAFE_WORK
    assert sum(len(group.writer_task_ids) for group in projection.groups) == 9
    assert projection.groups[0].prewarm_task_ids == ("prewarm-a",)


def test_adapter_rejects_aggregate_topology_with_more_than_nine_writers() -> None:
    from devkit_runtime import host_scheduler_topology_adapter as adapter

    slot = {
        **_slot(),
        "coordinator_lease_id": "lease-scheduler-a",
        "worktree_identity": "wt-scheduler-a",
    }
    parsed = parse_relay_host_scheduler_slot(slot)
    facts = (
        _fact(
            adapter,
            scheduler_id="scheduler-a",
            group_binding_hash=_hash("group"),
            writer_task_ids=("writer-a-1", "writer-a-2", "writer-a-3"),
        ),
        _fact(
            adapter,
            scheduler_id="scheduler-b",
            group_binding_hash=_hash("group-b"),
            writer_task_ids=("writer-b-1", "writer-b-2", "writer-b-3"),
        ),
        _fact(
            adapter,
            scheduler_id="scheduler-c",
            group_binding_hash=_hash("group-c"),
            writer_task_ids=("writer-c-1", "writer-c-2", "writer-c-3"),
        ),
        _fact(
            adapter,
            scheduler_id="scheduler-d",
            group_binding_hash=_hash("group-d"),
            writer_task_ids=("writer-d-1",),
        ),
    )

    assert (
        adapter.construct_host_scheduler_topology(parsed, facts) == adapter.NO_SAFE_WORK
    )


def test_adapter_rejects_cross_group_duplicate_design_task_id() -> None:
    from devkit_runtime import host_scheduler_topology_adapter as adapter

    slot = {
        **_slot(),
        "coordinator_lease_id": "lease-scheduler-a",
        "worktree_identity": "wt-scheduler-a",
        "writer_slot": None,
        "read_only": True,
    }
    parsed = parse_relay_host_scheduler_slot(slot)
    facts = (
        adapter.HostSchedulerTopologyFact(
            plan_hash=_hash("plan"),
            topology_hash=_hash("topology"),
            group_binding_hash=_hash("group"),
            scheduler_id="scheduler-a",
            coordinator_lease_id="lease-scheduler-a",
            worktree_identity="wt-scheduler-a",
            writer_task_ids=("writer-a",),
            prewarm_task_ids=(),
            attested_capacity=1,
            attestation_hash=_hash("attestation-a"),
            authoritative_actions=(
                adapter.HostAuthoritativeActionFact(
                    plan_hash=_hash("plan"),
                    task_id="design-shared",
                    kind="design",
                    target="writer-a",
                    group_binding_hash=_hash("group"),
                ),
            ),
        ),
        adapter.HostSchedulerTopologyFact(
            plan_hash=_hash("plan"),
            topology_hash=_hash("topology"),
            group_binding_hash=_hash("group-b"),
            scheduler_id="scheduler-b",
            coordinator_lease_id="lease-scheduler-b",
            worktree_identity="wt-scheduler-b",
            writer_task_ids=("writer-b",),
            prewarm_task_ids=(),
            attested_capacity=1,
            attestation_hash=_hash("attestation-b"),
            authoritative_actions=(
                adapter.HostAuthoritativeActionFact(
                    plan_hash=_hash("plan"),
                    task_id="design-shared",
                    kind="design",
                    target="writer-b",
                    group_binding_hash=_hash("group-b"),
                ),
            ),
        ),
    )

    assert adapter.construct_host_scheduler_topology(parsed, facts) == adapter.NO_SAFE_WORK
