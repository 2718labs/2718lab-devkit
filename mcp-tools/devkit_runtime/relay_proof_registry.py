"""Host-private durable Git proof registry for Relay integration.

Relay itself deliberately only sees the typed ``IntegrationProofResolver``
protocol.  This module owns the private repository target, Git subprocesses,
full receipts, and the registry ledger; none of those values are returned from
the public Relay status/result boundary.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from devkit_relay.canonical import canonical_hash
from devkit_relay.proofs import (
    IntegrationDeltaEntry,
    IntegrationExpectation,
    IntegrationProofError,
    IntegrationProofReceipt,
    IntegrationProofResolver,
    ProofFinalizationEvidence,
    ProofFinalizationFence,
    RelayFinalizationAuthority,
    RelayProofReservation,
    Settlement,
    validate_finalization_evidence,
    validate_finalization_fence,
    validate_integration_proof,
)

_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REF: Final = re.compile(r"refs/[A-Za-z0-9][A-Za-z0-9._/-]{0,510}\Z")
_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}\Z")
_OID: Final = re.compile(r"[0-9a-f]+\Z")
_DELTA_SCHEMA: Final = "2718lab-devkit/relay-integration-tree-delta-v1"
_TARGET_SCHEMA: Final = "2718lab-devkit/relay-proof-target-v1"
_MAX_GIT_SECONDS: Final = 15
_REGISTRY_SCHEMA_VERSION: Final = 2
_REGISTRY_COLUMNS: Final = (
    "proof_id",
    "expectation_hash",
    "target_key",
    "receipt_json",
    "state",
    "reservation_epoch",
    "finalization_id",
    "fence_json",
    "fence_hash",
    "settlement_result_hash",
)
_REGISTRY_EVENT_COLUMNS: Final = (
    "event_id",
    "proof_id",
    "reservation_epoch",
    "event_kind",
    "fence_hash",
    "result_hash",
)
_REGISTRY_TABLE_SQL: Final = """
CREATE TABLE relay_host_integration_proofs (
    proof_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(proof_id) = 71
        AND substr(proof_id, 1, 7) = 'sha256:'
        AND substr(proof_id, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    expectation_hash TEXT NOT NULL UNIQUE CHECK (
        length(expectation_hash) = 71
        AND substr(expectation_hash, 1, 7) = 'sha256:'
        AND substr(expectation_hash, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    target_key TEXT NOT NULL CHECK (
        length(target_key) = 71
        AND substr(target_key, 1, 7) = 'sha256:'
        AND substr(target_key, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
    state TEXT NOT NULL CHECK (state IN ('registered', 'reserved', 'consumed')),
    reservation_epoch INTEGER NOT NULL DEFAULT 0 CHECK (reservation_epoch >= 0),
    finalization_id TEXT CHECK (
        finalization_id IS NULL OR (
            length(finalization_id) = 32
            AND finalization_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    fence_json TEXT CHECK (fence_json IS NULL OR json_valid(fence_json)),
    fence_hash TEXT CHECK (
        fence_hash IS NULL OR (
            length(fence_hash) = 71
            AND substr(fence_hash, 1, 7) = 'sha256:'
            AND substr(fence_hash, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    settlement_result_hash TEXT CHECK (
        settlement_result_hash IS NULL OR (
            length(settlement_result_hash) = 71
            AND substr(settlement_result_hash, 1, 7) = 'sha256:'
            AND substr(settlement_result_hash, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        (state = 'registered' AND finalization_id IS NULL AND fence_json IS NULL
         AND fence_hash IS NULL AND settlement_result_hash IS NULL)
        OR (state = 'reserved' AND reservation_epoch > 0
            AND finalization_id IS NOT NULL AND fence_json IS NOT NULL
            AND fence_hash IS NOT NULL AND settlement_result_hash IS NULL)
        OR (state = 'consumed' AND reservation_epoch > 0
            AND finalization_id IS NOT NULL AND fence_json IS NOT NULL
            AND fence_hash IS NOT NULL AND settlement_result_hash IS NOT NULL)
    )
) STRICT
"""
_REGISTRY_TABLE_CREATE_SQL: Final = _REGISTRY_TABLE_SQL.replace(
    "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1
)
_REGISTRY_INDEX_SQL: Final = """
CREATE INDEX relay_host_proof_target_state
ON relay_host_integration_proofs(target_key, state)
"""
_REGISTRY_INDEX_CREATE_SQL: Final = _REGISTRY_INDEX_SQL.replace(
    "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
)
_REGISTRY_ACTIVE_INDEX_SQL: Final = """
CREATE UNIQUE INDEX relay_host_proof_active_target
ON relay_host_integration_proofs(target_key)
WHERE state IN ('registered', 'reserved')
"""
_REGISTRY_ACTIVE_INDEX_CREATE_SQL: Final = _REGISTRY_ACTIVE_INDEX_SQL.replace(
    "CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1
)
_REGISTRY_PENDING_INDEX_SQL: Final = """
CREATE INDEX relay_host_proof_pending
ON relay_host_integration_proofs(state, proof_id)
"""
_REGISTRY_PENDING_INDEX_CREATE_SQL: Final = _REGISTRY_PENDING_INDEX_SQL.replace(
    "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
)
_REGISTRY_EVENTS_TABLE_SQL: Final = """
CREATE TABLE relay_host_proof_events (
    event_id INTEGER PRIMARY KEY,
    proof_id TEXT NOT NULL CHECK (
        length(proof_id) = 71
        AND substr(proof_id, 1, 7) = 'sha256:'
        AND substr(proof_id, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    reservation_epoch INTEGER NOT NULL CHECK (reservation_epoch > 0),
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'reserved', 'git_apply_intent', 'git_applied_observed',
        'settle_committed', 'consumed', 'settle_aborted',
        'git_reverted_observed', 'released'
    )),
    fence_hash TEXT CHECK (
        fence_hash IS NULL OR (
            length(fence_hash) = 71
            AND substr(fence_hash, 1, 7) = 'sha256:'
            AND substr(fence_hash, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    result_hash TEXT CHECK (
        result_hash IS NULL OR (
            length(result_hash) = 71
            AND substr(result_hash, 1, 7) = 'sha256:'
            AND substr(result_hash, 8) NOT GLOB '*[^0-9a-f]*'
        )
    )
) STRICT
"""
_REGISTRY_EVENTS_TABLE_CREATE_SQL: Final = _REGISTRY_EVENTS_TABLE_SQL.replace(
    "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1
)
_REGISTRY_EVENT_INDEX_SQL: Final = """
CREATE INDEX relay_host_proof_event_epoch
ON relay_host_proof_events(proof_id, reservation_epoch, event_id)
"""
_REGISTRY_EVENT_INDEX_CREATE_SQL: Final = _REGISTRY_EVENT_INDEX_SQL.replace(
    "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
)


@dataclass(frozen=True, repr=False)
class GitProofTarget:
    """A host-configured repository/ref target, never caller supplied.

    ``repository`` is intentionally omitted from representations.  The opaque
    ``repository_id`` must be provisioned by the host configuration rather than
    derived from a path supplied by Relay.
    """

    repository: Path = field(repr=False)
    repository_id: str
    integration_ref: str
    attestor_id: str
    attestor_version: str

    def __post_init__(self) -> None:
        repository = Path(self.repository)
        if (
            not repository.is_absolute()
            or not _valid_digest(self.repository_id)
            or not _valid_ref(self.integration_ref)
            or not _valid_token(self.attestor_id)
            or not _valid_token(self.attestor_version)
        ):
            raise ValueError("invalid Git proof target")
        object.__setattr__(self, "repository", repository.resolve())

    def __repr__(self) -> str:
        return (
            "GitProofTarget("
            f"repository_id={self.repository_id!r}, "
            f"integration_ref={self.integration_ref!r}, "
            f"attestor_id={self.attestor_id!r}, "
            f"attestor_version={self.attestor_version!r})"
        )


class ControlledGitTargetResolver:
    """Resolve an opaque workspace only through an allowlisted host mapping."""

    def __init__(self, targets: Mapping[str, GitProofTarget]) -> None:
        normalized: dict[str, GitProofTarget] = {}
        repository_id_to_physical: dict[str, tuple[Path, str]] = {}
        physical_to_target: dict[tuple[Path, str], GitProofTarget] = {}
        for workspace_id, target in targets.items():
            if not _valid_digest(workspace_id) or type(target) is not GitProofTarget:
                raise ValueError("invalid controlled Git target")
            physical = (target.repository, target.integration_ref)
            previous_physical = repository_id_to_physical.get(target.repository_id)
            previous_target = physical_to_target.get(physical)
            if (
                (previous_physical is not None and previous_physical != physical)
                or (previous_target is not None and previous_target != target)
            ):
                raise ValueError("invalid controlled Git target")
            repository_id_to_physical[target.repository_id] = physical
            physical_to_target[physical] = target
            normalized[workspace_id] = target
        self._targets = normalized

    def resolve(self, workspace_id: str) -> GitProofTarget:
        target = self._targets.get(workspace_id)
        if target is None:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")
        return target

    def __repr__(self) -> str:
        return f"ControlledGitTargetResolver(target_count={len(self._targets)})"


def bootstrap_relay_proof_registry(database_path: str | Path) -> None:
    """Create the private ledger only from the explicit runtime bootstrap path.

    Production composition must call this from ``RuntimeBootstrap``.  Opening
    ``RelayProofRegistry`` never creates a database, directory, table, or lock
    root, so read/write invocation construction stays fail-closed on an
    unprepared data root.
    """

    try:
        path = Path(database_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            schema_state = _registry_schema_state(path)
            if not path.is_file() or schema_state == "invalid":
                raise sqlite3.DatabaseError
            if schema_state == "valid":
                return
        connection = sqlite3.connect(path, isolation_level=None, timeout=1.0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_REGISTRY_TABLE_CREATE_SQL)
            connection.execute(_REGISTRY_INDEX_CREATE_SQL)
            connection.execute(_REGISTRY_ACTIVE_INDEX_CREATE_SQL)
            connection.execute(_REGISTRY_PENDING_INDEX_CREATE_SQL)
            connection.execute(_REGISTRY_EVENTS_TABLE_CREATE_SQL)
            connection.execute(_REGISTRY_EVENT_INDEX_CREATE_SQL)
            connection.execute(f"PRAGMA user_version = {_REGISTRY_SCHEMA_VERSION}")
            _assert_registry_schema(connection)
            connection.execute("COMMIT")
        except sqlite3.Error:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None


@dataclass(frozen=True)
class _StoredProof:
    proof_id: str
    expectation_hash: str
    target_key: str
    receipt: IntegrationProofReceipt
    state: str
    reservation_epoch: int
    fence: ProofFinalizationFence | None
    settlement_result_hash: str | None


class _RegistryReservation(RelayProofReservation):
    """A durable, fenced reservation with evidence-only terminal settlement."""

    def __init__(
        self,
        registry: RelayProofRegistry,
        *,
        proof_id: str,
        receipt: IntegrationProofReceipt,
        fence: ProofFinalizationFence,
    ) -> None:
        self._registry = registry
        self._proof_id = proof_id
        self._receipt = receipt
        self._fence = fence
        self._terminal_evidence: ProofFinalizationEvidence | None = None
        self._settlement: Settlement | None = None

    @property
    def receipt(self) -> IntegrationProofReceipt:
        return self._receipt

    @property
    def fence(self) -> ProofFinalizationFence:
        return self._fence

    def settle(self, *, evidence: ProofFinalizationEvidence) -> Settlement:
        self._registry._validate_terminal_evidence(self._fence, evidence)
        if self._terminal_evidence is not None:
            if evidence != self._terminal_evidence or self._settlement is None:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            if self._settlement == "consumed":
                return "already_consumed"
            return "already_released"
        settlement = self._registry._settle(self, evidence)
        self._terminal_evidence = evidence
        self._settlement = settlement
        return settlement


class RelayProofRegistry(IntegrationProofResolver):
    """Durable host-only proof ledger and concrete real-Git attestor."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        target_resolver: ControlledGitTargetResolver,
        git_executable: str = "git",
    ) -> None:
        if type(target_resolver) is not ControlledGitTargetResolver:
            raise ValueError("invalid proof target resolver")
        if type(git_executable) is not str or not git_executable:
            raise ValueError("invalid Git executable")
        try:
            path = Path(database_path).resolve()
            if not path.is_file():
                raise OSError
        except (OSError, TypeError, ValueError):
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        self._database_path = path
        self._target_resolver = target_resolver
        self._git_executable = git_executable
        self._active_lock = threading.RLock()
        self._active: dict[str, _RegistryReservation] = {}
        self._closed = False
        self._assert_schema()

    def __repr__(self) -> str:
        return "RelayProofRegistry()"

    def close(self) -> None:
        self._closed = True

    def attest_and_register(self, expectation: IntegrationExpectation) -> str:
        """Create a real Git receipt and durably register it before ref CAS."""

        self._ensure_open()
        self._require_expectation_instance(expectation)
        target = self._resolve_target(expectation.workspace_id)
        target_key = _target_key(expectation.workspace_id, target)
        existing = self._find_by_expectation(expectation.expectation_hash)
        if existing is not None:
            self._assert_record_matches_target(existing, expectation, target, target_key)
            self._verify_receipt(target, existing.receipt)
            return existing.proof_id
        if self._find_active_target(target_key) is not None:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")

        receipt = self._build_receipt(expectation, target)
        proof_id = receipt.proof_id
        self._insert_registered(proof_id, expectation.expectation_hash, target_key, receipt)
        self._verify_receipt(target, receipt)
        return proof_id

    def reserve(
        self, proof_id: str, expectation: IntegrationExpectation
    ) -> RelayProofReservation:
        """Persist a complete fence before the exact Git base-to-final CAS."""

        self._ensure_open()
        if not _valid_digest(proof_id):
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID")
        self._require_expectation_instance(expectation)
        initial = self._find_by_proof_id(proof_id)
        if initial is None:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_UNREGISTERED")
        if initial.receipt.expectation != expectation:
            raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
        target = self._resolve_target(expectation.workspace_id)
        target_key = _target_key(expectation.workspace_id, target)
        self._assert_record_matches_target(initial, expectation, target, target_key)
        self._verify_receipt(target, initial.receipt)
        reservation = self._persist_reservation(
            proof_id, expectation, target, target_key
        )
        try:
            self._apply_fence_ref(target, reservation.fence, forward=True)
            self._record_event(
                reservation.fence,
                "git_applied_observed",
                result_hash=None,
            )
        except IntegrationProofError:
            self._forget(reservation)
            raise
        return reservation

    def recover_finalizations(self, authority: RelayFinalizationAuthority) -> None:
        """Reconcile every persisted reservation only from Relay terminal evidence."""

        self._ensure_open()
        for record in self._pending_reservations():
            fence = record.fence
            if fence is None:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            evidence = authority.resolve_or_abort_finalization(fence=fence)
            if type(evidence) is not ProofFinalizationEvidence:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            reservation = _RegistryReservation(
                self,
                proof_id=record.proof_id,
                receipt=record.receipt,
                fence=fence,
            )
            settlement = reservation.settle(evidence=evidence)
            if settlement not in {"consumed", "released"}:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")

    def _persist_reservation(
        self,
        proof_id: str,
        expectation: IntegrationExpectation,
        target: GitProofTarget,
        target_key: str,
    ) -> _RegistryReservation:
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise _reservation_database_error(error) from None
            record = self._record_by_proof(connection, proof_id)
            if record is None:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_UNREGISTERED")
            if record.receipt.expectation != expectation:
                raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
            self._assert_record_matches_target(record, expectation, target, target_key)
            if record.state == "consumed":
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_REPLAY")
            if record.state == "reserved":
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")
            if record.state != "registered":
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            fence = self._new_fence(
                proof_id,
                expectation,
                target,
                record.receipt,
                record.reservation_epoch + 1,
            )
            fence_json = _canonical_json(fence.to_dict())
            cursor = connection.execute(
                """
                UPDATE relay_host_integration_proofs
                SET state = 'reserved', reservation_epoch = ?, finalization_id = ?,
                    fence_json = ?, fence_hash = ?, settlement_result_hash = NULL
                WHERE proof_id = ? AND state = 'registered'
                  AND reservation_epoch = ? AND finalization_id IS NULL
                """,
                (
                    fence.reservation_epoch,
                    fence.finalization_id,
                    fence_json,
                    fence.fence_hash,
                    proof_id,
                    record.reservation_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")
            self._append_event(
                connection,
                fence,
                "reserved",
                result_hash=None,
            )
            self._append_event(
                connection,
                fence,
                "git_apply_intent",
                result_hash=None,
            )
            connection.execute("COMMIT")
        except IntegrationProofError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise _reservation_database_error(error) from None
        finally:
            connection.close()
        reservation = _RegistryReservation(
            self,
            proof_id=proof_id,
            receipt=record.receipt,
            fence=fence,
        )
        with self._active_lock:
            self._active[proof_id] = reservation
        return reservation

    def _settle(
        self,
        reservation: _RegistryReservation,
        evidence: ProofFinalizationEvidence,
    ) -> Settlement:
        fence = reservation.fence
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise _reservation_database_error(error) from None
            record = self._record_by_proof(connection, reservation._proof_id)
            if record is None:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            if record.state == "consumed":
                if (
                    evidence.state != "committed"
                    or record.fence != fence
                    or record.settlement_result_hash != evidence.result_hash
                ):
                    raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
                connection.execute("COMMIT")
                return "already_consumed"
            if record.state == "registered":
                if (
                    evidence.state != "aborted"
                    or not self._released_event(connection, fence)
                ):
                    raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
                connection.execute("COMMIT")
                return "already_released"
            if record.state != "reserved" or record.fence != fence:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            target = self._resolve_target(fence.workspace_id)
            self._assert_fence_target(fence, target, record)
            self._verify_receipt(target, record.receipt)
            if evidence.state == "committed":
                self._apply_fence_ref(target, fence, forward=True)
                self._append_event(
                    connection,
                    fence,
                    "settle_committed",
                    result_hash=evidence.result_hash,
                )
                cursor = connection.execute(
                    """
                    UPDATE relay_host_integration_proofs
                    SET state = 'consumed', settlement_result_hash = ?
                    WHERE proof_id = ? AND state = 'reserved'
                      AND reservation_epoch = ? AND fence_hash = ?
                    """,
                    (
                        evidence.result_hash,
                        reservation._proof_id,
                        fence.reservation_epoch,
                        fence.fence_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
                self._append_event(
                    connection,
                    fence,
                    "consumed",
                    result_hash=evidence.result_hash,
                )
                connection.execute("COMMIT")
                self._forget(reservation)
                return "consumed"
            self._apply_fence_ref(target, fence, forward=False)
            self._append_event(
                connection,
                fence,
                "settle_aborted",
                result_hash=None,
            )
            self._append_event(
                connection,
                fence,
                "git_reverted_observed",
                result_hash=None,
            )
            cursor = connection.execute(
                """
                UPDATE relay_host_integration_proofs
                SET state = 'registered', finalization_id = NULL, fence_json = NULL,
                    fence_hash = NULL, settlement_result_hash = NULL
                WHERE proof_id = ? AND state = 'reserved'
                  AND reservation_epoch = ? AND fence_hash = ?
                """,
                (reservation._proof_id, fence.reservation_epoch, fence.fence_hash),
            )
            if cursor.rowcount != 1:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            self._append_event(
                connection,
                fence,
                "released",
                result_hash=None,
            )
            connection.execute("COMMIT")
            self._forget(reservation)
            return "released"
        except IntegrationProofError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise _reservation_database_error(error) from None
        finally:
            connection.close()

    @staticmethod
    def _validate_terminal_evidence(
        fence: ProofFinalizationFence, evidence: ProofFinalizationEvidence
    ) -> None:
        try:
            validate_finalization_evidence(evidence)
        except ValueError as error:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT") from error
        if (
            evidence.state not in {"committed", "aborted"}
            or evidence.finalization_id != fence.finalization_id
            or evidence.fence_hash != fence.fence_hash
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")

    def _new_fence(
        self,
        proof_id: str,
        expectation: IntegrationExpectation,
        target: GitProofTarget,
        receipt: IntegrationProofReceipt,
        reservation_epoch: int,
    ) -> ProofFinalizationFence:
        fence = ProofFinalizationFence(
            finalization_id=uuid.uuid4().hex,
            reservation_epoch=reservation_epoch,
            integration_proof_id=proof_id,
            workspace_id=expectation.workspace_id,
            expectation_key=expectation.candidate_id,
            expectation_version=expectation.task_version,
            expectation_hash=expectation.expectation_hash,
            target_ref=target.integration_ref,
            base_oid=receipt.predecessor_commit,
            final_oid=receipt.final_commit,
        )
        try:
            validate_finalization_fence(fence)
        except ValueError as error:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT") from error
        return fence

    def _apply_fence_ref(
        self, target: GitProofTarget, fence: ProofFinalizationFence, *, forward: bool
    ) -> None:
        self._assert_fence_target(fence, target, None)
        source, destination = (
            (fence.base_oid, fence.final_oid)
            if forward
            else (fence.final_oid, fence.base_oid)
        )
        current = self._fence_ref_oid(target, fence)
        if current == destination:
            return
        if current != source:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
        self._git(
            target,
            "update-ref",
            fence.target_ref,
            destination,
            source,
            code="RELAY_INTEGRATION_PROOF_CORRUPT",
        )
        if self._fence_ref_oid(target, fence) != destination:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")

    def _fence_ref_oid(
        self, target: GitProofTarget, fence: ProofFinalizationFence
    ) -> str:
        symbolic = self._git(
            target,
            "for-each-ref",
            "--format=%(symref)",
            fence.target_ref,
            code="RELAY_INTEGRATION_PROOF_CORRUPT",
        )
        if symbolic.strip():
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
        return self._ref_oid(
            target,
            fence.target_ref,
            code="RELAY_INTEGRATION_PROOF_CORRUPT",
        )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _forget(self, reservation: _RegistryReservation) -> None:
        with self._active_lock:
            current = self._active.get(reservation._proof_id)
            if current is reservation:
                self._active.pop(reservation._proof_id, None)

    def _record_by_proof(
        self, connection: sqlite3.Connection, proof_id: str
    ) -> _StoredProof | None:
        row = connection.execute(
            """
            SELECT proof_id, expectation_hash, target_key, receipt_json, state,
                   reservation_epoch, finalization_id, fence_json, fence_hash,
                   settlement_result_hash
            FROM relay_host_integration_proofs
            WHERE proof_id = ?
            """,
            (proof_id,),
        ).fetchone()
        return None if row is None else self._decode_record(row)

    def _pending_reservations(self) -> tuple[_StoredProof, ...]:
        try:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT proof_id, expectation_hash, target_key, receipt_json, state,
                           reservation_epoch, finalization_id, fence_json, fence_hash,
                           settlement_result_hash
                    FROM relay_host_integration_proofs
                    WHERE state = 'reserved'
                    ORDER BY proof_id
                    """
                ).fetchall()
            finally:
                connection.close()
        except IntegrationProofError:
            raise
        except sqlite3.Error:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        return tuple(self._decode_record(row) for row in rows)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        fence: ProofFinalizationFence,
        event_kind: str,
        *,
        result_hash: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO relay_host_proof_events
                (proof_id, reservation_epoch, event_kind, fence_hash, result_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                fence.integration_proof_id,
                fence.reservation_epoch,
                event_kind,
                fence.fence_hash,
                result_hash,
            ),
        )

    def _record_event(
        self,
        fence: ProofFinalizationFence,
        event_kind: str,
        *,
        result_hash: str | None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._record_by_proof(connection, fence.integration_proof_id)
            if record is None or record.fence != fence:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            self._append_event(connection, fence, event_kind, result_hash=result_hash)
            connection.execute("COMMIT")
        except IntegrationProofError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise _reservation_database_error(error) from None
        finally:
            connection.close()

    @staticmethod
    def _released_event(
        connection: sqlite3.Connection, fence: ProofFinalizationFence
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM relay_host_proof_events
            WHERE proof_id = ? AND reservation_epoch = ? AND fence_hash = ?
              AND event_kind = 'released'
            """,
            (
                fence.integration_proof_id,
                fence.reservation_epoch,
                fence.fence_hash,
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def _assert_fence_target(
        fence: ProofFinalizationFence,
        target: GitProofTarget,
        record: _StoredProof | None,
    ) -> None:
        if (
            fence.target_ref != target.integration_ref
            or (record is not None and record.target_key != _target_key(fence.workspace_id, target))
            or (record is not None and record.receipt.integration_ref != fence.target_ref)
            or (record is not None and record.receipt.predecessor_commit != fence.base_oid)
            or (record is not None and record.receipt.final_commit != fence.final_oid)
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")

    def _assert_schema(self) -> None:
        try:
            connection = _readonly_registry_connection(self._database_path)
            try:
                _assert_registry_schema(connection)
            finally:
                connection.close()
        except sqlite3.Error:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                f"{self._database_path.as_uri()}?mode=rw",
                uri=True,
                isolation_level=None,
                timeout=1.0,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 1000")
            return connection
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None

    def _ensure_open(self) -> None:
        if self._closed:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")

    @staticmethod
    def _require_expectation_instance(expectation: object) -> None:
        if type(expectation) is not IntegrationExpectation:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID")

    def _resolve_target(self, workspace_id: str) -> GitProofTarget:
        try:
            return self._target_resolver.resolve(workspace_id)
        except IntegrationProofError:
            raise
        except Exception:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None

    def _find_by_proof_id(self, proof_id: str) -> _StoredProof | None:
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT proof_id, expectation_hash, target_key, receipt_json, state,
                           reservation_epoch, finalization_id, fence_json, fence_hash,
                           settlement_result_hash
                    FROM relay_host_integration_proofs
                    WHERE proof_id = ?
                    """,
                    (proof_id,),
                ).fetchone()
            finally:
                connection.close()
        except IntegrationProofError:
            raise
        except sqlite3.Error:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        return None if row is None else self._decode_record(row)

    def _find_by_expectation(self, expectation_hash: str) -> _StoredProof | None:
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT proof_id, expectation_hash, target_key, receipt_json, state,
                           reservation_epoch, finalization_id, fence_json, fence_hash,
                           settlement_result_hash
                    FROM relay_host_integration_proofs
                    WHERE expectation_hash = ?
                    """,
                    (expectation_hash,),
                ).fetchone()
            finally:
                connection.close()
        except IntegrationProofError:
            raise
        except sqlite3.Error:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        return None if row is None else self._decode_record(row)

    def _find_active_target(self, target_key: str) -> _StoredProof | None:
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT proof_id, expectation_hash, target_key, receipt_json, state,
                           reservation_epoch, finalization_id, fence_json, fence_hash,
                           settlement_result_hash
                    FROM relay_host_integration_proofs
                    WHERE target_key = ? AND state IN ('registered', 'reserved')
                    LIMIT 1
                    """,
                    (target_key,),
                ).fetchone()
            finally:
                connection.close()
        except IntegrationProofError:
            raise
        except sqlite3.Error:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        return None if row is None else self._decode_record(row)

    def _decode_record(self, row: sqlite3.Row) -> _StoredProof:
        try:
            proof_id = row["proof_id"]
            expectation_hash = row["expectation_hash"]
            target_key = row["target_key"]
            state = row["state"]
            reservation_epoch = row["reservation_epoch"]
            finalization_id = row["finalization_id"]
            fence_json = row["fence_json"]
            fence_hash = row["fence_hash"]
            settlement_result_hash = row["settlement_result_hash"]
            if (
                not _valid_digest(proof_id)
                or not _valid_digest(expectation_hash)
                or not _valid_digest(target_key)
                or state not in {"registered", "reserved", "consumed"}
                or type(reservation_epoch) is not int
                or reservation_epoch < 0
            ):
                raise ValueError
            receipt_json = row["receipt_json"]
            if type(receipt_json) is not str:
                raise ValueError
            raw_receipt = json.loads(
                receipt_json, object_pairs_hook=_json_object_without_duplicates
            )
            if _canonical_json(raw_receipt) != receipt_json:
                raise ValueError
            receipt = IntegrationProofReceipt.from_dict(raw_receipt)
            validate_integration_proof(proof_id, receipt.expectation, receipt)
            if receipt.expectation.expectation_hash != expectation_hash:
                raise ValueError
            fence: ProofFinalizationFence | None = None
            if state == "registered":
                if any(
                    value is not None
                    for value in (
                        finalization_id,
                        fence_json,
                        fence_hash,
                        settlement_result_hash,
                    )
                ):
                    raise ValueError
            else:
                if (
                    reservation_epoch < 1
                    or type(finalization_id) is not str
                    or type(fence_json) is not str
                    or type(fence_hash) is not str
                ):
                    raise ValueError
                raw_fence = json.loads(
                    fence_json, object_pairs_hook=_json_object_without_duplicates
                )
                if not isinstance(raw_fence, dict) or _canonical_json(raw_fence) != fence_json:
                    raise ValueError
                fence = ProofFinalizationFence(**raw_fence)
                validate_finalization_fence(fence)
                if (
                    fence.finalization_id != finalization_id
                    or fence.reservation_epoch != reservation_epoch
                    or fence.integration_proof_id != proof_id
                    or fence.fence_hash != fence_hash
                ):
                    raise ValueError
                if state == "consumed":
                    if not _valid_digest(settlement_result_hash):
                        raise ValueError
                elif settlement_result_hash is not None:
                    raise ValueError
            return _StoredProof(
                proof_id=proof_id,
                expectation_hash=expectation_hash,
                target_key=target_key,
                receipt=receipt,
                state=state,
                reservation_epoch=reservation_epoch,
                fence=fence,
                settlement_result_hash=settlement_result_hash,
            )
        except (
            IntegrationProofError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT") from None

    def _assert_record_matches_target(
        self,
        record: _StoredProof,
        expectation: IntegrationExpectation,
        target: GitProofTarget,
        target_key: str,
    ) -> None:
        receipt = record.receipt
        if (
            record.expectation_hash != expectation.expectation_hash
            or record.target_key != target_key
            or receipt.repository_id != target.repository_id
            or receipt.integration_ref != target.integration_ref
            or receipt.attestor_id != target.attestor_id
            or receipt.attestor_version != target.attestor_version
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")

    def _insert_registered(
        self,
        proof_id: str,
        expectation_hash: str,
        target_key: str,
        receipt: IntegrationProofReceipt,
    ) -> None:
        try:
            receipt_json = _canonical_json(receipt.to_dict())
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO relay_host_integration_proofs
                    (proof_id, expectation_hash, target_key, receipt_json, state)
                    VALUES (?, ?, ?, ?, 'registered')
                    """,
                    (proof_id, expectation_hash, target_key, receipt_json),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_REPLAY") from None
            except sqlite3.Error:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise IntegrationProofError(
                    "RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE"
                ) from None
            finally:
                connection.close()
        except IntegrationProofError:
            raise

    def _build_receipt(
        self, expectation: IntegrationExpectation, target: GitProofTarget
    ) -> IntegrationProofReceipt:
        object_format = self._object_format(target)
        object_length = _object_length(object_format)
        if object_length is None:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        self._require_complete_history(target)
        self._validate_expected_git_inputs(expectation, object_length)
        predecessor = expectation.predecessor_integration_head
        candidate = expectation.candidate_head_commit
        if self._ref_oid(target, target.integration_ref) != predecessor:
            raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
        self._require_object_type(target, predecessor, "commit")
        self._require_object_type(target, candidate, "commit")
        commits = self._linear_commits(target, predecessor, candidate, object_length)
        predecessor_tree = self._tree_oid(target, predecessor, object_length)
        candidate_tree = self._tree_oid(target, candidate, object_length)
        candidate_delta = self._delta(target, predecessor, candidate, object_length)
        if not candidate_delta:
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
        if expectation.candidate_diff_hash != _delta_hash(candidate_delta):
            raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
        final_commit = self._commit_tree(target, candidate_tree, predecessor, object_length)
        self._require_object_type(target, final_commit, "commit")
        final_tree = self._tree_oid(target, final_commit, object_length)
        final_delta = self._delta(target, predecessor, final_commit, object_length)
        receipt = IntegrationProofReceipt.create(
            expectation=expectation,
            object_format=object_format,
            repository_id=target.repository_id,
            integration_ref=target.integration_ref,
            predecessor_commit=predecessor,
            candidate_head_commit=candidate,
            candidate_commits=commits,
            final_commit=final_commit,
            predecessor_tree=predecessor_tree,
            candidate_tree=candidate_tree,
            final_tree=final_tree,
            final_parent_commit=predecessor,
            ref_before_commit=predecessor,
            ref_after_commit=final_commit,
            candidate_delta=candidate_delta,
            final_delta=final_delta,
            merge_free=True,
            linear_ancestry=True,
            attestor_id=target.attestor_id,
            attestor_version=target.attestor_version,
        )
        validate_integration_proof(receipt.proof_id, expectation, receipt)
        return receipt

    def _verify_receipt(
        self, target: GitProofTarget, receipt: IntegrationProofReceipt
    ) -> None:
        try:
            validate_integration_proof(receipt.proof_id, receipt.expectation, receipt)
        except IntegrationProofError:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT") from None
        if (
            receipt.repository_id != target.repository_id
            or receipt.integration_ref != target.integration_ref
            or receipt.attestor_id != target.attestor_id
            or receipt.attestor_version != target.attestor_version
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
        object_format = self._object_format(target)
        object_length = _object_length(object_format)
        if object_length is None or object_format != receipt.object_format:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        self._require_complete_history(target)
        self._validate_expected_git_inputs(receipt.expectation, object_length)
        for object_id, object_type in (
            (receipt.predecessor_commit, "commit"),
            (receipt.candidate_head_commit, "commit"),
            (receipt.final_commit, "commit"),
            (receipt.predecessor_tree, "tree"),
            (receipt.candidate_tree, "tree"),
            (receipt.final_tree, "tree"),
        ):
            self._require_object_type(target, object_id, object_type)
        commits = self._linear_commits(
            target,
            receipt.predecessor_commit,
            receipt.candidate_head_commit,
            object_length,
        )
        if commits != receipt.candidate_commits:
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
        if self._tree_oid(target, receipt.predecessor_commit, object_length) != receipt.predecessor_tree:
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
        if self._tree_oid(target, receipt.candidate_head_commit, object_length) != receipt.candidate_tree:
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
        if self._tree_oid(target, receipt.final_commit, object_length) != receipt.final_tree:
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
        parent = self._parents(target, receipt.final_commit, object_length)
        if parent != (receipt.final_commit, receipt.predecessor_commit):
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
        candidate_delta = self._delta(
            target,
            receipt.predecessor_commit,
            receipt.candidate_head_commit,
            object_length,
        )
        final_delta = self._delta(
            target,
            receipt.predecessor_commit,
            receipt.final_commit,
            object_length,
        )
        if (
            candidate_delta != receipt.candidate_delta
            or final_delta != receipt.final_delta
            or receipt.expectation.candidate_diff_hash != _delta_hash(candidate_delta)
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
    def _object_format(self, target: GitProofTarget) -> str:
        try:
            value = self._git(
                target,
                "rev-parse",
                "--show-object-format",
                code="RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE",
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID") from None
        return value

    def _validate_expected_git_inputs(
        self, expectation: IntegrationExpectation, object_length: int
    ) -> None:
        if (
            expectation.candidate_base_commit
            != expectation.predecessor_integration_head
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
        if not all(
            _valid_oid(value, object_length)
            for value in (
                expectation.candidate_base_commit,
                expectation.candidate_head_commit,
                expectation.predecessor_integration_head,
            )
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")

    def _require_complete_history(self, target: GitProofTarget) -> None:
        try:
            shallow = self._git(
                target,
                "rev-parse",
                "--is-shallow-repository",
                code="RELAY_INTEGRATION_ANCESTRY_INVALID",
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID") from None
        if shallow != "false":
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")

    def _ref_oid(
        self,
        target: GitProofTarget,
        reference: str,
        *,
        code: str = "RELAY_INTEGRATION_HEAD_STALE",
    ) -> str:
        try:
            value = self._git(
                target,
                "rev-parse",
                "--verify",
                f"{reference}^{{commit}}",
                code=code,
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError(code) from None
        object_length = _object_length(self._object_format(target))
        if object_length is None or not _valid_oid(value, object_length):
            raise IntegrationProofError(
                code
                if code == "RELAY_INTEGRATION_PROOF_CORRUPT"
                else "RELAY_INTEGRATION_OBJECT_INVALID"
            )
        return value

    def _tree_oid(self, target: GitProofTarget, commit: str, object_length: int) -> str:
        try:
            value = self._git(
                target,
                "rev-parse",
                "--verify",
                f"{commit}^{{tree}}",
                code="RELAY_INTEGRATION_OBJECT_INVALID",
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID") from None
        if not _valid_oid(value, object_length):
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        self._require_object_type(target, value, "tree")
        return value

    def _require_object_type(
        self, target: GitProofTarget, object_id: str, expected_type: str
    ) -> None:
        try:
            actual_type = self._git(
                target,
                "cat-file",
                "-t",
                object_id,
                code="RELAY_INTEGRATION_OBJECT_INVALID",
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID") from None
        if actual_type != expected_type:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")

    def _linear_commits(
        self,
        target: GitProofTarget,
        predecessor: str,
        candidate: str,
        object_length: int,
    ) -> tuple[str, ...]:
        commits: list[str] = []
        current = candidate
        seen: set[str] = set()
        while current != predecessor:
            if (
                len(commits) >= 256
                or current in seen
                or not _valid_oid(current, object_length)
            ):
                raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
            seen.add(current)
            parents = self._parents(target, current, object_length)
            if len(parents) != 2:
                raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
            commits.append(current)
            current = parents[1]
        if not commits:
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
        return tuple(reversed(commits))

    def _parents(
        self, target: GitProofTarget, commit: str, object_length: int
    ) -> tuple[str, ...]:
        try:
            raw = self._git(
                target,
                "cat-file",
                "commit",
                commit,
                code="RELAY_INTEGRATION_ANCESTRY_INVALID",
            )
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID") from None
        header, separator, _ = raw.partition(b"\n\n")
        if not separator:
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
        parents: list[str] = []
        for line in header.splitlines():
            if not line.startswith(b"parent "):
                continue
            try:
                parent = line[7:].decode("ascii")
            except UnicodeError:
                raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID") from None
            if not _valid_oid(parent, object_length):
                raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
            parents.append(parent)
        return (commit, *parents)

    def _delta(
        self,
        target: GitProofTarget,
        predecessor: str,
        successor: str,
        object_length: int,
    ) -> tuple[IntegrationDeltaEntry, ...]:
        raw = self._git(
            target,
            "diff-tree",
            "-r",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "--no-commit-id",
            "-z",
            predecessor,
            successor,
            code="RELAY_INTEGRATION_TREE_MISMATCH",
        )
        if not raw:
            return ()
        fields = raw.split(b"\x00")
        if fields[-1:] == [b""]:
            fields.pop()
        if len(fields) % 2:
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
        entries: list[IntegrationDeltaEntry] = []
        for offset in range(0, len(fields), 2):
            try:
                header = fields[offset].decode("ascii").split()
                if len(header) != 5 or not header[0].startswith(":"):
                    raise ValueError
                old_mode = header[0][1:]
                new_mode, old_oid, new_oid = header[1:4]
                path = fields[offset + 1].decode("utf-8", errors="strict")
            except (UnicodeError, ValueError):
                raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH") from None
            old_values = self._delta_side(target, old_oid, old_mode, object_length)
            new_values = self._delta_side(target, new_oid, new_mode, object_length)
            entries.append(
                IntegrationDeltaEntry(
                    path=path,
                    old_oid=old_values[0],
                    new_oid=new_values[0],
                    old_mode=old_values[1],
                    new_mode=new_values[1],
                    old_type=old_values[2],
                    new_type=new_values[2],
                )
            )
        try:
            return tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_SCOPE_MISMATCH") from None

    def _delta_side(
        self,
        target: GitProofTarget,
        object_id: str,
        mode: str,
        object_length: int,
    ) -> tuple[str | None, str | None, str | None]:
        if mode == "000000":
            if object_id != "0" * object_length:
                raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
            return None, None, None
        if not _valid_oid(object_id, object_length):
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        try:
            object_type = self._git(
                target,
                "cat-file",
                "-t",
                object_id,
                code="RELAY_INTEGRATION_OBJECT_INVALID",
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID") from None
        return object_id, mode, object_type

    def _commit_tree(
        self,
        target: GitProofTarget,
        tree: str,
        parent: str,
        object_length: int,
    ) -> str:
        try:
            value = self._git(
                target,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                "Relay host proof integration",
                code="RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE",
            ).decode("ascii").strip()
        except UnicodeError:
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID") from None
        if not _valid_oid(value, object_length):
            raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        return value

    def _git(
        self,
        target: GitProofTarget,
        *arguments: str,
        code: str,
    ) -> bytes:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("GIT_CONFIG_"):
                environment.pop(key, None)
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_REPLACE_REF_BASE",
            "GIT_NO_REPLACE_OBJECTS",
            "GIT_SHALLOW_FILE",
            "GIT_NAMESPACE",
            "GIT_QUARANTINE_PATH",
            "GIT_PREFIX",
            "GIT_CEILING_DIRECTORIES",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "2718lab Relay Host",
                "GIT_AUTHOR_EMAIL": "relay-host@invalid",
                "GIT_COMMITTER_NAME": "2718lab Relay Host",
                "GIT_COMMITTER_EMAIL": "relay-host@invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            completed = subprocess.run(
                [self._git_executable, "--no-replace-objects", *arguments],
                cwd=target.repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=_MAX_GIT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            raise IntegrationProofError(code) from None
        return completed.stdout


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_INVALID") from None


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _readonly_registry_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"{path.as_uri()}?mode=ro&immutable=1",
        uri=True,
        isolation_level=None,
    )


def _registry_schema_state(path: Path) -> str:
    try:
        connection = _readonly_registry_connection(path)
        try:
            row = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'relay_host_integration_proofs'
                """
            ).fetchone()
            if row is None:
                return "missing"
            _assert_registry_schema(connection)
            return "valid"
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "invalid"


def _assert_registry_schema(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'relay_host_integration_proofs'
        """
    ).fetchone()
    if table is None or _normalized_sql(table[0]) != _normalized_sql(_REGISTRY_TABLE_SQL):
        raise sqlite3.DatabaseError
    columns = tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(relay_host_integration_proofs)"
        ).fetchall()
    )
    if columns != _REGISTRY_COLUMNS:
        raise sqlite3.DatabaseError
    events_table = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'relay_host_proof_events'
        """
    ).fetchone()
    if events_table is None or _normalized_sql(events_table[0]) != _normalized_sql(
        _REGISTRY_EVENTS_TABLE_SQL
    ):
        raise sqlite3.DatabaseError
    event_columns = tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(relay_host_proof_events)"
        ).fetchall()
    )
    if event_columns != _REGISTRY_EVENT_COLUMNS:
        raise sqlite3.DatabaseError
    if connection.execute("PRAGMA user_version").fetchone()[0] != _REGISTRY_SCHEMA_VERSION:
        raise sqlite3.DatabaseError
    for name, expected_sql in (
        ("relay_host_proof_target_state", _REGISTRY_INDEX_SQL),
        ("relay_host_proof_active_target", _REGISTRY_ACTIVE_INDEX_SQL),
        ("relay_host_proof_pending", _REGISTRY_PENDING_INDEX_SQL),
        ("relay_host_proof_event_epoch", _REGISTRY_EVENT_INDEX_SQL),
    ):
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        if index is None or _normalized_sql(index[0]) != _normalized_sql(expected_sql):
            raise sqlite3.DatabaseError


def _normalized_sql(value: object) -> str:
    if type(value) is not str:
        raise sqlite3.DatabaseError
    return "".join(value.split()).casefold()


def _reservation_database_error(error: sqlite3.Error) -> IntegrationProofError:
    code = getattr(error, "sqlite_errorcode", None)
    if (
        type(code) is int
        and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    ) or "busy" in str(error).casefold() or "locked" in str(error).casefold():
        return IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")
    return IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")


def _target_key(workspace_id: str, target: GitProofTarget) -> str:
    del workspace_id
    return canonical_hash(
        {
            "schema": _TARGET_SCHEMA,
            "repository_id": target.repository_id,
            "integration_ref": target.integration_ref,
        }
    )


def _delta_hash(entries: tuple[IntegrationDeltaEntry, ...]) -> str:
    return canonical_hash(
        {"schema": _DELTA_SCHEMA, "entries": [entry.to_dict() for entry in entries]}
    )


def _object_length(object_format: str) -> int | None:
    return {"sha1": 40, "sha256": 64}.get(object_format)


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _valid_oid(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and _OID.fullmatch(value) is not None
    )


def _valid_ref(value: object) -> bool:
    if type(value) is not str or _REF.fullmatch(value) is None:
        return False
    return (
        ".." not in value
        and "@{" not in value
        and "\\" not in value
        and not value.endswith(("/", ".", ".lock"))
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _valid_token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None
