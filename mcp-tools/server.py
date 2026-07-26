"""2718lab-tools —— 2718lab-devkit 捆绑的 MCP 服务器(真·程序部分)。

把 devkit 里的几个校验脚本包装成 Claude 可直接调用的 MCP 工具:发布自检、
AstrBot 插件校验、MCP server 校验、Python 工程骨架校验。

用法:由插件的 .mcp.json 以 stdio 方式拉起(python server.py)。
依赖:官方 MCP Python SDK —— `pip install mcp`(或 `uv pip install mcp`)。

框架保真:本文件遵循 mcp-server-dev skill 的 (A) 包规范 ——
`from mcp.server.fastmcp import FastMCP`、装饰器带括号 `@mcp.tool()`、
工具 schema 由函数类型注解生成(不是 AstrBot 的 docstring Args 约定),
stdio 模式下不得写标准输出(stdout 是协议通道),日志走 stderr。
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping

from bugkiller.policy import route_case
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from orchestrator.adapters import detect_repository
from orchestrator.approval_service import ApprovalEffectService
from orchestrator.approvals import ApprovalError, ApprovalManifest
from orchestrator.models import Task, Workflow, WorkflowKind
from orchestrator.service import OrchestratorService, ServiceError
from orchestrator.store import SQLiteStore
from project_index import IndexError as ProjectIndexError
from project_index import ProjectIndexService
from project_index.checkpoints import CheckpointService

# server.py 在 <plugin_root>/mcp-tools/ 下;插件根 = 上一级,skills 脚本在其下
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILLS = PLUGIN_ROOT / "skills"

mcp = FastMCP(name="2718lab-tools")
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_DESTRUCTIVE_JOURNAL_MUTATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
_IDEMPOTENT_MUTATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


class RuntimeContractError(RuntimeError):
    """Stable runtime boundary failure returned through JSON tool envelopes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resolve_data_root() -> Path:
    """Resolve the durable data root without falling back into plugin source."""
    if os.environ.get("DEVKIT_HOME"):
        candidate = Path(os.environ["DEVKIT_HOME"])
    elif os.environ.get("BUGKILLER_HOME"):
        candidate = Path(os.environ["BUGKILLER_HOME"])
    elif os.environ.get("PLUGIN_DATA"):
        candidate = Path(os.environ["PLUGIN_DATA"])
    elif os.environ.get("CODEX_HOME"):
        candidate = Path(os.environ["CODEX_HOME"]) / "2718lab-devkit"
    else:
        candidate = Path.home() / ".codex" / "data" / "2718lab-devkit"

    resolved = candidate.expanduser().resolve()
    plugin_root = PLUGIN_ROOT.resolve()
    if resolved == plugin_root or plugin_root in resolved.parents:
        raise RuntimeContractError(
            "DATA_ROOT_INVALID", "plugin source cannot be used as data storage"
        )
    folded_parts = tuple(part.casefold() for part in resolved.parts)
    if any(
        folded_parts[index : index + 2] == ("plugins", "cache")
        for index in range(max(0, len(folded_parts) - 1))
    ):
        raise RuntimeContractError(
            "DATA_ROOT_INVALID", "plugin cache cannot be used as data storage"
        )
    return resolved


def _prepare_data_root() -> Path:
    root = _resolve_data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeContractError(
            "DATA_ROOT_UNAVAILABLE", "durable data root is unavailable"
        ) from error
    if not root.is_dir():
        raise RuntimeContractError(
            "DATA_ROOT_INVALID", "durable data root is not a directory"
        )
    return root


@contextmanager
def _orchestrator_runtime(
    index_service: ProjectIndexService | None = None,
) -> Iterator[tuple[SQLiteStore, OrchestratorService]]:
    root = _prepare_data_root()
    store = SQLiteStore(root / "orchestrator.sqlite3")
    try:
        yield (
            store,
            OrchestratorService(
                store, index_service=index_service, evidence_root="evidence"
            ),
        )
    finally:
        store.close()


@contextmanager
def _project_index_runtime() -> Iterator[tuple[ProjectIndexService, CheckpointService]]:
    root = _prepare_data_root()
    database = root / "project-index.sqlite3"
    index = ProjectIndexService(database)
    checkpoints = CheckpointService(database, root / "checkpoint-cas", index)
    try:
        yield index, checkpoints
    finally:
        checkpoints.close()
        index.close()


