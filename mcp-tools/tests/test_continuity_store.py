"""Private durable-storage tests for Continuity (CP-B)."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_atlas.extractors import (  # noqa: E402
    BoundExecutionReceipt as AtlasReceipt,
)
from devkit_atlas.extractors import (
    ExtractionRequest,
)
from devkit_atlas.service import (  # noqa: E402
    AcceptedAtlasProjectionEvidence,
    AcceptedAtlasProjectionRequest,
)
from devkit_continuity.cas import ContinuityCas, ContinuityCasError  # noqa: E402
from devkit_continuity.models import (  # noqa: E402
    ContinuityKey,
    ContinuityReceipt,
    FrozenEntry,
    FrozenView,
)
from devkit_continuity.service import ContinuityService  # noqa: E402
from devkit_continuity.store import ContinuityStore, ContinuityStoreError  # noqa: E402
from project_index.checkpoints import CheckpointFile  # noqa: E402
from project_index.models import CoverageGap, IndexNode, SnapshotFile  # noqa: E402


def _hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _key() -> ContinuityKey:
    return ContinuityKey("workflow", "task", 1, "acceptance", "ingestion", _hash(b"p"), _hash(b"e"))


def _view(key: ContinuityKey) -> FrozenView:
    body = b"after"
    return FrozenView.create(key=key, entries=(FrozenEntry("after_file", "src/a.py", _hash(body), len(body)),))


def _typed_inputs(*, after_body: bytes = b"after") -> tuple[ContinuityKey, AcceptedAtlasProjectionRequest, AcceptedAtlasProjectionEvidence]:
    request = AcceptedAtlasProjectionRequest.create(
        workflow_id="workflow", code_task_id="task", code_task_version=1,
        input_snapshot_id="input", output_snapshot_id="output", indexed_diff_hash=_hash(b"diff"),
        intent_id="intent", language="python", framework="pytest", checkpoint_id="checkpoint",
        checkpoint_hash=_hash(b"checkpoint"), output_query_trace_id="query",
        verification_artifact_hashes=(_hash(b"artifact"),), execution_receipt_ids=(_hash(b"receipt"),),
    )
    before_body = b"before"
    extraction = ExtractionRequest(
        "workflow", "task", request.acceptance_id, "kind", "intent", _hash(b"workspace"), "checkpoint", "input", "output", ("src",),
        (CheckpointFile("src/a.py", _hash(before_body), before_body),),
        (SnapshotFile("src/a.py", _hash(after_body), after_body),),
        (IndexNode("node", "function", "src/a.py", "run", "pkg.run", 1, 1, _hash(after_body)),),
        (CoverageGap("src/a.py", "PARSER_GAP", "gap"),),
        (AtlasReceipt(_hash(b"receipt"), "command", "workflow", "task", request.acceptance_id, _hash(b"workspace"), "output", ("python",), _hash(b"command"), _hash(b"input"), _hash(b"output"), 0, True),),
    )
    evidence = AcceptedAtlasProjectionEvidence(1, "python", "pytest", request.checkpoint_hash, request.indexed_diff_hash, "query", request.verification_artifact_hashes, extraction)
    key = ContinuityKey(request.workflow_id, request.code_task_id, request.code_task_version, request.acceptance_id, request.ingestion_key, request.payload_hash, request.evidence_binding_hash)
    return key, request, evidence


def test_bootstrap_creates_isolated_v1_wal_and_is_idempotent(tmp_path: Path) -> None:
    database, cas_root, scratch = tmp_path / "continuity.sqlite3", tmp_path / "cas", tmp_path / "scratch"
    store = ContinuityStore.bootstrap(database, cas_root, scratch)
    store.close()
    assert database.exists() and cas_root.is_dir()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "1"
    ContinuityStore.bootstrap(database, cas_root, scratch).close()


def test_readonly_open_never_creates_database_or_cas(tmp_path: Path) -> None:
    database, cas_root, scratch = tmp_path / "missing.sqlite3", tmp_path / "missing-cas", tmp_path / "scratch"
    with pytest.raises(ContinuityStoreError):
        ContinuityStore.open_readonly(database, cas_root, scratch)
    assert not database.exists() and not cas_root.exists()


def test_cas_verifies_bytes_and_preserves_existing_content(tmp_path: Path) -> None:
    root, scratch = tmp_path / "cas", tmp_path / "scratch"
    cas = ContinuityCas.bootstrap(root, scratch)
    body, digest = b"body", _hash(b"body")
    assert cas.put_verified(digest, len(body), body) == digest
    assert cas.read_verified(digest, len(body)) == body
    with pytest.raises(ContinuityCasError):
        cas.put_verified(digest, len(body), b"evil")
    assert cas.read_verified(digest, len(body)) == body


def test_store_has_immutable_relations_append_only_attempts_and_fenced_pointer_cas(tmp_path: Path) -> None:
    database, cas_root, scratch = tmp_path / "continuity.sqlite3", tmp_path / "cas", tmp_path / "scratch"
    store = ContinuityStore.bootstrap(database, cas_root, scratch)
    key, view = _key(), _view(_key())
    receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    assert store.insert_or_get_view(view, view.manifest_json) == view
    assert store.insert_or_get_receipt(receipt, "{}") == receipt
    first = store.append_attempt_event(key, 1, "claimed", None, None)
    second = store.append_attempt_event(key, 1, "frozen", view.view_id, receipt.receipt_hash)
    assert (first.state, second.state, store.current_attempt(key).state) == ("claimed", "frozen", "frozen")
    with pytest.raises(sqlite3.DatabaseError):
        store._connection.execute("DELETE FROM views")
    assert store.compare_and_swap_pointer(key, view, 0, 1).pointer_version == 1
    with pytest.raises(ContinuityStoreError):
        store.compare_and_swap_pointer(key, view, 0, 1)
    with pytest.raises(ContinuityStoreError):
        store.compare_and_swap_pointer(key, view, 1, 0)
    store.close()


def test_service_equal_freeze_reuses_and_changed_view_conflicts(tmp_path: Path) -> None:
    database, cas_root, scratch = tmp_path / "continuity.sqlite3", tmp_path / "cas", tmp_path / "scratch"
    service = ContinuityService(ContinuityStore.bootstrap(database, cas_root, scratch), ContinuityCas.open_prepared(cas_root, scratch, read_only=False))
    key, request, evidence = _typed_inputs()
    attempt = service.claim_or_reuse(key)
    view = service.freeze(attempt, request, evidence)
    assert service.freeze(attempt, request, evidence) == view
    _, _, altered = _typed_inputs(after_body=b"altered")
    with pytest.raises(ContinuityStoreError):
        service.freeze(attempt, request, altered)
