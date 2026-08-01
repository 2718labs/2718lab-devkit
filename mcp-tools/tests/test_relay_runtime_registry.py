"""Production Relay runtime registry and host-private capability boundaries."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_relay_runtime import plan, task

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


def test_inherited_handle_bridge_accepts_only_the_inherited_fd_selector() -> None:
    read_fd, write_fd = os.pipe()
    bridge: InheritedHandleHostBridge | None = None
    try:
        bridge = InheritedHandleHostBridge.from_environment(
            {"CODEX_DEVKIT_HOST_BRIDGE_FD": str(write_fd)}, platform="posix"
        )
        assert bridge is not None
        assert bridge.is_available
    finally:
        if bridge is not None:
            bridge.close()
        os.close(read_fd)
        os.close(write_fd)


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
