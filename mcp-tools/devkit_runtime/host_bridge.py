"""Framed host-private capability traffic over one private OS transport.

The bridge deliberately has no listener, socket bootstrap, file mailbox, or
environment-provided secret. A launcher passes either a Unix inherited
descriptor or a high-entropy local Windows named-pipe selector; all capability
material stays in authenticated frames on that private transport.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import select
import stat
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import Event, Lock, RLock
from typing import Final, cast

from . import (
    fastlane_terminal_protocol,
    host_envelopes,
    project_index_attestation_protocol,
)

_FRAME_SCHEMA: Final = "2718lab-devkit/host-bridge-v1"
_CAPABILITY_PROBE_SCHEMA: Final = "2718lab-devkit/host-capability-probe-v1"
_CAPABILITY_REPORT_SCHEMA: Final = "2718lab-devkit/host-capability-report-v1"
_CAPABILITY_PROBE_SCHEMA_V2: Final = "2718lab-devkit/host-capability-probe-v2"
_CAPABILITY_REPORT_SCHEMA_V2: Final = "2718lab-devkit/host-capability-report-v2"
_OPERATION_REQUEST_SCHEMA: Final = "2718lab-devkit/host-operation-request-v1"
_TERMINAL_RESULT_SCHEMA: Final = "2718lab-devkit/host-terminal-result-v1"
_PROOF_CONTINUATION_SCHEMA: Final = "2718lab-devkit/host-proof-continuation-v1"
_COMPILER_EVIDENCE_REQUEST_SCHEMA: Final = (
    "2718lab-devkit/compiler-evidence-request-v1"
)
_COMPILER_EVIDENCE_RESPONSE_SCHEMA: Final = (
    "2718lab-devkit/compiler-evidence-response-v1"
)
_STORAGE_PROFILE_REQUEST_SCHEMA: Final = (
    "2718lab-devkit/storage-profile-request-v1"
)
_STORAGE_PROFILE_SCHEMA: Final = "2718lab-devkit/storage-profile-v1"
_PROJECT_INDEX_ATTESTATION_SCHEMA: Final = (
    project_index_attestation_protocol.ATTESTATION_SCHEMA
)
_ROUTING_ATTESTATION_REQUEST_SCHEMA: Final = (
    "2718lab-devkit/routing-attestation-request-v1"
)
_ROUTING_ATTESTATION_RESPONSE_SCHEMA: Final = (
    "2718lab-devkit/routing-attestation-response-v1"
)
_FAST_LANE_TERMINAL_RESULT_SCHEMA: Final = (
    fastlane_terminal_protocol.TERMINAL_RESULT_SCHEMA
)
_FAST_LANE_TERMINAL_ACK_SCHEMA: Final = fastlane_terminal_protocol.TERMINAL_ACK_SCHEMA
_FAST_LANE_REFILL_REGISTRY_SCHEMA: Final = (
    "2718lab-devkit/fast_lane_refill_registry-v1"
)
_FAST_LANE_REFILL_REGISTRY_ACTION_PREFIX: Final = "refill-registry-"
_FRAME_FIELDS: Final = frozenset(
    {"schema", "kind", "action_id", "session_nonce", "sequence", "payload", "mac"}
)
_MAX_FRAME_BYTES: Final = 65_536
_MAX_JSON_DEPTH: Final = 12
_MAX_JSON_NODES: Final = 4_096
_MAX_CAPABILITY_PACKET_BYTES: Final = 8 * 1024
_MAX_OPERATION_PACKET_BYTES: Final = 40 * 1024
_MAX_PROOF_CONTINUATION_BYTES: Final = 2 * 1024
_MAX_TERMINAL_OPERATION_TOMBSTONES: Final = 256
_MAX_COMPILER_EVIDENCE_BYTES: Final = 40 * 1024
_COMPILER_EVIDENCE_TTL_SECONDS: Final = 120
_MAX_STORAGE_PROFILE_BYTES: Final = 8 * 1024
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
_STORAGE_PROFILE_FIELDS: Final = frozenset(
    {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "task_id",
        "source_plan_hash",
        "index_attestation_hash",
        "execution_context_hash",
        *_STORAGE_DESCRIPTOR_FIELDS,
        "profile_hash",
        "attestation_hash",
    }
)
_STORAGE_PROFILE_REQUEST_FIELDS: Final = frozenset(
    {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "task_id",
        "source_plan_hash",
        "index_attestation_hash",
        "nonce",
        "request_hash",
    }
)
_STORAGE_PROFILE_BUILD_ENV_CLASSES: Final = frozenset(
    {"managed_read_only", "managed_workspace", "disabled", "external"}
)
_STORAGE_PROFILE_SCALAR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_CAPABILITY_V2_TTL_SECONDS: Final = 120
_MAX_PROJECT_INDEX_ATTESTATION_BYTES: Final = (
    project_index_attestation_protocol.MAX_ATTESTATION_BYTES
)
_PROJECT_INDEX_ATTESTATION_TTL_SECONDS: Final = (
    project_index_attestation_protocol.ATTESTATION_TTL_SECONDS
)
_MAX_ROUTING_ATTESTATION_BYTES: Final = 40 * 1024
_ROUTING_ATTESTATION_TTL_SECONDS: Final = 120
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_FAST_LANE_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
_ENDPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAC = re.compile(r"[0-9a-f]{64}\Z")
_FAST_LANE_REFILL_REGISTRY_ACTION = re.compile(
    r"refill-registry-[0-9a-f]{64}\Z"
)
_FD_SELECTOR = re.compile(r"[0-9]{1,18}\Z")
_WINDOWS_PIPE_SELECTOR = re.compile(
    r"pipe:(?P<name>codex-devkit-(?P<server_pid>[1-9][0-9]{0,9})-"
    r"(?P<creation_filetime>[0-9a-f]{16})-(?P<token>[0-9a-f]{32}))\Z"
)
_MAX_WINDOWS_PIPE_NAME: Final = 96
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
        "compiler_evidence_request",
        "compiler_evidence_response",
        "storage_profile_request",
        "storage_profile_response",
        "project_index_attestation",
        "routing_attestation_request",
        "routing_attestation_response",
        "fast_lane_worker_terminal_result",
        "fast_lane_worker_terminal_ack",
        "fast_lane_refill_registry",
    }
)
_VALIDATED_PRIVATE_KINDS: Final = frozenset(
    {
        "capability_probe",
        "capability_report",
        "operation_request",
        "operation_result",
        "proof_continuation",
        "compiler_evidence_request",
        "compiler_evidence_response",
        "project_index_attestation",
        "routing_attestation_request",
        "routing_attestation_response",
        "fast_lane_worker_terminal_result",
        "fast_lane_worker_terminal_ack",
        "fast_lane_refill_registry",
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
class CapabilityProbeV2:
    """Generation-local request for the exact V5 Host and scheduler snapshots."""

    call_intent_hash: str
    preparation_id: str
    requested_capability_schemas: tuple[str, ...]
    probe_hash: str
    expires_at: int = field(repr=False)


@dataclass(frozen=True)
class RoutingAttestationRequest:
    """Generation-local set of normalized V5 requests awaiting Host proof."""

    call_intent_hash: str
    preparation_id: str
    routing_requests: tuple[dict[str, object], ...] = field(repr=False)
    routing_request_set_hash: str
    expires_at: int = field(repr=False)


@dataclass(frozen=True)
class OperationReceipt:
    """Predecessor receipt a terminal result must bind exactly."""

    kind: str
    task_id: str
    correlation_id: str
    envelope_hash: str
    binding: host_envelopes.EnvelopeBinding


@dataclass(frozen=True)
class CompilerEvidenceRequest:
    """One authenticated, generation-local request for compiler authority facts."""

    preparation_id: str
    call_intent_hash: str
    request_hash: str
    reasoning_effort: str
    requested_route_pairs: tuple[tuple[str, str], ...]
    assignment_skeletons: tuple[dict[str, object], ...] = field(repr=False)
    project_index_attestation_refs: tuple[dict[str, object], ...] = field(repr=False)
    routing_registry_binding_hash: str
    nonce: str = field(repr=False)
    expires_at: int


@dataclass(frozen=True)
class StorageProfileRequest:
    """One private, replay-bound request for Host-owned storage profile facts."""

    call_intent_hash: str
    preparation_id: str
    task_id: str
    source_plan_hash: str
    index_attestation_hash: str
    nonce: str = field(repr=False)
    request_hash: str


@dataclass(frozen=True)
class FastLaneRefillRegistryRequest:
    """One authenticated queue of remaining V5 skeletons.

    The queue is a host-owned handoff: the compiler only submits the exact
    remaining skeletons and their index references, while the host decides if
    and when a successor wave can be admitted.
    """

    correlation_id: str
    call_intent_hash: str
    preparation_id: str
    source_plan_hash: str
    index_context_hash: str
    routing_registry_binding_hash: str
    remaining_skeletons: tuple[dict[str, object], ...] = field(repr=False)
    index_attestation_refs: tuple[dict[str, object], ...] = field(repr=False)
    queue_registry_hash: str
    skeleton_package_hash: str
    expires_at: int = field(repr=False)


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
    """Concrete non-listening bridge backed by one private duplex descriptor.

    ``from_environment`` accepts a Unix inherited descriptor or a strict local
    Windows named-pipe selector.  The direct descriptor constructor exists for
    an already-established host session and for process-local harnesses; this
    module never opens a listener.
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
        # A private wake pipe lets a HostSession cancel a receiver that is
        # waiting for the next frame.  The transport itself may be an inherited
        # named pipe/anonymous pipe and closing it from another thread is not a
        # portable way to interrupt a blocked read.
        try:
            self._cancel_read_fd, self._cancel_write_fd = os.pipe()
        except OSError as error:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error
        self._cancel_event = Event()
        self._close_lock = Lock()
        # A session has one framed sequence in each direction.  Serialize all
        # bridge I/O so concurrent Fast Lane waves cannot interleave bytes or
        # consume one another's sequence slot.
        self._io_lock = RLock()
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
        self._pending_capability_v2: dict[str, CapabilityProbeV2] = {}
        self._received_capability_v2: dict[str, CapabilityProbeV2] = {}
        self._pending_routing_attestations: dict[str, RoutingAttestationRequest] = {}
        self._received_routing_attestations: dict[str, RoutingAttestationRequest] = {}
        self._pending_fast_lane_terminals: dict[str, dict[str, object]] = {}
        self._received_fast_lane_terminals: set[str] = set()
        self._pending_operations: dict[str, OperationReceipt] = {}
        self._received_operations: dict[str, OperationReceipt] = {}
        self._pending_compiler_evidence: dict[str, CompilerEvidenceRequest] = {}
        self._received_compiler_evidence: set[str] = set()
        self._pending_storage_profiles: dict[str, StorageProfileRequest] = {}
        self._received_storage_profiles: set[str] = set()
        self._sent_fast_lane_refill_registries: set[str] = set()
        self._received_fast_lane_refill_registries: set[str] = set()
        self._received_project_index_attestations: set[str] = set()
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
        """Open only the dedicated private transport selected by the launcher.

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
        descriptor = -1
        unavailable = False
        try:
            if target_platform == "nt":
                import msvcrt

                (
                    pipe_path,
                    expected_server_pid,
                    expected_creation_filetime,
                ) = _windows_named_pipe_target(selector)
                descriptor = os.open(pipe_path, os.O_BINARY | os.O_RDWR)
                windows_handle = msvcrt.get_osfhandle(descriptor)
                _assert_windows_private_duplex_ipc_handle(windows_handle)
                _assert_windows_named_pipe_server(
                    windows_handle,
                    expected_server_pid=expected_server_pid,
                    expected_creation_filetime=expected_creation_filetime,
                )
            else:
                if (
                    type(selector) is not str
                    or _FD_SELECTOR.fullmatch(selector) is None
                ):
                    raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
                handle = int(selector)
                if handle in {0, 1, 2}:
                    raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
                descriptor = os.dup(handle)
            _assert_private_duplex_ipc_descriptor(descriptor, platform=target_platform)
            os.set_inheritable(descriptor, False)
        except (
            HostBridgeError,
            ImportError,
            OSError,
            OverflowError,
            ValueError,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            unavailable = True
        if unavailable:
            # Raise outside the handler so a path-bearing OSError is not retained
            # in __cause__, __context__, or formatted traceback state.
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
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

    def send_capability_probe_v2(
        self, *, call_intent_hash: str, preparation_id: str, now: int
    ) -> CapabilityProbeV2:
        schemas = (
            "fastlane-host-dispatch-request-v1",
            "fastlane-routing-request-v5",
            "project-index-attestation-v1",
        )
        unsigned: dict[str, object] = {
            "schema": _CAPABILITY_PROBE_SCHEMA_V2,
            "call_intent_hash": call_intent_hash,
            "preparation_id": preparation_id,
            "requested_capability_schemas": list(schemas),
        }
        payload = {**unsigned, "probe_hash": _private_payload_hash(unsigned)}
        probe = _normalize_capability_probe_v2(payload, now=now)
        if probe.probe_hash in self._pending_capability_v2:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        _validate_private_packet_size(payload, _MAX_CAPABILITY_PACKET_BYTES)
        self._send_validated_private(
            kind="capability_probe", action_id=preparation_id, payload=payload
        )
        self._pending_capability_v2[probe.probe_hash] = probe
        return probe

    def receive_capability_probe_v2(self, *, now: int) -> CapabilityProbeV2:
        try:
            message = self._receive_private()
            if message.kind != "capability_probe":
                raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
            probe = _normalize_capability_probe_v2(message.payload, now=now)
            if (
                message.action_id != probe.preparation_id
                or probe.probe_hash in self._received_capability_v2
            ):
                raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        except HostBridgeError:
            self._poison()
            raise
        self._received_capability_v2[probe.probe_hash] = probe
        return probe

    def send_capability_report_v2(
        self,
        *,
        probe: CapabilityProbeV2,
        host_capabilities: Mapping[str, object],
        scheduler_facts: Mapping[str, object],
        now: int,
    ) -> dict[str, object]:
        if self._received_capability_v2.get(probe.probe_hash) != probe:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        unsigned: dict[str, object] = {
            "schema": _CAPABILITY_REPORT_SCHEMA_V2,
            "call_intent_hash": probe.call_intent_hash,
            "preparation_id": probe.preparation_id,
            "probe_hash": probe.probe_hash,
            "host_capabilities": dict(host_capabilities),
            "scheduler_facts": dict(scheduler_facts),
        }
        payload = {**unsigned, "report_hash": _private_payload_hash(unsigned)}
        normalized = _normalize_capability_report_v2(payload, probe=probe, now=now)
        self._send_validated_private(
            kind="capability_report", action_id=probe.preparation_id, payload=normalized
        )
        del self._received_capability_v2[probe.probe_hash]
        return normalized

    def receive_capability_report_v2(
        self, *, probe: CapabilityProbeV2, now: int
    ) -> dict[str, object]:
        if self._pending_capability_v2.get(probe.probe_hash) != probe:
            raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
        try:
            message = self._receive_private()
            if (
                message.kind != "capability_report"
                or message.action_id != probe.preparation_id
            ):
                raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
            normalized = _normalize_capability_report_v2(
                message.payload, probe=probe, now=now
            )
        except HostBridgeError:
            self._poison()
            raise
        del self._pending_capability_v2[probe.probe_hash]
        return normalized

    def send_routing_attestation_request(
        self,
        *,
        call_intent_hash: str,
        preparation_id: str,
        routing_requests: Sequence[Mapping[str, object]],
        now: int,
    ) -> RoutingAttestationRequest:
        """Send one ordered, canonical V5 request set over this generation."""

        request_set = [dict(item) for item in routing_requests]
        payload: dict[str, object] = {
            "schema": _ROUTING_ATTESTATION_REQUEST_SCHEMA,
            "call_intent_hash": call_intent_hash,
            "preparation_id": preparation_id,
            "routing_requests": request_set,
            "routing_request_set_hash": _private_payload_hash(request_set),
        }
        request = _normalize_routing_attestation_request(payload, now=now)
        if request.routing_request_set_hash in self._pending_routing_attestations:
            raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
        _validate_private_packet_size(payload, _MAX_ROUTING_ATTESTATION_BYTES)
        self._send_validated_private(
            kind="routing_attestation_request",
            action_id=preparation_id,
            payload=payload,
        )
        self._pending_routing_attestations[request.routing_request_set_hash] = request
        return request

    def receive_routing_attestation_request(
        self, *, now: int
    ) -> RoutingAttestationRequest:
        try:
            message = self._receive_private()
            if message.kind != "routing_attestation_request":
                raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
            request = _normalize_routing_attestation_request(message.payload, now=now)
            if (
                message.action_id != request.preparation_id
                or request.routing_request_set_hash
                in self._received_routing_attestations
            ):
                raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
        except HostBridgeError:
            self._poison()
            raise
        self._received_routing_attestations[request.routing_request_set_hash] = request
        return request

    def send_routing_attestation_response(
        self,
        *,
        request: RoutingAttestationRequest,
        attestations: Sequence[Mapping[str, object]],
        now: int,
    ) -> dict[str, object]:
        if self._received_routing_attestations.get(
            request.routing_request_set_hash
        ) != request:
            raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
        unsigned: dict[str, object] = {
            "schema": _ROUTING_ATTESTATION_RESPONSE_SCHEMA,
            "call_intent_hash": request.call_intent_hash,
            "preparation_id": request.preparation_id,
            "routing_request_set_hash": request.routing_request_set_hash,
            "attestations": [dict(item) for item in attestations],
        }
        payload = {
            **unsigned,
            "routing_registry_binding_hash": _private_payload_hash(unsigned),
        }
        normalized = _normalize_routing_attestation_response(
            payload, request=request, now=now
        )
        _validate_private_packet_size(normalized, _MAX_ROUTING_ATTESTATION_BYTES)
        self._send_validated_private(
            kind="routing_attestation_response",
            action_id=request.preparation_id,
            payload=normalized,
        )
        del self._received_routing_attestations[request.routing_request_set_hash]
        return normalized

    def receive_routing_attestation_response(
        self, *, request: RoutingAttestationRequest, now: int
    ) -> dict[str, object]:
        if self._pending_routing_attestations.get(
            request.routing_request_set_hash
        ) != request:
            raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
        try:
            message = self._receive_private()
            if (
                message.kind != "routing_attestation_response"
                or message.action_id != request.preparation_id
            ):
                raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
            normalized = _normalize_routing_attestation_response(
                message.payload, request=request, now=now
            )
        except HostBridgeError:
            self._poison()
            raise
        del self._pending_routing_attestations[request.routing_request_set_hash]
        return normalized

    def send_fast_lane_worker_terminal_result(
        self,
        *,
        terminal_result: Mapping[str, object],
        correlation_id: str,
        expected: Mapping[str, object],
        expires_at: int,
        now: int,
    ) -> dict[str, object]:
        if type(expires_at) is not int or expires_at <= now:
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
        normalized = _normalize_fast_lane_worker_terminal_result(
            terminal_result, expected=expected, expires_at=expires_at, now=now
        )
        receipt_hash = cast(str, normalized["terminal_receipt_hash"])
        if receipt_hash in self._pending_fast_lane_terminals:
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
        _validate_terminal_correlation(correlation_id)
        self._send_validated_private(
            kind="fast_lane_worker_terminal_result",
            action_id=correlation_id,
            payload=normalized,
        )
        self._pending_fast_lane_terminals[receipt_hash] = normalized
        return normalized

    def receive_fast_lane_worker_terminal_result(
        self,
        *,
        correlation_id: str,
        expected: Mapping[str, object],
        expires_at: int | None = None,
        now: int,
    ) -> dict[str, object]:
        _validate_terminal_correlation(correlation_id)
        try:
            if expires_at is not None and (
                type(expires_at) is not int or expires_at <= now
            ):
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            message = self._receive_private()
            if (
                message.kind != "fast_lane_worker_terminal_result"
                or message.action_id != correlation_id
            ):
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            normalized = _normalize_fast_lane_worker_terminal_result(
                message.payload, expected=expected, expires_at=expires_at, now=now
            )
            receipt_hash = cast(str, normalized["terminal_receipt_hash"])
            if receipt_hash in self._received_fast_lane_terminals:
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
        except HostBridgeError:
            self._poison()
            raise
        self._received_fast_lane_terminals.add(receipt_hash)
        return normalized

    def receive_next_fast_lane_worker_terminal_result(
        self,
        *,
        expected_by_assignment: Mapping[tuple[str, str], Mapping[str, object]],
        expires_at_by_assignment: Mapping[tuple[str, str], int] | None = None,
        now: int,
    ) -> tuple[str, dict[str, object]]:
        """Receive the next generation-bound terminal and resolve its stored batch binding."""

        try:
            message = self._receive_private()
            if message.kind != "fast_lane_worker_terminal_result":
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            _validate_terminal_correlation(message.action_id)
            payload = message.payload
            batch_hash = payload.get("batch_hash")
            task_id = payload.get("task_id")
            key = (cast(str, batch_hash), cast(str, task_id))
            expected = expected_by_assignment.get(key)
            if expected is None:
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            lease_expires_at = (
                None
                if expires_at_by_assignment is None
                else expires_at_by_assignment.get(key)
            )
            if lease_expires_at is not None and (
                type(lease_expires_at) is not int or lease_expires_at <= now
            ):
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            normalized = _normalize_fast_lane_worker_terminal_result(
                payload,
                expected=expected,
                expires_at=lease_expires_at,
                now=now,
            )
            receipt_hash = cast(str, normalized["terminal_receipt_hash"])
            if receipt_hash in self._received_fast_lane_terminals:
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            self._received_fast_lane_terminals.add(receipt_hash)
        except HostBridgeError:
            self._poison()
            raise
        return message.action_id, normalized

    def send_fast_lane_worker_terminal_ack(
        self,
        *,
        terminal_result: Mapping[str, object],
        correlation_id: str,
        accepted_event_seq: int,
        refill_trigger_hash: str,
    ) -> dict[str, object]:
        receipt_hash = terminal_result.get("terminal_receipt_hash")
        if receipt_hash not in self._received_fast_lane_terminals:
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
        unsigned: dict[str, object] = {
            "schema": _FAST_LANE_TERMINAL_ACK_SCHEMA,
            "call_intent_hash": terminal_result.get("call_intent_hash"),
            "preparation_id": terminal_result.get("preparation_id"),
            "batch_hash": terminal_result.get("batch_hash"),
            "task_id": terminal_result.get("task_id"),
            "terminal_receipt_hash": receipt_hash,
            "accepted_event_seq": accepted_event_seq,
            "refill_trigger_hash": refill_trigger_hash,
        }
        ack = {**unsigned, "ack_hash": _private_payload_hash(unsigned)}
        normalized = _normalize_fast_lane_worker_terminal_ack(
            ack, terminal_result=terminal_result
        )
        _validate_terminal_correlation(correlation_id)
        self._send_validated_private(
            kind="fast_lane_worker_terminal_ack",
            action_id=correlation_id,
            payload=normalized,
        )
        self._received_fast_lane_terminals.remove(cast(str, receipt_hash))
        return normalized

    def send_fast_lane_refill_registry_request(
        self,
        *,
        call_intent_hash: str,
        preparation_id: str,
        source_plan_hash: str,
        index_context_hash: str,
        routing_registry_binding_hash: str,
        source_plan_task_ids: Sequence[str],
        initial_skeletons: Sequence[Mapping[str, object]],
        remaining_skeletons: Sequence[Mapping[str, object]],
        index_attestation_refs: Sequence[Mapping[str, object]],
        skeleton_package_hash: str,
        now: int,
    ) -> FastLaneRefillRegistryRequest:
        """Register the authenticated remaining V5 skeletons with the Host.

        The queue hash is derived from the complete unsigned payload.  It is
        also used as the action suffix, making retries/replays visible to the
        host without exposing any task content outside the authenticated frame.
        """

        normalized_initial = [
            _normalize_assignment_skeleton(item) for item in initial_skeletons
        ]
        normalized_remaining = [
            _normalize_assignment_skeleton(item) for item in remaining_skeletons
        ]
        expected_package_hash = _validate_skeleton_package_coverage(
            normalized_initial,
            normalized_remaining,
            source_plan_hash=source_plan_hash,
            source_plan_task_ids=source_plan_task_ids,
        )
        if (
            type(skeleton_package_hash) is not str
            or _DIGEST.fullmatch(skeleton_package_hash) is None
        ):
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
        if not hmac.compare_digest(skeleton_package_hash, expected_package_hash):
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
        payload_without_hash: dict[str, object] = {
            "schema": _FAST_LANE_REFILL_REGISTRY_SCHEMA,
            "call_intent_hash": call_intent_hash,
            "preparation_id": preparation_id,
            "source_plan_hash": source_plan_hash,
            "index_context_hash": index_context_hash,
            "routing_registry_binding_hash": routing_registry_binding_hash,
            "remaining_skeletons": normalized_remaining,
            "index_attestation_refs": [dict(item) for item in index_attestation_refs],
        }
        queue_registry_hash = _private_payload_hash(payload_without_hash)
        payload = {
            **payload_without_hash,
            "queue_registry_hash": queue_registry_hash,
        }
        request = _normalize_fast_lane_refill_registry_request(
            payload,
            now=now,
            initial_skeletons=normalized_initial,
            source_plan_task_ids=source_plan_task_ids,
            skeleton_package_hash=expected_package_hash,
        )
        if request.queue_registry_hash in self._sent_fast_lane_refill_registries:
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
        _validate_private_packet_size(payload, _MAX_COMPILER_EVIDENCE_BYTES)
        action_id = _FAST_LANE_REFILL_REGISTRY_ACTION_PREFIX + queue_registry_hash[7:]
        self._send_validated_private(
            kind="fast_lane_refill_registry", action_id=action_id, payload=payload
        )
        self._sent_fast_lane_refill_registries.add(request.queue_registry_hash)
        return request

    def receive_fast_lane_refill_registry_request(
        self,
        *,
        source_plan_task_ids: Sequence[str],
        initial_skeletons: Sequence[Mapping[str, object]],
        now: int,
    ) -> FastLaneRefillRegistryRequest:
        """Receive and consume one authenticated refill registry request."""

        try:
            message = self._receive_private()
            request = _normalize_fast_lane_refill_registry_request(
                message.payload,
                now=now,
                initial_skeletons=initial_skeletons,
                source_plan_task_ids=source_plan_task_ids,
            )
            expected_action = (
                _FAST_LANE_REFILL_REGISTRY_ACTION_PREFIX
                + request.queue_registry_hash[7:]
            )
            if (
                message.kind != "fast_lane_refill_registry"
                or message.action_id != expected_action
                or request.queue_registry_hash in self._received_fast_lane_refill_registries
            ):
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
        except HostBridgeError:
            self._poison()
            raise
        self._received_fast_lane_refill_registries.add(request.queue_registry_hash)
        return request

    def receive_fast_lane_worker_terminal_ack(
        self, *, terminal_result: Mapping[str, object], correlation_id: str
    ) -> dict[str, object]:
        receipt_hash = terminal_result.get("terminal_receipt_hash")
        if self._pending_fast_lane_terminals.get(cast(str, receipt_hash)) != dict(
            terminal_result
        ):
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
        _validate_terminal_correlation(correlation_id)
        try:
            message = self._receive_private()
            if (
                message.kind != "fast_lane_worker_terminal_ack"
                or message.action_id != correlation_id
            ):
                raise HostBridgeError("HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID")
            normalized = _normalize_fast_lane_worker_terminal_ack(
                message.payload, terminal_result=terminal_result
            )
        except HostBridgeError:
            self._poison()
            raise
        del self._pending_fast_lane_terminals[cast(str, receipt_hash)]
        return normalized

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

    def send_fast_lane_dispatch_batch(
        self,
        *,
        batch: Mapping[str, object],
        binding: host_envelopes.EnvelopeBinding,
        correlation_id: str,
        now: int,
    ) -> OperationReceipt:
        """Send dispatch only through its closed, compiler-authorized envelope."""

        try:
            envelope = host_envelopes.build_fast_lane_dispatch_envelope(
                batch=batch,
                binding=binding,
                correlation_id=correlation_id,
                now=now,
            )
            receipt = _operation_receipt(envelope, now=now)
            payload = {
                "schema": _OPERATION_REQUEST_SCHEMA,
                "correlation_id": receipt.correlation_id,
                "envelope": envelope,
                "envelope_hash": receipt.envelope_hash,
            }
            _validate_private_packet_size(payload, _MAX_OPERATION_PACKET_BYTES)
        except host_envelopes.HostEnvelopeError as error:
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
        if self._operation_is_registered_or_tombstoned(receipt, now=now):
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
        self._ensure_terminal_operation_tombstone_capacity(receipt, now=now)
        self._send_validated_private(
            kind="operation_request", action_id=receipt.task_id, payload=payload
        )
        # The host owns execution/terminal lifecycle for this batch. Retain only
        # an expiry-bounded replay tombstone in the compiler process.
        self._remember_terminal_operation(receipt, now=now)
        return receipt

    def send_compiler_evidence_request(
        self,
        *,
        preparation_id: str,
        call_intent_hash: str,
        request_hash: str,
        reasoning_effort: str,
        requested_route_pairs: Sequence[Mapping[str, object]],
        assignment_skeletons: Sequence[Mapping[str, object]],
        project_index_attestation_refs: Sequence[Mapping[str, object]],
        routing_registry_binding_hash: str,
        now: int,
    ) -> CompilerEvidenceRequest:
        """Request one one-time compiler binding over this authenticated session."""

        nonce = _b64encode(secrets.token_bytes(32))
        request = _normalize_compiler_evidence_request(
            {
                "schema": _COMPILER_EVIDENCE_REQUEST_SCHEMA,
                "preparation_id": preparation_id,
                "call_intent_hash": call_intent_hash,
                "request_hash": request_hash,
                "reasoning_effort": reasoning_effort,
                "requested_route_pairs": list(requested_route_pairs),
                "assignment_skeletons": list(assignment_skeletons),
                "project_index_attestation_refs": list(
                    project_index_attestation_refs
                ),
                "routing_registry_binding_hash": routing_registry_binding_hash,
                "nonce": nonce,
                "expires_at": now + _COMPILER_EVIDENCE_TTL_SECONDS,
            },
            now=now,
        )
        if request.preparation_id in self._pending_compiler_evidence:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        payload = _compiler_evidence_request_payload(request)
        _validate_private_packet_size(payload, _MAX_COMPILER_EVIDENCE_BYTES)
        self._send_validated_private(
            kind="compiler_evidence_request",
            action_id=request.preparation_id,
            payload=payload,
        )
        self._pending_compiler_evidence[request.preparation_id] = request
        return request

    def send_project_index_attestation(
        self, *, attestation: Mapping[str, object], now: int
    ) -> dict[str, object]:
        """Send one persisted Project Index fact through the authenticated sideband."""

        normalized = _normalize_project_index_attestation(attestation, now=now)
        attestation_hash = normalized["attestation_hash"]
        assert type(attestation_hash) is str
        if attestation_hash in self._received_project_index_attestations:
            raise HostBridgeError("HOST_BRIDGE_PROJECT_INDEX_ATTESTATION_INVALID")
        _validate_private_packet_size(
            normalized, _MAX_PROJECT_INDEX_ATTESTATION_BYTES
        )
        self._send_validated_private(
            kind="project_index_attestation",
            action_id=cast(str, normalized["correlation_id"]),
            payload=normalized,
        )
        # This process never needs the actual root after deriving the opaque facts.
        return normalized

    def receive_project_index_attestation(self, *, now: int) -> dict[str, object]:
        """Receive one exact, expiry-bounded Project Index attestation once."""

        try:
            message = self._receive_private()
            if message.kind != "project_index_attestation":
                raise HostBridgeError(
                    "HOST_BRIDGE_PROJECT_INDEX_ATTESTATION_INVALID"
                )
            normalized = _normalize_project_index_attestation(
                message.payload, now=now
            )
            if message.action_id != normalized["correlation_id"]:
                raise HostBridgeError(
                    "HOST_BRIDGE_PROJECT_INDEX_ATTESTATION_INVALID"
                )
            attestation_hash = normalized["attestation_hash"]
            assert type(attestation_hash) is str
            if attestation_hash in self._received_project_index_attestations:
                raise HostBridgeError(
                    "HOST_BRIDGE_PROJECT_INDEX_ATTESTATION_INVALID"
                )
        except HostBridgeError:
            self._poison()
            raise
        self._received_project_index_attestations.add(attestation_hash)
        return normalized

    def receive_compiler_evidence_request(self, *, now: int) -> CompilerEvidenceRequest:
        """Receive one exact compiler request for a host-side registry lookup."""

        try:
            message = self._receive_private()
            request = _parse_compiler_evidence_request(message, now=now)
            if request.preparation_id in self._received_compiler_evidence:
                raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        except HostBridgeError:
            self._poison()
            raise
        self._received_compiler_evidence.add(request.preparation_id)
        return request

    def send_compiler_evidence_response(
        self,
        *,
        request: CompilerEvidenceRequest,
        response: Mapping[str, object],
        now: int,
    ) -> None:
        """Return only registry-bound facts for a request received on this session."""

        normalized_request = _normalize_compiler_evidence_request(
            _compiler_evidence_request_payload(request), now=now
        )
        if normalized_request.preparation_id not in self._received_compiler_evidence:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        normalized = _normalize_compiler_evidence_response(
            response, request=normalized_request, now=now
        )
        _validate_private_packet_size(normalized, _MAX_COMPILER_EVIDENCE_BYTES)
        self._send_validated_private(
            kind="compiler_evidence_response",
            action_id=normalized_request.preparation_id,
            payload=normalized,
        )
        self._received_compiler_evidence.remove(normalized_request.preparation_id)

    def receive_compiler_evidence_response(
        self, *, request: CompilerEvidenceRequest, now: int
    ) -> dict[str, object]:
        """Consume one exact response bound to the still-pending request and nonce."""

        normalized_request = _normalize_compiler_evidence_request(
            _compiler_evidence_request_payload(request), now=now
        )
        if self._pending_compiler_evidence.get(request.preparation_id) != request:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        try:
            message = self._receive_private()
            if (
                message.kind != "compiler_evidence_response"
                or message.action_id != request.preparation_id
            ):
                raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
            normalized = _normalize_compiler_evidence_response(
                message.payload, request=normalized_request, now=now
            )
        except HostBridgeError:
            self._poison()
            raise
        del self._pending_compiler_evidence[request.preparation_id]
        return normalized

    def send_storage_profile_request(
        self,
        *,
        call_intent_hash: str,
        preparation_id: str,
        task_id: str,
        source_plan_hash: str,
        index_attestation_hash: str,
    ) -> StorageProfileRequest:
        """Ask the Host for one path-free profile tied to a private session."""

        nonce = _b64encode(secrets.token_bytes(32))
        unsigned = {
            "schema": _STORAGE_PROFILE_REQUEST_SCHEMA,
            "call_intent_hash": call_intent_hash,
            "preparation_id": preparation_id,
            "task_id": task_id,
            "source_plan_hash": source_plan_hash,
            "index_attestation_hash": index_attestation_hash,
            "nonce": nonce,
        }
        request = _normalize_storage_profile_request(
            {**unsigned, "request_hash": _private_payload_hash(unsigned)}
        )
        if request.request_hash in self._pending_storage_profiles:
            raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
        payload = _storage_profile_request_payload(request)
        _validate_private_packet_size(payload, _MAX_STORAGE_PROFILE_BYTES)
        self._send_validated_private(
            kind="storage_profile_request",
            action_id=_storage_profile_action_id(request),
            payload=payload,
        )
        self._pending_storage_profiles[request.request_hash] = request
        return request

    def receive_storage_profile_request(self) -> StorageProfileRequest:
        """Receive exactly one Host-bound storage profile request once."""

        try:
            message = self._receive_private()
            request = _parse_storage_profile_request(message)
            if request.request_hash in self._received_storage_profiles:
                raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
        except HostBridgeError:
            self._poison()
            raise
        self._received_storage_profiles.add(request.request_hash)
        return request

    def send_storage_profile_response(
        self,
        *,
        request: StorageProfileRequest,
        response: Mapping[str, object],
    ) -> None:
        """Return one exact Host profile to the request's private session."""

        normalized_request = _normalize_storage_profile_request(
            _storage_profile_request_payload(request)
        )
        if normalized_request.request_hash not in self._received_storage_profiles:
            raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
        normalized = _normalize_storage_profile_response(
            response, request=normalized_request
        )
        _validate_private_packet_size(normalized, _MAX_STORAGE_PROFILE_BYTES)
        self._send_validated_private(
            kind="storage_profile_response",
            action_id=_storage_profile_action_id(normalized_request),
            payload=normalized,
        )
        self._received_storage_profiles.remove(normalized_request.request_hash)

    def receive_storage_profile_response(
        self, *, request: StorageProfileRequest
    ) -> dict[str, object]:
        """Consume one exact response bound to its still-pending request."""

        normalized_request = _normalize_storage_profile_request(
            _storage_profile_request_payload(request)
        )
        if (
            self._pending_storage_profiles.get(normalized_request.request_hash)
            != normalized_request
        ):
            raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
        try:
            message = self._receive_private()
            if (
                message.kind != "storage_profile_response"
                or message.action_id != _storage_profile_action_id(normalized_request)
            ):
                raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
            normalized = _normalize_storage_profile_response(
                message.payload, request=normalized_request
            )
        except HostBridgeError:
            self._poison()
            raise
        del self._pending_storage_profiles[normalized_request.request_hash]
        return normalized

    def receive_operation(
        self,
        *,
        now: int,
        expected: host_envelopes.EnvelopeExpectation | None = None,
    ) -> OperationReceipt:
        """Receive one validated operation and retain its predecessor receipt."""

        try:
            message = self._receive_private()
            receipt = _parse_operation_request(
                message,
                now=now,
                expected=expected,
                allowed_kinds={"coordinator_assignment", "peer_evidence_handoff"},
            )
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

    def receive_fast_lane_dispatch_batch(self, *, now: int) -> OperationReceipt:
        """Receive only the typed Fast Lane operation; generic receive stays closed."""

        try:
            message = self._receive_private()
            receipt = _parse_operation_request(
                message,
                now=now,
                expected=None,
                allowed_kinds={"fast_lane_dispatch_batch"},
            )
            if self._operation_is_registered_or_tombstoned(receipt, now=now):
                raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
        except (host_envelopes.HostEnvelopeError, HostBridgeError) as error:
            self._poison()
            if isinstance(error, HostBridgeError):
                raise
            raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID") from error
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
            _assert_terminal_predecessor(normalized, normalized_predecessor, now=now)
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
        """Write one canonical authenticated frame to the private transport."""

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
        with self._io_lock:
            if kind not in _MESSAGE_KINDS or kind == "session_open":
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            _validate_frame_action_id(kind, action_id, payload)
            self._ensure_open()
            try:
                encoded_payload = _json_object(payload)
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
            except (HostBridgeError, RecursionError):
                self._poison()
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from None
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

        with self._io_lock:
            self._ensure_open()
            try:
                frame = _decode_frame(
                    self._read_raw_frame(
                        self._read_fd,
                        cancel_event=self._cancel_event,
                        cancel_fd=self._cancel_read_fd,
                    )
                )
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

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_event.set()
            try:
                os.write(self._cancel_write_fd, b"\\x00")
            except OSError:
                pass
            cancel_descriptors = {
                self._cancel_read_fd,
                self._cancel_write_fd,
            }
            self._cancel_read_fd = -1
            self._cancel_write_fd = -1
            for descriptor in cancel_descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
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
            or not _is_valid_frame_action_id(kind, action_id, payload)
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
    def _read_raw_frame(
        descriptor: int,
        *,
        cancel_event: Event | None = None,
        cancel_fd: int | None = None,
    ) -> bytes:
        header = _read_exact(
            descriptor, 4, cancel_event=cancel_event, cancel_fd=cancel_fd
        )
        size = struct.unpack("!I", header)[0]
        if size == 0 or size > _MAX_FRAME_BYTES:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        return _read_exact(
            descriptor, size, cancel_event=cancel_event, cancel_fd=cancel_fd
        )

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


