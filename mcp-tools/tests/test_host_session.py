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

from devkit_fastlane.scripts.codex_account_quota import (
    CodexQuotaProvider,
    QuotaSnapshotEvidence,
)
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


def _official_quota_jsonl_server_code() -> str:
    payload = {
        "account": {
            "id": "account-test",
            "planType": "pro",
            "requiresOpenaiAuth": True,
            "type": "chatgpt",
        },
        "limits": {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "resetsAt": 1_786_162_042,
                    "usedPercent": 50,
                    "windowDurationMins": 10_080,
                },
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "resetsAt": 1_786_162_042,
                        "usedPercent": 50,
                        "windowDurationMins": 10_080,
                    },
                },
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {
                        "resetsAt": 1_786_171_853,
                        "usedPercent": 16,
                        "windowDurationMins": 10_080,
                    },
                },
            },
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    return (
        "import json\n"
        "import sys\n"
        f"payload = json.loads({encoded!r})\n"
        "for raw in sys.stdin:\n"
        "    message = json.loads(raw)\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'id': message['id'], 'result': {'userAgent': 'fake'}}), flush=True)\n"
        "    elif method == 'account/read':\n"
        "        print(json.dumps({'id': message['id'], 'result': payload['account']}), flush=True)\n"
        "    elif method == 'account/rateLimits/read':\n"
        "        print(json.dumps({'id': message['id'], 'result': payload['limits']}), flush=True)\n"
    )


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
    capacity_overrides: dict[str, object] | None = None,
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
    if capacity_overrides is not None:
        capacity.update(capacity_overrides)
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
    module: object,
    request_id: str,
    snapshot: dict[str, object],
    key: bytes,
    *,
    account_id_hash: str = _HASH_PREFIX + "d" * 64,
    main_limit_id: str = "codex",
    spark_limit_id: str = "codex_bengalfox",
) -> object:
    source = snapshot["source"]
    assert isinstance(source, dict)
    capacity = snapshot["capacity"]
    assert isinstance(capacity, dict)
    key_id = source["key_id"]
    assert isinstance(key_id, str)
    return module.HostQuotaAttestation(
        request_id=request_id,
        account_id_hash=account_id_hash,
        source_id_hash=source["source_id_hash"],
        main_limit_id=main_limit_id,
        spark_limit_id=spark_limit_id,
        capacity_hash=_hash(capacity),
        snapshot_seq=snapshot["snapshot_seq"],
        evidence=QuotaSnapshotEvidence(
            snapshot=snapshot,
            key_id=key_id,
            _key=key,
            account_id_hash=account_id_hash,
            plan_type="pro",
            main_limit_id=main_limit_id,
            spark_limit_id=spark_limit_id,
        ),
    )


def _attestation_from_provider_evidence(
    module: object,
    request_id: str,
    evidence: QuotaSnapshotEvidence,
) -> object:
    snapshot = dict(evidence.snapshot)
    source = snapshot["source"]
    capacity = snapshot["capacity"]
    snapshot_seq = snapshot["snapshot_seq"]
    assert isinstance(source, dict)
    assert isinstance(capacity, dict)
    assert isinstance(snapshot_seq, int)
    source_id_hash = source["source_id_hash"]
    assert isinstance(source_id_hash, str)
    return module.HostQuotaAttestation(
        request_id=request_id,
        account_id_hash=evidence.account_id_hash,
        source_id_hash=source_id_hash,
        main_limit_id=evidence.main_limit_id,
        spark_limit_id=evidence.spark_limit_id,
        capacity_hash=_hash(capacity),
        snapshot_seq=snapshot_seq,
        evidence=evidence,
    )


