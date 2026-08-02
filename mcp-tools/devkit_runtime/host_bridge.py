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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

_FRAME_SCHEMA: Final = "2718lab-devkit/host-bridge-v1"
_QUOTA_REQUEST_SCHEMA: Final = "2718lab-devkit/host-quota-snapshot-request-v1"
_QUOTA_RESPONSE_SCHEMA: Final = "2718lab-devkit/host-quota-snapshot-response-v1"
_QUOTA_SNAPSHOT_SCHEMA: Final = "2718lab-devkit/host-quota-snapshot-v1"
_FRAME_FIELDS: Final = frozenset(
    {"schema", "kind", "action_id", "session_nonce", "sequence", "payload", "mac"}
)
_MAX_FRAME_BYTES: Final = 65_536
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
        "quota_snapshot_request",
        "quota_snapshot",
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


@dataclass
class _CapabilityDelivery:
    endpoint: str
    capabilities: dict[str, str] = field(repr=False)
    state: str = "prepared"

    def receipt(self, action_id: str) -> CapabilityDeliveryReceipt:
        return CapabilityDeliveryReceipt(
            action_id=action_id, endpoint=self.endpoint, state=self.state
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

    def request_quota_snapshot(self, *, request_id: str) -> None:
        """Request one signed main/Spark usage snapshot from the private host."""

        _validate_action_id(request_id)
        self.send_private(
            kind="quota_snapshot_request",
            action_id=request_id,
            payload={"schema": _QUOTA_REQUEST_SCHEMA, "request_id": request_id},
        )

    def receive_quota_snapshot(self, *, request_id: str) -> dict[str, object]:
        """Receive a snapshot bound to the exact request id, failing closed otherwise."""

        _validate_action_id(request_id)
        try:
            message = self.receive()
            payload = message.payload
            snapshot = payload.get("snapshot")
            if (
                message.kind != "quota_snapshot"
                or message.action_id != request_id
                or set(payload) != {"schema", "snapshot"}
                or payload.get("schema") != _QUOTA_RESPONSE_SCHEMA
                or not isinstance(snapshot, dict)
                or snapshot.get("schema") != _QUOTA_SNAPSHOT_SCHEMA
            ):
                raise HostBridgeError("HOST_BRIDGE_QUOTA_INVALID")
            return dict(snapshot)
        except HostBridgeError:
            self._poison()
            raise

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
        """Read and validate exactly one authenticated private frame."""

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

    def _ensure_open(self) -> None:
        if self._closed or self._read_fd < 0 or self._write_fd < 0:
            raise HostBridgeError("HOST_BRIDGE_UNAVAILABLE")


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
