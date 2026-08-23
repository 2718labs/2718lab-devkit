"""Private, host-attested bootstrap boundary for a genuinely new empty project."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.compiler import RelayPlanError
from devkit_runtime.relay_runtime import (
    ProductionRegistryResolver,
    ProjectIndexBootstrapTransport,
    RelayRuntimeError,
    validate_project_index_bootstrap_receipt,
)

_NOW = 1_700_000_000
_HASH = "sha256:" + "a" * 64


def _canonical_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _binding(**attestation_changes: object) -> dict[str, object]:
    attestation: dict[str, object] = {
        "schema": "2718lab-devkit/new-project-bootstrap-attestation-v1",
        "workflow_id": "workflow-new-project",
        "workspace_id": "sha256:" + "1" * 64,
        "repository_id": "sha256:" + "2" * 64,
        "project_id": "sha256:" + "3" * 64,
        "bootstrap_root_identity": "sha256:" + "4" * 64,
        "initial_manifest_hash": "sha256:" + "5" * 64,
        "initial_entry_count": 0,
        "state": "new_empty",
        "capability_epoch": 7,
        "capability_hash": "sha256:" + "6" * 64,
        "attested_input_snapshot_id": "sha256:" + "7" * 64,
        "issued_at": _NOW - 1,
        "expires_at": _NOW + 60,
    }
    attestation.update(attestation_changes)
    attestation["attestation_hash"] = _canonical_hash(attestation)
    binding: dict[str, object] = {
        "schema": "2718lab-devkit/project-binding-v1",
        "mode": "new_empty_bootstrap",
        "workflow_id": attestation["workflow_id"],
        "workspace_id": attestation["workspace_id"],
        "repository_id": attestation["repository_id"],
        "project_id": attestation["project_id"],
        "bootstrap_root_identity": attestation["bootstrap_root_identity"],
        "attestation": attestation,
    }
    binding["binding_hash"] = _canonical_hash(binding)
    return binding


def _resolver(*, capability_valid: bool = True) -> ProductionRegistryResolver:
    return ProductionRegistryResolver(
        object(),
        object(),
        clock=lambda: _NOW,
        bootstrap_capability_verifier=lambda _attestation: capability_valid,
    )


def test_current_new_empty_attestation_yields_only_bootstrap_registry_binding() -> None:
    binding = _resolver().resolve_new_empty_bootstrap(_binding())

    assert binding["mode"] == "new_empty_bootstrap"
    assert binding["bootstrap_only"] is True
    assert "index_snapshot_id" not in binding
    assert "current" not in binding
    assert "path" not in repr(binding).lower()


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"initial_entry_count": 1}, "BOOTSTRAP_PROJECT_NOT_EMPTY"),
        ({"state": "indexed"}, "BOOTSTRAP_PROJECT_NOT_EMPTY"),
        ({"issued_at": _NOW - 121, "expires_at": _NOW - 1}, "BOOTSTRAP_ATTESTATION_STALE"),
    ],
)
def test_bootstrap_transport_rejects_nonempty_or_stale_inputs(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(RelayRuntimeError) as rejected:
        _resolver().resolve_new_empty_bootstrap(_binding(**changes))

    assert rejected.value.code == code


def test_bootstrap_transport_rejects_raw_path_extra_field_and_forgery() -> None:
    raw_path = _binding()
    raw_path["workspace_root"] = "G:/raw/root"
    with pytest.raises(RelayRuntimeError) as raw_rejected:
        _resolver().resolve_new_empty_bootstrap(raw_path)
    assert raw_rejected.value.code == "BOOTSTRAP_ATTESTATION_INVALID"

    nested = _binding()
    attestation = nested["attestation"]
    assert isinstance(attestation, dict)
    attestation["raw_path"] = "G:/raw/root"
    attestation["attestation_hash"] = _canonical_hash(
        {key: value for key, value in attestation.items() if key != "attestation_hash"}
    )
    nested["binding_hash"] = _canonical_hash(
        {key: value for key, value in nested.items() if key != "binding_hash"}
    )
    with pytest.raises(RelayRuntimeError) as nested_rejected:
        _resolver().resolve_new_empty_bootstrap(nested)
    assert nested_rejected.value.code == "BOOTSTRAP_ATTESTATION_INVALID"

    with pytest.raises(RelayRuntimeError) as forged:
        _resolver(capability_valid=False).resolve_new_empty_bootstrap(_binding())
    assert forged.value.code == "BOOTSTRAP_IDENTITY_MISMATCH"


class _HostOperations:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def project_index_register(
        self, *, bootstrap_root_identity: str, attestation_hash: str
    ) -> dict[str, object]:
        self.calls.append(
            ("project_index_register", bootstrap_root_identity, attestation_hash)
        )
        if self.fail == "register":
            raise RuntimeError("register failed")
        return {"workspace_id": "sha256:" + "1" * 64}

    def project_index_sync(
        self, *, workspace_id: str, attestation_hash: str
    ) -> dict[str, object]:
        self.calls.append(("project_index_sync", workspace_id, attestation_hash))
        if self.fail == "sync":
            raise RuntimeError("sync failed")
        result: dict[str, object] = {
            "workspace_id": workspace_id,
            "attested_input_snapshot_id": "sha256:" + "7" * 64,
            "initial_manifest_hash": "sha256:" + "5" * 64,
            "initial_entry_count": 0,
            "index_snapshot_id": "sha256:" + "8" * 64,
        }
        result["index_identity"] = _canonical_hash(
            {
                "workspace_id": result["workspace_id"],
                "attested_input_snapshot_id": result[
                    "attested_input_snapshot_id"
                ],
                "initial_manifest_hash": result["initial_manifest_hash"],
                "index_snapshot_id": result["index_snapshot_id"],
            }
        )
        return result


def test_bootstrap_transport_runs_only_register_then_sync_and_binds_receipt() -> None:
    binding = _resolver().resolve_new_empty_bootstrap(_binding())
    host = _HostOperations()

    receipt = ProjectIndexBootstrapTransport(host, clock=lambda: _NOW).execute(binding)

    assert [call[0] for call in host.calls] == [
        "project_index_register",
        "project_index_sync",
    ]
    assert receipt["schema"] == "2718lab-devkit/project-index-bootstrap-receipt-v1"
    assert receipt["attestation_hash"] == binding["attestation_hash"]
    assert receipt["workspace_id"] == binding["workspace_id"]
    assert receipt["initial_manifest_hash"] == binding["initial_manifest_hash"]
    assert receipt["attested_input_snapshot_id"] == binding[
        "attested_input_snapshot_id"
    ]
    assert validate_project_index_bootstrap_receipt(
        binding, receipt, clock=lambda: _NOW
    ) == receipt
    assert "lease" not in repr(receipt).lower()
    assert "path" not in repr(receipt).lower()


def test_resolver_validates_full_binding_and_receipt_for_indexed_recompile() -> None:
    project_binding = _binding()
    resolver = _resolver()
    registry_binding = resolver.resolve_new_empty_bootstrap(project_binding)
    host = _HostOperations()
    receipt = ProjectIndexBootstrapTransport(host, clock=lambda: _NOW).execute(
        registry_binding
    )
    host.calls.clear()

    validated = resolver.validate_bootstrap_recompile(
        project_binding=project_binding,
        receipt=receipt,
    )

    assert validated == receipt
    assert host.calls == []
    with pytest.raises(RelayPlanError) as still_unavailable:
        resolver.resolve(
            workflow_id="workflow-new-project",
            workspace_id=str(registry_binding["workspace_id"]),
            input_snapshot_id=str(receipt["index_snapshot_id"]),
            atlas_packet_ids=(),
        )
    assert still_unavailable.value.code == "registry_binding_unavailable"


def test_recompile_validation_rejects_hash_only_mismatched_stale_or_unknown_inputs() -> None:
    project_binding = _binding()
    resolver = _resolver()
    registry_binding = resolver.resolve_new_empty_bootstrap(project_binding)
    receipt = ProjectIndexBootstrapTransport(
        _HostOperations(), clock=lambda: _NOW
    ).execute(registry_binding)

    with pytest.raises(RelayRuntimeError) as hash_only_binding:
        resolver.validate_bootstrap_recompile(
            project_binding={"binding_hash": project_binding["binding_hash"]},
            receipt=receipt,
        )
    assert hash_only_binding.value.code == "BOOTSTRAP_ATTESTATION_INVALID"

    with pytest.raises(RelayRuntimeError) as hash_only_receipt:
        resolver.validate_bootstrap_recompile(
            project_binding=project_binding,
            receipt={"receipt_hash": receipt["receipt_hash"]},
        )
    assert hash_only_receipt.value.code == "BOOTSTRAP_RECEIPT_INVALID"

    mismatched = dict(receipt)
    mismatched["workspace_id"] = "sha256:" + "f" * 64
    mismatched["receipt_hash"] = _canonical_hash(
        {key: value for key, value in mismatched.items() if key != "receipt_hash"}
    )
    with pytest.raises(RelayRuntimeError) as mismatch:
        resolver.validate_bootstrap_recompile(
            project_binding=project_binding,
            receipt=mismatched,
        )
    assert mismatch.value.code == "BOOTSTRAP_RECEIPT_INVALID"

    stale = dict(receipt)
    stale["issued_at"] = _NOW - 121
    stale["expires_at"] = _NOW - 1
    stale["receipt_hash"] = _canonical_hash(
        {key: value for key, value in stale.items() if key != "receipt_hash"}
    )
    with pytest.raises(RelayRuntimeError) as stale_receipt:
        resolver.validate_bootstrap_recompile(
            project_binding=project_binding,
            receipt=stale,
        )
    assert stale_receipt.value.code == "BOOTSTRAP_RECEIPT_STALE"

    unknown = dict(receipt)
    unknown["extra"] = "not-accepted"
    with pytest.raises(RelayRuntimeError) as unknown_receipt:
        resolver.validate_bootstrap_recompile(
            project_binding=project_binding,
            receipt=unknown,
        )
    assert unknown_receipt.value.code == "BOOTSTRAP_RECEIPT_INVALID"


def test_server_bootstrap_transport_resolves_one_root_and_uses_existing_operations() -> None:
    from server import _run_project_index_bootstrap_transport

    binding = _resolver().resolve_new_empty_bootstrap(_binding())

    class RootResolver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def resolve_bootstrap_root(
            self, *, bootstrap_root_identity: str, attestation_hash: str
        ) -> str:
            self.calls.append((bootstrap_root_identity, attestation_hash))
            return "G:/host-private/new-project"

    class ProjectIndex:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def project_index_register(self, root: str) -> str:
            self.calls.append(("project_index_register", root))
            return str(binding["workspace_id"])

        def sync(self, workspace_id: str) -> object:
            self.calls.append(("project_index_sync", workspace_id))
            return SimpleNamespace(
                workspace_id=workspace_id,
                manifest_hash=binding["initial_manifest_hash"],
                file_count=0,
                snapshot_id="sha256:" + "8" * 64,
            )

    roots = RootResolver()
    index = ProjectIndex()
    receipt = _run_project_index_bootstrap_transport(
        binding,
        root_resolver=roots,
        project_index=index,
        clock=lambda: _NOW,
    )

    assert roots.calls == [
        (binding["bootstrap_root_identity"], binding["attestation_hash"])
    ]
    assert index.calls == [
        ("project_index_register", "G:/host-private/new-project"),
        ("project_index_sync", binding["workspace_id"]),
    ]
    assert receipt["workspace_id"] == binding["workspace_id"]


@pytest.mark.parametrize("failure", ["register", "sync"])
def test_bootstrap_transport_never_manufactures_receipt_after_host_failure(
    failure: str,
) -> None:
    binding = _resolver().resolve_new_empty_bootstrap(_binding())
    host = _HostOperations(fail=failure)

    with pytest.raises(RelayRuntimeError) as rejected:
        ProjectIndexBootstrapTransport(host, clock=lambda: _NOW).execute(binding)

    assert rejected.value.code == "BOOTSTRAP_HOST_OPERATION_FAILED"
    assert [call[0] for call in host.calls] == (
        ["project_index_register"]
        if failure == "register"
        else ["project_index_register", "project_index_sync"]
    )


def test_bootstrap_receipt_rejects_a_foreign_attestation_or_snapshot() -> None:
    binding = _resolver().resolve_new_empty_bootstrap(_binding())
    receipt = ProjectIndexBootstrapTransport(
        _HostOperations(), clock=lambda: _NOW
    ).execute(binding)

    foreign = dict(receipt)
    foreign["index_snapshot_id"] = _HASH
    foreign["receipt_hash"] = _canonical_hash(
        {key: value for key, value in foreign.items() if key != "receipt_hash"}
    )
    with pytest.raises(RelayRuntimeError) as rejected:
        validate_project_index_bootstrap_receipt(
            binding, foreign, clock=lambda: _NOW
        )
    assert rejected.value.code == "BOOTSTRAP_RECEIPT_INVALID"


def test_runtime_bootstrap_prepares_stores_but_never_registers_or_syncs_project() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "devkit_runtime" / "bootstrap.py"
    ).read_text(encoding="utf-8")

    assert "project_index_register(" not in source
    assert ".sync(" not in source
    assert "project-index-bootstrap-receipt-v1" not in source
