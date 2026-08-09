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
from devkit_continuity.canonical import canonical_json  # noqa: E402
from devkit_continuity.cas import ContinuityCas, ContinuityCasError  # noqa: E402
from devkit_continuity.models import (  # noqa: E402
    ContinuityAttempt,
    ContinuityError,
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
    assert not hasattr(ContinuityCas, "bootstrap")
    missing_root, missing_scratch = tmp_path / "missing-cas", tmp_path / "missing-scratch"
    with pytest.raises(ContinuityCasError):
        ContinuityCas.open_prepared(missing_root, missing_scratch, read_only=False)
    assert not missing_root.exists() and not missing_scratch.exists()
    config = _config(tmp_path)
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    store.close()
    assert config.continuity_database.exists() and config.continuity_cas_root.is_dir()
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "2"
    _config(tmp_path)


def test_runtime_bootstrap_transactionally_migrates_verified_v1_state(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(tmp_path / "data"), "CODEX_TASK_TEMP": str(scratch)}
    )
    config.data_root.mkdir()
    key, view = _key(), _view(_key())
    receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    with sqlite3.connect(config.continuity_database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
            CREATE TABLE views (view_id TEXT PRIMARY KEY NOT NULL, key_hash TEXT UNIQUE NOT NULL, manifest_hash TEXT NOT NULL, cas_root_hash TEXT NOT NULL, manifest_json TEXT NOT NULL);
            CREATE TABLE entries (view_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, byte_length INTEGER NOT NULL, PRIMARY KEY(view_id,role,path), FOREIGN KEY(view_id) REFERENCES views(view_id));
            CREATE TABLE receipts (receipt_hash TEXT PRIMARY KEY NOT NULL, key_hash TEXT NOT NULL, view_id TEXT NOT NULL, kind TEXT NOT NULL, receipt_json TEXT NOT NULL);
            CREATE TABLE attempts (key_hash TEXT NOT NULL, key_json TEXT NOT NULL, fence_epoch INTEGER NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('claimed','frozen','published','expired','abandoned')), view_id TEXT, receipt_hash TEXT, PRIMARY KEY(key_hash,sequence), CHECK((state='claimed' AND view_id IS NULL AND receipt_hash IS NULL) OR (state!='claimed' AND view_id IS NOT NULL AND receipt_hash IS NOT NULL)));
            CREATE TABLE pointers (workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL, view_id TEXT NOT NULL, pointer_version INTEGER NOT NULL, fence_epoch INTEGER NOT NULL, PRIMARY KEY(workflow_id,code_task_id,code_task_version));
            INSERT INTO schema_metadata(key,value) VALUES('schema_version','1');
            """
        )
        for table in ("views", "entries", "receipts", "attempts"):
            connection.execute(f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END")
            connection.execute(f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END")
        connection.execute(
            "INSERT INTO views(view_id,key_hash,manifest_hash,cas_root_hash,manifest_json) VALUES(?,?,?,?,?)",
            (view.view_id, key.key_hash, view.manifest_hash, view.cas_root_hash, view.manifest_json),
        )
        connection.executemany(
            "INSERT INTO entries(view_id,role,path,content_hash,byte_length) VALUES(?,?,?,?,?)",
            [(view.view_id, item.role, item.path, item.content_hash, item.byte_length) for item in view.entries],
        )
        connection.execute(
            "INSERT INTO receipts(receipt_hash,key_hash,view_id,kind,receipt_json) VALUES(?,?,?,?,?)",
            (receipt.receipt_hash, key.key_hash, view.view_id, "frozen", canonical_json({"key": key.to_dict(), "view_id": view.view_id, "kind": "frozen"})),
        )
        connection.execute(
            "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)",
            (key.key_hash, canonical_json(key.to_dict()), 1, 1, "frozen", view.view_id, receipt.receipt_hash),
        )
        connection.execute(
            "INSERT INTO pointers(workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch) VALUES(?,?,?,?,?,?)",
            (key.workflow_id, key.code_task_id, key.code_task_version, view.view_id, 1, 1),
        )
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "2"
        assert {"views_v1", "entries_v1", "receipts_v1", "attempts_v1", "pointers_v1"} <= {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    migrated = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    assert migrated.current_attempt(key) == ContinuityAttempt(key, 1, "frozen", view.view_id, receipt.receipt_hash)
    assert migrated.pointer_for(key) is not None
    migrated.close()


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
    store._connection.rollback()
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
    assert service.freeze(service.claim_or_reuse(key), request, evidence) == view
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


def test_atomic_claim_fence_rejects_stale_freeze_and_publish(tmp_path: Path) -> None:
    _, store = _prepared_store(tmp_path)
    key, view = _key(), _view(_key())
    first = store.claim_or_reuse_atomic(key)
    second = store.claim_or_reuse_atomic(key)
    assert (first.fence_epoch, second.fence_epoch) == (1, 2)
    receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    with pytest.raises(ContinuityError):
        store.append_attempt_event(key, 1, "claimed", None, None)
    with pytest.raises(ContinuityError):
        store.freeze_attempt_atomic(first, view, receipt)
    with pytest.raises(ContinuityError):
        store.publish_attempt_atomic(first, view)
    assert store.current_attempt(key) == second


def test_atomic_freeze_rolls_back_view_entry_and_receipt_on_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _prepared_store(tmp_path)
    key, view = _key(), _view(_key())
    attempt = store.claim_or_reuse_atomic(key)
    receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")

    def fail_receipt(*_args: object) -> None:
        raise sqlite3.IntegrityError("injected")

    monkeypatch.setattr(store, "_insert_receipt_row", fail_receipt, raising=False)
    with pytest.raises(ContinuityError):
        store.freeze_attempt_atomic(attempt, view, receipt)
    assert store._connection.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 0
    assert store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert store._connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert store.current_attempt(key) == attempt


def test_atomic_publish_rolls_back_pointer_when_event_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _prepared_store(tmp_path)
    key, view = _key(), _view(_key())
    claimed = store.claim_or_reuse_atomic(key)
    frozen = store.freeze_attempt_atomic(
        claimed, view, ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    )

    original = getattr(store, "_append_attempt_row", lambda *_args: None)

    def fail_published(*args: object) -> None:
        if args[3] == "published":
            raise sqlite3.IntegrityError("injected")
        original(*args)

    monkeypatch.setattr(store, "_append_attempt_row", fail_published, raising=False)
    with pytest.raises(ContinuityError):
        store.publish_attempt_atomic(frozen, view)
    assert store.pointer_for(key) is None
    assert store.current_attempt(key) == frozen


def test_atomic_sqlite_races_normalize_to_continuity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _prepared_store(tmp_path)
    key = _key()

    def duplicate(*_args: object) -> None:
        raise sqlite3.IntegrityError("duplicate")

    monkeypatch.setattr(store, "_append_attempt_row", duplicate, raising=False)
    with pytest.raises(ContinuityError):
        store.claim_or_reuse_atomic(key)


def test_v2_foreign_keys_are_enabled_and_reject_orphans(tmp_path: Path) -> None:
    _, store = _prepared_store(tmp_path)
    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO entries(view_id,role,path,content_hash,byte_length) VALUES(?,?,?,?,?)",
            ("missing", "after_file", "a.py", _hash(b"x"), 1),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO receipts(receipt_hash,key_hash,view_id,kind,receipt_json) VALUES(?,?,?,?,?)",
            (_hash(b"receipt"), _hash(b"key"), _hash(b"view"), "frozen", "{}"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)",
            (_hash(b"key"), "{}", 1, 1, "claimed", None, None),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO pointers(key_hash,workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch) VALUES(?,?,?,?,?,?,?)",
            (_hash(b"key"), "workflow", "task", 1, _hash(b"view"), 1, 1),
        )


def test_cas_stage_failure_cleans_owner_stage_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    body, digest = b"stage", _hash(b"stage")
    original = cas._read_stage
    calls = 0

    def fail_once(*args: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ContinuityCasError("injected")
        return original(*args)

    monkeypatch.setattr(cas, "_read_stage", fail_once)
    with pytest.raises(ContinuityCasError):
        cas.put_verified(digest, len(body), body)
    assert not list((config.continuity_cas_root / ".staging").glob("*.stage"))
    assert cas.put_verified(digest, len(body), body) == digest


def test_cas_stage_write_failure_cleans_owner_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    body, digest = b"write", _hash(b"write")
    from devkit_continuity import cas as cas_module

    original = cas_module.os.write
    calls = 0

    def fail_once(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected")
        return original(descriptor, data)

    monkeypatch.setattr(cas_module.os, "write", fail_once)
    with pytest.raises(ContinuityCasError):
        cas.put_verified(digest, len(body), body)
    assert not list((config.continuity_cas_root / ".staging").glob("*.stage"))
    assert cas.put_verified(digest, len(body), body) == digest


def test_cas_stage_collision_keeps_other_owner_and_does_not_block_later_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    body, digest = b"collision", _hash(b"collision")
    stage_root = config.continuity_cas_root / ".staging"
    stage_root.mkdir()
    owner_stage = stage_root / (digest[7:] + ".owner.stage")
    owner_stage.write_bytes(b"owner")
    from devkit_continuity import cas as cas_module

    tokens = iter(("owner", "later"))
    monkeypatch.setattr(cas_module.secrets, "token_hex", lambda _size: next(tokens))
    with pytest.raises(ContinuityCasError):
        cas.put_verified(digest, len(body), body)
    assert owner_stage.exists()
    assert cas.put_verified(digest, len(body), body) == digest
