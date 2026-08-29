"""Private, inert Fast Lane host-boundary adapter.

Only an opaque compiler-evidence handle issued by ``HostSession`` may cross
this boundary. Public capability claims remain insufficient to start work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from . import host_envelopes
from .host_session import (
    HostCapabilityFact,
    HostRoute,
    HostSchedulingFacts,
    HostSession,
    _CompilerInvocation,
)

NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HASH: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_HASH: Final = re.compile(r"[0-9a-f]{64}\Z")
_STORAGE_PROFILE_SCHEMA: Final = "2718lab-devkit/storage-profile-v1"
_STORAGE_INTENT_SCHEMA: Final = "2718lab.storage.intent.v1"
_STORAGE_TARGET_SCHEMA: Final = "2718lab.storage.target.v1"
_STORAGE_PROFILE_FIELDS: Final = frozenset(
    {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "task_id",
        "source_plan_hash",
        "index_attestation_hash",
        "execution_context_hash",
        "repository_identity",
        "workspace_manifest_hash",
        "cargo_lock_hash",
        "toolchain_digest",
        "target_triple",
        "profile",
        "features_hash",
        "build_env_class",
        "profile_hash",
        "attestation_hash",
    }
)
_STORAGE_DESCRIPTOR_FIELDS: Final = (
    "repository_identity",
    "workspace_manifest_hash",
    "cargo_lock_hash",
    "toolchain_digest",
    "target_triple",
    "profile",
    "features_hash",
    "build_env_class",
)
_LABEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PREPARATION_ID: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_FAST_LANE_TASK_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
_STORAGE_PROFILE_SCALAR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_STORAGE_PROFILE_BUILD_ENV_CLASSES: Final = frozenset(
    {"managed_read_only", "managed_workspace", "disabled", "external"}
)
_PATH_PART: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
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
_PLANNER_REQUEST_FIELDS: Final = frozenset(
    {"schema", "action", "assignment_skeletons", "project_index_attestation_refs"}
)
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
_MAX_DISPATCH_JSON_DEPTH: Final = 12


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
    assignment_token: str = field(repr=False)
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
    bridge_attested: bool = False
    evidence_expires_at: int | None = None
    preparation_id: str | None = None
    call_intent_hash: str | None = None
    storage_budgets: tuple[tuple[str, int, int], ...] = ()


def _normalized_storage_budgets(
    value: object,
) -> tuple[tuple[str, int, int], ...]:
    """Normalize caller budgets kept outside the pre-Host skeleton."""

    if value is None:
        return ()
    if not isinstance(value, Mapping) or len(value) > 16:
        raise ValueError("storage budgets are unavailable")
    normalized: list[tuple[str, int, int]] = []
    for raw_task_id, raw_budget in value.items():
        if (
            type(raw_task_id) is not str
            or _FAST_LANE_TASK_ID.fullmatch(raw_task_id) is None
            or not isinstance(raw_budget, Mapping)
        ):
            raise ValueError("storage budget is invalid")
        task_id = raw_task_id
        if set(raw_budget) != {"bytes", "files"}:
            raise ValueError("storage budget is invalid")
        requested_bytes = raw_budget.get("bytes")
        requested_files = raw_budget.get("files")
        if (
            type(requested_bytes) is not int
            or not 0 < requested_bytes <= (1 << 64) - 1
            or type(requested_files) is not int
            or not 0 < requested_files <= (1 << 64) - 1
        ):
            raise ValueError("storage budget is invalid")
        normalized.append((task_id, requested_bytes, requested_files))
    normalized.sort(key=lambda item: item[0])
    if len({item[0] for item in normalized}) != len(normalized):
        raise ValueError("storage budget tasks are duplicated")
    return tuple(normalized)


def _storage_intents_for_profiles(
    profiles: tuple[dict[str, object], ...],
    assignments: Sequence[Mapping[str, object]],
    budgets: tuple[tuple[str, int, int], ...],
) -> list[dict[str, object]]:
    """Construct post-Host intents from Host facts and external budgets."""

    if not profiles:
        if budgets:
            raise ValueError("Host storage profiles are unavailable")
        return []
    if not budgets or len(profiles) != len(assignments):
        raise ValueError("Host storage profiles are incomplete")
    budget_by_task = {task_id: (bytes_, files) for task_id, bytes_, files in budgets}
    assignment_task_ids: list[str] = []
    for assignment in assignments:
        task_id = assignment.get("task_id")
        source_plan_hash = assignment.get("source_plan_hash")
        if (
            type(task_id) is not str
            or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
            or type(source_plan_hash) is not str
            or _HASH.fullmatch(source_plan_hash) is None
        ):
            raise ValueError("storage profile assignment binding is invalid")
        assignment_task_ids.append(task_id)
    if (
        len(set(assignment_task_ids)) != len(assignment_task_ids)
        or set(budget_by_task) != set(assignment_task_ids)
    ):
        raise ValueError("storage budget/profile bindings are invalid")
    profile_task_ids: list[str] = []
    for profile in profiles:
        if type(profile) is not dict or set(profile) != _STORAGE_PROFILE_FIELDS:
            raise ValueError("Host storage profile fields are invalid")
        task_id = profile.get("task_id")
        call_intent_hash = profile.get("call_intent_hash")
        preparation_id = profile.get("preparation_id")
        if (
            type(task_id) is not str
            or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
            or type(call_intent_hash) is not str
            or _RAW_HASH.fullmatch(call_intent_hash) is None
            or type(preparation_id) is not str
            or _PREPARATION_ID.fullmatch(preparation_id) is None
            or profile.get("schema") != _STORAGE_PROFILE_SCHEMA
        ):
            raise ValueError("Host storage profile is invalid")
        for field_name in (
            "source_plan_hash",
            "index_attestation_hash",
            "execution_context_hash",
            "repository_identity",
            "workspace_manifest_hash",
            "cargo_lock_hash",
            "toolchain_digest",
            "features_hash",
            "profile_hash",
            "attestation_hash",
        ):
            value = profile.get(field_name)
            if type(value) is not str or _HASH.fullmatch(value) is None:
                raise ValueError("Host storage profile hashes are invalid")
        target_triple = profile.get("target_triple")
        if (
            type(target_triple) is not str
            or _STORAGE_PROFILE_SCALAR.fullmatch(target_triple) is None
            or profile.get("profile") != "dev"
        ):
            raise ValueError("Host storage profile scalar is invalid")
        if profile.get("build_env_class") not in _STORAGE_PROFILE_BUILD_ENV_CLASSES:
            raise ValueError("Host storage profile build environment is invalid")
        unsigned = {
            key: profile[key]
            for key in _STORAGE_PROFILE_FIELDS
            if key not in {"profile_hash", "attestation_hash"}
        }
        if not hmac.compare_digest(
            cast(str, profile["profile_hash"]), _canonical_hash(unsigned)
        ):
            raise ValueError("Host storage profile binding is invalid")
        profile_task_ids.append(task_id)
    if profile_task_ids != assignment_task_ids:
        raise ValueError("storage budget/profile bindings are invalid")
    intents: list[dict[str, object]] = []
    for assignment, profile in zip(assignments, profiles, strict=True):
        task_id = assignment.get("task_id")
        source_plan_hash = assignment.get("source_plan_hash")
        assert type(task_id) is str
        budget = budget_by_task.get(task_id)
        if (
            budget is None
            or profile.get("source_plan_hash") != source_plan_hash
        ):
            raise ValueError("storage profile assignment binding is invalid")
        context_hash = profile.get("execution_context_hash")
        if (
            type(source_plan_hash) is not str
            or _HASH.fullmatch(source_plan_hash) is None
            or type(context_hash) is not str
            or _HASH.fullmatch(context_hash) is None
        ):
            raise ValueError("storage profile hashes are invalid")
        descriptor = {
            "schema": _STORAGE_TARGET_SCHEMA,
            "artifact_kind": "fastlane-task",
            **{field: profile.get(field) for field in _STORAGE_DESCRIPTOR_FIELDS},
        }
        if any(
            type(descriptor[field]) is not str or not descriptor[field]
            for field in _STORAGE_DESCRIPTOR_FIELDS
        ):
            raise ValueError("storage profile descriptor is invalid")
        requested_bytes, requested_files = budget
        intent_preimage = {
            "target_descriptor": descriptor,
            "task_id": task_id,
            "plan_binding": source_plan_hash,
            "context_hash": context_hash,
            "requested_bytes": requested_bytes,
            "requested_files": requested_files,
        }
        intent = {
            "schema": _STORAGE_INTENT_SCHEMA,
            "task_id": task_id,
            "plan_binding": source_plan_hash,
            "context_hash": context_hash,
            "storage_intent_hash": _canonical_hash(intent_preimage),
            "requested_bytes": requested_bytes,
            "requested_files": requested_files,
            "target_descriptor": descriptor,
        }
        from .storage_intent import parse_storage_intent

        intents.append(parse_storage_intent(intent).to_dict())
    return intents


def prepare_verified_host_facts(
    session: object,
    *,
    capability_facts: Sequence[HostCapabilityFact] | object = (),
    preparation_id: object = None,
    call_intent_hash: object = None,
    routing_registry_binding_hash: object = None,
    request: object = None,
    reasoning_effort: object = None,
    requested_routes: Sequence[HostRoute] | object = None,
    storage_budgets: object = None,
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
        normalized_storage_budgets = _normalized_storage_budgets(storage_budgets)
        bridge_attested = False
        if request is not None or reasoning_effort is not None:
            normalized_request = _planner_request(request)
            request_bytes = _canonical_bytes(normalized_request)
            if (
                type(reasoning_effort) is not str
                or len(request_bytes) > _MAX_DISPATCH_REQUEST_BYTES
            ):
                return NO_SAFE_WORK
            if requested_routes is None:
                requested_route_set: set[HostRoute] = set()
                for assignment in cast(
                    list[dict[str, object]], normalized_request["assignment_skeletons"]
                ):
                    proof = cast(dict[str, object], assignment["routing_proof"])
                    result = cast(dict[str, object], proof["result"])
                    route = cast(dict[str, object], result["route"])
                    requested_route_set.add(
                        HostRoute(
                            model=cast(str, route["model"]),
                            effort=cast(str, route["effort"]),
                        )
                    )
                requested_routes = tuple(
                    sorted(
                        requested_route_set,
                        key=lambda route: (route.model, route.effort),
                    )
                )
            elif not isinstance(requested_routes, Sequence) or isinstance(
                requested_routes, (str, bytes, bytearray)
            ):
                return NO_SAFE_WORK
            else:
                requested_routes = tuple(requested_routes)
            skeletons = tuple(
                cast(
                    list[dict[str, object]],
                    normalized_request["assignment_skeletons"],
                )
            )
            storage_task_ids = tuple(
                cast(str, skeleton["task_id"]) for skeleton in skeletons
            )
            if normalized_storage_budgets and {
                task_id for task_id, _bytes, _files in normalized_storage_budgets
            } != set(storage_task_ids):
                return NO_SAFE_WORK
            bridge_attested = session.bind_compiler_request(
                preparation_id=normalized_preparation_id,
                call_intent_hash=cast(str, call_intent_hash),
                request_hash=_hash_bytes(request_bytes),
                reasoning_effort=reasoning_effort,
                requested_routes=requested_routes,
                assignment_skeletons=skeletons,
                project_index_attestation_refs=tuple(
                    cast(
                        list[dict[str, object]],
                        normalized_request["project_index_attestation_refs"],
                    )
                ),
                routing_registry_binding_hash=cast(
                    str, routing_registry_binding_hash
                ),
                storage_task_ids=(storage_task_ids if normalized_storage_budgets else ()),
            )
            if not bridge_attested:
                return NO_SAFE_WORK
        else:
            if normalized_storage_budgets:
                return NO_SAFE_WORK
            scheduling = session.scheduling_facts(tuple(capability_facts))
            if type(scheduling) is not HostSchedulingFacts:
                return NO_SAFE_WORK
        evidence = session.prepare_compiler_evidence(
            preparation_id=normalized_preparation_id
        )
        if evidence == NO_SAFE_WORK:
            return NO_SAFE_WORK
        expires_at = session.compiler_evidence_expires_at(evidence)
        if expires_at is None:
            return NO_SAFE_WORK
        return _PreparedHostFacts(
            session=session,
            evidence=evidence,
            capability_facts=tuple(capability_facts),
            bridge_attested=bridge_attested,
            evidence_expires_at=expires_at,
            preparation_id=normalized_preparation_id,
            call_intent_hash=(
                cast(str, call_intent_hash) if bridge_attested else None
            ),
            storage_budgets=normalized_storage_budgets,
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
        normalized_request = (
            _planner_request(request)
            if prepared.bridge_attested
            else _dispatch_request(request)
        )
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
        if prepared.bridge_attested:
            skeletons = cast(
                list[dict[str, object]], normalized_request["assignment_skeletons"]
            )
            if len(skeletons) != len(fact_mappings) or any(
                mapping[field_name] != skeleton[field_name]
                for mapping, skeleton in zip(fact_mappings, skeletons, strict=True)
                for field_name in (
                    "task_id",
                    "write_scope",
                    "concurrency_mode",
                    "dispatch_order",
                    "index_context_hash",
                    "predecessor_hash",
                    "source_plan_hash",
                )
            ):
                return NO_SAFE_WORK
            for mapping, skeleton in zip(fact_mappings, skeletons, strict=True):
                proof = cast(dict[str, object], skeleton["routing_proof"])
                result = cast(dict[str, object], proof["result"])
                route = cast(dict[str, object], result["route"])
                if mapping["route"] != {
                    "model": route["model"],
                    "reasoning_effort": route["effort"],
                    "routing_context_hash": proof["routing_context_hash"],
                    "routing_result_hash": proof["routing_result_hash"],
                    "require_explicit_route": True,
                }:
                    return NO_SAFE_WORK
        elif normalized_request["assignments"] != fact_mappings:
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
        if prepared.bridge_attested:
            if material.registry_binding_hash is None:
                return NO_SAFE_WORK
            attested_routes = {
                HostRoute(model=fact.route.model, effort=fact.route.reasoning_effort)
                for fact in facts
            }
        else:
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
        # Storage intents are local compiler proof material only in Task4a.  Do
        # not extend the established dispatch-batch schema before Task4b owns
        # Host admission/execution.  Constructing them here still validates the
        # Host profile order, the caller budgets, and every profile/dispatch
        # binding after the full compiler evidence response has been verified.
        if prepared.storage_budgets:
            _storage_intents_for_profiles(
                material.storage_profiles,
                fact_mappings,
                prepared.storage_budgets,
            )
        elif material.storage_profiles:
            return NO_SAFE_WORK
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


def dispatch_fast_lane_with_host_facts(
    request: object,
    *,
    reasoning_effort: object,
    verified_host_facts: object,
    correlation_id: object,
    now: object,
    refill_callback: object = None,
) -> object | str:
    """Compile and commit one batch to the authenticated bridge, never publicly."""

    if (
        type(verified_host_facts) is not _PreparedHostFacts
        or not verified_host_facts.bridge_attested
        or type(correlation_id) is not str
        or type(now) is not int
        or (refill_callback is not None and not callable(refill_callback))
    ):
        return NO_SAFE_WORK
    prepared = verified_host_facts
    batch = compile_fast_lane_with_host_facts(
        request,
        reasoning_effort=reasoning_effort,
        verified_host_facts=prepared,
    )
    if type(batch) is not dict or prepared.evidence_expires_at is None:
        return NO_SAFE_WORK
    try:
        assignments = batch["assignments"]
        assert type(assignments) is list and assignments
        first = assignments[0]
        assert type(first) is dict
        route = first["route"]
        assert type(route) is dict
        binding = host_envelopes.EnvelopeBinding(
            task_id=first["task_id"],
            lease_epoch=first["lease_epoch"],
            assignment_token=first["assignment_token"],
            dispatch_context_hash=first["dispatch_binding_hash"],
            route_hash=route["routing_result_hash"],
            expires_at=prepared.evidence_expires_at,
        )
        return prepared.session.send_fast_lane_dispatch_batch(
            batch=batch,
            binding=binding,
            correlation_id=correlation_id,
            now=now,
            call_intent_hash=prepared.call_intent_hash,
            preparation_id=prepared.preparation_id,
            refill_callback=cast(
                Callable[[Mapping[str, object]], object] | None, refill_callback
            ),
        )
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


def _planner_request(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _PLANNER_REQUEST_FIELDS:
        raise ValueError("planner request is invalid")
    skeletons = value.get("assignment_skeletons")
    references = value.get("project_index_attestation_refs")
    if (
        value.get("schema") != "2718lab-devkit/fastlane-host-planner-request-v1"
        or value.get("action") != "plan_dispatch"
        or type(skeletons) is not list
        or not skeletons
        or len(skeletons) > 16
        or type(references) is not list
        or len(references) != len(skeletons)
    ):
        raise ValueError("planner request is invalid")
    from .host_bridge import (
        _normalize_assignment_skeleton,
        _normalize_project_index_attestation_ref,
    )

    normalized_skeletons = [_normalize_assignment_skeleton(item) for item in skeletons]
    normalized_references = [
        _normalize_project_index_attestation_ref(item) for item in references
    ]
    _validate_dispatch_order_sequence(normalized_skeletons)
    if [item["task_id"] for item in normalized_skeletons] != [
        item["task_id"] for item in normalized_references
    ]:
        raise ValueError("planner request is invalid")
    return {
        "schema": value["schema"],
        "action": value["action"],
        "assignment_skeletons": normalized_skeletons,
        "project_index_attestation_refs": normalized_references,
    }


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
    normalized_assignments = [
        _bounded_dispatch_assignment(assignment) for assignment in assignments
    ]
    _validate_dispatch_order_sequence(normalized_assignments)
    return {
        "schema": value["schema"],
        "action": value["action"],
        "assignments": normalized_assignments,
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
        or not 0 <= value["dispatch_order"] < 16
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

    if depth > _MAX_DISPATCH_JSON_DEPTH:
        raise ValueError("dispatch request is too deep")
    if type(value) is str:
        return len(
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
    if type(value) is bool:
        return 4 if value else 5
    if value is None:
        return 4
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
    _validate_dispatch_order_sequence(facts)
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
            or any(ord(character) < 32 for character in item)
        ):
            raise ValueError("write scope is invalid")
        canonical_item = unicodedata.normalize("NFC", item).casefold()
        parts = canonical_item.split("/")
        if any(
            _PATH_PART.fullmatch(part) is None
            or part in {".", ".."}
            or part.endswith((".", " "))
            or part.rstrip(" .").split(".", 1)[0] in _WINDOWS_RESERVED_NAMES
            for part in parts
        ):
            raise ValueError("write scope is invalid")
        normalized.append("/".join(parts))
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


def _dispatch_fact_from_mapping(value: object) -> _HostDispatchFact:
    """Elevate one closed wire mapping only after every scalar has normalized."""

    normalized = _bounded_dispatch_assignment(value)
    route = cast(dict[str, object], normalized["route"])
    fact = _HostDispatchFact(
        task_id=cast(str, normalized["task_id"]),
        route=_HostDispatchRoute(
            model=cast(str, route["model"]),
            reasoning_effort=cast(str, route["reasoning_effort"]),
            routing_context_hash=cast(str, route["routing_context_hash"]),
            routing_result_hash=cast(str, route["routing_result_hash"]),
            require_explicit_route=cast(bool, route["require_explicit_route"]),
        ),
        lease_id=cast(str, normalized["lease_id"]),
        lease_epoch=cast(int, normalized["lease_epoch"]),
        task_version=cast(int, normalized["task_version"]),
        assignment_token=cast(str, normalized["assignment_token"]),
        write_scope=tuple(cast(list[str], normalized["write_scope"])),
        concurrency_mode=cast(str, normalized["concurrency_mode"]),
        dispatch_order=cast(int, normalized["dispatch_order"]),
        index_context_hash=cast(str, normalized["index_context_hash"]),
        worktree_identity=cast(str, normalized["worktree_identity"]),
        worktree_base=cast(str, normalized["worktree_base"]),
        integration_head=cast(str, normalized["integration_head"]),
        predecessor_hash=cast(str, normalized["predecessor_hash"]),
        source_plan_hash=cast(str, normalized["source_plan_hash"]),
        ledger_epoch=cast(int, normalized["ledger_epoch"]),
        active_lease_set_hash=cast(str, normalized["active_lease_set_hash"]),
    )
    if normalized["dispatch_binding_hash"] != _dispatch_fact_mapping(fact)[
        "dispatch_binding_hash"
    ]:
        raise ValueError("dispatch binding hash is invalid")
    return fact


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
    _validate_dispatch_order_sequence(facts)
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


def _validate_dispatch_order_sequence(values: Sequence[object]) -> None:
    """Validate source-plan coordinates without coupling a wave to ordering.

    ``dispatch_order`` remains an authenticated source-plan coordinate.  A
    refill is a sparse slice, so batch admission must not require the slice to
    be sorted; the projection layer owns global dependency/order validation.
    """

    orders: list[object] = []
    for value in values:
        if type(value) is _HostDispatchFact:
            orders.append(value.dispatch_order)
        elif type(value) is dict:
            orders.append(value.get("dispatch_order"))
        else:
            raise ValueError("dispatch order is invalid")
    if any(type(order) is not int or not 0 <= order < 16 for order in orders):
        raise ValueError("dispatch order is invalid")
    if len(set(orders)) != len(orders):
        raise ValueError("dispatch order is duplicated")


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
    "dispatch_fast_lane_with_host_facts",
    "prepare_verified_host_facts",
    "project_role_transfer",
]
