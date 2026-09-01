from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))


def _adapter() -> object:
    return importlib.import_module("devkit_runtime.fastlane_host_adapter")


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _canonical_hash(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


def _authenticated_v5_fixture() -> dict[str, object]:
    from devkit_fastlane.scripts import fastlane_routing, team_efficiency

    hash_a = _canonical_hash({"fixture": "a"})
    hash_b = _canonical_hash({"fixture": "b"})
    source_plan_hash = _canonical_hash({"fixture": "plan"})
    dependency: dict[str, object] = {
        "schema": "2718lab-devkit/dependency-state-v1",
        "graph_epoch": 1,
        "direct_dependency_ids": [],
        "completed_dependency_ids": [],
    }
    dependency["dependency_state_hash"] = _canonical_hash(dependency)
    scheduler = {
        "event_seq": 1,
        "route_epoch": 1,
        "override_epoch": 0,
        "recovery_epoch": 0,
        "ready_event_seq": 1,
        "dispatch_cause": "task_ready",
        "transport_state": "connected",
        "execution_state": "unknown",
        "lease_state": "unclaimed",
        "evidence_state": "none",
        "lease_epoch": 0,
        "recovery_probe_count_epoch": 0,
        "fence_count_epoch": 0,
        "fenced_replacement_count_task": 0,
    }
    host = {
        "schema": "2718lab-devkit/host-capabilities-v1",
        "host_id_hash": hash_a,
        "capability_epoch": 1,
        "total_slots": 4,
        "model_slot_limits": {"luna": 4, "terra": 0, "sol": 0, "spark": 0},
        "models": [
            {
                "model_id": "gpt-5.6-luna",
                "status": "available",
                "efforts": ["max"],
            }
        ],
        "entitlements": [],
    }
    unit = {
        "task": {
            "schema": "2718lab-devkit/task-routing-profile-v5",
            "task_id": "TASK-V5",
            "role": "execution",
            "access": "workspace_write",
            "write_scope_count": 1,
            "write_scope_breadth": "single_file",
            "read_scope_count": 0,
            "read_scope_breadth": "none",
            "overlap_risk": "none",
            "overlap_count": 0,
            "dependency_depth": 0,
            "downstream_critical_count": 0,
            "critical_path": False,
            "criticality": "low",
            "cross_module": False,
            "database_work": False,
            "migration": False,
            "security_sensitive": False,
            "destructive": False,
            "external_boundary": False,
            "architecture_conflict": False,
            "design_ambiguity": False,
            "verification_cost": "none",
            "blocker_severity": "none",
            "authorization": "not_required",
            "authorization_evidence_hash": None,
            "narrow_decoupling_eligible": False,
            "strike": None,
            "gate_matrix_hash": hash_a,
            "profile_evidence_hash": hash_b,
        },
        "dependency_state": dependency,
        "write_scope": ["src/task_v5.py"],
        "concurrency_mode": "parallel",
        "dispatch_order": 0,
        "index_context_hash": hash_a,
        "predecessor_hash": hash_b,
    }
    planner = team_efficiency._authenticated_v5_helper_module(
        "authenticated_v5_planner"
    )
    normalized_unit = team_efficiency._authenticated_v5_units([unit])[0]
    unit["task"]["profile_evidence_hash"] = team_efficiency._sha256_json(
        planner._routing_profile_material(source_plan_hash, normalized_unit)
    )
    routing_requests = team_efficiency.prepare_authenticated_v5_routing_requests(
        [unit],
        source_plan_hash=source_plan_hash,
        host_capabilities=host,
        scheduler_facts=scheduler,
    )
    request_binding_hash = fastlane_routing.v5_request_binding_hash(
        routing_requests[0]
    )
    attestation: dict[str, object] = {
        "schema": "2718lab-devkit/host-child-route-attestation-v1",
        "status": "attested",
        "request_binding_hash": request_binding_hash,
        "host_id_hash": hash_a,
        "capability_epoch": 1,
        "lease_epoch": 0,
        "issued_event_seq": 1,
        "expires_event_seq": 1,
        "route": {
            "lane": "luna",
            "model": "gpt-5.6-luna",
            "effort": "max",
            "rank": 40,
        },
        "inherit_current_session_model": False,
        "refusal_code": None,
    }
    attestation["attestation_hash"] = _canonical_hash(attestation)
    attestation_items = [
        {
            "task_id": "TASK-V5",
            "request_binding_hash": request_binding_hash,
            "attestation": attestation,
        }
    ]
    compiled = team_efficiency.compile_authenticated_v5_assignment_skeletons(
        [unit],
        source_plan_hash=source_plan_hash,
        routing_requests=routing_requests,
        attestation_items=attestation_items,
    )
    index_query = {
        "schema": "2718lab-devkit/project-index-attestation-v1",
        "operation": "query",
        "correlation_id": "index-" + "c" * 64,
        "workspace_id": _hash("d"),
        "workspace_binding_hash": _hash("e"),
        "root_identity_hash": _hash("f"),
        "expires_at": 1_700_000_120,
        "snapshot_id": _hash("0"),
        "snapshot_attestation_hash": _hash("1"),
        "head_hash": _hash("2"),
        "manifest_hash": _hash("3"),
        "parser_set_hash": _hash("5"),
        "query_receipt_hash": _hash("6"),
        "index_context_hash": hash_a,
    }
    index_query["attestation_hash"] = _canonical_hash(index_query)
    index_ref = {
        key: value
        for key, value in index_query.items()
        if key
        not in {
            "schema",
            "operation",
            "expires_at",
            "head_hash",
            "manifest_hash",
            "parser_set_hash",
        }
    }
    index_ref["task_id"] = "TASK-V5"
    planner_request = {
        "schema": "2718lab-devkit/fastlane-host-planner-request-v1",
        "action": "plan_dispatch",
        "assignment_skeletons": compiled["assignment_skeletons"],
        "project_index_attestation_refs": [index_ref],
    }
    return {
        "call_intent_hash": "a" * 64,
        "preparation_id": "dispatch-v5-1",
        "host": host,
        "scheduler": scheduler,
        "unit": unit,
        "source_plan_hash": source_plan_hash,
        "routing_requests": routing_requests,
        "attestation_items": attestation_items,
        "compiled": compiled,
        "index_query": index_query,
        "index_ref": index_ref,
        "planner_request": planner_request,
    }


def _v5_dispatch_fact(adapter: object, fixture: dict[str, object]) -> object:
    skeleton = fixture["compiled"]["assignment_skeletons"][0]
    proof = skeleton["routing_proof"]
    result_route = proof["result"]["route"]
    return adapter._HostDispatchFact(
        task_id=skeleton["task_id"],
        route=adapter._HostDispatchRoute(
            model=result_route["model"],
            reasoning_effort=result_route["effort"],
            routing_context_hash=proof["routing_context_hash"],
            routing_result_hash=proof["routing_result_hash"],
            require_explicit_route=True,
        ),
        lease_id="lease-task-v5",
        lease_epoch=1,
        task_version=1,
        assignment_token=_hash("3"),
        write_scope=tuple(skeleton["write_scope"]),
        concurrency_mode=skeleton["concurrency_mode"],
        dispatch_order=skeleton["dispatch_order"],
        index_context_hash=skeleton["index_context_hash"],
        worktree_identity=_hash("5"),
        worktree_base=_hash("6"),
        integration_head=_hash("7"),
        predecessor_hash=skeleton["predecessor_hash"],
        source_plan_hash=skeleton["source_plan_hash"],
        ledger_epoch=11,
        active_lease_set_hash=_hash("b"),
    )


def _pipe_pair() -> tuple[object, object]:
    from devkit_runtime.host_bridge import InheritedHandleHostBridge

    child_to_host_read, child_to_host_write = os.pipe()
    host_to_child_read, host_to_child_write = os.pipe()
    key = b"k" * 32
    nonce = b"fastlane-dispatch-private-nonce"
    return (
        InheritedHandleHostBridge.from_file_descriptors(
            read_fd=host_to_child_read,
            write_fd=child_to_host_write,
            session_key=key,
            session_nonce=nonce,
        ),
        InheritedHandleHostBridge.from_file_descriptors(
            read_fd=child_to_host_read,
            write_fd=host_to_child_write,
            session_key=key,
            session_nonce=nonce,
        ),
    )


def _dispatch_fact(adapter: object, *, task: str, scope: str, order: int = 0) -> object:
    task_hash_character = "a" if task.endswith("a") else "b"
    return adapter._HostDispatchFact(
        task_id=task,
        route=adapter._HostDispatchRoute(
            model="gpt-5.6-terra",
            reasoning_effort="high",
            routing_context_hash=_hash("1"),
            routing_result_hash=_hash(task_hash_character),
            require_explicit_route=True,
        ),
        lease_id=f"lease-{task}",
        lease_epoch=7,
        task_version=3,
        assignment_token=_hash("3"),
        write_scope=(scope,),
        concurrency_mode="parallel",
        dispatch_order=order,
        index_context_hash=_hash("4"),
        worktree_identity=_hash("5"),
        worktree_base=_hash("6"),
        integration_head=_hash("7"),
        predecessor_hash=_hash("8"),
        source_plan_hash=_hash("9"),
        ledger_epoch=11,
        active_lease_set_hash=_hash("a"),
    )


def _dispatch_request(adapter: object, facts: tuple[object, ...]) -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/fastlane-host-dispatch-request-v1",
        "action": "dispatch_all",
        "assignments": [adapter._dispatch_fact_mapping(fact) for fact in facts],
    }


