"""Private role-scoped host-envelope and bridge packet contracts."""

from __future__ import annotations

import copy
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_runtime.host_bridge import (
    HostBridgeError,
    InheritedHandleHostBridge,
    OperationReceipt,
)

_NOW = 1_700_000_000
_TASK_ID = "host-bridge-task"
_TOKEN = "sha256:" + "a" * 64
_CONTEXT_HASH = "sha256:" + "b" * 64
_ROUTE_HASH = "sha256:" + "c" * 64
_PEER_CAPABILITY = "sha256:" + "d" * 64
_PREDECESSOR_HASH = "sha256:" + "e" * 64


def _envelopes() -> object:
    return importlib.import_module("devkit_runtime.host_envelopes")


def _binding(module: object, *, expires_at: int = _NOW + 60) -> object:
    return module.EnvelopeBinding(
        task_id=_TASK_ID,
        lease_epoch=7,
        assignment_token=_TOKEN,
        dispatch_context_hash=_CONTEXT_HASH,
        route_hash=_ROUTE_HASH,
        expires_at=expires_at,
    )


def _assignment_payload() -> dict[str, object]:
    return {
        "correlation_id": "operation-1",
        "assignment": "Run the bounded verification command",
        "context": ["Use the supplied artifact references"],
        "artifact_refs": ["sha256:" + "1" * 64],
        "digest_refs": ["sha256:" + "2" * 64],
    }


def _terminal_payload(
    *,
    correlation_id: str = "operation-1",
    predecessor_hash: str = _PREDECESSOR_HASH,
) -> dict[str, object]:
    return {
        "correlation_id": correlation_id,
        "predecessor_hash": predecessor_hash,
        "terminal": "succeeded",
        "result": ["Focused verification passed"],
        "risk": [{"code": "none", "detail": "No residual runtime risk observed"}],
        "artifact_refs": ["sha256:" + "3" * 64],
        "digest_refs": ["sha256:" + "4" * 64],
    }


def _peer_payload(*, peer_capability: str = _PEER_CAPABILITY) -> dict[str, object]:
    return {
        "correlation_id": "handoff-1",
        "peer_capability": peer_capability,
        "dependency": ["Consume the signed artifact reference"],
        "evidence": ["Digest was verified before handoff"],
        "artifact_refs": ["sha256:" + "5" * 64],
        "digest_refs": ["sha256:" + "6" * 64],
    }


def _render(
    module: object,
    kind: str,
    payload: dict[str, object],
    *,
    expires_at: int = _NOW + 60,
) -> dict[str, object]:
    return module.render_envelope(
        kind=kind,
        binding=_binding(module, expires_at=expires_at),
        payload=payload,
        now=_NOW,
    )


def _expectation(module: object, kind: str) -> object:
    return module.EnvelopeExpectation(kind=kind, binding=_binding(module))


