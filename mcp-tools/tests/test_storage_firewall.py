from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (MCP_TOOLS, TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


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
    """Build a canonical legacy V5 skeleton without any storage field."""

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
        "storage_budget": {"bytes": 4096, "files": 8},
    }
    api = team_efficiency._AuthenticatedV5Api()
    planner = team_efficiency._authenticated_v5_helper_module(
        "authenticated_v5_planner"
    )
    normalized_unit = planner.normalize_units(api, [unit])[0]
    unit["task"]["profile_evidence_hash"] = team_efficiency._sha256_json(
        planner._routing_profile_material(source_plan_hash, normalized_unit)
    )
    routing_requests = team_efficiency.prepare_authenticated_v5_routing_requests(
        [unit],
        source_plan_hash=source_plan_hash,
        host_capabilities=host,
        scheduler_facts=scheduler,
    )
    request_binding_hash = fastlane_routing.v5_request_binding_hash(routing_requests[0])
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
    index_ref = {
        "task_id": "TASK-V5",
        "correlation_id": "index-" + "c" * 64,
        "workspace_id": _hash("d"),
        "workspace_binding_hash": _hash("e"),
        "root_identity_hash": _hash("f"),
        "snapshot_id": _hash("0"),
        "snapshot_attestation_hash": _hash("1"),
        "query_receipt_hash": _hash("6"),
        "index_context_hash": hash_a,
        "attestation_hash": _hash("7"),
    }
    return {
        "call_intent_hash": "a" * 64,
        "preparation_id": "dispatch-v5-1",
        "host": host,
        "scheduler": scheduler,
        "source_plan_hash": source_plan_hash,
        "routing_requests": routing_requests,
        "attestation_items": attestation_items,
        "compiled": compiled,
        "planner_request": {
            "schema": "2718lab-devkit/fastlane-host-planner-request-v1",
            "action": "plan_dispatch",
            "assignment_skeletons": compiled["assignment_skeletons"],
            "project_index_attestation_refs": [index_ref],
        },
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
        worktree_identity=_hash("4"),
        worktree_base=_hash("5"),
        integration_head=_hash("6"),
        predecessor_hash=skeleton["predecessor_hash"],
        source_plan_hash=skeleton["source_plan_hash"],
        ledger_epoch=11,
        active_lease_set_hash=_hash("b"),
    )


def _pipe_pair() -> tuple[object, object]:
    from devkit_runtime.host_bridge import InheritedHandleHostBridge

    child_to_host_read, child_to_host_write = os.pipe()
    host_to_child_read, host_to_child_write = os.pipe()
    return (
        InheritedHandleHostBridge.from_file_descriptors(
            read_fd=host_to_child_read,
            write_fd=child_to_host_write,
            session_key=b"k" * 32,
            session_nonce=b"fastlane-storage-private-nonce",
        ),
        InheritedHandleHostBridge.from_file_descriptors(
            read_fd=child_to_host_read,
            write_fd=host_to_child_write,
            session_key=b"k" * 32,
            session_nonce=b"fastlane-storage-private-nonce",
        ),
    )


def _storage_profile_response(request: object) -> dict[str, object]:
    from devkit_runtime.host_bridge import StorageProfileRequest

    assert isinstance(request, StorageProfileRequest)
    response: dict[str, object] = {
        "schema": "2718lab-devkit/storage-profile-v1",
        "call_intent_hash": request.call_intent_hash,
        "preparation_id": request.preparation_id,
        "task_id": request.task_id,
        "source_plan_hash": request.source_plan_hash,
        "index_attestation_hash": request.index_attestation_hash,
        "repository_identity": _hash("1"),
        "workspace_manifest_hash": _hash("2"),
        "cargo_lock_hash": _hash("3"),
        "toolchain_digest": _hash("4"),
        "target_triple": "x86_64-pc-windows-msvc",
        "profile": "dev",
        "features_hash": _hash("5"),
        "build_env_class": "managed_workspace",
        "execution_context_hash": _hash("6"),
    }
    response["profile_hash"] = _canonical_hash(response)
    # The Host alone binds generation and TTL into this opaque attestation.
    response["attestation_hash"] = _hash("7")
    return response