def _normalize_capability_probe_v2(value: object, *, now: int) -> CapabilityProbeV2:
    expected_schemas = [
        "fastlane-host-dispatch-request-v1",
        "fastlane-routing-request-v5",
        "project-index-attestation-v1",
    ]
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "call_intent_hash",
            "preparation_id",
            "requested_capability_schemas",
            "probe_hash",
        }
        or value.get("schema") != _CAPABILITY_PROBE_SCHEMA_V2
        or type(now) is not int
        or now < 0
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    call_intent_hash = value.get("call_intent_hash")
    preparation_id = value.get("preparation_id")
    probe_hash = value.get("probe_hash")
    if (
        type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(preparation_id) is not str
        or _IDENTIFIER.fullmatch(preparation_id) is None
        or value.get("requested_capability_schemas") != expected_schemas
        or type(probe_hash) is not str
        or _DIGEST.fullmatch(probe_hash) is None
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    unsigned = dict(value)
    unsigned.pop("probe_hash")
    if not hmac.compare_digest(probe_hash, _private_payload_hash(unsigned)):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    return CapabilityProbeV2(
        call_intent_hash=call_intent_hash,
        preparation_id=preparation_id,
        requested_capability_schemas=tuple(expected_schemas),
        probe_hash=probe_hash,
        expires_at=now + _CAPABILITY_V2_TTL_SECONDS,
    )


def _normalize_capability_report_v2(
    value: object, *, probe: CapabilityProbeV2, now: int
) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "call_intent_hash",
            "preparation_id",
            "probe_hash",
            "host_capabilities",
            "scheduler_facts",
            "report_hash",
        }
        or value.get("schema") != _CAPABILITY_REPORT_SCHEMA_V2
        or value.get("call_intent_hash") != probe.call_intent_hash
        or value.get("preparation_id") != probe.preparation_id
        or value.get("probe_hash") != probe.probe_hash
        or type(now) is not int
        or not now < probe.expires_at
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    try:
        from devkit_fastlane.scripts import fastlane_routing

        policy = fastlane_routing.load_policy_v5()
        host = fastlane_routing._normalise_host_v5(value["host_capabilities"], policy)
        scheduler = fastlane_routing._normalise_scheduler(value["scheduler_facts"])
    except Exception as error:
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID") from error
    report_hash = value.get("report_hash")
    unsigned = dict(value)
    unsigned.pop("report_hash")
    if (
        value["host_capabilities"] != host
        or value["scheduler_facts"] != scheduler
        or type(report_hash) is not str
        or _DIGEST.fullmatch(report_hash) is None
        or not hmac.compare_digest(report_hash, _private_payload_hash(unsigned))
    ):
        raise HostBridgeError("HOST_BRIDGE_CAPABILITY_INVALID")
    _validate_private_packet_size(value, _MAX_CAPABILITY_PACKET_BYTES)
    return dict(value)