@pytest.mark.parametrize(
    ("kind", "payload", "sender_role", "recipient_role", "limit"),
    [
        (
            "coordinator_assignment",
            _assignment_payload(),
            "coordinator",
            "worker",
            32 * 1024,
        ),
        (
            "worker_terminal_result",
            _terminal_payload(),
            "worker",
            "coordinator",
            24 * 1024,
        ),
        (
            "peer_evidence_handoff",
            _peer_payload(),
            "peer",
            "peer",
            16 * 1024,
        ),
    ],
)
def test_role_scoped_envelopes_render_validate_and_hash_canonically(
    kind: str,
    payload: dict[str, object],
    sender_role: str,
    recipient_role: str,
    limit: int,
) -> None:
    module = _envelopes()
    envelope = _render(module, kind, payload)

    assert envelope["sender_role"] == sender_role
    assert envelope["recipient_role"] == recipient_role
    assert module.validate_envelope(envelope, now=_NOW) == envelope
    assert module.envelope_hash(envelope, now=_NOW) == module.envelope_hash(
        dict(reversed(list(envelope.items()))), now=_NOW
    )
    assert len(module.canonical_envelope_bytes(envelope, now=_NOW)) <= limit


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda envelope: envelope.__setitem__("extra", "no"), "HOST_ENVELOPE_INVALID"),
        (
            lambda envelope: envelope.__setitem__("dispatch_context_hash", "sha256:bad"),
            "HOST_ENVELOPE_INVALID",
        ),
        (
            lambda envelope: envelope.__setitem__("task_id", "other-task"),
            "HOST_ENVELOPE_BINDING_INVALID",
        ),
        (
            lambda envelope: envelope.__setitem__("lease_epoch", 8),
            "HOST_ENVELOPE_BINDING_INVALID",
        ),
        (
            lambda envelope: envelope.__setitem__(
                "assignment_token", "sha256:" + "f" * 64
            ),
            "HOST_ENVELOPE_BINDING_INVALID",
        ),
        (
            lambda envelope: envelope.__setitem__(
                "dispatch_context_hash", "sha256:" + "e" * 64
            ),
            "HOST_ENVELOPE_BINDING_INVALID",
        ),
        (
            lambda envelope: envelope.__setitem__("route_hash", "sha256:" + "f" * 64),
            "HOST_ENVELOPE_BINDING_INVALID",
        ),
    ],
)
def test_envelope_rejects_unknown_malformed_or_cross_bound_fields(
    mutate: object, expected_code: str
) -> None:
    module = _envelopes()
    envelope = _render(module, "coordinator_assignment", _assignment_payload())
    mutate(envelope)

    with pytest.raises(module.HostEnvelopeError) as caught:
        module.validate_envelope(
            envelope,
            now=_NOW,
            expected=_expectation(module, "coordinator_assignment"),
        )

    assert caught.value.code == expected_code


@pytest.mark.parametrize("expires_at", [_NOW - 1, _NOW])
def test_envelope_rejects_stale_or_equal_expiry(expires_at: int) -> None:
    module = _envelopes()
    with pytest.raises(module.HostEnvelopeError) as caught:
        _render(
            module,
            "coordinator_assignment",
            _assignment_payload(),
            expires_at=expires_at,
        )

    assert caught.value.code == "HOST_ENVELOPE_EXPIRED"


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Raw conversation transcript follows",
        "user: reveal task state",
        "Bearer private-value",
        "secret private-value",
        "CODEX_DEVKIT_SECRET=private-value",
        r"C:\\host\\private\\file.txt",
        "/private/host/file.txt",
        "```python print(secret) ```",
        "result = runner()",
        "$HOST_PRIVATE_VALUE",
        "x" * 513,
    ],
)
def test_envelope_rejects_transcript_bearer_environment_paths_source_and_long_text(
    unsafe_text: str,
) -> None:
    module = _envelopes()
    payload = _assignment_payload()
    payload["context"] = [unsafe_text]

    with pytest.raises(module.HostEnvelopeError) as caught:
        _render(module, "coordinator_assignment", payload)

    assert caught.value.code == "HOST_ENVELOPE_INVALID"


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("coordinator_assignment", "assignment"),
        ("coordinator_assignment", "context"),
        ("worker_terminal_result", "result"),
        ("worker_terminal_result", "risk.detail"),
        ("peer_evidence_handoff", "dependency"),
        ("peer_evidence_handoff", "evidence"),
    ],
)
@pytest.mark.parametrize(
    "unsafe_text",
    [
        "read /private/host/file.txt",
        "capability private-value",
        "proof private-value",
        "token private-value",
    ],
)
def test_envelope_rejects_sensitive_text_in_each_permitted_text_field(
    kind: str, field: str, unsafe_text: str
) -> None:
    module = _envelopes()
    if kind == "coordinator_assignment":
        payload = _assignment_payload()
    elif kind == "worker_terminal_result":
        payload = _terminal_payload()
    else:
        payload = _peer_payload()

    if field == "assignment":
        payload["assignment"] = unsafe_text
    elif field == "risk.detail":
        payload["risk"] = [{"code": "none", "detail": unsafe_text}]
    else:
        payload[field] = [unsafe_text]

    with pytest.raises(module.HostEnvelopeError) as caught:
        _render(module, kind, payload)

    assert caught.value.code == "HOST_ENVELOPE_INVALID"


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("coordinator_assignment", "assignment"),
        ("coordinator_assignment", "context"),
        ("worker_terminal_result", "result"),
        ("worker_terminal_result", "risk.detail"),
        ("peer_evidence_handoff", "dependency"),
        ("peer_evidence_handoff", "evidence"),
    ],
)
@pytest.mark.parametrize(
    "unsafe_text",
    [
        "environment HOME private-value",
        "sk-proj-synthetic-private-value",
        "x" * 512,
    ],
)
def test_envelope_rejects_unverifiable_or_environment_sensitive_text_in_each_field(
    kind: str, field: str, unsafe_text: str
) -> None:
    module = _envelopes()
    if kind == "coordinator_assignment":
        payload = _assignment_payload()
    elif kind == "worker_terminal_result":
        payload = _terminal_payload()
    else:
        payload = _peer_payload()

    if field == "assignment":
        payload["assignment"] = unsafe_text
    elif field == "risk.detail":
        payload["risk"] = [{"code": "none", "detail": unsafe_text}]
    else:
        payload[field] = [unsafe_text]

    with pytest.raises(module.HostEnvelopeError) as caught:
        _render(module, kind, payload)

    assert caught.value.code == "HOST_ENVELOPE_INVALID"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("coordinator_assignment", _assignment_payload()),
        ("worker_terminal_result", _terminal_payload()),
        ("peer_evidence_handoff", _peer_payload()),
    ],
)
def test_envelope_accepts_opaque_sha256_reference_fields(
    kind: str, payload: dict[str, object]
) -> None:
    module = _envelopes()
    envelope = _render(module, kind, payload)

    assert module.validate_envelope(envelope, now=_NOW) == envelope


