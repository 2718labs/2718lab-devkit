"""Closed, path-neutral result envelopes for the public MCP adapter.

This module deliberately contains no server registration or domain invocation.
Domain objects enter through explicit projectors; arbitrary Python values never
reach the public result boundary.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Final, TypeVar, cast

from devkit_atlas.models import (
    AcceptanceProjection,
    AtlasEdge,
    AtlasError,
    AtlasNode,
    AtlasStatus,
    GraphQueryResult,
    ImplementationPacket,
    PreparationResult,
    RenderResult,
    TestSpec,
)
from devkit_relay.compiler import RelayPlanError
from devkit_relay.service import RelayError
from devkit_relay.store import RelayStoreError
from project_index.checkpoints import Checkpoint, RestoreResult
from project_index.models import (
    CoverageGap,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexSnapshot,
    IndexState,
    IndexStatus,
    QueryResult,
    SourceWindow,
)

RESULT_SCHEMA: Final = "2718lab-devkit/tool-result-v1"
MAX_RESULT_BYTES: Final = 524_288
MAX_STRING_BYTES: Final = 65_536
MAX_LIST_ITEMS: Final = 512
MAX_PUBLIC_DEPTH: Final = 32

SUCCESS_KEYS: Final = frozenset({"schema", "ok", "data"})
FAILURE_KEYS: Final = frozenset({"schema", "ok", "error"})

TOOL_ANNOTATIONS: Final[dict[str, tuple[bool, bool, bool, bool]]] = {
    "project_index_register": (False, False, True, False),
    "project_index_sync": (False, False, True, False),
    "project_index_status": (True, False, True, False),
    "project_index_query": (False, False, True, False),
    "worktree_checkpoint_create": (False, False, True, False),
    "worktree_checkpoint_status": (True, False, True, False),
    "worktree_checkpoint_restore": (False, True, False, False),
    "atlas_query": (True, False, True, False),
    "atlas_prepare": (False, False, True, False),
    "atlas_render": (True, False, True, False),
    "atlas_accept": (False, False, True, False),
    "relay_compile": (True, False, True, False),
    "relay_start": (False, False, True, False),
    "relay_status": (True, False, True, False),
    "relay_handoff": (False, False, False, False),
    "relay_integrate": (False, True, False, False),
}
TOOL_ANNOTATION_TABLE = TOOL_ANNOTATIONS


class ResultContractError(ValueError):
    """Raised when a value cannot cross the public result boundary."""


class RuntimeContractError(RuntimeError):
    """Stable runtime configuration error accepted by the result adapter."""

    def __init__(self, code: str, message: str = "runtime request rejected") -> None:
        self.code = code
        super().__init__(message)


ToolResultError = ResultContractError

_JSONValue = (
    None | bool | int | float | str | list["_JSONValue"] | dict[str, "_JSONValue"]
)
_Projector = Callable[[object], dict[str, object]]
_T = TypeVar("_T")
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_RAW_SECRET = re.compile(
    r"(?ix)(?:\b(?:sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{8,})\b|"
    r"-----begin\s+[a-z\s]*private\s+key-----|"
    r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"private[_-]?key|password|secret)\s*[:=]\s*\S+)"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

_FORBIDDEN_KEYS = frozenset(
    {
        "bearer",
        "body",
        "capability",
        "credential",
        "data_root",
        "full_proof",
        "full_receipt",
        "host_path",
        "password",
        "proof",
        "proof_body",
        "receipt",
        "receipt_body",
        "secret",
        "source",
        "source_body",
        "source_text",
        "stderr",
        "stdout",
        "text",
        "token",
        "traceback",
        "workspace_root",
        "root_path",
    }
)

_GENERAL_ERROR_CODES = frozenset(
    {
        "DATA_ROOT_INVALID",
        "DATA_ROOT_UNAVAILABLE",
        "STORAGE_ERROR",
        "INTERNAL_ERROR",
        "INVALID_REQUEST",
        "NOT_FOUND",
    }
)
_INDEX_ERROR_CODES = frozenset(
    {
        "WORKSPACE_UNREGISTERED",
        "WORKSPACE_REBIND",
        "UNSAFE_WORKSPACE",
        "HISTORICAL_UNVERIFIED",
        "INDEX_UNAVAILABLE",
        "INDEX_STALE",
        "INDEX_CORRUPT",
        "INVALID_QUERY",
        "NOT_FOUND",
        "WORKTREE_UNOWNED",
        "SCOPE_ESCAPE",
        "UNSAFE_PATH_TYPE",
        "ROLLBACK_DRIFT",
    }
)
_ATLAS_ERROR_CODES = frozenset(
    {
        "ATLAS_REQUEST_INVALID",
        "ATLAS_SNAPSHOT_STALE",
        "ATLAS_PACKET_NOT_FOUND",
        "ATLAS_PACKET_STALE",
        "ATLAS_EVIDENCE_UNAVAILABLE",
        "ATLAS_EVIDENCE_CONFLICT",
        "ATLAS_MIGRATION_CONFLICT",
    }
)
_RELAY_ERROR_CODES = frozenset(
    {
        "RELAY_REQUEST_INVALID",
        "RELAY_PLAN_INVALID",
        "RELAY_PLAN_STALE",
        "RELAY_STATE_STALE",
        "RELAY_IDEMPOTENCY_CONFLICT",
        "RELAY_LEASE_CONFLICT",
        "RELAY_CANDIDATE_STALE_BASE",
        "RELAY_CANDIDATE_CONFLICT",
        "RELAY_MIGRATION_CONFLICT",
        "RELAY_CAPABILITY_INVALID",
        "RELAY_CAPABILITY_EXPIRED",
        "RELAY_CAPABILITY_SCOPE",
        "RELAY_CAPABILITY_BROKER_UNAVAILABLE",
        "RELAY_INTEGRATION_PROOF_REQUIRED",
        "RELAY_INTEGRATION_PROOF_INVALID",
        "RELAY_INTEGRATION_PROOF_UNREGISTERED",
        "RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE",
        "RELAY_INTEGRATION_PROOF_BUSY",
        "RELAY_INTEGRATION_BINDING_MISMATCH",
        "RELAY_INTEGRATION_OBJECT_INVALID",
        "RELAY_INTEGRATION_ANCESTRY_INVALID",
        "RELAY_INTEGRATION_SCOPE_MISMATCH",
        "RELAY_INTEGRATION_TREE_MISMATCH",
        "RELAY_INTEGRATION_HEAD_STALE",
        "RELAY_INTEGRATION_PROOF_REPLAY",
        "RELAY_INTEGRATION_PROOF_CORRUPT",
        "RELAY_SCHEMA_INCOMPATIBLE",
        "RELAY_STORAGE_ERROR",
        "RELAY_CANDIDATE_INVALID",
        "RELAY_STALE_BASE",
    }
)
_PUBLIC_ERROR_CODES = (
    _GENERAL_ERROR_CODES | _INDEX_ERROR_CODES | _ATLAS_ERROR_CODES | _RELAY_ERROR_CODES
)


def _fail(detail: str) -> ResultContractError:
    return ResultContractError(detail)


def _validate_budget(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_RESULT_BYTES:
        raise _fail("invalid result byte budget")
    return value


def _forbidden_key(key: str) -> bool:
    normalized = key.casefold()
    return (
        normalized in _FORBIDDEN_KEYS
        or normalized.endswith("_bearer")
        or normalized.endswith("_capability")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
    )


def _safe_string(
    value: object,
    *,
    key: str | None = None,
    allow_empty: bool = True,
    allow_multiline: bool = False,
    maximum: int = MAX_STRING_BYTES,
) -> str:
    if type(value) is not str:
        raise _fail("unsupported public value")
    if not allow_empty and not value:
        raise _fail("empty public value")
    if len(value.encode("utf-8")) > maximum:
        raise _fail("oversize public value")
    if not allow_multiline and any(
        ord(character) < 32 and character not in "\t\n\r" for character in value
    ):
        raise _fail("control character in public value")
    if key is not None and _forbidden_key(key):
        raise _fail("sensitive public field")
    if (
        _ABSOLUTE_PATH.match(value)
        or _BEARER.search(value)
        or _RAW_SECRET.search(value)
    ):
        raise _fail("sensitive public value")
    if not allow_multiline and ("\n" in value or "\r" in value):
        raise _fail("source-like public value")
    return value


def _json_value(
    value: object,
    *,
    key: str | None = None,
    depth: int = 0,
    active: set[int] | None = None,
) -> _JSONValue:
    if value is None:
        raise _fail("null public value")
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _fail("non-finite public number")
        return value
    if type(value) is str:
        return _safe_string(value, key=key)
    if isinstance(value, (Path, bytes, bytearray, memoryview, Enum)):
        raise _fail("unsupported public value")
    if type(value) is list:
        if depth >= MAX_PUBLIC_DEPTH:
            raise _fail("public value nesting exceeds bound")
        items = cast(list[object], value)
        if len(items) > MAX_LIST_ITEMS:
            raise _fail("public container exceeds item bound")
        container_id = id(value)
        active_ids = set() if active is None else active
        if container_id in active_ids:
            raise _fail("cyclic public value")
        active_ids.add(container_id)
        try:
            return [
                _json_value(item, depth=depth + 1, active=active_ids) for item in items
            ]
        finally:
            active_ids.remove(container_id)
    if type(value) is dict:
        if depth >= MAX_PUBLIC_DEPTH:
            raise _fail("public value nesting exceeds bound")
        mapping = cast(dict[object, object], value)
        if len(mapping) > MAX_LIST_ITEMS:
            raise _fail("public container exceeds item bound")
        container_id = id(value)
        active_ids = set() if active is None else active
        if container_id in active_ids:
            raise _fail("cyclic public value")
        active_ids.add(container_id)
        output: dict[str, _JSONValue] = {}
        try:
            for raw_key, item in mapping.items():
                if type(raw_key) is not str:
                    raise _fail("non-string public key")
                if _forbidden_key(raw_key):
                    raise _fail("sensitive public field")
                output[raw_key] = _json_value(
                    item, key=raw_key, depth=depth + 1, active=active_ids
                )
            return output
        finally:
            active_ids.remove(container_id)
    raise _fail("unsupported public value")


def _encoded_size(value: object) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _fail("value is not JSON-safe") from exc


def envelope_success(
    data: object, *, byte_budget: int = MAX_RESULT_BYTES
) -> dict[str, object]:
    """Build the only permitted success envelope."""

    budget = _validate_budget(byte_budget)
    if type(data) is not dict:
        raise _fail("success data must be an object")
    safe_data = cast(dict[str, _JSONValue], _json_value(data))
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "data": safe_data,
    }
    if _encoded_size(result) > budget:
        raise _fail("oversize result")
    return result


def envelope_failure(code: str, message: str = "request rejected") -> dict[str, object]:
    """Build a failure envelope without echoing exception details."""

    if type(code) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
        raise _fail("invalid public error code")
    if message != "request rejected":
        raise _fail("public error messages are fixed")
    return {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {"code": code, "message": "request rejected"},
    }


def _bounded_list(
    value: object, *, maximum: int = MAX_LIST_ITEMS
) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise _fail("invalid bounded list")
    return tuple(value)


def _string_list(value: object, *, maximum: int = MAX_LIST_ITEMS) -> list[str]:
    return [
        _safe_string(item, allow_empty=False)
        for item in _bounded_list(value, maximum=maximum)
    ]


def _relative_path(value: object, *, allow_empty: bool = False) -> str:
    path = _safe_string(value, allow_empty=allow_empty)
    normalized = path.replace("\\", "/")
    if not normalized and allow_empty:
        return ""
    if (
        normalized.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise _fail("host path in public result")
    return normalized


def _identifier(value: object, *, allow_empty: bool = False) -> str:
    result = _safe_string(value, allow_empty=allow_empty)
    if result and _IDENTIFIER.fullmatch(result) is None:
        raise _fail("invalid public identifier")
    return result


def _hash(value: object, *, allow_empty: bool = False) -> str:
    result = _safe_string(value, allow_empty=allow_empty)
    if result and _HASH.fullmatch(result) is None:
        raise _fail("invalid public hash")
    return result


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _fail("invalid public count")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise _fail("invalid public boolean")
    return value


def _enum_value(value: object, enum_type: type[_T]) -> str:
    if type(value) is not enum_type or not isinstance(value, Enum):
        raise _fail("invalid public enum")
    enum_value = getattr(value, "value", None)
    return _safe_string(enum_value, allow_empty=False)


def _optional_string(value: object, *, key: str | None = None) -> str | None:
    if value is None:
        return None
    return _safe_string(value, key=key)


def _gap(value: object) -> dict[str, object]:
    if type(value) is not CoverageGap:
        raise _fail("invalid project-index gap")
    return {
        "path": _relative_path(value.path),
        "code": _identifier(value.code),
        "message": _safe_string(value.message, maximum=4_096),
    }


def _index_node(value: object) -> dict[str, object]:
    if type(value) is not IndexNode:
        raise _fail("invalid project-index node")
    attributes: list[dict[str, str]] = []
    for item in _bounded_list(value.attributes, maximum=64):
        if not isinstance(item, tuple) or len(item) != 2:
            raise _fail("invalid project-index attributes")
        name, attribute_value = item
        attributes.append(
            {
                "name": _safe_string(name, allow_empty=False),
                "value": _safe_string(attribute_value),
            }
        )
    return {
        "node_id": _identifier(value.node_id),
        "kind": _identifier(value.kind),
        "path": _relative_path(value.path),
        "name": _safe_string(value.name),
        "qualified_name": _safe_string(value.qualified_name),
        "start_line": _nonnegative_int(value.start_line),
        "end_line": _nonnegative_int(value.end_line),
        "content_hash": _hash(value.content_hash, allow_empty=True),
        "attributes": attributes,
        "extractor_id": _safe_string(value.extractor_id),
        "extractor_version": _safe_string(value.extractor_version),
        "provenance": _identifier(value.provenance),
    }


def _index_edge(value: object) -> dict[str, object]:
    if type(value) is not IndexEdge:
        raise _fail("invalid project-index edge")
    return {
        "edge_id": _identifier(value.edge_id),
        "source_id": _identifier(value.source_id),
        "target_id": _identifier(value.target_id),
        "relation": _identifier(value.relation),
        "path": _relative_path(value.path, allow_empty=True),
        "start_line": _nonnegative_int(value.start_line),
        "end_line": _nonnegative_int(value.end_line),
        "content_hash": _hash(value.content_hash, allow_empty=True),
        "extractor_id": _safe_string(value.extractor_id),
        "extractor_version": _safe_string(value.extractor_version),
        "provenance": _identifier(value.provenance),
    }


def _source_window(value: object) -> dict[str, object]:
    if type(value) is not SourceWindow:
        raise _fail("invalid source window")
    return {
        "path": _relative_path(value.path),
        "start_line": _nonnegative_int(value.start_line),
        "end_line": _nonnegative_int(value.end_line),
        "content_hash": _hash(value.content_hash),
    }


def _workspace_id(workspace: object) -> str:
    return _safe_string(workspace, allow_empty=False)


def _snapshot_workspace(snapshot: IndexSnapshot) -> str:
    workspace = snapshot.workspace_id or snapshot.workspace
    if (
        snapshot.workspace_id
        and snapshot.workspace
        and snapshot.workspace_id != snapshot.workspace
    ):
        raise _fail("workspace binding mismatch")
    return _workspace_id(workspace)


def _index_snapshot_data(snapshot: IndexSnapshot) -> dict[str, object]:
    if type(snapshot) is not IndexSnapshot:
        raise _fail("invalid project-index snapshot")
    data: dict[str, object] = {
        "workspace_id": _snapshot_workspace(snapshot),
        "snapshot_id": _identifier(snapshot.snapshot_id),
        "state": _enum_value(snapshot.state, IndexState),
        "file_count": _nonnegative_int(snapshot.file_count),
        "blob_count": _nonnegative_int(snapshot.blob_count),
        "reused_blob_count": _nonnegative_int(snapshot.reused_blob_count),
        "node_count": _nonnegative_int(snapshot.node_count),
        "edge_count": _nonnegative_int(snapshot.edge_count),
        "gap_count": _nonnegative_int(snapshot.gap_count),
        "manifest_hash": _hash(snapshot.manifest_hash, allow_empty=True),
        "parser_set_hash": _hash(snapshot.parser_set_hash, allow_empty=True),
        "binding_state": _identifier(snapshot.binding_state),
    }
    head = _optional_string(snapshot.head)
    if head is not None:
        data["head"] = head
    return data


def _index_status_data(status: IndexStatus) -> dict[str, object]:
    if type(status) is not IndexStatus:
        raise _fail("invalid project-index status")
    data: dict[str, object] = {
        "workspace_id": _workspace_id(status.workspace),
        "state": _enum_value(status.state, IndexState),
        "required_paths": [
            _relative_path(item) for item in _bounded_list(status.required_paths)
        ],
        "missing_paths": [
            _relative_path(item) for item in _bounded_list(status.missing_paths)
        ],
        "changed_paths": [
            _relative_path(item) for item in _bounded_list(status.changed_paths)
        ],
        "gaps": [_gap(item) for item in _bounded_list(status.gaps)],
        "binding_state": _identifier(status.binding_state),
    }
    if status.snapshot_id is not None:
        data["snapshot_id"] = _identifier(status.snapshot_id)
    return data


def _query_data(result: QueryResult) -> dict[str, object]:
    if type(result) is not QueryResult:
        raise _fail("invalid project-index query result")
    return {
        "trace_id": _identifier(result.trace_id),
        "snapshot_id": _identifier(result.snapshot_id),
        "state": _enum_value(result.state, IndexState),
        "nodes": [_index_node(item) for item in _bounded_list(result.nodes)],
        "edges": [_index_edge(item) for item in _bounded_list(result.edges)],
        "source_windows": [
            _source_window(item) for item in _bounded_list(result.source_windows)
        ],
        "gaps": [_gap(item) for item in _bounded_list(result.gaps)],
        "truncated": _bool(result.truncated),
    }


def project_index_register(workspace_id: object) -> dict[str, object]:
    return envelope_success({"workspace_id": _workspace_id(workspace_id)})


def project_index_sync(snapshot: IndexSnapshot) -> dict[str, object]:
    return envelope_success(_index_snapshot_data(snapshot))


def project_index_status(status: IndexStatus) -> dict[str, object]:
    return envelope_success(_index_status_data(status))


def project_index_query(result: QueryResult) -> dict[str, object]:
    return envelope_success(_query_data(result))


def _checkpoint_data(checkpoint: Checkpoint) -> dict[str, object]:
    if type(checkpoint) is not Checkpoint:
        raise _fail("invalid checkpoint")
    if checkpoint.workspace_root:
        raise _fail("legacy workspace root is not public")
    data: dict[str, object] = {
        "checkpoint_id": _identifier(checkpoint.checkpoint_id),
        "workflow_id": _identifier(checkpoint.workflow_id),
        "task_id": _identifier(checkpoint.task_id),
        "owner": _identifier(checkpoint.owner),
        "lease_epoch": checkpoint.lease_epoch
        if type(checkpoint.lease_epoch) is int and checkpoint.lease_epoch > 0
        else (_fail("invalid lease epoch")),
        "workspace_id": _workspace_id(checkpoint.workspace_id),
        "snapshot_id": _identifier(checkpoint.snapshot_id),
        "write_scope": [
            _relative_path(item) for item in _bounded_list(checkpoint.write_scope)
        ],
        "write_scope_hash": _hash(checkpoint.write_scope_hash),
        "manifest_hash": _hash(checkpoint.manifest_hash),
        "cas_root_hash": _hash(checkpoint.cas_root_hash),
        "entry_count": _nonnegative_int(checkpoint.entry_count),
        "kind": _identifier(checkpoint.kind),
    }
    if checkpoint.parent_checkpoint_id is not None:
        data["parent_checkpoint_id"] = _identifier(checkpoint.parent_checkpoint_id)
    return data


def project_checkpoint_create(checkpoint: Checkpoint) -> dict[str, object]:
    return envelope_success(_checkpoint_data(checkpoint))


def project_checkpoint_status(checkpoint: Checkpoint) -> dict[str, object]:
    return envelope_success(_checkpoint_data(checkpoint))


def project_checkpoint_restore(result: RestoreResult) -> dict[str, object]:
    if type(result) is not RestoreResult:
        raise _fail("invalid restore result")
    return envelope_success(
        {
            "checkpoint_id": _identifier(result.checkpoint_id),
            "rescue_checkpoint_id": _identifier(result.rescue_checkpoint_id),
            "restored_snapshot_id": _identifier(result.restored_snapshot_id),
            "changed_paths": [
                _relative_path(item) for item in _bounded_list(result.changed_paths)
            ],
        }
    )


def _atlas_node(value: object) -> dict[str, object]:
    if type(value) is not AtlasNode:
        raise _fail("invalid Atlas node")
    data: dict[str, object] = {
        "node_id": _safe_string(value.node_id, allow_empty=False),
        "kind": _enum_value(value.kind, type(value.kind)),
        "schema_version": _safe_string(value.schema_version, allow_empty=False),
        "extractor_id": _safe_string(value.extractor_id),
        "extractor_version": _safe_string(value.extractor_version),
        "provenance": _safe_string(value.provenance, allow_empty=False),
        "source_hashes": [_hash(item) for item in _bounded_list(value.source_hashes)],
    }
    for key, raw_value in (
        ("created_at", value.created_at),
        ("superseded_at", value.superseded_at),
        ("quarantine_state", value.quarantine_state),
    ):
        optional_value = _optional_string(raw_value)
        if optional_value is not None:
            data[key] = optional_value
    return data


def _atlas_edge(value: object) -> dict[str, object]:
    if type(value) is not AtlasEdge:
        raise _fail("invalid Atlas edge")
    data: dict[str, object] = {
        "edge_id": _safe_string(value.edge_id, allow_empty=False),
        "relation": _enum_value(value.relation, type(value.relation)),
        "source_id": _safe_string(value.source_id, allow_empty=False),
        "target_id": _safe_string(value.target_id, allow_empty=False),
        "source_kind": _enum_value(value.source_kind, type(value.source_kind)),
        "target_kind": _enum_value(value.target_kind, type(value.target_kind)),
        "schema_version": _safe_string(value.schema_version, allow_empty=False),
        "provenance": _safe_string(value.provenance, allow_empty=False),
    }
    created_at = _optional_string(value.created_at)
    if created_at is not None:
        data["created_at"] = created_at
    return data


def project_atlas_query(result: GraphQueryResult) -> dict[str, object]:
    if type(result) is not GraphQueryResult:
        raise _fail("invalid Atlas query result")
    return envelope_success(
        {
            "nodes": [_atlas_node(item) for item in _bounded_list(result.nodes)],
            "edges": [_atlas_edge(item) for item in _bounded_list(result.edges)],
            "truncated": _bool(result.truncated),
        }
    )


def _test_spec(value: object) -> dict[str, object]:
    if type(value) is not TestSpec:
        raise _fail("invalid Atlas test specification")
    exit_code = value.expected_exit_code
    if type(exit_code) is not int or exit_code < 0:
        raise _fail("invalid Atlas test specification")
    return {
        "argv": [
            _safe_string(item, allow_empty=False, maximum=4_096)
            for item in _bounded_list(value.argv, maximum=64)
        ],
        "expected_exit_code": exit_code,
    }


def _packet(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if type(value) is not ImplementationPacket:
        raise _fail("invalid Atlas packet")
    return {
        "packet_id": _safe_string(value.packet_id, allow_empty=False),
        "trace_id": _safe_string(value.trace_id, allow_empty=False),
        "workspace_id": _workspace_id(value.workspace_id),
        "snapshot_id": _safe_string(value.snapshot_id, allow_empty=False),
        "recipe_id": _safe_string(value.recipe_id, allow_empty=False),
        "node_ids": _string_list(value.node_ids),
        "edge_ids": _string_list(value.edge_ids),
        "evidence_hashes": [
            _hash(item) for item in _bounded_list(value.evidence_hashes)
        ],
        "gaps": _string_list(value.gaps),
        "source_hashes": [_hash(item) for item in _bounded_list(value.source_hashes)],
        "template_hashes": [
            _hash(item) for item in _bounded_list(value.template_hashes)
        ],
        "next_action": _safe_string(value.next_action),
        "request_hash": _safe_string(value.request_hash),
        "matcher_version": _safe_string(value.matcher_version),
        "target_paths": [
            _relative_path(item) for item in _bounded_list(value.target_paths)
        ],
        "target_symbols": _string_list(value.target_symbols),
    }


def project_atlas_prepare(result: PreparationResult) -> dict[str, object]:
    if type(result) is not PreparationResult:
        raise _fail("invalid Atlas preparation result")
    packet = _packet(result.packet)
    if packet is None:
        raise _fail("Atlas preparation packet is required")
    return envelope_success(
        {
            "status": _enum_value(result.status, AtlasStatus),
            "packet": packet,
            "candidate_recipe_ids": _string_list(result.candidate_recipe_ids),
            "reasons": _string_list(result.reasons),
        }
    )


def project_atlas_render(result: RenderResult) -> dict[str, object]:
    if type(result) is not RenderResult:
        raise _fail("invalid Atlas render result")
    patch = _safe_string(
        result.patch_candidate,
        key="patch_candidate",
        allow_multiline=True,
        maximum=131_072,
    )
    return envelope_success(
        {
            "status": _enum_value(result.status, AtlasStatus),
            "packet_id": _safe_string(result.packet_id, allow_empty=False),
            "patch_candidate": patch,
            "patch_hash": _hash(result.patch_hash, allow_empty=True),
            "bindings_hash": _hash(result.bindings_hash, allow_empty=True),
            "test_specs": [
                _test_spec(item) for item in _bounded_list(result.test_specs)
            ],
            "reasons": _string_list(result.reasons),
        }
    )


def project_atlas_accept(result: AcceptanceProjection) -> dict[str, object]:
    if type(result) is not AcceptanceProjection:
        raise _fail("invalid Atlas acceptance result")
    recipe_id = _optional_string(result.recipe_id)
    if recipe_id is None:
        raise _fail("Atlas acceptance recipe is required")
    return envelope_success(
        {
            "acceptance_id": _safe_string(result.acceptance_id, allow_empty=False),
            "code_task_id": _safe_string(result.code_task_id, allow_empty=False),
            "output_snapshot_id": _safe_string(
                result.output_snapshot_id, allow_empty=False
            ),
            "atlas_ingest_state": _enum_value(result.atlas_ingest_state, AtlasStatus),
            "episode_id": _safe_string(result.episode_id, allow_empty=False),
            "recipe_id": recipe_id,
            "reasons": _string_list(result.reasons),
        }
    )


def _exact_dict(value: object, expected: set[str], detail: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise _fail(detail)
    return cast(dict[str, object], value)


def _relay_pair_list(value: object, keys: tuple[str, str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in _bounded_list(value, maximum=64):
        pair = _exact_dict(item, set(keys), "invalid Relay pair")
        result.append(
            {
                keys[0]: _identifier(pair[keys[0]]),
                keys[1]: _safe_string(pair[keys[1]], maximum=4_096),
            }
        )
    return result


def _relay_scope_list(value: object) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in _bounded_list(value, maximum=64):
        scope = _exact_dict(item, {"path", "kind"}, "invalid Relay write scope")
        kind = _identifier(scope["kind"])
        if kind not in {"file", "tree"}:
            raise _fail("invalid Relay write scope")
        result.append({"path": _relative_path(scope["path"]), "kind": kind})
    return result


def _relay_route(value: object) -> dict[str, str]:
    route = _exact_dict(
        value, {"route_class", "model", "reasoning_effort"}, "invalid Relay route"
    )
    return {
        "route_class": _identifier(route["route_class"]),
        "model": _safe_string(route["model"], allow_empty=False),
        "reasoning_effort": _identifier(route["reasoning_effort"]),
    }


def _relay_task(value: object) -> dict[str, object]:
    task = _exact_dict(
        value,
        {
            "task_id",
            "kind",
            "title",
            "objective",
            "priority",
            "dependencies",
            "write_scope",
            "route",
            "constraints",
            "acceptance_criteria",
            "atlas_packet_ids",
            "required_evidence",
            "prewarm_for_task_id",
            "retry_policy",
        },
        "invalid Relay task",
    )
    priority = task["priority"]
    if type(priority) is not int or not 1 <= priority <= 100:
        raise _fail("invalid Relay task")
    retry = _exact_dict(
        task["retry_policy"],
        {"max_attempts", "retryable_codes"},
        "invalid Relay retry policy",
    )
    attempts = retry["max_attempts"]
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise _fail("invalid Relay retry policy")
    result: dict[str, object] = {
        "task_id": _identifier(task["task_id"]),
        "kind": _identifier(task["kind"]),
        "title": _safe_string(task["title"], maximum=256),
        "objective": _safe_string(task["objective"], maximum=4_096),
        "priority": priority,
        "dependencies": [
            _identifier(item) for item in _bounded_list(task["dependencies"])
        ],
        "write_scope": _relay_scope_list(task["write_scope"]),
        "route": _relay_route(task["route"]),
        "constraints": _relay_pair_list(task["constraints"], ("code", "detail")),
        "acceptance_criteria": _relay_pair_list(
            task["acceptance_criteria"], ("criterion_id", "description")
        ),
        "atlas_packet_ids": [
            _hash(item) for item in _bounded_list(task["atlas_packet_ids"])
        ],
        "required_evidence": _relay_pair_list(
            task["required_evidence"], ("kind", "selector")
        ),
        "retry_policy": {
            "max_attempts": attempts,
            "retryable_codes": [
                _identifier(item) for item in _bounded_list(retry["retryable_codes"])
            ],
        },
    }
    if task["prewarm_for_task_id"] is not None:
        result["prewarm_for_task_id"] = _identifier(task["prewarm_for_task_id"])
    return result


def _relay_edges(value: object) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in _bounded_list(value, maximum=2_048):
        edge = _exact_dict(
            item,
            {"from_task_id", "kind", "to_task_id"},
            "invalid Relay edge",
        )
        result.append(
            {
                "from_task_id": _identifier(edge["from_task_id"]),
                "kind": _identifier(edge["kind"]),
                "to_task_id": _identifier(edge["to_task_id"]),
            }
        )
    return result


def _relay_plan(value: object) -> dict[str, object]:
    plan = _exact_dict(
        value,
        {
            "schema",
            "workflow_id",
            "workspace_binding",
            "base_commit",
            "capacity",
            "runtime_policy_id",
            "tasks",
            "dependencies",
            "conflicts",
            "queues",
            "plan_hash",
        },
        "invalid Relay plan",
    )
    binding = _exact_dict(
        plan["workspace_binding"],
        {"workspace_id", "input_snapshot_id", "atlas_packet_ids"},
        "invalid Relay binding",
    )
    capacity = plan["capacity"]
    if type(capacity) is not int or not 1 <= capacity <= 3:
        raise _fail("invalid Relay plan")
    queues = _exact_dict(
        plan["queues"],
        {
            "prepared_prewarms",
            "ready",
            "running_slots",
            "review_integration",
            "terminal",
        },
        "invalid Relay queues",
    )
    return {
        "schema": _safe_string(plan["schema"], allow_empty=False),
        "workflow_id": _identifier(plan["workflow_id"]),
        "workspace_binding": {
            "workspace_id": _workspace_id(binding["workspace_id"]),
            "input_snapshot_id": _hash(binding["input_snapshot_id"]),
            "atlas_packet_ids": [
                _hash(item) for item in _bounded_list(binding["atlas_packet_ids"])
            ],
        },
        "base_commit": _safe_string(plan["base_commit"], allow_empty=False),
        "capacity": capacity,
        "runtime_policy_id": _safe_string(plan["runtime_policy_id"], allow_empty=False),
        "tasks": [
            _relay_task(item) for item in _bounded_list(plan["tasks"], maximum=64)
        ],
        "dependencies": _relay_edges(plan["dependencies"]),
        "conflicts": _relay_edges(plan["conflicts"]),
        "queues": {
            name: [
                _identifier(item) for item in _bounded_list(queues[name], maximum=64)
            ]
            for name in (
                "prepared_prewarms",
                "ready",
                "running_slots",
                "review_integration",
                "terminal",
            )
        },
        "plan_hash": _hash(plan["plan_hash"]),
    }


def project_relay_compile(plan: object) -> dict[str, object]:
    return envelope_success(_relay_plan(plan))


def _relay_run(value: object) -> dict[str, object]:
    run = _exact_dict(
        value,
        {
            "run_id",
            "workflow_id",
            "plan_hash",
            "workspace_id",
            "input_snapshot_id",
            "base_commit",
            "capacity",
            "schedule_version",
        },
        "invalid Relay run",
    )
    capacity = run["capacity"]
    schedule = run["schedule_version"]
    if (
        type(capacity) is not int
        or type(schedule) is not int
        or capacity < 1
        or schedule < 0
    ):
        raise _fail("invalid Relay run")
    return {
        "run_id": _identifier(run["run_id"]),
        "workflow_id": _identifier(run["workflow_id"]),
        "plan_hash": _hash(run["plan_hash"]),
        "workspace_id": _workspace_id(run["workspace_id"]),
        "input_snapshot_id": _hash(run["input_snapshot_id"]),
        "base_commit": _safe_string(run["base_commit"], allow_empty=False),
        "capacity": capacity,
        "schedule_version": schedule,
    }


def _relay_status_task(value: object) -> dict[str, object]:
    task = _exact_dict(
        value,
        {
            "task_id",
            "kind",
            "priority",
            "state",
            "task_version",
            "scope_owner",
            "candidate_id",
            "last_lease_epoch",
        },
        "invalid Relay status task",
    )
    result: dict[str, object] = {
        "task_id": _identifier(task["task_id"]),
        "kind": _identifier(task["kind"]),
        "priority": _nonnegative_int(task["priority"]),
        "state": _identifier(task["state"]),
        "task_version": _nonnegative_int(task["task_version"]),
        "last_lease_epoch": _nonnegative_int(task["last_lease_epoch"]),
    }
    if task["scope_owner"] is not None:
        result["scope_owner"] = _identifier(task["scope_owner"])
    if task["candidate_id"] is not None:
        result["candidate_id"] = _identifier(task["candidate_id"])
    return result


def _relay_status_lease(value: object) -> dict[str, object]:
    lease = _exact_dict(
        value,
        {
            "lease_id",
            "task_id",
            "action_id",
            "epoch",
            "task_version",
            "lease_kind",
            "endpoint",
            "state",
            "released_at",
        },
        "invalid Relay lease",
    )
    result: dict[str, object] = {
        "lease_id": _identifier(lease["lease_id"]),
        "task_id": _identifier(lease["task_id"]),
        "action_id": _identifier(lease["action_id"]),
        "epoch": _nonnegative_int(lease["epoch"]),
        "task_version": _nonnegative_int(lease["task_version"]),
        "lease_kind": _identifier(lease["lease_kind"]),
        "state": _identifier(lease["state"]),
    }
    endpoint = _optional_string(lease["endpoint"])
    if endpoint is not None:
        result["endpoint"] = endpoint
    released_at = _optional_string(lease["released_at"])
    if released_at is not None:
        result["released_at"] = released_at
    return result


def _relay_candidate(value: object) -> dict[str, object]:
    candidate = _exact_dict(
        value,
        {
            "candidate_id",
            "task_id",
            "originating_epoch",
            "branch",
            "base_commit",
            "head_commit",
            "diff_hash",
            "evidence_hashes",
            "pr_reference",
            "status",
            "review_digest",
            "integration_commit",
        },
        "invalid Relay candidate",
    )
    result: dict[str, object] = {
        "candidate_id": _identifier(candidate["candidate_id"]),
        "task_id": _identifier(candidate["task_id"]),
        "originating_epoch": _nonnegative_int(candidate["originating_epoch"]),
        "branch": _safe_string(candidate["branch"], allow_empty=False),
        "base_commit": _safe_string(candidate["base_commit"], allow_empty=False),
        "head_commit": _safe_string(candidate["head_commit"], allow_empty=False),
        "diff_hash": _hash(candidate["diff_hash"]),
        "evidence_hashes": [
            _hash(item) for item in _bounded_list(candidate["evidence_hashes"])
        ],
        "status": _identifier(candidate["status"]),
    }
    for key in ("pr_reference", "review_digest", "integration_commit"):
        optional_value = _optional_string(candidate[key])
        if optional_value is not None:
            result[key] = optional_value
    return result


def _relay_directive(value: object) -> dict[str, object]:
    directive = _exact_dict(
        value,
        {
            "directive_id",
            "workflow_id",
            "task_id",
            "expected_schedule_version",
            "route",
            "relay_start_request",
        },
        "invalid Relay refill directive",
    )
    return {
        "directive_id": _identifier(directive["directive_id"]),
        "workflow_id": _identifier(directive["workflow_id"]),
        "task_id": _identifier(directive["task_id"]),
        "expected_schedule_version": _nonnegative_int(
            directive["expected_schedule_version"]
        ),
        "route": _relay_route(directive["route"]),
    }


def _relay_queues(value: object) -> dict[str, list[dict[str, object]]]:
    queues = _exact_dict(
        value,
        {
            "prepared_prewarms",
            "ready",
            "running_slots",
            "review_integration",
            "terminal",
        },
        "invalid Relay queues",
    )
    return {
        name: [
            _relay_status_task(item) for item in _bounded_list(queues[name], maximum=64)
        ]
        for name in (
            "prepared_prewarms",
            "ready",
            "running_slots",
            "review_integration",
            "terminal",
        )
    }


def project_relay_status(status: object) -> dict[str, object]:
    value = _exact_dict(
        status,
        {
            "schema",
            "workflow_id",
            "run",
            "schedule_version",
            "tasks",
            "leases",
            "candidates",
            "outstanding_action_ids",
            "refill_directives",
            "queues",
        },
        "invalid Relay status",
    )
    return envelope_success(
        {
            "workflow_id": _identifier(value["workflow_id"]),
            "run": _relay_run(value["run"]),
            "schedule_version": _nonnegative_int(value["schedule_version"]),
            "tasks": [
                _relay_status_task(item)
                for item in _bounded_list(value["tasks"], maximum=64)
            ],
            "leases": [
                _relay_status_lease(item)
                for item in _bounded_list(value["leases"], maximum=128)
            ],
            "candidates": [
                _relay_candidate(item)
                for item in _bounded_list(value["candidates"], maximum=64)
            ],
            "outstanding_action_ids": [
                _identifier(item)
                for item in _bounded_list(value["outstanding_action_ids"], maximum=128)
            ],
            "refill_directives": [
                _relay_directive(item)
                for item in _bounded_list(value["refill_directives"], maximum=128)
            ],
            "queues": _relay_queues(value["queues"]),
        }
    )


def _relay_action(value: object) -> dict[str, object]:
    action = _exact_dict(
        value,
        {
            "action_id",
            "kind",
            "workflow_id",
            "task_id",
            "lease",
            "route",
            "model",
            "reasoning_effort",
        },
        "invalid Relay action",
    )
    lease = _exact_dict(
        action["lease"], {"lease_id", "epoch", "task_version"}, "invalid Relay lease"
    )
    return {
        "action_id": _identifier(action["action_id"]),
        "kind": _identifier(action["kind"]),
        "workflow_id": _identifier(action["workflow_id"]),
        "task_id": _identifier(action["task_id"]),
        "lease": {
            "lease_id": _identifier(lease["lease_id"]),
            "epoch": _nonnegative_int(lease["epoch"]),
            "task_version": _nonnegative_int(lease["task_version"]),
        },
        "route": _relay_route(action["route"]),
        "model": _safe_string(action["model"], allow_empty=False),
        "reasoning_effort": _identifier(action["reasoning_effort"]),
    }


def project_relay_start(result: object) -> dict[str, object]:
    value = _exact_dict(
        result,
        {"schema", "workflow_id", "run_id", "schedule_version", "host_actions"},
        "invalid Relay start result",
    )
    return envelope_success(
        {
            "workflow_id": _identifier(value["workflow_id"]),
            "run_id": _identifier(value["run_id"]),
            "schedule_version": _nonnegative_int(value["schedule_version"]),
            "actions": [
                _relay_action(item)
                for item in _bounded_list(value["host_actions"], maximum=64)
            ],
        }
    )


def _relay_mutation(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail("invalid Relay mutation result")
    keys = set(value)
    if keys not in (
        {"workflow_id", "schedule_version", "task"},
        {"workflow_id", "schedule_version", "task", "candidate"},
    ):
        raise _fail("invalid Relay mutation result")
    result: dict[str, object] = {
        "workflow_id": _identifier(value["workflow_id"]),
        "schedule_version": _nonnegative_int(value["schedule_version"]),
        "task": _relay_status_task(value["task"]),
    }
    if "candidate" in value:
        result["candidate"] = _relay_candidate(value["candidate"])
    return result


def project_relay_handoff(result: object) -> dict[str, object]:
    return envelope_success(_relay_mutation(result))


def project_relay_integrate(result: object) -> dict[str, object]:
    return envelope_success(_relay_mutation(result))


def result_from_exception(
    exception: BaseException, *, invalid_code: str = "INVALID_REQUEST"
) -> dict[str, object]:
    """Map domain failures in locked precedence without exposing details."""

    if not isinstance(exception, Exception):
        raise exception
    code: str
    if isinstance(exception, RuntimeContractError):
        candidate = exception.code
        code = candidate if candidate in _PUBLIC_ERROR_CODES else "INTERNAL_ERROR"
    elif isinstance(exception, RelayPlanError):
        code = "RELAY_PLAN_INVALID"
    elif isinstance(exception, AtlasError):
        candidate = getattr(exception, "code", "")
        code = candidate if candidate in _ATLAS_ERROR_CODES else "ATLAS_REQUEST_INVALID"
    elif isinstance(exception, RelayError):
        candidate = getattr(exception, "code", "")
        code = candidate if candidate in _RELAY_ERROR_CODES else "RELAY_REQUEST_INVALID"
    elif isinstance(exception, IndexError):
        candidate = getattr(exception, "code", "")
        code = candidate if candidate in _INDEX_ERROR_CODES else "INDEX_UNAVAILABLE"
    elif isinstance(exception, RelayStoreError):
        code = "STORAGE_ERROR"
    elif isinstance(exception, (sqlite3.Error, OSError)):
        code = "STORAGE_ERROR"
    elif isinstance(exception, KeyError):
        code = "NOT_FOUND"
    elif isinstance(exception, (TypeError, ValueError)):
        if type(invalid_code) is not str or invalid_code not in _PUBLIC_ERROR_CODES:
            code = "INVALID_REQUEST"
        else:
            code = invalid_code
    else:
        code = "INTERNAL_ERROR"
    return envelope_failure(code)


def project_tool_result(tool_name: str, value: object) -> dict[str, object]:
    """Dispatch one of the sixteen explicit projectors; no fallback exists."""

    if type(tool_name) is not str or tool_name not in TOOL_ANNOTATIONS:
        raise _fail("unknown tool projector")
    projectors: dict[str, _Projector] = {
        "project_index_register": project_index_register,
        "project_index_sync": cast(_Projector, project_index_sync),
        "project_index_status": cast(_Projector, project_index_status),
        "project_index_query": cast(_Projector, project_index_query),
        "worktree_checkpoint_create": cast(_Projector, project_checkpoint_create),
        "worktree_checkpoint_status": cast(_Projector, project_checkpoint_status),
        "worktree_checkpoint_restore": cast(_Projector, project_checkpoint_restore),
        "atlas_query": cast(_Projector, project_atlas_query),
        "atlas_prepare": cast(_Projector, project_atlas_prepare),
        "atlas_render": cast(_Projector, project_atlas_render),
        "atlas_accept": cast(_Projector, project_atlas_accept),
        "relay_compile": project_relay_compile,
        "relay_start": project_relay_start,
        "relay_status": project_relay_status,
        "relay_handoff": project_relay_handoff,
        "relay_integrate": project_relay_integrate,
    }
    return projectors[tool_name](value)


project_result = project_tool_result
make_success = envelope_success
make_failure = envelope_failure