def _storage_profile_request() -> object:
    from devkit_runtime import host_bridge

    nonce = base64.urlsafe_b64encode(b"n" * 32).decode("ascii").rstrip("=")
    request = {
        "schema": "2718lab-devkit/storage-profile-request-v1",
        "call_intent_hash": "a" * 64,
        "preparation_id": "storage-profile-1",
        "task_id": "TASK-V5",
        "source_plan_hash": _hash("8"),
        "index_attestation_hash": _hash("9"),
        "nonce": nonce,
    }
    request["request_hash"] = _canonical_hash(request)
    return host_bridge._normalize_storage_profile_request(request)


def _storage_intent(
    *, task_id: str, plan_binding: str, context_hash: str
) -> dict[str, object]:
    descriptor = {
        "schema": "2718lab.storage.target.v1",
        "artifact_kind": "fastlane-task",
        "repository_identity": _hash("1"),
        "workspace_manifest_hash": _hash("2"),
        "cargo_lock_hash": _hash("3"),
        "toolchain_digest": _hash("4"),
        "target_triple": "x86_64-pc-windows-msvc",
        "profile": "dev",
        "features_hash": _hash("5"),
        "build_env_class": "managed_workspace",
    }
    intent: dict[str, object] = {
        "schema": "2718lab.storage.intent.v1",
        "task_id": task_id,
        "plan_binding": plan_binding,
        "context_hash": context_hash,
        "requested_bytes": 4096,
        "requested_files": 8,
        "target_descriptor": descriptor,
    }
    intent["storage_intent_hash"] = _canonical_hash(
        {
            "target_descriptor": descriptor,
            "task_id": task_id,
            "plan_binding": plan_binding,
            "context_hash": context_hash,
            "requested_bytes": 4096,
            "requested_files": 8,
        }
    )
    return intent


def test_public_request_rejects_caller_storage_descriptors() -> None:
    from devkit_fastlane.scripts import authenticated_v5_projection as projection

    with pytest.raises(ValueError, match="STORAGE_TARGET_KEY_INVALID"):
        projection._reject_public_storage_facts(
            {"storage_contexts": {"TASK-V5": {"repository_identity": _hash("1")}}}
        )
    with pytest.raises(ValueError, match="STORAGE_TARGET_KEY_INVALID"):
        projection._reject_public_storage_facts(
            [{"execution_context_hash": _hash("2")}]
        )


def test_pre_host_skeleton_remains_the_legacy_exact_eight_fields() -> None:
    fixture = _authenticated_v5_fixture()
    skeleton = fixture["compiled"]["assignment_skeletons"][0]
    assert set(skeleton) == {
        "task_id",
        "routing_proof",
        "write_scope",
        "concurrency_mode",
        "dispatch_order",
        "index_context_hash",
        "predecessor_hash",
        "source_plan_hash",
    }


def test_verified_private_profile_round_trip_constructs_local_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_profile_round_trip(monkeypatch, admit=False)


def test_storage_admission_request_is_session_bound_and_replay_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_profile_round_trip(monkeypatch, admit=True)


def _admission_response(request: dict[str, object]) -> dict[str, object]:
    intent = request["storage_intent"]
    receipt = {
        "schema": "2718lab.storage.admission-receipt.v1",
        "admission_id": _hash("a"),
        "profile_attestation_hash": request["profile_attestation_hash"],
        "storage_intent_hash": intent["storage_intent_hash"],
        "storage_binding_hash": _hash("b"),
        "target_key": _canonical_hash(intent["target_descriptor"]),
        "assigned_root_identity": _hash("c"),
        "target_family_lease_id": _hash("d"),
        "reserved_bytes": intent["requested_bytes"],
        "reserved_files": intent["requested_files"],
        "free_space_before": 8192,
        "free_space_after_reserve": 4096,
        "free_space_floor": 1024,
        "expires_at": 1_700_000_060,
    }
    receipt["receipt_hash"] = _canonical_hash(receipt)
    return {
        "schema": "2718lab.storage.admission-response.v1",
        "correlation_id": request["correlation_id"],
        "request_hash": request["request_hash"],
        "receipt": receipt,
    }