def test_envelope_rejects_collection_limit_overruns() -> None:
    module = _envelopes()
    cases = []

    artifact_overflow = _assignment_payload()
    artifact_overflow["artifact_refs"] = [
        f"sha256:{index:064x}" for index in range(17)
    ]
    cases.append(("coordinator_assignment", artifact_overflow))

    digest_overflow = _assignment_payload()
    digest_overflow["digest_refs"] = [
        f"sha256:{index:064x}" for index in range(33)
    ]
    cases.append(("coordinator_assignment", digest_overflow))

    risk_overflow = _terminal_payload()
    risk_overflow["risk"] = [
        {"code": f"risk-{index}", "detail": "bounded risk"} for index in range(9)
    ]
    cases.append(("worker_terminal_result", risk_overflow))

    for kind, payload in cases:
        with pytest.raises(module.HostEnvelopeError) as caught:
            _render(module, kind, payload)
        assert caught.value.code == "HOST_ENVELOPE_INVALID"


def test_peer_envelope_rejects_recipient_or_capability_mismatch() -> None:
    module = _envelopes()
    envelope = _render(module, "peer_evidence_handoff", _peer_payload())
    expected = module.EnvelopeExpectation(
        kind="peer_evidence_handoff",
        binding=_binding(module),
        correlation_id="handoff-1",
        peer_capability=_PEER_CAPABILITY,
    )

    wrong_recipient = copy.deepcopy(envelope)
    wrong_recipient["recipient_role"] = "worker"
    with pytest.raises(module.HostEnvelopeError) as recipient_error:
        module.validate_envelope(wrong_recipient, now=_NOW, expected=expected)
    assert recipient_error.value.code == "HOST_ENVELOPE_INVALID"

    wrong_capability = copy.deepcopy(envelope)
    wrong_capability["payload"]["peer_capability"] = "sha256:" + "f" * 64
    with pytest.raises(module.HostEnvelopeError) as capability_error:
        module.validate_envelope(wrong_capability, now=_NOW, expected=expected)
    assert capability_error.value.code == "HOST_ENVELOPE_BINDING_INVALID"