def _normalize_routing_attestation_request(
    value: object, *, now: int
) -> RoutingAttestationRequest:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "call_intent_hash",
            "preparation_id",
            "routing_requests",
            "routing_request_set_hash",
        }
        or value.get("schema") != _ROUTING_ATTESTATION_REQUEST_SCHEMA
        or type(now) is not int
        or now < 0
    ):
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
    call_intent_hash = value.get("call_intent_hash")
    preparation_id = value.get("preparation_id")
    request_set_hash = value.get("routing_request_set_hash")
    candidates = value.get("routing_requests")
    if (
        type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(preparation_id) is not str
        or _IDENTIFIER.fullmatch(preparation_id) is None
        or type(request_set_hash) is not str
        or _DIGEST.fullmatch(request_set_hash) is None
        or type(candidates) is not list
        or not 1 <= len(candidates) <= 16
    ):
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
    try:
        from devkit_fastlane.scripts import fastlane_routing

        policy = fastlane_routing.load_policy_v5()
        normalized = [
            fastlane_routing._normalise_request_v5(candidate, policy)
            for candidate in candidates
        ]
    except Exception as error:
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID") from error
    task_ids = [request["task"]["task_id"] for request in normalized]
    if (
        candidates != normalized
        or any(request["child_route_attestation"] is not None for request in normalized)
        or task_ids != sorted(task_ids)
        or len(set(task_ids)) != len(task_ids)
        or not hmac.compare_digest(request_set_hash, _private_payload_hash(normalized))
    ):
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
    _validate_private_packet_size(value, _MAX_ROUTING_ATTESTATION_BYTES)
    return RoutingAttestationRequest(
        call_intent_hash=call_intent_hash,
        preparation_id=preparation_id,
        routing_requests=tuple(dict(request) for request in normalized),
        routing_request_set_hash=request_set_hash,
        expires_at=now + _ROUTING_ATTESTATION_TTL_SECONDS,
    )


