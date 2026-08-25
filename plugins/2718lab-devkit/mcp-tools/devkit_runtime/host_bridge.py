"""Framed host-private capability traffic over one inherited OS handle.

The bridge deliberately has no listener, socket bootstrap, file mailbox, or
environment-provided secret.  A launcher may pass only its dedicated inherited
descriptor/handle selector; all capability material stays in authenticated
frames on that private handle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import stat
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from . import host_envelopes

_FRAME_SCHEMA: Final = "2718lab-devkit/host-bridge-v1"
_CAPABILITY_PROBE_SCHEMA: Final = "2718lab-devkit/host-capability-probe-v1"
_CAPABILITY_REPORT_SCHEMA: Final = "2718lab-devkit/host-capability-report-v1"
_OPERATION_REQUEST_SCHEMA: Final = "2718lab-devkit/host-operation-request-v1"
_TERMINAL_RESULT_SCHEMA: Final = "2718lab-devkit/host-terminal-result-v1"
_PROOF_CONTINUATION_SCHEMA: Final = "2718lab-devkit/host-proof-continuation-v1"
_FRAME_FIELDS: Final = frozenset(
    {"schema", "kind", "action_id", "session_nonce", "sequence", "payload", "mac"}
)
_MAX_FRAME_BYTES: Final = 65_536
_MAX_CAPABILITY_PACKET_BYTES: Final = 8 * 1024
_MAX_OPERATION_PACKET_BYTES: Final = 40 * 1024
_MAX_PROOF_CONTINUATION_BYTES: Final = 2 * 1024
_MAX_TERMINAL_OPERATION_TOMBSTONES: Final = 256
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_ENDPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAC = re.compile(r"[0-9a-f]{64}\Z")
_HANDLE_SELECTOR = re.compile(r"[0-9]{1,18}\Z")
_MESSAGE_KINDS: Final = frozenset(
    {
        "session_open",
        "capability_prepare",
        "capability_recovery",
        "capability_ack",
        "proof_register",
        "proof_attest",
        "proof_result",
        "capability_probe",
        "capability_report",
        "operation_request",
        "operation_result",
        "proof_continuation",
    }
)
_VALIDATED_PRIVATE_KINDS: Final = frozenset(
    {
        "capability_probe",
        "capability_report",
        "operation_request",
        "operation_result",
        "proof_continuation",
    }
)
_BINDING_FIELDS: Final = frozenset(
    {
        "task_id",
        "lease_epoch",
        "assignment_token",
        "dispatch_context_hash",
        "route_hash",
        "expires_at",
    }
)


class HostBridgeError(RuntimeError):
    """Stable internal failure; messages never echo a selector or bearer."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CapabilityDeliveryReceipt:
    """Bearer-free delivery state suitable for internal status bookkeeping."""

    action_id: str
    endpoint: str
    bundle_hash: str
    state: str


@dataclass(frozen=True)
class PrivateHostMessage:
    """One authenticated private message; its payload is intentionally redacted."""

    kind: str
    action_id: str
    sequence: int
    payload: dict[str, object] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "PrivateHostMessage("
            f"kind={self.kind!r}, action_id={self.action_id!r}, sequence={self.sequence!r})"
        )


@dataclass(frozen=True)
class CapabilityProbe:
    """A hash-bound, bearer-free request for named host capabilities."""

    binding: host_envelopes.EnvelopeBinding
    capability_names: tuple[str, ...]
    probe_hash: str


@dataclass(frozen=True)
class OperationReceipt:
    """Predecessor receipt a terminal result must bind exactly."""

    kind: str
    task_id: str
    correlation_id: str
    envelope_hash: str
    binding: host_envelopes.EnvelopeBinding


@dataclass
class _CapabilityDelivery:
    endpoint: str
    capabilities: dict[str, str] = field(repr=False)
    state: str = "prepared"

    def receipt(self, action_id: str) -> CapabilityDeliveryReceipt:
        return CapabilityDeliveryReceipt(
            action_id=action_id,
            endpoint=self.endpoint,
            bundle_hash=_private_payload_hash(self.capabilities),
            state=self.state,
        )


