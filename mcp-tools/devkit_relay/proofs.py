"""Typed host-private integration proof contract for Relay.

Relay deliberately knows nothing about repository paths or Git processes.  A
host resolver attests Git truth and returns an immutable receipt through the
protocols in this module; Relay validates and durably binds that receipt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .canonical import canonical_hash

_EXPECTATION_SCHEMA = "2718lab-devkit/relay-integration-expectation-v1"
_RECEIPT_SCHEMA = "2718lab-devkit/relay-integration-proof-receipt-v1"
_DELTA_SCHEMA = "2718lab-devkit/relay-integration-tree-delta-v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}\Z")
_DRIVE_PATH = re.compile(r"[A-Za-z]:")
_MODES_BY_TYPE = {
    "blob": frozenset({"100644", "100755", "120000"}),
    "commit": frozenset({"160000"}),
    "tree": frozenset({"040000", "40000"}),
}
_PROOF_ERROR_CODES = frozenset(
    {
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
    }
)


class IntegrationProofError(RuntimeError):
    """A bounded host-proof failure safe to expose as a stable Relay code."""

    def __init__(self, code: str) -> None:
        if code not in _PROOF_ERROR_CODES:
            code = "RELAY_INTEGRATION_PROOF_INVALID"
        self.code = code
        super().__init__(code)


@runtime_checkable
class IntegrationProofReservation(Protocol):
    """Exclusive registry reservation held across Relay's SQLite commit."""

    @property
    def receipt(self) -> IntegrationProofReceipt:
        """Return the immutable receipt registered for the reserved proof."""

        ...

    def consume(self) -> None:
        """Mark a successfully persisted proof consumed in the host ledger."""

        ...

    def release(self) -> None:
        """Release a reservation after a failed Relay transaction."""

        ...


@runtime_checkable
class IntegrationProofResolver(Protocol):
    """Host-private proof registry and Git-attestation boundary."""

    def reserve(
        self, proof_id: str, expectation: IntegrationExpectation
    ) -> IntegrationProofReservation:
        """Exclusively reserve one registered proof for an exact expectation."""

        ...


@dataclass(frozen=True)
class IntegrationScopeEntry:
    """One exact file or segment-bounded tree authorization."""

    path: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind}

    @classmethod
    def from_dict(cls, value: object) -> IntegrationScopeEntry:
        data = _exact_dict(value, {"path", "kind"})
        return cls(path=_string(data["path"]), kind=_string(data["kind"]))


@dataclass(frozen=True)
class IntegrationDeltaEntry:
    """One canonical full-tree Git delta entry."""

    path: str
    old_oid: str | None
    new_oid: str | None
    old_mode: str | None
    new_mode: str | None
    old_type: str | None
    new_type: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "old_oid": self.old_oid,
            "new_oid": self.new_oid,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "old_type": self.old_type,
            "new_type": self.new_type,
        }

    @classmethod
    def from_dict(cls, value: object) -> IntegrationDeltaEntry:
        fields = {
            "path",
            "old_oid",
            "new_oid",
            "old_mode",
            "new_mode",
            "old_type",
            "new_type",
        }
        data = _exact_dict(value, fields)
        return cls(
            path=_string(data["path"]),
            old_oid=_optional_string(data["old_oid"]),
            new_oid=_optional_string(data["new_oid"]),
            old_mode=_optional_string(data["old_mode"]),
            new_mode=_optional_string(data["new_mode"]),
            old_type=_optional_string(data["old_type"]),
            new_type=_optional_string(data["new_type"]),
        )


