"""The locked 17-tool stdio surface for the 2718lab DevKit runtime."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from devkit_runtime.tool_metadata import TOOL_ANNOTATIONS

if TYPE_CHECKING:
    from devkit_relay.service import RelayService
    from devkit_runtime.composition import RuntimeRoot
    from devkit_runtime.config import RuntimeConfigError
    from devkit_runtime.relay_runtime import RelayRuntime, RelayRuntimeError
    from devkit_runtime.uow import RuntimeUnitOfWork
    from project_index.checkpoints import WorkspaceOwnership

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
mcp = FastMCP(name="2718lab-devkit")
_FASTLANE_HOST_SESSION: object | None = None


class _StrictModel(BaseModel):
    """MCP request records reject unknown fields before entering a runtime UoW."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class TaskLeaseRef(_StrictModel):
    """The exact public lease tuple; it never carries a root or a capability."""

    workflow_id: str
    task_id: str
    owner: str
    lease_epoch: int

    @model_validator(mode="after")
    def _validate_shape(self) -> TaskLeaseRef:
        if (
            not self.workflow_id.strip()
            or not self.task_id.strip()
            or not self.owner.strip()
            or self.lease_epoch < 1
        ):
            raise ValueError("invalid task lease")
        return self


class RelayCompileRequest(_StrictModel):
    schema_: Literal["2718lab-devkit/relay-compile-request-v1"] = Field(alias="schema")
    workflow_id: str
    workspace_id: str
    input_snapshot_id: str
    base_commit: str
    capacity: int
    tasks: list[dict[str, object]]


class RelayStartCreateRequest(_StrictModel):
    mode: Literal["create"]
    plan: dict[str, object]
    idempotency_key: str


class RelayStartRefillRequest(_StrictModel):
    mode: Literal["refill"]
    workflow_id: str
    refill_directive_id: str
    expected_schedule_version: int
    idempotency_key: str


class _RelayLifecycleRequest(_StrictModel):
    workflow_id: str
    task_id: str
    epoch: int
    endpoint: str
    expected_task_version: int
    capability: str


class RelayBindEndpointRequest(_RelayLifecycleRequest):
    action: Literal["bind_endpoint"]


class RelayHeartbeatRequest(_RelayLifecycleRequest):
    action: Literal["heartbeat"]


class RelayEvidenceRequest(_RelayLifecycleRequest):
    action: Literal["evidence"]
    evidence: dict[str, object]


class RelayTerminalRequest(_RelayLifecycleRequest):
    action: Literal["terminal"]
    outcome: object


class RelayCandidateHandoffRequest(_RelayLifecycleRequest):
    action: Literal["candidate_handoff"]
    candidate: dict[str, object]


class RelayApproveReadonlyRequest(_RelayLifecycleRequest):
    action: Literal["approve_readonly"]


class RelayReviewRequest(_RelayLifecycleRequest):
    action: Literal["review"]
    candidate_id: str
    review_digest: str


class RelayRebaseRequest(_RelayLifecycleRequest):
    action: Literal["rebase"]
    candidate_id: str
    base_commit: str
    head_commit: str
    diff_hash: str
    evidence_hashes: list[str]


class RelayRejectRequest(_RelayLifecycleRequest):
    action: Literal["reject"]
    candidate_id: str


class RelayIntegrateRequest(_RelayLifecycleRequest):
    action: Literal["integrate"]
    candidate_id: str
    integration_proof_id: str


RelayStartRequest = RelayStartCreateRequest | RelayStartRefillRequest
RelayHandoffRequest = (
    RelayBindEndpointRequest
    | RelayHeartbeatRequest
    | RelayEvidenceRequest
    | RelayTerminalRequest
    | RelayCandidateHandoffRequest
)
RelayIntegrationRequest = (
    RelayApproveReadonlyRequest
    | RelayReviewRequest
    | RelayRebaseRequest
    | RelayRejectRequest
    | RelayIntegrateRequest
)


class _TaskLeaseAuthority(Protocol):
    """Host-private task authority injected by an embedding process when present."""

    def ownership_for(
        self, task_lease: TaskLeaseRef, *, workspace_id: str
    ) -> WorkspaceOwnership: ...

    def bind_output_snapshot(
        self, task_lease: TaskLeaseRef, *, workspace_id: str, snapshot: object
    ) -> None: ...

    def record_query_receipt(
        self, task_lease: TaskLeaseRef, *, workspace_id: str, result: object
    ) -> None: ...


def _compile_host_relay_request_from_runtime(
    request: Mapping[str, object],
    *,
    clock: Callable[[], float],
) -> dict[str, object]:
    """Delegate every V2/V3 bootstrap to the fixed RuntimeRoot composition."""

    return _runtime_root().host_relay_bootstrap().compile(request, clock=clock)