def _planner_request(adapter: object, facts: tuple[object, ...]) -> dict[str, object]:
    skeletons = []
    references = []
    for fact in facts:
        mapping = adapter._dispatch_fact_mapping(fact)
        skeletons.append(
            {
                key: mapping[key]
                for key in (
                    "task_id",
                    "route",
                    "write_scope",
                    "concurrency_mode",
                    "dispatch_order",
                    "index_context_hash",
                    "predecessor_hash",
                    "source_plan_hash",
                    "ledger_epoch",
                    "active_lease_set_hash",
                )
            }
        )
        references.append(
            {
                "task_id": mapping["task_id"],
                "correlation_id": "index-" + "c" * 64,
                "workspace_id": _hash("d"),
                "workspace_binding_hash": _hash("e"),
                "root_identity_hash": _hash("f"),
                "snapshot_id": _hash("0"),
                "snapshot_attestation_hash": _hash("1"),
                "query_receipt_hash": _hash("2"),
                "index_context_hash": mapping["index_context_hash"],
                "attestation_hash": _hash("3"),
            }
        )
    return {
        "schema": "2718lab-devkit/fastlane-host-planner-request-v1",
        "action": "plan_dispatch",
        "assignment_skeletons": skeletons,
        "project_index_attestation_refs": references,
    }