def _normalize_routing_attestation_response(
    value: object, *, request: RoutingAttestationRequest, now: int
) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "call_intent_hash",
            "preparation_id",
            "routing_request_set_hash",
            "attestations",
            "routing_registry_binding_hash",
        }
        or value.get("schema") != _ROUTING_ATTESTATION_RESPONSE_SCHEMA
        or value.get("call_intent_hash") != request.call_intent_hash
        or value.get("preparation_id") != request.preparation_id
        or value.get("routing_request_set_hash") != request.routing_request_set_hash
        or type(now) is not int
        or not now < request.expires_at
    ):
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
    items = value.get("attestations")
    registry_hash = value.get("routing_registry_binding_hash")
    unsigned = dict(value)
    unsigned.pop("routing_registry_binding_hash")
    if (
        type(items) is not list
        or len(items) != len(request.routing_requests)
        or type(registry_hash) is not str
        or _DIGEST.fullmatch(registry_hash) is None
        or not hmac.compare_digest(registry_hash, _private_payload_hash(unsigned))
    ):
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
    normalized_items: list[dict[str, object]] = []
    try:
        from devkit_fastlane.scripts import fastlane_routing

        policy = fastlane_routing.load_policy_v5()
        for original_request, item in zip(request.routing_requests, items, strict=True):
            if type(item) is not dict or set(item) != {
                "task_id",
                "request_binding_hash",
                "attestation",
            }:
                raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
            task_id = original_request["task"]["task_id"]
            binding_hash = fastlane_routing.v5_request_binding_hash(original_request)
            if item.get("task_id") != task_id or item.get(
                "request_binding_hash"
            ) != binding_hash:
                raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
            routed_request = dict(original_request)
            routed_request["child_route_attestation"] = item.get("attestation")
            normalized_request = fastlane_routing._normalise_request_v5(
                routed_request, policy
            )
            fastlane_routing.route_v5(normalized_request, policy=policy)
            if routed_request != normalized_request:
                raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
            normalized_items.append(dict(item))
    except HostBridgeError:
        raise
    except Exception as error:
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID") from error
    if items != normalized_items:
        raise HostBridgeError("HOST_BRIDGE_ROUTING_ATTESTATION_INVALID")
    _validate_private_packet_size(value, _MAX_ROUTING_ATTESTATION_BYTES)
    return dict(value)