def _verified_profile_round_trip(
    monkeypatch: pytest.MonkeyPatch, *, admit: bool
) -> None:
    """Exercise the framed Host bridge and local post-profile compilation."""

    import devkit_runtime.host_session as host_session
    from devkit_runtime import fastlane_host_adapter as adapter
    from devkit_runtime.host_bridge import InheritedHandleHostBridge

    fixture = _authenticated_v5_fixture()
    child, host = _pipe_pair()
    fact = _v5_dispatch_fact(adapter, fixture)
    fact_mapping = adapter._dispatch_fact_mapping(fact)
    lease_hash = adapter._lease_scope_binding_hash(fact)
    failure: list[BaseException] = []
    admission_requests: list[dict[str, object]] = []

    def host_reply() -> None:
        try:
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
            profile_request = host.receive_storage_profile_request()
            host.send_storage_profile_response(
                request=profile_request,
                response=_storage_profile_response(profile_request),
            )
            if admit:
                for _ in range(2):
                    message = host._receive_private()
                    request = message.payload
                    assert message.kind == "storage_admission_request"
                    assert message.action_id == request["correlation_id"]
                    assert set(request) == {
                        "schema",
                        "correlation_id",
                        "profile_attestation_hash",
                        "storage_intent",
                        "request_hash",
                    }
                    assert request["schema"] == "2718lab.storage.admission-request.v1"
                    assert request["request_hash"] == _canonical_hash(
                        {
                            key: value
                            for key, value in request.items()
                            if key != "request_hash"
                        }
                    )
                    admission_requests.append(request)
                    host._send_validated_private(
                        kind="storage_admission_response",
                        action_id=message.action_id,
                        payload=_admission_response(request),
                    )
        except BaseException as error:  # report peer errors after the round trip
            failure.append(error)

    worker = threading.Thread(target=host_reply, daemon=True)
    worker.start()
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
            session.resolve_capability_snapshot_v2(
                call_intent_hash=fixture["call_intent_hash"],
                preparation_id=fixture["preparation_id"],
            )
            is not None
        )
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
            request=fixture["planner_request"],
            reasoning_effort="max",
            storage_budgets={"TASK-V5": {"bytes": 4096, "files": 8}},
        )
        assert type(prepared).__name__ == "_PreparedHostFacts"
        assert len(prepared.storage_intents) == 1
        assert prepared.storage_intents[0]["task_id"] == "TASK-V5"
        if admit:
            from devkit_runtime.host_bridge import (
                HostBridgeError,
                StorageAdmissionReceipt,
            )
            from devkit_runtime.storage_intent import parse_storage_intent

            intent = parse_storage_intent(prepared.storage_intents[0])
            # Same attestation/profile facts on another Python session do not
            # become an issued reference, even when the bridge object matches.
            foreign = host_session.HostSession(
                bridge=child, clock=lambda: 1_700_000_000
            )
            assert (
                foreign.request_storage_admission(
                    intent, profile_attestation_hash=_hash("7")
                )
                == "STORAGE_TARGET_KEY_INVALID"
            )
            first = session.request_storage_admission(
                intent, profile_attestation_hash=_hash("7")
            )
            second = session.request_storage_admission(
                intent, profile_attestation_hash=_hash("7")
            )
            assert isinstance(first, StorageAdmissionReceipt)
            assert first == second
            assert len(admission_requests) == 2  # no local decision/cache admission
            assert (
                admission_requests[0]["correlation_id"]
                != admission_requests[1]["correlation_id"]
            )
            assert len(child._storage_admission_completions) == 2
            with pytest.raises(
                HostBridgeError, match="HOST_BRIDGE_STORAGE_ADMISSION_INVALID"
            ):
                child.request_storage_admission(
                    admission_requests[0],
                    now=1_700_000_000,
                    expires_at=1_700_000_120,
                    clock=lambda: 1_700_000_000,
                )
            assert set(first.to_dict()) == set(
                _admission_response(admission_requests[0])["receipt"]
            )
        batch = adapter.compile_fast_lane_with_host_facts(
            fixture["planner_request"],
            reasoning_effort="max",
            verified_host_facts=prepared,
        )
        assert isinstance(batch, dict)
        assert "storage_intents" not in batch
    finally:
        session.close()
        host.close()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not failure


