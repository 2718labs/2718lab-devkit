"""Relay finalization journal corruption and fencing coverage."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.store import RelayStore, RelayStoreError

try:
    from devkit_relay.proofs import ProofFinalizationFence
except ImportError:

    @dataclass(frozen=True, slots=True)
    class ProofFinalizationFence:
        finalization_id: str
        reservation_epoch: int
        integration_proof_id: str
        workspace_id: str
        expectation_key: str
        expectation_version: int
        expectation_hash: str
        target_ref: str
        base_oid: str
        final_oid: str

        @property
        def fence_hash(self) -> str:
            return canonical_hash(
                {
                    "finalization_id": self.finalization_id,
                    "reservation_epoch": self.reservation_epoch,
                    "integration_proof_id": self.integration_proof_id,
                    "workspace_id": self.workspace_id,
                    "expectation_key": self.expectation_key,
                    "expectation_version": self.expectation_version,
                    "expectation_hash": self.expectation_hash,
                    "target_ref": self.target_ref,
                    "base_oid": self.base_oid,
                    "final_oid": self.final_oid,
                }
            )


def _fence(finalization_id: str) -> ProofFinalizationFence:
    return ProofFinalizationFence(
        finalization_id=finalization_id,
        reservation_epoch=1,
        integration_proof_id="sha256:" + "a" * 64,
        workspace_id="sha256:" + "b" * 64,
        expectation_key="candidate-r7",
        expectation_version=1,
        expectation_hash="sha256:" + "c" * 64,
        target_ref="refs/heads/main",
        base_oid="d" * 40,
        final_oid="e" * 40,
    )


def _insert_prepared(store: RelayStore, fence: ProofFinalizationFence) -> None:
    store._require_connection().execute(
        """
        INSERT INTO relay_v3_finalization_journal
            (finalization_id, reservation_epoch, integration_proof_id, workspace_id,
             expectation_key, expectation_version, expectation_hash, target_ref,
             base_oid, final_oid, fence_hash, state, result_hash, journal_version,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, 1, ?, ?)
        """,
        (
            fence.finalization_id,
            fence.reservation_epoch,
            fence.integration_proof_id,
            fence.workspace_id,
            fence.expectation_key,
            fence.expectation_version,
            fence.expectation_hash,
            fence.target_ref,
            fence.base_oid,
            fence.final_oid,
            fence.fence_hash,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )


def test_finalization_rejects_wrong_fence_hash_without_mutation(tmp_path: Path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    fence = _fence("finalization-r7-fence")
    _insert_prepared(store, fence)
    connection = store._require_connection()
    connection.execute(
        """
        UPDATE relay_v3_finalization_journal
        SET fence_hash = ?
        WHERE finalization_id = ?
        """,
        ("sha256:" + "f" * 64, fence.finalization_id),
    )
    before = store.database_fingerprint()

    with pytest.raises(RelayStoreError) as raised:
        store.resolve_or_abort_finalization(fence=fence)

    assert raised.value.code == "RELAY_FINALIZATION_CONFLICT"
    assert store.database_fingerprint() == before


def test_finalization_rejects_orphan_outcome_without_mutation(tmp_path: Path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    fence = _fence("finalization-r7-orphan")
    connection = store._require_connection()
    connection.execute(
        """
        INSERT INTO relay_v3_finalization_outcomes
            (finalization_id, fence_hash, integration_proof_id, expectation_key,
             expectation_version, expectation_hash, result_hash, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fence.finalization_id,
            fence.fence_hash,
            fence.integration_proof_id,
            fence.expectation_key,
            fence.expectation_version,
            fence.expectation_hash,
            "sha256:" + "f" * 64,
            "{}",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    before = store.database_fingerprint()

    with pytest.raises(RelayStoreError) as raised:
        store.resolve_or_abort_finalization(fence=fence)

    assert raised.value.code == "RELAY_FINALIZATION_CONFLICT"
    assert store.database_fingerprint() == before
