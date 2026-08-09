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
from devkit_runtime.config import RuntimeConfig, RuntimeConfigError  # noqa: E402
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
    config = _unprepared_config(tmp_path)
    RuntimeBootstrap.run(
        config,
        proof_registry_bootstrap=lambda database: sqlite3.connect(database).close(),
    )
    return config


def _unprepared_config(tmp_path: Path) -> RuntimeConfig:
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(tmp_path / "data"), "CODEX_TASK_TEMP": str(scratch)}
    )


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


def _create_verified_v1(
    config: RuntimeConfig, *, direct_frozen: bool = False
) -> tuple[ContinuityKey, FrozenView, ContinuityReceipt]:
    config.data_root.mkdir(parents=True, exist_ok=True)
    key, view = _key(), _view(_key())
    frozen = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    published = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published")
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
        for receipt in (frozen, published):
            connection.execute(
                "INSERT INTO receipts(receipt_hash,key_hash,view_id,kind,receipt_json) VALUES(?,?,?,?,?)",
                (receipt.receipt_hash, key.key_hash, view.view_id, receipt.kind, canonical_json({"key": key.to_dict(), "view_id": view.view_id, "kind": receipt.kind})),
            )
        attempts = (
            ((key.key_hash, canonical_json(key.to_dict()), 1, 1, "frozen", view.view_id, frozen.receipt_hash),)
            if direct_frozen
            else (
                (key.key_hash, canonical_json(key.to_dict()), 1, 1, "claimed", None, None),
                (key.key_hash, canonical_json(key.to_dict()), 1, 2, "frozen", view.view_id, frozen.receipt_hash),
            )
        )
        connection.executemany(
            "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)",
            attempts,
        )
        connection.execute(
            "INSERT INTO pointers(workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch) VALUES(?,?,?,?,?,?)",
            (key.workflow_id, key.code_task_id, key.code_task_version, view.view_id, 1, 1),
        )
    return key, view, frozen


def _restore_v1_update_trigger(connection: sqlite3.Connection, table: str) -> None:
    connection.execute(
        f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END"
    )


def _assert_v1_not_switched(config: RuntimeConfig) -> None:
    with sqlite3.connect(config.continuity_database) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"schema_metadata", "views", "entries", "receipts", "attempts", "pointers"} <= names
        assert not {"continuity_keys", "views_v1", "entries_v1", "receipts_v1", "attempts_v1", "pointers_v1"} & names
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "1"
        assert connection.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 1


def _v1_state_snapshot(config: RuntimeConfig) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    with sqlite3.connect(config.continuity_database) as connection:
        return tuple(
            (
                table,
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid")),
            )
            for table in ("schema_metadata", "views", "entries", "receipts", "attempts", "pointers")
        )


def _tamper_table_sql(database: Path, table: str, source: str, replacement: str) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None and isinstance(row[0], str) and source in row[0]
        connection.execute("PRAGMA writable_schema=ON")
        try:
            changed = connection.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                (row[0].replace(source, replacement, 1), table),
            ).rowcount
            assert changed == 1
        finally:
            connection.execute("PRAGMA writable_schema=OFF")


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


