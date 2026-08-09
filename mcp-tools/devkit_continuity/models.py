"""Frozen, validated records for the private Continuity foundation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .canonical import (
    canonical_frozen_view_manifest,
    canonical_json,
    cas_root_identity,
    is_hash_id,
    key_identity,
    manifest_identity,
    receipt_identity,
    view_identity,
)

_ENTRY_ROLES = frozenset({"before_file", "after_file"})
_COMMAND_ABSOLUTE_PATH_VALUE = re.compile(r"(?:^|=)(?:/|[A-Za-z]:[\\/])")


class ContinuityError(ValueError):
    """Stable private validation failure whose message is its code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ContinuityKey:
    workflow_id: str
    code_task_id: str
    code_task_version: int
    acceptance_id: str
    ingestion_key: str
    payload_hash: str
    evidence_binding_hash: str

    def __post_init__(self) -> None:
        for value in (
            self.workflow_id,
            self.code_task_id,
            self.acceptance_id,
            self.ingestion_key,
        ):
            _require_identifier(value)
        _require_nonnegative_integer(self.code_task_version, "CODE_TASK_VERSION_INVALID")
        _require_hash(self.payload_hash)
        _require_hash(self.evidence_binding_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "code_task_id": self.code_task_id,
            "code_task_version": self.code_task_version,
            "acceptance_id": self.acceptance_id,
            "ingestion_key": self.ingestion_key,
            "payload_hash": self.payload_hash,
            "evidence_binding_hash": self.evidence_binding_hash,
        }

    @property
    def key_hash(self) -> str:
        """Return the Continuity-owned, domain-separated key identity."""
        return key_identity(self)


