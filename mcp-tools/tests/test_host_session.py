"""Private HostSession attestation and fail-closed quota boundaries."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_fastlane.scripts.codex_account_quota import QuotaSnapshotEvidence
from devkit_runtime.host_bridge import InheritedHandleHostBridge
from devkit_runtime.host_envelopes import (
    EnvelopeBinding,
    EnvelopeExpectation,
    render_envelope,
)

_NOW = 1_700_000_000
_HASH_PREFIX = "sha256:"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: object) -> str:
    return _HASH_PREFIX + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _host_session_module() -> object:
    spec = importlib.util.find_spec("devkit_runtime.host_session")
    assert spec is not None, "HostSession private adapter is not implemented"
    return importlib.import_module("devkit_runtime.host_session")


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


def _snapshot(
    *,
    key: bytes,
    sequence: int = 1,
    main_used: int = 500_000,
    observed_at: str = "2023-11-14T22:13:20Z",
    valid_until: str = "2023-11-14T22:15:20Z",
    source_id_hash: str | None = None,
) -> dict[str, object]:
    capacity = {
        "ledger_epoch": 7,
        "global_main_active": 1,
        "global_spark_active": 0,
        "host_main_active": 1,
        "host_spark_active": 0,
        "host_main_cap": 3,
        "host_spark_cap": 1,
        "active_lease_set_hash": _HASH_PREFIX + "a" * 64,
    }
    source = {
        "kind": "codex_host_usage_snapshot",
        "source_id_hash": (
            _hash("codex-app-server-account-rate-limits")
            if source_id_hash is None
            else source_id_hash
        ),
        "key_id": _HASH_PREFIX + hashlib.sha256(key).hexdigest(),
    }
    unsigned: dict[str, object] = {
        "schema": "2718lab-devkit/host-quota-snapshot-v1",
        "source": source,
        "snapshot_seq": sequence,
        "observed_at_utc_z": observed_at,
        "valid_until_utc_z": valid_until,
        "sample_window_seconds": 300,
        "main": {
            "period_id_hash": _HASH_PREFIX + "b" * 64,
            "used_ppm": main_used,
            "delta_ppm_300s": 0,
        },
        "spark": {
            "period_id_hash": _HASH_PREFIX + "c" * 64,
            "used_ppm": 100_000,
            "delta_ppm_300s": 0,
        },
        "capacity": capacity,
    }
    snapshot_hash = _hash(unsigned)
    signed = {**unsigned, "snapshot_hash": snapshot_hash}
    signature = hmac.new(
        key, _canonical(signed).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        **signed,
        "signature": {"algorithm": "hmac-sha256", "value": signature},
    }


def _attestation(
    module: object, request_id: str, snapshot: dict[str, object], key: bytes
) -> object:
    source = snapshot["source"]
    assert isinstance(source, dict)
    capacity = snapshot["capacity"]
    assert isinstance(capacity, dict)
    key_id = source["key_id"]
    assert isinstance(key_id, str)
    return module.HostQuotaAttestation(
        request_id=request_id,
        account_id_hash=_HASH_PREFIX + "d" * 64,
        source_id_hash=source["source_id_hash"],
        main_limit_id="codex",
        spark_limit_id="codex_bengalfox",
        capacity_hash=_hash(capacity),
        snapshot_seq=snapshot["snapshot_seq"],
        evidence=QuotaSnapshotEvidence(
            snapshot=snapshot,
            key_id=key_id,
            _key=key,
            account_id_hash=_HASH_PREFIX + "d" * 64,
            plan_type="pro",
            main_limit_id="codex",
            spark_limit_id="codex_bengalfox",
        ),
    )


def _reply_once(
    host: InheritedHandleHostBridge,
    snapshot: dict[str, object],
    *,
    action_id: str | None = None,
) -> threading.Thread:
    def reply() -> None:
        request = host.receive()
        host.send_private(
            kind="quota_snapshot",
            action_id=request.action_id if action_id is None else action_id,
            payload={
                "schema": "2718lab-devkit/host-quota-snapshot-response-v1",
                "snapshot": snapshot,
            },
        )

    thread = threading.Thread(target=reply)
    thread.start()
    return thread


def test_host_session_without_an_inherited_bridge_never_resolves_quota() -> None:
    module = _host_session_module()
    resolved: list[object] = []
    session = module.HostSession(
        bridge=None,
        quota_evidence_resolver=lambda *_: resolved.append(object()),
        clock=lambda: _NOW,
    )

    assert session.read_quota() == "NO_SAFE_WORK"
    assert session.read_quota() == "NO_SAFE_WORK"
    assert resolved == []


def test_host_session_from_environment_never_probes_or_falls_back_when_missing_or_invalid() -> None:
    module = _host_session_module()
    resolved: list[object] = []

    missing = module.HostSession.from_environment(
        environ={},
        platform="posix",
        quota_evidence_resolver=lambda *_: resolved.append(object()),
        clock=lambda: _NOW,
    )
    invalid = module.HostSession.from_environment(
        environ={"CODEX_DEVKIT_HOST_BRIDGE_FD": "not-a-descriptor"},
        platform="posix",
        quota_evidence_resolver=lambda *_: resolved.append(object()),
        clock=lambda: _NOW,
    )

    assert missing.read_quota() == "NO_SAFE_WORK"
    assert invalid.read_quota() == "NO_SAFE_WORK"
    assert resolved == []


def test_host_session_from_environment_rejects_stdio_and_regular_file_selectors(
    tmp_path: Path,
) -> None:
    module = _host_session_module()
    regular = tmp_path / "not-private-ipc"
    descriptor = os.open(regular, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        stdio = module.HostSession.from_environment(
            environ={"CODEX_DEVKIT_HOST_BRIDGE_FD": "1"},
            platform="posix",
            quota_evidence_resolver=lambda *_: None,
            clock=lambda: _NOW,
        )
        file_backed = module.HostSession.from_environment(
            environ={"CODEX_DEVKIT_HOST_BRIDGE_FD": str(descriptor)},
            platform="posix",
            quota_evidence_resolver=lambda *_: None,
            clock=lambda: _NOW,
        )
    finally:
        os.close(descriptor)

    assert stdio.read_quota() == "NO_SAFE_WORK"
    assert file_backed.read_quota() == "NO_SAFE_WORK"


def test_declared_or_pending_route_data_never_becomes_a_scheduling_fact() -> None:
    module = _host_session_module()
    route = module.HostRoute(model="gpt-5.6-terra", effort="max")
    session = module.HostSession(
        bridge=None,
        quota_evidence_resolver=lambda *_: None,
        clock=lambda: _NOW,
    )

    declared = session.declare_routes((route,))

    assert declared[0].state is module.HostCapabilityState.DECLARED
    assert session.scheduling_facts(declared) == "NO_SAFE_WORK"
    assert session.observe_execution(
        {"clientThreadId": "pending-123", "model": "gpt-5.6-terra"},
        predecessor=object(),
        now=_NOW,
    ) == "NO_SAFE_WORK"


def test_host_session_exports_only_exact_bridge_attested_model_effort_pairs() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    route = module.HostRoute(model="gpt-5.6-terra", effort="max")
    binding = _binding()
    reported_hash = _HASH_PREFIX + "f" * 64

    def reply() -> None:
        probe = host.receive_capability_probe(now=_NOW, expected=binding)
        host.send_capability_report(
            probe=probe,
            capability_hashes={name: reported_hash for name in probe.capability_names},
            now=_NOW,
        )

    thread = threading.Thread(target=reply, daemon=True)
    thread.start()
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda *_: None,
        clock=lambda: _NOW,
    )
    try:
        facts = session.attest_routes(binding=binding, routes=(route,), now=_NOW)
        scheduled = session.scheduling_facts(facts)
        forged = replace(facts[0], route=replace(facts[0].route, effort="high"))
        rejected = session.scheduling_facts((forged,))
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()

    assert facts[0].state is module.HostCapabilityState.ATTESTED
    assert isinstance(scheduled, module.HostSchedulingFacts)
    assert scheduled.routes == (route,)
    assert rejected == "NO_SAFE_WORK"


def test_host_session_accepts_only_exact_same_process_attested_quota_evidence() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    snapshot = _snapshot(key=key)
    resolver_calls: list[str] = []

    def resolve(request_id: str, received: dict[str, object]) -> object:
        resolver_calls.append(request_id)
        assert received == snapshot
        return _attestation(module, request_id, snapshot, key)

    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        clock=lambda: _NOW,
    )
    thread = _reply_once(host, snapshot)
    try:
        facts = session.read_quota()
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()

    assert isinstance(facts, module.HostQuotaFacts)
    assert facts.snapshot == snapshot
    assert facts.account_id_hash == _HASH_PREFIX + "d" * 64
    assert facts.main_limit_id == "codex"
    assert facts.spark_limit_id == "codex_bengalfox"
    assert len(resolver_calls) == 1


def test_host_session_rejects_a_legal_snapshot_replay_then_freezes() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    snapshot = _snapshot(key=key, sequence=7)
    resolver_calls: list[str] = []

    def resolve(request_id: str, received: dict[str, object]) -> object:
        resolver_calls.append(request_id)
        return _attestation(module, request_id, received, key)

    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        clock=lambda: _NOW,
    )
    first = _reply_once(host, snapshot)
    try:
        assert isinstance(session.read_quota(), module.HostQuotaFacts)
        first.join(timeout=2)
        second = _reply_once(host, snapshot)
        assert session.read_quota() == "NO_SAFE_WORK"
        second.join(timeout=2)
        assert session.read_quota() == "NO_SAFE_WORK"
    finally:
        child.close()
        host.close()

    assert len(resolver_calls) == 2


def test_host_session_rejects_a_quota_response_bound_to_a_different_request() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    snapshot = _snapshot(key=key)
    resolver_calls: list[object] = []
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda *_: resolver_calls.append(object()),
        clock=lambda: _NOW,
    )
    thread = _reply_once(host, snapshot, action_id="quota-foreign")
    try:
        assert session.read_quota() == "NO_SAFE_WORK"
        thread.join(timeout=2)
        assert session.read_quota() == "NO_SAFE_WORK"
    finally:
        child.close()
        host.close()

    assert resolver_calls == []


def test_host_session_uses_the_injected_host_clock_for_future_and_stale_snapshots() -> None:
    module = _host_session_module()
    cases = (
        ("2023-11-14T22:13:21Z", "2023-11-14T22:15:21Z"),
        ("2023-11-14T22:10:00Z", "2023-11-14T22:12:00Z"),
    )
    for observed_at, valid_until in cases:
        child, host = _pipe_pair()
        key = b"q" * 32
        snapshot = _snapshot(
            key=key,
            observed_at=observed_at,
            valid_until=valid_until,
        )
        resolver_calls: list[str] = []

        def resolve(request_id: str, received: dict[str, object]) -> object:
            resolver_calls.append(request_id)
            return _attestation(module, request_id, received, key)

        session = module.HostSession(
            bridge=child,
            quota_evidence_resolver=resolve,
            clock=lambda: _NOW,
        )
        thread = _reply_once(host, snapshot)
        try:
            assert session.read_quota() == "NO_SAFE_WORK"
            thread.join(timeout=2)
            assert session.read_quota() == "NO_SAFE_WORK"
        finally:
            child.close()
            host.close()

        assert len(resolver_calls) == 1


def test_host_session_rejects_equal_or_decreasing_snapshot_sequences() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    first_snapshot = _snapshot(key=key, sequence=7, main_used=500_000)
    second_snapshot = _snapshot(key=key, sequence=7, main_used=510_000)

    def resolve(request_id: str, received: dict[str, object]) -> object:
        return _attestation(module, request_id, received, key)

    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        clock=lambda: _NOW,
    )
    first = _reply_once(host, first_snapshot)
    try:
        assert isinstance(session.read_quota(), module.HostQuotaFacts)
        first.join(timeout=2)
        second = _reply_once(host, second_snapshot)
        assert session.read_quota() == "NO_SAFE_WORK"
        second.join(timeout=2)
    finally:
        child.close()
        host.close()


def test_host_session_rejects_foreign_account_pool_or_capacity_attestations() -> None:
    module = _host_session_module()
    mutations = (
        lambda value: replace(value, account_id_hash=_HASH_PREFIX + "e" * 64),
        lambda value: replace(value, main_limit_id="not-codex"),
        lambda value: replace(value, spark_limit_id="foreign-spark"),
        lambda value: replace(value, capacity_hash=_HASH_PREFIX + "e" * 64),
    )
    for mutate in mutations:
        child, host = _pipe_pair()
        key = b"q" * 32
        snapshot = _snapshot(key=key)

        def resolve(request_id: str, received: dict[str, object]) -> object:
            return mutate(_attestation(module, request_id, received, key))

        session = module.HostSession(
            bridge=child,
            quota_evidence_resolver=resolve,
            clock=lambda: _NOW,
        )
        thread = _reply_once(host, snapshot)
        try:
            assert session.read_quota() == "NO_SAFE_WORK"
            thread.join(timeout=2)
        finally:
            child.close()
            host.close()


def test_host_session_rejects_a_non_official_signed_quota_source() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    snapshot = _snapshot(key=key, source_id_hash=_HASH_PREFIX + "e" * 64)

    def resolve(request_id: str, received: dict[str, object]) -> object:
        return _attestation(module, request_id, received, key)

    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        clock=lambda: _NOW,
    )
    thread = _reply_once(host, snapshot)
    try:
        assert session.read_quota() == "NO_SAFE_WORK"
        thread.join(timeout=2)
        assert session.is_available is False
    finally:
        child.close()
        host.close()


def test_host_session_freezes_permanently_after_private_transport_corruption() -> None:
    module = _host_session_module()
    attacks = ("mac", "nonce", "sequence", "partial")
    for attack in attacks:
        child, host = _pipe_pair()
        resolver_calls: list[object] = []
        errors: list[BaseException] = []
        session = module.HostSession(
            bridge=child,
            quota_evidence_resolver=lambda *_: resolver_calls.append(object()),
            clock=lambda: _NOW,
        )

        def reply() -> None:
            try:
                host.receive()
                if attack == "partial":
                    os.write(host._write_fd, b"\x00\x00")
                    os.close(host._write_fd)
                    return
                sender = InheritedHandleHostBridge.from_file_descriptors(
                    read_fd=os.dup(host._read_fd),
                    write_fd=os.dup(host._write_fd),
                    session_key=b"w" * 32 if attack == "mac" else b"k" * 32,
                    session_nonce=(
                        b"foreign-host-session-nonce"
                        if attack == "nonce"
                        else b"host-session-private-bridge-nonce"
                    ),
                )
                try:
                    if attack == "sequence":
                        sender._next_out = 2
                    sender.send_private(
                        kind="quota_snapshot",
                        action_id="quota-forged",
                        payload={
                            "schema": "2718lab-devkit/host-quota-snapshot-response-v1",
                            "snapshot": {},
                        },
                    )
                finally:
                    sender.close()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        try:
            assert session.read_quota() == "NO_SAFE_WORK"
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert session.is_available is False
            assert session.read_quota() == "NO_SAFE_WORK"
        finally:
            child.close()
            host.close()

        assert errors == []
        assert resolver_calls == []


def test_host_session_observes_an_authenticated_terminal_without_executing_work() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    route = module.HostRoute(model="gpt-5.6-terra", effort="max")
    binding = _binding()
    reported_hash = _HASH_PREFIX + "f" * 64

    def capability_reply() -> None:
        probe = host.receive_capability_probe(now=_NOW, expected=binding)
        host.send_capability_report(
            probe=probe,
            capability_hashes={name: reported_hash for name in probe.capability_names},
            now=_NOW,
        )

    thread = threading.Thread(target=capability_reply, daemon=True)
    thread.start()
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda *_: None,
        clock=lambda: _NOW,
    )
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
        scheduled = session.scheduling_facts((executed,))
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()

    assert executed.state is module.HostCapabilityState.EXECUTED
    assert isinstance(scheduled, module.HostSchedulingFacts)
    assert scheduled.routes == (route,)