def _quota_expectation(
    module: object,
    snapshot: dict[str, object],
    *,
    account_id_hash: str = _HASH_PREFIX + "d" * 64,
    main_limit_id: str = "codex",
    spark_limit_id: str = "codex_bengalfox",
    snapshot_seq_high_water: int | None = None,
) -> object:
    snapshot_seq = snapshot["snapshot_seq"]
    snapshot_capacity = snapshot["capacity"]
    source = snapshot["source"]
    assert isinstance(snapshot_seq, int)
    assert isinstance(snapshot_capacity, dict)
    assert isinstance(source, dict)
    source_id_hash = source["source_id_hash"]
    key_id = source["key_id"]
    ledger_epoch = snapshot_capacity["ledger_epoch"]
    active_lease_set_hash = snapshot_capacity["active_lease_set_hash"]
    assert isinstance(source_id_hash, str)
    assert isinstance(key_id, str)
    assert isinstance(ledger_epoch, int)
    assert isinstance(active_lease_set_hash, str)
    return module.HostQuotaExpectation(
        account_id_hash=account_id_hash,
        source_id_hash=source_id_hash,
        key_id=key_id,
        main_limit_id=main_limit_id,
        spark_limit_id=spark_limit_id,
        capacity_hash=_hash(snapshot_capacity),
        ledger_epoch=ledger_epoch,
        active_lease_set_hash=active_lease_set_hash,
        snapshot_seq_high_water=(
            snapshot_seq - 1
            if snapshot_seq_high_water is None
            else snapshot_seq_high_water
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

    thread = threading.Thread(target=reply, daemon=True)
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


def test_host_session_from_environment_never_probes_or_falls_back_when_missing_or_invalid() -> (
    None
):
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
    assert (
        session.observe_execution(
            {"clientThreadId": "pending-123", "model": "gpt-5.6-terra"},
            predecessor=object(),
            now=_NOW,
        )
        == "NO_SAFE_WORK"
    )


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
    assert scheduled.binding_hash is not None
    assert not hasattr(scheduled, "binding")
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
        quota_expectation=_quota_expectation(module, snapshot),
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


def test_host_session_accepts_successive_provider_snapshots_with_one_session_key() -> (
    None
):
    module = _host_session_module()
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
    times = iter((float(_NOW), float(_NOW) + 0.001))
    provider = CodexQuotaProvider(
        command=[sys.executable, "-c", _official_quota_jsonl_server_code()],
        memory_only=True,
        now=lambda: next(times),
    )
    first_evidence = provider.read(capacity=capacity)
    second_evidence = provider.read(capacity=capacity)
    first_snapshot = dict(first_evidence.snapshot)
    second_snapshot = dict(second_evidence.snapshot)

    assert first_evidence.key_id == second_evidence.key_id
    assert first_snapshot["snapshot_seq"] < second_snapshot["snapshot_seq"]

    evidence_by_snapshot_hash = {
        first_snapshot["snapshot_hash"]: first_evidence,
        second_snapshot["snapshot_hash"]: second_evidence,
    }

    def resolve(request_id: str, received: dict[str, object]) -> object:
        snapshot_hash = received["snapshot_hash"]
        evidence = evidence_by_snapshot_hash.get(snapshot_hash)
        assert evidence is not None
        assert dict(evidence.snapshot) == received
        return _attestation_from_provider_evidence(module, request_id, evidence)

    child, host = _pipe_pair()
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        quota_expectation=_quota_expectation(
            module,
            first_snapshot,
            account_id_hash=first_evidence.account_id_hash,
            main_limit_id=first_evidence.main_limit_id,
            spark_limit_id=first_evidence.spark_limit_id,
        ),
        clock=lambda: _NOW,
    )
    first_reply = _reply_once(host, first_snapshot)
    try:
        first_facts = session.read_quota()
        first_reply.join(timeout=2)
        second_reply = _reply_once(host, second_snapshot)
        second_facts = session.read_quota()
        second_reply.join(timeout=2)
    finally:
        child.close()
        host.close()

    assert isinstance(first_facts, module.HostQuotaFacts)
    assert isinstance(second_facts, module.HostQuotaFacts)
    assert first_facts.snapshot_seq < second_facts.snapshot_seq

    next_times = iter((float(_NOW),))
    next_provider = CodexQuotaProvider(
        command=[sys.executable, "-c", _official_quota_jsonl_server_code()],
        memory_only=True,
        now=lambda: next(next_times),
    )
    next_evidence = next_provider.read(capacity=capacity)
    next_snapshot = dict(next_evidence.snapshot)

    assert next_evidence.key_id != first_evidence.key_id

    next_child, next_host = _pipe_pair()
    next_session = module.HostSession(
        bridge=next_child,
        quota_evidence_resolver=lambda request_id, received: (
            _attestation_from_provider_evidence(module, request_id, next_evidence)
            if dict(next_evidence.snapshot) == received
            else None
        ),
        quota_expectation=_quota_expectation(
            module,
            next_snapshot,
            account_id_hash=next_evidence.account_id_hash,
            main_limit_id=next_evidence.main_limit_id,
            spark_limit_id=next_evidence.spark_limit_id,
        ),
        clock=lambda: _NOW,
    )
    next_reply = _reply_once(next_host, next_snapshot)
    try:
        next_facts = next_session.read_quota()
        next_reply.join(timeout=2)
    finally:
        next_child.close()
        next_host.close()

    assert isinstance(next_facts, module.HostQuotaFacts)


def test_host_session_requires_a_trusted_quota_expectation_before_reading() -> None:
    module = _host_session_module()
    requests: list[str] = []
    resolver_calls: list[str] = []

    class QuotaTrapBridge:
        is_available = True

        def request_quota_snapshot(self, *, request_id: str) -> None:
            requests.append(request_id)
            raise AssertionError("quota request must not be sent without expectation")

        def close(self) -> None:
            return None

    session = module.HostSession(
        bridge=QuotaTrapBridge(),
        quota_evidence_resolver=lambda request_id, _: resolver_calls.append(request_id),
        clock=lambda: _NOW,
    )

    assert session.read_quota() == "NO_SAFE_WORK"
    assert requests == []
    assert resolver_calls == []


def test_host_session_locks_the_expected_account_across_snapshot_sequences() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    first_snapshot = _snapshot(key=key, sequence=10)
    second_snapshot = _snapshot(key=key, sequence=11)

    def resolve(request_id: str, received: dict[str, object]) -> object:
        account_id_hash = (
            _HASH_PREFIX + "d" * 64
            if received["snapshot_seq"] == 10
            else _HASH_PREFIX + "e" * 64
        )
        return _attestation(
            module,
            request_id,
            received,
            key,
            account_id_hash=account_id_hash,
        )

    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        quota_expectation=_quota_expectation(
            module, first_snapshot, snapshot_seq_high_water=9
        ),
        clock=lambda: _NOW,
    )
    first = _reply_once(host, first_snapshot)
    try:
        assert isinstance(session.read_quota(), module.HostQuotaFacts)
        first.join(timeout=2)
        second = _reply_once(host, second_snapshot)
        assert session.read_quota() == "NO_SAFE_WORK"
        second.join(timeout=2)
        assert session.is_available is False
    finally:
        child.close()
        host.close()


