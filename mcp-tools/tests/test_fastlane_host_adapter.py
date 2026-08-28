from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
import sys
import threading
from collections.abc import Sequence
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


def _dispatch_fact(adapter: object, *, task: str, scope: str) -> object:
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
        dispatch_order=0,
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
        _dispatch_fact(adapter, task="task-b", scope="src/b.py"),
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
            (replace(first, concurrency_mode="serial", dispatch_order=1), overlapping)
        )

    serial_facts = (
        replace(first, concurrency_mode="serial", dispatch_order=1),
        replace(overlapping, concurrency_mode="serial", dispatch_order=2),
    )
    request, prepared, bridges = _prepared_dispatch(adapter, serial_facts)
    try:
        accepted = adapter.compile_fast_lane_with_host_facts(
            request, reasoning_effort="high", verified_host_facts=prepared
        )
    finally:
        for bridge in bridges:
            bridge.close()
    assert [item["dispatch_order"] for item in accepted["assignments"]] == [1, 2]


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
