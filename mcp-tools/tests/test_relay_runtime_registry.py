"""Production Relay runtime registry and host-private capability boundaries."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
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


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle semantics")
def test_inherited_handle_bridge_rejects_windows_regular_file_handle(
    tmp_path: Path,
) -> None:
    import _winapi
    import msvcrt

    mailbox = tmp_path / "not-a-private-windows-ipc-handle"
    descriptor = os.open(mailbox, os.O_RDWR | os.O_CREAT, 0o600)
    duplicated_handle: int | None = None
    bridge: InheritedHandleHostBridge | None = None
    try:
        duplicated_handle = _winapi.DuplicateHandle(
            _winapi.GetCurrentProcess(),
            msvcrt.get_osfhandle(descriptor),
            _winapi.GetCurrentProcess(),
            0,
            False,
            _winapi.DUPLICATE_SAME_ACCESS,
        )
        with pytest.raises(HostBridgeError) as caught:
            bridge = InheritedHandleHostBridge.from_environment(
                {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": str(duplicated_handle)},
                platform="nt",
            )
    finally:
        if bridge is not None:
            bridge.close()
        elif duplicated_handle is not None:
            try:
                _winapi.CloseHandle(duplicated_handle)
            except OSError:
                pass
        os.close(descriptor)

    assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"
    assert mailbox.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle semantics")
@pytest.mark.parametrize("endpoint_name", ["read", "write"])
def test_inherited_handle_bridge_rejects_windows_one_way_pipe_handle(
    endpoint_name: str,
) -> None:
    import _winapi
    import msvcrt

    read_fd, write_fd = os.pipe()
    duplicated_handle: int | None = None
    bridge: InheritedHandleHostBridge | None = None
    try:
        duplicated_handle = _winapi.DuplicateHandle(
            _winapi.GetCurrentProcess(),
            msvcrt.get_osfhandle(read_fd if endpoint_name == "read" else write_fd),
            _winapi.GetCurrentProcess(),
            0,
            False,
            _winapi.DUPLICATE_SAME_ACCESS,
        )
        with pytest.raises(HostBridgeError) as caught:
            bridge = InheritedHandleHostBridge.from_environment(
                {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": str(duplicated_handle)},
                platform="nt",
            )
    finally:
        if bridge is not None:
            bridge.close()
        elif duplicated_handle is not None:
            try:
                _winapi.CloseHandle(duplicated_handle)
            except OSError:
                pass
        os.close(read_fd)
        os.close(write_fd)

    assert caught.value.code == "HOST_BRIDGE_UNAVAILABLE"


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle semantics")
@pytest.mark.parametrize(
    "standard_handle", [-10, -11, -12], ids=["stdin", "stdout", "stderr"]
)
def test_inherited_handle_bridge_rejects_windows_standard_handle_object_alias(
    standard_handle: int,
) -> None:
    import _winapi
    import ctypes
    from multiprocessing import Pipe

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_std_handle = kernel32.SetStdHandle
    set_std_handle.argtypes = (ctypes.c_ulong, ctypes.c_void_p)
    set_std_handle.restype = ctypes.c_int
    standard_handle_selector = ctypes.c_ulong(standard_handle).value
    peer, inherited = Pipe(duplex=True)
    standard_alias = _winapi.DuplicateHandle(
        _winapi.GetCurrentProcess(),
        inherited.fileno(),
        _winapi.GetCurrentProcess(),
        0,
        False,
        _winapi.DUPLICATE_SAME_ACCESS,
    )
    candidate_alias = _winapi.DuplicateHandle(
        _winapi.GetCurrentProcess(),
        standard_alias,
        _winapi.GetCurrentProcess(),
        0,
        False,
        _winapi.DUPLICATE_SAME_ACCESS,
    )
    original_standard_handle = _winapi.GetStdHandle(standard_handle)
    bridge: InheritedHandleHostBridge | None = None
    standard_handle_replaced = False
    try:
        assert candidate_alias != standard_alias
        if not set_std_handle(
            standard_handle_selector, ctypes.c_void_p(standard_alias)
        ):
            raise OSError(ctypes.get_last_error(), "SetStdHandle failed")
        standard_handle_replaced = True
        assert _winapi.GetStdHandle(standard_handle) == standard_alias

        caught: HostBridgeError | None = None
        try:
            bridge = InheritedHandleHostBridge.from_environment(
                {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": str(candidate_alias)},
                platform="nt",
            )
        except HostBridgeError as error:
            caught = error

        if bridge is not None:
            bridge.prepare_capability(
                action_id="standard-alias",
                endpoint="bridge/standard-alias",
                capabilities={"heartbeat": "test-private-capability"},
            )
        assert bridge is None
        assert caught is not None
        assert caught.code == "HOST_BRIDGE_UNAVAILABLE"
        assert peer.poll(0) is False
    finally:
        if standard_handle_replaced:
            if not set_std_handle(
                standard_handle_selector, ctypes.c_void_p(original_standard_handle)
            ):
                raise OSError(ctypes.get_last_error(), "SetStdHandle restore failed")
        if bridge is not None:
            bridge.close()
        else:
            try:
                _winapi.CloseHandle(candidate_alias)
            except OSError:
                pass
        try:
            _winapi.CloseHandle(standard_alias)
        except OSError:
            pass
        peer.close()
        inherited.close()


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


def test_inherited_handle_bridge_accepts_only_duplex_inherited_ipc_selector() -> None:
    closers: list[Callable[[], None]]
    if os.name == "nt":
        import _winapi
        from multiprocessing import Pipe

        peer, inherited = Pipe(duplex=True)
        handle = _winapi.DuplicateHandle(
            _winapi.GetCurrentProcess(),
            inherited.fileno(),
            _winapi.GetCurrentProcess(),
            0,
            False,
            _winapi.DUPLICATE_SAME_ACCESS,
        )
        environ = {"CODEX_DEVKIT_HOST_BRIDGE_HANDLE": str(handle)}
        platform = "nt"
        closers = [peer.close, inherited.close]
    else:
        peer, inherited = socket.socketpair()
        environ = {"CODEX_DEVKIT_HOST_BRIDGE_FD": str(inherited.fileno())}
        platform = "posix"
        closers = [peer.close, inherited.close]
    bridge: InheritedHandleHostBridge | None = None
    try:
        bridge = InheritedHandleHostBridge.from_environment(environ, platform=platform)
        assert bridge is not None
        assert bridge.is_available
    finally:
        if bridge is not None:
            bridge.close()
        for close in closers:
            close()


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


def test_inherited_handle_bridge_round_trips_a_quota_snapshot_request() -> None:
    child, host = _pipe_pair()
    snapshot = {
        "schema": "2718lab-devkit/host-quota-snapshot-v1",
        "snapshot_hash": "sha256:" + "a" * 64,
        "signature": {"algorithm": "hmac-sha256", "key_id": "sha256:" + "b" * 64},
    }
    try:
        child.request_quota_snapshot(request_id="quota-epoch-1")
        request = host.receive()
        assert request.kind == "quota_snapshot_request"
        assert request.action_id == "quota-epoch-1"
        assert request.payload == {
            "schema": "2718lab-devkit/host-quota-snapshot-request-v1",
            "request_id": "quota-epoch-1",
        }

        host.send_private(
            kind="quota_snapshot",
            action_id="quota-epoch-1",
            payload={
                "schema": "2718lab-devkit/host-quota-snapshot-response-v1",
                "snapshot": snapshot,
            },
        )
        assert child.receive_quota_snapshot(request_id="quota-epoch-1") == snapshot
    finally:
        child.close()
        host.close()


def test_inherited_handle_bridge_rejects_an_unbound_quota_snapshot_response() -> None:
    child, host = _pipe_pair()
    try:
        host.send_private(
            kind="quota_snapshot",
            action_id="quota-epoch-foreign",
            payload={
                "schema": "2718lab-devkit/host-quota-snapshot-response-v1",
                "snapshot": {},
            },
        )
        with pytest.raises(HostBridgeError) as caught:
            child.receive_quota_snapshot(request_id="quota-epoch-1")
        assert caught.value.code == "HOST_BRIDGE_QUOTA_INVALID"
        assert child.is_available is False
    finally:
        child.close()
        host.close()


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