def _prepared_dispatch(
    adapter: object,
    facts: tuple[object, ...],
    *,
    trusted_facts: tuple[object, ...] | None = None,
    trusted_lease_hashes: tuple[str, ...] | None = None,
    trusted_dispatch_hashes: tuple[str, ...] | None = None,
) -> tuple[object, object, object]:
    import devkit_runtime.host_session as host_session
    from devkit_runtime.host_envelopes import EnvelopeBinding

    request = _dispatch_request(adapter, facts)
    child, host = _pipe_pair()
    binding = EnvelopeBinding(
        task_id="task-1",
        lease_epoch=7,
        assignment_token=_hash("b"),
        dispatch_context_hash=_hash("c"),
        route_hash=_hash("d"),
        expires_at=1_700_000_060,
    )
    route = host_session.HostRoute(model="gpt-5.6-terra", effort="high")
    resolver_facts = facts if trusted_facts is None else trusted_facts

    def reply() -> None:
        probe = host.receive_capability_probe(now=1_700_000_000, expected=binding)
        host.send_capability_report(
            probe=probe,
            capability_hashes={name: _hash("e") for name in probe.capability_names},
            now=1_700_000_000,
        )

    thread = threading.Thread(target=reply, daemon=True)
    thread.start()
    session = host_session.HostSession(
        bridge=child,
        clock=lambda: 1_700_000_000,
        compiler_evidence_provider=lambda preparation: preparation,
        compiler_invocation_resolver=lambda _preparation_id: (
            host_session._CompilerInvocationBinding(
                request_hash=_canonical_hash(request),
                reasoning_effort="high",
                verified_route_result_hashes=tuple(
                    sorted(fact.route.routing_result_hash for fact in resolver_facts)
                ),
                verified_lease_scope_bindings=tuple(
                    sorted(
                        adapter._lease_scope_binding_hash(fact)
                        for fact in resolver_facts
                    )
                    if trusted_lease_hashes is None
                    else trusted_lease_hashes
                ),
                dispatch_facts=resolver_facts,
                dispatch_binding_hashes=(
                    tuple(
                        adapter._dispatch_fact_mapping(fact)["dispatch_binding_hash"]
                        for fact in resolver_facts
                    )
                    if trusted_dispatch_hashes is None
                    else trusted_dispatch_hashes
                ),
            )
        ),
    )
    capability_facts = session.attest_routes(
        binding=binding,
        routes=(route,),
        now=1_700_000_000,
    )
    thread.join(timeout=2)
    prepared = adapter.prepare_verified_host_facts(
        session,
        capability_facts=capability_facts,
        preparation_id="dispatch-batch",
    )
    return request, prepared, (child, host)


def test_adapter_fails_closed_when_verified_host_facts_are_missing() -> None:
    adapter = _adapter()

    assert (
        adapter.compile_fast_lane_with_host_facts(
            {}, reasoning_effort="ultra", verified_host_facts=None
        )
        == adapter.NO_SAFE_WORK
    )


def test_private_host_facts_form_one_mechanical_dispatch_all_request() -> None:
    adapter = _adapter()
    facts = (
        _dispatch_fact(adapter, task="task-a", scope="src/a.py"),
        _dispatch_fact(adapter, task="task-b", scope="src/b.py", order=1),
    )
    request, prepared, bridges = _prepared_dispatch(adapter, facts)
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result["schema"] == "2718lab-devkit/fastlane-host-dispatch-batch-v1"
    assert result["action"] == "dispatch_all"
    assert result["selection_authority"] == "host_attested_compiler"
    assert result["llm_choice"] is False
    assert [item["task_id"] for item in result["assignments"]] == ["task-a", "task-b"]
    assert all(
        item["route"]["require_explicit_route"] is True
        for item in result["assignments"]
    )
    assert result["dispatch_binding_hashes"] == [
        item["dispatch_binding_hash"] for item in result["assignments"]
    ]
    assert result["batch_hash"] == _canonical_hash(
        {key: value for key, value in result.items() if key != "batch_hash"}
    )


def test_any_public_request_tamper_burns_the_entire_dispatch_batch() -> None:
    adapter = _adapter()
    facts = (_dispatch_fact(adapter, task="task-a", scope="src/a.py"),)
    request, prepared, bridges = _prepared_dispatch(adapter, facts)
    tampered = json.loads(json.dumps(request))
    tampered["assignments"][0]["lease_epoch"] = 8
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            tampered,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK


def test_dispatch_binding_hash_covers_every_assignment_field() -> None:
    adapter = _adapter()
    mapping = adapter._dispatch_fact_mapping(
        _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    )
    binding_hash = mapping.pop("dispatch_binding_hash")
    mutations = (
        (("task_id",), "task-z"),
        (("route", "model"), "gpt-5.6-luna"),
        (("route", "reasoning_effort"), "max"),
        (("route", "routing_context_hash"), _hash("b")),
        (("route", "routing_result_hash"), _hash("c")),
        (("route", "require_explicit_route"), False),
        (("lease_id",), "lease-rebound"),
        (("lease_epoch",), 8),
        (("task_version",), 4),
        (("assignment_token",), _hash("d")),
        (("write_scope",), ["src/b.py"]),
        (("concurrency_mode",), "serial"),
        (("dispatch_order",), 1),
        (("index_context_hash",), _hash("e")),
        (("worktree_identity",), _hash("f")),
        (("worktree_base",), _hash("0")),
        (("integration_head",), _hash("b")),
        (("predecessor_hash",), _hash("c")),
        (("source_plan_hash",), _hash("d")),
        (("ledger_epoch",), 12),
        (("active_lease_set_hash",), _hash("e")),
    )
    for path, replacement in mutations:
        mutated = json.loads(json.dumps(mapping))
        target = mutated
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = replacement
        assert _canonical_hash(mutated) != binding_hash, path

        valid_fact = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
        request, prepared, bridges = _prepared_dispatch(adapter, (valid_fact,))
        assignment = request["assignments"][0]
        request_target = assignment
        for part in path[:-1]:
            request_target = request_target[part]
        request_target[path[-1]] = replacement
        try:
            result = adapter.compile_fast_lane_with_host_facts(
                request,
                reasoning_effort="high",
                verified_host_facts=prepared,
            )
        finally:
            for bridge in bridges:
                bridge.close()
        assert result == adapter.NO_SAFE_WORK, path


@pytest.mark.parametrize(
    "scope",
    (
        ".",
        "..",
        "../outside.py",
        "src/name.",
        "src/alias./child.py",
        "src/*",
        "src/file?.py",
        "src/[abc].py",
        "src/space name.py",
        "src/[]",
    ),
)
def test_illegal_or_wildcard_scope_against_concrete_scope_fails_closed(
    scope: str,
) -> None:
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    request, prepared, bridges = _prepared_dispatch(adapter, (valid,))
    request["assignments"][0]["write_scope"] = [scope]
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK


def test_invalid_scope_in_trusted_host_fact_fails_after_request_hash_matches() -> None:
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    invalid = replace(valid, write_scope=("src/windows-alias.",))
    request, prepared, bridges = _prepared_dispatch(
        adapter,
        (valid,),
        trusted_facts=(invalid,),
        trusted_lease_hashes=(_hash("f"),),
        trusted_dispatch_hashes=(_hash("0"),),
    )
    assert (
        _canonical_hash(request)
        == prepared.session._compiler_evidence[prepared.evidence].request_hash
    )
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK


def test_scope_conflict_respects_segment_boundary_and_blocks_parent_escape() -> None:
    adapter = _adapter()

    assert adapter._scopes_overlap(("src/a",), ("src/ab",)) is False
    assert adapter._scopes_overlap(("src/a",), ("src/a/child.py",)) is True
    with pytest.raises(ValueError, match="write scope is invalid"):
        adapter._canonical_write_scope(("src/a/../../outside.py",))


def test_matching_request_hash_still_rejects_trusted_dispatch_binding_mismatch() -> (
    None
):
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    request, prepared, bridges = _prepared_dispatch(
        adapter,
        (valid,),
        trusted_dispatch_hashes=(_hash("0"),),
    )
    assert (
        _canonical_hash(request)
        == prepared.session._compiler_evidence[prepared.evidence].request_hash
    )
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK


def test_matching_request_hash_rejects_different_but_valid_trusted_fact() -> None:
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    rebound = replace(valid, worktree_base=_hash("0"))
    request, prepared, bridges = _prepared_dispatch(
        adapter,
        (valid,),
        trusted_facts=(rebound,),
    )
    assert (
        _canonical_hash(request)
        == prepared.session._compiler_evidence[prepared.evidence].request_hash
    )
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK


def test_request_shape_is_bounded_before_canonical_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    request, prepared, bridges = _prepared_dispatch(adapter, (valid,))
    request["assignments"][0]["route"]["model"] = "m" * 129
    canonical_called = False

    def forbidden_canonical(_value: object) -> bytes:
        nonlocal canonical_called
        canonical_called = True
        raise AssertionError("unbounded request reached canonical JSON")

    monkeypatch.setattr(adapter, "_canonical_bytes", forbidden_canonical)
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK
    assert canonical_called is False