@contextmanager
def _approval_runtime() -> Iterator[ApprovalEffectService]:
    service = ApprovalEffectService(_prepare_data_root() / "approvals.sqlite3")
    try:
        yield service
    finally:
        service.journal.close()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key.value if isinstance(key, Enum) else key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _safe_call(operation: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": _json_safe(operation())}
    except RuntimeContractError as error:
        code = error.code
    except ServiceError as error:
        code = error.code
    except ProjectIndexError as error:
        code = error.code
    except ApprovalError:
        code = "APPROVAL_REJECTED"
    except KeyError:
        code = "NOT_FOUND"
    except (TypeError, ValueError):
        code = "INVALID_REQUEST"
    except sqlite3.Error:
        code = "STORAGE_ERROR"
    except OSError:
        code = "RUNTIME_IO_ERROR"
    except Exception:
        code = "INTERNAL_ERROR"
    return {"ok": False, "error": {"code": code, "message": "request rejected"}}


def _manifest(payload: Mapping[str, Any]) -> ApprovalManifest:
    return ApprovalManifest(**dict(payload))


def _lease_tuple_complete(
    workflow_id: str,
    task_id: str,
    owner: str,
    lease_epoch: int,
) -> bool:
    supplied = (bool(workflow_id), bool(task_id), bool(owner), lease_epoch > 0)
    if any(supplied) and not all(supplied):
        raise RuntimeContractError("INVALID_REQUEST", "task lease tuple is incomplete")
    return all(supplied)


def _content_hash(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@mcp.tool(annotations=_IDEMPOTENT_MUTATION)
def project_index_sync(
    workspace: str,
    include_paths: list[str] | None = None,
    workflow_id: str = "",
    task_id: str = "",
    owner: str = "",
    lease_epoch: int = 0,
    bind_as: str = "",
) -> dict[str, Any]:
    """Synchronize a deterministic project snapshot and optionally bind task output."""

    def operation() -> Any:
        lease_bound = _lease_tuple_complete(workflow_id, task_id, owner, lease_epoch)
        if bind_as not in {"", "output"}:
            raise RuntimeContractError(
                "INVALID_REQUEST", "unsupported snapshot binding"
            )
        if bind_as == "output" and not lease_bound:
            raise RuntimeContractError(
                "INVALID_REQUEST", "output binding requires a task lease"
            )

        with _project_index_runtime() as (index, _):
            snapshot = index.sync(workspace, include_paths)
            if bind_as != "output":
                return snapshot
            with _orchestrator_runtime(index) as (store, service):
                service.strict_ownership(
                    workflow_id,
                    task_id,
                    owner=owner,
                    epoch=lease_epoch,
                )
                binding = store.get_index_binding(task_id)
                if binding is None:
                    raise RuntimeContractError(
                        "INDEX_UNAVAILABLE", "task has no strict index binding"
                    )
                indexed_diff = index.diff(
                    binding.input_snapshot_id, snapshot.snapshot_id
                )
                service.record_output_snapshot(
                    task_id,
                    owner=owner,
                    epoch=lease_epoch,
                    snapshot_id=snapshot.snapshot_id,
                    diff_hash=_content_hash(indexed_diff),
                )
            return snapshot

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def project_index_status(
    workspace: str,
    snapshot_id: str | None = None,
    required_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Return deterministic index freshness and coverage metadata."""

    def operation() -> Any:
        with _project_index_runtime() as (index, _):
            return index.status(workspace, snapshot_id, required_paths)

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def project_index_query(
    workspace: str,
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
    workflow_id: str = "",
    task_id: str = "",
    owner: str = "",
    lease_epoch: int = 0,
) -> dict[str, Any]:
    """Query bounded nodes: lexical matches text, graph follows all edges, and impact follows incoming dependents."""

    def operation() -> Any:
        lease_bound = _lease_tuple_complete(workflow_id, task_id, owner, lease_epoch)
        with _project_index_runtime() as (index, _):
            result = index.query(
                workspace,
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
            if lease_bound:
                receipt = index.get_query_receipt(result.trace_id)
                with _orchestrator_runtime(index) as (_, service):
                    service.record_index_query(
                        workflow_id,
                        task_id,
                        owner=owner,
                        epoch=lease_epoch,
                        trace_id=result.trace_id,
                        snapshot_id=result.snapshot_id,
                        miss_escape_used=receipt.miss_escape_used,
                    )
            return result

    return _safe_call(operation)


@mcp.tool(annotations=_IDEMPOTENT_MUTATION)
def worktree_checkpoint_create(
    workflow_id: str,
    task_id: str,
    owner: str,
    lease_epoch: int,
    snapshot_id: str,
) -> dict[str, Any]:
    """Create and record a checkpoint for the strict task's stored write scope."""

    def operation() -> Any:
        with _project_index_runtime() as (index, checkpoints):
            with _orchestrator_runtime(index) as (_, service):
                ownership = service.strict_ownership(
                    workflow_id,
                    task_id,
                    owner=owner,
                    epoch=lease_epoch,
                )
                checkpoint = checkpoints.create(ownership, snapshot_id)
                service.record_checkpoint(
                    task_id,
                    owner=owner,
                    epoch=lease_epoch,
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                return checkpoint

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def worktree_checkpoint_status(checkpoint_id: str) -> dict[str, Any]:
    """Return verified checkpoint metadata without exposing stored file bodies."""

    def operation() -> Any:
        with _project_index_runtime() as (_, checkpoints):
            return checkpoints.status(checkpoint_id)

    return _safe_call(operation)


@mcp.tool(annotations=_DESTRUCTIVE_JOURNAL_MUTATION)
def worktree_checkpoint_restore(
    workflow_id: str,
    task_id: str,
    owner: str,
    lease_epoch: int,
    checkpoint_id: str,
    expected_current_snapshot_id: str,
) -> dict[str, Any]:
    """Restore a task-owned checkpoint after lease and snapshot compare-and-swap checks."""

    def operation() -> Any:
        with _project_index_runtime() as (index, checkpoints):
            with _orchestrator_runtime(index) as (_, service):
                ownership = service.strict_ownership(
                    workflow_id,
                    task_id,
                    owner=owner,
                    epoch=lease_epoch,
                )
                return checkpoints.restore(
                    ownership,
                    checkpoint_id,
                    expected_current_snapshot_id,
                )

    return _safe_call(operation)


@mcp.tool()
def workflow_create(
    workflow_id: str,
    kind: str,
    title: str,
    product_summary: str,
    policy_version: str = "",
) -> dict[str, Any]:
    """Create one durable linear or DAG workflow."""

    def operation() -> Workflow:
        now = datetime.now(UTC).isoformat()
        workflow = Workflow(
            workflow_id,
            WorkflowKind(kind),
            title,
            product_summary,
            policy_version=policy_version,
            created_at=now,
            updated_at=now,
        )
        with _orchestrator_runtime() as (_, service):
            return service.create_workflow(workflow)

    return _safe_call(operation)


@mcp.tool()
def workflow_register_task(
    workflow_id: str,
    task_id: str,
    title: str,
    owner_role: str,
    card: str,
    dependencies: list[str] | None = None,
    write_scope: list[str] | None = None,
    direct_contract_hashes: list[str] | None = None,
    required_evidence: list[str] | None = None,
    input_hash: str = "",
    strict_index: bool = False,
    workspace_root: str = "",
    input_snapshot_id: str = "",
    task_node_ids: list[str] | None = None,
    contract_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Register one task and its task-scoped durable context."""

    def operation() -> Task:
        task = Task(
            task_id,
            workflow_id,
            title,
            owner_role,
            dependencies=tuple(dependencies or ()),
            write_scope=tuple(write_scope or ()),
        )
        if strict_index:
            with (
                _project_index_runtime() as (index, _),
                _orchestrator_runtime(index) as (_, service),
            ):
                return service.register_task(
                    task,
                    card=card,
                    direct_contract_hashes=tuple(direct_contract_hashes or ()),
                    required_evidence=tuple(required_evidence or ()),
                    input_hash=input_hash,
                    strict_index=True,
                    workspace_root=workspace_root,
                    input_snapshot_id=input_snapshot_id,
                    task_node_ids=tuple(task_node_ids or ()),
                    contract_node_ids=tuple(contract_node_ids or ()),
                )
        with _orchestrator_runtime() as (_, service):
            return service.register_task(
                task,
                card=card,
                direct_contract_hashes=tuple(direct_contract_hashes or ()),
                required_evidence=tuple(required_evidence or ()),
                input_hash=input_hash,
            )

    return _safe_call(operation)


@mcp.tool()
def workflow_ready(workflow_id: str) -> dict[str, Any]:
    """Promote and return the next durable ready wave."""

    def operation() -> tuple[Task, ...]:
        with _orchestrator_runtime() as (_, service):
            return service.ready_wave(workflow_id)

    return _safe_call(operation)


@mcp.tool()
def workflow_claim(
    task_id: str,
    owner: str,
    expires_at: str,
    host_target: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Claim a ready task and optionally bind its canonical Codex target."""

    def operation() -> Any:
        with (
            _project_index_runtime() as (index, _),
            _orchestrator_runtime(index) as (_, service),
        ):
            return service.claim_task(
                task_id,
                owner,
                expires_at=expires_at,
                host_target=host_target,
                now=now,
            )

    return _safe_call(operation)


@mcp.tool()
def workflow_endpoint_bind(
    workflow_id: str,
    task_id: str,
    owner: str,
    lease_epoch: int,
    host_target: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Bind or replace the current lease's canonical Codex collaboration target."""

    def operation() -> Any:
        with _orchestrator_runtime() as (_, service):
            return service.bind_endpoint(
                workflow_id,
                task_id,
                owner=owner,
                epoch=lease_epoch,
                host_target=host_target,
                now=now,
            )

    return _safe_call(operation)


@mcp.tool()
def workflow_complete(
    task_id: str,
    expected_version: int,
    owner: str,
    lease_epoch: int,
    result_hash: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    """Complete a task using task version and lease compare-and-swap."""

    def operation() -> Task:
        with (
            _project_index_runtime() as (index, _),
            _orchestrator_runtime(index) as (_, service),
        ):
            return service.complete_task(
                task_id,
                expected_version=expected_version,
                owner=owner,
                epoch=lease_epoch,
                result_hash=result_hash,
                now=now,
            )

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def workflow_status(workflow_id: str) -> dict[str, Any]:
    """Return the workflow record and all task state summaries."""

    def operation() -> dict[str, Any]:
        with _orchestrator_runtime() as (_, service):
            return service.status(workflow_id)

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def workflow_context(
    workflow_id: str,
    role: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Return a product, coordinator, or single-agent scoped projection."""

    def operation() -> dict[str, Any]:
        with _orchestrator_runtime() as (_, service):
            return service.context(workflow_id, role=role, task_id=task_id)

    return _safe_call(operation)


@mcp.tool()
def workflow_artifact_register(
    workflow_id: str,
    task_id: str,
    owner: str,
    lease_epoch: int,
    kind: str,
    artifact_hash: str,
    safe_path: str,
    size: int,
    redaction_version: str,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Register metadata for a task-owned redacted artifact; accepts no body."""

    def operation() -> Any:
        if snapshot_id is not None:
            with (
                _project_index_runtime() as (index, _),
                _orchestrator_runtime(index) as (_, service),
            ):
                return service.register_artifact(
                    workflow_id,
                    task_id,
                    owner=owner,
                    epoch=lease_epoch,
                    kind=kind,
                    content_hash=artifact_hash,
                    safe_path=safe_path,
                    size=size,
                    redaction_version=redaction_version,
                    snapshot_id=snapshot_id,
                )
        with _orchestrator_runtime() as (_, service):
            return service.register_artifact(
                workflow_id,
                task_id,
                owner=owner,
                epoch=lease_epoch,
                kind=kind,
                content_hash=artifact_hash,
                safe_path=safe_path,
                size=size,
                redaction_version=redaction_version,
            )

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def workflow_peers(workflow_id: str, task_id: str) -> dict[str, Any]:
    """Return only authorized dependency or common-contract peers."""

    def operation() -> Any:
        with _orchestrator_runtime() as (_, service):
            return service.peers(workflow_id, task_id)

    return _safe_call(operation)


@mcp.tool()
def workflow_message_send(
    workflow_id: str,
    sender_task_id: str,
    recipient_task_id: str,
    owner: str,
    lease_epoch: int,
    correlation_id: str,
    artifact_hash: str,
    metadata: dict[str, str],
    ttl_seconds: int,
) -> dict[str, Any]:
    """Enqueue an artifact reference and return only minimal delivery data."""

    def operation() -> Any:
        with _orchestrator_runtime() as (_, service):
            return service.send_message(
                workflow_id,
                sender_task_id,
                recipient_task_id,
                owner=owner,
                epoch=lease_epoch,
                correlation_id=correlation_id,
                artifact_hash=artifact_hash,
                metadata=metadata,
                ttl_seconds=ttl_seconds,
            )

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def workflow_inbox(
    workflow_id: str,
    recipient_task_id: str,
    owner: str,
    lease_epoch: int,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Read only the current recipient lease's durable mailbox."""

    def operation() -> Any:
        with _orchestrator_runtime() as (_, service):
            return service.inbox(
                workflow_id,
                recipient_task_id,
                owner=owner,
                epoch=lease_epoch,
                cursor=cursor,
                limit=limit,
            )

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def workflow_artifact_resolve(
    workflow_id: str,
    recipient_task_id: str,
    owner: str,
    lease_epoch: int,
    delivery_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Resolve one current recipient delivery to its registered artifact metadata."""

    def operation() -> Any:
        with _orchestrator_runtime() as (_, service):
            return service.resolve_artifact(
                workflow_id,
                recipient_task_id,
                owner=owner,
                epoch=lease_epoch,
                delivery_id=delivery_id,
                now=now,
            )

    return _safe_call(operation)


@mcp.tool()
def workflow_message_ack(
    workflow_id: str,
    recipient_task_id: str,
    owner: str,
    lease_epoch: int,
    delivery_id: str,
) -> dict[str, Any]:
    """Acknowledge one recipient-owned durable mailbox delivery."""

    def operation() -> Any:
        with _orchestrator_runtime() as (_, service):
            return service.ack_message(
                workflow_id,
                recipient_task_id,
                delivery_id,
                owner=owner,
                epoch=lease_epoch,
            )

    return _safe_call(operation)


@mcp.tool()
def workflow_cancel(
    workflow_id: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Cancel a workflow and every nonterminal task atomically."""

    def operation() -> Workflow:
        with _orchestrator_runtime() as (_, service):
            return service.cancel_workflow(
                workflow_id, expected_version=expected_version
            )

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def bugkiller_route(
    risk_triggers: list[str],
    luna_available: bool = True,
    terra_available: bool = True,
    approved_escalation: bool = False,
) -> dict[str, Any]:
    """Return the pure Bugkiller risk route without executing any command."""
    return _safe_call(
        lambda: route_case(
            risk_triggers,
            luna_available=luna_available,
            terra_available=terra_available,
            approved_escalation=approved_escalation,
        )
    )


def _detect_adapters(repository: str) -> dict[str, Any]:
    def operation() -> Any:
        task_temp = _prepare_data_root() / "task-temp"
        task_temp.mkdir(parents=True, exist_ok=True)
        return detect_repository(repository, task_temp_root=task_temp)

    return _safe_call(operation)


@mcp.tool(annotations=_READ_ONLY)
def workflow_detect_adapters(repository: str) -> dict[str, Any]:
    """Return shared structured verification command specs without executing them."""
    return _detect_adapters(repository)


@mcp.tool(annotations=_READ_ONLY)
def bugkiller_detect_adapters(repository: str) -> dict[str, Any]:
    """Compatibility alias for workflow_detect_adapters."""
    return _detect_adapters(repository)


def _approval_prepare(manifest: dict[str, Any], expires_at: str) -> dict[str, Any]:
    def operation() -> Any:
        with _approval_runtime() as service:
            return service.prepare(
                _manifest(manifest), expires_at=datetime.fromisoformat(expires_at)
            )

    return _safe_call(operation)


def _approval_grant(approval_id: str) -> dict[str, Any]:
    def operation() -> Any:
        with _approval_runtime() as service:
            return service.grant(approval_id)

    return _safe_call(operation)


def _approval_deny(approval_id: str) -> dict[str, Any]:
    def operation() -> Any:
        with _approval_runtime() as service:
            return service.deny(approval_id)

    return _safe_call(operation)


def _approval_claim(approval_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    def operation() -> Any:
        with _approval_runtime() as service:
            return service.claim(approval_id, _manifest(manifest))

    return _safe_call(operation)


@mcp.tool()
def workflow_approval_prepare(
    manifest: dict[str, Any], expires_at: str
) -> dict[str, Any]:
    """Prepare a shared immutable approval record; performs no external effect."""
    return _approval_prepare(manifest, expires_at)


@mcp.tool(annotations=_DESTRUCTIVE_JOURNAL_MUTATION)
def workflow_approval_grant(approval_id: str) -> dict[str, Any]:
    """Record approval only after the host obtains explicit user confirmation."""
    return _approval_grant(approval_id)


@mcp.tool()
def workflow_approval_deny(approval_id: str) -> dict[str, Any]:
    """Record denial for a prepared workflow manifest without consuming a grant."""
    return _approval_deny(approval_id)


@mcp.tool(annotations=_DESTRUCTIVE_JOURNAL_MUTATION)
def workflow_approval_claim(
    approval_id: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Claim a single-use workflow grant; never executes Git or network work."""
    return _approval_claim(approval_id, manifest)


@mcp.tool()
def bugkiller_approval_prepare(
    manifest: dict[str, Any], expires_at: str
) -> dict[str, Any]:
    """Compatibility alias for workflow_approval_prepare."""
    return _approval_prepare(manifest, expires_at)


@mcp.tool(annotations=_DESTRUCTIVE_JOURNAL_MUTATION)
def bugkiller_approval_grant(approval_id: str) -> dict[str, Any]:
    """Compatibility alias for workflow_approval_grant after user confirmation."""
    return _approval_grant(approval_id)


@mcp.tool()
def bugkiller_approval_deny(approval_id: str) -> dict[str, Any]:
    """Compatibility alias for workflow_approval_deny."""
    return _approval_deny(approval_id)


@mcp.tool(annotations=_DESTRUCTIVE_JOURNAL_MUTATION)
def bugkiller_approval_claim(
    approval_id: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Compatibility alias for workflow_approval_claim."""
    return _approval_claim(approval_id, manifest)


def _run_script(script: Path, target: str) -> str:
    if not script.exists():
        return f"[2718lab-tools] 找不到校验脚本:{script}"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), target],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return f"[2718lab-tools] 校验超时(>120s):{script.name}"
    except OSError as e:
        return f"[2718lab-tools] 无法运行 {script.name}:{e}"
    body = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return body or f"[2718lab-tools] {script.name} 无输出(exit={proc.returncode})"


@mcp.tool(annotations=_READ_ONLY)
def check_release(repo_dir: str) -> str:
    """发布前自检:对 AstrBot 插件 / 工具仓库运行 oss-repo-ops 的 check_release.py 并返回报告(version 2 段号浮点数陷阱、repo .git、astrbot_version 引号、16MB 体积、必备文件等)。repo_dir 为仓库目录路径。"""
    return _run_script(
        SKILLS / "oss-repo-ops" / "scripts" / "check_release.py", repo_dir
    )


@mcp.tool(annotations=_READ_ONLY)
def validate_astrbot_plugin(plugin_dir: str) -> str:
    """校验 AstrBot 插件:运行 astrbot-plugin-dev 的 validate_plugin.py 并返回结果。plugin_dir 为插件目录路径。"""
    return _run_script(
        SKILLS / "astrbot-plugin-dev" / "scripts" / "validate_plugin.py", plugin_dir
    )


@mcp.tool(annotations=_READ_ONLY)
def validate_mcp_server(target: str) -> str:
    """校验 MCP 服务器代码:运行 mcp-server-dev 的 validate_mcp_server.py(混包检测、装饰器括号、transport 白名单等)。target 为 server 目录或文件路径。"""
    return _run_script(
        SKILLS / "mcp-server-dev" / "scripts" / "validate_mcp_server.py", target
    )


@mcp.tool(annotations=_READ_ONLY)
def check_python_project(project_dir: str) -> str:
    """校验 Python 工程骨架:运行 python-engineering 的 validate_project.py(pyproject / 版本号 / 布局等)。project_dir 为项目目录路径。"""
    return _run_script(
        SKILLS / "python-engineering" / "scripts" / "validate_project.py", project_dir
    )


if __name__ == "__main__":
    # 由 .mcp.json 以 stdio 方式拉起;本地调试可 `uv run mcp dev server.py`
    mcp.run()