class _RequestError(ValueError):
    """A bounded, public request failure raised before a persistent operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_TModel = TypeVar("_TModel", bound=BaseModel)
_RUNTIME_ROOT: RuntimeRoot | None = None
_TASK_LEASE_AUTHORITY: _TaskLeaseAuthority | None = None
_MAX_PACKAGE_PAGE_LIMIT = 128


def _tool_annotations(name: str) -> ToolAnnotations:
    read_only, destructive, idempotent, open_world = TOOL_ANNOTATIONS[name]
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _default_runtime_root() -> RuntimeRoot:
    """Bootstrap durable local stores before exposing the default process root."""

    from devkit_runtime.bootstrap import RuntimeBootstrap
    from devkit_runtime.composition import RuntimeRoot
    from devkit_runtime.config import RuntimeConfig

    config = RuntimeConfig.load(protected_roots=(PLUGIN_ROOT,))
    required_databases = (
        config.orchestrator_database,
        config.project_index_database,
        config.continuity_database,
        config.atlas_database,
        config.relay_database,
        config.relay_proof_registry_database,
    )
    if not all(path.is_file() for path in required_databases):
        RuntimeBootstrap.run(config)
    return RuntimeRoot(config)


def _runtime_root() -> RuntimeRoot:
    global _RUNTIME_ROOT
    if _RUNTIME_ROOT is None:
        _RUNTIME_ROOT = _default_runtime_root()
    return _RUNTIME_ROOT


def _install_runtime_root_for_host(root: RuntimeRoot) -> None:
    """Private embedding seam for a host-injected broker or proof resolver."""

    from devkit_runtime.composition import RuntimeRoot

    if not isinstance(root, RuntimeRoot):
        raise TypeError("root must be a RuntimeRoot")
    global _RUNTIME_ROOT
    previous = _RUNTIME_ROOT
    _RUNTIME_ROOT = root
    if previous is not None and previous is not root:
        previous.shutdown()


def _install_task_lease_authority_for_host(authority: _TaskLeaseAuthority) -> None:
    """Install a private verifier without adding a public workflow surface."""

    global _TASK_LEASE_AUTHORITY
    _TASK_LEASE_AUTHORITY = authority


def _shutdown_runtime() -> None:
    root = _RUNTIME_ROOT
    if root is not None:
        root.shutdown()
    session = _FASTLANE_HOST_SESSION
    close = getattr(session, "close", None)
    if callable(close):
        close()


atexit.register(_shutdown_runtime)


def _failure(code: str) -> dict[str, object]:
    from devkit_runtime.tool_result import envelope_failure

    return envelope_failure(code)


def _host_session() -> object:
    global _FASTLANE_HOST_SESSION
    if _FASTLANE_HOST_SESSION is None:
        from devkit_runtime.host_session import HostSession

        _FASTLANE_HOST_SESSION = HostSession.from_environment(
            environ=None, platform=os.name, clock=time.time
        )
    return _FASTLANE_HOST_SESSION


def _project_index_attestation(
    uow: RuntimeUnitOfWork,
    operation: str,
    *,
    workspace_id: str,
    snapshot_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, object] | None:
    """Send only persisted, path-free Project Index evidence to a live Host."""

    from devkit_runtime.host_bridge import build_project_index_attestation
    from devkit_runtime.host_session import HostSession

    correlation_id = _current_index_correlation()
    if correlation_id is None:
        return None
    session = _host_session()
    if type(session) is not HostSession or not session.is_available:
        return None
    material = uow.project_checkpoint.project_index.host_attestation_material(
        workspace_id,
        snapshot_id=snapshot_id,
        trace_id=trace_id,
    )
    now = int(time.time())
    attestation = build_project_index_attestation(
        operation=operation,
        correlation_id=correlation_id,
        material=material,
        now=now,
    )
    sent = session.send_project_index_attestation(attestation)
    if sent is None:
        return None
    return {
        key: value
        for key, value in sent.items()
        if key
        in {
            "correlation_id",
            "workspace_id",
            "workspace_binding_hash",
            "root_identity_hash",
            "snapshot_id",
            "snapshot_attestation_hash",
            "query_receipt_hash",
            "index_context_hash",
            "attestation_hash",
            "expires_at",
        }
    }


def _current_index_correlation() -> str | None:
    """Read the Host reservation from MCP `_meta`, never from public arguments."""

    try:
        meta = mcp.get_context().request_context.meta
    except (LookupError, ValueError):
        return None
    if meta is None or type(meta.model_extra) is not dict:
        return None
    value = meta.model_extra.get("2718lab/host-index-correlation")
    if (
        type(value) is not str
        or len(value) != 70
        or not value.startswith("index-")
        or any(character not in "0123456789abcdef" for character in value[6:])
    ):
        return None
    return value


def _current_fastlane_intent_hash() -> str | None:
    """Read the Host call intent from current MCP metadata only."""

    try:
        meta = mcp.get_context().request_context.meta
    except (LookupError, ValueError):
        return None
    if meta is None or type(meta.model_extra) is not dict:
        return None
    value = meta.model_extra.get("2718lab/host-fastlane-intent-hash")
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        return None
    return value


def _current_fastlane_index_query_correlation() -> str | None:
    """Read the Host-selected Fast Lane query receipt from reserved metadata."""

    try:
        meta = mcp.get_context().request_context.meta
    except (LookupError, ValueError):
        return None
    if meta is None or type(meta.model_extra) is not dict:
        return None
    value = meta.model_extra.get("2718lab/host-fastlane-index-query-correlation")
    if (
        type(value) is not str
        or len(value) != 70
        or not value.startswith("index-")
        or any(character not in "0123456789abcdef" for character in value[6:])
    ):
        return None
    return value


def _sync_result_snapshot_id(value: object) -> str:
    from project_index.models import IndexSyncResult

    if type(value) is not IndexSyncResult:
        raise TypeError("invalid sync result")
    return value.snapshot.snapshot_id


def _query_result_trace_id(value: object) -> str:
    from project_index.models import QueryResult

    if type(value) is not QueryResult:
        raise TypeError("invalid query result")
    return value.trace_id


def _runtime_failure(
    error: RuntimeConfigError | RelayRuntimeError,
) -> dict[str, object]:
    from devkit_runtime.config import RuntimeConfigError

    if isinstance(error, RuntimeConfigError):
        if error.code in {
            "DATA_ROOT_INVALID",
            "DATA_ROOT_UNAVAILABLE",
            "PROJECT_AUTHORITY_UNAVAILABLE",
            "PROJECT_SCOPE_INVALID",
        }:
            return _failure(error.code)
        return _failure("INTERNAL_ERROR")
    if error.code == "RELAY_STORAGE_ERROR":
        return _failure("STORAGE_ERROR")
    if error.code in {"RELAY_REQUEST_INVALID", "RELAY_CAPABILITY_BROKER_UNAVAILABLE"}:
        return _failure(error.code)
    return _failure("INTERNAL_ERROR")


def _invoke(
    tool_name: str,
    *,
    read_only: bool,
    invalid_code: str = "INVALID_REQUEST",
    operation: Callable[[RuntimeUnitOfWork], object],
    private_success: Callable[[RuntimeUnitOfWork, object], dict[str, object] | None]
    | None = None,
) -> dict[str, object]:
    """Run one operation inside a fresh UoW and project its exact result."""

    try:
        with _runtime_root().open_uow(read_only=read_only) as uow:
            value = operation(uow)
            projected = uow.tool_results.project(tool_name, value)
            if private_success is not None:
                attestation = private_success(uow, value)
                if attestation is not None:
                    data = projected.get("data")
                    if type(data) is not dict:
                        raise TypeError("successful result has no data")
                    data["index_attestation"] = attestation
            return projected
    except _RequestError as error:
        return _failure(error.code)
    except Exception as error:
        from devkit_runtime.config import RuntimeConfigError
        from devkit_runtime.relay_runtime import RelayRuntimeError
        from devkit_runtime.tool_result import (
            ResultContractError,
            result_from_exception,
        )

        if isinstance(error, (RuntimeConfigError, RelayRuntimeError)):
            return _runtime_failure(error)
        if isinstance(error, ResultContractError):
            return _failure("INTERNAL_ERROR")
        return result_from_exception(error, invalid_code=invalid_code)


def _parse_model(value: object, model: type[_TModel], *, code: str) -> _TModel:
    try:
        if isinstance(value, model):
            return value
        return model.model_validate(value)
    except ValidationError as error:
        raise _RequestError(code) from error


def _task_lease(value: TaskLeaseRef | None) -> TaskLeaseRef | None:
    if value is None:
        return None
    return _parse_model(value, TaskLeaseRef, code="INVALID_REQUEST")


def _package_page_request(
    offset: int | None,
    limit: int | None,
    *,
    require_snapshot: bool,
    snapshot_id: str | None,
) -> tuple[int, int] | None:
    """Validate an explicit package catalog page before opening a UoW."""

    if offset is None and limit is None:
        return None
    if (
        offset is None
        or limit is None
        or type(offset) is not int
        or type(limit) is not int
        or offset < 0
        or not 1 <= limit <= _MAX_PACKAGE_PAGE_LIMIT
    ):
        raise _RequestError("INVALID_QUERY")
    if require_snapshot and (type(snapshot_id) is not str or not snapshot_id):
        raise _RequestError("INVALID_QUERY")
    return offset, limit


def _model_payload(value: BaseModel) -> dict[str, object]:
    return cast(dict[str, object], value.model_dump(by_alias=True))


def _relay_compile_request(value: RelayCompileRequest) -> dict[str, object]:
    return _model_payload(
        _parse_model(value, RelayCompileRequest, code="RELAY_REQUEST_INVALID")
    )


def _relay_start_request(value: RelayStartRequest) -> dict[str, object]:
    if isinstance(value, (RelayStartCreateRequest, RelayStartRefillRequest)):
        return _model_payload(value)
    if type(value) is not dict:
        raise _RequestError("RELAY_REQUEST_INVALID")
    model = {
        "create": RelayStartCreateRequest,
        "refill": RelayStartRefillRequest,
    }.get(value.get("mode"))
    if model is None:
        raise _RequestError("RELAY_REQUEST_INVALID")
    return _model_payload(_parse_model(value, model, code="RELAY_REQUEST_INVALID"))


def _relay_handoff_request(value: RelayHandoffRequest) -> dict[str, object]:
    handoff_models: tuple[type[BaseModel], ...] = (
        RelayBindEndpointRequest,
        RelayHeartbeatRequest,
        RelayEvidenceRequest,
        RelayTerminalRequest,
        RelayCandidateHandoffRequest,
    )
    if isinstance(value, handoff_models):
        return _model_payload(value)
    if type(value) is not dict:
        raise _RequestError("RELAY_REQUEST_INVALID")
    model = {
        "bind_endpoint": RelayBindEndpointRequest,
        "heartbeat": RelayHeartbeatRequest,
        "evidence": RelayEvidenceRequest,
        "terminal": RelayTerminalRequest,
        "candidate_handoff": RelayCandidateHandoffRequest,
    }.get(value.get("action"))
    if model is None:
        raise _RequestError("RELAY_REQUEST_INVALID")
    return _model_payload(_parse_model(value, model, code="RELAY_REQUEST_INVALID"))


def _relay_integrate_request(value: RelayIntegrationRequest) -> dict[str, object]:
    integrate_models: tuple[type[BaseModel], ...] = (
        RelayApproveReadonlyRequest,
        RelayReviewRequest,
        RelayRebaseRequest,
        RelayRejectRequest,
        RelayIntegrateRequest,
    )
    if isinstance(value, integrate_models):
        return _model_payload(value)
    if type(value) is not dict:
        raise _RequestError("RELAY_REQUEST_INVALID")
    model = {
        "approve_readonly": RelayApproveReadonlyRequest,
        "review": RelayReviewRequest,
        "rebase": RelayRebaseRequest,
        "reject": RelayRejectRequest,
        "integrate": RelayIntegrateRequest,
    }.get(value.get("action"))
    if model is None:
        raise _RequestError("RELAY_REQUEST_INVALID")
    return _model_payload(_parse_model(value, model, code="RELAY_REQUEST_INVALID"))


def _require_lease_authority() -> _TaskLeaseAuthority:
    if _TASK_LEASE_AUTHORITY is None:
        raise _RequestError("WORKTREE_UNOWNED")
    return _TASK_LEASE_AUTHORITY


def _workspace_ownership(
    task_lease: TaskLeaseRef, *, workspace_id: str
) -> WorkspaceOwnership:
    from project_index.checkpoints import WorkspaceOwnership

    ownership = _require_lease_authority().ownership_for(
        task_lease, workspace_id=workspace_id
    )
    if (
        not isinstance(ownership, WorkspaceOwnership)
        or ownership.workflow_id != task_lease.workflow_id
        or ownership.task_id != task_lease.task_id
        or ownership.owner != task_lease.owner
        or ownership.lease_epoch != task_lease.lease_epoch
        or ownership.workspace_id != workspace_id
    ):
        raise _RequestError("WORKTREE_UNOWNED")
    return ownership


def _relay_runtime(uow: RuntimeUnitOfWork) -> RelayRuntime:
    from devkit_runtime.relay_runtime import RelayRuntime

    relay = uow.relay
    if not isinstance(relay, RelayRuntime):
        raise _RequestError("RELAY_REQUEST_INVALID")
    return relay


def _relay_service(runtime: RelayRuntime) -> RelayService:
    """Use the lifecycle service already owned by the typed Relay runtime."""

    from devkit_relay.service import RelayService

    service = runtime._relay_service
    if not isinstance(service, RelayService):
        raise _RequestError("RELAY_REQUEST_INVALID")
    return service


@mcp.tool(annotations=_tool_annotations("project_index_register"))
def project_index_register(workspace_root: str) -> dict[str, object]:
    """Register the sole public path input and return only an opaque workspace id."""

    return _invoke(
        "project_index_register",
        read_only=False,
        operation=lambda uow: (
            uow.project_checkpoint.project_index.project_index_register(workspace_root)
        ),
        private_success=lambda uow, value: _project_index_attestation(
            uow, "register", workspace_id=str(value)
        ),
    )


@mcp.tool(annotations=_tool_annotations("project_index_sync"))
def project_index_sync(
    workspace_id: str,
    include_paths: list[str] | None = None,
    task_lease: TaskLeaseRef | None = None,
    bind_as: Literal["output"] | None = None,
    package_page_limit: int = 128,
) -> dict[str, object]:
    """Synchronize one registered workspace without accepting a second root."""

    try:
        lease = _task_lease(task_lease)
        package_page = _package_page_request(
            0,
            package_page_limit,
            require_snapshot=False,
            snapshot_id=None,
        )
        if (lease is None) != (bind_as is None):
            raise _RequestError("INVALID_REQUEST")
    except _RequestError as error:
        return _failure(error.code)

    def operation(uow: RuntimeUnitOfWork) -> object:
        from project_index.models import IndexSyncResult

        authority = _require_lease_authority() if lease is not None else None
        snapshot = uow.project_checkpoint.project_index.sync(
            workspace_id, include_paths
        )
        if package_page is None:
            raise _RequestError("INVALID_QUERY")
        page_offset, page_limit = package_page
        result = IndexSyncResult(
            snapshot=snapshot,
            package_page=uow.project_checkpoint.project_index.package_page(
                workspace_id,
                snapshot.snapshot_id,
                offset=page_offset,
                limit=page_limit,
            ),
        )
        if authority is not None:
            if lease is None:
                raise _RequestError("INVALID_REQUEST")
            authority.bind_output_snapshot(
                lease, workspace_id=workspace_id, snapshot=snapshot
            )
        return result

    return _invoke(
        "project_index_sync",
        read_only=False,
        operation=operation,
        private_success=lambda uow, value: _project_index_attestation(
            uow,
            "sync",
            workspace_id=workspace_id,
            snapshot_id=_sync_result_snapshot_id(value),
        ),
    )


@mcp.tool(annotations=_tool_annotations("project_index_status"))
def project_index_status(
    workspace_id: str,
    snapshot_id: str | None = None,
    required_paths: list[str] | None = None,
    package_ids: list[str] | None = None,
    package_page_offset: int | None = None,
    package_page_limit: int | None = None,
) -> dict[str, object]:
    """Read verified index status through the opaque workspace boundary."""

    try:
        package_page = _package_page_request(
            package_page_offset,
            package_page_limit,
            require_snapshot=True,
            snapshot_id=snapshot_id,
        )
    except _RequestError as error:
        return _failure(error.code)

    def operation(uow: RuntimeUnitOfWork) -> object:
        from project_index.models import IndexState, IndexStatusResult

        status = uow.project_checkpoint.project_index.status(
            workspace_id,
            snapshot_id,
            required_paths,
            package_ids=None if package_ids is None else tuple(package_ids),
        )
        if package_page is None or status.state is IndexState.INDEX_UNAVAILABLE:
            return status
        if snapshot_id is None:
            raise _RequestError("INVALID_QUERY")
        page_offset, page_limit = package_page
        return IndexStatusResult(
            status=status,
            package_page=uow.project_checkpoint.project_index.package_page(
                workspace_id,
                snapshot_id,
                offset=page_offset,
                limit=page_limit,
            ),
        )

    return _invoke(
        "project_index_status",
        read_only=True,
        operation=operation,
    )


@mcp.tool(annotations=_tool_annotations("project_index_query"))
def project_index_query(
    workspace_id: str,
    snapshot_id: str,
    query: str,
    mode: Literal["lexical", "graph", "impact"] = "lexical",
    node_kinds: list[str] | None = None,
    relations: list[str] | None = None,
    max_nodes: int = 50,
    max_depth: int = 1,
    source_lines: int = 12,
    byte_budget: int = 32768,
    allow_miss_escape: bool = False,
    task_lease: TaskLeaseRef | None = None,
    package_ids: list[str] | None = None,
) -> dict[str, object]:
    """Run a receipt-persisting bounded query against one workspace id."""

    try:
        lease = _task_lease(task_lease)
    except _RequestError as error:
        return _failure(error.code)

    def operation(uow: RuntimeUnitOfWork) -> object:
        authority = _require_lease_authority() if lease is not None else None
        result = uow.project_checkpoint.query(
            workspace_id,
            snapshot_id,
            query,
            mode=mode,
            node_kinds=tuple(node_kinds or ()),
            relations=tuple(relations or ()),
            max_nodes=max_nodes,
            max_depth=max_depth,
            source_lines=source_lines,
            byte_budget=byte_budget,
            allow_miss_escape=allow_miss_escape,
            package_ids=None if package_ids is None else tuple(package_ids),
        )
        if authority is not None:
            if lease is None:
                raise _RequestError("INVALID_REQUEST")
            authority.record_query_receipt(
                lease, workspace_id=workspace_id, result=result
            )
        return result

    return _invoke(
        "project_index_query",
        read_only=False,
        operation=operation,
        private_success=lambda uow, value: _project_index_attestation(
            uow,
            "query",
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            trace_id=_query_result_trace_id(value),
        ),
    )


@mcp.tool(annotations=_tool_annotations("worktree_checkpoint_create"))
def worktree_checkpoint_create(
    workspace_id: str, task_lease: TaskLeaseRef, snapshot_id: str
) -> dict[str, object]:
    """Create a checkpoint only after a host-private lease authority resolves scope."""

    try:
        lease = _task_lease(task_lease)
        if lease is None:
            raise _RequestError("INVALID_REQUEST")
    except _RequestError as error:
        return _failure(error.code)
    return _invoke(
        "worktree_checkpoint_create",
        read_only=False,
        operation=lambda uow: uow.project_checkpoint.create(
            workspace_id,
            _workspace_ownership(lease, workspace_id=workspace_id),
            snapshot_id,
        ),
    )


@mcp.tool(annotations=_tool_annotations("worktree_checkpoint_status"))
def worktree_checkpoint_status(
    workspace_id: str, checkpoint_id: str
) -> dict[str, object]:
    """Read checkpoint metadata through its compound workspace boundary."""

    return _invoke(
        "worktree_checkpoint_status",
        read_only=True,
        operation=lambda uow: uow.project_checkpoint.status(
            workspace_id, checkpoint_id
        ),
    )


@mcp.tool(annotations=_tool_annotations("worktree_checkpoint_restore"))
def worktree_checkpoint_restore(
    workspace_id: str,
    task_lease: TaskLeaseRef,
    checkpoint_id: str,
    expected_current_snapshot_id: str,
) -> dict[str, object]:
    """Restore only a task-owned checkpoint after the root authority check."""

    try:
        lease = _task_lease(task_lease)
        if lease is None:
            raise _RequestError("INVALID_REQUEST")
    except _RequestError as error:
        return _failure(error.code)
    return _invoke(
        "worktree_checkpoint_restore",
        read_only=False,
        operation=lambda uow: uow.project_checkpoint.restore(
            workspace_id,
            _workspace_ownership(lease, workspace_id=workspace_id),
            checkpoint_id,
            expected_current_snapshot_id,
        ),
    )


@mcp.tool(annotations=_tool_annotations("atlas_query"))
def atlas_query(
    root_node_ids: list[str] | None = None,
    node_kinds: list[str] | None = None,
    relations: list[str] | None = None,
    intent_id: str | None = None,
    max_nodes: int = 50,
    max_edges: int = 100,
    max_depth: int = 1,
    byte_budget: int = 65536,
) -> dict[str, object]:
    """Read bounded Atlas graph facts only."""

    return _invoke(
        "atlas_query",
        read_only=True,
        operation=lambda uow: uow.atlas.graph_query(
            root_node_ids=root_node_ids,
            node_kinds=node_kinds,
            relations=relations,
            intent_id=intent_id,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_depth=max_depth,
            byte_budget=byte_budget,
        ),
        invalid_code="ATLAS_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("atlas_prepare"))
def atlas_prepare(
    workspace_id: str,
    snapshot_id: str,
    intent_id: str,
    language: str,
    framework: str | None = None,
    target_paths: list[str] | None = None,
    target_symbols: list[str] | None = None,
    max_candidates: int = 20,
    byte_budget: int = 131072,
) -> dict[str, object]:
    """Prepare an Atlas packet through one call-owned read/write UoW."""

    return _invoke(
        "atlas_prepare",
        read_only=False,
        operation=lambda uow: uow.atlas.prepare(
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            intent_id=intent_id,
            language=language,
            framework=framework,
            target_paths=target_paths,
            target_symbols=target_symbols,
            max_candidates=max_candidates,
            byte_budget=byte_budget,
        ),
        invalid_code="ATLAS_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("atlas_render"))
def atlas_render(
    workspace_id: str, snapshot_id: str, packet_id: str, bindings: dict[str, str]
) -> dict[str, object]:
    """Render a verified Atlas packet without exposing any host path."""

    return _invoke(
        "atlas_render",
        read_only=True,
        operation=lambda uow: uow.atlas.render(
            workspace_id, snapshot_id, packet_id, bindings
        ),
        invalid_code="ATLAS_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("atlas_accept"))
def atlas_accept(
    workflow_id: str, code_task_id: str, acceptance_id: str, ingestion_key: str
) -> dict[str, object]:
    """Accept Atlas evidence by its four opaque identifiers only."""

    return _invoke(
        "atlas_accept",
        read_only=False,
        operation=lambda uow: uow.accept_atlas(
            workflow_id, code_task_id, acceptance_id, ingestion_key
        ),
        invalid_code="ATLAS_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("relay_compile"))
def relay_compile(request: RelayCompileRequest) -> dict[str, object]:
    """Compile a locked Relay request using only verified read snapshots."""

    from devkit_relay.compiler import compile_plan

    try:
        payload = _relay_compile_request(request)
    except _RequestError as error:
        return _failure(error.code)
    return _invoke(
        "relay_compile",
        read_only=True,
        operation=lambda uow: compile_plan(payload, registry_resolver=uow.registry),
        invalid_code="RELAY_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("fastlane_compile"))
def fastlane_compile(
    request: dict[str, object],
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"],
    enable: bool = False,
) -> dict[str, object]:
    """Compile inert Fast Lane descriptors without receiving host-private evidence.

    The host remains responsible for capability attestations, worktree/lease fencing,
    model dispatch, terminal receipts, and execution.  This MCP boundary only
    validates and renders the deterministic local scheduling plan.
    """

    if type(request) is not dict or type(enable) is not bool:
        return _failure("FASTLANE_REQUEST_INVALID")
    session = _host_session()
    intent_hash = _current_fastlane_intent_hash()
    index_query_correlation = _current_fastlane_index_query_correlation()
    from devkit_runtime.host_session import HostSession

    if type(session) is HostSession and session.is_available:
        if intent_hash is None or index_query_correlation is None or not enable:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        return _fastlane_authenticated_dispatch(
            request,
            reasoning_effort,
            call_intent_hash=intent_hash,
            index_query_correlation=index_query_correlation,
        )
    from devkit_fastlane import compile_fast_lane
    from devkit_runtime.tool_result import ResultContractError, envelope_success

    try:
        plan = compile_fast_lane(
            request,
            reasoning_effort=reasoning_effort,
            enable=enable,
        )
        return envelope_success(_fastlane_public_value(plan))
    except ResultContractError:
        return _failure("INTERNAL_ERROR")
    except (TypeError, ValueError):
        return _failure("FASTLANE_REQUEST_INVALID")


def _fastlane_authenticated_dispatch(
    request: dict[str, object],
    reasoning_effort: str,
    *,
    call_intent_hash: str,
    index_query_correlation: str,
) -> dict[str, object]:
    """Use no request-carried authority; the inherited bridge is the only gate."""

    from devkit_runtime.fastlane_host_adapter import (
        NO_SAFE_WORK,
        dispatch_fast_lane_with_host_facts,
        prepare_verified_host_facts,
    )
    from devkit_runtime.host_bridge import (
        FastLaneRefillRegistryRequest,
        OperationReceipt,
    )
    from devkit_runtime.host_session import HostRoute, HostSession
    from devkit_runtime.tool_result import ResultContractError, envelope_success

    try:
        if reasoning_effort == "ultra":
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        session = _host_session()
        if type(session) is not HostSession or not session.is_available:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        index_attestation = session.project_index_query_attestation(
            correlation_id=index_query_correlation
        )
        if index_attestation is None:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        index_context_hash = index_attestation.get("index_context_hash")
        if type(index_context_hash) is not str:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        project_binding = request.get("project_binding")
        work_package = request.get("work_package")
        if (
            type(project_binding) is not dict
            or type(work_package) is not dict
            or project_binding.get("workspace_id")
            != index_attestation.get("workspace_id")
            or work_package.get("input_snapshot_id")
            != index_attestation.get("snapshot_id")
        ):
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        preparation_id = f"compiler-{secrets.token_hex(16)}"
        capability_snapshot = session.resolve_capability_snapshot_v2(
            call_intent_hash=call_intent_hash,
            preparation_id=preparation_id,
            expires_at_ceiling=cast(int, index_attestation["expires_at"]),
        )
        if capability_snapshot is None:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        from devkit_fastlane.scripts.team_efficiency import (
            compile_authenticated_v5_assignment_skeletons,
            prepare_authenticated_v5_routing_from_request,
            validate_authenticated_v5_skeleton_package,
        )

        projected = prepare_authenticated_v5_routing_from_request(
            request,
            index_context_hash=index_context_hash,
            host_capabilities=capability_snapshot.host_capabilities,
            scheduler_facts=capability_snapshot.scheduler_facts,
        )
        routing_snapshot = session.resolve_routing_attestations(
            call_intent_hash=call_intent_hash,
            preparation_id=preparation_id,
            routing_requests=projected["all_routing_requests"],
        )
        if routing_snapshot is None:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        initial_units = projected["units"]
        remaining_units = projected["remaining_units"]
        initial_storage_budgets = {
            unit["task"]["task_id"]: unit["storage_budget"]
            for unit in initial_units
            if "storage_budget" in unit
        }
        # The compiler/profile exchange can attest only the live initial
        # skeletons. Remaining work is materialized later by the Host-owned
        # refill registry, which has no storage-profile proof channel yet.
        # Budgeted successors therefore fail closed before publication.
        if len(initial_storage_budgets) not in {0, len(initial_units)} or any(
            "storage_budget" in unit for unit in remaining_units
        ):
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        initial_task_ids = {unit["task"]["task_id"] for unit in initial_units}
        initial_requests: list[dict[str, object]] = []
        remaining_requests: list[dict[str, object]] = []
        for item in routing_snapshot.routing_requests:
            task = item.get("task")
            if type(task) is not dict:
                return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
            task_id = task.get("task_id")
            if type(task_id) is not str:
                return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
            if task_id in initial_task_ids:
                initial_requests.append(item)
            else:
                remaining_requests.append(item)
        initial_attestations = [
            item
            for item in routing_snapshot.attestations
            if item["task_id"] in initial_task_ids
        ]
        if len(initial_requests) != len(initial_units) or len(
            initial_attestations
        ) != len(initial_units):
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        compiled = compile_authenticated_v5_assignment_skeletons(
            initial_units,
            source_plan_hash=projected["source_plan_hash"],
            routing_requests=initial_requests,
            attestation_items=initial_attestations,
        )
        compiled_remaining = (
            compile_authenticated_v5_assignment_skeletons(
                remaining_units,
                source_plan_hash=projected["source_plan_hash"],
                routing_requests=remaining_requests,
                attestation_items=[
                    item
                    for item in routing_snapshot.attestations
                    if item["task_id"] not in initial_task_ids
                ],
            )
            if remaining_units
            else {"assignment_skeletons": [], "requested_route_pairs": []}
        )
        attestation_ref_fields = (
            "correlation_id",
            "workspace_id",
            "workspace_binding_hash",
            "root_identity_hash",
            "snapshot_id",
            "snapshot_attestation_hash",
            "query_receipt_hash",
            "index_context_hash",
            "attestation_hash",
        )
        raw_skeletons = compiled.get("assignment_skeletons")
        raw_remaining_skeletons = compiled_remaining.get("assignment_skeletons")
        if type(raw_skeletons) is not list or type(raw_remaining_skeletons) is not list:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        skeletons: list[dict[str, object]] = []
        remaining_skeletons: list[dict[str, object]] = []
        for raw_skeleton, destination in (
            (raw_skeletons, skeletons),
            (raw_remaining_skeletons, remaining_skeletons),
        ):
            for skeleton in raw_skeleton:
                if (
                    type(skeleton) is not dict
                    or type(skeleton.get("task_id")) is not str
                ):
                    return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
                destination.append(skeleton)
        skeleton_package_hash = validate_authenticated_v5_skeleton_package(
            projected["all_units"],
            skeletons,
            remaining_skeletons,
            source_plan_hash=projected["source_plan_hash"],
        )
        index_refs = [
            {
                "task_id": skeleton["task_id"],
                **{field: index_attestation[field] for field in attestation_ref_fields},
            }
            for skeleton in skeletons
        ]
        planner_request = {
            "schema": "2718lab-devkit/fastlane-host-planner-request-v1",
            "action": "plan_dispatch",
            "assignment_skeletons": skeletons,
            "project_index_attestation_refs": index_refs,
        }
        requested_route_pairs: set[tuple[str, str]] = set()
        for item in routing_snapshot.attestations:
            route = cast(dict[str, object], item["route"])
            model = route.get("model")
            effort = route.get("effort")
            if type(model) is not str or type(effort) is not str:
                return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
            requested_route_pairs.add((model, effort))
        requested_routes = tuple(
            HostRoute(model=model, effort=effort)
            for model, effort in sorted(requested_route_pairs)
        )
        prepared = prepare_verified_host_facts(
            session,
            preparation_id=preparation_id,
            call_intent_hash=call_intent_hash,
            routing_registry_binding_hash=(
                routing_snapshot.routing_registry_binding_hash
            ),
            request=planner_request,
            reasoning_effort=reasoning_effort,
            requested_routes=requested_routes,
            storage_budgets=initial_storage_budgets,
        )
        if prepared == NO_SAFE_WORK:
            return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")
        now = int(time.time())
        correlation_id = f"operation-{secrets.randbelow(999_999_999_999) + 1}"

        queue_registry = None
        if remaining_units:
            queue_registry = session.send_fast_lane_refill_registry(
                call_intent_hash=call_intent_hash,
                preparation_id=preparation_id,
                source_plan_hash=projected["source_plan_hash"],
                index_context_hash=index_context_hash,
                routing_registry_binding_hash=(
                    routing_snapshot.routing_registry_binding_hash
                ),
                source_plan_task_ids=[
                    unit["task"]["task_id"] for unit in projected["all_units"]
                ],
                initial_skeletons=skeletons,
                remaining_skeletons=remaining_skeletons,
                index_attestation_refs=[
                    {
                        "task_id": skeleton["task_id"],
                        **{
                            field: index_attestation[field]
                            for field in attestation_ref_fields
                        },
                    }
                    for skeleton in remaining_skeletons
                ],
                skeleton_package_hash=skeleton_package_hash,
                now=now,
            )
            if type(queue_registry) is not FastLaneRefillRegistryRequest:
                return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")

        def refill_callback(trigger: Mapping[str, object]) -> dict[str, object]:
            """Record the real next-boundary result for this fully dispatched plan."""

            request_hash = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
            )
            queued_ids = [skeleton["task_id"] for skeleton in remaining_skeletons]
            return {
                "schema": "2718lab-devkit/fastlane-refill-receipt-v1",
                "state": ("QUEUED_WAVE_PENDING" if queued_ids else "NO_QUEUED_WORK"),
                "request_hash": request_hash,
                "refill_trigger_hash": trigger["refill_trigger_hash"],
                "queue_registry_hash": (
                    queue_registry.queue_registry_hash if queue_registry else None
                ),
                "queued_task_ids": queued_ids,
            }

        receipt = dispatch_fast_lane_with_host_facts(
            planner_request,
            reasoning_effort=reasoning_effort,
            verified_host_facts=prepared,
            correlation_id=correlation_id,
            now=now,
            refill_callback=refill_callback,
        )
        if type(receipt) is not OperationReceipt:
            return _failure("FASTLANE_HOST_DISPATCH_REJECTED")
        return envelope_success(
            {
                "state": "DISPATCH_COMMITTED",
                "task_id": receipt.task_id,
                "correlation_id": receipt.correlation_id,
                "dispatch_envelope_hash": receipt.envelope_hash,
            }
        )
    except ResultContractError:
        return _failure("INTERNAL_ERROR")
    except Exception:
        return _failure("FASTLANE_HOST_AUTHORITY_UNAVAILABLE")


def _fastlane_public_value(value: object) -> object:
    """Remove compiler-only null sentinels before the no-null MCP envelope."""

    if type(value) is dict:
        return {
            key: _fastlane_public_value(item)
            for key, item in value.items()
            if item is not None
        }
    if type(value) is list:
        return [_fastlane_public_value(item) for item in value if item is not None]
    return value


@mcp.tool(annotations=_tool_annotations("relay_start"))
def relay_start(request: RelayStartRequest) -> dict[str, object]:
    """Start Relay only when the private capability broker is available."""

    try:
        payload = _relay_start_request(request)
    except _RequestError as error:
        return _failure(error.code)
    return _invoke(
        "relay_start",
        read_only=False,
        operation=lambda uow: _relay_runtime(uow).start(payload),
        invalid_code="RELAY_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("relay_status"))
def relay_status(workflow_id: str) -> dict[str, object]:
    """Read Relay status without requiring a broker or proof provider."""

    return _invoke(
        "relay_status",
        read_only=True,
        operation=lambda uow: uow.relay.status(workflow_id),
        invalid_code="RELAY_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("relay_handoff"))
def relay_handoff(request: RelayHandoffRequest) -> dict[str, object]:
    """Apply one exact worker lifecycle action through the Relay runtime owner."""

    try:
        payload = _relay_handoff_request(request)
    except _RequestError as error:
        return _failure(error.code)
    return _invoke(
        "relay_handoff",
        read_only=False,
        operation=lambda uow: _relay_service(_relay_runtime(uow)).handoff(payload),
        invalid_code="RELAY_REQUEST_INVALID",
    )


@mcp.tool(annotations=_tool_annotations("relay_integrate"))
def relay_integrate(request: RelayIntegrationRequest) -> dict[str, object]:
    """Apply a proof-bound Relay integration action with no head/commit inputs."""

    try:
        payload = _relay_integrate_request(request)
    except _RequestError as error:
        return _failure(error.code)
    return _invoke(
        "relay_integrate",
        read_only=False,
        operation=lambda uow: _relay_service(_relay_runtime(uow)).integrate(payload),
        invalid_code="RELAY_REQUEST_INVALID",
    )


if __name__ == "__main__":
    mcp.run()