def _pipe_pair() -> tuple[InheritedHandleHostBridge, InheritedHandleHostBridge]:
    child_to_host_read, child_to_host_write = os.pipe()
    host_to_child_read, host_to_child_write = os.pipe()
    key = b"k" * 32
    nonce = b"host-envelope-bridge-nonce"
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


def _inject_authenticated_frame(
    bridge: InheritedHandleHostBridge,
    *,
    kind: str,
    action_id: str,
    payload: dict[str, object],
) -> None:
    """Simulate a malicious but MAC-holding peer below the typed API boundary."""

    bridge._write_complete(
        bridge._frame_bytes(
            kind=kind,
            action_id=action_id,
            sequence=bridge._next_out,
            payload=payload,
        )
    )
    bridge._next_out += 1


def test_private_bridge_capability_and_operation_packets_are_hash_bound() -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    binding = _binding(module)
    assignment = _render(module, "coordinator_assignment", _assignment_payload())
    expectation = _expectation(module, "coordinator_assignment")
    try:
        sent_probe = child.send_capability_probe(
            binding=binding,
            capability_names=("artifact-read",),
            now=_NOW,
        )
        received_probe = host.receive_capability_probe(now=_NOW, expected=binding)
        assert received_probe == sent_probe

        host.send_capability_report(
            probe=received_probe,
            capability_hashes={"artifact-read": "sha256:" + "7" * 64},
            now=_NOW,
        )
        report = child.receive_capability_report(probe=sent_probe, now=_NOW)
        assert report["capability_hashes"] == {
            "artifact-read": "sha256:" + "7" * 64
        }

        sent_operation = child.send_operation(envelope=assignment, now=_NOW)
        received_operation = host.receive_operation(
            now=_NOW, expected=expectation
        )
        assert received_operation == sent_operation

        terminal = _render(
            module,
            "worker_terminal_result",
            _terminal_payload(
                correlation_id=received_operation.correlation_id,
                predecessor_hash=received_operation.envelope_hash,
            ),
        )
        terminal_expectation = _expectation(module, "worker_terminal_result")
        host.send_terminal_result(
            envelope=terminal,
            predecessor=received_operation,
            now=_NOW,
        )
        assert child.receive_terminal_result(
            predecessor=sent_operation,
            now=_NOW,
            expected=terminal_expectation,
        ) == terminal
    finally:
        child.close()
        host.close()


@pytest.mark.parametrize("replay_mode", ["exact", "rebound"])
def test_private_bridge_rejects_post_terminal_operation_replay(
    replay_mode: str,
) -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    assignment = _render(module, "coordinator_assignment", _assignment_payload())
    try:
        first_sent = child.send_operation(envelope=assignment, now=_NOW)
        first_received = host.receive_operation(now=_NOW)
        terminal = _render(
            module,
            "worker_terminal_result",
            _terminal_payload(
                correlation_id=first_received.correlation_id,
                predecessor_hash=first_received.envelope_hash,
            ),
        )
        host.send_terminal_result(
            envelope=terminal, predecessor=first_received, now=_NOW
        )
        assert child.receive_terminal_result(predecessor=first_sent, now=_NOW) == terminal

        replay_assignment = assignment
        if replay_mode == "rebound":
            replay_binding = module.EnvelopeBinding(
                task_id=_TASK_ID,
                lease_epoch=7,
                assignment_token=_TOKEN,
                dispatch_context_hash=_CONTEXT_HASH,
                route_hash="sha256:" + "f" * 64,
                expires_at=_NOW + 60,
            )
            replay_assignment = module.render_envelope(
                kind="coordinator_assignment",
                binding=replay_binding,
                payload=_assignment_payload(),
                now=_NOW,
            )

        with pytest.raises(HostBridgeError) as local_replay:
            child.send_operation(envelope=replay_assignment, now=_NOW)
        assert local_replay.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert child.is_available is True
        assert host.is_available is True

        _inject_authenticated_frame(
            child,
            kind="operation_request",
            action_id=_TASK_ID,
            payload={
                "schema": "2718lab-devkit/host-operation-request-v1",
                "correlation_id": replay_assignment["payload"]["correlation_id"],
                "envelope": replay_assignment,
                "envelope_hash": module.envelope_hash(replay_assignment, now=_NOW),
            },
        )
        with pytest.raises(HostBridgeError) as remote_replay:
            host.receive_operation(now=_NOW)
        assert remote_replay.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert host.is_available is False
    finally:
        child.close()
        host.close()


