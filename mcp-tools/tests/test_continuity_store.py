"""Private durable-storage tests for Continuity (CP-B)."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from dataclasses import replace
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
    ContinuityAttempt,
    ContinuityKey,
    ContinuityReceipt,
    FrozenEntry,
    FrozenView,
)
from devkit_continuity.service import ContinuityService  # noqa: E402
from devkit_continuity.store import ContinuityStore, ContinuityStoreError  # noqa: E402
from devkit_runtime.bootstrap import RuntimeBootstrap  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from project_index.checkpoints import CheckpointFile  # noqa: E402
from project_index.models import CoverageGap, IndexNode, SnapshotFile  # noqa: E402


def _hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _key() -> ContinuityKey:
    return ContinuityKey("workflow", "task", 1, "acceptance", "ingestion", _hash(b"p"), _hash(b"e"))


def _view(key: ContinuityKey) -> FrozenView:
    body = b"after"
    return FrozenView.create(key=key, entries=(FrozenEntry("after_file", "src/a.py", _hash(body), len(body)),))


def _config(tmp_path: Path) -> RuntimeConfig:
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(tmp_path / "data"), "CODEX_TASK_TEMP": str(scratch)}
    )
    RuntimeBootstrap.run(
        config,
        proof_registry_bootstrap=lambda database: sqlite3.connect(database).close(),
    )
    return config


def _prepared_store(tmp_path: Path) -> tuple[RuntimeConfig, ContinuityStore]:
    config = _config(tmp_path)
    return config, ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )


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


def test_runtime_bootstrap_is_the_only_continuity_creation_seam(tmp_path: Path) -> None:
    assert not hasattr(ContinuityStore, "bootstrap")
    config = _config(tmp_path)
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    store.close()
    assert config.continuity_database.exists() and config.continuity_cas_root.is_dir()
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "1"
    _config(tmp_path)


def test_readonly_open_never_creates_database_or_cas(tmp_path: Path) -> None:
    database, cas_root, scratch = tmp_path / "missing.sqlite3", tmp_path / "missing-cas", tmp_path / "scratch"
    with pytest.raises(ContinuityStoreError):
        ContinuityStore.open_readonly(database, cas_root, scratch)
    assert not database.exists() and not cas_root.exists()


def test_cas_verifies_bytes_and_preserves_existing_content(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cas = ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    body, digest = b"body", _hash(b"body")
    assert cas.put_verified(digest, len(body), body) == digest
    assert cas.read_verified(digest, len(body)) == body
    with pytest.raises(ContinuityCasError):
        cas.put_verified(digest, len(body), b"evil")
    assert cas.read_verified(digest, len(body)) == body


def test_store_has_immutable_relations_append_only_attempts_and_fenced_pointer_cas(tmp_path: Path) -> None:
    _, store = _prepared_store(tmp_path)
    key, view = _key(), _view(_key())
    receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    assert store.insert_or_get_view(view, view.manifest_json) == view
    assert store.insert_or_get_receipt(receipt, "{}") == receipt
    first = store.append_attempt_event(key, 1, "claimed", None, None)
    second = store.append_attempt_event(key, 1, "frozen", view.view_id, receipt.receipt_hash)
    assert (first.state, second.state, store.current_attempt(key).state) == ("claimed", "frozen", "frozen")
    with pytest.raises(sqlite3.DatabaseError):
        store._connection.execute("DELETE FROM views")
    assert store.compare_and_swap_pointer(key, view, 0, 0, 1).pointer_version == 1
    with pytest.raises(ContinuityStoreError):
        store.compare_and_swap_pointer(key, view, 0, 1, 1)
    with pytest.raises(ContinuityStoreError):
        store.compare_and_swap_pointer(key, view, 1, 99, 99)
    assert store.compare_and_swap_pointer(key, view, 1, 1, 2).pointer_version == 2
    with pytest.raises(ContinuityStoreError):
        store.compare_and_swap_pointer(key, view, 2, 0, 2)
    store.close()


def test_service_equal_freeze_reuses_and_changed_view_conflicts(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False))
    key, request, evidence = _typed_inputs()
    attempt = service.claim_or_reuse(key)
    view = service.freeze(attempt, request, evidence)
    assert service.freeze(attempt, request, evidence) == view
    _, _, altered = _typed_inputs(after_body=b"altered")
    with pytest.raises(ContinuityStoreError):
        service.freeze(attempt, request, altered)


def test_prepared_open_rejects_same_name_noop_trigger_and_pointer_shape(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    store.close()
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute("DROP TRIGGER views_immutable_delete")
        connection.execute("CREATE TRIGGER views_immutable_delete BEFORE DELETE ON views BEGIN SELECT 1; END")
        connection.commit()
    with pytest.raises(ContinuityStoreError):
        ContinuityStore.open_readwrite(config.continuity_database, config.continuity_cas_root, config.scratch_root)

    config, store = _prepared_store(tmp_path / "shape")
    store.close()
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute("DROP TABLE pointers")
        connection.execute("CREATE TABLE pointers (workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL, view_id TEXT NOT NULL, pointer_version INTEGER NOT NULL, fence_epoch INTEGER NOT NULL)")
        connection.commit()
    with pytest.raises(ContinuityStoreError):
        ContinuityStore.open_readwrite(config.continuity_database, config.continuity_cas_root, config.scratch_root)


def test_cas_revalidates_shard_before_post_open_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cas = ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    body, digest = b"body", _hash(b"body")
    cas.put_verified(digest, len(body), body)
    from devkit_continuity import cas as cas_module

    original = cas_module._safe_root

    def reject_shard(path: Path, *, create: bool) -> None:
        if path.name == digest[7:9]:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
        original(path, create=create)

    monkeypatch.setattr(cas_module, "_safe_root", reject_shard)
    with pytest.raises(ContinuityCasError):
        cas.read_verified(digest, len(body))


def test_unrelated_typed_evidence_fails_before_cas_or_database_write(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False))
    key, request, evidence = _typed_inputs()
    with pytest.raises(ContinuityStoreError):
        service.freeze(ContinuityAttempt(key, 1, "claimed", None, None), request, replace(evidence, language="other"))
    assert not (config.continuity_cas_root / "sha256").exists()
    assert store._connection.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 0
    assert store._connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