@dataclass(frozen=True)
class IntegrationExpectation:
    """Exact Relay state and candidate binding reserved by the host attestor."""

    workflow_id: str
    run_id: str
    plan_hash: str
    workspace_id: str
    task_id: str
    task_version: int
    originating_epoch: int
    sol_scope: str
    candidate_id: str
    candidate_base_commit: str
    candidate_head_commit: str
    candidate_diff_hash: str
    candidate_evidence_hashes: tuple[str, ...]
    review_digest: str
    predecessor_integration_head: str
    predecessor_integration_version: int
    write_scope: tuple[IntegrationScopeEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _EXPECTATION_SCHEMA,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "originating_epoch": self.originating_epoch,
            "sol_scope": self.sol_scope,
            "candidate_id": self.candidate_id,
            "candidate_base_commit": self.candidate_base_commit,
            "candidate_head_commit": self.candidate_head_commit,
            "candidate_diff_hash": self.candidate_diff_hash,
            "candidate_evidence_hashes": list(self.candidate_evidence_hashes),
            "review_digest": self.review_digest,
            "predecessor_integration_head": self.predecessor_integration_head,
            "predecessor_integration_version": self.predecessor_integration_version,
            "write_scope": [entry.to_dict() for entry in self.write_scope],
        }

    @property
    def expectation_hash(self) -> str:
        return canonical_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> IntegrationExpectation:
        fields = {
            "schema",
            "workflow_id",
            "run_id",
            "plan_hash",
            "workspace_id",
            "task_id",
            "task_version",
            "originating_epoch",
            "sol_scope",
            "candidate_id",
            "candidate_base_commit",
            "candidate_head_commit",
            "candidate_diff_hash",
            "candidate_evidence_hashes",
            "review_digest",
            "predecessor_integration_head",
            "predecessor_integration_version",
            "write_scope",
        }
        data = _exact_dict(value, fields)
        if data["schema"] != _EXPECTATION_SCHEMA:
            raise ValueError("invalid expectation schema")
        evidence = _exact_list(data["candidate_evidence_hashes"])
        scopes = _exact_list(data["write_scope"])
        return cls(
            workflow_id=_string(data["workflow_id"]),
            run_id=_string(data["run_id"]),
            plan_hash=_string(data["plan_hash"]),
            workspace_id=_string(data["workspace_id"]),
            task_id=_string(data["task_id"]),
            task_version=_integer(data["task_version"]),
            originating_epoch=_integer(data["originating_epoch"]),
            sol_scope=_string(data["sol_scope"]),
            candidate_id=_string(data["candidate_id"]),
            candidate_base_commit=_string(data["candidate_base_commit"]),
            candidate_head_commit=_string(data["candidate_head_commit"]),
            candidate_diff_hash=_string(data["candidate_diff_hash"]),
            candidate_evidence_hashes=tuple(_string(item) for item in evidence),
            review_digest=_string(data["review_digest"]),
            predecessor_integration_head=_string(data["predecessor_integration_head"]),
            predecessor_integration_version=_integer(
                data["predecessor_integration_version"]
            ),
            write_scope=tuple(IntegrationScopeEntry.from_dict(item) for item in scopes),
        )


