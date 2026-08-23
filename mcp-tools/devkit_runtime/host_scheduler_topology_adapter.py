"""Host-private reconstruction of scheduler topology from one Relay slot.

Relay may provide only its bound scheduler slot.  Capacity, attestation, and
the aggregate writer count are independently supplied by the Host resolver and
are never accepted from Relay material.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

from .fastlane_host_intent import ParsedRelayHostSchedulerSlot

NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HOST_TOPOLOGY_SCHEMA: Final = "2718lab-devkit/host-scheduler-topology-v1"
_HASH: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_OPAQUE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class HostSchedulerTopologyFact:
    """Complete private Host fact for one Relay scheduler group."""

    plan_hash: str
    topology_hash: str
    group_binding_hash: str
    scheduler_id: str
    coordinator_lease_id: str
    worktree_identity: str
    writer_task_ids: tuple[str, ...]
    prewarm_task_ids: tuple[str, ...]
    attested_capacity: int
    attestation_hash: str


def construct_host_scheduler_topology(
    slot: object,
    facts: object,
) -> dict[str, object] | str:
    """Build a Host-only V1 shape after every slot/fact binding agrees."""

    if type(slot) is not ParsedRelayHostSchedulerSlot or not _slot_is_valid(slot):
        return NO_SAFE_WORK
    normalized_facts = _normalized_facts(facts)
    if normalized_facts is None:
        return NO_SAFE_WORK
    target = _matching_fact(slot, normalized_facts)
    if target is None or not _slot_is_admissible(slot, target):
        return NO_SAFE_WORK
    groups = [_host_group(fact) for fact in normalized_facts]
    return _with_binding(
        {
            "schema": _HOST_TOPOLOGY_SCHEMA,
            "relay_plan_hash": target.plan_hash,
            "relay_topology_hash": target.topology_hash,
            "groups": groups,
        },
        "projection_hash",
    )


def _normalized_facts(
    value: object,
) -> tuple[HostSchedulerTopologyFact, ...] | None:
    if type(value) is not tuple or not 1 <= len(value) <= 9:
        return None
    if any(type(fact) is not HostSchedulerTopologyFact for fact in value):
        return None
    facts = tuple(value)
    if not all(_fact_is_valid(fact) for fact in facts):
        return None
    plan_hashes = {fact.plan_hash for fact in facts}
    topology_hashes = {fact.topology_hash for fact in facts}
    group_identities = {
        (fact.scheduler_id, fact.coordinator_lease_id, fact.worktree_identity)
        for fact in facts
    }
    writer_ids = [writer for fact in facts for writer in fact.writer_task_ids]
    if (
        len(plan_hashes) != 1
        or len(topology_hashes) != 1
        or len(group_identities) != len(facts)
        or len(set(writer_ids)) != len(writer_ids)
        or len(writer_ids) > 9
    ):
        return None
    return tuple(sorted(facts, key=lambda fact: fact.scheduler_id))


def _slot_is_valid(slot: ParsedRelayHostSchedulerSlot) -> bool:
    return (
        slot.schema == "2718lab-devkit/relay-host-scheduler-slot-v1"
        and _is_hash(slot.plan_hash)
        and _is_hash(slot.topology_hash)
        and _is_hash(slot.group_binding_hash)
        and _is_identifier(slot.scheduler_id)
        and _is_opaque_id(slot.coordinator_lease_id)
        and _is_opaque_id(slot.worktree_identity)
        and type(slot.read_only) is bool
        and (
            (slot.read_only and slot.writer_slot is None)
            or (
                not slot.read_only
                and type(slot.writer_slot) is int
                and 1 <= slot.writer_slot <= 3
            )
        )
    )


def _fact_is_valid(fact: HostSchedulerTopologyFact) -> bool:
    writer_ids = fact.writer_task_ids
    prewarm_ids = fact.prewarm_task_ids
    return (
        _is_hash(fact.plan_hash)
        and _is_hash(fact.topology_hash)
        and _is_hash(fact.group_binding_hash)
        and _is_identifier(fact.scheduler_id)
        and _is_opaque_id(fact.coordinator_lease_id)
        and _is_opaque_id(fact.worktree_identity)
        and type(writer_ids) is tuple
        and 1 <= len(writer_ids) <= 3
        and all(_is_identifier(writer) for writer in writer_ids)
        and len(set(writer_ids)) == len(writer_ids)
        and type(prewarm_ids) is tuple
        and len(prewarm_ids) <= 16
        and all(_is_identifier(prewarm) for prewarm in prewarm_ids)
        and len(set(prewarm_ids)) == len(prewarm_ids)
        and not set(writer_ids).intersection(prewarm_ids)
        and type(fact.attested_capacity) is int
        and 1 <= fact.attested_capacity <= 3
        and len(writer_ids) <= fact.attested_capacity
        and _is_hash(fact.attestation_hash)
    )


def _slot_matches_fact(
    slot: ParsedRelayHostSchedulerSlot,
    fact: HostSchedulerTopologyFact,
) -> bool:
    return (
        slot.plan_hash == fact.plan_hash
        and slot.topology_hash == fact.topology_hash
        and slot.group_binding_hash == fact.group_binding_hash
        and slot.scheduler_id == fact.scheduler_id
        and slot.coordinator_lease_id == fact.coordinator_lease_id
        and slot.worktree_identity == fact.worktree_identity
    )


def _matching_fact(
    slot: ParsedRelayHostSchedulerSlot,
    facts: tuple[HostSchedulerTopologyFact, ...],
) -> HostSchedulerTopologyFact | None:
    matched = [fact for fact in facts if _slot_matches_fact(slot, fact)]
    return matched[0] if len(matched) == 1 else None


def _slot_is_admissible(
    slot: ParsedRelayHostSchedulerSlot,
    target: HostSchedulerTopologyFact,
) -> bool:
    if slot.read_only:
        return slot.writer_slot is None and bool(target.prewarm_task_ids)
    return (
        type(slot.writer_slot) is int
        and 1 <= slot.writer_slot <= len(target.writer_task_ids)
        and slot.writer_slot <= target.attested_capacity
    )


def _host_group(fact: HostSchedulerTopologyFact) -> dict[str, object]:
    return _with_binding(
        {
            "scheduler_id": fact.scheduler_id,
            "coordinator_lease_id": fact.coordinator_lease_id,
            "worktree_identity": fact.worktree_identity,
            "writer_task_ids": list(fact.writer_task_ids),
            "prewarm_task_ids": list(fact.prewarm_task_ids),
            "relay_group_binding_hash": fact.group_binding_hash,
            "attested_capacity": fact.attested_capacity,
            "attestation_hash": fact.attestation_hash,
        },
        "group_binding_hash",
    )


def _with_binding(value: dict[str, object], field: str) -> dict[str, object]:
    bound = dict(value)
    bound[field] = _canonical_hash(bound)
    return bound


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


def _is_identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _is_opaque_id(value: object) -> bool:
    return type(value) is str and _OPAQUE_ID.fullmatch(value) is not None


def _canonical_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