def test_storage_admission_rejects_substitution_and_unknown_fields() -> None:
    from devkit_runtime import host_bridge
    from devkit_runtime.storage_intent import parse_storage_intent

    intent = parse_storage_intent(
        _storage_intent(
            task_id="TASK-V5", plan_binding=_hash("8"), context_hash=_hash("6")
        )
    )
    request = host_bridge.build_storage_admission_request(
        intent, profile_attestation_hash=_hash("7")
    )
    response = _admission_response(request)
    malformed = [
        dict(request, assigned_root="G:/private"),
        {
            key: value
            for key, value in request.items()
            if key != "profile_attestation_hash"
        },
    ]
    for candidate in malformed:
        with pytest.raises(host_bridge.HostBridgeError):
            host_bridge._normalize_storage_admission_request(candidate)
    for field, value in (
        ("target_key", _hash("f")),
        ("storage_intent_hash", _hash("f")),
        ("profile_attestation_hash", _hash("f")),
        ("reserved_bytes", True),
        ("reserved_files", 9),
        ("expires_at", 1_700_000_000),
        ("free_space_floor", 0),
        ("admission_id", "G:/private"),
        ("assigned_root", "G:/private"),
    ):
        candidate = copy.deepcopy(response)
        candidate["receipt"][field] = value
        candidate["receipt"]["receipt_hash"] = _canonical_hash(
            {
                key: value
                for key, value in candidate["receipt"].items()
                if key != "receipt_hash"
            }
        )
        with pytest.raises(host_bridge.HostBridgeError):
            host_bridge._normalize_storage_admission_response(
                candidate, request=request, now=1_700_000_000, expires_at=1_700_000_120
            )
    child, host = _pipe_pair()
    try:
        with pytest.raises(
            host_bridge.HostBridgeError, match="HOST_BRIDGE_STORAGE_PROFILE_INVALID"
        ):
            child.request_storage_admission(
                request,
                now=1_700_000_000,
                expires_at=1_700_000_120,
                clock=lambda: 1_700_000_000,
            )
    finally:
        child.close()
        host.close()


def test_storage_admission_deadline_and_close_cancel_blocked_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_runtime import host_bridge, host_session
    from devkit_runtime.storage_intent import parse_storage_intent

    # Real pipe I/O with a fixed trusted clock: the transport deadline must
    # still advance monotonically. Peer never returns an admission response.
    for scenario in ("no_reply", "full_pipe", "close", "direct_close"):
        child, host = _pipe_pair()
        profile_request = _storage_profile_request()
        pending = child.send_storage_profile_request(
            **{
                name: getattr(profile_request, name)
                for name in (
                    "call_intent_hash",
                    "preparation_id",
                    "task_id",
                    "source_plan_hash",
                    "index_attestation_hash",
                )
            }
        )
        peer_request = host.receive_storage_profile_request()
        host.send_storage_profile_response(
            request=peer_request, response=_storage_profile_response(peer_request)
        )
        profile = child.receive_storage_profile_response(request=pending)
        session = host_session.HostSession(bridge=child, clock=lambda: 1_700_000_000)
        # This test isolates transport lifecycle after actual profile delivery;
        # preparation enrollment itself is covered by the round-trip test.
        session._completed_storage_profiles[_hash("7")] = (
            host_session._CompletedStorageProfile(
                profile=profile,
                bridge=child,
                expires_at=1_700_000_060
                if scenario in {"close", "direct_close"}
                else 1_700_000_001,
                requested_bytes=4096,
                requested_files=8,
            )
        )
        intent = parse_storage_intent(
            _storage_intent(
                task_id="TASK-V5", plan_binding=_hash("8"), context_hash=_hash("6")
            )
        )
        if scenario == "full_pipe":
            with host_bridge._nonblocking_pipe_writer(child._write_fd) as write:
                for _ in range(1024):
                    try:
                        if write(b"x" * 4096) == 0:
                            break
                    except BlockingIOError:
                        break
                else:
                    raise AssertionError("fixture pipe did not reach bounded capacity")
        restore_reached = threading.Event()
        restore_allowed = threading.Event()
        original_writer = host_bridge._nonblocking_pipe_writer
        if scenario == "direct_close":

            @contextmanager
            def paused_restore(descriptor: int):
                with original_writer(descriptor) as write:
                    try:
                        yield write
                    finally:
                        restore_reached.set()
                        assert restore_allowed.wait(timeout=3), (
                            "mode-restore barrier was not released"
                        )

            monkeypatch.setattr(host_bridge, "_nonblocking_pipe_writer", paused_restore)
        result: list[object] = []
        worker = threading.Thread(
            target=lambda: result.append(
                session.request_storage_admission(
                    intent, profile_attestation_hash=_hash("7")
                )
            ),
            daemon=True,
        )
        closer: threading.Thread | None = None
        worker.start()
        try:
            if scenario != "full_pipe":
                message = host._receive_private(deadline=time.monotonic() + 2)
                assert message.kind == "storage_admission_request"
            if scenario == "close":
                closer = threading.Thread(target=session.close, daemon=True)
                closer.start()
                closer.join(timeout=2)
                assert not closer.is_alive(), (
                    "close waited behind admission's business lock"
                )
            if scenario == "direct_close":
                assert restore_reached.wait(timeout=2)
                borrowed_fd = child._write_fd
                child.close()
                child.close()  # repeated close must not bypass deferred cleanup
                assert not child.is_available
                assert child._active_io_count == 1
                assert not child._descriptors_closed
                assert child._write_fd == borrowed_fd
                os.fstat(borrowed_fd)  # mode restoration still owns this exact fd
                restore_allowed.set()
            worker.join(timeout=3)
            assert not worker.is_alive(), (
                "admission did not terminate at its transport deadline"
            )
            assert result == ["STORAGE_STAT_UNAVAILABLE"]
            assert not child._storage_admission_completions
            assert not child._storage_admission_decisions
            assert child._active_io_count == 0
            assert child._descriptors_closed
        finally:
            # Only test-owned descriptors/threads; ensure a failing regression
            # cannot leave a blocked peer alive in the test process.
            restore_allowed.set()
            child.close()
            host.close()
            worker.join(timeout=2)
            if closer is not None:
                closer.join(timeout=2)
            session.close()
            monkeypatch.setattr(
                host_bridge, "_nonblocking_pipe_writer", original_writer
            )