class InheritedHandleHostBridge:
    """Concrete non-listening bridge backed by a dedicated inherited handle.

    ``from_environment`` is the launch-path constructor.  It accepts only the
    numeric inherited handle selector frozen in the design lock.  The direct
    descriptor constructor exists for an already-established host session and
    for process-local harnesses; it does not open any listener.
    """

    def __init__(
        self,
        *,
        read_fd: int,
        write_fd: int,
        session_key: bytes,
        session_nonce: bytes,
        owns_descriptors: bool,
        bootstrap_required: bool,
    ) -> None:
        if type(read_fd) is not int or read_fd < 0:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if type(write_fd) is not int or write_fd < 0:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if type(session_key) is not bytes or len(session_key) != 32:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if type(session_nonce) is not bytes or not 16 <= len(session_nonce) <= 64:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._session_key = session_key
        self._session_nonce = _b64encode(session_nonce)
        self._owns_descriptors = owns_descriptors
        self._bootstrap_required = bootstrap_required
        self._bootstrap_sent = not bootstrap_required
        self._next_out = 1
        self._next_in = 1
        self._deliveries: dict[str, _CapabilityDelivery] = {}
        self._pending_capability_probes: set[str] = set()
        self._received_capability_probes: set[str] = set()
        self._pending_operations: dict[str, OperationReceipt] = {}
        self._received_operations: dict[str, OperationReceipt] = {}
        self._terminal_operation_tombstones: dict[
            tuple[str, str, str, str, str, str], int
        ] = {}
        self._closed = False

    @classmethod
    def from_file_descriptors(
        cls,
        *,
        read_fd: int,
        write_fd: int,
        session_key: bytes,
        session_nonce: bytes,
        owns_descriptors: bool = True,
    ) -> InheritedHandleHostBridge:
        """Build a bridge for an already private, authenticated session."""

        return cls(
            read_fd=read_fd,
            write_fd=write_fd,
            session_key=session_key,
            session_nonce=session_nonce,
            owns_descriptors=owns_descriptors,
            bootstrap_required=False,
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        platform: str | None = None,
    ) -> InheritedHandleHostBridge | None:
        """Open only the dedicated inherited handle selected by the launcher.

        Absence is intentionally non-fatal so read-only Relay operations remain
        available.  An invalid selector is fail-closed and is never reflected
        into an exception, log, result, or environment value.
        """

        values = os.environ if environ is None else environ
        target_platform = os.name if platform is None else platform
        if target_platform == "nt":
            selector = values.get("CODEX_DEVKIT_HOST_BRIDGE_HANDLE")
        elif target_platform == "posix":
            selector = values.get("CODEX_DEVKIT_HOST_BRIDGE_FD")
        else:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if selector is None or selector == "":
            return None
        if type(selector) is not str or _HANDLE_SELECTOR.fullmatch(selector) is None:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        handle = int(selector)
        if handle in {0, 1, 2}:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        descriptor = -1
        try:
            if target_platform == "nt":
                _assert_windows_private_duplex_ipc_handle(handle)
                import msvcrt

                descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
            else:
                descriptor = os.dup(handle)
            _assert_private_duplex_ipc_descriptor(descriptor, platform=target_platform)
            os.set_inheritable(descriptor, False)
        except (
            HostBridgeError,
            ImportError,
            OSError,
            OverflowError,
            ValueError,
        ) as error:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if isinstance(error, HostBridgeError):
                raise
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error
        return cls(
            read_fd=descriptor,
            write_fd=descriptor,
            session_key=os.urandom(32),
            session_nonce=os.urandom(32),
            owns_descriptors=True,
            bootstrap_required=True,
        )

    @classmethod
    def accept_from_file_descriptors(
        cls,
        *,
        read_fd: int,
        write_fd: int,
        owns_descriptors: bool = True,
    ) -> InheritedHandleHostBridge:
        """Accept a session-open frame sent by an inherited-handle peer.

        This is a host-private counterpart, not a public socket/listener.  The
        initial key is carried only through the possessed inherited handle, and
        every following frame is MACed with it.
        """

        bridge: InheritedHandleHostBridge | None = None
        try:
            raw = cls._read_raw_frame(read_fd)
            frame = _decode_frame(raw)
            payload = frame.get("payload")
            session_nonce_value = frame.get("session_nonce")
            if (
                frame.get("schema") != _FRAME_SCHEMA
                or frame.get("kind") != "session_open"
                or frame.get("action_id") != "session"
                or frame.get("sequence") != 0
                or not isinstance(session_nonce_value, str)
                or not isinstance(payload, dict)
                or set(payload) != {"session_key"}
            ):
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            encoded_key = payload.get("session_key")
            if not isinstance(encoded_key, str):
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            try:
                session_key = _b64decode(encoded_key)
                session_nonce = _b64decode(session_nonce_value)
            except ValueError as error:
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error
            bridge = cls(
                read_fd=read_fd,
                write_fd=write_fd,
                session_key=session_key,
                session_nonce=session_nonce,
                owns_descriptors=owns_descriptors,
                bootstrap_required=False,
            )
            bridge._verify_frame(frame, expected_sequence=0)
            return bridge
        except Exception:
            if bridge is not None:
                bridge.close()
            elif owns_descriptors:
                for descriptor in {read_fd, write_fd}:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise

    @property
    def is_available(self) -> bool:
        return not self._closed

    def prepare_capability(
        self,
        *,
        action_id: str,
        endpoint: str,
        capabilities: Mapping[str, str],
    ) -> CapabilityDeliveryReceipt:
        """Deliver one action-keyed bearer bundle without persisting it."""

        _validate_action_id(action_id)
        _validate_endpoint(endpoint)
        normalized = _validate_capabilities(capabilities)
        self._ensure_open()
        current = self._deliveries.get(action_id)
        if current is not None:
            if current.endpoint != endpoint or current.capabilities != normalized:
                raise HostBridgeError("HOST_BRIDGE_DELIVERY_CONFLICT")
            return current.receipt(action_id)
        current = _CapabilityDelivery(endpoint=endpoint, capabilities=normalized)
        self.send_private(
            kind="capability_prepare",
            action_id=action_id,
            payload={"endpoint": endpoint, "capabilities": normalized},
        )
        self._deliveries[action_id] = current
        return current.receipt(action_id)

    def recover_capability(self, action_id: str) -> CapabilityDeliveryReceipt:
        """Re-send the same action-keyed private delivery, never a new lease."""

        _validate_action_id(action_id)
        self._ensure_open()
        current = self._deliveries.get(action_id)
        if current is None:
            raise HostBridgeError("HOST_BRIDGE_DELIVERY_UNKNOWN")
        if current.state == "acknowledged":
            return current.receipt(action_id)
        self.send_private(
            kind="capability_recovery",
            action_id=action_id,
            payload={
                "endpoint": current.endpoint,
                "capabilities": dict(current.capabilities),
            },
        )
        current.state = "recovering"
        return current.receipt(action_id)

    def delivery_receipt(self, action_id: str) -> CapabilityDeliveryReceipt:
        """Return only opaque delivery metadata, never a bearer."""

        _validate_action_id(action_id)
        self._ensure_open()
        current = self._deliveries.get(action_id)
        if current is None:
            raise HostBridgeError("HOST_BRIDGE_DELIVERY_UNKNOWN")
        return current.receipt(action_id)

    def send_acknowledgement(self, action_id: str) -> None:
        """Send an authenticated host acknowledgement for a prepared delivery."""

        _validate_action_id(action_id)
        self.send_private(kind="capability_ack", action_id=action_id, payload={})

    def send_capability_probe(
        self,
        *,
        binding: host_envelopes.EnvelopeBinding | Mapping[str, object],
        capability_names: Sequence[str],
        now: int,
    ) -> CapabilityProbe:
        """Request named capability identities without transmitting a bearer."""

        try:
            normalized_binding = _normalize_bridge_binding(binding, now=now)
            normalized_names = _normalize_capability_names(capability_names)
            payload = _capability_probe_payload(normalized_binding, normalized_names)
            _validate_private_packet_size(payload, _MAX_CAPABILITY_PACKET_BYTES)
            probe = CapabilityProbe(
                binding=normalized_binding,
                capability_names=normalized_names,
                probe_hash=_private_payload_hash(payload),
            )
        except host_envelopes.HostEnvelopeError as error:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID") from error
        if probe.probe_hash in self._pending_capability_probes:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        self._send_validated_private(
            kind="capability_probe", action_id=probe.binding.task_id, payload=payload
        )
        self._pending_capability_probes.add(probe.probe_hash)
        return probe

    def receive_capability_probe(
        self,
        *,
        now: int,
        expected: host_envelopes.EnvelopeBinding | Mapping[str, object] | None = None,
    ) -> CapabilityProbe:
        """Receive one exact, hash-bound capability probe and consume its sequence."""

        expected_binding: host_envelopes.EnvelopeBinding | None = None
        try:
            if expected is not None:
                expected_binding = _normalize_bridge_binding(expected, now=now)
            message = self._receive_private()
            probe = _parse_capability_probe(message, now=now)
            if expected_binding is not None and probe.binding != expected_binding:
                raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
            if probe.probe_hash in self._received_capability_probes:
                raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        except host_envelopes.HostEnvelopeError as error:
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID") from error
        except HostBridgeError:
            self._poison()
            raise
        self._received_capability_probes.add(probe.probe_hash)
        return probe

    def send_capability_report(
        self,
        *,
        probe: CapabilityProbe,
        capability_hashes: Mapping[str, str],
        now: int,
    ) -> None:
        """Return only content hashes for exactly the requested capabilities."""

        try:
            normalized_probe = _normalize_probe(probe, now=now)
            normalized_hashes = _normalize_capability_hashes(
                capability_hashes, normalized_probe.capability_names
            )
            if normalized_probe.probe_hash not in self._received_capability_probes:
                raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
            payload = {
                "schema": _CAPABILITY_REPORT_SCHEMA,
                **host_envelopes.binding_mapping(normalized_probe.binding, now=now),
                "probe_hash": normalized_probe.probe_hash,
                "capability_hashes": normalized_hashes,
            }
            _validate_private_packet_size(payload, _MAX_CAPABILITY_PACKET_BYTES)
        except host_envelopes.HostEnvelopeError as error:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID") from error
        self._send_validated_private(
            kind="capability_report",
            action_id=normalized_probe.binding.task_id,
            payload=payload,
        )
        self._received_capability_probes.remove(normalized_probe.probe_hash)

    def receive_capability_report(
        self, *, probe: CapabilityProbe, now: int
    ) -> dict[str, object]:
        """Receive a report bound to one still-pending probe, exactly once."""

        try:
            expected_probe = _normalize_probe(probe, now=now)
        except host_envelopes.HostEnvelopeError as error:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID") from error
        if expected_probe.probe_hash not in self._pending_capability_probes:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        try:
            message = self._receive_private()
            report = _parse_capability_report(message, probe=expected_probe, now=now)
        except host_envelopes.HostEnvelopeError as error:
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID") from error
        except HostBridgeError:
            self._poison()
            raise
        self._pending_capability_probes.remove(expected_probe.probe_hash)
        return report

    def send_operation(
        self, *, envelope: Mapping[str, object], now: int
    ) -> OperationReceipt:
        """Send a validated assignment or peer handoff as a private operation."""

        try:
            normalized = host_envelopes.validate_envelope(envelope, now=now)
            if normalized["kind"] not in {
                "coordinator_assignment",
                "peer_evidence_handoff",
            }:
                raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
            receipt = _operation_receipt(normalized, now=now)
            payload = {
                "schema": _OPERATION_REQUEST_SCHEMA,
                "correlation_id": receipt.correlation_id,
                "envelope": normalized,
                "envelope_hash": receipt.envelope_hash,
            }
            _validate_private_packet_size(payload, _MAX_OPERATION_PACKET_BYTES)
        except host_envelopes.HostEnvelopeError as error:
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
        if self._operation_is_registered_or_tombstoned(receipt, now=now):
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
        self._send_validated_private(
            kind="operation_request", action_id=receipt.task_id, payload=payload
        )
        self._pending_operations[receipt.envelope_hash] = receipt
        return receipt

    def receive_operation(
        self,
        *,
        now: int,
        expected: host_envelopes.EnvelopeExpectation | None = None,
    ) -> OperationReceipt:
        """Receive one validated operation and retain its predecessor receipt."""

        try:
            message = self._receive_private()
            receipt = _parse_operation_request(message, now=now, expected=expected)
            if self._operation_is_registered_or_tombstoned(receipt, now=now):
                raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
        except host_envelopes.HostEnvelopeError as error:
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
        except HostBridgeError:
            self._poison()
            raise
        self._received_operations[receipt.envelope_hash] = receipt
        return receipt

    def send_terminal_result(
        self,
        *,
        envelope: Mapping[str, object],
        predecessor: OperationReceipt,
        now: int,
    ) -> None:
        """Send a terminal result tied to one consumed operation predecessor."""

        try:
            normalized = host_envelopes.validate_envelope(envelope, now=now)
            if normalized["kind"] != "worker_terminal_result":
                raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
            normalized_predecessor = _normalize_operation_receipt(predecessor, now=now)
            if normalized_predecessor.kind != "coordinator_assignment":
                raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
            _assert_terminal_predecessor(
                normalized, normalized_predecessor, now=now
            )
            registered_predecessor = self._received_operations.get(
                normalized_predecessor.envelope_hash
            )
            if registered_predecessor != normalized_predecessor:
                raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
            self._ensure_terminal_operation_tombstone_capacity(
                normalized_predecessor, now=now
            )
            payload = {
                "schema": _TERMINAL_RESULT_SCHEMA,
                "correlation_id": normalized_predecessor.correlation_id,
                "envelope": normalized,
                "envelope_hash": host_envelopes.envelope_hash(normalized, now=now),
            }
            _validate_private_packet_size(payload, _MAX_OPERATION_PACKET_BYTES)
        except host_envelopes.HostEnvelopeError as error:
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
        self._send_validated_private(
            kind="operation_result",
            action_id=normalized_predecessor.task_id,
            payload=payload,
        )
        self._remember_terminal_operation(normalized_predecessor, now=now)
        del self._received_operations[normalized_predecessor.envelope_hash]

    def receive_terminal_result(
        self,
        *,
        predecessor: OperationReceipt,
        now: int,
        expected: host_envelopes.EnvelopeExpectation | None = None,
    ) -> dict[str, object]:
        """Receive an exact terminal result tied to one pending operation."""

        normalized_predecessor = _normalize_operation_receipt(predecessor, now=now)
        if normalized_predecessor.kind != "coordinator_assignment":
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
        registered_predecessor = self._pending_operations.get(
            normalized_predecessor.envelope_hash
        )
        if registered_predecessor != normalized_predecessor:
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
        self._ensure_terminal_operation_tombstone_capacity(
            normalized_predecessor, now=now
        )
        try:
            message = self._receive_private()
            envelope = _parse_terminal_result(
                message,
                predecessor=normalized_predecessor,
                now=now,
                expected=expected,
            )
        except host_envelopes.HostEnvelopeError as error:
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
        except HostBridgeError:
            self._poison()
            raise
        self._remember_terminal_operation(normalized_predecessor, now=now)
        del self._pending_operations[normalized_predecessor.envelope_hash]
        return envelope

    def send_proof_continuation(
        self,
        *,
        proof_id: str,
        continuation_id: str,
        previous_digest: str,
        reference: str,
    ) -> None:
        """Send an opaque, hash-bound proof continuation instead of proof bytes."""

        _validate_proof_id(proof_id)
        _validate_action_id(continuation_id)
        _validate_proof_id(previous_digest)
        _validate_proof_id(reference)
        unsigned = {
            "schema": _PROOF_CONTINUATION_SCHEMA,
            "proof_id": proof_id,
            "continuation_id": continuation_id,
            "previous_digest": previous_digest,
            "reference": reference,
        }
        payload = {
            **unsigned,
            "continuation_hash": _private_payload_hash(unsigned),
        }
        _validate_private_packet_size(payload, _MAX_PROOF_CONTINUATION_BYTES)
        self._send_validated_private(
            kind="proof_continuation", action_id="proof", payload=payload
        )

    def receive_proof_continuation(
        self,
        *,
        proof_id: str,
        continuation_id: str,
        previous_digest: str,
    ) -> dict[str, str]:
        """Receive only the expected opaque reference and its chained digest."""

        _validate_proof_id(proof_id)
        _validate_action_id(continuation_id)
        _validate_proof_id(previous_digest)
        try:
            message = self._receive_private()
            continuation = _parse_proof_continuation(
                message,
                proof_id=proof_id,
                continuation_id=continuation_id,
                previous_digest=previous_digest,
            )
        except HostBridgeError:
            self._poison()
            raise
        return continuation

    def register_proof(self, proof_id: str, proof: Mapping[str, object]) -> None:
        """Forward a full integration proof only over the private bridge."""

        _validate_proof_id(proof_id)
        self.send_private(
            kind="proof_register",
            action_id="proof",
            payload={"proof_id": proof_id, "proof": _json_object(proof)},
        )

    def request_proof_attestation(
        self, proof_id: str, expectation: Mapping[str, object]
    ) -> None:
        """Request a host-private proof attestation without exposing its body."""

        _validate_proof_id(proof_id)
        self.send_private(
            kind="proof_attest",
            action_id="proof",
            payload={"proof_id": proof_id, "expectation": _json_object(expectation)},
        )

    def send_private(
        self, *, kind: str, action_id: str, payload: Mapping[str, object]
    ) -> None:
        """Write one canonical authenticated frame to the inherited handle."""

        if kind in _VALIDATED_PRIVATE_KINDS:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        self._send_private(kind=kind, action_id=action_id, payload=payload)

    def _send_validated_private(
        self, *, kind: str, action_id: str, payload: Mapping[str, object]
    ) -> None:
        """Write a packet only after its typed private validator has succeeded."""

        if kind not in _VALIDATED_PRIVATE_KINDS:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        self._send_private(kind=kind, action_id=action_id, payload=payload)

    def _send_private(
        self, *, kind: str, action_id: str, payload: Mapping[str, object]
    ) -> None:
        if kind not in _MESSAGE_KINDS or kind == "session_open":
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        _validate_action_id(action_id)
        encoded_payload = _json_object(payload)
        self._ensure_open()
        bootstrap = (
            self._frame_bytes(
                kind="session_open",
                action_id="session",
                sequence=0,
                payload={"session_key": _b64encode(self._session_key)},
            )
            if not self._bootstrap_sent
            else None
        )
        private_frame = self._frame_bytes(
            kind=kind,
            action_id=action_id,
            sequence=self._next_out,
            payload=encoded_payload,
        )
        if bootstrap is not None:
            self._write_complete(bootstrap)
            self._bootstrap_sent = True
        self._write_complete(private_frame)
        self._next_out += 1

    def receive(self) -> PrivateHostMessage:
        """Read only legacy private messages; typed packets need typed receivers."""

        message = self._receive_private()
        if message.kind in _VALIDATED_PRIVATE_KINDS:
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        return message

    def _receive_private(self) -> PrivateHostMessage:
        """Read one framed message for a typed private validator."""

        self._ensure_open()
        try:
            frame = _decode_frame(self._read_raw_frame(self._read_fd))
            self._verify_frame(frame, expected_sequence=self._next_in)
        except HostBridgeError:
            self._poison()
            raise
        self._next_in += 1
        kind = frame["kind"]
        action_id = frame["action_id"]
        payload = frame["payload"]
        assert type(kind) is str
        assert type(action_id) is str
        assert type(frame["sequence"]) is int
        assert type(payload) is dict
        if kind == "capability_ack" and payload == {}:
            delivery = self._deliveries.get(action_id)
            if delivery is not None:
                delivery.state = "acknowledged"
        return PrivateHostMessage(
            kind=kind,
            action_id=action_id,
            sequence=frame["sequence"],
            payload=payload,
        )

    def close(self) -> None:
        """Close only the descriptor(s) this bridge owns."""

        if self._closed:
            return
        self._closed = True
        if not self._owns_descriptors:
            return
        descriptors = {self._read_fd, self._write_fd}
        self._read_fd = -1
        self._write_fd = -1
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _frame_bytes(
        self, *, kind: str, action_id: str, sequence: int, payload: dict[str, object]
    ) -> bytes:
        unsigned = {
            "schema": _FRAME_SCHEMA,
            "kind": kind,
            "action_id": action_id,
            "session_nonce": self._session_nonce,
            "sequence": sequence,
            "payload": payload,
        }
        encoded = _canonical_bytes(unsigned)
        frame = {
            **unsigned,
            "mac": hmac.new(self._session_key, encoded, hashlib.sha256).hexdigest(),
        }
        raw = _canonical_bytes(frame)
        if len(raw) > _MAX_FRAME_BYTES:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        return struct.pack("!I", len(raw)) + raw

    def _verify_frame(
        self, frame: dict[str, object], *, expected_sequence: int
    ) -> None:
        if set(frame) != _FRAME_FIELDS:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        schema = frame["schema"]
        kind = frame["kind"]
        action_id = frame["action_id"]
        session_nonce = frame["session_nonce"]
        sequence = frame["sequence"]
        payload = frame["payload"]
        mac = frame["mac"]
        if (
            schema != _FRAME_SCHEMA
            or type(kind) is not str
            or kind not in _MESSAGE_KINDS
            or type(action_id) is not str
            or _IDENTIFIER.fullmatch(action_id) is None
            or type(session_nonce) is not str
            or session_nonce != self._session_nonce
            or type(sequence) is not int
            or type(sequence) is bool
            or type(payload) is not dict
            or type(mac) is not str
            or _MAC.fullmatch(mac) is None
        ):
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        unsigned = {
            "schema": schema,
            "kind": kind,
            "action_id": action_id,
            "session_nonce": session_nonce,
            "sequence": sequence,
            "payload": payload,
        }
        expected_mac = hmac.new(
            self._session_key, _canonical_bytes(unsigned), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(mac, expected_mac):
            raise HostBridgeError("HOST_BRIDGE_AUTH_FAILED")
        if sequence != expected_sequence:
            raise HostBridgeError("HOST_BRIDGE_SEQUENCE_INVALID")

    @staticmethod
    def _read_raw_frame(descriptor: int) -> bytes:
        header = _read_exact(descriptor, 4)
        size = struct.unpack("!I", header)[0]
        if size == 0 or size > _MAX_FRAME_BYTES:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        return _read_exact(descriptor, size)

    def _write_complete(self, payload: bytes) -> None:
        try:
            written = os.write(self._write_fd, payload)
        except OSError as error:
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error
        if written != len(payload):
            self._poison()
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")

    def _poison(self) -> None:
        """Irreversibly fail closed after an untrustworthy transport event."""

        self.close()

    def _operation_is_registered_or_tombstoned(
        self, receipt: OperationReceipt, *, now: int
    ) -> bool:
        """Reject duplicate or rebound operations without retaining envelope content."""

        self._prune_terminal_operation_tombstones(now=now)
        identity = _operation_replay_identity(receipt)
        return (
            any(
                _operation_replay_identity(existing) == identity
                for existing in self._pending_operations.values()
            )
            or any(
                _operation_replay_identity(existing) == identity
                for existing in self._received_operations.values()
            )
            or any(
                _operation_replay_identity_from_key(key) == identity
                for key in self._terminal_operation_tombstones
            )
        )

    def _ensure_terminal_operation_tombstone_capacity(
        self, receipt: OperationReceipt, *, now: int
    ) -> None:
        self._prune_terminal_operation_tombstones(now=now)
        key = _operation_replay_key(receipt)
        if (
            key in self._terminal_operation_tombstones
            or len(self._terminal_operation_tombstones)
            >= _MAX_TERMINAL_OPERATION_TOMBSTONES
        ):
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")

    def _remember_terminal_operation(
        self, receipt: OperationReceipt, *, now: int
    ) -> None:
        self._ensure_terminal_operation_tombstone_capacity(receipt, now=now)
        self._terminal_operation_tombstones[_operation_replay_key(receipt)] = (
            receipt.binding.expires_at
        )

    def _prune_terminal_operation_tombstones(self, *, now: int) -> None:
        expired = [
            key
            for key, expires_at in self._terminal_operation_tombstones.items()
            if expires_at <= now
        ]
        for key in expired:
            del self._terminal_operation_tombstones[key]

    def _ensure_open(self) -> None:
        if self._closed or self._read_fd < 0 or self._write_fd < 0:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")


def _normalize_bridge_binding(
    value: host_envelopes.EnvelopeBinding | Mapping[str, object], *, now: int
) -> host_envelopes.EnvelopeBinding:
    if type(value) is host_envelopes.EnvelopeBinding:
        host_envelopes.binding_mapping(value, now=now)
        return value
    if type(value) is dict:
        return host_envelopes.validate_binding_mapping(value, now=now)
    raise host_envelopes.HostEnvelopeError("HOST_ENVELOPE_INVALID")


def _normalize_capability_names(value: object) -> tuple[str, ...]:
    if type(value) is not list and type(value) is not tuple:
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    if not value or len(value) > 16:
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    normalized: list[str] = []
    for name in value:
        if type(name) is not str or _IDENTIFIER.fullmatch(name) is None:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        normalized.append(name)
    if len(set(normalized)) != len(normalized):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    return tuple(sorted(normalized))


def _normalize_capability_hashes(
    value: object, capability_names: tuple[str, ...]
) -> dict[str, str]:
    if type(value) is not dict or set(value) != set(capability_names):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    normalized: dict[str, str] = {}
    for name in capability_names:
        capability_hash = value.get(name)
        if type(capability_hash) is not str or _DIGEST.fullmatch(capability_hash) is None:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        normalized[name] = capability_hash
    return dict(sorted(normalized.items()))


def _capability_probe_payload(
    binding: host_envelopes.EnvelopeBinding, capability_names: tuple[str, ...]
) -> dict[str, object]:
    return {
        "schema": _CAPABILITY_PROBE_SCHEMA,
        "task_id": binding.task_id,
        "lease_epoch": binding.lease_epoch,
        "assignment_token": binding.assignment_token,
        "dispatch_context_hash": binding.dispatch_context_hash,
        "route_hash": binding.route_hash,
        "expires_at": binding.expires_at,
        "capability_names": list(capability_names),
    }


def _normalize_probe(probe: CapabilityProbe, *, now: int) -> CapabilityProbe:
    if type(probe) is not CapabilityProbe:
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    binding = _normalize_bridge_binding(probe.binding, now=now)
    capability_names = _normalize_capability_names(probe.capability_names)
    payload = _capability_probe_payload(binding, capability_names)
    expected_hash = _private_payload_hash(payload)
    if (
        type(probe.probe_hash) is not str
        or _DIGEST.fullmatch(probe.probe_hash) is None
        or not hmac.compare_digest(probe.probe_hash, expected_hash)
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    return CapabilityProbe(
        binding=binding,
        capability_names=capability_names,
        probe_hash=expected_hash,
    )


def _parse_capability_probe(
    message: PrivateHostMessage, *, now: int
) -> CapabilityProbe:
    payload = message.payload
    if (
        message.kind != "capability_probe"
        or set(payload)
        != {
            "schema",
            "task_id",
            "lease_epoch",
            "assignment_token",
            "dispatch_context_hash",
            "route_hash",
            "expires_at",
            "capability_names",
        }
        or payload.get("schema") != _CAPABILITY_PROBE_SCHEMA
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    binding = host_envelopes.validate_binding_mapping(
        {field: payload[field] for field in _BINDING_FIELDS}, now=now
    )
    capability_names = _normalize_capability_names(payload["capability_names"])
    normalized_payload = _capability_probe_payload(binding, capability_names)
    if message.action_id != binding.task_id or payload != normalized_payload:
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    _validate_private_packet_size(payload, _MAX_CAPABILITY_PACKET_BYTES)
    return CapabilityProbe(
        binding=binding,
        capability_names=capability_names,
        probe_hash=_private_payload_hash(normalized_payload),
    )


def _parse_capability_report(
    message: PrivateHostMessage, *, probe: CapabilityProbe, now: int
) -> dict[str, object]:
    payload = message.payload
    if (
        message.kind != "capability_report"
        or set(payload)
        != {
            "schema",
            "task_id",
            "lease_epoch",
            "assignment_token",
            "dispatch_context_hash",
            "route_hash",
            "expires_at",
            "probe_hash",
            "capability_hashes",
        }
        or payload.get("schema") != _CAPABILITY_REPORT_SCHEMA
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    binding = host_envelopes.validate_binding_mapping(
        {field: payload[field] for field in _BINDING_FIELDS}, now=now
    )
    capability_hashes = _normalize_capability_hashes(
        payload["capability_hashes"], probe.capability_names
    )
    normalized = {
        "schema": _CAPABILITY_REPORT_SCHEMA,
        **host_envelopes.binding_mapping(binding, now=now),
        "probe_hash": probe.probe_hash,
        "capability_hashes": capability_hashes,
    }
    if (
        message.action_id != probe.binding.task_id
        or binding != probe.binding
        or type(payload["probe_hash"]) is not str
        or not hmac.compare_digest(payload["probe_hash"], probe.probe_hash)
        or payload != normalized
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    _validate_private_packet_size(payload, _MAX_CAPABILITY_PACKET_BYTES)
    return normalized


def _operation_receipt(
    envelope: Mapping[str, object], *, now: int
) -> OperationReceipt:
    normalized = host_envelopes.validate_envelope(envelope, now=now)
    payload = normalized["payload"]
    assert type(payload) is dict
    correlation_id = payload["correlation_id"]
    kind = normalized["kind"]
    task_id = normalized["task_id"]
    assert type(correlation_id) is str
    assert type(kind) is str
    assert type(task_id) is str
    binding = host_envelopes.validate_binding_mapping(
        {field: normalized[field] for field in _BINDING_FIELDS}, now=now
    )
    return OperationReceipt(
        kind=kind,
        task_id=task_id,
        correlation_id=correlation_id,
        envelope_hash=host_envelopes.envelope_hash(normalized, now=now),
        binding=binding,
    )


def _operation_replay_key(
    receipt: OperationReceipt,
) -> tuple[str, str, str, str, str, str]:
    """Retain only opaque operation, assignment, context, and envelope identities."""

    return (
        receipt.kind,
        receipt.task_id,
        receipt.correlation_id,
        receipt.binding.assignment_token,
        receipt.binding.dispatch_context_hash,
        receipt.envelope_hash,
    )


def _operation_replay_identity(
    receipt: OperationReceipt,
) -> tuple[str, str, str, str, str]:
    key = _operation_replay_key(receipt)
    return key[:-1]


def _operation_replay_identity_from_key(
    key: tuple[str, str, str, str, str, str],
) -> tuple[str, str, str, str, str]:
    return key[:-1]


def _normalize_operation_receipt(value: OperationReceipt, *, now: int) -> OperationReceipt:
    if type(value) is not OperationReceipt:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    if value.kind not in {"coordinator_assignment", "peer_evidence_handoff"}:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    _validate_action_id(value.task_id)
    _validate_action_id(value.correlation_id)
    if type(value.envelope_hash) is not str or _DIGEST.fullmatch(value.envelope_hash) is None:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    try:
        binding = _normalize_bridge_binding(value.binding, now=now)
    except host_envelopes.HostEnvelopeError as error:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
    if binding.task_id != value.task_id:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    return OperationReceipt(
        kind=value.kind,
        task_id=value.task_id,
        correlation_id=value.correlation_id,
        envelope_hash=value.envelope_hash,
        binding=binding,
    )


def _parse_operation_request(
    message: PrivateHostMessage,
    *,
    now: int,
    expected: host_envelopes.EnvelopeExpectation | None,
) -> OperationReceipt:
    payload = message.payload
    envelope_value = payload.get("envelope")
    correlation_id = payload.get("correlation_id")
    received_envelope_hash = payload.get("envelope_hash")
    if (
        message.kind != "operation_request"
        or set(payload) != {"schema", "correlation_id", "envelope", "envelope_hash"}
        or payload.get("schema") != _OPERATION_REQUEST_SCHEMA
        or type(envelope_value) is not dict
        or type(correlation_id) is not str
        or type(received_envelope_hash) is not str
    ):
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    envelope = host_envelopes.validate_envelope(
        envelope_value, now=now, expected=expected
    )
    if envelope["kind"] not in {"coordinator_assignment", "peer_evidence_handoff"}:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    receipt = _operation_receipt(envelope, now=now)
    if (
        message.action_id != receipt.task_id
        or not hmac.compare_digest(correlation_id, receipt.correlation_id)
        or not hmac.compare_digest(received_envelope_hash, receipt.envelope_hash)
    ):
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    _validate_private_packet_size(payload, _MAX_OPERATION_PACKET_BYTES)
    return receipt


def _assert_terminal_predecessor(
    envelope: Mapping[str, object], predecessor: OperationReceipt, *, now: int
) -> None:
    payload = envelope["payload"]
    task_id = envelope["task_id"]
    assert type(payload) is dict
    assert type(task_id) is str
    predecessor_binding = host_envelopes.binding_mapping(predecessor.binding, now=now)
    if (
        task_id != predecessor.task_id
        or payload.get("correlation_id") != predecessor.correlation_id
        or payload.get("predecessor_hash") != predecessor.envelope_hash
        or any(envelope[field] != value for field, value in predecessor_binding.items())
    ):
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")


def _parse_terminal_result(
    message: PrivateHostMessage,
    *,
    predecessor: OperationReceipt,
    now: int,
    expected: host_envelopes.EnvelopeExpectation | None,
) -> dict[str, object]:
    payload = message.payload
    envelope_value = payload.get("envelope")
    correlation_id = payload.get("correlation_id")
    received_envelope_hash = payload.get("envelope_hash")
    if (
        message.kind != "operation_result"
        or set(payload) != {"schema", "correlation_id", "envelope", "envelope_hash"}
        or payload.get("schema") != _TERMINAL_RESULT_SCHEMA
        or type(envelope_value) is not dict
        or type(correlation_id) is not str
        or type(received_envelope_hash) is not str
    ):
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    envelope = host_envelopes.validate_envelope(
        envelope_value, now=now, expected=expected
    )
    if envelope["kind"] != "worker_terminal_result":
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    _assert_terminal_predecessor(envelope, predecessor, now=now)
    expected_envelope_hash = host_envelopes.envelope_hash(envelope, now=now)
    if (
        message.action_id != predecessor.task_id
        or not hmac.compare_digest(correlation_id, predecessor.correlation_id)
        or not hmac.compare_digest(received_envelope_hash, expected_envelope_hash)
    ):
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    _validate_private_packet_size(payload, _MAX_OPERATION_PACKET_BYTES)
    return envelope


def _parse_proof_continuation(
    message: PrivateHostMessage,
    *,
    proof_id: str,
    continuation_id: str,
    previous_digest: str,
) -> dict[str, str]:
    payload = message.payload
    if (
        message.kind != "proof_continuation"
        or set(payload)
        != {
            "schema",
            "proof_id",
            "continuation_id",
            "previous_digest",
            "reference",
            "continuation_hash",
        }
        or payload.get("schema") != _PROOF_CONTINUATION_SCHEMA
        or message.action_id != "proof"
        or payload.get("proof_id") != proof_id
        or payload.get("continuation_id") != continuation_id
        or payload.get("previous_digest") != previous_digest
    ):
        raise HostBridgeError("HOST_BRIDGE_PROOF_CONTINUATION_INVALID")
    reference = payload.get("reference")
    continuation_hash = payload.get("continuation_hash")
    if (
        type(reference) is not str
        or _DIGEST.fullmatch(reference) is None
        or type(continuation_hash) is not str
        or _DIGEST.fullmatch(continuation_hash) is None
    ):
        raise HostBridgeError("HOST_BRIDGE_PROOF_CONTINUATION_INVALID")
    unsigned = {
        "schema": _PROOF_CONTINUATION_SCHEMA,
        "proof_id": proof_id,
        "continuation_id": continuation_id,
        "previous_digest": previous_digest,
        "reference": reference,
    }
    if not hmac.compare_digest(continuation_hash, _private_payload_hash(unsigned)):
        raise HostBridgeError("HOST_BRIDGE_PROOF_CONTINUATION_INVALID")
    _validate_private_packet_size(payload, _MAX_PROOF_CONTINUATION_BYTES)
    return {
        "proof_id": proof_id,
        "continuation_id": continuation_id,
        "previous_digest": previous_digest,
        "reference": reference,
        "continuation_hash": continuation_hash,
    }


def _private_payload_hash(payload: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _validate_private_packet_size(payload: Mapping[str, object], maximum: int) -> None:
    if len(_canonical_bytes(payload)) > maximum:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _assert_private_duplex_ipc_descriptor(descriptor: int, *, platform: str) -> None:
    """Accept only a non-stdio, bidirectional IPC descriptor for launch use."""

    if descriptor in {0, 1, 2}:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    try:
        mode = os.fstat(descriptor).st_mode
        is_private_ipc = (
            stat.S_ISFIFO(mode) if platform == "nt" else stat.S_ISSOCK(mode)
        )
        if not is_private_ipc:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if platform != "nt" and (
            os.read(descriptor, 0) != b"" or os.write(descriptor, b"") != 0
        ):
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    except HostBridgeError:
        raise
    except OSError as error:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error


def _assert_windows_private_duplex_ipc_handle(handle: int) -> None:
    """Reject console, disk, and one-way Windows handles before CRT adoption."""

    try:
        import _winapi

        for standard_handle in (-10, -11, -12):
            if not _windows_handles_are_provably_distinct(
                handle, _winapi.GetStdHandle(standard_handle)
            ):
                raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if _winapi.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if not _windows_named_pipe_info_available(handle):
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        access_mask = _windows_file_access_mask(handle)
        if access_mask & 0x0003 != 0x0003:  # FILE_READ_DATA | FILE_WRITE_DATA
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    except HostBridgeError:
        raise
    except (
        AttributeError,
        ImportError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error


def _windows_handles_are_provably_distinct(first: int, second: int) -> bool:
    """Accept only a verified distinct kernel object, never an ambiguous comparison."""

    import ctypes

    kernelbase = ctypes.WinDLL("kernelbase", use_last_error=True)
    compare_object_handles = kernelbase.CompareObjectHandles
    compare_object_handles.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    compare_object_handles.restype = ctypes.c_int
    ctypes.set_last_error(0)
    if compare_object_handles(first, second):
        return False
    return ctypes.get_last_error() == 1656  # ERROR_NOT_SAME_OBJECT


def _windows_named_pipe_info_available(handle: int) -> bool:
    """Confirm that a pipe handle supports a non-writing named-pipe query."""

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_named_pipe_info = kernel32.GetNamedPipeInfo
    get_named_pipe_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
    )
    get_named_pipe_info.restype = ctypes.c_int
    flags = ctypes.c_ulong()
    if not get_named_pipe_info(handle, ctypes.byref(flags), None, None, None):
        return False
    return True


def _windows_file_access_mask(handle: int) -> int:
    """Return access rights through NtQueryInformationFile without writing data."""

    import ctypes

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    ntdll = ctypes.WinDLL("ntdll")
    query_information_file = ntdll.NtQueryInformationFile
    query_information_file.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
    )
    query_information_file.restype = ctypes.c_long
    io_status = _IoStatusBlock()
    access_mask = ctypes.c_ulong()
    status = query_information_file(
        handle,
        ctypes.byref(io_status),
        ctypes.byref(access_mask),
        ctypes.sizeof(access_mask),
        8,  # FileAccessInformation
    )
    if status != 0:
        raise OSError("NtQueryInformationFile failed")
    return access_mask.value


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = os.read(descriptor, remaining)
        except OSError as error:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error
        if not chunk:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode_frame(raw: bytes) -> dict[str, object]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
        if type(decoded) is not dict or _canonical_bytes(decoded) != raw:
            raise ValueError
        return decoded
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, TypeError) as error:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    if type(value) is not dict:
        value = dict(value)
    try:
        encoded = _canonical_bytes(value)
        decoded = json.loads(encoded.decode("utf-8"))
    except (HostBridgeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error
    if type(decoded) is not dict:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
    _validate_json_value(decoded)
    return decoded


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
    if type(value) is list:
        for item in value:
            _validate_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            _validate_json_value(item)
        return
    raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _validate_action_id(value: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _validate_endpoint(value: str) -> None:
    if type(value) is not str or _ENDPOINT.fullmatch(value) is None:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _validate_proof_id(value: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _validate_capabilities(value: Mapping[str, str]) -> dict[str, str]:
    if type(value) is not dict or not value or len(value) > 16:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
    normalized: dict[str, str] = {}
    for action, bearer in value.items():
        if (
            type(action) is not str
            or _IDENTIFIER.fullmatch(action) is None
            or type(bearer) is not str
            or not bearer
            or len(bearer) > 8_192
        ):
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        normalized[action] = bearer
    return dict(sorted(normalized.items()))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if type(value) is not str:
        raise ValueError
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
