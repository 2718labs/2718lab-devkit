"""Shared canonical, single-use approval manifests and recoverable effect records.

This module deliberately has no Git, GitHub, subprocess, or network client.  A
host supplies the already-authorized external fact query and effect execution
interfaces; SQLite only records the authorization and recovery decision.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .redaction import canonical_json, canonical_sha256


class ApprovalError(RuntimeError):
    """Raised when an approval cannot authorize the requested effect."""


class ApprovalState(str, Enum):
    PREPARED = "PREPARED"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class EffectState(str, Enum):
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ApprovalManifest:
    """All immutable facts a human approval is allowed to cover."""

    action: str
    repo_realpath: str
    origin_fingerprint: str
    base_head: str
    status_hash: str
    diff_hash: str
    test_hash: str
    risk_hash: str
    commit_message: str | None = None
    remote: str | None = None
    ref: str | None = None
    pr_payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.action not in {"commit", "push", "pr"}:
            raise ValueError("action must be commit, push, or pr")
        for field in (
            "repo_realpath",
            "origin_fingerprint",
            "base_head",
            "status_hash",
            "diff_hash",
            "test_hash",
            "risk_hash",
        ):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} is required")
        if self.action == "commit":
            if not isinstance(self.commit_message, str) or not self.commit_message:
                raise ValueError("commit requires commit_message")
            if (
                self.remote is not None
                or self.ref is not None
                or self.pr_payload is not None
            ):
                raise ValueError("commit manifest cannot contain push or pr fields")
        elif self.action == "push":
            if (
                not isinstance(self.remote, str)
                or not self.remote
                or not isinstance(self.ref, str)
                or not self.ref
            ):
                raise ValueError("push requires remote and ref")
            if self.commit_message is not None or self.pr_payload is not None:
                raise ValueError("push manifest cannot contain commit or pr fields")
        else:
            if not isinstance(self.pr_payload, Mapping) or not self.pr_payload:
                raise ValueError("pr requires pr_payload")
            if (
                self.commit_message is not None
                or self.remote is not None
                or self.ref is not None
            ):
                raise ValueError("pr manifest cannot contain commit or push fields")
        try:
            canonical_json(self.as_dict())
        except (TypeError, ValueError) as error:
            raise ValueError("manifest must be canonical JSON") from error

    def as_dict(self) -> dict[str, Any]:
        """Return the full canonical approval payload, including null fields."""

        return {
            "action": self.action,
            "repo_realpath": self.repo_realpath,
            "origin_fingerprint": self.origin_fingerprint,
            "base_head": self.base_head,
            "status_hash": self.status_hash,
            "diff_hash": self.diff_hash,
            "test_hash": self.test_hash,
            "risk_hash": self.risk_hash,
            "commit_message": self.commit_message,
            "remote": self.remote,
            "ref": self.ref,
            "pr_payload": dict(self.pr_payload)
            if self.pr_payload is not None
            else None,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    manifest: ApprovalManifest
    state: ApprovalState
    expires_at: datetime


@dataclass(frozen=True)
class EffectRecord:
    id: str
    approval_id: str
    action: str
    manifest: ApprovalManifest
    state: EffectState


class EffectHost(Protocol):
    """Host boundary: no command is performed by the approval journal itself."""

    def query_effect(
        self, *, action: str, manifest: dict[str, Any]
    ) -> object | None: ...

    def execute_effect(self, *, action: str, manifest: dict[str, Any]) -> object: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _encode_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _decode_manifest(value: str) -> ApprovalManifest:
    return ApprovalManifest(**json.loads(value))


class ApprovalJournal:
    """SQLite journal implementing one immutable approval per external effect."""

    def __init__(
        self, database: str | Path, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._connection = sqlite3.connect(str(database), isolation_level=None)
        self._now = now or (lambda: datetime.now(UTC))
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS approval_records (
                id TEXT PRIMARY KEY,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS effect_records (
                id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE REFERENCES approval_records(id),
                action TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                external_fact_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self._connection.close()

    def prepare(
        self, manifest: ApprovalManifest, *, expires_at: datetime
    ) -> ApprovalRecord:
        now = _as_utc(self._now())
        expires = _as_utc(expires_at)
        record_id = str(uuid.uuid4())
        payload = canonical_json(manifest.as_dict())
        with self._transaction():
            self._connection.execute(
                "INSERT INTO approval_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    payload,
                    manifest.digest,
                    ApprovalState.PREPARED.value,
                    _encode_time(expires),
                    _encode_time(now),
                    _encode_time(now),
                ),
            )
        return ApprovalRecord(record_id, manifest, ApprovalState.PREPARED, expires)

    def grant(self, approval_id: str) -> ApprovalRecord:
        return self._transition_approval(
            approval_id, ApprovalState.PREPARED, ApprovalState.GRANTED
        )

    def deny(self, approval_id: str) -> ApprovalRecord:
        return self._transition_approval(
            approval_id, ApprovalState.PREPARED, ApprovalState.DENIED
        )

    def claim(self, approval_id: str, manifest: ApprovalManifest) -> EffectRecord:
        now = _as_utc(self._now())
        with self._transaction():
            row = self._approval_row(approval_id)
            state = ApprovalState(row["state"])
            if state is ApprovalState.DENIED:
                raise ApprovalError("approval denied")
            if state is ApprovalState.CLAIMED:
                raise ApprovalError("approval already claimed")
            if state is not ApprovalState.GRANTED:
                raise ApprovalError(f"approval is not granted: {state.value}")
            if _as_utc(datetime.fromisoformat(row["expires_at"])) <= now:
                raise ApprovalError("approval expired")
            if row["manifest_hash"] != manifest.digest:
                self._connection.execute(
                    "UPDATE approval_records SET state=?, updated_at=? WHERE id=?",
                    (ApprovalState.DENIED.value, _encode_time(now), approval_id),
                )
                raise ApprovalError("approval manifest changed")

            effect_id = str(uuid.uuid4())
            self._connection.execute(
                "UPDATE approval_records SET state=?, updated_at=? WHERE id=?",
                (ApprovalState.CLAIMED.value, _encode_time(now), approval_id),
            )
            self._connection.execute(
                "INSERT INTO effect_records VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    effect_id,
                    approval_id,
                    manifest.action,
                    canonical_json(manifest.as_dict()),
                    manifest.digest,
                    EffectState.CLAIMED.value,
                    _encode_time(now),
                    _encode_time(now),
                ),
            )
        return EffectRecord(
            effect_id, approval_id, manifest.action, manifest, EffectState.CLAIMED
        )

    def recover(self, effect_id: str, host: EffectHost) -> EffectRecord:
        """Resolve a crashed claim by checking the host before every retry."""

        record = self._effect_record(effect_id)
        if record.state is EffectState.SUCCEEDED:
            return record
        if record.state is not EffectState.CLAIMED:
            raise ApprovalError("failed effect requires a new approval")

        fact = host.query_effect(
            action=record.action, manifest=record.manifest.as_dict()
        )
        if fact is not None:
            return self.succeed(effect_id, fact)
        try:
            host.execute_effect(
                action=record.action, manifest=record.manifest.as_dict()
            )
            fact = host.query_effect(
                action=record.action, manifest=record.manifest.as_dict()
            )
        except Exception as error:
            return self.fail(effect_id, str(error))
        if fact is None:
            return self.fail(effect_id, "external fact absent after effect execution")
        return self.succeed(effect_id, fact)

    def succeed(self, effect_id: str, external_fact: object) -> EffectRecord:
        if external_fact is None:
            raise ValueError("external_fact is required")
        return self._finish(
            effect_id, EffectState.SUCCEEDED, external_fact=external_fact
        )

    def fail(self, effect_id: str, error: str) -> EffectRecord:
        if not error:
            raise ValueError("error is required")
        return self._finish(effect_id, EffectState.FAILED, error=error)

    def _transition_approval(
        self, approval_id: str, expected: ApprovalState, target: ApprovalState
    ) -> ApprovalRecord:
        now = _as_utc(self._now())
        with self._transaction():
            row = self._approval_row(approval_id)
            state = ApprovalState(row["state"])
            if state is not expected:
                raise ApprovalError(f"approval cannot transition from {state.value}")
            self._connection.execute(
                "UPDATE approval_records SET state=?, updated_at=? WHERE id=?",
                (target.value, _encode_time(now), approval_id),
            )
        return ApprovalRecord(
            approval_id,
            _decode_manifest(row["manifest_json"]),
            target,
            datetime.fromisoformat(row["expires_at"]),
        )

    def _finish(
        self,
        effect_id: str,
        target: EffectState,
        *,
        external_fact: object | None = None,
        error: str | None = None,
    ) -> EffectRecord:
        now = _as_utc(self._now())
        with self._transaction():
            record = self._effect_record(effect_id)
            if record.state is not EffectState.CLAIMED:
                raise ApprovalError(
                    f"effect cannot transition from {record.state.value}"
                )
            fact_json = (
                canonical_json(external_fact) if external_fact is not None else None
            )
            self._connection.execute(
                "UPDATE effect_records SET state=?, external_fact_json=?, error=?, updated_at=? WHERE id=?",
                (target.value, fact_json, error, _encode_time(now), effect_id),
            )
            approval_target = ApprovalState(target.value)
            self._connection.execute(
                "UPDATE approval_records SET state=?, updated_at=? WHERE id=?",
                (approval_target.value, _encode_time(now), record.approval_id),
            )
        return EffectRecord(
            record.id, record.approval_id, record.action, record.manifest, target
        )

    def _approval_row(self, approval_id: str) -> sqlite3.Row:
        self._connection.row_factory = sqlite3.Row
        row = self._connection.execute(
            "SELECT * FROM approval_records WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalError("approval not found")
        return row

    def _effect_record(self, effect_id: str) -> EffectRecord:
        self._connection.row_factory = sqlite3.Row
        row = self._connection.execute(
            "SELECT * FROM effect_records WHERE id=?", (effect_id,)
        ).fetchone()
        if row is None:
            raise ApprovalError("effect not found")
        return EffectRecord(
            id=row["id"],
            approval_id=row["approval_id"],
            action=row["action"],
            manifest=_decode_manifest(row["manifest_json"]),
            state=EffectState(row["state"]),
        )

    @contextmanager
    def _transaction(self):
        """Commit each logical state transition atomically for crash recovery."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