def test_host_session_locks_the_exact_spark_pool_and_capacity_lease_set() -> None:
    module = _host_session_module()
    base_key = b"q" * 32
    expected_snapshot = _snapshot(key=base_key, sequence=9)
    cases = (
        (
            "foreign-spark-pool",
            _snapshot(key=base_key, sequence=10),
            {"spark_limit_id": "foreign-spark"},
        ),
        (
            "foreign-ledger-epoch",
            _snapshot(
                key=base_key,
                sequence=10,
                capacity_overrides={"ledger_epoch": 999},
            ),
            {},
        ),
        (
            "foreign-active-lease-set",
            _snapshot(
                key=base_key,
                sequence=10,
                capacity_overrides={"active_lease_set_hash": _HASH_PREFIX + "e" * 64},
            ),
            {},
        ),
    )
    for _, snapshot, attestation_values in cases:
        child, host = _pipe_pair()

        def resolve(request_id: str, received: dict[str, object]) -> object:
            return _attestation(
                module,
                request_id,
                received,
                base_key,
                **attestation_values,
            )

        session = module.HostSession(
            bridge=child,
            quota_evidence_resolver=resolve,
            quota_expectation=_quota_expectation(
                module, expected_snapshot, snapshot_seq_high_water=9
            ),
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


def test_host_session_rejects_a_self_consistent_source_key_rebind() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    first_key = b"q" * 32
    second_key = b"r" * 32
    first_snapshot = _snapshot(key=first_key, sequence=10)
    second_snapshot = _snapshot(key=second_key, sequence=11)

    def resolve(request_id: str, received: dict[str, object]) -> object:
        key = first_key if received["snapshot_seq"] == 10 else second_key
        return _attestation(module, request_id, received, key)

    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=resolve,
        quota_expectation=_quota_expectation(
            module, first_snapshot, snapshot_seq_high_water=9
        ),
        clock=lambda: _NOW,
    )
    first = _reply_once(host, first_snapshot)
    try:
        assert isinstance(session.read_quota(), module.HostQuotaFacts)
        first.join(timeout=2)
        second = _reply_once(host, second_snapshot)
        assert session.read_quota() == "NO_SAFE_WORK"
        second.join(timeout=2)
        assert session.is_available is False
    finally:
        child.close()
        host.close()


