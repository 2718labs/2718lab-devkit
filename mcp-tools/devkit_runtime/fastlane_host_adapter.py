"""Private, inert Fast Lane host-boundary adapter.

Only an opaque compiler-evidence handle issued by ``HostSession`` may cross
this boundary. Public capability claims remain insufficient to start work.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .host_session import (
    HostCapabilityFact,
    HostRoute,
    HostSchedulingFacts,
    HostSession,
    _CompilerInvocation,
)

NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HASH: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LABEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PATH_PART: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_TRANSFER_ROLES: Final = {
    "coordinator_to_worker": ("coordinator", "worker"),
    "worker_to_coordinator": ("worker", "coordinator"),
    "peer_handoff": ("peer", "peer"),
}
_SCHEDULER_ROLES: Final = frozenset(
    {"execution", "verification", "prewarm", "review", "design_probe"}
)
_DISPATCH_MODES: Final = frozenset({"parallel", "serial", "isolated_worktree"})
_DISPATCH_REQUEST_FIELDS: Final = frozenset({"schema", "action", "assignments"})
_DISPATCH_ASSIGNMENT_FIELDS: Final = frozenset(
    {
        "task_id",
        "route",
        "lease_id",
        "lease_epoch",
        "task_version",
        "assignment_token",
        "write_scope",
        "concurrency_mode",
        "dispatch_order",
        "index_context_hash",
        "worktree_identity",
        "worktree_base",
        "integration_head",
        "predecessor_hash",
        "source_plan_hash",
        "ledger_epoch",
        "active_lease_set_hash",
        "dispatch_binding_hash",
    }
)
_DISPATCH_ROUTE_FIELDS: Final = frozenset(
    {
        "model",
        "reasoning_effort",
        "routing_context_hash",
        "routing_result_hash",
        "require_explicit_route",
    }
)
_MAX_DISPATCH_REQUEST_BYTES: Final = 65_536


@dataclass(frozen=True)
class _HostDispatchRoute:
    model: str
    reasoning_effort: str
    routing_context_hash: str
    routing_result_hash: str
    require_explicit_route: bool


@dataclass(frozen=True)
class _HostDispatchFact:
    task_id: str
    route: _HostDispatchRoute
    lease_id: str
    lease_epoch: int
    task_version: int
    assignment_token: str
    write_scope: tuple[str, ...]
    concurrency_mode: str
    dispatch_order: int
    index_context_hash: str
    worktree_identity: str
    worktree_base: str
    integration_head: str
    predecessor_hash: str
    source_plan_hash: str
    ledger_epoch: int
    active_lease_set_hash: str


@dataclass(frozen=True)
class _PreparedHostFacts:
    session: HostSession
    evidence: object
    capability_facts: tuple[HostCapabilityFact, ...]


def prepare_verified_host_facts(
    session: object,
    *,
    capability_facts: Sequence[HostCapabilityFact] | object,
    preparation_id: object = None,
) -> _PreparedHostFacts | str:
    """Accept no public substitute for session-owned compiler evidence."""

    normalized_preparation_id = _label(preparation_id)
    if (
        type(session) is not HostSession
        or not isinstance(capability_facts, Sequence)
        or isinstance(capability_facts, (str, bytes, bytearray))
        or normalized_preparation_id is None
    ):
        return NO_SAFE_WORK
    try:
        scheduling = session.scheduling_facts(tuple(capability_facts))
        if type(scheduling) is not HostSchedulingFacts:
            return NO_SAFE_WORK
        evidence = session.prepare_compiler_evidence(
            preparation_id=normalized_preparation_id
        )
        if evidence == NO_SAFE_WORK:
            return NO_SAFE_WORK
        return _PreparedHostFacts(
            session=session,
            evidence=evidence,
            capability_facts=tuple(capability_facts),
        )
    except Exception:
        return NO_SAFE_WORK


def compile_fast_lane_with_host_facts(
    request: object,
    *,
    reasoning_effort: object,
    verified_host_facts: object,
) -> dict[str, object] | str:
    """Validate one entire trusted batch and emit an inert dispatch request."""

    if type(verified_host_facts) is not _PreparedHostFacts:
        return NO_SAFE_WORK
    prepared = verified_host_facts
    try:
        normalized_request = _dispatch_request(request)
        if _bounded_json_size(normalized_request) > _MAX_DISPATCH_REQUEST_BYTES:
            return NO_SAFE_WORK
        request_bytes = _canonical_bytes(normalized_request)
        if len(request_bytes) > _MAX_DISPATCH_REQUEST_BYTES:
            return NO_SAFE_WORK
        material = prepared.session.consume_compiler_evidence(prepared.evidence)
        if type(material) is not _CompilerInvocation:
            return NO_SAFE_WORK
        if (
            type(reasoning_effort) is not str
            or material.reasoning_effort != reasoning_effort
            or _hash_bytes(request_bytes) != material.request_hash
        ):
            return NO_SAFE_WORK
        facts = _normalized_dispatch_facts(material.dispatch_facts)
        fact_mappings = [_dispatch_fact_mapping(fact) for fact in facts]
        if normalized_request["assignments"] != fact_mappings:
            return NO_SAFE_WORK
        if tuple(sorted(fact.route.routing_result_hash for fact in facts)) != tuple(
            material.verified_route_result_hashes
        ):
            return NO_SAFE_WORK
        if tuple(sorted(_lease_scope_binding_hash(fact) for fact in facts)) != tuple(
            material.verified_lease_scope_bindings
        ):
            return NO_SAFE_WORK
        dispatch_binding_hashes = tuple(
            mapping["dispatch_binding_hash"] for mapping in fact_mappings
        )
        if dispatch_binding_hashes != material.dispatch_binding_hashes:
            return NO_SAFE_WORK
        scheduling = prepared.session.scheduling_facts(prepared.capability_facts)
        if type(scheduling) is not HostSchedulingFacts:
            return NO_SAFE_WORK
        attested_routes = set(scheduling.routes)
        if any(
            HostRoute(
                model=fact.route.model,
                effort=fact.route.reasoning_effort,
            )
            not in attested_routes
            for fact in facts
        ):
            return NO_SAFE_WORK
        _validate_batch_fences(facts)
        batch: dict[str, object] = {
            "schema": "2718lab-devkit/fastlane-host-dispatch-batch-v1",
            "action": "dispatch_all",
            "selection_authority": "host_attested_compiler",
            "llm_choice": False,
            "source_plan_hash": facts[0].source_plan_hash,
            "ledger_epoch": facts[0].ledger_epoch,
            "active_lease_set_hash": facts[0].active_lease_set_hash,
            "dispatch_binding_hashes": list(dispatch_binding_hashes),
            "assignments": fact_mappings,
        }
        batch["batch_hash"] = _canonical_hash(batch)
        return batch
    except Exception:
        return NO_SAFE_WORK


def project_role_transfer(
    *,
    kind: object,
    task_id: object,
    role: object,
    assignment_token: object,
    context_hash: object,
    summary_hash: object,
    artifact_hashes: object,
    digest_hashes: object,
) -> dict[str, object] | str:
    """Project one bounded, hash-only role handoff without sending its body."""

    roles = _TRANSFER_ROLES.get(kind) if type(kind) is str else None
    normalized_task_id = _label(task_id)
    normalized_role = _label(role)
    token = _digest(assignment_token)
    context = _digest(context_hash)
    summary = _digest(summary_hash)
    artifacts = _digest_list(artifact_hashes, maximum=16)
    digests = _digest_list(digest_hashes, maximum=32)
    if (
        roles is None
        or normalized_task_id is None
        or normalized_role not in _SCHEDULER_ROLES
        or token is None
        or context is None
        or summary is None
        or artifacts is None
        or digests is None
    ):
        return NO_SAFE_WORK
    transfer: dict[str, object] = {
        "schema": "2718lab-devkit/fastlane-host-transfer-v1",
        "kind": kind,
        "sender_role": roles[0],
        "recipient_role": roles[1],
        "task_id": normalized_task_id,
        "role": normalized_role,
        "assignment_token": token,
        "context_hash": context,
        "summary_hash": summary,
        "artifact_hashes": artifacts,
        "digest_hashes": digests,
    }
    transfer["transfer_hash"] = _canonical_hash(transfer)
    return transfer


def _label(value: object) -> str | None:
    if type(value) is not str or _LABEL.fullmatch(value) is None:
        return None
    return value


def _digest(value: object) -> str | None:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        return None
    return value


def _digest_list(value: object, *, maximum: int) -> list[str] | None:
    try:
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            return None
        if len(value) > maximum:
            return None
        normalized = [_digest(item) for item in value]
    except Exception:
        return None
    if any(item is None for item in normalized):
        return None
    digests = [item for item in normalized if item is not None]
    if len(set(digests)) != len(digests):
        return None
    return digests


def _dispatch_request(value: object) -> dict[str, object]:
    if (
        type(value) is not dict
        or len(value) != len(_DISPATCH_REQUEST_FIELDS)
        or set(value) != _DISPATCH_REQUEST_FIELDS
    ):
        raise ValueError("dispatch request is invalid")
    assignments = value.get("assignments")
    if (
        value.get("schema") != "2718lab-devkit/fastlane-host-dispatch-request-v1"
        or value.get("action") != "dispatch_all"
        or type(assignments) is not list
        or not assignments
        or len(assignments) > 16
    ):
        raise ValueError("dispatch request is invalid")
    return {
        "schema": value["schema"],
        "action": value["action"],
        "assignments": [
            _bounded_dispatch_assignment(assignment) for assignment in assignments
        ],
    }


def _bounded_dispatch_assignment(value: object) -> dict[str, object]:
    if (
        type(value) is not dict
        or len(value) != len(_DISPATCH_ASSIGNMENT_FIELDS)
        or set(value) != _DISPATCH_ASSIGNMENT_FIELDS
    ):
        raise ValueError("dispatch assignment is invalid")
    route = value.get("route")
    if (
        type(route) is not dict
        or len(route) != len(_DISPATCH_ROUTE_FIELDS)
        or set(route) != _DISPATCH_ROUTE_FIELDS
    ):
        raise ValueError("dispatch route is invalid")
    normalized_route = {
        "model": _bounded_string(route.get("model"), maximum=128),
        "reasoning_effort": _bounded_string(route.get("reasoning_effort"), maximum=128),
        "routing_context_hash": _bounded_string(
            route.get("routing_context_hash"), maximum=71
        ),
        "routing_result_hash": _bounded_string(
            route.get("routing_result_hash"), maximum=71
        ),
        "require_explicit_route": route.get("require_explicit_route"),
    }
    if normalized_route["require_explicit_route"] is not True:
        raise ValueError("dispatch route is invalid")
    write_scope = value.get("write_scope")
    if type(write_scope) is not list or not write_scope or len(write_scope) > 32:
        raise ValueError("dispatch write scope is invalid")
    normalized_scope = list(_canonical_write_scope(tuple(write_scope)))
    integer_fields = (
        "lease_epoch",
        "task_version",
        "dispatch_order",
        "ledger_epoch",
    )
    if any(type(value.get(field)) is not int for field in integer_fields):
        raise ValueError("dispatch integer field is invalid")
    if (
        not 0 < value["lease_epoch"] <= 2**63 - 1
        or not 0 <= value["task_version"] <= 2**63 - 1
        or not 0 <= value["dispatch_order"] <= 16
        or not 0 < value["ledger_epoch"] <= 2**63 - 1
    ):
        raise ValueError("dispatch integer field is out of bounds")
    string_bounds = {
        "task_id": 128,
        "lease_id": 128,
        "assignment_token": 71,
        "concurrency_mode": 32,
        "index_context_hash": 71,
        "worktree_identity": 71,
        "worktree_base": 71,
        "integration_head": 71,
        "predecessor_hash": 71,
        "source_plan_hash": 71,
        "active_lease_set_hash": 71,
        "dispatch_binding_hash": 71,
    }
    normalized = {
        field: _bounded_string(value.get(field), maximum=maximum)
        for field, maximum in string_bounds.items()
    }
    return {
        "task_id": normalized["task_id"],
        "route": normalized_route,
        "lease_id": normalized["lease_id"],
        "lease_epoch": value["lease_epoch"],
        "task_version": value["task_version"],
        "assignment_token": normalized["assignment_token"],
        "write_scope": normalized_scope,
        "concurrency_mode": normalized["concurrency_mode"],
        "dispatch_order": value["dispatch_order"],
        "index_context_hash": normalized["index_context_hash"],
        "worktree_identity": normalized["worktree_identity"],
        "worktree_base": normalized["worktree_base"],
        "integration_head": normalized["integration_head"],
        "predecessor_hash": normalized["predecessor_hash"],
        "source_plan_hash": normalized["source_plan_hash"],
        "ledger_epoch": value["ledger_epoch"],
        "active_lease_set_hash": normalized["active_lease_set_hash"],
        "dispatch_binding_hash": normalized["dispatch_binding_hash"],
    }


def _bounded_string(value: object, *, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("dispatch string field is invalid")
    return value


def _bounded_json_size(value: object, *, depth: int = 0) -> int:
    """Count exact compact-JSON bytes without constructing the whole document."""

    if depth > 4:
        raise ValueError("dispatch request is too deep")
    if type(value) is str:
        return len(
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        return len(str(value))
    if type(value) is list:
        return (
            2
            + max(0, len(value) - 1)
            + sum(_bounded_json_size(item, depth=depth + 1) for item in value)
        )
    if type(value) is dict:
        return (
            2
            + max(0, len(value) - 1)
            + sum(
                _bounded_json_size(key, depth=depth + 1)
                + 1
                + _bounded_json_size(item, depth=depth + 1)
                for key, item in value.items()
            )
        )
    raise ValueError("dispatch request contains an unsupported value")


def _normalized_dispatch_facts(value: object) -> tuple[_HostDispatchFact, ...]:
    if type(value) is not tuple or not value or len(value) > 16:
        raise ValueError("dispatch facts are unavailable")
    facts = tuple(_normalized_dispatch_fact(fact) for fact in value)
    if len({fact.task_id for fact in facts}) != len(facts):
        raise ValueError("dispatch tasks are duplicated")
    return facts


def _normalized_dispatch_fact(value: object) -> _HostDispatchFact:
    if type(value) is not _HostDispatchFact:
        raise ValueError("dispatch fact is foreign")
    route = value.route
    scope = _canonical_write_scope(value.write_scope)
    if (
        type(route) is not _HostDispatchRoute
        or _label(route.model) is None
        or _label(route.reasoning_effort) is None
        or route.reasoning_effort == "ultra"
        or _digest(route.routing_context_hash) is None
        or _digest(route.routing_result_hash) is None
        or route.require_explicit_route is not True
        or _label(value.task_id) is None
        or _label(value.lease_id) is None
        or type(value.lease_epoch) is not int
        or value.lease_epoch <= 0
        or type(value.task_version) is not int
        or value.task_version < 0
        or _digest(value.assignment_token) is None
        or value.concurrency_mode not in _DISPATCH_MODES
        or type(value.dispatch_order) is not int
        or value.dispatch_order < 0
        or (value.concurrency_mode == "serial") != (value.dispatch_order > 0)
        or _digest(value.index_context_hash) is None
        or _digest(value.worktree_identity) is None
        or _digest(value.worktree_base) is None
        or _digest(value.integration_head) is None
        or _digest(value.predecessor_hash) is None
        or _digest(value.source_plan_hash) is None
        or type(value.ledger_epoch) is not int
        or value.ledger_epoch <= 0
        or _digest(value.active_lease_set_hash) is None
    ):
        raise ValueError("dispatch fact is invalid")
    return _HostDispatchFact(**{**value.__dict__, "write_scope": scope})


def _canonical_write_scope(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > 32:
        raise ValueError("write scope is invalid")
    normalized: list[str] = []
    for item in value:
        if (
            type(item) is not str
            or not item
            or len(item) > 256
            or item != item.strip()
            or "\\" in item
            or item.startswith("/")
        ):
            raise ValueError("write scope is invalid")
        parts = item.split("/")
        if any(
            _PATH_PART.fullmatch(part) is None
            or part in {".", ".."}
            or part.endswith((".", " "))
            for part in parts
        ):
            raise ValueError("write scope is invalid")
        normalized.append(item)
    if tuple(sorted(normalized)) != tuple(normalized):
        raise ValueError("write scope is not canonical")
    if len(set(normalized)) != len(normalized):
        raise ValueError("write scope is duplicated")
    return tuple(normalized)


def _dispatch_fact_mapping(value: object) -> dict[str, object]:
    fact = _normalized_dispatch_fact(value)
    mapping: dict[str, object] = {
        "task_id": fact.task_id,
        "route": {
            "model": fact.route.model,
            "reasoning_effort": fact.route.reasoning_effort,
            "routing_context_hash": fact.route.routing_context_hash,
            "routing_result_hash": fact.route.routing_result_hash,
            "require_explicit_route": True,
        },
        "lease_id": fact.lease_id,
        "lease_epoch": fact.lease_epoch,
        "task_version": fact.task_version,
        "assignment_token": fact.assignment_token,
        "write_scope": list(fact.write_scope),
        "concurrency_mode": fact.concurrency_mode,
        "dispatch_order": fact.dispatch_order,
        "index_context_hash": fact.index_context_hash,
        "worktree_identity": fact.worktree_identity,
        "worktree_base": fact.worktree_base,
        "integration_head": fact.integration_head,
        "predecessor_hash": fact.predecessor_hash,
        "source_plan_hash": fact.source_plan_hash,
        "ledger_epoch": fact.ledger_epoch,
        "active_lease_set_hash": fact.active_lease_set_hash,
    }
    mapping["dispatch_binding_hash"] = _canonical_hash(mapping)
    return mapping


def _lease_scope_binding_hash(value: object) -> str:
    fact = _normalized_dispatch_fact(value)
    return _canonical_hash(
        {
            "task_id": fact.task_id,
            "lease_id": fact.lease_id,
            "lease_epoch": fact.lease_epoch,
            "task_version": fact.task_version,
            "assignment_token": fact.assignment_token,
            "write_scope": list(fact.write_scope),
            "worktree_identity": fact.worktree_identity,
            "predecessor_hash": fact.predecessor_hash,
            "ledger_epoch": fact.ledger_epoch,
            "active_lease_set_hash": fact.active_lease_set_hash,
        }
    )


def _validate_batch_fences(facts: tuple[_HostDispatchFact, ...]) -> None:
    if len({fact.source_plan_hash for fact in facts}) != 1:
        raise ValueError("source plans are mixed")
    if len({fact.ledger_epoch for fact in facts}) != 1:
        raise ValueError("ledger epochs are mixed")
    if len({fact.active_lease_set_hash for fact in facts}) != 1:
        raise ValueError("active lease sets are mixed")
    serial_orders = [
        fact.dispatch_order for fact in facts if fact.concurrency_mode == "serial"
    ]
    if len(serial_orders) != len(set(serial_orders)):
        raise ValueError("serial order is duplicated")
    for index, left in enumerate(facts):
        for right in facts[index + 1 :]:
            if not _scopes_overlap(left.write_scope, right.write_scope):
                continue
            if left.concurrency_mode == right.concurrency_mode == "serial":
                continue
            if (
                left.concurrency_mode == right.concurrency_mode == "isolated_worktree"
                and left.worktree_identity != right.worktree_identity
            ):
                continue
            raise ValueError("parallel write scopes overlap")


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    for left_item in left:
        left_folded = left_item.casefold()
        for right_item in right:
            right_folded = right_item.casefold()
            if (
                left_folded == right_folded
                or left_folded.startswith(right_folded + "/")
                or right_folded.startswith(left_folded + "/")
            ):
                return True
    return False


def _canonical_hash(value: object) -> str:
    return _hash_bytes(_canonical_bytes(value))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = [
    "NO_SAFE_WORK",
    "compile_fast_lane_with_host_facts",
    "prepare_verified_host_facts",
    "project_role_transfer",
]