_FAST_LANE_TERMINAL_BINDING_FIELDS: Final = (
    fastlane_terminal_protocol.TERMINAL_BINDING_FIELDS
)


def _normalize_fast_lane_worker_terminal_result(
    value: object,
    *,
    expected: Mapping[str, object],
    expires_at: int | None = None,
    now: int,
) -> dict[str, object]:
    try:
        return fastlane_terminal_protocol.normalize_worker_terminal_result(
            value, expected=expected, expires_at=expires_at, now=now
        )
    except fastlane_terminal_protocol.FastLaneTerminalProtocolError as error:
        raise HostBridgeError(error.code) from error


def _normalize_fast_lane_worker_terminal_ack(
    value: object, *, terminal_result: Mapping[str, object]
) -> dict[str, object]:
    try:
        return fastlane_terminal_protocol.normalize_worker_terminal_ack(
            value, terminal_result=terminal_result
        )
    except fastlane_terminal_protocol.FastLaneTerminalProtocolError as error:
        raise HostBridgeError(error.code) from error


def _validate_terminal_correlation(value: object) -> None:
    try:
        fastlane_terminal_protocol.validate_terminal_correlation(value)
    except fastlane_terminal_protocol.FastLaneTerminalProtocolError as error:
        raise HostBridgeError(error.code) from error


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
        if (
            type(capability_hash) is not str
            or _DIGEST.fullmatch(capability_hash) is None
        ):
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


def _normalize_compiler_evidence_request(
    value: object, *, now: int
) -> CompilerEvidenceRequest:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "preparation_id",
            "call_intent_hash",
            "request_hash",
            "reasoning_effort",
            "requested_route_pairs",
            "assignment_skeletons",
            "project_index_attestation_refs",
            "routing_registry_binding_hash",
            "nonce",
            "expires_at",
        }
        or value.get("schema") != _COMPILER_EVIDENCE_REQUEST_SCHEMA
        or type(now) is not int
        or now < 0
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    preparation_id = value.get("preparation_id")
    call_intent_hash = value.get("call_intent_hash")
    request_hash = value.get("request_hash")
    reasoning_effort = value.get("reasoning_effort")
    route_pairs = value.get("requested_route_pairs")
    assignment_skeletons = value.get("assignment_skeletons")
    attestation_refs = value.get("project_index_attestation_refs")
    routing_registry_binding_hash = value.get("routing_registry_binding_hash")
    nonce = value.get("nonce")
    expires_at = value.get("expires_at")
    if (
        type(preparation_id) is not str
        or _IDENTIFIER.fullmatch(preparation_id) is None
        or type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(request_hash) is not str
        or _DIGEST.fullmatch(request_hash) is None
        or reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}
        or type(route_pairs) is not list
        or not route_pairs
        or len(route_pairs) > 16
        or type(assignment_skeletons) is not list
        or not assignment_skeletons
        or len(assignment_skeletons) > 16
        or type(attestation_refs) is not list
        or len(attestation_refs) != len(assignment_skeletons)
        or type(routing_registry_binding_hash) is not str
        or _DIGEST.fullmatch(routing_registry_binding_hash) is None
        or type(nonce) is not str
        or type(expires_at) is not int
        or not now < expires_at <= now + _COMPILER_EVIDENCE_TTL_SECONDS
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    try:
        if len(_b64decode(nonce)) != 32 or _b64encode(_b64decode(nonce)) != nonce:
            raise ValueError
    except ValueError as error:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID") from error
    normalized_pairs: list[tuple[str, str]] = []
    for pair in route_pairs:
        if type(pair) is not dict or set(pair) != {"model", "effort"}:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        model = pair.get("model")
        effort = pair.get("effort")
        if (
            type(model) is not str
            or _IDENTIFIER.fullmatch(model) is None
            or type(effort) is not str
            or _IDENTIFIER.fullmatch(effort) is None
            or effort == "ultra"
        ):
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        normalized_pairs.append((model, effort))
    if normalized_pairs != sorted(set(normalized_pairs)):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    normalized_skeletons = [
        _normalize_assignment_skeleton(item) for item in assignment_skeletons
    ]
    normalized_refs = [
        _normalize_project_index_attestation_ref(item) for item in attestation_refs
    ]
    task_ids = [cast(str, item["task_id"]) for item in normalized_skeletons]
    if len(set(task_ids)) != len(task_ids):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    if [item["task_id"] for item in normalized_refs] != task_ids:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    receipt_bindings = {
        _canonical_bytes(
            {key: value for key, value in item.items() if key != "task_id"}
        )
        for item in normalized_refs
    }
    if len(receipt_bindings) != 1:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    skeleton_pairs: list[tuple[str, str]] = []
    for item in normalized_skeletons:
        proof = cast(dict[str, object], item["routing_proof"])
        result = cast(dict[str, object], proof["result"])
        route = cast(dict[str, object], result["route"])
        skeleton_pairs.append((cast(str, route["model"]), cast(str, route["effort"])))
    skeleton_pairs = sorted(set(skeleton_pairs))
    if skeleton_pairs != normalized_pairs:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    for skeleton, reference in zip(normalized_skeletons, normalized_refs, strict=True):
        if skeleton["index_context_hash"] != reference["index_context_hash"]:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    if len({item["source_plan_hash"] for item in normalized_skeletons}) != 1:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    planner_preimage = {
        "schema": "2718lab-devkit/fastlane-host-planner-request-v1",
        "action": "plan_dispatch",
        "assignment_skeletons": normalized_skeletons,
        "project_index_attestation_refs": normalized_refs,
    }
    if not hmac.compare_digest(request_hash, _private_payload_hash(planner_preimage)):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    return CompilerEvidenceRequest(
        preparation_id=preparation_id,
        call_intent_hash=call_intent_hash,
        request_hash=request_hash,
        reasoning_effort=reasoning_effort,
        requested_route_pairs=tuple(normalized_pairs),
        assignment_skeletons=tuple(normalized_skeletons),
        project_index_attestation_refs=tuple(normalized_refs),
        routing_registry_binding_hash=routing_registry_binding_hash,
        nonce=nonce,
        expires_at=expires_at,
    )