@dataclass(frozen=True)
class IntegrationProofReceipt:
    """Immutable host-attested Git truth receipt; its hash is the proof ID."""

    expectation: IntegrationExpectation
    object_format: str
    repository_id: str
    integration_ref: str
    predecessor_commit: str
    candidate_head_commit: str
    candidate_commits: tuple[str, ...]
    final_commit: str
    predecessor_tree: str
    candidate_tree: str
    final_tree: str
    final_parent_commit: str
    ref_before_commit: str
    ref_after_commit: str
    candidate_delta: tuple[IntegrationDeltaEntry, ...]
    final_delta: tuple[IntegrationDeltaEntry, ...]
    candidate_delta_hash: str
    final_delta_hash: str
    merge_free: bool
    linear_ancestry: bool
    attestor_id: str
    attestor_version: str

    @classmethod
    def create(
        cls,
        *,
        expectation: IntegrationExpectation,
        object_format: str,
        repository_id: str,
        integration_ref: str,
        predecessor_commit: str,
        candidate_head_commit: str,
        candidate_commits: tuple[str, ...],
        final_commit: str,
        predecessor_tree: str,
        candidate_tree: str,
        final_tree: str,
        final_parent_commit: str,
        ref_before_commit: str,
        ref_after_commit: str,
        candidate_delta: tuple[IntegrationDeltaEntry, ...],
        final_delta: tuple[IntegrationDeltaEntry, ...],
        merge_free: bool,
        linear_ancestry: bool,
        attestor_id: str,
        attestor_version: str,
    ) -> IntegrationProofReceipt:
        return cls(
            expectation=expectation,
            object_format=object_format,
            repository_id=repository_id,
            integration_ref=integration_ref,
            predecessor_commit=predecessor_commit,
            candidate_head_commit=candidate_head_commit,
            candidate_commits=candidate_commits,
            final_commit=final_commit,
            predecessor_tree=predecessor_tree,
            candidate_tree=candidate_tree,
            final_tree=final_tree,
            final_parent_commit=final_parent_commit,
            ref_before_commit=ref_before_commit,
            ref_after_commit=ref_after_commit,
            candidate_delta=candidate_delta,
            final_delta=final_delta,
            candidate_delta_hash=_delta_hash(candidate_delta),
            final_delta_hash=_delta_hash(final_delta),
            merge_free=merge_free,
            linear_ancestry=linear_ancestry,
            attestor_id=attestor_id,
            attestor_version=attestor_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "expectation": self.expectation.to_dict(),
            "expectation_hash": self.expectation.expectation_hash,
            "object_format": self.object_format,
            "repository_id": self.repository_id,
            "integration_ref": self.integration_ref,
            "predecessor_commit": self.predecessor_commit,
            "candidate_head_commit": self.candidate_head_commit,
            "candidate_commits": list(self.candidate_commits),
            "final_commit": self.final_commit,
            "predecessor_tree": self.predecessor_tree,
            "candidate_tree": self.candidate_tree,
            "final_tree": self.final_tree,
            "final_parent_commit": self.final_parent_commit,
            "ref_before_commit": self.ref_before_commit,
            "ref_after_commit": self.ref_after_commit,
            "candidate_delta": [entry.to_dict() for entry in self.candidate_delta],
            "candidate_delta_hash": self.candidate_delta_hash,
            "final_delta": [entry.to_dict() for entry in self.final_delta],
            "final_delta_hash": self.final_delta_hash,
            "merge_free": self.merge_free,
            "linear_ancestry": self.linear_ancestry,
            "attestor_id": self.attestor_id,
            "attestor_version": self.attestor_version,
        }

    @property
    def proof_id(self) -> str:
        return canonical_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> IntegrationProofReceipt:
        fields = {
            "schema",
            "expectation",
            "expectation_hash",
            "object_format",
            "repository_id",
            "integration_ref",
            "predecessor_commit",
            "candidate_head_commit",
            "candidate_commits",
            "final_commit",
            "predecessor_tree",
            "candidate_tree",
            "final_tree",
            "final_parent_commit",
            "ref_before_commit",
            "ref_after_commit",
            "candidate_delta",
            "candidate_delta_hash",
            "final_delta",
            "final_delta_hash",
            "merge_free",
            "linear_ancestry",
            "attestor_id",
            "attestor_version",
        }
        data = _exact_dict(value, fields)
        if data["schema"] != _RECEIPT_SCHEMA:
            raise ValueError("invalid receipt schema")
        expectation = IntegrationExpectation.from_dict(data["expectation"])
        if data["expectation_hash"] != expectation.expectation_hash:
            raise ValueError("invalid expectation hash")
        candidate_commits = _exact_list(data["candidate_commits"])
        candidate_delta = _exact_list(data["candidate_delta"])
        final_delta = _exact_list(data["final_delta"])
        return cls(
            expectation=expectation,
            object_format=_string(data["object_format"]),
            repository_id=_string(data["repository_id"]),
            integration_ref=_string(data["integration_ref"]),
            predecessor_commit=_string(data["predecessor_commit"]),
            candidate_head_commit=_string(data["candidate_head_commit"]),
            candidate_commits=tuple(_string(item) for item in candidate_commits),
            final_commit=_string(data["final_commit"]),
            predecessor_tree=_string(data["predecessor_tree"]),
            candidate_tree=_string(data["candidate_tree"]),
            final_tree=_string(data["final_tree"]),
            final_parent_commit=_string(data["final_parent_commit"]),
            ref_before_commit=_string(data["ref_before_commit"]),
            ref_after_commit=_string(data["ref_after_commit"]),
            candidate_delta=tuple(
                IntegrationDeltaEntry.from_dict(item) for item in candidate_delta
            ),
            final_delta=tuple(
                IntegrationDeltaEntry.from_dict(item) for item in final_delta
            ),
            candidate_delta_hash=_string(data["candidate_delta_hash"]),
            final_delta_hash=_string(data["final_delta_hash"]),
            merge_free=_boolean(data["merge_free"]),
            linear_ancestry=_boolean(data["linear_ancestry"]),
            attestor_id=_string(data["attestor_id"]),
            attestor_version=_string(data["attestor_version"]),
        )