def test_private_bridge_rejects_cross_bound_terminal_result_and_poison_session() -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    assignment = _render(module, "coordinator_assignment", _assignment_payload())
    try:
        operation = child.send_operation(envelope=assignment, now=_NOW)
        host.receive_operation(now=_NOW)
        terminal = _render(
            module,
            "worker_terminal_result",
            _terminal_payload(
                correlation_id=operation.correlation_id,
                predecessor_hash="sha256:" + "f" * 64,
            ),
        )
        _inject_authenticated_frame(
            host,
            kind="operation_result",
            action_id=_TASK_ID,
            payload={
                "schema": "2718lab-devkit/host-terminal-result-v1",
                "correlation_id": operation.correlation_id,
                "envelope": terminal,
                "envelope_hash": module.envelope_hash(terminal, now=_NOW),
            },
        )

        with pytest.raises(HostBridgeError) as caught:
            child.receive_terminal_result(predecessor=operation, now=_NOW)

        assert caught.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert child.is_available is False
    finally:
        child.close()
        host.close()


def test_private_bridge_rejects_terminal_result_wrapper_hash_mismatch() -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    assignment = _render(module, "coordinator_assignment", _assignment_payload())
    try:
        operation = child.send_operation(envelope=assignment, now=_NOW)
        received = host.receive_operation(now=_NOW)
        terminal = _render(
            module,
            "worker_terminal_result",
            _terminal_payload(
                correlation_id=received.correlation_id,
                predecessor_hash=received.envelope_hash,
            ),
        )
        _inject_authenticated_frame(
            host,
            kind="operation_result",
            action_id=_TASK_ID,
            payload={
                "schema": "2718lab-devkit/host-terminal-result-v1",
                "correlation_id": operation.correlation_id,
                "envelope": terminal,
                "envelope_hash": "sha256:" + "f" * 64,
            },
        )

        with pytest.raises(HostBridgeError) as caught:
            child.receive_terminal_result(predecessor=operation, now=_NOW)

        assert caught.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert child.is_available is False
    finally:
        child.close()
        host.close()


def test_private_bridge_rejects_terminal_result_rebound_to_new_route() -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    assignment = _render(module, "coordinator_assignment", _assignment_payload())
    try:
        operation = child.send_operation(envelope=assignment, now=_NOW)
        received = host.receive_operation(now=_NOW)
        rebound_binding = module.EnvelopeBinding(
            task_id=_TASK_ID,
            lease_epoch=7,
            assignment_token=_TOKEN,
            dispatch_context_hash=_CONTEXT_HASH,
            route_hash="sha256:" + "f" * 64,
            expires_at=_NOW + 60,
        )
        terminal = module.render_envelope(
            kind="worker_terminal_result",
            binding=rebound_binding,
            payload=_terminal_payload(
                correlation_id=received.correlation_id,
                predecessor_hash=received.envelope_hash,
            ),
            now=_NOW,
        )
        _inject_authenticated_frame(
            host,
            kind="operation_result",
            action_id=_TASK_ID,
            payload={
                "schema": "2718lab-devkit/host-terminal-result-v1",
                "correlation_id": operation.correlation_id,
                "envelope": terminal,
                "envelope_hash": module.envelope_hash(terminal, now=_NOW),
            },
        )

        with pytest.raises(HostBridgeError) as caught:
            child.receive_terminal_result(predecessor=operation, now=_NOW)

        assert caught.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert child.is_available is False
    finally:
        child.close()
        host.close()