@pytest.mark.parametrize(
    ("table", "source", "replacement"),
    (
        ("continuity_keys", "acceptance_id TEXT NOT NULL", "acceptance_id TEXT"),
        ("continuity_keys", "acceptance_id TEXT NOT NULL", "acceptance_id BLOB NOT NULL"),
        ("continuity_keys", "key_json TEXT UNIQUE NOT NULL", "key_json TEXT NOT NULL"),
        ("entries", "byte_length INTEGER NOT NULL CHECK(byte_length>=0)", "byte_length INTEGER NOT NULL"),
        (
            "views",
            "FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash)",
            "FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash) ON DELETE CASCADE",
        ),
    ),
    ids=("not-null", "declared-type", "unique", "check", "foreign-key-action"),
)
def test_prepared_v2_open_rejects_declared_schema_contract_tampering(
    tmp_path: Path, table: str, source: str, replacement: str
) -> None:
    config, store = _prepared_store(tmp_path)
    store.close()
    _tamper_table_sql(config.continuity_database, table, source, replacement)
    with pytest.raises(ContinuityStoreError) as error:
        ContinuityStore.open_readwrite(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
    assert error.value.code == "CONTINUITY_STORE_UNPREPARED"


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TABLE sqliteevil (value TEXT)",
        "CREATE UNIQUE INDEX continuity_keys_extra_unique ON continuity_keys(workflow_id)",
        "CREATE TRIGGER pointers_extra_update BEFORE UPDATE ON pointers BEGIN SELECT 1; END",
    ),
    ids=("extra-table", "extra-unique-index", "extra-trigger"),
)
def test_prepared_v2_open_rejects_extra_schema_objects(tmp_path: Path, statement: str) -> None:
    config, store = _prepared_store(tmp_path)
    store.close()
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute(statement)
    with pytest.raises(ContinuityStoreError) as error:
        ContinuityStore.open_readwrite(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
    assert error.value.code == "CONTINUITY_STORE_UNPREPARED"


def test_runtime_bootstrap_rejects_v1_declared_schema_contract_tampering(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    _tamper_table_sql(
        config.continuity_database,
        "views",
        "manifest_hash TEXT NOT NULL",
        "manifest_hash BLOB",
    )
    with pytest.raises(RuntimeConfigError) as error:
        RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    assert error.value.code == "DATA_ROOT_UNAVAILABLE"
    _assert_v1_not_switched(config)


def test_runtime_bootstrap_normalizes_continuity_sqlite_setup_errors(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    config.data_root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute("CREATE VIEW schema_metadata AS SELECT 1 AS key, 1 AS value")
    with pytest.raises(RuntimeConfigError) as error:
        RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    assert error.value.code == "DATA_ROOT_UNAVAILABLE"


def test_runtime_bootstrap_transactionally_migrates_verified_v1_state(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    key, view, receipt = _create_verified_v1(config)
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


def test_prepared_v2_open_rejects_partial_v1_audit_tables(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute("DROP TABLE entries_v1")
    with pytest.raises(ContinuityStoreError) as error:
        ContinuityStore.open_readwrite(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
    assert error.value.code == "CONTINUITY_STORE_UNPREPARED"


def test_runtime_bootstrap_migrates_verified_legacy_direct_frozen_v1_state(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    key, view, receipt = _create_verified_v1(config, direct_frozen=True)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    migrated = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    assert migrated.current_attempt(key) == ContinuityAttempt(key, 1, "frozen", view.view_id, receipt.receipt_hash)
    assert migrated.pointer_for(key) is not None
    migrated.close()


@pytest.mark.parametrize(
    "tamper",
    (
        "manifest_hash",
        "cas_root_hash",
        "view_id",
        "receipt_payload",
        "receipt_hash",
        "attempt_receipt",
        "pointer_fence",
    ),
)
def test_runtime_bootstrap_rejects_tampered_v1_without_switching_legacy_tables(
    tmp_path: Path, tamper: str
) -> None:
    config = _unprepared_config(tmp_path)
    key, view, receipt = _create_verified_v1(config)
    with sqlite3.connect(config.continuity_database) as connection:
        if tamper == "manifest_hash":
            connection.execute("DROP TRIGGER views_immutable_update")
            connection.execute("UPDATE views SET manifest_hash=?", (_hash(b"tampered manifest"),))
            _restore_v1_update_trigger(connection, "views")
        elif tamper == "cas_root_hash":
            connection.execute("DROP TRIGGER views_immutable_update")
            connection.execute("UPDATE views SET cas_root_hash=?", (_hash(b"tampered cas root"),))
            _restore_v1_update_trigger(connection, "views")
        elif tamper == "view_id":
            changed_view_id = _hash(b"tampered view")
            for table in ("views", "entries", "receipts", "attempts"):
                connection.execute(f"DROP TRIGGER {table}_immutable_update")
            for table in ("views", "entries", "receipts", "pointers"):
                connection.execute(f"UPDATE {table} SET view_id=?", (changed_view_id,))
            connection.execute("UPDATE attempts SET view_id=? WHERE state!='claimed'", (changed_view_id,))
            for table in ("views", "entries", "receipts", "attempts"):
                _restore_v1_update_trigger(connection, table)
        elif tamper == "receipt_payload":
            connection.execute("DROP TRIGGER receipts_immutable_update")
            connection.execute("UPDATE receipts SET receipt_json='{}' WHERE receipt_hash=?", (receipt.receipt_hash,))
            _restore_v1_update_trigger(connection, "receipts")
        elif tamper == "receipt_hash":
            changed_receipt_hash = _hash(b"tampered receipt")
            for table in ("receipts", "attempts"):
                connection.execute(f"DROP TRIGGER {table}_immutable_update")
            connection.execute("UPDATE receipts SET receipt_hash=? WHERE receipt_hash=?", (changed_receipt_hash, receipt.receipt_hash))
            connection.execute("UPDATE attempts SET receipt_hash=? WHERE state='published'", (changed_receipt_hash,))
            for table in ("receipts", "attempts"):
                _restore_v1_update_trigger(connection, table)
        elif tamper == "attempt_receipt":
            published = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published")
            connection.execute("DROP TRIGGER attempts_immutable_update")
            connection.execute("UPDATE attempts SET receipt_hash=? WHERE state='frozen'", (published.receipt_hash,))
            _restore_v1_update_trigger(connection, "attempts")
        elif tamper == "pointer_fence":
            connection.execute("UPDATE pointers SET fence_epoch=2")
        else:
            raise AssertionError(f"unknown tamper: {tamper}")
    before = _v1_state_snapshot(config)
    with pytest.raises(RuntimeConfigError) as error:
        RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    assert error.value.code == "DATA_ROOT_UNAVAILABLE"
    _assert_v1_not_switched(config)
    assert _v1_state_snapshot(config) == before


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


def test_state_machine_is_only_lifecycle_write_surface_and_relations_are_immutable(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    for name in ("append_attempt_event", "insert_or_get_view", "insert_or_get_receipt", "compare_and_swap_pointer"):
        assert not hasattr(store, name)
    service = ContinuityService(
        store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    )
    key, request, evidence = _typed_inputs()
    claimed = service.claim_or_reuse(key)
    view = service.freeze(claimed, request, evidence)
    pointer = service.publish(claimed, view)
    assert pointer.view_id == view.view_id
    assert store.current_attempt(key) is not None
    assert store.current_attempt(key).state == "published"
    with pytest.raises(sqlite3.DatabaseError):
        store._connection.execute("DELETE FROM views")
    store._connection.rollback()
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


def test_service_publishes_from_original_claim_and_rejects_noncurrent_attempt(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    )
    key, request, evidence = _typed_inputs()
    claimed = service.claim_or_reuse(key)
    view = service.freeze(claimed, request, evidence)
    pointer = service.publish(claimed, view)
    assert pointer.view_id == view.view_id
    assert store.current_attempt(key).state == "published"
    published_receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published")
    store._connection.execute(
        "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)",
        (key.key_hash, canonical_json(key.to_dict()), 1, 4, "abandoned", view.view_id, published_receipt.receipt_hash),
    )
    store._connection.commit()
    assert service.claim_or_reuse(key) == ContinuityAttempt(key, 2, "claimed", None, None)
    with pytest.raises(ContinuityStoreError):
        service.publish(claimed, view)


def test_service_only_upgrades_the_original_claim_to_current_frozen_attempt(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    )
    key, request, evidence = _typed_inputs()
    claimed = service.claim_or_reuse(key)
    view = service.freeze(claimed, request, evidence)
    forged = ContinuityAttempt(key, claimed.fence_epoch, "published", view.view_id, _hash(b"forged"))
    with pytest.raises(ContinuityStoreError):
        service.publish(forged, view)
    assert service.publish(claimed, view).view_id == view.view_id


def test_claim_or_reuse_reuses_active_attempt_at_each_lifecycle_state(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    )
    key, request, evidence = _typed_inputs()
    claimed = service.claim_or_reuse(key)
    assert service.claim_or_reuse(key) == claimed
    view = service.freeze(claimed, request, evidence)
    frozen = store.current_attempt(key)
    assert frozen is not None and frozen.state == "frozen"
    assert service.claim_or_reuse(key) == frozen
    service.publish(claimed, view)
    published = store.current_attempt(key)
    assert published is not None and published.state == "published"
    assert service.claim_or_reuse(key) == published


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


def test_atomic_current_fence_rejects_stale_freeze_and_publish(tmp_path: Path) -> None:
    _, store = _prepared_store(tmp_path)
    key, view = _key(), _view(_key())
    first = store.claim_or_reuse_atomic(key)
    frozen_receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    frozen = store.freeze_attempt_atomic(first, view, frozen_receipt)
    store.publish_attempt_atomic(frozen, view)
    published = store.current_attempt(key)
    assert published is not None and published.state == "published"
    published_receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published")
    store._connection.executemany(
        "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)",
        (
            (key.key_hash, canonical_json(key.to_dict()), 1, 4, "abandoned", view.view_id, published_receipt.receipt_hash),
        ),
    )
    store._connection.commit()
    second = store.claim_or_reuse_atomic(key)
    assert second == ContinuityAttempt(key, 2, "claimed", None, None)
    receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
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


@pytest.mark.parametrize("error_type", (RuntimeError, TypeError))
def test_atomic_rolls_back_unexpected_exception_and_connection_remains_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    _, store = _prepared_store(tmp_path)
    key = _key()
    original = store._append_attempt_row

    def explode(*_args: object) -> None:
        raise error_type("injected")

    monkeypatch.setattr(store, "_append_attempt_row", explode)
    with pytest.raises(error_type, match="injected"):
        store.claim_or_reuse_atomic(key)
    assert store._connection.execute("SELECT COUNT(*) FROM continuity_keys").fetchone()[0] == 0
    monkeypatch.setattr(store, "_append_attempt_row", original)
    assert store.claim_or_reuse_atomic(key) == ContinuityAttempt(key, 1, "claimed", None, None)


def test_atomic_initial_pointer_integrity_race_normalizes_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _prepared_store(tmp_path)
    key, view = _key(), _view(_key())
    frozen = store.freeze_attempt_atomic(
        store.claim_or_reuse_atomic(key),
        view,
        ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen"),
    )

    def duplicate(*_args: object) -> None:
        raise sqlite3.IntegrityError("duplicate pointer")

    monkeypatch.setattr(store, "_insert_pointer_row", duplicate, raising=False)
    with pytest.raises(ContinuityError):
        store.publish_attempt_atomic(frozen, view)
    assert store.pointer_for(key) is None
    assert store.current_attempt(key) == frozen


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