def test_host_session_rejects_snapshot_at_or_below_the_expected_high_water() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    key = b"q" * 32
    snapshot = _snapshot(key=key, sequence=10)
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda request_id, received: _attestation(
            module, request_id, received, key
        ),
        quota_expectation=_quota_expectation(
            module, snapshot, snapshot_seq_high_water=10
        ),
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


def test_private_quota_evidence_and_attestation_reprs_redact_the_hmac_key() -> None:
    module = _host_session_module()
    key = b"very-secret-quota-key-1234567890"
    snapshot = _snapshot(key=key)
    evidence = QuotaSnapshotEvidence(
        snapshot=snapshot,
        key_id=snapshot["source"]["key_id"],
        _key=key,
        account_id_hash=_HASH_PREFIX + "d" * 64,
        plan_type="pro",
        main_limit_id="codex",
        spark_limit_id="codex_bengalfox",
    )
    attestation = module.HostQuotaAttestation(
        request_id="quota-repr",
        account_id_hash=_HASH_PREFIX + "d" * 64,
        source_id_hash=snapshot["source"]["source_id_hash"],
        main_limit_id="codex",
        spark_limit_id="codex_bengalfox",
        capacity_hash=_hash(snapshot["capacity"]),
        snapshot_seq=snapshot["snapshot_seq"],
        evidence=evidence,
    )

    for rendered in (repr(evidence), repr(attestation)):
        assert repr(key) not in rendered
        assert key.hex() not in rendered


def test_host_session_records_bounded_unavailable_capability_facts() -> None:
    module = _host_session_module()
    route = module.HostRoute(model="gpt-5.6-terra", effort="max")
    session = module.HostSession(
        bridge=None,
        quota_evidence_resolver=lambda *_: None,
        clock=lambda: _NOW,
    )

    assert (
        session.attest_routes(binding=_binding(), routes=(route,), now=_NOW)
        == "NO_SAFE_WORK"
    )
    unavailable = session.last_unavailable

    assert isinstance(unavailable, module.HostUnavailableFacts)
    assert unavailable.reason_code == "HOST_SESSION_UNAVAILABLE"
    assert unavailable.bounds == (route,)
    assert unavailable.facts[0].state is module.HostCapabilityState.UNAVAILABLE
    assert unavailable.facts[0].route == route
    assert unavailable.facts[0].attestation_hash is None
    assert unavailable.facts[0].binding_hash is None
    assert unavailable.facts[0].reason_code == "HOST_SESSION_UNAVAILABLE"


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
        quota_expectation=_quota_expectation(module, snapshot),
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
        quota_expectation=_quota_expectation(module, snapshot),
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