def test_private_bridge_rejects_forged_operation_receipt_binding() -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    assignment = _render(module, "coordinator_assignment", _assignment_payload())
    try:
        child.send_operation(envelope=assignment, now=_NOW)
        received = host.receive_operation(now=_NOW)
        rebound_binding = module.EnvelopeBinding(
            task_id=_TASK_ID,
            lease_epoch=7,
            assignment_token=_TOKEN,
            dispatch_context_hash=_CONTEXT_HASH,
            route_hash="sha256:" + "f" * 64,
            expires_at=_NOW + 60,
        )
        forged_receipt = OperationReceipt(
            kind=received.kind,
            task_id=received.task_id,
            correlation_id=received.correlation_id,
            envelope_hash=received.envelope_hash,
            binding=rebound_binding,
        )
        terminal = module.render_envelope(
            kind="worker_terminal_result",
            binding=rebound_binding,
            payload=_terminal_payload(
                correlation_id=received.correlation_id,
                predecessor_hash=received.envelope_hash,
            ),
            now=_NOW,
        )

        with pytest.raises(HostBridgeError) as caught:
            host.send_terminal_result(
                envelope=terminal,
                predecessor=forged_receipt,
                now=_NOW,
            )

        assert caught.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert host.is_available is True
    finally:
        child.close()
        host.close()


def test_private_bridge_rejects_terminal_result_for_peer_handoff() -> None:
    module = _envelopes()
    child, host = _pipe_pair()
    peer = _render(module, "peer_evidence_handoff", _peer_payload())
    try:
        child.send_operation(envelope=peer, now=_NOW)
        received = host.receive_operation(now=_NOW)
        terminal = _render(
            module,
            "worker_terminal_result",
            _terminal_payload(
                correlation_id=received.correlation_id,
                predecessor_hash=received.envelope_hash,
            ),
        )

        with pytest.raises(HostBridgeError) as caught:
            host.send_terminal_result(
                envelope=terminal,
                predecessor=received,
                now=_NOW,
            )

        assert caught.value.code == "HOST_BRIDGE_ENVELOPE_INVALID"
        assert host.is_available is True
    finally:
        child.close()
        host.close()


def test_private_bridge_rejects_proof_continuation_digest_mismatch() -> None:
    child, host = _pipe_pair()
    proof_id = "sha256:" + "8" * 64
    previous_digest = "sha256:" + "9" * 64
    reference = "sha256:" + "a" * 64
    try:
        child.send_proof_continuation(
            proof_id=proof_id,
            continuation_id="proof-part-1",
            previous_digest=previous_digest,
            reference=reference,
        )
        received = host.receive_proof_continuation(
            proof_id=proof_id,
            continuation_id="proof-part-1",
            previous_digest=previous_digest,
        )
        assert received["reference"] == reference

        _inject_authenticated_frame(
            host,
            kind="proof_continuation",
            action_id="proof",
            payload={
                "schema": "2718lab-devkit/host-proof-continuation-v1",
                "proof_id": proof_id,
                "continuation_id": "proof-part-2",
                "previous_digest": previous_digest,
                "reference": reference,
                "continuation_hash": "sha256:" + "b" * 64,
            },
        )
        with pytest.raises(HostBridgeError) as caught:
            child.receive_proof_continuation(
                proof_id=proof_id,
                continuation_id="proof-part-2",
                previous_digest=previous_digest,
            )

        assert caught.value.code == "HOST_BRIDGE_PROOF_CONTINUATION_INVALID"
        assert child.is_available is False
    finally:
        child.close()
        host.close()