def test_profile_tamper_or_missing_field_fails_closed() -> None:
    from devkit_runtime import host_bridge
    from devkit_runtime.host_bridge import HostBridgeError

    request = _storage_profile_request()
    profile = _storage_profile_response(request)
    tampered = copy.deepcopy(profile)
    tampered["profile_hash"] = _hash("f")
    with pytest.raises(HostBridgeError, match="HOST_BRIDGE_STORAGE_PROFILE_INVALID"):
        host_bridge._normalize_storage_profile_response(tampered, request=request)
    missing = copy.deepcopy(profile)
    missing.pop("attestation_hash")
    with pytest.raises(HostBridgeError, match="HOST_BRIDGE_STORAGE_PROFILE_INVALID"):
        host_bridge._normalize_storage_profile_response(missing, request=request)


def test_v2_remains_compatible_while_v3_requires_one_root_storage_intent() -> None:
    from test_fastlane_host_intent import _intent, _with_binding

    from devkit_runtime.fastlane_host_intent import (
        NO_SAFE_WORK,
        StorageIntentError,
        parse_host_execution_intent,
        validate_host_execution_intent,
    )

    legacy = _intent()
    legacy_result = parse_host_execution_intent(legacy)
    assert legacy_result != NO_SAFE_WORK
    assert not isinstance(legacy_result, StorageIntentError)
    assert legacy_result.schema.endswith("-v2")
    assert legacy_result.storage_intent is None
    assert validate_host_execution_intent(legacy) is NO_SAFE_WORK

    candidate = _intent()
    candidate["schema"] = "2718lab-devkit/fastlane-host-execution-intent-v3"
    task_id = candidate["assignment"]["predecessor"]["task_id"]
    context_hash = _hash("a")
    candidate["storage_intent"] = _storage_intent(
        task_id=task_id,
        plan_binding=candidate["source_plan_hash"],
        context_hash=context_hash,
    )
    candidate["execution_context_hash"] = context_hash
    candidate = _with_binding(candidate, "intent_hash")
    parsed = parse_host_execution_intent(candidate)
    assert not isinstance(parsed, StorageIntentError)
    assert parsed != NO_SAFE_WORK
    assert parsed.schema.endswith("-v3")
    assert parsed.storage_intent is not None
    assert validate_host_execution_intent(candidate) is NO_SAFE_WORK

    duplicate = copy.deepcopy(candidate)
    duplicate["assignment"]["storage_intent"] = copy.deepcopy(
        candidate["storage_intent"]
    )
    duplicate["assignment"] = _with_binding(
        duplicate["assignment"], "assignment_binding_hash"
    )
    duplicate = _with_binding(duplicate, "intent_hash")
    missing = copy.deepcopy(candidate)
    missing.pop("storage_intent")
    missing = _with_binding(missing, "intent_hash")
    for malformed in (duplicate, missing):
        result = parse_host_execution_intent(malformed)
        assert isinstance(result, StorageIntentError)
        assert result.code == "STORAGE_TARGET_KEY_INVALID"
