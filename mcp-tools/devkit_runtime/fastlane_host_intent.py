"""Pure, fail-closed validation for future Fast Lane Host execution intents.

This module intentionally has no Host, app, subprocess, network, thread, or
worktree dependency.  It only validates a fully pre-bound JSON-shaped intent
and returns an immutable typed representation when every binding agrees.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, Literal, cast

NO_SAFE_WORK: Final = "NO_SAFE_WORK"

_SCHEMA: Final = "2718lab-devkit/fastlane-host-execution-intent-v2"
_PREDECESSOR_SCHEMA: Final = "2718lab-devkit/fastlane-external-lease-predecessor-v2"
_HASH_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")
_IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_COMMON_DIR_PATTERN: Final = re.compile(r"[A-Za-z]:/[A-Za-z0-9._/-]{1,510}\Z")
_VALID_EFFORTS: Final = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})

_ROOT_KEYS: Final = frozenset(
    {
        "schema",
        "projection_hash",
        "source_plan_hash",
        "workflow_hash",
        "assignment",
        "route",
        "capability_facts",
        "packets",
        "source",
        "create",
        "lease",
        "intent_hash",
    }
)
_ASSIGNMENT_KEYS: Final = frozenset(
    {
        "assignment_id",
        "assignment_token",
        "predecessor",
        "assignment_binding_hash",
    }
)
_PREDECESSOR_KEYS: Final = frozenset(
    {
        "schema",
        "projection_hash",
        "source_plan_hash",
        "workflow_hash",
        "task_id",
        "role",
        "assignment_id",
        "assignment_token",
        "routing_result_hash",
        "ledger_epoch",
        "active_lease_set_hash",
        "lease_epoch",
        "predecessor_hash",
    }
)
_ROUTE_KEYS: Final = frozenset(
    {
        "model",
        "reasoning_effort",
        "routing_context_hash",
        "routing_result_hash",
        "session_scope",
        "inherit_current_session_model",
        "require_explicit_route",
        "route_binding_hash",
    }
)
_CAPABILITY_FACT_KEYS: Final = frozenset(
    {
        "model",
        "reasoning_effort",
        "state",
        "attestation_hash",
        "capability_binding_hash",
    }
)
_PACKET_KEYS: Final = frozenset(
    {
        "task_packet_hash",
        "input_packet_hash",
        "index_packet_hash",
        "assignment_id",
        "assignment_token",
        "routing_context_hash",
        "packet_binding_hash",
    }
)
_SOURCE_KEYS: Final = frozenset(
    {
        "project_id",
        "registered_project_id",
        "repository",
        "common_dir",
        "ref",
        "commit",
        "tree",
        "source_binding_hash",
    }
)
_CREATE_KEYS: Final = frozenset(
    {
        "request_hash",
        "operation_id",
        "project_id",
        "repository",
        "common_dir",
        "ref",
        "commit",
        "tree",
        "assignment_id",
        "assignment_token",
        "lease_fencing_token",
        "create_binding_hash",
    }
)
_LEASE_KEYS: Final = frozenset(
    {
        "owner",
        "epoch",
        "fencing_token",
        "assignment_id",
        "assignment_token",
        "predecessor_hash",
        "ledger_epoch",
        "active_lease_set_hash",
        "lease_binding_hash",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedHostCapabilityFact:
    """A capability claim parsed from a candidate, without Host verification."""

    model: str
    reasoning_effort: str
    state: str
    attestation_hash: str
    capability_binding_hash: str


@dataclass(frozen=True, slots=True)
class HostCapabilityExpectation:
    """A public capability field set used only for non-authorizing comparison."""

    model: str
    reasoning_effort: str
    state: str
    attestation_hash: str


@dataclass(frozen=True, slots=True)
class HostExecutionExpectationProjection:
    """A non-authorizing, public fieldwise comparison projection."""

    candidate_intent_hash: str
    projection_hash: str
    source_plan_hash: str
    workflow_hash: str
    assignment_id: str
    assignment_token: str
    predecessor_hash: str
    task_id: str
    role: str
    model: str
    reasoning_effort: str
    routing_context_hash: str
    routing_result_hash: str
    session_scope: str
    inherit_current_session_model: bool
    require_explicit_route: bool
    capability_facts: tuple[HostCapabilityExpectation, ...]
    ledger_epoch: int
    active_lease_set_hash: str
    task_packet_hash: str
    input_packet_hash: str
    index_packet_hash: str
    project_id: str
    registered_project_id: str
    repository: str
    common_dir: str
    source_ref: str
    source_commit: str
    source_tree: str
    create_request_hash: str
    operation_id: str
    lease_owner: str
    lease_epoch: int
    lease_fencing_token: str


@dataclass(frozen=True, slots=True)
class ParsedHostExecutionIntent:
    """A structurally valid candidate parsed without Host authorization."""

    schema: str
    intent_hash: str
    projection_hash: str
    source_plan_hash: str
    workflow_hash: str
    assignment_id: str
    assignment_token: str
    assignment_binding_hash: str
    predecessor_hash: str
    task_id: str
    role: str
    model: str
    reasoning_effort: str
    routing_context_hash: str
    routing_result_hash: str
    session_scope: str
    inherit_current_session_model: bool
    require_explicit_route: bool
    route_binding_hash: str
    parsed_capability_facts: tuple[ParsedHostCapabilityFact, ...]
    ledger_epoch: int
    active_lease_set_hash: str
    task_packet_hash: str
    input_packet_hash: str
    index_packet_hash: str
    packet_binding_hash: str
    project_id: str
    registered_project_id: str
    repository: str
    common_dir: str
    source_ref: str
    source_commit: str
    source_tree: str
    source_binding_hash: str
    create_request_hash: str
    operation_id: str
    create_binding_hash: str
    lease_owner: str
    lease_epoch: int
    lease_fencing_token: str
    lease_binding_hash: str


def validate_host_execution_intent(
    candidate: object,
    *,
    expectation: object | None = None,
) -> Literal["NO_SAFE_WORK"]:
    """Fail closed until a Desktop Host-private verifier is available.

    Public Python data, including a self-consistent parsed candidate and a
    matching comparison projection, cannot authorize Fast Lane execution.
    """

    del candidate, expectation
    return NO_SAFE_WORK


def parse_host_execution_intent(
    candidate: object,
) -> ParsedHostExecutionIntent | Literal["NO_SAFE_WORK"]:
    """Parse a candidate structurally; the result is never an authorization."""

    try:
        parsed = _parse(candidate)
    except Exception:
        return NO_SAFE_WORK
    return parsed if parsed is not None else NO_SAFE_WORK


def _parse(candidate: object) -> ParsedHostExecutionIntent | None:
    root = _bound_mapping(candidate, _ROOT_KEYS, "intent_hash")
    if root is None or _text(root, "schema") != _SCHEMA:
        return None

    projection_hash = _valid_hash(root, "projection_hash")
    source_plan_hash = _valid_hash(root, "source_plan_hash")
    workflow_hash = _valid_hash(root, "workflow_hash")
    intent_hash = _valid_hash(root, "intent_hash")
    if (
        projection_hash is None
        or source_plan_hash is None
        or workflow_hash is None
        or intent_hash is None
    ):
        return None

    assignment = _bound_mapping(
        root["assignment"], _ASSIGNMENT_KEYS, "assignment_binding_hash"
    )
    route = _bound_mapping(root["route"], _ROUTE_KEYS, "route_binding_hash")
    packets = _bound_mapping(root["packets"], _PACKET_KEYS, "packet_binding_hash")
    source = _bound_mapping(root["source"], _SOURCE_KEYS, "source_binding_hash")
    create = _bound_mapping(root["create"], _CREATE_KEYS, "create_binding_hash")
    lease = _bound_mapping(root["lease"], _LEASE_KEYS, "lease_binding_hash")
    if any(
        value is None
        for value in (assignment, route, packets, source, create, lease)
    ):
        return None

    assert assignment is not None
    assert route is not None
    assert packets is not None
    assert source is not None
    assert create is not None
    assert lease is not None

    validated_route = _validate_route(route)
    validated_source = _validate_source(source)
    if validated_route is None or validated_source is None:
        return None
    (
        model,
        reasoning_effort,
        routing_context_hash,
        routing_result_hash,
        session_scope,
        inherit_current_session_model,
        require_explicit_route,
        route_binding_hash,
    ) = validated_route
    (
        project_id,
        registered_project_id,
        repository,
        common_dir,
        source_ref,
        source_commit,
        source_tree,
        source_binding_hash,
    ) = validated_source

    assignment_id = _valid_hash(assignment, "assignment_id")
    assignment_token = _valid_hash(assignment, "assignment_token")
    assignment_binding_hash = _valid_hash(assignment, "assignment_binding_hash")
    predecessor = _bound_mapping(
        assignment["predecessor"],
        _PREDECESSOR_KEYS,
        "predecessor_hash",
    )
    if (
        assignment_id is None
        or assignment_token is None
        or assignment_binding_hash is None
        or predecessor is None
    ):
        return None
    validated_predecessor = _validate_predecessor(
        predecessor,
        projection_hash=projection_hash,
        source_plan_hash=source_plan_hash,
        workflow_hash=workflow_hash,
        assignment_id=assignment_id,
        assignment_token=assignment_token,
        routing_result_hash=routing_result_hash,
    )
    if validated_predecessor is None:
        return None
    (
        task_id,
        role,
        predecessor_hash,
        lease_epoch,
        ledger_epoch,
        active_lease_set_hash,
    ) = validated_predecessor

    capability_facts = _validate_capability_facts(root["capability_facts"])
    if capability_facts is None or not _has_candidate_capability_claim(
        capability_facts,
        model,
        reasoning_effort,
    ):
        return None

    if not _validate_packets(
        packets,
        assignment_id=assignment_id,
        assignment_token=assignment_token,
        routing_context_hash=routing_context_hash,
    ):
        return None
    if not _validate_lease(
        lease,
        assignment_id=assignment_id,
        assignment_token=assignment_token,
        predecessor_hash=predecessor_hash,
        lease_epoch=lease_epoch,
        ledger_epoch=ledger_epoch,
        active_lease_set_hash=active_lease_set_hash,
    ):
        return None
    if not _validate_create(
        create,
        project_id=project_id,
        repository=repository,
        common_dir=common_dir,
        source_ref=source_ref,
        source_commit=source_commit,
        source_tree=source_tree,
        assignment_id=assignment_id,
        assignment_token=assignment_token,
        lease_fencing_token=_valid_hash(lease, "fencing_token"),
    ):
        return None

    task_packet_hash = _valid_hash(packets, "task_packet_hash")
    input_packet_hash = _valid_hash(packets, "input_packet_hash")
    index_packet_hash = _valid_hash(packets, "index_packet_hash")
    packet_binding_hash = _valid_hash(packets, "packet_binding_hash")
    create_request_hash = _valid_hash(create, "request_hash")
    operation_id = _valid_identifier(create, "operation_id")
    create_binding_hash = _valid_hash(create, "create_binding_hash")
    lease_owner = _valid_identifier(lease, "owner")
    lease_fencing_token = _valid_hash(lease, "fencing_token")
    lease_binding_hash = _valid_hash(lease, "lease_binding_hash")
    if (
        task_packet_hash is None
        or input_packet_hash is None
        or index_packet_hash is None
        or packet_binding_hash is None
        or create_request_hash is None
        or operation_id is None
        or create_binding_hash is None
        or lease_owner is None
        or lease_fencing_token is None
        or lease_binding_hash is None
    ):
        return None

    return ParsedHostExecutionIntent(
        schema=_SCHEMA,
        intent_hash=intent_hash,
        projection_hash=projection_hash,
        source_plan_hash=source_plan_hash,
        workflow_hash=workflow_hash,
        assignment_id=assignment_id,
        assignment_token=assignment_token,
        assignment_binding_hash=assignment_binding_hash,
        predecessor_hash=predecessor_hash,
        task_id=task_id,
        role=role,
        model=model,
        reasoning_effort=reasoning_effort,
        routing_context_hash=routing_context_hash,
        routing_result_hash=routing_result_hash,
        session_scope=session_scope,
        inherit_current_session_model=inherit_current_session_model,
        require_explicit_route=require_explicit_route,
        route_binding_hash=route_binding_hash,
        parsed_capability_facts=capability_facts,
        ledger_epoch=ledger_epoch,
        active_lease_set_hash=active_lease_set_hash,
        task_packet_hash=task_packet_hash,
        input_packet_hash=input_packet_hash,
        index_packet_hash=index_packet_hash,
        packet_binding_hash=packet_binding_hash,
        project_id=project_id,
        registered_project_id=registered_project_id,
        repository=repository,
        common_dir=common_dir,
        source_ref=source_ref,
        source_commit=source_commit,
        source_tree=source_tree,
        source_binding_hash=source_binding_hash,
        create_request_hash=create_request_hash,
        operation_id=operation_id,
        create_binding_hash=create_binding_hash,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        lease_fencing_token=lease_fencing_token,
        lease_binding_hash=lease_binding_hash,
    )


def _validate_route(
    route: dict[str, object],
) -> tuple[str, str, str, str, str, bool, bool, str] | None:
    model = _valid_model(route, "model")
    reasoning_effort = _text(route, "reasoning_effort")
    routing_context_hash = _valid_hash(route, "routing_context_hash")
    routing_result_hash = _valid_hash(route, "routing_result_hash")
    session_scope = _text(route, "session_scope")
    inherit_current_session_model = _bool(route, "inherit_current_session_model")
    require_explicit_route = _bool(route, "require_explicit_route")
    route_binding_hash = _valid_hash(route, "route_binding_hash")
    if (
        model is None
        or reasoning_effort not in _VALID_EFFORTS
        or routing_context_hash is None
        or routing_result_hash is None
        or session_scope != "external"
        or inherit_current_session_model is not False
        or require_explicit_route is not True
        or route_binding_hash is None
    ):
        return None
    if reasoning_effort == "ultra" or "spark" in model.casefold():
        return None
    return (
        model,
        reasoning_effort,
        routing_context_hash,
        routing_result_hash,
        session_scope,
        inherit_current_session_model,
        require_explicit_route,
        route_binding_hash,
    )


def _validate_source(
    source: dict[str, object],
) -> tuple[str, str, str, str, str, str, str, str] | None:
    project_id = _valid_identifier(source, "project_id")
    registered_project_id = _valid_identifier(source, "registered_project_id")
    repository = _canonical_repository(_text(source, "repository"))
    common_dir = _canonical_common_dir(_text(source, "common_dir"))
    source_ref = _canonical_ref(_text(source, "ref"))
    source_commit = _git_object_id(_text(source, "commit"))
    source_tree = _git_object_id(_text(source, "tree"))
    source_binding_hash = _valid_hash(source, "source_binding_hash")
    if (
        project_id is None
        or registered_project_id is None
        or project_id != registered_project_id
        or repository is None
        or common_dir is None
        or source_ref is None
        or source_commit is None
        or source_tree is None
        or source_binding_hash is None
    ):
        return None
    return (
        project_id,
        registered_project_id,
        repository,
        common_dir,
        source_ref,
        source_commit,
        source_tree,
        source_binding_hash,
    )


def _validate_predecessor(
    predecessor: dict[str, object],
    *,
    projection_hash: str,
    source_plan_hash: str,
    workflow_hash: str,
    assignment_id: str,
    assignment_token: str,
    routing_result_hash: str,
) -> tuple[str, str, str, int, int, str] | None:
    if _text(predecessor, "schema") != _PREDECESSOR_SCHEMA:
        return None
    predecessor_hash = _valid_hash(predecessor, "predecessor_hash")
    task_id = _valid_identifier(predecessor, "task_id")
    role = _valid_identifier(predecessor, "role")
    lease_epoch = _positive_int(predecessor, "lease_epoch")
    ledger_epoch = _positive_int(predecessor, "ledger_epoch")
    active_lease_set_hash = _valid_hash(predecessor, "active_lease_set_hash")
    if (
        predecessor_hash is None
        or task_id is None
        or role is None
        or lease_epoch is None
        or ledger_epoch is None
        or active_lease_set_hash is None
    ):
        return None
    expected_fields = {
        "projection_hash": projection_hash,
        "source_plan_hash": source_plan_hash,
        "workflow_hash": workflow_hash,
        "assignment_id": assignment_id,
        "assignment_token": assignment_token,
        "routing_result_hash": routing_result_hash,
    }
    if any(predecessor[field] != value for field, value in expected_fields.items()):
        return None
    return (
        task_id,
        role,
        predecessor_hash,
        lease_epoch,
        ledger_epoch,
        active_lease_set_hash,
    )


def _validate_capability_facts(
    value: object,
) -> tuple[ParsedHostCapabilityFact, ...] | None:
    """Parse candidate-provided capability claim vocabulary without verifying it."""

    if type(value) is not list or not 1 <= len(value) <= 16:
        return None
    facts: list[ParsedHostCapabilityFact] = []
    seen_routes: set[tuple[str, str]] = set()
    for raw_fact in value:
        fact = _bound_mapping(
            raw_fact,
            _CAPABILITY_FACT_KEYS,
            "capability_binding_hash",
        )
        if fact is None:
            return None
        model = _valid_model(fact, "model")
        reasoning_effort = _text(fact, "reasoning_effort")
        state = _text(fact, "state")
        attestation_hash = _valid_hash(fact, "attestation_hash")
        capability_binding_hash = _valid_hash(fact, "capability_binding_hash")
        route_key = (model, reasoning_effort) if model and reasoning_effort else None
        if (
            model is None
            or reasoning_effort not in _VALID_EFFORTS
            or state != "attested"
            or attestation_hash is None
            or capability_binding_hash is None
            or route_key is None
            or route_key in seen_routes
        ):
            return None
        seen_routes.add(route_key)
        facts.append(
            ParsedHostCapabilityFact(
                model=model,
                reasoning_effort=reasoning_effort,
                state=state,
                attestation_hash=attestation_hash,
                capability_binding_hash=capability_binding_hash,
            )
        )
    return tuple(facts)


def _has_candidate_capability_claim(
    facts: tuple[ParsedHostCapabilityFact, ...],
    model: str,
    reasoning_effort: str,
) -> bool:
    return any(
        fact.model == model
        and fact.reasoning_effort == reasoning_effort
        and fact.state == "attested"
        for fact in facts
    )


def matches_host_execution_expectation(
    parsed: object,
    expectation: object,
) -> bool:
    """Compare public fields exactly; a match is never an authorization."""

    if (
        type(parsed) is not ParsedHostExecutionIntent
        or type(expectation) is not HostExecutionExpectationProjection
    ):
        return False
    parsed_intent = cast(ParsedHostExecutionIntent, parsed)
    expectation_projection = cast(HostExecutionExpectationProjection, expectation)
    if not _is_expectation_projection(expectation_projection):
        return False
    expected_capability_facts = tuple(
        HostCapabilityExpectation(
            model=fact.model,
            reasoning_effort=fact.reasoning_effort,
            state=fact.state,
            attestation_hash=fact.attestation_hash,
        )
        for fact in parsed_intent.parsed_capability_facts
    )
    return (
        expectation_projection.candidate_intent_hash == parsed_intent.intent_hash
        and expectation_projection.projection_hash == parsed_intent.projection_hash
        and expectation_projection.source_plan_hash == parsed_intent.source_plan_hash
        and expectation_projection.workflow_hash == parsed_intent.workflow_hash
        and expectation_projection.assignment_id == parsed_intent.assignment_id
        and expectation_projection.assignment_token == parsed_intent.assignment_token
        and expectation_projection.predecessor_hash == parsed_intent.predecessor_hash
        and expectation_projection.task_id == parsed_intent.task_id
        and expectation_projection.role == parsed_intent.role
        and expectation_projection.model == parsed_intent.model
        and expectation_projection.reasoning_effort == parsed_intent.reasoning_effort
        and expectation_projection.routing_context_hash
        == parsed_intent.routing_context_hash
        and expectation_projection.routing_result_hash
        == parsed_intent.routing_result_hash
        and expectation_projection.session_scope == parsed_intent.session_scope
        and expectation_projection.inherit_current_session_model
        == parsed_intent.inherit_current_session_model
        and expectation_projection.require_explicit_route
        == parsed_intent.require_explicit_route
        and expectation_projection.capability_facts == expected_capability_facts
        and expectation_projection.ledger_epoch == parsed_intent.ledger_epoch
        and expectation_projection.active_lease_set_hash
        == parsed_intent.active_lease_set_hash
        and expectation_projection.task_packet_hash == parsed_intent.task_packet_hash
        and expectation_projection.input_packet_hash == parsed_intent.input_packet_hash
        and expectation_projection.index_packet_hash == parsed_intent.index_packet_hash
        and expectation_projection.project_id == parsed_intent.project_id
        and expectation_projection.registered_project_id
        == parsed_intent.registered_project_id
        and expectation_projection.repository == parsed_intent.repository
        and expectation_projection.common_dir == parsed_intent.common_dir
        and expectation_projection.source_ref == parsed_intent.source_ref
        and expectation_projection.source_commit == parsed_intent.source_commit
        and expectation_projection.source_tree == parsed_intent.source_tree
        and expectation_projection.create_request_hash
        == parsed_intent.create_request_hash
        and expectation_projection.operation_id == parsed_intent.operation_id
        and expectation_projection.lease_owner == parsed_intent.lease_owner
        and expectation_projection.lease_epoch == parsed_intent.lease_epoch
        and expectation_projection.lease_fencing_token
        == parsed_intent.lease_fencing_token
    )


def _is_expectation_projection(
    expectation: HostExecutionExpectationProjection,
) -> bool:
    """Validate comparison fields without simulating a Host trust decision."""

    if not _capability_expectations_are_valid(expectation.capability_facts):
        return False
    hash_values = (
        expectation.candidate_intent_hash,
        expectation.projection_hash,
        expectation.source_plan_hash,
        expectation.workflow_hash,
        expectation.assignment_id,
        expectation.assignment_token,
        expectation.predecessor_hash,
        expectation.routing_context_hash,
        expectation.routing_result_hash,
        expectation.active_lease_set_hash,
        expectation.task_packet_hash,
        expectation.input_packet_hash,
        expectation.index_packet_hash,
        expectation.create_request_hash,
        expectation.lease_fencing_token,
    )
    return (
        all(_is_hash_value(value) for value in hash_values)
        and _is_identifier_value(expectation.task_id)
        and _is_identifier_value(expectation.role)
        and _is_model_value(expectation.model)
        and expectation.reasoning_effort in _VALID_EFFORTS
        and expectation.session_scope == "external"
        and expectation.inherit_current_session_model is False
        and expectation.require_explicit_route is True
        and _is_positive_int_value(expectation.ledger_epoch)
        and _is_identifier_value(expectation.project_id)
        and _is_identifier_value(expectation.registered_project_id)
        and expectation.project_id == expectation.registered_project_id
        and _canonical_repository(_as_text(expectation.repository)) is not None
        and _canonical_common_dir(_as_text(expectation.common_dir)) is not None
        and _canonical_ref(_as_text(expectation.source_ref)) is not None
        and _git_object_id(_as_text(expectation.source_commit)) is not None
        and _git_object_id(_as_text(expectation.source_tree)) is not None
        and _is_identifier_value(expectation.operation_id)
        and _is_identifier_value(expectation.lease_owner)
        and _is_positive_int_value(expectation.lease_epoch)
    )


def _capability_expectations_are_valid(value: object) -> bool:
    if type(value) is not tuple or not 1 <= len(value) <= 16:
        return False
    seen_routes: set[tuple[str, str]] = set()
    for fact in value:
        if type(fact) is not HostCapabilityExpectation:
            return False
        expected_fact = cast(HostCapabilityExpectation, fact)
        route_key = (expected_fact.model, expected_fact.reasoning_effort)
        if (
            not _is_model_value(expected_fact.model)
            or expected_fact.reasoning_effort not in _VALID_EFFORTS
            or expected_fact.state != "attested"
            or not _is_hash_value(expected_fact.attestation_hash)
            or route_key in seen_routes
        ):
            return False
        seen_routes.add(route_key)
    return True


def _validate_packets(
    packets: dict[str, object],
    *,
    assignment_id: str,
    assignment_token: str,
    routing_context_hash: str,
) -> bool:
    for field in ("task_packet_hash", "input_packet_hash", "index_packet_hash"):
        if _valid_hash(packets, field) is None:
            return False
    return (
        _valid_hash(packets, "packet_binding_hash") is not None
        and packets["assignment_id"] == assignment_id
        and packets["assignment_token"] == assignment_token
        and packets["routing_context_hash"] == routing_context_hash
    )


def _validate_lease(
    lease: dict[str, object],
    *,
    assignment_id: str,
    assignment_token: str,
    predecessor_hash: str,
    lease_epoch: int,
    ledger_epoch: int,
    active_lease_set_hash: str,
) -> bool:
    return (
        _valid_identifier(lease, "owner") is not None
        and _valid_hash(lease, "fencing_token") is not None
        and _valid_hash(lease, "lease_binding_hash") is not None
        and _positive_int(lease, "epoch") == lease_epoch
        and lease["assignment_id"] == assignment_id
        and lease["assignment_token"] == assignment_token
        and lease["predecessor_hash"] == predecessor_hash
        and lease["ledger_epoch"] == ledger_epoch
        and lease["active_lease_set_hash"] == active_lease_set_hash
    )


def _validate_create(
    create: dict[str, object],
    *,
    project_id: str,
    repository: str,
    common_dir: str,
    source_ref: str,
    source_commit: str,
    source_tree: str,
    assignment_id: str,
    assignment_token: str,
    lease_fencing_token: str | None,
) -> bool:
    return (
        _valid_hash(create, "request_hash") is not None
        and _valid_identifier(create, "operation_id") is not None
        and _valid_hash(create, "create_binding_hash") is not None
        and lease_fencing_token is not None
        and create["project_id"] == project_id
        and create["repository"] == repository
        and create["common_dir"] == common_dir
        and create["ref"] == source_ref
        and create["commit"] == source_commit
        and create["tree"] == source_tree
        and create["assignment_id"] == assignment_id
        and create["assignment_token"] == assignment_token
        and create["lease_fencing_token"] == lease_fencing_token
    )


def _bound_mapping(
    value: object,
    expected_keys: frozenset[str],
    binding_field: str,
) -> dict[str, object] | None:
    mapping = _exact_mapping(value, expected_keys)
    if mapping is None:
        return None
    binding_hash = _valid_hash(mapping, binding_field)
    if binding_hash is None:
        return None
    unbound = dict(mapping)
    del unbound[binding_field]
    return mapping if _canonical_hash(unbound) == binding_hash else None


def _exact_mapping(
    value: object,
    expected_keys: frozenset[str],
) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    mapping = cast(dict[str, object], value)
    return mapping if set(mapping) == expected_keys else None


def _text(mapping: dict[str, object], field: str) -> str | None:
    value = mapping[field]
    return value if type(value) is str else None


def _bool(mapping: dict[str, object], field: str) -> bool | None:
    value = mapping[field]
    return value if type(value) is bool else None


def _non_negative_int(mapping: dict[str, object], field: str) -> int | None:
    value = mapping[field]
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        return None
    return cast(int, value)


def _positive_int(mapping: dict[str, object], field: str) -> int | None:
    value = _non_negative_int(mapping, field)
    return value if value is not None and value > 0 else None


def _as_text(value: object) -> str | None:
    return value if type(value) is str else None


def _is_hash_value(value: object) -> bool:
    text = _as_text(value)
    return text is not None and _HASH_PATTERN.fullmatch(text) is not None


def _is_identifier_value(value: object) -> bool:
    text = _as_text(value)
    return text is not None and _IDENTIFIER_PATTERN.fullmatch(text) is not None


def _is_model_value(value: object) -> bool:
    text = _as_text(value)
    return text is not None and _is_identifier_value(text) and text.startswith("gpt-")


def _is_positive_int_value(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def _valid_hash(mapping: dict[str, object], field: str) -> str | None:
    value = _text(mapping, field)
    return value if value is not None and _HASH_PATTERN.fullmatch(value) else None


def _valid_identifier(mapping: dict[str, object], field: str) -> str | None:
    value = _text(mapping, field)
    return value if value is not None and _IDENTIFIER_PATTERN.fullmatch(value) else None


def _valid_model(mapping: dict[str, object], field: str) -> str | None:
    value = _valid_identifier(mapping, field)
    return value if value is not None and value.startswith("gpt-") else None


def _canonical_repository(value: str | None) -> str | None:
    if (
        value is None
        or len(value) > 512
        or value.startswith(("/", "."))
        or "\\" in value
        or "//" in value
        or "/../" in value
        or value.endswith("/..")
        or any(
            not _IDENTIFIER_PATTERN.fullmatch(segment) for segment in value.split("/")
        )
    ):
        return None
    return value


def _canonical_common_dir(value: str | None) -> str | None:
    if (
        value is None
        or not _COMMON_DIR_PATTERN.fullmatch(value)
        or "//" in value
        or "/../" in value
        or value.endswith("/..")
    ):
        return None
    return value


def _canonical_ref(value: str | None) -> str | None:
    if (
        value is None
        or not value.startswith("refs/")
        or len(value) > 255
        or "//" in value
        or "/../" in value
        or value.endswith("/..")
        or any(
            not _IDENTIFIER_PATTERN.fullmatch(segment) for segment in value.split("/")
        )
    ):
        return None
    return value


def _git_object_id(value: str | None) -> str | None:
    return value if value is not None and _GIT_OBJECT_PATTERN.fullmatch(value) else None


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
