"""Server-only Relay compilation keeps bootstrap authority outside MCP input."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from devkit_relay.compiler import RelayPlanError
from devkit_runtime.composition import RuntimeRoot
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.relay_runtime import RelayRuntimeError
from server import RelayCompileRequest, _compile_host_relay_request

_NOW = 1_700_000_000


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


def _bootstrap_binding() -> dict[str, object]:
    attestation: dict[str, object] = {
        "schema": "2718lab-devkit/new-project-bootstrap-attestation-v1",
        "workflow_id": "host-bootstrap",
        "workspace_id": "sha256:" + "1" * 64,
        "repository_id": "sha256:" + "2" * 64,
        "project_id": "sha256:" + "3" * 64,
        "bootstrap_root_identity": "sha256:" + "4" * 64,
        "initial_manifest_hash": "sha256:" + "5" * 64,
        "initial_entry_count": 0,
        "state": "new_empty",
        "capability_epoch": 1,
        "capability_hash": "sha256:" + "6" * 64,
        "attested_input_snapshot_id": "sha256:" + "7" * 64,
        "issued_at": _NOW - 1,
        "expires_at": _NOW + 60,
    }
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


def _task(task_id: str, *, scope: str | None = None) -> dict[str, object]:
    writer = scope is not None
    return {
        "task_id": task_id,
        "kind": "implementation" if writer else "prewarm",
        "stage": "a1_writer" if writer else "a3_prewarm",
        "title": task_id,
        "objective": task_id,
        "priority": 50,
        "dependencies": [],
        "write_scope": [{"path": scope, "kind": "file"}] if scope else [],
        "route": {
            "route_class": "terra_high" if writer else "luna_medium",
            "model": "gpt-5.6-terra" if writer else "gpt-5.6-luna",
            "reasoning_effort": "high" if writer else "medium",
        },
        "constraints": [],
        "acceptance_criteria": [],
        "atlas_packet_ids": [],
        "required_evidence": [],
        "design_for_task_id": None,
        "prewarm_for_task_id": "writer-a" if not writer else None,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
        "split_policy": None,
        "split_parent_task_id": None,
        "split_depth": 0,
        "split_verdict": None,
    }


def _v3_request() -> dict[str, object]:
    binding = _bootstrap_binding()
    return {
        "schema": "2718lab-devkit/relay-compile-request-v3",
        "workflow_id": binding["workflow_id"],
        "workspace_id": binding["workspace_id"],
        "input_snapshot_id": binding["attestation"]["attested_input_snapshot_id"],  # type: ignore[index]
        "base_commit": "a" * 40,
        "capacity": 1,
        "project_binding": binding,
        "scheduler_topology": {
            "schema": "2718lab-devkit/scheduler-topology-v1",
            "max_writers_per_scheduler": 3,
            "max_parallel_writers": 9,
            "groups": [
                {
                    "scheduler_id": "sha256:" + "8" * 64,
                    "coordinator_lease_id": "sha256:" + "9" * 64,
                    "worktree_identity": "sha256:" + "b" * 64,
                    "writer_task_ids": ["writer-a"],
                    "prewarm_task_ids": ["prewarm-a"],
                }
            ],
        },
        "tasks": [_task("writer-a", scope="src/a.py"), _task("prewarm-a")],
    }


def _v1_request() -> dict[str, object]:
    task = _task("writer-a", scope="src/a.py")
    for field in (
        "stage",
        "design_for_task_id",
        "split_policy",
        "split_parent_task_id",
        "split_depth",
        "split_verdict",
    ):
        task.pop(field)
    return {
        "schema": "2718lab-devkit/relay-compile-request-v1",
        "workflow_id": "host-indexed",
        "workspace_id": "sha256:" + "1" * 64,
        "input_snapshot_id": "sha256:" + "c" * 64,
        "base_commit": "a" * 40,
        "capacity": 1,
        "tasks": [task],
    }


class _ProjectIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.current_snapshot_id: str | None = None

    def project_index_register(self, root: str) -> str:
        self.calls.append(("register", root))
        return "sha256:" + "1" * 64

    def sync(self, workspace_id: str) -> object:
        self.calls.append(("sync", workspace_id))
        self.current_snapshot_id = "sha256:" + "c" * 64
        return SimpleNamespace(
            workspace_id=workspace_id,
            manifest_hash="sha256:" + "5" * 64,
            file_count=0,
            snapshot_id=self.current_snapshot_id,
        )

    def assert_current(self, workspace_id: str, snapshot_id: str) -> None:
        if (
            workspace_id != "sha256:" + "1" * 64
            or snapshot_id != self.current_snapshot_id
        ):
            raise RuntimeError("index unavailable")


class _AtlasStore:
    def get_packet_verified(self, packet_id: str) -> object:
        raise AssertionError(f"unexpected Atlas lookup: {packet_id}")


class _BootstrapAuthority:
    def __init__(self, *, capability_valid: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.verified: list[str] = []
        self.capability_valid = capability_valid

    def verify_bootstrap_capability(self, attestation: Mapping[str, object]) -> bool:
        self.verified.append(str(attestation["attestation_hash"]))
        return self.capability_valid

    def resolve_bootstrap_root(
        self, *, bootstrap_root_identity: str, attestation_hash: str
    ) -> str:
        self.calls.append((bootstrap_root_identity, attestation_hash))
        return "G:/host-private/bootstrap-root"


def test_public_relay_compile_model_rejects_v2_v3_authority_fields() -> None:
    request = _v3_request()

    with pytest.raises(ValidationError):
        RelayCompileRequest.model_validate(request)


def test_host_private_production_composition_requires_injected_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    opened: list[object] = []

    def unexpected_uow(**_kwargs: object) -> object:
        opened.append(object())
        return opened[-1]

    root = RuntimeRoot(
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(tmp_path / "data"),
                "CODEX_TASK_TEMP": str(scratch),
            }
        ),
        uow_factory=unexpected_uow,
    )
    monkeypatch.setattr("server._RUNTIME_ROOT", root)

    with pytest.raises(RelayRuntimeError) as rejected:
        server._compile_host_relay_request_from_runtime(
            _v3_request(), bootstrap_authority=None, clock=lambda: _NOW
        )

    assert rejected.value.code == "BOOTSTRAP_HOST_AUTHORITY_UNAVAILABLE"
    assert opened == []


def test_host_private_production_composition_rejects_non_bootstrap_schema() -> None:
    with pytest.raises(RelayRuntimeError) as rejected:
        server._compile_host_relay_request_from_runtime(
            _v1_request(),
            bootstrap_authority=_BootstrapAuthority(),
            clock=lambda: _NOW,
        )

    assert rejected.value.code == "BOOTSTRAP_HOST_REQUEST_INVALID"


@pytest.mark.parametrize(
    "schema",
    (
        "2718lab-devkit/relay-compile-request-v2",
        "2718lab-devkit/relay-compile-request-v3",
    ),
)
def test_host_private_production_composition_runs_bootstrap_receipt_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
) -> None:
    class BootstrapUow:
        def __init__(self, project_index: _ProjectIndex) -> None:
            self.project_checkpoint = SimpleNamespace(project_index=project_index)
            self.atlas_store = _AtlasStore()
            self.closed = False

        def __enter__(self) -> BootstrapUow:
            return self

        def __exit__(self, *_args: object) -> None:
            self.closed = True

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    project_index = _ProjectIndex()
    opened: list[BootstrapUow] = []
    read_modes: list[bool] = []

    def open_bootstrap_uow(*, read_only: bool, **_kwargs: object) -> BootstrapUow:
        read_modes.append(read_only)
        uow = BootstrapUow(project_index)
        opened.append(uow)
        return uow

    root = RuntimeRoot(
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(tmp_path / "data"),
                "CODEX_TASK_TEMP": str(scratch),
            }
        ),
        uow_factory=open_bootstrap_uow,
    )
    monkeypatch.setattr("server._RUNTIME_ROOT", root)
    request = _v3_request()
    if schema.endswith("-v2"):
        request["schema"] = schema
        request.pop("scheduler_topology")

    plan = server._compile_host_relay_request_from_runtime(
        request,
        bootstrap_authority=_BootstrapAuthority(),
        clock=lambda: _NOW,
    )

    assert read_modes == [False]
    assert len(opened) == 1
    assert opened[0].closed is True
    assert project_index.calls == [
        ("register", "G:/host-private/bootstrap-root"),
        ("sync", "sha256:" + "1" * 64),
    ]
    assert plan["schema"] == schema.replace("compile-request", "plan")
    assert "G:/host-private/bootstrap-root" not in repr(plan)


def test_host_v3_bootstrap_compiles_descriptor_then_recompiles_from_receipt() -> None:
    project_index = _ProjectIndex()
    authority = _BootstrapAuthority()

    plan = _compile_host_relay_request(
        _v3_request(),
        bootstrap_authority=authority,
        project_index=project_index,
        atlas_store=_AtlasStore(),
        clock=lambda: _NOW,
    )

    attestation = _bootstrap_binding()["attestation"]
    assert isinstance(attestation, dict)
    attestation_hash = attestation["attestation_hash"]
    assert authority.verified == [attestation_hash, attestation_hash, attestation_hash]
    assert authority.calls == [("sha256:" + "4" * 64, attestation_hash)]
    assert project_index.calls == [
        ("register", "G:/host-private/bootstrap-root"),
        ("sync", "sha256:" + "1" * 64),
    ]
    assert plan["schema"] == "2718lab-devkit/relay-plan-v3"
    binding = plan["project_binding"]
    assert isinstance(binding, dict)
    assert binding["mode"] == "indexed"
    assert binding["bootstrap_receipt"]["index_snapshot_id"] == "sha256:" + "c" * 64  # type: ignore[index]
    assert "bootstrap-index" not in [task["task_id"] for task in plan["tasks"]]  # type: ignore[index]
    assert "G:/host-private/bootstrap-root" not in repr(plan)


def test_host_private_compile_accepts_exact_v1_and_v2_requests() -> None:
    indexed = _ProjectIndex()
    indexed.current_snapshot_id = "sha256:" + "c" * 64
    authority = _BootstrapAuthority()

    v1_plan = _compile_host_relay_request(
        _v1_request(),
        bootstrap_authority=authority,
        project_index=indexed,
        atlas_store=_AtlasStore(),
        clock=lambda: _NOW,
    )
    v2_request = _v3_request()
    v2_request["schema"] = "2718lab-devkit/relay-compile-request-v2"
    v2_request.pop("scheduler_topology")
    v2_plan = _compile_host_relay_request(
        v2_request,
        bootstrap_authority=authority,
        project_index=_ProjectIndex(),
        atlas_store=_AtlasStore(),
        clock=lambda: _NOW,
    )

    assert v1_plan["schema"] == "2718lab-devkit/relay-plan-v1"
    assert v2_plan["schema"] == "2718lab-devkit/relay-plan-v2"
    assert authority.calls == [("sha256:" + "4" * 64, authority.verified[1])]


def test_host_private_compile_rejects_host_topology_envelope() -> None:
    request = _v3_request()
    topology = request["scheduler_topology"]
    assert isinstance(topology, dict)
    topology["schema"] = "2718lab-devkit/host-scheduler-topology-v1"

    with pytest.raises(RelayPlanError, match="invalid_scheduler_topology"):
        _compile_host_relay_request(
            request,
            bootstrap_authority=_BootstrapAuthority(),
            project_index=_ProjectIndex(),
            atlas_store=_AtlasStore(),
            clock=lambda: _NOW,
        )


def test_host_bootstrap_fails_closed_when_injected_authority_cannot_verify() -> None:
    project_index = _ProjectIndex()

    with pytest.raises(Exception, match="bootstrap_attestation_required"):
        _compile_host_relay_request(
            _v3_request(),
            bootstrap_authority=_BootstrapAuthority(capability_valid=False),
            project_index=project_index,
            atlas_store=_AtlasStore(),
            clock=lambda: _NOW,
        )