@pytest.mark.parametrize("level", ("root", "assignment", "route"))
def test_overwide_mapping_short_circuits_before_key_iteration_or_canonical_json(
    level: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    request, prepared, bridges = _prepared_dispatch(adapter, (valid,))

    class CountingKey:
        def __init__(self) -> None:
            self.hash_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            return 987_654_321

        def __eq__(self, other: object) -> bool:
            return self is other

    target = request
    if level in {"assignment", "route"}:
        target = request["assignments"][0]
    if level == "route":
        target = target["route"]
    for index in range(2_048):
        target[f"unexpected-{index}"] = index
    counting_key = CountingKey()
    target[counting_key] = "must-not-be-visited"
    counting_key.hash_calls = 0
    canonical_called = False

    def forbidden_canonical(_value: object) -> bytes:
        nonlocal canonical_called
        canonical_called = True
        raise AssertionError("overwide mapping reached canonical JSON")

    monkeypatch.setattr(adapter, "_canonical_bytes", forbidden_canonical)
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK
    assert counting_key.hash_calls == 0
    assert canonical_called is False


def test_request_serialized_size_is_capped_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    valid = _dispatch_fact(adapter, task="task-a", scope="src/a.py")
    request, prepared, bridges = _prepared_dispatch(adapter, (valid,))
    base = request["assignments"][0]
    scopes = [f"scope{index:02d}/" + "a" * 240 for index in range(32)]
    oversized = []
    for _index in range(16):
        assignment = json.loads(json.dumps(base))
        assignment["write_scope"] = scopes
        oversized.append(assignment)
    request["assignments"] = oversized
    canonical_called = False

    def forbidden_canonical(_value: object) -> bytes:
        nonlocal canonical_called
        canonical_called = True
        raise AssertionError("oversized request reached canonical JSON")

    monkeypatch.setattr(adapter, "_canonical_bytes", forbidden_canonical)
    try:
        result = adapter.compile_fast_lane_with_host_facts(
            request,
            reasoning_effort="high",
            verified_host_facts=prepared,
        )
    finally:
        for bridge in bridges:
            bridge.close()

    assert result == adapter.NO_SAFE_WORK
    assert canonical_called is False


def test_overlapping_parallel_scopes_fail_closed_but_serial_scopes_are_ordered() -> (
    None
):
    adapter = _adapter()
    first = _dispatch_fact(adapter, task="task-a", scope="src/shared")
    overlapping = replace(
        _dispatch_fact(adapter, task="task-b", scope="src/shared/child.py"),
        concurrency_mode="parallel",
        dispatch_order=1,
    )
    request, prepared, bridges = _prepared_dispatch(adapter, (first, overlapping))
    try:
        rejected = adapter.compile_fast_lane_with_host_facts(
            request, reasoning_effort="high", verified_host_facts=prepared
        )
    finally:
        for bridge in bridges:
            bridge.close()
    assert rejected == adapter.NO_SAFE_WORK
    with pytest.raises(ValueError, match="parallel write scopes overlap"):
        adapter._validate_batch_fences(
            (replace(first, concurrency_mode="serial", dispatch_order=0), overlapping)
        )

    serial_facts = (
        replace(first, concurrency_mode="serial", dispatch_order=0),
        replace(overlapping, concurrency_mode="serial", dispatch_order=1),
    )
    request, prepared, bridges = _prepared_dispatch(adapter, serial_facts)
    try:
        accepted = adapter.compile_fast_lane_with_host_facts(
            request, reasoning_effort="high", verified_host_facts=prepared
        )
    finally:
        for bridge in bridges:
            bridge.close()
    assert [item["dispatch_order"] for item in accepted["assignments"]] == [0, 1]


def test_adapter_exposes_no_forgeable_verified_host_facts_marker() -> None:
    adapter = _adapter()

    assert not hasattr(adapter, "VerifiedHostFacts")
    assert not hasattr(adapter, "_CONSTRUCTION_CAPABILITY")


def test_unverified_capability_values_cannot_bootstrap_verified_host_facts() -> None:
    adapter = _adapter()
    from devkit_runtime.host_session import HostSession

    unavailable_session = HostSession(
        bridge=None,
        clock=lambda: 1.0,
    )

    assert (
        adapter.prepare_verified_host_facts(
            unavailable_session,
            capability_facts=(),
        )
        == adapter.NO_SAFE_WORK
    )


def test_adapter_contains_no_host_execution_calls() -> None:
    adapter = _adapter()
    tree = ast.parse(inspect.getsource(adapter))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_names.update(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )

    assert called_names.isdisjoint(
        {
            "spawn_agent",
            "create_thread",
            "fork_thread",
            "workflow_claim",
            "workflow_complete",
            "worktree",
            "checkout",
            "archive",
            "project_index_query",
        }
    )


def test_environment_session_round_trips_evidence_and_typed_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    import devkit_runtime.host_session as host_session
    from devkit_runtime.host_bridge import InheritedHandleHostBridge, OperationReceipt

    fixture = _authenticated_v5_fixture()
    child, host = _pipe_pair()
    fact = _v5_dispatch_fact(adapter, fixture)
    request = fixture["planner_request"]
    fact_mapping = adapter._dispatch_fact_mapping(fact)
    lease_hash = adapter._lease_scope_binding_hash(fact)
    received: list[OperationReceipt] = []

    def host_reply() -> None:
        assert host.receive_project_index_attestation(now=1_700_000_000) == fixture[
            "index_query"
        ]
        probe = host.receive_capability_probe_v2(now=1_700_000_000)
        host.send_capability_report_v2(
            probe=probe,
            host_capabilities=fixture["host"],
            scheduler_facts=fixture["scheduler"],
            now=1_700_000_000,
        )
        routing_request = host.receive_routing_attestation_request(
            now=1_700_000_000
        )
        assert list(routing_request.routing_requests) == fixture["routing_requests"]
        host.send_routing_attestation_response(
            request=routing_request,
            attestations=fixture["attestation_items"],
            now=1_700_000_000,
        )
        evidence_request = host.receive_compiler_evidence_request(now=1_700_000_000)
        response = {
            "schema": "2718lab-devkit/compiler-evidence-response-v1",
            "preparation_id": evidence_request.preparation_id,
            "request_hash": evidence_request.request_hash,
            "reasoning_effort": evidence_request.reasoning_effort,
            "verified_route_result_hashes": [fact.route.routing_result_hash],
            "verified_lease_scope_bindings": [lease_hash],
            "dispatch_facts": [fact_mapping],
            "dispatch_binding_hashes": [fact_mapping["dispatch_binding_hash"]],
            "nonce": evidence_request.nonce,
            "expires_at": evidence_request.expires_at,
        }
        response["registry_binding_hash"] = _canonical_hash(response)
        host.send_compiler_evidence_response(
            request=evidence_request,
            response=response,
            now=1_700_000_000,
        )
        received.append(host.receive_fast_lane_dispatch_batch(now=1_700_000_000))

    thread = threading.Thread(target=host_reply, daemon=True)
    thread.start()
    monkeypatch.setattr(
        InheritedHandleHostBridge,
        "from_environment",
        classmethod(lambda cls, environ=None, *, platform=None: child),
    )
    session = host_session.HostSession.from_environment(
        environ={}, platform="posix", clock=lambda: 1_700_000_000
    )
    assert session.send_project_index_attestation(fixture["index_query"]) == fixture[
        "index_query"
    ]
    capability = session.resolve_capability_snapshot_v2(
        call_intent_hash=fixture["call_intent_hash"],
        preparation_id=fixture["preparation_id"],
    )
    assert capability is not None
    routing = session.resolve_routing_attestations(
        call_intent_hash=fixture["call_intent_hash"],
        preparation_id=fixture["preparation_id"],
        routing_requests=fixture["routing_requests"],
    )
    assert routing is not None
    prepared = adapter.prepare_verified_host_facts(
        session,
        preparation_id=fixture["preparation_id"],
        call_intent_hash=fixture["call_intent_hash"],
        routing_registry_binding_hash=routing.routing_registry_binding_hash,
        request=request,
        reasoning_effort="max",
    )
    assert type(prepared).__name__ == "_PreparedHostFacts"
    batch = adapter.compile_fast_lane_with_host_facts(
        request,
        reasoning_effort="max",
        verified_host_facts=prepared,
    )
    assert type(batch) is dict
    first = batch["assignments"][0]
    route = first["route"]
    receipt = session.send_fast_lane_dispatch_batch(
        batch=batch,
        binding=host_session.host_envelopes.EnvelopeBinding(
            task_id=first["task_id"],
            lease_epoch=first["lease_epoch"],
            assignment_token=first["assignment_token"],
            dispatch_context_hash=first["dispatch_binding_hash"],
            route_hash=route["routing_result_hash"],
            expires_at=1_700_000_120,
        ),
        correlation_id="operation-1",
        now=1_700_000_000,
        refill_callback=lambda trigger: {"trigger": dict(trigger)},
        call_intent_hash=fixture["call_intent_hash"],
        preparation_id=fixture["preparation_id"],
    )
    thread.join(timeout=2)
    try:
        assert type(receipt) is OperationReceipt
        assert received == [receipt]
    finally:
        host.close()
        child.close()


def test_fast_lane_terminal_ack_removes_only_completed_assignment() -> None:
    adapter = _adapter()
    from devkit_runtime import host_bridge, host_envelopes
    from devkit_runtime.host_session import HostSession

    fixture = _authenticated_v5_fixture()
    fact_a = _v5_dispatch_fact(adapter, fixture)
    fact_b = replace(
        fact_a,
        task_id="TASK-V5-B",
        route=replace(
            fact_a.route,
            routing_context_hash=_hash("c"),
            routing_result_hash=_hash("d"),
        ),
        lease_id="lease-task-v5-b",
        assignment_token=_hash("e"),
        write_scope=("src/task_v5_b.py",),
        dispatch_order=1,
    )
    mappings = [
        adapter._dispatch_fact_mapping(fact) for fact in (fact_a, fact_b)
    ]
    batch: dict[str, object] = {
        "schema": "2718lab-devkit/fastlane-host-dispatch-batch-v1",
        "action": "dispatch_all",
        "selection_authority": "host_attested_compiler",
        "llm_choice": False,
        "source_plan_hash": fact_a.source_plan_hash,
        "ledger_epoch": fact_a.ledger_epoch,
        "active_lease_set_hash": fact_a.active_lease_set_hash,
        "dispatch_binding_hashes": [
            mapping["dispatch_binding_hash"] for mapping in mappings
        ],
        "assignments": mappings,
    }
    batch["batch_hash"] = _canonical_hash(batch)
    assert host_envelopes._validate_fast_lane_dispatch_batch(batch) == batch
    child, host = _pipe_pair()
    session = HostSession(bridge=child, clock=lambda: 1_700_000_000)
    first_route = mappings[0]["route"]
    binding = host_envelopes.EnvelopeBinding(
        task_id=fact_a.task_id,
        lease_epoch=fact_a.lease_epoch,
        assignment_token=fact_a.assignment_token,
        dispatch_context_hash=mappings[0]["dispatch_binding_hash"],
        route_hash=first_route["routing_result_hash"],
        expires_at=1_700_000_120,
    )
    received_dispatch: list[host_bridge.OperationReceipt] = []
    refill_receipts: list[dict[str, object]] = []

    def refill_callback(trigger: Mapping[str, object]) -> dict[str, object]:
        receipt = {"state": "NO_QUEUED_WORK", **dict(trigger)}
        refill_receipts.append(receipt)
        return receipt
    dispatch_reader = threading.Thread(
        target=lambda: received_dispatch.append(
            host.receive_fast_lane_dispatch_batch(now=1_700_000_000)
        ),
        daemon=True,
    )
    dispatch_reader.start()
    receipt = session.send_fast_lane_dispatch_batch(
        batch=batch,
        binding=binding,
        correlation_id="operation-2",
        now=1_700_000_000,
        call_intent_hash=fixture["call_intent_hash"],
        preparation_id=fixture["preparation_id"],
        refill_callback=refill_callback,
    )
    assert type(receipt) is host_bridge.OperationReceipt
    dispatch_reader.join(timeout=2)
    assert received_dispatch == [receipt]
    expected = dict(
        session._pending_fast_lane_terminals[(batch["batch_hash"], fact_a.task_id)].expected
    )
    terminal: dict[str, object] = {
        "schema": "2718lab-devkit/fastlane-worker-terminal-result-v1",
        **expected,
        "terminal": "succeeded",
        "result": ["result.verified"],
        "risk": [],
        "artifact_refs": [],
        "digest_refs": [],
        "event_seq": 1,
        "expires_at": 1_700_000_120,
    }
    terminal["terminal_receipt_hash"] = _canonical_hash(terminal)
    assert len(terminal) == 23
    assert (
        host_bridge._normalize_fast_lane_worker_terminal_result(
            terminal,
            expected=expected,
            expires_at=1_700_000_120,
            now=1_700_000_000,
        )
        == terminal
    )
    expired = {**terminal, "expires_at": 1_700_000_000}
    expired["terminal_receipt_hash"] = _canonical_hash(
        {key: value for key, value in expired.items() if key != "terminal_receipt_hash"}
    )
    with pytest.raises(
        host_bridge.HostBridgeError,
        match="HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID",
    ):
        host_bridge._normalize_fast_lane_worker_terminal_result(
            expired, expected=expected, now=1_700_000_000
        )
    invalid = {**terminal, "terminal": "cancelled"}
    invalid["terminal_receipt_hash"] = _canonical_hash(
        {key: value for key, value in invalid.items() if key != "terminal_receipt_hash"}
    )
    with pytest.raises(
        host_bridge.HostBridgeError,
        match="HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID",
    ):
        host_bridge._normalize_fast_lane_worker_terminal_result(
            invalid,
            expected=expected,
            expires_at=1_700_000_120,
            now=1_700_000_000,
        )
    correlation_id = "terminal-" + "d" * 64
    host.send_fast_lane_worker_terminal_result(
        terminal_result=terminal,
        correlation_id=correlation_id,
        expected=expected,
        expires_at=1_700_000_120,
        now=1_700_000_000,
    )
    ack = host.receive_fast_lane_worker_terminal_ack(
        terminal_result=terminal, correlation_id=correlation_id
    )
    assert type(ack) is dict and len(ack) == 9
    deadline = time.monotonic() + 2
    while not refill_receipts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(refill_receipts) == 1
    assert ack["refill_trigger_hash"] == refill_receipts[0]["refill_trigger_hash"]
    assert (batch["batch_hash"], fact_a.task_id) not in session._pending_fast_lane_terminals
    assert (batch["batch_hash"], fact_b.task_id) in session._pending_fast_lane_terminals
    close_started = time.monotonic()
    session.close()
    assert time.monotonic() - close_started < 0.5
    host.close()


def test_compiler_evidence_cross_language_fixed_vector() -> None:
    from devkit_runtime import host_bridge

    vector = json.loads(
        (MCP_TOOLS / "tests" / "compiler_evidence_vector.json").read_text(
            encoding="utf-8"
        )
    )
    request = host_bridge._normalize_compiler_evidence_request(
        vector["request"], now=1_700_000_000
    )
    assert len(vector["request"]) == 11
    assert set(vector["request"]["assignment_skeletons"][0]) == {
        "task_id",
        "routing_proof",
        "write_scope",
        "concurrency_mode",
        "dispatch_order",
        "index_context_hash",
        "predecessor_hash",
        "source_plan_hash",
    }
    assert host_bridge._normalize_compiler_evidence_response(
        vector["response"], request=request, now=1_700_000_000
    ) == vector["response"]
    assert vector["frame"]["canonical_payload_hash"] == _canonical_hash(
        vector["request"]
    )
    for item in vector["project_index_attestations"]:
        assert host_bridge._normalize_project_index_attestation(
            item["payload"], now=1_700_000_000
        ) == item["payload"]
    read_fd, write_fd = os.pipe()
    bridge = host_bridge.InheritedHandleHostBridge.from_file_descriptors(
        read_fd=read_fd,
        write_fd=write_fd,
        session_key=bytes.fromhex(vector["frame"]["session_key_hex"]),
        session_nonce=bytes.fromhex(vector["frame"]["session_nonce_hex"]),
        owns_descriptors=True,
    )
    try:
        frame = bridge._frame_bytes(
            kind=vector["frame"]["kind"],
            action_id=vector["frame"]["action_id"],
            sequence=vector["frame"]["sequence"],
            payload=vector["request"],
        )
        assert json.loads(frame[4:])["mac"] == vector["frame"]["mac"]
        for item in vector["project_index_attestations"]:
            sideband = bridge._frame_bytes(
                kind="project_index_attestation",
                action_id=item["payload"]["correlation_id"],
                sequence=item["sequence"],
                payload=item["payload"],
            )
            assert json.loads(sideband[4:])["mac"] == item["mac"]
    finally:
        bridge.close()


def test_registry_hash_tamper_never_issues_compiler_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    import devkit_runtime.host_session as host_session
    from devkit_runtime.host_bridge import InheritedHandleHostBridge

    child, host = _pipe_pair()
    fact = _dispatch_fact(adapter, task="task-1", scope="src/a.py")
    request = _planner_request(adapter, (fact,))
    fact_mapping = adapter._dispatch_fact_mapping(fact)

    def host_reply() -> None:
        evidence_request = host.receive_compiler_evidence_request(now=1_700_000_000)
        host._send_private(
            kind="compiler_evidence_response",
            action_id=evidence_request.preparation_id,
            payload={
                "schema": "2718lab-devkit/compiler-evidence-response-v1",
                "preparation_id": evidence_request.preparation_id,
                "request_hash": evidence_request.request_hash,
                "reasoning_effort": evidence_request.reasoning_effort,
                "verified_route_result_hashes": [fact.route.routing_result_hash],
                "verified_lease_scope_bindings": [
                    adapter._lease_scope_binding_hash(fact)
                ],
                "dispatch_facts": [fact_mapping],
                "dispatch_binding_hashes": [fact_mapping["dispatch_binding_hash"]],
                "nonce": evidence_request.nonce,
                "expires_at": evidence_request.expires_at,
                "registry_binding_hash": _hash("0"),
            },
        )

    thread = threading.Thread(target=host_reply, daemon=True)
    thread.start()
    monkeypatch.setattr(
        InheritedHandleHostBridge,
        "from_environment",
        classmethod(lambda cls, environ=None, *, platform=None: child),
    )
    session = host_session.HostSession.from_environment(
        environ={}, platform="posix", clock=lambda: 1_700_000_000
    )
    try:
        assert (
            adapter.prepare_verified_host_facts(
                session,
                preparation_id="dispatch-private-2",
                request=request,
                reasoning_effort="high",
            )
            == adapter.NO_SAFE_WORK
        )
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()


@pytest.mark.parametrize(
    ("kind", "sender_role", "recipient_role"),
    (
        ("coordinator_to_worker", "coordinator", "worker"),
        ("worker_to_coordinator", "worker", "coordinator"),
        ("peer_handoff", "peer", "peer"),
    ),
)
def test_role_transfers_are_hash_only_bounded_projections(
    kind: str, sender_role: str, recipient_role: str
) -> None:
    adapter = _adapter()

    transfer = adapter.project_role_transfer(
        kind=kind,
        task_id="FAST-LANE-ADAPTER",
        role="execution",
        assignment_token=_hash("a"),
        context_hash=_hash("b"),
        summary_hash=_hash("c"),
        artifact_hashes=(_hash("d"),),
        digest_hashes=(_hash("e"),),
    )

    assert transfer["sender_role"] == sender_role
    assert transfer["recipient_role"] == recipient_role
    assert transfer["summary_hash"] == _hash("c")
    assert transfer["artifact_hashes"] == [_hash("d")]
    assert transfer["digest_hashes"] == [_hash("e")]
    assert "raw" not in transfer


def test_role_transfer_rejects_raw_or_path_like_content() -> None:
    adapter = _adapter()
    common = {
        "kind": "worker_to_coordinator",
        "task_id": "FAST-LANE-ADAPTER",
        "role": "execution",
        "assignment_token": _hash("a"),
        "context_hash": _hash("b"),
        "summary_hash": _hash("c"),
        "artifact_hashes": (_hash("d"),),
        "digest_hashes": (_hash("e"),),
    }

    assert (
        adapter.project_role_transfer(
            **{**common, "summary_hash": "raw prompt or secret"}
        )
        == adapter.NO_SAFE_WORK
    )
    assert (
        adapter.project_role_transfer(
            **{**common, "artifact_hashes": (r"D:\\private\\raw.log",)}
        )
        == adapter.NO_SAFE_WORK
    )


def test_role_transfer_fails_closed_when_hash_sequence_raises() -> None:
    adapter = _adapter()

    class ExplodingSequence(Sequence[str]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> str:
            del index
            raise RuntimeError("untrusted sequence iteration")

    assert (
        adapter.project_role_transfer(
            kind="worker_to_coordinator",
            task_id="FAST-LANE-ADAPTER",
            role="execution",
            assignment_token=_hash("a"),
            context_hash=_hash("b"),
            summary_hash=_hash("c"),
            artifact_hashes=ExplodingSequence(),
            digest_hashes=(_hash("e"),),
        )
        == adapter.NO_SAFE_WORK
    )