@dataclass(frozen=True, slots=True)
class FrozenEntry:
    role: str
    path: str
    content_hash: str
    byte_length: int

    def __post_init__(self) -> None:
        if self.role not in _ENTRY_ROLES:
            raise ContinuityError("ENTRY_ROLE_INVALID")
        _require_relative_path(self.path)
        _require_hash(self.content_hash)
        _require_nonnegative_integer(self.byte_length, "BYTE_LENGTH_INVALID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class ChangedNode:
    """Redacted, typed index-node metadata needed for deterministic replay."""

    node_id: str
    kind: str
    path: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_identifier(self.node_id)
        _require_identifier(self.kind)
        _require_relative_path(self.path)
        _require_hash(self.content_hash)

    def to_dict(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "path": self.path,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """A typed, non-body coverage gap retained in a frozen view."""

    path: str
    code: str
    message: str

    def __post_init__(self) -> None:
        _require_relative_path(self.path)
        _require_identifier(self.code)
        _require_identifier(self.message)

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class BoundExecutionReceipt:
    """Redacted execution receipt metadata, with no host path permitted."""

    receipt_id: str
    kind: str
    workflow_id: str
    task_id: str
    acceptance_id: str
    workspace_hash: str
    output_snapshot_id: str
    command_spec: tuple[str, ...]
    command_spec_hash: str
    input_hash: str
    output_hash: str
    exit_code: int
    success: bool

    def __post_init__(self) -> None:
        for value in (
            self.receipt_id,
            self.kind,
            self.workflow_id,
            self.task_id,
            self.acceptance_id,
            self.output_snapshot_id,
        ):
            _require_identifier(value)
        for value in (
            self.workspace_hash,
            self.command_spec_hash,
            self.input_hash,
            self.output_hash,
        ):
            _require_hash(value)
        command_spec = tuple(self.command_spec)
        if not command_spec:
            raise ContinuityError("COMMAND_SPEC_INVALID")
        for item in command_spec:
            _require_command_atom(item)
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ContinuityError("EXIT_CODE_INVALID")
        if not isinstance(self.success, bool):
            raise ContinuityError("SUCCESS_INVALID")
        object.__setattr__(self, "command_spec", command_spec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "kind": self.kind,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "acceptance_id": self.acceptance_id,
            "workspace_hash": self.workspace_hash,
            "output_snapshot_id": self.output_snapshot_id,
            "command_spec": list(self.command_spec),
            "command_spec_hash": self.command_spec_hash,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "exit_code": self.exit_code,
            "success": self.success,
        }


@dataclass(frozen=True, slots=True)
class FrozenView:
    view_id: str
    manifest_hash: str
    cas_root_hash: str
    key: ContinuityKey
    entries: tuple[FrozenEntry, ...]
    input_snapshot_ids: tuple[str, ...] = ()
    output_snapshot_ids: tuple[str, ...] = ()
    checkpoint_ids: tuple[str, ...] = ()
    query_ids: tuple[str, ...] = ()
    verification_artifact_hashes: tuple[str, ...] = ()
    execution_receipt_ids: tuple[str, ...] = ()
    request_hash: str | None = None
    evidence_hash: str | None = None
    changed_nodes: tuple[ChangedNode, ...] = ()
    coverage_gaps: tuple[CoverageGap, ...] = ()
    execution_receipts: tuple[BoundExecutionReceipt, ...] = ()

    def __post_init__(self) -> None:
        _require_hash(self.view_id)
        _require_hash(self.manifest_hash)
        _require_hash(self.cas_root_hash)
        if not isinstance(self.key, ContinuityKey):
            raise ContinuityError("KEY_INVALID")
        entries = tuple(self.entries)
        if not all(isinstance(item, FrozenEntry) for item in entries):
            raise ContinuityError("ENTRY_INVALID")
        if tuple(sorted(entries, key=_entry_sort_key)) != entries:
            raise ContinuityError("ENTRIES_NOT_CANONICAL")
        if len({(item.role, item.path) for item in entries}) != len(entries):
            raise ContinuityError("ENTRIES_DUPLICATE")
        input_snapshot_ids = _identifier_tuple(self.input_snapshot_ids)
        output_snapshot_ids = _identifier_tuple(self.output_snapshot_ids)
        checkpoint_ids = _identifier_tuple(self.checkpoint_ids)
        query_ids = _identifier_tuple(self.query_ids)
        verification_artifact_hashes = _hash_tuple(self.verification_artifact_hashes)
        execution_receipt_ids = _identifier_tuple(self.execution_receipt_ids)
        _require_optional_hash(self.request_hash)
        _require_optional_hash(self.evidence_hash)
        changed_nodes = _typed_tuple(self.changed_nodes, ChangedNode, "CHANGED_NODE_INVALID")
        coverage_gaps = _typed_tuple(self.coverage_gaps, CoverageGap, "COVERAGE_GAP_INVALID")
        receipts = _typed_tuple(
            self.execution_receipts, BoundExecutionReceipt, "EXECUTION_RECEIPT_INVALID"
        )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "input_snapshot_ids", input_snapshot_ids)
        object.__setattr__(self, "output_snapshot_ids", output_snapshot_ids)
        object.__setattr__(self, "checkpoint_ids", checkpoint_ids)
        object.__setattr__(self, "query_ids", query_ids)
        object.__setattr__(
            self, "verification_artifact_hashes", verification_artifact_hashes
        )
        object.__setattr__(self, "execution_receipt_ids", execution_receipt_ids)
        object.__setattr__(self, "changed_nodes", changed_nodes)
        object.__setattr__(self, "coverage_gaps", coverage_gaps)
        object.__setattr__(self, "execution_receipts", receipts)

    @classmethod
    def create(
        cls,
        *,
        key: ContinuityKey,
        entries: tuple[FrozenEntry, ...] | list[FrozenEntry],
        input_snapshot_ids: tuple[str, ...] | list[str] = (),
        output_snapshot_ids: tuple[str, ...] | list[str] = (),
        checkpoint_ids: tuple[str, ...] | list[str] = (),
        query_ids: tuple[str, ...] | list[str] = (),
        verification_artifact_hashes: tuple[str, ...] | list[str] = (),
        execution_receipt_ids: tuple[str, ...] | list[str] = (),
        request_hash: str | None = None,
        evidence_hash: str | None = None,
        changed_nodes: tuple[ChangedNode, ...] | list[ChangedNode] = (),
        coverage_gaps: tuple[CoverageGap, ...] | list[CoverageGap] = (),
        execution_receipts: tuple[BoundExecutionReceipt, ...]
        | list[BoundExecutionReceipt] = (),
    ) -> FrozenView:
        ordered_entries = tuple(entries)
        ordered_inputs = tuple(input_snapshot_ids)
        ordered_outputs = tuple(output_snapshot_ids)
        ordered_checkpoints = tuple(checkpoint_ids)
        ordered_queries = tuple(query_ids)
        ordered_artifacts = tuple(verification_artifact_hashes)
        ordered_receipt_ids = tuple(execution_receipt_ids)
        ordered_nodes = _typed_tuple(
            changed_nodes, ChangedNode, "CHANGED_NODE_INVALID"
        )
        ordered_gaps = _typed_tuple(coverage_gaps, CoverageGap, "COVERAGE_GAP_INVALID")
        ordered_receipts = _typed_tuple(
            execution_receipts, BoundExecutionReceipt, "EXECUTION_RECEIPT_INVALID"
        )
        manifest = canonical_frozen_view_manifest(
            key,
            ordered_entries,
            input_snapshot_ids=ordered_inputs,
            output_snapshot_ids=ordered_outputs,
            checkpoint_ids=ordered_checkpoints,
            query_ids=ordered_queries,
            verification_artifact_hashes=ordered_artifacts,
            execution_receipt_ids=ordered_receipt_ids,
            request_hash=request_hash,
            evidence_hash=evidence_hash,
            changed_nodes=ordered_nodes,
            coverage_gaps=ordered_gaps,
            execution_receipts=ordered_receipts,
        )
        manifest_hash = manifest_identity(manifest)
        cas_root_hash = cas_root_identity(ordered_entries)
        return cls(
            view_id=view_identity(manifest_hash, cas_root_hash),
            manifest_hash=manifest_hash,
            cas_root_hash=cas_root_hash,
            key=key,
            entries=ordered_entries,
            input_snapshot_ids=ordered_inputs,
            output_snapshot_ids=ordered_outputs,
            checkpoint_ids=ordered_checkpoints,
            query_ids=ordered_queries,
            verification_artifact_hashes=ordered_artifacts,
            execution_receipt_ids=ordered_receipt_ids,
            request_hash=request_hash,
            evidence_hash=evidence_hash,
            changed_nodes=ordered_nodes,
            coverage_gaps=ordered_gaps,
            execution_receipts=ordered_receipts,
        )

    @property
    def manifest_json(self) -> str:
        """Return the canonical, persistence-ready manifest payload."""
        return canonical_json(
            canonical_frozen_view_manifest(
                self.key,
                self.entries,
                input_snapshot_ids=self.input_snapshot_ids,
                output_snapshot_ids=self.output_snapshot_ids,
                checkpoint_ids=self.checkpoint_ids,
                query_ids=self.query_ids,
                verification_artifact_hashes=self.verification_artifact_hashes,
                execution_receipt_ids=self.execution_receipt_ids,
                request_hash=self.request_hash,
                evidence_hash=self.evidence_hash,
                changed_nodes=self.changed_nodes,
                coverage_gaps=self.coverage_gaps,
                execution_receipts=self.execution_receipts,
            )
        )


@dataclass(frozen=True, slots=True)
class ContinuityAttempt:
    key: ContinuityKey
    fence_epoch: int
    state: str
    view_id: str | None
    receipt_hash: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ContinuityKey):
            raise ContinuityError("KEY_INVALID")
        _require_positive_integer(self.fence_epoch, "FENCE_EPOCH_INVALID")
        _require_identifier(self.state)
        if self.view_id is None or self.receipt_hash is None:
            if self.state != "claimed" or self.view_id is not None or self.receipt_hash is not None:
                raise ContinuityError("ATTEMPT_BINDING_INVALID")
            return
        _require_hash(self.view_id)
        _require_hash(self.receipt_hash)


