"""Private HostSession capability, lease, and compiler-evidence boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import devkit_runtime.host_session as host_session
from devkit_runtime.host_bridge import InheritedHandleHostBridge
from devkit_runtime.host_envelopes import (
    EnvelopeBinding,
    EnvelopeExpectation,
    render_envelope,
)

_NOW = 1_700_000_000
_HASH_PREFIX = "sha256:"


def _hash(value: object) -> str:
    return _HASH_PREFIX + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _pipe_pair() -> tuple[InheritedHandleHostBridge, InheritedHandleHostBridge]:
    child_to_host_read, child_to_host_write = os.pipe()
    host_to_child_read, host_to_child_write = os.pipe()
    key = b"k" * 32
    nonce = b"host-session-private-bridge-nonce"
    child = InheritedHandleHostBridge.from_file_descriptors(
        read_fd=host_to_child_read,
        write_fd=child_to_host_write,
        session_key=key,
        session_nonce=nonce,
    )
    host = InheritedHandleHostBridge.from_file_descriptors(
        read_fd=child_to_host_read,
        write_fd=host_to_child_write,
        session_key=key,
        session_nonce=nonce,
    )
    return child, host


def _binding() -> EnvelopeBinding:
    return EnvelopeBinding(
        task_id="task-1",
        lease_epoch=7,
        assignment_token=_HASH_PREFIX + "a" * 64,
        dispatch_context_hash=_HASH_PREFIX + "b" * 64,
        route_hash=_HASH_PREFIX + "c" * 64,
        expires_at=_NOW + 60,
    )


def _compiler_binding(
    *,
    request_hash: str = _HASH_PREFIX + "1" * 64,
    reasoning_effort: str = "high",
    route_hashes: tuple[str, ...] = (_HASH_PREFIX + "2" * 64,),
    lease_hashes: tuple[str, ...] = (_HASH_PREFIX + "3" * 64,),
    dispatch_facts: tuple[object, ...] = (),
    dispatch_binding_hashes: tuple[str, ...] = (),
) -> object:
    return host_session._CompilerInvocationBinding(
        request_hash=request_hash,
        reasoning_effort=reasoning_effort,
        verified_route_result_hashes=route_hashes,
        verified_lease_scope_bindings=lease_hashes,
        dispatch_facts=dispatch_facts,
        dispatch_binding_hashes=dispatch_binding_hashes,
    )


def _compiler_session(
    *,
    provider: object,
    resolver: object | None = None,
    topology_resolver: object | None = None,
    host_action_capacity_resolver: object | None = None,
    clock: object | None = None,
) -> tuple[host_session.HostSession, InheritedHandleHostBridge, InheritedHandleHostBridge]:
    child, host = _pipe_pair()
    session = host_session.HostSession(
        bridge=child,
        clock=(lambda: _NOW) if clock is None else clock,
        compiler_evidence_provider=provider,
        compiler_invocation_resolver=(
            (lambda _preparation_id: _compiler_binding())
            if resolver is None
            else resolver
        ),
        topology_fact_resolver=topology_resolver,
        host_action_capacity_resolver=host_action_capacity_resolver,
    )
    return session, child, host


def _with_binding(value: dict[str, object], field: str) -> dict[str, object]:
    bound = dict(value)
    bound.pop(field, None)
    bound[field] = _hash(bound)
    return bound


def _topology(*, capacities: tuple[int, int] = (2, 1)) -> object:
    relay_groups = []
    for scheduler_id, worktree_identity, writers, prewarm in (
        ("scheduler-a", "wt-a", ["writer-a"], ["prewarm-a"]),
        ("scheduler-b", "wt-b", ["writer-b"], []),
    ):
        relay_groups.append(
            _with_binding(
                {
                    "scheduler_id": scheduler_id,
                    "coordinator_lease_id": f"lease-{scheduler_id}",
                    "worktree_identity": worktree_identity,
                    "writer_task_ids": writers,
                    "prewarm_task_ids": prewarm,
                },
                "group_binding_hash",
            )
        )
    relay_topology = _with_binding(
        {
            "schema": "2718lab-devkit/scheduler-topology-v1",
            "plan_hash": _hash({"plan": "topology"}),
            "groups": relay_groups,
        },
        "topology_hash",
    )
    groups = []
    for relay_group, capacity, attestation in zip(
        relay_groups,
        capacities,
        (_hash({"attestation": "a"}), _hash({"attestation": "b"})),
        strict=True,
    ):
        groups.append(
            _with_binding(
                {
                    "scheduler_id": relay_group["scheduler_id"],
                    "coordinator_lease_id": relay_group["coordinator_lease_id"],
                    "worktree_identity": relay_group["worktree_identity"],
                    "writer_task_ids": relay_group["writer_task_ids"],
                    "prewarm_task_ids": relay_group["prewarm_task_ids"],
                    "relay_group_binding_hash": relay_group["group_binding_hash"],
                    "attested_capacity": capacity,
                    "attestation_hash": attestation,
                },
                "group_binding_hash",
            )
        )
    from devkit_runtime.fastlane_host_intent import (
        parse_host_scheduler_topology_projection,
    )

    return parse_host_scheduler_topology_projection(
        _with_binding(
            {
                "schema": "2718lab-devkit/host-scheduler-topology-v1",
                "relay_plan_hash": relay_topology["plan_hash"],
                "relay_topology_hash": relay_topology["topology_hash"],
                "groups": groups,
            },
            "projection_hash",
        )
    )


def test_topology_resolution_only_returns_attested_opaque_worktree_capacity() -> None:
    topology = _topology()

    def resolver(resolved_topology: object) -> object:
        return (
            host_session.HostTopologyGroupFact(
                plan_hash=resolved_topology.relay_plan_hash,
                topology_hash=resolved_topology.relay_topology_hash,
                group_binding_hash=resolved_topology.groups[0].relay_group_binding_hash,
                scheduler_id="scheduler-a",
                coordinator_lease_id="lease-scheduler-a",
                worktree_identity="wt-a",
                writer_task_ids=("writer-a",),
                prewarm_task_ids=("prewarm-a",),
                attested_capacity=2,
                attestation_hash=_hash({"attestation": "a"}),
            ),
            host_session.HostTopologyGroupFact(
                plan_hash=resolved_topology.relay_plan_hash,
                topology_hash=resolved_topology.relay_topology_hash,
                group_binding_hash=resolved_topology.groups[1].relay_group_binding_hash,
                scheduler_id="scheduler-b",
                coordinator_lease_id="lease-scheduler-b",
                worktree_identity="wt-b",
                writer_task_ids=("writer-b",),
                prewarm_task_ids=(),
                attested_capacity=1,
                attestation_hash=_hash({"attestation": "b"}),
            ),
        )

    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=resolver,
    )
    try:
        resolved = session.resolve_scheduler_topology(topology)
    finally:
        child.close()
        host.close()

    assert isinstance(resolved, host_session.HostResolvedSchedulerTopology)
    assert resolved.schema == "2718lab-devkit/host-scheduler-topology-v1"
    assert resolved.relay_plan_hash.startswith(_HASH_PREFIX)
    assert resolved.relay_topology_hash.startswith(_HASH_PREFIX)
    assert resolved.projection_hash.startswith(_HASH_PREFIX)
    assert resolved.groups[0].worktree_identity == "wt-a"
    assert resolved.groups[0].attested_capacity == 2
    assert resolved.groups[0].writer_task_ids == ("writer-a",)
    assert resolved.groups[0].prewarm_task_ids == ("prewarm-a",)
    assert resolved.groups[0].relay_group_binding_hash.startswith(_HASH_PREFIX)
    assert resolved.groups[0].group_binding_hash.startswith(_HASH_PREFIX)
    assert resolved.audit_binding_hash.startswith(_HASH_PREFIX)
    assert "path" not in repr(resolved).lower()
    assert "quota" not in repr(resolved).lower()
    assert "model" not in repr(resolved).lower()


def test_topology_resolution_does_not_spend_writer_capacity_on_prewarm() -> None:
    topology = _topology(capacities=(1, 1))
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=lambda resolved_topology: (
        host_session.HostTopologyGroupFact(
            plan_hash=resolved_topology.relay_plan_hash,
            topology_hash=resolved_topology.relay_topology_hash,
            group_binding_hash=resolved_topology.groups[0].relay_group_binding_hash,
            scheduler_id="scheduler-a",
            coordinator_lease_id="lease-scheduler-a",
            worktree_identity="wt-a",
            writer_task_ids=("writer-a",),
            prewarm_task_ids=("prewarm-a",),
            attested_capacity=1,
            attestation_hash=_hash({"attestation": "a"}),
        ),
        host_session.HostTopologyGroupFact(
            plan_hash=resolved_topology.relay_plan_hash,
            topology_hash=resolved_topology.relay_topology_hash,
            group_binding_hash=resolved_topology.groups[1].relay_group_binding_hash,
            scheduler_id="scheduler-b",
            coordinator_lease_id="lease-scheduler-b",
            worktree_identity="wt-b",
            writer_task_ids=("writer-b",),
            prewarm_task_ids=(),
            attested_capacity=1,
            attestation_hash=_hash({"attestation": "b"}),
        ),
        ),
    )
    try:
        resolved = session.resolve_scheduler_topology(topology)
    finally:
        child.close()
        host.close()

    assert isinstance(resolved, host_session.HostResolvedSchedulerTopology)
    assert resolved.groups[0].attested_capacity == 1
    assert resolved.groups[0].prewarm_task_ids == ("prewarm-a",)
    assert not hasattr(resolved.groups[0], "writer_lease")


def test_host_session_resolves_relay_slot_through_private_aggregate_adapter() -> None:
    slot = {
        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
        "plan_hash": _hash("plan"),
        "topology_hash": _hash("topology"),
        "group_binding_hash": _hash("group-a"),
        "scheduler_id": "scheduler-a",
        "coordinator_lease_id": "lease-a",
        "worktree_identity": "wt-a",
        "writer_slot": 1,
        "read_only": False,
    }
    facts = (
        host_session.HostTopologyGroupFact(
            plan_hash=_hash("plan"),
            topology_hash=_hash("topology"),
            group_binding_hash=_hash("group-a"),
            scheduler_id="scheduler-a",
            coordinator_lease_id="lease-a",
            worktree_identity="wt-a",
            writer_task_ids=("writer-a",),
            prewarm_task_ids=(),
            attested_capacity=1,
            attestation_hash=_hash("attestation-a"),
            authoritative_actions=(
                host_session.HostAuthoritativeActionFact(
                    plan_hash=_hash("plan"),
                    task_id="writer-a",
                    kind="implementation",
                    target="writer-a",
                    group_binding_hash=_hash("group-a"),
                ),
                host_session.HostAuthoritativeActionFact(
                    plan_hash=_hash("plan"),
                    task_id="design-a",
                    kind="design",
                    target="writer-a",
                    group_binding_hash=_hash("group-a"),
                ),
            ),
        ),
    )
    capacity = {"writer_capacity": 1, "reader_capacity": 1}
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=lambda _slot: facts,
        host_action_capacity_resolver=lambda: capacity,
    )
    try:
        resolved = session.resolve_relay_host_scheduler_slot(slot)
        design_slot = {**slot, "writer_slot": None, "read_only": True}
        actions = [
            {
                "kind": "codex.spawn_agent",
                "task_id": "writer-a",
                "task_contract": {
                    "task_id": "writer-a",
                    "kind": "implementation",
                },
                "relay_host_scheduler_slot": slot,
            },
            {
                "kind": "codex.spawn_agent",
                "task_id": "design-a",
                "task_contract": {
                    "task_id": "design-a",
                    "kind": "design",
                    "design_for_task_id": "writer-a",
                },
                "relay_host_scheduler_slot": design_slot,
            },
        ]
        invalid_slot = {**slot, "writer_slot": 2}
        task_swap = {
            **actions[0],
            "task_contract": {"task_id": "prewarm-a", "kind": "prewarm"},
        }
        kind_swap = {
            **actions[0],
            "task_contract": {"task_id": "writer-a", "kind": "prewarm"},
        }
        assert session.admit_relay_actions(actions) is True
        assert (
            session.admit_relay_actions(
                [{**actions[0], "relay_host_scheduler_slot": invalid_slot}]
            )
            is False
        )
        assert session.admit_relay_actions([task_swap]) is False
        assert session.admit_relay_actions([kind_swap]) is False
        for field in ("plan_hash", "topology_hash", "group_binding_hash"):
            assert (
                session.admit_relay_actions(
                    [{**actions[0], "relay_host_scheduler_slot": {**slot, field: _hash(field)}}]
                )
                is False
            )
        capacity["writer_capacity"] = 0
        assert session.admit_relay_actions(actions) is False
    finally:
        child.close()
        host.close()

    assert isinstance(resolved, host_session.HostResolvedSchedulerTopology)
    assert resolved.relay_plan_hash == _hash("plan")
    assert resolved.relay_topology_hash == _hash("topology")
    assert resolved.groups[0].relay_group_binding_hash == _hash("group-a")
    assert resolved.groups[0].attested_capacity == 1


def test_host_session_rejects_paired_design_id_swap_against_authoritative_actions() -> None:
    slot = {
        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
        "plan_hash": _hash("plan"),
        "topology_hash": _hash("topology"),
        "group_binding_hash": _hash("group-a"),
        "scheduler_id": "scheduler-a",
        "coordinator_lease_id": "lease-a",
        "worktree_identity": "wt-a",
        "writer_slot": None,
        "read_only": True,
    }
    facts = (
        host_session.HostTopologyGroupFact(
            plan_hash=_hash("plan"),
            topology_hash=_hash("topology"),
            group_binding_hash=_hash("group-a"),
            scheduler_id="scheduler-a",
            coordinator_lease_id="lease-a",
            worktree_identity="wt-a",
            writer_task_ids=("writer-a", "writer-b"),
            prewarm_task_ids=(),
            attested_capacity=2,
            attestation_hash=_hash("attestation-a"),
            authoritative_actions=(
                host_session.HostAuthoritativeActionFact(
                    plan_hash=_hash("plan"),
                    task_id="design-a",
                    kind="design",
                    target="writer-a",
                    group_binding_hash=_hash("group-a"),
                ),
                host_session.HostAuthoritativeActionFact(
                    plan_hash=_hash("plan"),
                    task_id="design-b",
                    kind="design",
                    target="writer-b",
                    group_binding_hash=_hash("group-a"),
                ),
            ),
        ),
    )
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=lambda _slot: facts,
        host_action_capacity_resolver=lambda: {
            "writer_capacity": 0,
            "reader_capacity": 2,
        },
    )
    try:
        authoritative_designs = [
            {
                "kind": "codex.spawn_agent",
                "task_id": "design-a",
                "task_contract": {
                    "task_id": "design-a",
                    "kind": "design",
                    "design_for_task_id": "writer-a",
                },
                "relay_host_scheduler_slot": slot,
            },
            {
                "kind": "codex.spawn_agent",
                "task_id": "design-b",
                "task_contract": {
                    "task_id": "design-b",
                    "kind": "design",
                    "design_for_task_id": "writer-b",
                },
                "relay_host_scheduler_slot": slot,
            },
        ]
        swapped_designs = [
            {
                "kind": "codex.spawn_agent",
                "task_id": "design-a",
                "task_contract": {
                    "task_id": "design-a",
                    "kind": "design",
                    "design_for_task_id": "writer-b",
                },
                "relay_host_scheduler_slot": slot,
            },
            {
                "kind": "codex.spawn_agent",
                "task_id": "design-b",
                "task_contract": {
                    "task_id": "design-b",
                    "kind": "design",
                    "design_for_task_id": "writer-a",
                },
                "relay_host_scheduler_slot": slot,
            },
        ]

        assert session.admit_relay_actions(authoritative_designs) is True
        assert session.admit_relay_actions(swapped_designs) is False
    finally:
        child.close()
        host.close()


def test_host_session_resolves_changing_authoritative_facts_once_per_batch() -> None:
    slot = {
        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
        "plan_hash": _hash("plan"),
        "topology_hash": _hash("topology"),
        "group_binding_hash": _hash("group-a"),
        "scheduler_id": "scheduler-a",
        "coordinator_lease_id": "lease-a",
        "worktree_identity": "wt-a",
        "writer_slot": None,
        "read_only": True,
    }

    def facts(
        *, design_a_target: str, design_b_target: str
    ) -> tuple[host_session.HostTopologyGroupFact, ...]:
        return (
            host_session.HostTopologyGroupFact(
                plan_hash=_hash("plan"),
                topology_hash=_hash("topology"),
                group_binding_hash=_hash("group-a"),
                scheduler_id="scheduler-a",
                coordinator_lease_id="lease-a",
                worktree_identity="wt-a",
                writer_task_ids=("writer-a", "writer-b"),
                prewarm_task_ids=(),
                attested_capacity=2,
                attestation_hash=_hash("attestation-a"),
                authoritative_actions=(
                    host_session.HostAuthoritativeActionFact(
                        plan_hash=_hash("plan"),
                        task_id="design-a",
                        kind="design",
                        target=design_a_target,
                        group_binding_hash=_hash("group-a"),
                    ),
                    host_session.HostAuthoritativeActionFact(
                        plan_hash=_hash("plan"),
                        task_id="design-b",
                        kind="design",
                        target=design_b_target,
                        group_binding_hash=_hash("group-a"),
                    ),
                ),
            ),
        )

    calls = 0

    def resolver(_slot: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return facts(design_a_target="writer-a", design_b_target="writer-a")
        return facts(design_a_target="writer-b", design_b_target="writer-b")

    actions = [
        {
            "kind": "codex.spawn_agent",
            "task_id": "design-a",
            "task_contract": {
                "task_id": "design-a",
                "kind": "design",
                "design_for_task_id": "writer-a",
            },
            "relay_host_scheduler_slot": slot,
        },
        {
            "kind": "codex.spawn_agent",
            "task_id": "design-b",
            "task_contract": {
                "task_id": "design-b",
                "kind": "design",
                "design_for_task_id": "writer-b",
            },
            "relay_host_scheduler_slot": slot,
        },
    ]
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=resolver,
        host_action_capacity_resolver=lambda: {
            "writer_capacity": 0,
            "reader_capacity": 2,
        },
    )
    try:
        assert session.admit_relay_actions(actions) is False
        assert calls == 1
    finally:
        child.close()
        host.close()


def test_host_session_binds_canonical_authoritative_actions_to_private_audit() -> None:
    slot = {
        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
        "plan_hash": _hash("plan"),
        "topology_hash": _hash("topology"),
        "group_binding_hash": _hash("group-a"),
        "scheduler_id": "scheduler-a",
        "coordinator_lease_id": "lease-a",
        "worktree_identity": "wt-a",
        "writer_slot": None,
        "read_only": True,
    }

    def resolve_audit(
        action_facts: tuple[host_session.HostAuthoritativeActionFact, ...],
    ) -> str:
        facts = (
            host_session.HostTopologyGroupFact(
                plan_hash=_hash("plan"),
                topology_hash=_hash("topology"),
                group_binding_hash=_hash("group-a"),
                scheduler_id="scheduler-a",
                coordinator_lease_id="lease-a",
                worktree_identity="wt-a",
                writer_task_ids=("writer-a", "writer-b"),
                prewarm_task_ids=(),
                attested_capacity=2,
                attestation_hash=_hash("attestation-a"),
                authoritative_actions=action_facts,
            ),
        )
        session, child, host = _compiler_session(
            provider=lambda preparation: preparation,
            topology_resolver=lambda _slot: facts,
        )
        try:
            resolved = session.resolve_relay_host_scheduler_slot(slot)
        finally:
            child.close()
            host.close()
        assert isinstance(resolved, host_session.HostResolvedSchedulerTopology)
        return resolved.audit_binding_hash

    action_a = host_session.HostAuthoritativeActionFact(
        plan_hash=_hash("plan"),
        task_id="design-a",
        kind="design",
        target="writer-a",
        group_binding_hash=_hash("group-a"),
    )
    action_b = host_session.HostAuthoritativeActionFact(
        plan_hash=_hash("plan"),
        task_id="design-b",
        kind="design",
        target="writer-b",
        group_binding_hash=_hash("group-a"),
    )
    changed_action_b = replace(action_b, target="writer-a")

    canonical_audit = resolve_audit((action_a, action_b))
    reordered_audit = resolve_audit((action_b, action_a))
    changed_audit = resolve_audit((action_a, changed_action_b))

    assert canonical_audit == reordered_audit
    assert canonical_audit != changed_audit


def test_host_session_applies_independent_writer_and_reader_capacity() -> None:
    plan_hash = _hash("capacity-plan")
    topology_hash = _hash("capacity-topology")
    facts: list[host_session.HostTopologyGroupFact] = []
    actions: list[dict[str, object]] = []
    for scheduler_id in ("scheduler-a", "scheduler-b", "scheduler-c"):
        group_hash = _hash(f"capacity-{scheduler_id}")
        writers = tuple(f"{scheduler_id}-writer-{index}" for index in range(1, 4))
        action_facts = [
            host_session.HostAuthoritativeActionFact(
                plan_hash=plan_hash,
                task_id=writer,
                kind="implementation",
                target=writer,
                group_binding_hash=group_hash,
            )
            for writer in writers
        ]
        if scheduler_id == "scheduler-a":
            action_facts.append(
                host_session.HostAuthoritativeActionFact(
                    plan_hash=plan_hash,
                    task_id="reader-a",
                    kind="design",
                    target=writers[0],
                    group_binding_hash=group_hash,
                )
            )
        facts.append(
            host_session.HostTopologyGroupFact(
                plan_hash=plan_hash,
                topology_hash=topology_hash,
                group_binding_hash=group_hash,
                scheduler_id=scheduler_id,
                coordinator_lease_id=f"lease-{scheduler_id}",
                worktree_identity=f"wt-{scheduler_id}",
                writer_task_ids=writers,
                prewarm_task_ids=(),
                attested_capacity=3,
                attestation_hash=_hash(f"attestation-{scheduler_id}"),
                authoritative_actions=tuple(action_facts),
            )
        )
        for writer_slot, writer in enumerate(writers, start=1):
            actions.append(
                {
                    "kind": "codex.spawn_agent",
                    "task_id": writer,
                    "task_contract": {
                        "task_id": writer,
                        "kind": "implementation",
                    },
                    "relay_host_scheduler_slot": {
                        "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
                        "plan_hash": plan_hash,
                        "topology_hash": topology_hash,
                        "group_binding_hash": group_hash,
                        "scheduler_id": scheduler_id,
                        "coordinator_lease_id": f"lease-{scheduler_id}",
                        "worktree_identity": f"wt-{scheduler_id}",
                        "writer_slot": writer_slot,
                        "read_only": False,
                    },
                }
            )
    actions.append(
        {
            "kind": "codex.spawn_agent",
            "task_id": "reader-a",
            "task_contract": {
                "task_id": "reader-a",
                "kind": "design",
                "design_for_task_id": "scheduler-a-writer-1",
            },
            "relay_host_scheduler_slot": {
                "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
                "plan_hash": plan_hash,
                "topology_hash": topology_hash,
                "group_binding_hash": _hash("capacity-scheduler-a"),
                "scheduler_id": "scheduler-a",
                "coordinator_lease_id": "lease-scheduler-a",
                "worktree_identity": "wt-scheduler-a",
                "writer_slot": None,
                "read_only": True,
            },
        }
    )
    capacity: dict[str, int] = {"writer_capacity": 9, "reader_capacity": 0}
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=lambda _slot: tuple(facts),
        host_action_capacity_resolver=lambda: capacity,
    )
    try:
        assert session.admit_relay_actions(actions) is False
        capacity["reader_capacity"] = 1
        assert session.admit_relay_actions(actions) is True
    finally:
        child.close()
        host.close()


@pytest.mark.parametrize("field", ["plan_hash", "topology_hash", "group_binding_hash"])
def test_host_session_rejects_each_private_relay_hash_mismatch(field: str) -> None:
    topology = _topology()

    def resolver(resolved_topology: object) -> object:
        facts = tuple(
            host_session.HostTopologyGroupFact(
                plan_hash=resolved_topology.relay_plan_hash,
                topology_hash=resolved_topology.relay_topology_hash,
                group_binding_hash=group.relay_group_binding_hash,
                scheduler_id=group.scheduler_id,
                coordinator_lease_id=group.coordinator_lease_id,
                worktree_identity=group.worktree_identity,
                writer_task_ids=group.writer_task_ids,
                prewarm_task_ids=group.prewarm_task_ids,
                attested_capacity=group.attested_capacity,
                attestation_hash=group.attestation_hash,
            )
            for group in resolved_topology.groups
        )
        return (replace(facts[0], **{field: _hash(f"rebound-{field}")}), *facts[1:])

    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        topology_resolver=resolver,
    )
    try:
        resolved = session.resolve_scheduler_topology(topology)
    finally:
        child.close()
        host.close()

    assert resolved == "NO_SAFE_WORK"


def test_host_session_missing_or_invalid_inherited_bridge_stays_unavailable(
    tmp_path: Path,
) -> None:
    missing = host_session.HostSession.from_environment(
        environ={}, platform="posix", clock=lambda: _NOW
    )
    invalid = host_session.HostSession.from_environment(
        environ={"CODEX_DEVKIT_HOST_BRIDGE_FD": "not-a-descriptor"},
        platform="posix",
        clock=lambda: _NOW,
    )
    regular = tmp_path / "not-private-ipc"
    descriptor = os.open(regular, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        file_backed = host_session.HostSession.from_environment(
            environ={"CODEX_DEVKIT_HOST_BRIDGE_FD": str(descriptor)},
            platform="posix",
            clock=lambda: _NOW,
        )
    finally:
        os.close(descriptor)

    assert missing.is_available is False
    assert invalid.is_available is False
    assert file_backed.is_available is False
    assert not hasattr(host_session.HostSession, "read_quota")


def test_declared_route_never_becomes_a_scheduling_fact() -> None:
    route = host_session.HostRoute(model="gpt-5.6-terra", effort="max")
    session = host_session.HostSession(bridge=None, clock=lambda: _NOW)

    declared = session.declare_routes((route,))

    assert declared[0].state is host_session.HostCapabilityState.DECLARED
    assert session.scheduling_facts(declared) == "NO_SAFE_WORK"


def test_host_session_exports_only_exact_bridge_attested_model_effort_pairs() -> None:
    child, host = _pipe_pair()
    route = host_session.HostRoute(model="gpt-5.6-terra", effort="max")
    binding = _binding()

    def reply() -> None:
        probe = host.receive_capability_probe(now=_NOW, expected=binding)
        host.send_capability_report(
            probe=probe,
            capability_hashes={
                name: _HASH_PREFIX + "f" * 64 for name in probe.capability_names
            },
            now=_NOW,
        )

    thread = threading.Thread(target=reply, daemon=True)
    thread.start()
    session = host_session.HostSession(bridge=child, clock=lambda: _NOW)
    try:
        facts = session.attest_routes(binding=binding, routes=(route,), now=_NOW)
        assert isinstance(facts, tuple)
        scheduled = session.scheduling_facts(facts)
        forged = replace(facts[0], route=replace(facts[0].route, effort="high"))
        rejected = session.scheduling_facts((forged,))
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()

    assert isinstance(scheduled, host_session.HostSchedulingFacts)
    assert scheduled.routes == (route,)
    assert scheduled.binding_hash.startswith(_HASH_PREFIX)
    assert rejected == "NO_SAFE_WORK"


def test_host_session_observes_authenticated_terminal_without_executing_work() -> None:
    child, host = _pipe_pair()
    route = host_session.HostRoute(model="gpt-5.6-terra", effort="max")
    binding = _binding()

    def capability_reply() -> None:
        probe = host.receive_capability_probe(now=_NOW, expected=binding)
        host.send_capability_report(
            probe=probe,
            capability_hashes={
                name: _HASH_PREFIX + "f" * 64 for name in probe.capability_names
            },
            now=_NOW,
        )

    thread = threading.Thread(target=capability_reply, daemon=True)
    thread.start()
    session = host_session.HostSession(bridge=child, clock=lambda: _NOW)
    assignment = render_envelope(
        kind="coordinator_assignment",
        binding=binding,
        payload={
            "correlation_id": "operation-1",
            "assignment": "assignment.verify",
            "context": ["context.artifact-refs"],
            "artifact_refs": [_HASH_PREFIX + "1" * 64],
            "digest_refs": [_HASH_PREFIX + "2" * 64],
        },
        now=_NOW,
    )
    try:
        facts = session.attest_routes(binding=binding, routes=(route,), now=_NOW)
        assert isinstance(facts, tuple)
        predecessor = child.send_operation(envelope=assignment, now=_NOW)
        received = host.receive_operation(
            now=_NOW,
            expected=EnvelopeExpectation(kind="coordinator_assignment", binding=binding),
        )
        terminal = render_envelope(
            kind="worker_terminal_result",
            binding=binding,
            payload={
                "correlation_id": "operation-1",
                "predecessor_hash": received.envelope_hash,
                "terminal": "succeeded",
                "result": ["result.verified"],
                "risk": [{"code": "none", "detail": "risk.none"}],
                "artifact_refs": [_HASH_PREFIX + "3" * 64],
                "digest_refs": [_HASH_PREFIX + "4" * 64],
            },
            now=_NOW,
        )
        host.send_terminal_result(envelope=terminal, predecessor=received, now=_NOW)
        executed = session.observe_execution(
            facts[0], predecessor=predecessor, now=_NOW
        )
        replayed = session.observe_execution(
            facts[0], predecessor=predecessor, now=_NOW
        )
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()

    assert isinstance(executed, host_session.HostCapabilityFact)
    assert executed.state is host_session.HostCapabilityState.EXECUTED
    assert replayed == "NO_SAFE_WORK"


@pytest.mark.parametrize(
    "terminal_binding",
    [
        replace(_binding(), task_id="task-2"),
        replace(_binding(), lease_epoch=8),
        replace(_binding(), assignment_token=_HASH_PREFIX + "d" * 64),
        replace(_binding(), dispatch_context_hash=_HASH_PREFIX + "e" * 64),
    ],
)
def test_host_session_rejects_execution_crossing_attested_lease_binding(
    terminal_binding: EnvelopeBinding,
) -> None:
    child, host = _pipe_pair()
    route = host_session.HostRoute(model="gpt-5.6-terra", effort="max")
    attested_binding = _binding()

    def capability_reply() -> None:
        probe = host.receive_capability_probe(now=_NOW, expected=attested_binding)
        host.send_capability_report(
            probe=probe,
            capability_hashes={
                name: _HASH_PREFIX + "f" * 64 for name in probe.capability_names
            },
            now=_NOW,
        )

    thread = threading.Thread(target=capability_reply, daemon=True)
    thread.start()
    session = host_session.HostSession(bridge=child, clock=lambda: _NOW)
    assignment = render_envelope(
        kind="coordinator_assignment",
        binding=terminal_binding,
        payload={
            "correlation_id": "operation-2",
            "assignment": "assignment.verify",
            "context": ["context.artifact-refs"],
            "artifact_refs": [_HASH_PREFIX + "1" * 64],
            "digest_refs": [_HASH_PREFIX + "2" * 64],
        },
        now=_NOW,
    )
    try:
        facts = session.attest_routes(
            binding=attested_binding, routes=(route,), now=_NOW
        )
        assert isinstance(facts, tuple)
        predecessor = child.send_operation(envelope=assignment, now=_NOW)
        host.receive_operation(
            now=_NOW,
            expected=EnvelopeExpectation(
                kind="coordinator_assignment", binding=terminal_binding
            ),
        )

        assert (
            session.observe_execution(facts[0], predecessor=predecessor, now=_NOW)
            == "NO_SAFE_WORK"
        )
        assert session.is_available is False
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()


def test_host_session_compiler_evidence_is_capability_and_lease_bound_without_quota() -> None:
    preparations: list[object] = []

    def provider(preparation: object) -> object:
        preparations.append(preparation)
        return preparation

    binding = _compiler_binding(
        request_hash=_HASH_PREFIX + "4" * 64,
        route_hashes=(_HASH_PREFIX + "5" * 64, _HASH_PREFIX + "6" * 64),
        lease_hashes=(_HASH_PREFIX + "7" * 64,),
    )
    session, child, host = _compiler_session(
        provider=provider, resolver=lambda _preparation_id: binding
    )
    try:
        handle = session.prepare_compiler_evidence(
            preparation_id="prep-capability-only"
        )
        invocation = session.consume_compiler_evidence(handle)
    finally:
        child.close()
        host.close()

    assert len(preparations) == 1
    assert invocation.schema == "2718lab-devkit/compiler-invocation-v2"
    assert invocation.request_hash == binding.request_hash
    assert invocation.verified_route_result_hashes == (
        _HASH_PREFIX + "5" * 64,
        _HASH_PREFIX + "6" * 64,
    )
    assert invocation.verified_lease_scope_bindings == (
        _HASH_PREFIX + "7" * 64,
    )
    assert invocation.issued_at == _NOW
    assert invocation.expires_at == _NOW + 120
    assert invocation.binding_hash == _hash(
        {
            "preparation_id": "prep-capability-only",
            "request_hash": binding.request_hash,
            "reasoning_effort": binding.reasoning_effort,
            "verified_route_result_hashes": binding.verified_route_result_hashes,
            "verified_lease_scope_bindings": binding.verified_lease_scope_bindings,
            "issued_at": invocation.issued_at,
            "expires_at": invocation.expires_at,
        }
    )
    assert "quota" not in repr(invocation).lower()
    assert session.consume_compiler_evidence(handle) == "NO_SAFE_WORK"


def test_compiler_invocation_binding_hash_binds_ordered_dispatch_hashes() -> None:
    dispatch_hashes = (_HASH_PREFIX + "4" * 64, _HASH_PREFIX + "5" * 64)
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        resolver=lambda _preparation_id: _compiler_binding(
            dispatch_facts=("fact-a", "fact-b"),
            dispatch_binding_hashes=dispatch_hashes,
        ),
    )
    try:
        handle = session.prepare_compiler_evidence(preparation_id="prep-dispatch")
        invocation = session.consume_compiler_evidence(handle)
    finally:
        child.close()
        host.close()

    assert invocation.dispatch_binding_hashes == dispatch_hashes
    assert invocation.binding_hash == _hash(
        {
            "preparation_id": "prep-dispatch",
            "request_hash": invocation.request_hash,
            "reasoning_effort": invocation.reasoning_effort,
            "verified_route_result_hashes": invocation.verified_route_result_hashes,
            "verified_lease_scope_bindings": invocation.verified_lease_scope_bindings,
            "issued_at": invocation.issued_at,
            "expires_at": invocation.expires_at,
            "dispatch_binding_hashes": dispatch_hashes,
        }
    )


def test_compiler_evidence_fails_closed_without_provider_or_binding() -> None:
    child, host = _pipe_pair()
    try:
        missing_provider = host_session.HostSession(
            bridge=child,
            clock=lambda: _NOW,
            compiler_invocation_resolver=lambda _preparation_id: _compiler_binding(),
        )
        assert (
            missing_provider.prepare_compiler_evidence(preparation_id="prep-missing")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()

    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        resolver=lambda _preparation_id: None,
    )
    try:
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-no-binding")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()


@pytest.mark.parametrize(
    "binding",
    [
        _compiler_binding(reasoning_effort="ultra"),
        _compiler_binding(request_hash="not-a-hash"),
        _compiler_binding(route_hashes=()),
        _compiler_binding(lease_hashes=(_HASH_PREFIX + "3" * 64,) * 2),
    ],
)
def test_compiler_evidence_rejects_invalid_capability_or_lease_binding(
    binding: object,
) -> None:
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        resolver=lambda _preparation_id: binding,
    )
    try:
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-invalid")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()


def test_compiler_evidence_burns_failed_preparation_and_rejects_public_material() -> None:
    calls: list[object] = []

    def provider(preparation: object) -> object:
        calls.append(preparation)
        return {"secret": "public-substitute"}

    session, child, host = _compiler_session(provider=provider)
    try:
        first = session.prepare_compiler_evidence(preparation_id="prep-burned")
        second = session.prepare_compiler_evidence(preparation_id="prep-burned")
    finally:
        child.close()
        host.close()

    assert first == second == "NO_SAFE_WORK"
    assert len(calls) == 1


def test_compiler_evidence_expires_and_clock_regression_fails_closed() -> None:
    now = [_NOW]
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation,
        clock=lambda: now[0],
    )
    try:
        handle = session.prepare_compiler_evidence(preparation_id="prep-expiring")
        now[0] = _NOW + 120
        assert session.consume_compiler_evidence(handle) == "NO_SAFE_WORK"

        now[0] = _NOW - 1
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-regressed")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()


def test_close_clears_unconsumed_compiler_evidence() -> None:
    session, child, host = _compiler_session(
        provider=lambda preparation: preparation
    )
    try:
        handle = session.prepare_compiler_evidence(preparation_id="prep-close")
        session.close()
        assert session.consume_compiler_evidence(handle) == "NO_SAFE_WORK"
    finally:
        child.close()
        host.close()