def _compiler_evidence_request_payload(
    request: CompilerEvidenceRequest,
) -> dict[str, object]:
    if type(request) is not CompilerEvidenceRequest:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    return {
        "schema": _COMPILER_EVIDENCE_REQUEST_SCHEMA,
        "preparation_id": request.preparation_id,
        "call_intent_hash": request.call_intent_hash,
        "request_hash": request.request_hash,
        "reasoning_effort": request.reasoning_effort,
        "requested_route_pairs": [
            {"model": model, "effort": effort}
            for model, effort in request.requested_route_pairs
        ],
        "assignment_skeletons": [dict(item) for item in request.assignment_skeletons],
        "project_index_attestation_refs": [
            dict(item) for item in request.project_index_attestation_refs
        ],
        "routing_registry_binding_hash": request.routing_registry_binding_hash,
        "nonce": request.nonce,
        "expires_at": request.expires_at,
    }


def _parse_compiler_evidence_request(
    message: PrivateHostMessage, *, now: int
) -> CompilerEvidenceRequest:
    if message.kind != "compiler_evidence_request":
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    request = _normalize_compiler_evidence_request(message.payload, now=now)
    if message.action_id != request.preparation_id:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    _validate_private_packet_size(message.payload, _MAX_COMPILER_EVIDENCE_BYTES)
    return request