@dataclass(frozen=True, slots=True)
class ContinuityPointer:
    workflow_id: str
    code_task_id: str
    code_task_version: int
    view_id: str
    pointer_version: int
    fence_epoch: int

    def __post_init__(self) -> None:
        _require_identifier(self.workflow_id)
        _require_identifier(self.code_task_id)
        _require_nonnegative_integer(self.code_task_version, "CODE_TASK_VERSION_INVALID")
        _require_hash(self.view_id)
        _require_positive_integer(self.pointer_version, "POINTER_VERSION_INVALID")
        _require_positive_integer(self.fence_epoch, "FENCE_EPOCH_INVALID")


@dataclass(frozen=True, slots=True)
class ContinuityReceipt:
    key: ContinuityKey
    view_id: str
    receipt_hash: str
    kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ContinuityKey):
            raise ContinuityError("KEY_INVALID")
        _require_hash(self.view_id)
        _require_hash(self.receipt_hash)
        _require_identifier(self.kind)

    @classmethod
    def create(
        cls, *, key: ContinuityKey, view_id: str, kind: str
    ) -> ContinuityReceipt:
        return cls(key, view_id, receipt_identity(key, view_id, kind), kind)


def _entry_sort_key(entry: FrozenEntry) -> tuple[str, str, str]:
    return entry.role, entry.path, entry.content_hash


def _require_hash(value: object) -> None:
    if not is_hash_id(value):
        raise ContinuityError("HASH_ID_INVALID")


def _require_identifier(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ContinuityError("IDENTIFIER_INVALID")
    if any(ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContinuityError("IDENTIFIER_INVALID")


def _require_relative_path(value: object) -> None:
    _require_identifier(value)
    if not isinstance(value, str):
        raise ContinuityError("PATH_INVALID")
    if "\\" in value or value.startswith("/") or ":" in value:
        raise ContinuityError("PATH_INVALID")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ContinuityError("PATH_INVALID")


def _require_optional_hash(value: object) -> None:
    if value is not None:
        _require_hash(value)


def _require_command_atom(value: object) -> None:
    _require_identifier(value)
    if not isinstance(value, str):
        raise ContinuityError("COMMAND_SPEC_INVALID")
    if _COMMAND_ABSOLUTE_PATH_VALUE.search(value) is not None:
        raise ContinuityError("COMMAND_SPEC_INVALID")


def _typed_tuple(value: object, expected_type: type[Any], code: str) -> tuple[Any, ...]:
    try:
        result = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ContinuityError(code) from error
    if not all(isinstance(item, expected_type) for item in result):
        raise ContinuityError(code)
    return result


def _identifier_tuple(value: object) -> tuple[str, ...]:
    try:
        result = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ContinuityError("IDENTIFIER_INVALID") from error
    for item in result:
        _require_identifier(item)
    return result


def _hash_tuple(value: object) -> tuple[str, ...]:
    try:
        result = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ContinuityError("HASH_ID_INVALID") from error
    for item in result:
        _require_hash(item)
    return result


def _require_nonnegative_integer(value: object, code: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContinuityError(code)


def _require_positive_integer(value: object, code: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContinuityError(code)
