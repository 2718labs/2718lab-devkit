"""The locked 17-tool stdio surface for the 2718lab DevKit runtime."""

from __future__ import annotations

import atexit
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from devkit_fastlane import compile_fast_lane
from devkit_relay.compiler import compile_plan
from devkit_relay.service import RelayService
from devkit_runtime.composition import RuntimeRoot
from devkit_runtime.config import RuntimeConfig, RuntimeConfigError
from devkit_runtime.relay_runtime import RelayRuntime, RelayRuntimeError
from devkit_runtime.tool_result import (
    TOOL_ANNOTATIONS,
    ResultContractError,
    envelope_failure,
    envelope_success,
    result_from_exception,
)
from devkit_runtime.uow import RuntimeUnitOfWork
from project_index.checkpoints import WorkspaceOwnership

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
mcp = FastMCP(name="2718lab-devkit")


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


class _RequestError(ValueError):
    """A bounded, public request failure raised before a persistent operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_TModel = TypeVar("_TModel", bound=BaseModel)
_RUNTIME_ROOT: RuntimeRoot | None = None
_TASK_LEASE_AUTHORITY: _TaskLeaseAuthority | None = None


def _tool_annotations(name: str) -> ToolAnnotations:
    read_only, destructive, idempotent, open_world = TOOL_ANNOTATIONS[name]
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _default_runtime_root() -> RuntimeRoot:
    """Create the pure process root; bootstrap is deliberately never implicit."""

    return RuntimeRoot(RuntimeConfig.load(protected_roots=(PLUGIN_ROOT,)))


def _runtime_root() -> RuntimeRoot:
    global _RUNTIME_ROOT
    if _RUNTIME_ROOT is None:
        _RUNTIME_ROOT = _default_runtime_root()
    return _RUNTIME_ROOT


def _install_runtime_root_for_host(root: RuntimeRoot) -> None:
    """Private embedding seam for a host-injected broker or proof resolver."""

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


atexit.register(_shutdown_runtime)


def _failure(code: str) -> dict[str, object]:
    return envelope_failure(code)


def _runtime_failure(
    error: RuntimeConfigError | RelayRuntimeError,
) -> dict[str, object]:
    if isinstance(error, RuntimeConfigError):
        if error.code in {
            "DATA_ROOT_INVALID",
            "DATA_ROOT_UNAVAILABLE",
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
) -> dict[str, object]:
    """Run one operation inside a fresh UoW and project its exact result."""

    try:
        with _runtime_root().open_uow(read_only=read_only) as uow:
            return uow.tool_results.project(tool_name, operation(uow))
    except _RequestError as error:
        return _failure(error.code)
    except (RuntimeConfigError, RelayRuntimeError) as error:
        return _runtime_failure(error)
    except ResultContractError:
        return _failure("INTERNAL_ERROR")
    except Exception as error:
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
    relay = uow.relay
    if not isinstance(relay, RelayRuntime):
        raise _RequestError("RELAY_REQUEST_INVALID")
    return relay


def _relay_service(runtime: RelayRuntime) -> RelayService:
    """Use the lifecycle service already owned by the typed Relay runtime."""

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
    )


@mcp.tool(annotations=_tool_annotations("project_index_sync"))
def project_index_sync(
    workspace_id: str,
    include_paths: list[str] | None = None,
    task_lease: TaskLeaseRef | None = None,
    bind_as: Literal["output"] | None = None,
) -> dict[str, object]:
    """Synchronize one registered workspace without accepting a second root."""

    try:
        lease = _task_lease(task_lease)
        if (lease is None) != (bind_as is None):
            raise _RequestError("INVALID_REQUEST")
    except _RequestError as error:
        return _failure(error.code)

    def operation(uow: RuntimeUnitOfWork) -> object:
        authority = _require_lease_authority() if lease is not None else None
        snapshot = uow.project_checkpoint.project_index.sync(
            workspace_id, include_paths
        )
        if authority is not None:
            if lease is None:
                raise _RequestError("INVALID_REQUEST")
            authority.bind_output_snapshot(
                lease, workspace_id=workspace_id, snapshot=snapshot
            )
        return snapshot

    return _invoke("project_index_sync", read_only=False, operation=operation)


@mcp.tool(annotations=_tool_annotations("project_index_status"))
def project_index_status(
    workspace_id: str,
    snapshot_id: str | None = None,
    required_paths: list[str] | None = None,
) -> dict[str, object]:
    """Read verified index status through the opaque workspace boundary."""

    return _invoke(
        "project_index_status",
        read_only=True,
        operation=lambda uow: uow.project_checkpoint.project_index.status(
            workspace_id, snapshot_id, required_paths
        ),
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
        )
        if authority is not None:
            if lease is None:
                raise _RequestError("INVALID_REQUEST")
            authority.record_query_receipt(
                lease, workspace_id=workspace_id, result=result
            )
        return result

    return _invoke("project_index_query", read_only=False, operation=operation)


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
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "ultra",
    enable: bool = False,
) -> dict[str, object]:
    """Compile inert Fast Lane descriptors without receiving host-private evidence.

    The host remains responsible for quota attestations, worktree/lease fencing,
    model dispatch, terminal receipts, and execution.  This MCP boundary only
    validates and renders the deterministic local scheduling plan.
    """

    if type(request) is not dict or type(enable) is not bool:
        return _failure("FASTLANE_REQUEST_INVALID")
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