def _normalize_assignment_skeleton(value: object) -> dict[str, object]:
    fields = {
        "task_id",
        "routing_proof",
        "write_scope",
        "concurrency_mode",
        "dispatch_order",
        "index_context_hash",
        "predecessor_hash",
        "source_plan_hash",
    }
    if type(value) is not dict or set(value) != fields:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    proof = value.get("routing_proof")
    if type(proof) is not dict or set(proof) != {
        "request",
        "result",
        "request_binding_hash",
        "attestation_hash",
        "routing_context_hash",
        "routing_result_hash",
    }:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    task_id = value.get("task_id")
    scope = value.get("write_scope")
    mode = value.get("concurrency_mode")
    order = value.get("dispatch_order")
    digest_fields = (
        "request_binding_hash",
        "attestation_hash",
        "routing_context_hash",
        "routing_result_hash",
        "index_context_hash",
        "predecessor_hash",
        "source_plan_hash",
    )
    digest_values = {
        **{field: proof.get(field) for field in digest_fields[:4]},
        **{field: value.get(field) for field in digest_fields[4:]},
    }
    if (
        type(task_id) is not str
        or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
        or any(
            type(item) is not str or _DIGEST.fullmatch(item) is None
            for item in digest_values.values()
        )
        or mode not in {"parallel", "serial", "isolated_worktree"}
        or type(order) is not int
        or not 0 <= order < 16
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    try:
        if type(scope) is not list:
            raise ValueError
        from .fastlane_host_adapter import _canonical_write_scope

        normalized_scope = list(_canonical_write_scope(tuple(scope)))
    except (TypeError, ValueError) as error:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID") from error
    try:
        from devkit_fastlane.scripts import fastlane_routing

        request = fastlane_routing._normalise_request_v5(
            proof["request"], fastlane_routing.load_policy_v5()
        )
        result = fastlane_routing.route_v5(request)
        task = cast(dict[str, object], request["task"])
        scheduler = cast(dict[str, object], request["scheduler_facts"])
        attestation = cast(dict[str, object], request["child_route_attestation"])
        expected_context = _private_payload_hash(
            {
                "schema": "team-efficiency/fast-lane-routing-context-binding-v1",
                "source_plan_hash": value["source_plan_hash"],
                "task_id": task_id,
                "scheduler_role": task["role"],
                "routing_request_hash": _private_payload_hash(request),
                "scheduler_facts_hash": _private_payload_hash(scheduler),
            }
        )
        if (
            request != proof["request"]
            or result != proof["result"]
            or task["task_id"] != task_id
            or fastlane_routing.v5_request_binding_hash(request)
            != digest_values["request_binding_hash"]
            or attestation.get("attestation_hash") != digest_values["attestation_hash"]
            or expected_context != digest_values["routing_context_hash"]
            or _private_payload_hash(result) != digest_values["routing_result_hash"]
        ):
            raise ValueError
    except Exception as error:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID") from error
    return {
        "task_id": task_id,
        "routing_proof": dict(proof),
        "write_scope": normalized_scope,
        "concurrency_mode": mode,
        "dispatch_order": order,
        "index_context_hash": digest_values["index_context_hash"],
        "predecessor_hash": digest_values["predecessor_hash"],
        "source_plan_hash": digest_values["source_plan_hash"],
    }


def _normalize_project_index_attestation_ref(value: object) -> dict[str, object]:
    fields = {
        "task_id",
        "correlation_id",
        "workspace_id",
        "workspace_binding_hash",
        "root_identity_hash",
        "snapshot_id",
        "snapshot_attestation_hash",
        "query_receipt_hash",
        "index_context_hash",
        "attestation_hash",
    }
    if type(value) is not dict or set(value) != fields:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    task_id = value.get("task_id")
    if type(task_id) is not str or _FAST_LANE_TASK_ID.fullmatch(task_id) is None:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    correlation_id = value.get("correlation_id")
    if not _is_index_correlation(correlation_id):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    for field_name in fields - {"task_id", "correlation_id"}:
        item = value.get(field_name)
        if type(item) is not str or _DIGEST.fullmatch(item) is None:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    return {field_name: value[field_name] for field_name in sorted(fields)}


def _validate_skeleton_package_coverage(
    initial_skeletons: Sequence[Mapping[str, object]],
    remaining_skeletons: Sequence[Mapping[str, object]],
    *,
    source_plan_hash: str,
    source_plan_task_ids: Sequence[str],
) -> str:
    """Bind both waves to one complete, hole-free source-plan package.

    Individual waves retain source dispatch coordinates and may therefore have
    holes.  Only their union is required to cover every source task exactly
    once and to contain every global dispatch coordinate exactly once.
    """

    if (
        type(source_plan_hash) is not str
        or _DIGEST.fullmatch(source_plan_hash) is None
        or not isinstance(source_plan_task_ids, Sequence)
        or isinstance(source_plan_task_ids, (str, bytes, bytearray))
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    source_ids = list(source_plan_task_ids)
    if (
        not 1 <= len(source_ids) <= 16
        or any(
            type(task_id) is not str or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
            for task_id in source_ids
        )
        or len(set(source_ids)) != len(source_ids)
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    if not isinstance(initial_skeletons, Sequence) or isinstance(
        initial_skeletons, (str, bytes, bytearray)
    ) or not isinstance(remaining_skeletons, Sequence) or isinstance(
        remaining_skeletons, (str, bytes, bytearray)
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    combined = [*initial_skeletons, *remaining_skeletons]
    if len(combined) != len(source_ids):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    task_ids: list[str] = []
    orders: list[int] = []
    for skeleton in combined:
        if type(skeleton) is not dict:
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
        task_id = skeleton.get("task_id")
        order = skeleton.get("dispatch_order")
        if (
            type(task_id) is not str
            or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
            or type(order) is not int
            or not 0 <= order < len(source_ids)
            or skeleton.get("source_plan_hash") != source_plan_hash
        ):
            raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
        task_ids.append(task_id)
        orders.append(order)
    if (
        len(set(task_ids)) != len(task_ids)
        or set(task_ids) != set(source_ids)
        or len(set(orders)) != len(orders)
        or set(orders) != set(range(len(source_ids)))
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    ordered = sorted(combined, key=lambda item: cast(int, item["dispatch_order"]))
    return _private_payload_hash(
        {
            "schema": "2718lab-devkit/authenticated-v5-skeleton-package-v1",
            "source_plan_hash": source_plan_hash,
            "task_ids": source_ids,
            "assignment_skeletons": ordered,
        }
    )


def _normalize_fast_lane_refill_registry_request(
    value: object,
    *,
    now: int,
    initial_skeletons: Sequence[Mapping[str, object]] = (),
    source_plan_task_ids: Sequence[str] | None = None,
    skeleton_package_hash: str | None = None,
) -> FastLaneRefillRegistryRequest:
    """Validate the exact9-field authenticated successor-wave registry."""

    fields = {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "source_plan_hash",
        "index_context_hash",
        "routing_registry_binding_hash",
        "remaining_skeletons",
        "index_attestation_refs",
        "queue_registry_hash",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or value.get("schema") != _FAST_LANE_REFILL_REGISTRY_SCHEMA
        or type(now) is not int
        or now < 0
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    call_intent_hash = value.get("call_intent_hash")
    preparation_id = value.get("preparation_id")
    source_plan_hash = value.get("source_plan_hash")
    index_context_hash = value.get("index_context_hash")
    routing_registry_binding_hash = value.get("routing_registry_binding_hash")
    queue_registry_hash = value.get("queue_registry_hash")
    if (
        type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(preparation_id) is not str
        or _IDENTIFIER.fullmatch(preparation_id) is None
        or any(
            type(item) is not str or _DIGEST.fullmatch(item) is None
            for item in (
                source_plan_hash,
                index_context_hash,
                routing_registry_binding_hash,
                queue_registry_hash,
            )
        )
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    raw_skeletons = value.get("remaining_skeletons")
    raw_refs = value.get("index_attestation_refs")
    if (
        type(raw_skeletons) is not list
        or not 1 <= len(raw_skeletons) <= 16
        or type(raw_refs) is not list
        or len(raw_refs) != len(raw_skeletons)
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    try:
        skeletons = [_normalize_assignment_skeleton(item) for item in raw_skeletons]
        refs = [_normalize_project_index_attestation_ref(item) for item in raw_refs]
    except HostBridgeError as error:
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID") from error
    task_ids = [cast(str, item["task_id"]) for item in skeletons]
    ref_task_ids = [cast(str, item["task_id"]) for item in refs]
    if (
        len(set(task_ids)) != len(task_ids)
        or ref_task_ids != task_ids
        or any(
            item["source_plan_hash"] != source_plan_hash
            or item["index_context_hash"] != index_context_hash
            for item in skeletons
        )
        or any(item["index_context_hash"] != index_context_hash for item in refs)
        or raw_skeletons != skeletons
        or raw_refs != refs
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    if source_plan_task_ids is None:
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    try:
        normalized_initial = [
            _normalize_assignment_skeleton(item) for item in initial_skeletons
        ]
        expected_package_hash = _validate_skeleton_package_coverage(
            normalized_initial,
            skeletons,
            source_plan_hash=cast(str, source_plan_hash),
            source_plan_task_ids=source_plan_task_ids,
        )
    except (HostBridgeError, TypeError, ValueError) as error:
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID") from error
    if skeleton_package_hash is None:
        skeleton_package_hash = expected_package_hash
    if (
        type(skeleton_package_hash) is not str
        or _DIGEST.fullmatch(skeleton_package_hash) is None
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    if not hmac.compare_digest(skeleton_package_hash, expected_package_hash):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    if any(
        item["index_context_hash"] != index_context_hash
        for item in normalized_initial
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    unsigned = dict(value)
    unsigned.pop("queue_registry_hash")
    if not hmac.compare_digest(
        cast(str, queue_registry_hash), _private_payload_hash(unsigned)
    ):
        raise HostBridgeError("HOST_BRIDGE_FAST_LANE_REFILL_INVALID")
    return FastLaneRefillRegistryRequest(
        correlation_id=_FAST_LANE_REFILL_REGISTRY_ACTION_PREFIX
        + cast(str, queue_registry_hash)[7:],
        call_intent_hash=cast(str, call_intent_hash),
        preparation_id=cast(str, preparation_id),
        source_plan_hash=cast(str, source_plan_hash),
        index_context_hash=cast(str, index_context_hash),
        routing_registry_binding_hash=cast(str, routing_registry_binding_hash),
        remaining_skeletons=tuple(skeletons),
        index_attestation_refs=tuple(refs),
        queue_registry_hash=cast(str, queue_registry_hash),
        expires_at=now + _COMPILER_EVIDENCE_TTL_SECONDS,
    )


def _normalize_project_index_attestation(
    value: object, *, now: int
) -> dict[str, object]:
    try:
        return project_index_attestation_protocol.normalize_attestation(value, now=now)
    except (
        project_index_attestation_protocol.ProjectIndexAttestationProtocolError
    ) as error:
        raise HostBridgeError(error.code) from error


def build_project_index_attestation(
    *,
    operation: str,
    correlation_id: str,
    material: Mapping[str, object],
    now: int,
) -> dict[str, object]:
    """Build one closed sideband packet from already persisted index material."""
    try:
        return project_index_attestation_protocol.build_attestation(
            operation=operation,
            correlation_id=correlation_id,
            material=material,
            now=now,
        )
    except (
        project_index_attestation_protocol.ProjectIndexAttestationProtocolError
    ) as error:
        raise HostBridgeError(error.code) from error


def _is_index_correlation(value: object) -> bool:
    return project_index_attestation_protocol.is_index_correlation(value)


def _normalize_storage_profile_request(value: object) -> StorageProfileRequest:
    """Validate one exact, nonce-bound private profile request."""

    if (
        type(value) is not dict
        or set(value) != _STORAGE_PROFILE_REQUEST_FIELDS
        or value.get("schema") != _STORAGE_PROFILE_REQUEST_SCHEMA
    ):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    call_intent_hash = value.get("call_intent_hash")
    preparation_id = value.get("preparation_id")
    task_id = value.get("task_id")
    source_plan_hash = value.get("source_plan_hash")
    index_attestation_hash = value.get("index_attestation_hash")
    nonce = value.get("nonce")
    request_hash = value.get("request_hash")
    if (
        type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(preparation_id) is not str
        or _IDENTIFIER.fullmatch(preparation_id) is None
        or type(task_id) is not str
        or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
        or type(source_plan_hash) is not str
        or _DIGEST.fullmatch(source_plan_hash) is None
        or type(index_attestation_hash) is not str
        or _DIGEST.fullmatch(index_attestation_hash) is None
        or type(nonce) is not str
        or type(request_hash) is not str
        or _DIGEST.fullmatch(request_hash) is None
    ):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    try:
        decoded_nonce = _b64decode(nonce)
    except ValueError as error:
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID") from error
    if len(decoded_nonce) != 32 or _b64encode(decoded_nonce) != nonce:
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    unsigned = dict(value)
    unsigned.pop("request_hash")
    if not hmac.compare_digest(request_hash, _private_payload_hash(unsigned)):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    return StorageProfileRequest(
        call_intent_hash=call_intent_hash,
        preparation_id=preparation_id,
        task_id=task_id,
        source_plan_hash=source_plan_hash,
        index_attestation_hash=index_attestation_hash,
        nonce=nonce,
        request_hash=request_hash,
    )


def _storage_profile_request_payload(
    request: StorageProfileRequest,
) -> dict[str, object]:
    if type(request) is not StorageProfileRequest:
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    return {
        "schema": _STORAGE_PROFILE_REQUEST_SCHEMA,
        "call_intent_hash": request.call_intent_hash,
        "preparation_id": request.preparation_id,
        "task_id": request.task_id,
        "source_plan_hash": request.source_plan_hash,
        "index_attestation_hash": request.index_attestation_hash,
        "nonce": request.nonce,
        "request_hash": request.request_hash,
    }


def _storage_profile_action_id(request: StorageProfileRequest) -> str:
    normalized = _normalize_storage_profile_request(
        _storage_profile_request_payload(request)
    )
    return normalized.request_hash[7:]


def _parse_storage_profile_request(
    message: PrivateHostMessage,
) -> StorageProfileRequest:
    if message.kind != "storage_profile_request":
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    request = _normalize_storage_profile_request(message.payload)
    if message.action_id != _storage_profile_action_id(request):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    _validate_private_packet_size(message.payload, _MAX_STORAGE_PROFILE_BYTES)
    return request


def _normalize_storage_profile_response(
    value: object,
    *,
    request: StorageProfileRequest,
) -> dict[str, object]:
    """Validate the exact Host-owned profile and its request bindings.

    ``attestation_hash`` deliberately remains opaque: the Host includes its
    private bridge generation and expiry deadline in that attestation, neither
    of which crosses this Python protocol boundary.  The authenticated frame,
    pending request, and exact profile hash still make substitution fail closed.
    """

    if type(request) is not StorageProfileRequest:
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    normalized_request = _normalize_storage_profile_request(
        _storage_profile_request_payload(request)
    )
    if (
        type(value) is not dict
        or set(value) != _STORAGE_PROFILE_FIELDS
        or value.get("schema") != _STORAGE_PROFILE_SCHEMA
        or value.get("call_intent_hash") != normalized_request.call_intent_hash
        or value.get("preparation_id") != normalized_request.preparation_id
        or value.get("task_id") != normalized_request.task_id
        or value.get("source_plan_hash") != normalized_request.source_plan_hash
        or value.get("index_attestation_hash")
        != normalized_request.index_attestation_hash
    ):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    call_intent_hash = value.get("call_intent_hash")
    preparation_id = value.get("preparation_id")
    task_id = value.get("task_id")
    if (
        type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(preparation_id) is not str
        or _IDENTIFIER.fullmatch(preparation_id) is None
        or type(task_id) is not str
        or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
    ):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
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
        item = value.get(field_name)
        if type(item) is not str or _DIGEST.fullmatch(item) is None:
            raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    target_triple = value.get("target_triple")
    if (
        type(target_triple) is not str
        or _STORAGE_PROFILE_SCALAR.fullmatch(target_triple) is None
        or value.get("profile") != "dev"
    ):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    if value.get("build_env_class") not in _STORAGE_PROFILE_BUILD_ENV_CLASSES:
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    unsigned = {
        field_name: value[field_name]
        for field_name in _STORAGE_PROFILE_FIELDS
        if field_name not in {"profile_hash", "attestation_hash"}
    }
    profile_hash = cast(str, value["profile_hash"])
    if not hmac.compare_digest(profile_hash, _private_payload_hash(unsigned)):
        raise HostBridgeError("HOST_BRIDGE_STORAGE_PROFILE_INVALID")
    return {field_name: value[field_name] for field_name in sorted(_STORAGE_PROFILE_FIELDS)}


def _normalize_compiler_evidence_response(
    value: object, *, request: CompilerEvidenceRequest, now: int
) -> dict[str, object]:
    expected_fields = {
        "schema",
        "preparation_id",
        "request_hash",
        "reasoning_effort",
        "verified_route_result_hashes",
        "verified_lease_scope_bindings",
        "dispatch_facts",
        "dispatch_binding_hashes",
        "nonce",
        "expires_at",
        "registry_binding_hash",
    }
    if (
        type(value) is not dict
        or set(value) != expected_fields
        or value.get("schema") != _COMPILER_EVIDENCE_RESPONSE_SCHEMA
        or value.get("preparation_id") != request.preparation_id
        or value.get("request_hash") != request.request_hash
        or value.get("reasoning_effort") != request.reasoning_effort
        or value.get("nonce") != request.nonce
        or value.get("expires_at") != request.expires_at
        or not now < request.expires_at
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    facts = value.get("dispatch_facts")
    dispatch_hashes = value.get("dispatch_binding_hashes")
    route_hashes = _normalized_sorted_digest_list(
        value.get("verified_route_result_hashes")
    )
    lease_hashes = _normalized_sorted_digest_list(
        value.get("verified_lease_scope_bindings")
    )
    if (
        type(facts) is not list
        or not facts
        or len(facts) > 16
        or type(dispatch_hashes) is not list
        or len(dispatch_hashes) != len(facts)
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    from .fastlane_host_adapter import (
        _dispatch_fact_from_mapping,
        _dispatch_fact_mapping,
        _lease_scope_binding_hash,
        _validate_batch_fences,
    )

    try:
        normalized_facts = tuple(_dispatch_fact_from_mapping(fact) for fact in facts)
        _validate_batch_fences(normalized_facts)
        normalized_mappings = [
            _dispatch_fact_mapping(fact) for fact in normalized_facts
        ]
        expected_dispatch_hashes = [
            mapping["dispatch_binding_hash"] for mapping in normalized_mappings
        ]
        expected_route_hashes = sorted(
            {fact.route.routing_result_hash for fact in normalized_facts}
        )
        expected_lease_hashes = sorted(
            {_lease_scope_binding_hash(fact) for fact in normalized_facts}
        )
    except Exception as error:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID") from error
    requested_pairs = {(model, effort) for model, effort in request.requested_route_pairs}
    fact_pairs = {
        (fact.route.model, fact.route.reasoning_effort) for fact in normalized_facts
    }
    skeletons = request.assignment_skeletons
    skeleton_by_task = {item["task_id"]: item for item in skeletons}
    if len(skeleton_by_task) != len(skeletons) or len(normalized_facts) != len(skeletons):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    for fact, mapping in zip(normalized_facts, normalized_mappings, strict=True):
        skeleton = skeleton_by_task.get(fact.task_id)
        if skeleton is None:
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
        proof = cast(dict[str, object], skeleton["routing_proof"])
        result = cast(dict[str, object], proof["result"])
        result_route = cast(dict[str, object], result["route"])
        expected_route = {
            "model": result_route["model"],
            "reasoning_effort": result_route["effort"],
            "routing_context_hash": proof["routing_context_hash"],
            "routing_result_hash": proof["routing_result_hash"],
            "require_explicit_route": True,
        }
        if mapping["route"] != expected_route or any(
            mapping[field_name] != skeleton[field_name]
            for field_name in (
                "write_scope",
                "concurrency_mode",
                "dispatch_order",
                "index_context_hash",
                "predecessor_hash",
                "source_plan_hash",
            )
        ):
            raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    if (
        facts != normalized_mappings
        or dispatch_hashes != expected_dispatch_hashes
        or route_hashes != expected_route_hashes
        or lease_hashes != expected_lease_hashes
        or fact_pairs != requested_pairs
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    registry_binding_hash = value.get("registry_binding_hash")
    unsigned = dict(value)
    unsigned.pop("registry_binding_hash", None)
    if (
        type(registry_binding_hash) is not str
        or _DIGEST.fullmatch(registry_binding_hash) is None
        or not hmac.compare_digest(registry_binding_hash, _private_payload_hash(unsigned))
    ):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    _validate_private_packet_size(value, _MAX_COMPILER_EVIDENCE_BYTES)
    return dict(value)


def _normalized_sorted_digest_list(value: object) -> list[str]:
    if type(value) is not list or not value or len(value) > 16:
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    if any(type(item) is not str or _DIGEST.fullmatch(item) is None for item in value):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    if value != sorted(set(value)):
        raise HostBridgeError("HOST_BRIDGE_COMPILER_EVIDENCE_INVALID")
    return list(value)


def _operation_receipt(envelope: Mapping[str, object], *, now: int) -> OperationReceipt:
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
        {field: normalized[field] for field in _BINDING_FIELDS},
        now=now,
        allow_fast_lane=kind == "fast_lane_dispatch_batch",
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


def _normalize_operation_receipt(
    value: OperationReceipt, *, now: int
) -> OperationReceipt:
    if type(value) is not OperationReceipt:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    if value.kind not in {"coordinator_assignment", "peer_evidence_handoff"}:
        raise HostBridgeError("HOST_BRIDGE_ENVELOPE_INVALID")
    _validate_action_id(value.task_id)
    _validate_action_id(value.correlation_id)
    if (
        type(value.envelope_hash) is not str
        or _DIGEST.fullmatch(value.envelope_hash) is None
    ):
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
    allowed_kinds: set[str],
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
    if envelope["kind"] not in allowed_kinds:
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


def _private_payload_hash(payload: object) -> str:
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


def _windows_named_pipe_target(selector: str) -> tuple[str, int, int]:
    """Map a process-identity-bound selector to the local Windows pipe namespace."""

    if type(selector) is not str:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    matched = _WINDOWS_PIPE_SELECTOR.fullmatch(selector)
    if matched is None:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    name = matched.group("name")
    server_pid = int(matched.group("server_pid"))
    creation_filetime = int(matched.group("creation_filetime"), 16)
    if len(name) > _MAX_WINDOWS_PIPE_NAME:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    if server_pid > 0xFFFF_FFFF:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    return rf"\\.\pipe\{name}", server_pid, creation_filetime


def _assert_windows_private_duplex_ipc_handle(handle: int) -> None:
    """Reject console, disk, and one-way Windows handles before CRT adoption."""

    try:
        import _winapi

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


def _assert_windows_named_pipe_server(
    handle: int,
    *,
    expected_server_pid: int,
    expected_creation_filetime: int,
) -> None:
    """Bind the opened pipe to the selector's exact launcher process identity."""

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_server_pid = kernel32.GetNamedPipeServerProcessId
        get_server_pid.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        get_server_pid.restype = ctypes.c_int
        observed_server_pid = ctypes.c_ulong()
        if not get_server_pid(handle, ctypes.byref(observed_server_pid)):
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if observed_server_pid.value != expected_server_pid:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        if (
            _windows_process_creation_filetime(expected_server_pid)
            != expected_creation_filetime
        ):
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


def _windows_process_creation_filetime(process_id: int) -> int:
    """Read one process creation identity without retaining a process handle."""

    import ctypes

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    open_process.restype = ctypes.c_void_p
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    )
    get_process_times.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    process = open_process(0x1000, False, process_id)  # QUERY_LIMITED_INFORMATION
    if not process:
        raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
    try:
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        if not get_process_times(
            process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        return (creation.high << 32) | creation.low
    finally:
        close_handle(process)


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


def _read_exact(
    descriptor: int,
    size: int,
    *,
    cancel_event: Event | None = None,
    cancel_fd: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        if cancel_event is not None:
            if cancel_event.is_set():
                raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
            # POSIX pipes can be waited on together with the private wake fd.
            # Windows' select() only accepts sockets, so fall back to the
            # named/anonymous-pipe availability probe below.
            readable: list[int] | None
            try:
                wait_fds = [descriptor]
                if cancel_fd is not None and cancel_fd >= 0:
                    wait_fds.append(cancel_fd)
                readable, _, _ = select.select(wait_fds, [], [], 0.1)
            except (OSError, ValueError):
                readable = None
            if readable is not None:
                if not readable:
                    continue
                if cancel_fd is not None and cancel_fd in readable:
                    raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
            else:
                available = _windows_pipe_readable(descriptor)
                if available is False:
                    time.sleep(0.05)
                    continue
        try:
            chunk = os.read(descriptor, remaining)
        except OSError as error:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE") from error
        if not chunk:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _windows_pipe_readable(descriptor: int) -> bool | None:
    """Return pipe readiness on Windows, or None when the handle is unknown."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        import msvcrt

        available = ctypes.c_ulong()
        handle = msvcrt.get_osfhandle(descriptor)
        ok = ctypes.windll.kernel32.PeekNamedPipe(
            ctypes.c_void_p(handle),
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        )
        if ok:
            return bool(available.value)
        # A broken/closed pipe should be handed to os.read so it produces the
        # stable unavailable result rather than spinning in the poll loop.
        error = ctypes.get_last_error()
        if error in {109, 232}:  # ERROR_BROKEN_PIPE / ERROR_NO_DATA
            return True
    except (AttributeError, OSError, OverflowError, ValueError):
        pass
    return None


def _decode_frame(raw: bytes) -> dict[str, object]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
        if type(decoded) is not dict or _canonical_bytes(decoded) != raw:
            raise ValueError
        return decoded
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        TypeError,
        RecursionError,
    ) as error:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error


def _canonical_bytes(value: object) -> bytes:
    try:
        _validate_json_value(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    if type(value) is not dict:
        value = dict(value)
    try:
        encoded = _canonical_bytes(value)
        decoded = json.loads(encoded.decode("utf-8"))
    except (
        HostBridgeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID") from error
    if type(decoded) is not dict:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
    _validate_json_value(decoded)
    return decoded


def _validate_json_value(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if math.isfinite(item):
                continue
            raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
        if type(item) is list:
            if len(item) > _MAX_JSON_NODES - nodes:
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            if len(item) > _MAX_JSON_NODES - nodes:
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            if any(type(key) is not str for key in item):
                raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")
            pending.extend((child, depth + 1) for child in item.values())
            continue
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _validate_action_id(value: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _validate_frame_action_id(
    kind: str, action_id: str, payload: Mapping[str, object]
) -> None:
    if not _is_valid_frame_action_id(kind, action_id, payload):
        raise HostBridgeError("HOST_BRIDGE_FRAME_INVALID")


def _is_valid_frame_action_id(kind: object, action_id: object, payload: object) -> bool:
    if type(action_id) is not str:
        return False
    if kind == "fast_lane_refill_registry":
        return _FAST_LANE_REFILL_REGISTRY_ACTION.fullmatch(action_id) is not None
    if _IDENTIFIER.fullmatch(action_id) is not None:
        return True
    if (
        kind != "operation_request"
        or _FAST_LANE_TASK_ID.fullmatch(action_id) is None
        or type(payload) is not dict
    ):
        return False
    envelope = payload.get("envelope")
    return (
        type(envelope) is dict
        and envelope.get("kind") == "fast_lane_dispatch_batch"
        and envelope.get("task_id") == action_id
    )


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