def test_host_session_uses_the_injected_host_clock_for_future_and_stale_snapshots() -> (
    None
):
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
            quota_expectation=_quota_expectation(module, snapshot),
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
        quota_expectation=_quota_expectation(module, first_snapshot),
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
            quota_expectation=_quota_expectation(module, snapshot),
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
        quota_expectation=_quota_expectation(module, snapshot),
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
            quota_expectation=_quota_expectation(module, _snapshot(key=b"q" * 32)),
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


def test_host_session_observes_an_authenticated_terminal_without_executing_work() -> (
    None
):
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
            expected=EnvelopeExpectation(
                kind="coordinator_assignment", binding=binding
            ),
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
        replayed = session.observe_execution(
            facts[0], predecessor=predecessor, now=_NOW
        )
    finally:
        thread.join(timeout=2)
        child.close()
        host.close()

    assert executed.state is module.HostCapabilityState.EXECUTED
    assert executed.binding_hash is not None
    assert not hasattr(executed, "binding")
    assert binding.assignment_token not in repr(executed)
    assert isinstance(scheduled, module.HostSchedulingFacts)
    assert scheduled.routes == (route,)
    assert replayed == "NO_SAFE_WORK"


def test_host_session_rejects_terminals_crossing_attested_binding_fields() -> None:
    module = _host_session_module()
    route = module.HostRoute(model="gpt-5.6-terra", effort="max")
    reported_hash = _HASH_PREFIX + "f" * 64
    mutations = (
        lambda binding: replace(binding, task_id="task-2"),
        lambda binding: replace(binding, lease_epoch=binding.lease_epoch + 1),
        lambda binding: replace(binding, assignment_token=_HASH_PREFIX + "d" * 64),
        lambda binding: replace(binding, dispatch_context_hash=_HASH_PREFIX + "e" * 64),
        lambda binding: replace(binding, route_hash=_HASH_PREFIX + "f" * 64),
    )
    for mutate in mutations:
        child, host = _pipe_pair()
        attested_binding = _binding()
        terminal_binding = mutate(attested_binding)

        def capability_reply() -> None:
            probe = host.receive_capability_probe(now=_NOW, expected=attested_binding)
            host.send_capability_report(
                probe=probe,
                capability_hashes={
                    name: reported_hash for name in probe.capability_names
                },
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
            predecessor = child.send_operation(envelope=assignment, now=_NOW)
            received = host.receive_operation(
                now=_NOW,
                expected=EnvelopeExpectation(
                    kind="coordinator_assignment", binding=terminal_binding
                ),
            )
            terminal = render_envelope(
                kind="worker_terminal_result",
                binding=terminal_binding,
                payload={
                    "correlation_id": "operation-2",
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

            assert (
                session.observe_execution(facts[0], predecessor=predecessor, now=_NOW)
                == "NO_SAFE_WORK"
            )
            assert session.is_available is False
        finally:
            thread.join(timeout=2)
            child.close()
            host.close()


def test_host_session_rejects_a_terminal_with_the_wrong_role() -> None:
    module = _host_session_module()
    child, host = _pipe_pair()
    route = module.HostRoute(model="gpt-5.6-terra", effort="max")
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
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda *_: None,
        clock=lambda: _NOW,
    )
    assignment = render_envelope(
        kind="coordinator_assignment",
        binding=binding,
        payload={
            "correlation_id": "operation-3",
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
            expected=EnvelopeExpectation(
                kind="coordinator_assignment", binding=binding
            ),
        )
        terminal = render_envelope(
            kind="worker_terminal_result",
            binding=binding,
            payload={
                "correlation_id": "operation-3",
                "predecessor_hash": received.envelope_hash,
                "terminal": "succeeded",
                "result": ["result.verified"],
                "risk": [{"code": "none", "detail": "risk.none"}],
                "artifact_refs": [_HASH_PREFIX + "3" * 64],
                "digest_refs": [_HASH_PREFIX + "4" * 64],
            },
            now=_NOW,
        )
        terminal["sender_role"] = "coordinator"
        host._send_validated_private(
            kind="operation_result",
            action_id=received.task_id,
            payload={
                "schema": "2718lab-devkit/host-terminal-result-v1",
                "correlation_id": received.correlation_id,
                "envelope": terminal,
                "envelope_hash": _HASH_PREFIX + "0" * 64,
            },
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


def test_host_session_compiler_evidence_fails_closed_without_provider() -> None:
    module = _host_session_module()
    session = module.HostSession(
        bridge=None,
        quota_evidence_resolver=lambda *_: None,
        clock=lambda: _NOW,
    )

    assert (
        session.prepare_compiler_evidence(preparation_id="prep-1") == "NO_SAFE_WORK"
    )


def _fresh_quota_session_for_compiler_evidence(
    module: object,
    *,
    provider: object,
) -> tuple[object, InheritedHandleHostBridge, InheritedHandleHostBridge]:
    """Return a live session with one independently attested quota snapshot."""

    child, host = _pipe_pair()
    key = b"q" * 32
    snapshot = _snapshot(key=key)
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda request_id, received: _attestation(
            module, request_id, received, key
        ),
        quota_expectation=_quota_expectation(module, snapshot),
        compiler_evidence_provider=provider,
        clock=lambda: _NOW,
    )
    reply = _reply_once(host, snapshot)
    try:
        assert isinstance(session.read_quota(), module.HostQuotaFacts)
    finally:
        reply.join(timeout=2)
    return session, child, host


def test_host_session_compiler_evidence_requires_fresh_quota_before_provider() -> None:
    module = _host_session_module()
    provider_calls: list[str] = []
    child, host = _pipe_pair()
    session = module.HostSession(
        bridge=child,
        quota_evidence_resolver=lambda *_: None,
        compiler_evidence_provider=lambda preparation_id: provider_calls.append(
            preparation_id
        )
        or object(),
        clock=lambda: _NOW,
    )

    try:
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-no-quota")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()

    assert provider_calls == []


def test_host_session_compiler_evidence_rejects_untrusted_mutable_material_without_lineage() -> None:
    module = _host_session_module()
    raw_secret = "compiler-bearer-never-public"
    mutable_material = {
        "index": {"trusted": True, "expires_at": _NOW + 60},
        "terminal": {"trusted": True},
        "secret": raw_secret,
    }
    provider_calls: list[str] = []
    session, child, host = _fresh_quota_session_for_compiler_evidence(
        module,
        provider=lambda preparation_id: provider_calls.append(preparation_id)
        or mutable_material,
    )

    try:
        result = session.prepare_compiler_evidence(preparation_id="prep-no-lineage")
        mutable_material["secret"] = "mutated-after-provider-return"
    finally:
        child.close()
        host.close()

    assert result == "NO_SAFE_WORK"
    assert provider_calls == ["prep-no-lineage"]
    assert raw_secret not in repr(result)


def test_host_session_compiler_evidence_burns_preparation_after_provider_failure() -> None:
    module = _host_session_module()
    provider_calls: list[str] = []

    def provider(preparation_id: str) -> object:
        provider_calls.append(preparation_id)
        if len(provider_calls) == 1:
            raise RuntimeError("provider internal failure")
        return object()

    session, child, host = _fresh_quota_session_for_compiler_evidence(
        module, provider=provider
    )
    try:
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-burned")
            == "NO_SAFE_WORK"
        )
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-burned")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()

    assert provider_calls == ["prep-burned"]


def test_host_session_compiler_evidence_freeze_clears_and_denies_preparation() -> None:
    module = _host_session_module()
    provider_calls: list[str] = []
    session, child, host = _fresh_quota_session_for_compiler_evidence(
        module,
        provider=lambda preparation_id: provider_calls.append(preparation_id)
        or object(),
    )
    try:
        session._freeze()
        assert (
            session.prepare_compiler_evidence(preparation_id="prep-frozen")
            == "NO_SAFE_WORK"
        )
    finally:
        child.close()
        host.close()

    assert provider_calls == []