def validate_integration_proof(
    proof_id: str,
    expectation: IntegrationExpectation,
    receipt: IntegrationProofReceipt,
) -> None:
    """Fail closed unless a receipt is an exact, bounded integration proof."""

    if (
        type(proof_id) is not str
        or _DIGEST.fullmatch(proof_id) is None
        or type(expectation) is not IntegrationExpectation
        or type(receipt) is not IntegrationProofReceipt
    ):
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID")
    try:
        _validate_expectation(expectation)
        if (
            type(receipt.expectation) is not IntegrationExpectation
            or type(receipt.object_format) is not str
            or type(receipt.candidate_commits) is not tuple
            or type(receipt.candidate_delta) is not tuple
            or type(receipt.final_delta) is not tuple
            or len(receipt.candidate_commits) > 256
            or len(receipt.candidate_delta) > 4096
            or len(receipt.final_delta) > 4096
        ):
            raise TypeError
        computed_proof_id = receipt.proof_id
    except (AttributeError, OverflowError, TypeError, ValueError, UnicodeError):
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID") from None
    if computed_proof_id != proof_id:
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID")
    if receipt.expectation != expectation:
        raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")

    object_length = {"sha1": 40, "sha256": 64}.get(receipt.object_format)
    if object_length is None:
        raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
    if (
        not _valid_digest(receipt.repository_id)
        or not _valid_digest(receipt.candidate_delta_hash)
        or not _valid_digest(receipt.final_delta_hash)
        or not _valid_ref(receipt.integration_ref)
        or not _valid_token(receipt.attestor_id)
        or not _valid_token(receipt.attestor_version)
    ):
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID")

    object_ids = (
        expectation.candidate_base_commit,
        expectation.candidate_head_commit,
        expectation.predecessor_integration_head,
        receipt.predecessor_commit,
        receipt.candidate_head_commit,
        *receipt.candidate_commits,
        receipt.final_commit,
        receipt.predecessor_tree,
        receipt.candidate_tree,
        receipt.final_tree,
        receipt.final_parent_commit,
        receipt.ref_before_commit,
        receipt.ref_after_commit,
    )
    if any(not _valid_oid(value, object_length) for value in object_ids):
        raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")

    if (
        receipt.predecessor_commit != expectation.predecessor_integration_head
        or receipt.ref_before_commit != expectation.predecessor_integration_head
        or expectation.candidate_base_commit != expectation.predecessor_integration_head
    ):
        raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
    if receipt.candidate_head_commit != expectation.candidate_head_commit:
        raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
    if (
        type(receipt.merge_free) is not bool
        or type(receipt.linear_ancestry) is not bool
        or not receipt.merge_free
        or not receipt.linear_ancestry
        or not receipt.candidate_commits
        or len(receipt.candidate_commits) > 256
        or receipt.candidate_commits[-1] != receipt.candidate_head_commit
        or len(set(receipt.candidate_commits)) != len(receipt.candidate_commits)
        or receipt.final_parent_commit != receipt.predecessor_commit
        or receipt.final_commit == receipt.predecessor_commit
    ):
        raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
    if receipt.ref_after_commit != receipt.final_commit:
        raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
    if (
        receipt.candidate_tree != receipt.final_tree
        or receipt.predecessor_tree == receipt.final_tree
        or not receipt.candidate_delta
        or len(receipt.candidate_delta) > 4096
        or receipt.candidate_delta != receipt.final_delta
        or receipt.candidate_delta_hash != _delta_hash(receipt.candidate_delta)
        or receipt.final_delta_hash != _delta_hash(receipt.final_delta)
    ):
        raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")

    _validate_delta(receipt.candidate_delta, object_length, expectation.write_scope)


def _validate_expectation(expectation: IntegrationExpectation) -> None:
    if (
        not _valid_identifier(expectation.workflow_id)
        or not _valid_identifier(expectation.run_id)
        or not _valid_digest(expectation.plan_hash)
        or not _valid_digest(expectation.workspace_id)
        or not _valid_identifier(expectation.task_id)
        or type(expectation.task_version) is not int
        or expectation.task_version < 1
        or type(expectation.originating_epoch) is not int
        or expectation.originating_epoch < 1
        or expectation.sol_scope != "sol:integrate"
        or not _valid_identifier(expectation.candidate_id)
        or not _valid_digest(expectation.candidate_diff_hash)
        or not _valid_digest(expectation.review_digest)
        or type(expectation.predecessor_integration_version) is not int
        or expectation.predecessor_integration_version < 0
        or type(expectation.candidate_evidence_hashes) is not tuple
        or len(expectation.candidate_evidence_hashes) > 32
        or tuple(sorted(expectation.candidate_evidence_hashes))
        != expectation.candidate_evidence_hashes
        or len(set(expectation.candidate_evidence_hashes))
        != len(expectation.candidate_evidence_hashes)
        or any(
            not _valid_digest(item) for item in expectation.candidate_evidence_hashes
        )
        or type(expectation.write_scope) is not tuple
        or not expectation.write_scope
        or len(expectation.write_scope) > 32
    ):
        raise ValueError("invalid expectation")
    _validate_scope_entries(expectation.write_scope)


