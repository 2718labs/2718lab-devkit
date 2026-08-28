"""Production Relay runtime registry and host-private capability boundaries."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_relay_runtime import plan, task

import devkit_runtime.host_bridge as host_bridge_module
from devkit_relay.compiler import RelayPlanError
from devkit_relay.store import RelayStore
from devkit_runtime.host_bridge import HostBridgeError, InheritedHandleHostBridge
from devkit_runtime.relay_runtime import (
    ProductionRegistryResolver,
    RelayCapabilitySecretProvider,
    RelayRuntimeError,
    open_relay_ro,
)

_WORKSPACE_ID = "sha256:" + "d" * 64
_SNAPSHOT_ID = "sha256:" + "b" * 64
_PACKET_ID = "sha256:" + "c" * 64


class _CurrentProjectIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def assert_current(self, workspace_id: str, snapshot_id: str) -> object:
        self.calls.append((workspace_id, snapshot_id))
        return object()


class _Packet:
    def __init__(self, workspace_id: str, snapshot_id: str) -> None:
        self.workspace_id = workspace_id
        self.snapshot_id = snapshot_id


class _VerifiedAtlas:
    def __init__(self, packet: _Packet | None) -> None:
        self.packet = packet
        self.calls: list[str] = []

    def get_packet_verified(self, packet_id: str) -> _Packet | None:
        self.calls.append(packet_id)
        return self.packet


def _pipe_pair() -> tuple[InheritedHandleHostBridge, InheritedHandleHostBridge]:
    child_to_host_read, child_to_host_write = os.pipe()
    host_to_child_read, host_to_child_write = os.pipe()
    key = b"k" * 32
    nonce = b"relay-host-bridge-nonce"
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


def test_inherited_handle_bridge_rejects_missing_or_invalid_selectors() -> None:
    assert InheritedHandleHostBridge.from_environment({}) is None
    with pytest.raises(HostBridgeError) as caught:
        InheritedHandleHostBridge.from_environment(
            {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": "not-a-handle"}, platform="nt"
        )

    assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"
    assert "not-a-handle" not in str(caught.value)


@pytest.mark.parametrize(
    "selector",
    [
        "1234",
        "handle:1234",
        "pipe:",
        "pipe:other-product-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-not-a-pid-0123456789abcdef-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-0-0123456789abcdef-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-01-0123456789abcdef-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-4294967296-0123456789abcdef-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-42-not-filetime-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-42-0123456789abcdef-0123456789abcdef0123456789abcdeg",
        "pipe:codex-devkit-42-0123456789abcdef-0123456789abcdef0123456789abcdef/child",
        r"pipe:codex-devkit-42-0123456789abcdef-0123456789abcdef0123456789abcdef\child",
        r"pipe:\\server\pipe\codex-devkit-42-0123456789abcdef-0123456789abcdef0123456789abcdef",
        "pipe:codex-devkit-42-0123456789abcdef-0123456789abcdef0123456789abcdef.",
        "pipe:codex-devkit-" + "a" * 97,
    ],
)
def test_windows_named_pipe_selector_rejects_untagged_or_unsafe_values(
    selector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = False

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        nonlocal opened
        opened = True
        raise AssertionError("invalid selector reached os.open")

    monkeypatch.setattr(host_bridge_module.os, "open", unexpected_open)
    with pytest.raises(HostBridgeError) as caught:
        InheritedHandleHostBridge.from_environment(
            {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": selector}, platform="nt"
        )

    assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"
    assert selector not in str(caught.value)
    assert opened is False


def test_windows_named_pipe_selector_maps_only_to_local_namespace() -> None:
    name = "codex-devkit-42-0123456789abcdef-0123456789abcdef0123456789abcdef"

    assert host_bridge_module._windows_named_pipe_target(f"pipe:{name}") == (
        rf"\\.\pipe\{name}",
        42,
        0x0123456789ABCDEF,
    )


def test_windows_named_pipe_open_failure_drops_sensitive_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "fedcba9876543210fedcba9876543210"
    selector = f"pipe:codex-devkit-42-0123456789abcdef-{token}"

    def fail_open(path: str, _flags: int) -> int:
        raise OSError(f"cannot open {path}")

    monkeypatch.setattr(host_bridge_module.os, "open", fail_open)
    with pytest.raises(HostBridgeError) as caught:
        InheritedHandleHostBridge.from_environment(
            {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": selector}, platform="nt"
        )

    error = caught.value
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert error.code == "HOST_BRIDGE_UNAVAILABLE"
    assert error.__cause__ is None
    assert error.__context__ is None
    for sensitive in (selector, token, r"\\.\pipe"):
        assert sensitive not in str(error)
        assert sensitive not in rendered


@pytest.mark.parametrize(
    ("platform", "selector_name"),
    [
        ("posix", "CODEX_DEVKIT_HOST_BRIDGE_FD"),
        ("nt", "CODEX_DEVKIT_HOST_BRIDGE_HANDLE"),
    ],
)
def test_inherited_handle_bridge_rejects_stdio_selector(
    platform: str, selector_name: str
) -> None:
    with pytest.raises(HostBridgeError) as caught:
        InheritedHandleHostBridge.from_environment(
            {selector_name: "1"}, platform=platform
        )

    assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"


def test_inherited_handle_bridge_rejects_regular_file_selector(tmp_path: Path) -> None:
    mailbox = tmp_path / "not-a-private-ipc-handle"
    descriptor = os.open(mailbox, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(HostBridgeError) as caught:
            InheritedHandleHostBridge.from_environment(
                {"CODEX_DEVKIT_HOST_BRIDGE_FD": str(descriptor)}, platform="posix"
            )
    finally:
        os.close(descriptor)

    assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"
    assert mailbox.read_bytes() == b""


@pytest.mark.parametrize(
    ("corruption", "first_error"),
    [
        ("parse", "HOST_BRIDGE_FRAME_INVALID"),
        ("nonce", "HOST_BRIDGE_FRAME_INVALID"),
        ("mac", "HOST_BRIDGE_AUTH_FAILED"),
    ],
)
def test_bad_bootstrap_closes_owned_transport_before_reaccept(
    corruption: str, first_error: str
) -> None:
    read_fd, write_fd = os.pipe()
    bootstrap_reply_fd = os.dup(write_fd)
    sender_read_fd, sender_reply_fd = os.pipe()
    key = b"b" * 32
    nonce = b"relay-bootstrap-nonce"
    sender = InheritedHandleHostBridge(
        read_fd=sender_read_fd,
        write_fd=write_fd,
        session_key=key,
        session_nonce=nonce,
        owns_descriptors=False,
        bootstrap_required=True,
    )
    retry_reply_fd = os.dup(write_fd)
    accepted: InheritedHandleHostBridge | None = None
    try:
        bootstrap = sender._frame_bytes(
            kind="session_open",
            action_id="session",
            sequence=0,
            payload={"session_key": host_bridge_module._b64encode(key)},
        )
        if corruption == "parse":
            bad_payload = b"not-canonical-json"
        else:
            bad_frame = json.loads(bootstrap[4:].decode("utf-8"))
            if corruption == "nonce":
                bad_frame["session_nonce"] = "!"
            else:
                bad_frame["mac"] = "0" * 64
            bad_payload = host_bridge_module._canonical_bytes(bad_frame)
        os.write(write_fd, struct.pack("!I", len(bad_payload)) + bad_payload)
        sender.send_private(
            kind="capability_ack", action_id="bootstrap-action", payload={}
        )

        with pytest.raises(HostBridgeError) as first:
            InheritedHandleHostBridge.accept_from_file_descriptors(
                read_fd=read_fd, write_fd=bootstrap_reply_fd
            )
        assert first.value.code == first_error

        retry_error: HostBridgeError | None = None
        try:
            accepted = InheritedHandleHostBridge.accept_from_file_descriptors(
                read_fd=read_fd, write_fd=retry_reply_fd
            )
        except HostBridgeError as error:
            retry_error = error

        assert accepted is None
        assert retry_error is not None
        assert retry_error.code == "HOST_BRIDGE_UNAVAILABLE"
    finally:
        if accepted is not None:
            accepted.close()
        sender.close()
        for descriptor in (read_fd, write_fd, bootstrap_reply_fd, retry_reply_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass
        os.close(sender_read_fd)
        os.close(sender_reply_fd)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX socket inheritance")
def test_inherited_handle_bridge_accepts_only_duplex_inherited_ipc_selector() -> None:
    peer, inherited = socket.socketpair()
    environ = {"CODEX_DEVKIT_HOST_BRIDGE_FD": str(inherited.fileno())}
    closers: list[Callable[[], None]] = [peer.close, inherited.close]
    bridge: InheritedHandleHostBridge | None = None
    try:
        bridge = InheritedHandleHostBridge.from_environment(environ, platform="posix")
        assert bridge is not None
        assert bridge.is_available
    finally:
        if bridge is not None:
            bridge.close()
        for close in closers:
            close()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named pipes")
@pytest.mark.parametrize("reply_mode", ["pong", "silent"])
def test_windows_named_pipe_selector_opens_duplex_and_exchanges_frames(
    reply_mode: str,
) -> None:
    import _winapi
    import ctypes
    import msvcrt

    creation_filetime = host_bridge_module._windows_process_creation_filetime(
        os.getpid()
    )
    pipe_name = (
        f"codex-devkit-{os.getpid()}-{creation_filetime:016x}-{os.urandom(16).hex()}"
    )
    selector = f"pipe:{pipe_name}"
    pipe_path = rf"\\.\pipe\{pipe_name}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_named_pipe = kernel32.CreateNamedPipeW
    create_named_pipe.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    create_named_pipe.restype = ctypes.c_void_p
    connect_named_pipe = kernel32.ConnectNamedPipe
    connect_named_pipe.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    connect_named_pipe.restype = ctypes.c_int
    open_thread = kernel32.OpenThread
    open_thread.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    open_thread.restype = ctypes.c_void_p
    cancel_synchronous_io = kernel32.CancelSynchronousIo
    cancel_synchronous_io.argtypes = (ctypes.c_void_p,)
    cancel_synchronous_io.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sddl.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    )
    convert_sddl.restype = ctypes.c_int

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.c_ulong),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", ctypes.c_int),
        ]

    security_descriptor = ctypes.c_void_p()
    if not convert_sddl("D:P(A;;GA;;;OW)", 1, ctypes.byref(security_descriptor), None):
        raise OSError(ctypes.get_last_error(), "owner-only SDDL conversion failed")
    security_attributes = SecurityAttributes(
        ctypes.sizeof(SecurityAttributes), security_descriptor, False
    )
    server_handle = create_named_pipe(
        pipe_path,
        0x00000003 | 0x00080000,  # PIPE_ACCESS_DUPLEX | FIRST_PIPE_INSTANCE
        0x00000008,  # byte mode, blocking, and PIPE_REJECT_REMOTE_CLIENTS
        1,
        65_536,
        65_536,
        0,
        ctypes.byref(security_attributes),
    )
    local_free(security_descriptor)
    if server_handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")

    errors: list[BaseException] = []
    release_silent_server = threading.Event()

    def serve() -> None:
        nonlocal server_handle
        descriptor = -1
        host: InheritedHandleHostBridge | None = None
        try:
            if not connect_named_pipe(server_handle, None):
                error = ctypes.get_last_error()
                if error != 535:  # ERROR_PIPE_CONNECTED
                    raise OSError(error, "ConnectNamedPipe failed")
            descriptor = msvcrt.open_osfhandle(
                int(server_handle), os.O_BINARY | os.O_RDWR
            )
            server_handle = None
            host = InheritedHandleHostBridge.accept_from_file_descriptors(
                read_fd=descriptor, write_fd=descriptor
            )
            descriptor = -1
            ping = host.receive()
            assert (ping.kind, ping.action_id, ping.payload) == (
                "capability_ack",
                "pipe-ping",
                {},
            )
            if reply_mode == "pong":
                host.send_private(
                    kind="capability_ack", action_id="pipe-pong", payload={}
                )
            else:
                release_silent_server.wait(timeout=4)
        except BaseException as error:
            errors.append(error)
        finally:
            if host is not None:
                host.close()
            elif descriptor >= 0:
                os.close(descriptor)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    child: InheritedHandleHostBridge | None = None
    client_thread: threading.Thread | None = None
    client_errors: list[BaseException] = []
    try:
        child = InheritedHandleHostBridge.from_environment(
            {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": selector}, platform="nt"
        )
        assert child is not None

        def exchange() -> None:
            try:
                assert child is not None
                child.send_private(
                    kind="capability_ack", action_id="pipe-ping", payload={}
                )
                pong = child.receive()
                assert (pong.kind, pong.action_id, pong.payload) == (
                    "capability_ack",
                    "pipe-pong",
                    {},
                )
            except BaseException as error:
                client_errors.append(error)

        client_thread = threading.Thread(target=exchange, daemon=True)
        client_thread.start()
        client_thread.join(timeout=2)
        if client_thread.is_alive():
            assert client_thread.native_id is not None
            thread_handle = open_thread(0x0001, False, client_thread.native_id)
            if not thread_handle:
                raise OSError(ctypes.get_last_error(), "OpenThread failed")
            try:
                if not cancel_synchronous_io(thread_handle):
                    raise OSError(ctypes.get_last_error(), "CancelSynchronousIo failed")
            finally:
                close_handle(thread_handle)
            client_thread.join(timeout=2)
        release_silent_server.set()
        if reply_mode == "pong":
            assert client_errors == []
        else:
            assert len(client_errors) == 1
            assert isinstance(client_errors[0], HostBridgeError)
            assert child.is_available is False
            rendered = "".join(
                traceback.format_exception(
                    type(client_errors[0]),
                    client_errors[0],
                    client_errors[0].__traceback__,
                )
            )
            for sensitive in (selector, pipe_name.rsplit("-", 1)[-1], pipe_path):
                assert sensitive not in rendered
        assert not client_thread.is_alive()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert errors == []
    finally:
        release_silent_server.set()
        if child is not None:
            child.close()
        if server_handle is not None:
            if thread.is_alive() and thread.native_id is not None:
                thread_handle = open_thread(0x0001, False, thread.native_id)
                if thread_handle:
                    try:
                        cancel_synchronous_io(thread_handle)
                    finally:
                        close_handle(thread_handle)
            _winapi.CloseHandle(server_handle)
        thread.join(timeout=2)
        if client_thread is not None:
            client_thread.join(timeout=2)
        assert not thread.is_alive()
        assert client_thread is None or not client_thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named pipes")
@pytest.mark.parametrize("mismatch", ["pid", "creation_filetime"])
def test_windows_named_pipe_rejects_reused_server_identity_before_bootstrap(
    mismatch: str,
) -> None:
    import _winapi
    import ctypes
    import msvcrt

    actual_pid = os.getpid()
    creation_filetime = host_bridge_module._windows_process_creation_filetime(
        actual_pid
    )
    encoded_pid = (
        actual_pid + 1
        if mismatch == "pid" and actual_pid < 0xFFFF_FFFF
        else actual_pid - 1
        if mismatch == "pid"
        else actual_pid
    )
    encoded_creation = (
        creation_filetime ^ 1 if mismatch == "creation_filetime" else creation_filetime
    )
    pipe_name = (
        f"codex-devkit-{encoded_pid}-{encoded_creation:016x}-{os.urandom(16).hex()}"
    )
    selector = f"pipe:{pipe_name}"
    pipe_path = rf"\\.\pipe\{pipe_name}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_named_pipe = kernel32.CreateNamedPipeW
    create_named_pipe.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    create_named_pipe.restype = ctypes.c_void_p
    connect_named_pipe = kernel32.ConnectNamedPipe
    connect_named_pipe.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    connect_named_pipe.restype = ctypes.c_int
    open_thread = kernel32.OpenThread
    open_thread.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    open_thread.restype = ctypes.c_void_p
    cancel_synchronous_io = kernel32.CancelSynchronousIo
    cancel_synchronous_io.argtypes = (ctypes.c_void_p,)
    cancel_synchronous_io.restype = ctypes.c_int
    server_handle = create_named_pipe(
        pipe_path,
        0x00000003 | 0x00080000,
        0x00000008,
        1,
        65_536,
        65_536,
        0,
        None,
    )
    if server_handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")

    observed: list[bytes] = []
    errors: list[BaseException] = []

    def serve() -> None:
        nonlocal server_handle
        descriptor = -1
        try:
            if not connect_named_pipe(server_handle, None):
                error = ctypes.get_last_error()
                if error != 535:
                    raise OSError(error, "ConnectNamedPipe failed")
            descriptor = msvcrt.open_osfhandle(
                int(server_handle), os.O_BINARY | os.O_RDWR
            )
            server_handle = None
            observed.append(os.read(descriptor, 1))
        except BaseException as error:
            errors.append(error)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(HostBridgeError) as caught:
            InheritedHandleHostBridge.from_environment(
                {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": selector}, platform="nt"
            )
        assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert errors == []
        assert observed == [b""]
    finally:
        if server_handle is not None:
            if thread.is_alive() and thread.native_id is not None:
                thread_handle = open_thread(0x0001, False, thread.native_id)
                if thread_handle:
                    try:
                        cancel_synchronous_io(thread_handle)
                    finally:
                        _winapi.CloseHandle(thread_handle)
            _winapi.CloseHandle(server_handle)
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.skipif(
    not hasattr(os, "set_blocking"),
    reason="Python runtime does not expose os.set_blocking on this platform",
)
def test_oversized_private_delivery_never_becomes_prepared_or_retriable() -> None:
    child, host = _pipe_pair()
    capabilities = {f"action-{index}": "b" * 8_192 for index in range(8)}
    try:
        with pytest.raises(HostBridgeError) as first:
            child.prepare_capability(
                action_id="oversized-action",
                endpoint="bridge/oversized-action",
                capabilities=capabilities,
            )
        assert first.value.code == "HOST_BRIDGE_FRAME_INVALID"

        with pytest.raises(HostBridgeError) as receipt:
            child.delivery_receipt("oversized-action")
        assert receipt.value.code == "HOST_BRIDGE_DELIVERY_UNKNOWN"

        with pytest.raises(HostBridgeError) as retry:
            child.prepare_capability(
                action_id="oversized-action",
                endpoint="bridge/oversized-action",
                capabilities=capabilities,
            )
        assert retry.value.code == "HOST_BRIDGE_FRAME_INVALID"

        os.set_blocking(host._read_fd, False)
        with pytest.raises(BlockingIOError):
            os.read(host._read_fd, 1)
    finally:
        child.close()
        host.close()


def test_partial_private_write_poison_session_without_prepared_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child, host = _pipe_pair()
    original_write = host_bridge_module.os.write
    writes = 0

    def partial_then_transport_error(descriptor: int, payload: object) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            assert isinstance(payload, bytes | memoryview)
            return original_write(descriptor, payload[:1])
        raise OSError("simulated private transport interruption")

    monkeypatch.setattr(host_bridge_module.os, "write", partial_then_transport_error)
    try:
        with pytest.raises(HostBridgeError) as first:
            child.prepare_capability(
                action_id="partial-action",
                endpoint="bridge/partial-action",
                capabilities={"heartbeat": "private-capability"},
            )
        assert first.value.code == "HOST_BRIDGE_UNAVAILABLE"
        assert child.is_available is False

        with pytest.raises(HostBridgeError) as receipt:
            child.delivery_receipt("partial-action")
        assert receipt.value.code == "HOST_BRIDGE_UNAVAILABLE"

        with pytest.raises(HostBridgeError) as retry:
            child.prepare_capability(
                action_id="partial-action",
                endpoint="bridge/partial-action",
                capabilities={"heartbeat": "private-capability"},
            )
        assert retry.value.code == "HOST_BRIDGE_UNAVAILABLE"
        assert writes == 1
    finally:
        child.close()
        host.close()


def test_inherited_handle_bridge_frames_authenticates_sequences_and_recovers_delivery() -> (
    None
):
    child, host = _pipe_pair()
    bearer = "private-capability-bearer-never-public"
    try:
        prepared = child.prepare_capability(
            action_id="action-a",
            endpoint="bridge/action-a",
            capabilities={"heartbeat": bearer},
        )
        private_message = host.receive()
        assert private_message.kind == "capability_prepare"
        assert private_message.payload["capabilities"] == {"heartbeat": bearer}
        assert bearer not in repr(private_message)
        assert bearer not in repr(prepared)

        recovered = child.recover_capability("action-a")
        assert recovered.action_id == prepared.action_id == "action-a"
        recovery_message = host.receive()
        assert recovery_message.kind == "capability_recovery"
        assert recovery_message.payload["capabilities"] == {"heartbeat": bearer}

        host.send_acknowledgement("action-a")
        acknowledgement = child.receive()
        assert acknowledgement.kind == "capability_ack"
        assert child.delivery_receipt("action-a").state == "acknowledged"
        assert child.prepare_capability(
            action_id="action-a",
            endpoint="bridge/action-a",
            capabilities={"heartbeat": bearer},
        ) == child.delivery_receipt("action-a")
    finally:
        child.close()
        host.close()


def test_host_bridge_exposes_no_quota_snapshot_operations() -> None:
    bridge = InheritedHandleHostBridge.__new__(InheritedHandleHostBridge)

    assert not hasattr(bridge, "request_quota_snapshot")
    assert not hasattr(bridge, "receive_quota_snapshot")


def test_inherited_handle_bridge_rejects_bad_frame_mac_and_sequence() -> None:
    child, host = _pipe_pair()
    try:
        child.prepare_capability(
            action_id="action-a",
            endpoint="bridge/action-a",
            capabilities={"heartbeat": "private-capability"},
        )
        host.receive()

        forged = InheritedHandleHostBridge.from_file_descriptors(
            read_fd=os.dup(host._read_fd),
            write_fd=os.dup(child._write_fd),
            session_key=b"w" * 32,
            session_nonce=b"relay-host-bridge-nonce",
        )
        try:
            forged.send_private(kind="capability_ack", action_id="action-a", payload={})
            with pytest.raises(HostBridgeError) as mac_error:
                host.receive()
            assert mac_error.value.code == "HOST_BRIDGE_AUTH_FAILED"
        finally:
            forged.close()
    finally:
        child.close()
        host.close()

    child, host = _pipe_pair()
    try:
        child.prepare_capability(
            action_id="action-a",
            endpoint="bridge/action-a",
            capabilities={"heartbeat": "private-capability"},
        )
        host.receive()
        repeated_sequence = InheritedHandleHostBridge.from_file_descriptors(
            read_fd=os.dup(host._read_fd),
            write_fd=os.dup(child._write_fd),
            session_key=b"k" * 32,
            session_nonce=b"relay-host-bridge-nonce",
        )
        try:
            repeated_sequence.send_private(
                kind="capability_ack", action_id="action-a", payload={}
            )
            with pytest.raises(HostBridgeError) as sequence_error:
                host.receive()
            assert sequence_error.value.code == "HOST_BRIDGE_SEQUENCE_INVALID"
        finally:
            repeated_sequence.close()
    finally:
        child.close()
        host.close()


def test_inherited_handle_bridge_rejects_replayed_capability_probe_sequence() -> None:
    child, host = _pipe_pair()
    try:
        child.send_capability_probe(
            binding={
                "task_id": "task-1",
                "lease_epoch": 7,
                "assignment_token": "sha256:" + "a" * 64,
                "dispatch_context_hash": "sha256:" + "b" * 64,
                "route_hash": "sha256:" + "c" * 64,
                "expires_at": 1_700_000_060,
            },
            capability_names=("artifact-read",),
            now=1_700_000_000,
        )
        host.receive_capability_probe(now=1_700_000_000)

        replay = InheritedHandleHostBridge.from_file_descriptors(
            read_fd=os.dup(host._read_fd),
            write_fd=os.dup(child._write_fd),
            session_key=b"k" * 32,
            session_nonce=b"relay-host-bridge-nonce",
        )
        try:
            replay._write_complete(
                replay._frame_bytes(
                    kind="capability_probe",
                    action_id="task-1",
                    sequence=replay._next_out,
                    payload={
                        "schema": "2718lab-devkit/host-capability-probe-v1",
                        "task_id": "task-1",
                        "lease_epoch": 7,
                        "assignment_token": "sha256:" + "a" * 64,
                        "dispatch_context_hash": "sha256:" + "b" * 64,
                        "route_hash": "sha256:" + "c" * 64,
                        "expires_at": 1_700_000_060,
                        "capability_names": ["artifact-read"],
                    },
                )
            )
            with pytest.raises(HostBridgeError) as caught:
                host.receive()
            assert caught.value.code == "HOST_BRIDGE_SEQUENCE_INVALID"
            assert host.is_available is False
        finally:
            replay.close()
    finally:
        child.close()
        host.close()


def test_production_registry_resolver_requires_current_verified_packet_binding() -> (
    None
):
    project_index = _CurrentProjectIndex()
    atlas = _VerifiedAtlas(_Packet(_WORKSPACE_ID, _SNAPSHOT_ID))
    resolver = ProductionRegistryResolver(project_index, atlas)

    binding = resolver.resolve(
        workflow_id="relay-runtime-v3",
        workspace_id=_WORKSPACE_ID,
        input_snapshot_id=_SNAPSHOT_ID,
        atlas_packet_ids=(_PACKET_ID,),
    )

    assert binding == {
        "workflow_id": "relay-runtime-v3",
        "workspace_id": _WORKSPACE_ID,
        "input_snapshot_id": _SNAPSHOT_ID,
        "atlas_packet_ids": [_PACKET_ID],
        "current": True,
    }
    assert project_index.calls == [(_WORKSPACE_ID, _SNAPSHOT_ID)]
    assert atlas.calls == [_PACKET_ID]

    stale = ProductionRegistryResolver(
        _CurrentProjectIndex(),
        _VerifiedAtlas(_Packet(_WORKSPACE_ID, "sha256:" + "e" * 64)),
    )
    with pytest.raises(RelayPlanError) as caught:
        stale.resolve(
            workflow_id="relay-runtime-v3",
            workspace_id=_WORKSPACE_ID,
            input_snapshot_id=_SNAPSHOT_ID,
            atlas_packet_ids=(_PACKET_ID,),
        )
    assert caught.value.code == "registry_binding_stale"


def test_capability_secret_provider_requires_existing_binary_key(
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "relay-capability.key"
    secret_path.write_bytes(b"s" * 32)

    provider = RelayCapabilitySecretProvider(secret_path)
    assert provider.load() == b"s" * 32
    assert secret_path.read_bytes() == b"s" * 32

    with pytest.raises(RelayRuntimeError) as missing:
        RelayCapabilitySecretProvider(tmp_path / "missing.key").load()
    assert missing.value.code == "RELAY_CAPABILITY_INVALID"

    secret_path.write_bytes(b"too-short")
    with pytest.raises(RelayRuntimeError) as invalid:
        provider.load()
    assert invalid.value.code == "RELAY_CAPABILITY_INVALID"


def test_open_relay_ro_reads_from_verified_snapshot_without_source_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(database)
    store.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            )
        ),
        idempotency_key="ro-open",
    )
    store.close()
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.glob("relay.sqlite3*")
    }
    scratch_root = tmp_path.parent / "relay-ro-scratch"
    scratch_root.mkdir()
    reader = open_relay_ro(database, scratch_root=scratch_root)
    try:
        status = reader.status("relay-runtime-v3")
    finally:
        reader.close()

    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.glob("relay.sqlite3*")
    }
    assert status["workflow_id"] == "relay-runtime-v3"
    assert before == after