def _validate_scope_entries(entries: tuple[IntegrationScopeEntry, ...]) -> None:
    if any(type(entry) is not IntegrationScopeEntry for entry in entries):
        raise ValueError("invalid scope")
    if (
        tuple(sorted(entries, key=lambda item: (item.path.encode("utf-8"), item.kind)))
        != entries
    ):
        raise ValueError("noncanonical scope")
    aliases: set[str] = set()
    for entry in entries:
        if entry.kind not in {"file", "tree"} or not _valid_path(entry.path):
            raise ValueError("invalid scope")
        alias = entry.path.casefold()
        if alias in aliases:
            raise ValueError("scope alias")
        aliases.add(alias)


def _validate_delta(
    entries: tuple[IntegrationDeltaEntry, ...],
    object_length: int,
    scope: tuple[IntegrationScopeEntry, ...],
) -> None:
    if any(type(entry) is not IntegrationDeltaEntry for entry in entries):
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID")
    try:
        ordered = tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
    except UnicodeError:
        raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH") from None
    if ordered != entries:
        raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH")
    aliases: set[str] = set()
    for entry in entries:
        if not _valid_path(entry.path):
            raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH")
        alias = entry.path.casefold()
        if alias in aliases:
            raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH")
        aliases.add(alias)
        if not any(_scope_covers(item, entry.path) for item in scope):
            raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH")
        if not _valid_delta_side(
            entry.old_oid, entry.old_mode, entry.old_type, object_length
        ) or not _valid_delta_side(
            entry.new_oid, entry.new_mode, entry.new_type, object_length
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        if entry.old_oid is None and entry.new_oid is None:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        if (
            entry.old_oid == entry.new_oid
            and entry.old_mode == entry.new_mode
            and entry.old_type == entry.new_type
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")


def _scope_covers(scope: IntegrationScopeEntry, path: str) -> bool:
    if scope.kind == "file":
        return path == scope.path
    return path == scope.path or path.startswith(scope.path + "/")


def _valid_delta_side(
    oid: str | None, mode: str | None, object_type: str | None, object_length: int
) -> bool:
    if oid is None or mode is None or object_type is None:
        return oid is None and mode is None and object_type is None
    return (
        _valid_oid(oid, object_length)
        and object_type in _MODES_BY_TYPE
        and mode in _MODES_BY_TYPE[object_type]
    )


def _valid_path(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 4096:
        return False
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return (
        len(encoded) <= 4096
        and unicodedata.normalize("NFC", value) == value
        and not value.startswith(("/", "~"))
        and _DRIVE_PATH.match(value) is None
        and "\\" not in value
        and "\x00" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _valid_ref(value: object) -> bool:
    if type(value) is not str or len(value) > 512 or not value.startswith("refs/"):
        return False
    parts = value.split("/")
    if (
        not _valid_utf8(value)
        or unicodedata.normalize("NFC", value) != value
        or "\\" in value
        or ".." in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
        or any(character in " ~^:?*[" for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    return all(
        part not in {"", ".", ".."}
        and not part.startswith(".")
        and not part.endswith(".lock")
        for part in parts
    )


def _valid_oid(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and value != "0" * length
        and re.fullmatch(r"[0-9a-f]+", value) is not None
    )


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _valid_identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _valid_token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _valid_utf8(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def _delta_hash(entries: tuple[IntegrationDeltaEntry, ...]) -> str:
    return canonical_hash(
        {"schema": _DELTA_SCHEMA, "entries": [entry.to_dict() for entry in entries]}
    )


def _exact_dict(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid object fields")
    return value


def _exact_list(value: object) -> list[object]:
    if type(value) is not list:
        raise ValueError("invalid list")
    return value


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("invalid string")
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and type(value) is not str:
        raise ValueError("invalid optional string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("invalid integer")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid boolean")
    return value
