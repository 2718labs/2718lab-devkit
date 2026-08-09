"""Private durable-storage tests for Continuity (CP-B)."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
from devkit_continuity import store as continuity_store  # noqa: E402
from devkit_continuity.canonical import canonical_json  # noqa: E402
from devkit_continuity.cas import ContinuityCas, ContinuityCasError  # noqa: E402
from devkit_continuity.models import (  # noqa: E402
    ContinuityAttempt,
    ContinuityError,
    ContinuityKey,
    ContinuityReceipt,
    FrozenEntry,
    FrozenView,
    ReplayMetadata,
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
        "workflow", "task", request.acceptance_id, "code", "intent", _hash(b"workspace"), "checkpoint", "input", "output", ("src",),
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


_V1_TABLE_NAMES = ("schema_metadata", "views", "entries", "receipts", "attempts", "pointers")
_V2_LIVE_IMMUTABLE_TABLES = ("continuity_keys", "views", "entries", "receipts", "attempts")


def _create_legacy_v2_triggers(connection: sqlite3.Connection) -> None:
    for table in _V2_LIVE_IMMUTABLE_TABLES:
        for action in ("update", "delete"):
            connection.execute(
                f"CREATE TRIGGER {table}_immutable_{action} BEFORE {action.upper()} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END"
            )


def _create_legacy_empty_v2(config: RuntimeConfig) -> None:
    config.data_root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.continuity_database) as connection:
        for statement in continuity_store._V2_TABLE_SQL.values():
            connection.execute(statement)
        connection.execute("INSERT INTO schema_metadata(key,value) VALUES('schema_version','2')")
        _create_legacy_v2_triggers(connection)


def _create_legacy_populated_v2(
    config: RuntimeConfig,
) -> tuple[ContinuityKey, FrozenView]:
    config.data_root.mkdir(parents=True, exist_ok=True)
    key = _key()
    view = _view(key)
    frozen = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen")
    published = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published")
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in continuity_store._V2_TABLE_SQL.values():
            connection.execute(statement)
        connection.execute("INSERT INTO schema_metadata(key,value) VALUES('schema_version','2')")
        _create_legacy_v2_triggers(connection)
        connection.execute(
            "INSERT INTO continuity_keys(key_hash,key_json,workflow_id,code_task_id,"
            "code_task_version,acceptance_id,ingestion_key,payload_hash,evidence_binding_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key.key_hash,
                canonical_json(key.to_dict()),
                key.workflow_id,
                key.code_task_id,
                key.code_task_version,
                key.acceptance_id,
                key.ingestion_key,
                key.payload_hash,
                key.evidence_binding_hash,
            ),
        )
        connection.execute(
            "INSERT INTO views(view_id,key_hash,manifest_hash,cas_root_hash,manifest_json) "
            "VALUES(?,?,?,?,?)",
            (view.view_id, key.key_hash, view.manifest_hash, view.cas_root_hash, view.manifest_json),
        )
        connection.executemany(
            "INSERT INTO entries(view_id,role,path,content_hash,byte_length) VALUES(?,?,?,?,?)",
            [
                (view.view_id, item.role, item.path, item.content_hash, item.byte_length)
                for item in view.entries
            ],
        )
        for receipt in (frozen, published):
            connection.execute(
                "INSERT INTO receipts(receipt_hash,key_hash,view_id,kind,receipt_json) "
                "VALUES(?,?,?,?,?)",
                (
                    receipt.receipt_hash,
                    key.key_hash,
                    view.view_id,
                    receipt.kind,
                    canonical_json(
                        {
                            "key": key.to_dict(),
                            "view_id": view.view_id,
                            "kind": receipt.kind,
                        }
                    ),
                ),
            )
        connection.executemany(
            "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                (key.key_hash, canonical_json(key.to_dict()), 1, 1, "claimed", None, None),
                (key.key_hash, canonical_json(key.to_dict()), 1, 2, "frozen", view.view_id, frozen.receipt_hash),
                (key.key_hash, canonical_json(key.to_dict()), 1, 3, "published", view.view_id, published.receipt_hash),
            ),
        )
        connection.execute(
            "INSERT INTO pointers(key_hash,workflow_id,code_task_id,code_task_version,view_id,"
            "pointer_version,fence_epoch) VALUES(?,?,?,?,?,?,?)",
            (
                key.key_hash,
                key.workflow_id,
                key.code_task_id,
                key.code_task_version,
                view.view_id,
                1,
                1,
            ),
        )
    return key, view


def _create_legacy_v2_with_unsealed_audit(config: RuntimeConfig) -> None:
    key, _view_value, _receipt = _create_verified_v1(config)
    with sqlite3.connect(config.continuity_database) as connection:
        for table in ("views", "entries", "receipts", "attempts"):
            for action in ("update", "delete"):
                connection.execute(f"DROP TRIGGER {table}_immutable_{action}")
        for table in _V1_TABLE_NAMES:
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
        for statement in continuity_store._V2_TABLE_SQL.values():
            connection.execute(statement)
        connection.execute("INSERT INTO schema_metadata(key,value) VALUES('schema_version','2')")
        _create_legacy_v2_triggers(connection)
        connection.execute(
            "INSERT INTO continuity_keys(key_hash,key_json,workflow_id,code_task_id,"
            "code_task_version,acceptance_id,ingestion_key,payload_hash,evidence_binding_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key.key_hash,
                canonical_json(key.to_dict()),
                key.workflow_id,
                key.code_task_id,
                key.code_task_version,
                key.acceptance_id,
                key.ingestion_key,
                key.payload_hash,
                key.evidence_binding_hash,
            ),
        )
        for table in ("views", "entries", "receipts", "attempts"):
            columns = ",".join(
                row[1] for row in connection.execute(f"PRAGMA table_xinfo({table}_v1)")
            )
            connection.execute(
                f"INSERT INTO {table}({columns}) SELECT {columns} FROM {table}_v1"
            )
        pointer = connection.execute(
            "SELECT workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch "
            "FROM pointers_v1"
        ).fetchone()
        assert pointer is not None
        connection.execute(
            "INSERT INTO pointers(key_hash,workflow_id,code_task_id,code_task_version,view_id,"
            "pointer_version,fence_epoch) VALUES(?,?,?,?,?,?,?)",
            (key.key_hash, *pointer),
        )


def _continuity_database_snapshot(config: RuntimeConfig) -> tuple[object, ...]:
    with sqlite3.connect(config.continuity_database) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE lower(substr(name,1,7)) != 'sqlite_' ORDER BY type,name"
            )
        )
        tables = tuple(row[1] for row in objects if row[0] == "table")
        return (
            connection.execute("PRAGMA journal_mode").fetchone()[0],
            objects,
            tuple(
                (table, tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid")))
                for table in tables
            ),
        )


def _audit_trigger_names() -> set[str]:
    return {
        f"{table}_v1_immutable_{action}"
        for table in _V1_TABLE_NAMES
        for action in ("insert", "update", "delete")
    }


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
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "3"
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
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "3"
        assert {"views_v1", "entries_v1", "receipts_v1", "attempts_v1", "pointers_v1"} <= {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    migrated = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    assert migrated.current_attempt(key) == ContinuityAttempt(key, 1, "frozen", view.view_id, receipt.receipt_hash)
    assert migrated.pointer_for(key) is not None
    migrated.close()


def test_fresh_v3_has_an_empty_immutable_audit_seal_relation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        assert connection.execute("SELECT COUNT(*) FROM v1_audit_seals").fetchone()[0] == 0
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    assert {
        "v1_audit_seals_immutable_insert",
        "v1_audit_seals_immutable_update",
        "v1_audit_seals_immutable_delete",
    } <= trigger_names


def test_v1_migration_creates_a_v3_audit_seal_and_all_audit_triggers(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        seals = tuple(
            connection.execute(
                "SELECT audit_id,source_version,content_hash,schema_hash FROM v1_audit_seals"
            )
        )
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    assert len(seals) == 1
    assert seals[0][1] == 1
    assert all(isinstance(value, str) and value.startswith("sha256:") for value in seals[0][2:])
    assert _audit_trigger_names() <= trigger_names


@pytest.mark.parametrize(
    "statement",
    (
        "INSERT INTO schema_metadata_v1(key,value) VALUES('other','value')",
        "UPDATE views_v1 SET manifest_hash='tampered'",
        "DELETE FROM pointers_v1",
    ),
    ids=("insert", "update", "delete"),
)
def test_v1_audit_triggers_reject_ordinary_mutations(tmp_path: Path, statement: str) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)


@pytest.mark.parametrize("read_only", (True, False), ids=("readonly", "readwrite"))
def test_audit_seal_rejects_bypassed_historical_pointer_mutation(
    tmp_path: Path, read_only: bool
) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    trigger_name = "pointers_v1_immutable_update"
    with sqlite3.connect(config.continuity_database) as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()
        if trigger is not None:
            connection.execute(f"DROP TRIGGER {trigger_name}")
        assert connection.execute(
            "UPDATE pointers_v1 SET pointer_version=pointer_version+1"
        ).rowcount == 1
        if trigger is not None:
            connection.execute(trigger[0])
    with pytest.raises(ContinuityStoreError) as error:
        (ContinuityStore.open_readonly if read_only else ContinuityStore.open_readwrite)(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
    assert error.value.code == "CONTINUITY_STORE_UNPREPARED"


def test_legacy_v2_with_unsealed_audit_is_rejected_without_writes(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_legacy_v2_with_unsealed_audit(config)
    assert not config.continuity_cas_root.exists()
    before = _continuity_database_snapshot(config)
    with pytest.raises(RuntimeConfigError) as error:
        RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    assert error.value.code == "DATA_ROOT_UNAVAILABLE"
    assert _continuity_database_snapshot(config) == before
    assert not config.continuity_cas_root.exists()


def test_legacy_v2_without_audit_migrates_to_v3(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_legacy_empty_v2(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        assert connection.execute("SELECT COUNT(*) FROM v1_audit_seals").fetchone()[0] == 0


def test_legacy_populated_v2_without_audit_migrates_losslessly_to_v3(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    key, view = _create_legacy_populated_v2(config)
    tables = ("continuity_keys", "views", "entries", "receipts", "attempts", "pointers")
    with sqlite3.connect(config.continuity_database) as connection:
        before = {
            table: tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in tables
        }
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        after = {
            table: tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in tables
        }
        assert connection.execute("SELECT COUNT(*) FROM v1_audit_seals").fetchone()[0] == 0
    assert after == before
    store = ContinuityStore.open_readonly(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    expected = ContinuityAttempt(
        key,
        1,
        "published",
        view.view_id,
        ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published").receipt_hash,
    )
    assert store.current_attempt(key) == expected
    assert store.pointer_for(key) is not None
    store.close()


def test_live_pointer_progression_does_not_break_the_audit_seal(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    key, view, _receipt = _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        seal_before = connection.execute(
            "SELECT audit_id,source_version,content_hash,schema_hash FROM v1_audit_seals"
        ).fetchone()
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    frozen = store.current_attempt(key)
    assert frozen is not None and frozen.state == "frozen"
    store.publish_attempt_atomic(frozen, view)
    published_receipt = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="published")
    store._connection.execute(
        "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            key.key_hash,
            canonical_json(key.to_dict()),
            1,
            4,
            "abandoned",
            view.view_id,
            published_receipt.receipt_hash,
        ),
    )
    store._connection.commit()
    claimed = store.claim_or_reuse_atomic(key)
    frozen_next = store.freeze_attempt_atomic(
        claimed,
        view,
        ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen"),
    )
    pointer = store.publish_attempt_atomic(frozen_next, view)
    assert pointer.fence_epoch == 2 and pointer.pointer_version == 2
    store.close()
    with sqlite3.connect(config.continuity_database) as connection:
        seal_after = connection.execute(
            "SELECT audit_id,source_version,content_hash,schema_hash FROM v1_audit_seals"
        ).fetchone()
    assert seal_after == seal_before
    reopened = ContinuityStore.open_readonly(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    reopened.close()


def test_prepared_open_rejects_tampered_v1_audit_table_shape(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        connection.execute("ALTER TABLE views_v1 ADD COLUMN evil TEXT")
    with pytest.raises(ContinuityStoreError) as error:
        ContinuityStore.open_readwrite(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
    assert error.value.code == "CONTINUITY_STORE_UNPREPARED"


def test_prepared_readonly_open_rejects_mutated_v1_audit_rows(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    _create_verified_v1(config)
    RuntimeBootstrap.run(config, proof_registry_bootstrap=lambda database: sqlite3.connect(database).close())
    with sqlite3.connect(config.continuity_database) as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='views_v1_immutable_update'"
        ).fetchone()
        assert trigger is not None
        connection.execute("DROP TRIGGER views_v1_immutable_update")
        assert connection.execute(
            "UPDATE views_v1 SET manifest_hash='tampered'"
        ).rowcount == 1
        connection.execute(trigger[0])
    with pytest.raises(ContinuityStoreError) as error:
        ContinuityStore.open_readonly(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
    assert error.value.code == "CONTINUITY_STORE_UNPREPARED"


def test_prepared_v3_open_rejects_partial_v1_audit_tables(tmp_path: Path) -> None:
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


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_real_publish_cleans_stage_without_removing_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    body, digest = b"native-stage", _hash(b"native-stage")
    cas = ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False)
    assert cas._native_backend is not None
    assert cas.put_verified(digest, len(body), body) == digest
    assert not tuple((config.continuity_cas_root / ".staging").glob("*.stage"))
    cas.close()

    reopened = ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=True)
    assert reopened.read_verified(digest, len(body)) == body
    reopened.close()


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


def test_service_freeze_persists_complete_typed_replay_metadata(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store,
        ContinuityCas.open_prepared(
            config.continuity_cas_root, config.scratch_root, read_only=False
        ),
    )
    key, request, evidence = _typed_inputs()

    view = service.freeze(service.claim_or_reuse(key), request, evidence)

    assert view.replay_metadata == ReplayMetadata(
        task_kind="code",
        intent_id=request.intent_id,
        workspace_hash=evidence.extraction_request.workspace_hash,
        write_scope=evidence.extraction_request.write_scope,
        indexed_diff_hash=request.indexed_diff_hash,
        language=request.language,
        framework=request.framework,
        checkpoint_hash=request.checkpoint_hash,
    )
    assert '"schema":"continuity-frozen-view/v2"' in view.manifest_json


def test_v1_audit_parser_keeps_legacy_manifest_nonreplayable(tmp_path: Path) -> None:
    config = _unprepared_config(tmp_path)
    key, view, _receipt = _create_verified_v1(config)
    with sqlite3.connect(config.continuity_database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT view_id,key_hash,manifest_hash,cas_root_hash,manifest_json FROM views"
        ).fetchone()
    assert row is not None

    parsed = continuity_store._v1_frozen_view(key, row, view.entries)

    assert parsed == view
    assert parsed.replay_metadata is None


@pytest.mark.parametrize("tamper", ("unknown", "missing"))
def test_manifest_parser_rejects_unknown_or_incomplete_v2_payload(
    tmp_path: Path, tamper: str
) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store,
        ContinuityCas.open_prepared(
            config.continuity_cas_root, config.scratch_root, read_only=False
        ),
    )
    key, request, evidence = _typed_inputs()
    view = service.freeze(service.claim_or_reuse(key), request, evidence)
    row = store._connection.execute(
        "SELECT view_id,key_hash,manifest_hash,cas_root_hash,manifest_json FROM views"
    ).fetchone()
    assert row is not None
    manifest = json.loads(row["manifest_json"])
    if tamper == "unknown":
        manifest["unexpected"] = True
    else:
        del manifest["replay_metadata"]
    tampered = dict(row)
    tampered["manifest_json"] = canonical_json(manifest)

    with pytest.raises(ContinuityStoreError):
        continuity_store._frozen_view_from_manifest(key, tampered, view.entries)


def test_prepared_open_revalidates_persisted_v2_manifest_schema(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store,
        ContinuityCas.open_prepared(
            config.continuity_cas_root, config.scratch_root, read_only=False
        ),
    )
    key, request, evidence = _typed_inputs()
    service.freeze(service.claim_or_reuse(key), request, evidence)
    store.close()
    with sqlite3.connect(config.continuity_database) as connection:
        manifest_json = connection.execute("SELECT manifest_json FROM views").fetchone()[0]
        manifest = json.loads(manifest_json)
        manifest["unexpected"] = True
        connection.execute("DROP TRIGGER views_immutable_update")
        connection.execute(
            "UPDATE views SET manifest_json=?", (canonical_json(manifest),)
        )
        _restore_v1_update_trigger(connection, "views")
    with pytest.raises(ContinuityStoreError):
        ContinuityStore.open_readwrite(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )


def test_prepared_reopen_rebuilds_bound_v2_manifest(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(
        store,
        ContinuityCas.open_prepared(
            config.continuity_cas_root, config.scratch_root, read_only=False
        ),
    )
    key, request, evidence = _typed_inputs()
    view = service.freeze(service.claim_or_reuse(key), request, evidence)
    store.close()

    reopened = ContinuityStore.open_readonly(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )

    assert reopened.current_attempt(key) == ContinuityAttempt(
        key, 1, "frozen", view.view_id, ContinuityReceipt.create(
            key=key, view_id=view.view_id, kind="frozen"
        ).receipt_hash
    )
    reopened.close()


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


@pytest.mark.skipif(os.name == "nt", reason="portable path CAS revalidation hook")
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


def test_typed_receipt_workspace_mismatch_fails_before_cas_or_database_write(
    tmp_path: Path,
) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False))
    key, request, evidence = _typed_inputs()
    altered_receipt = replace(
        evidence.extraction_request.execution_receipts[0], workspace_hash=_hash(b"other-workspace")
    )
    altered_extraction = replace(
        evidence.extraction_request, execution_receipts=(altered_receipt,)
    )
    altered_evidence = replace(evidence, extraction_request=altered_extraction)

    with pytest.raises(ContinuityStoreError):
        service.freeze(
            ContinuityAttempt(key, 1, "claimed", None, None), request, altered_evidence
        )

    assert not (config.continuity_cas_root / "sha256").exists()
    assert store._connection.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 0
    assert store._connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_invalid_typed_file_fails_before_cas_or_database_write(tmp_path: Path) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False))
    key, request, evidence = _typed_inputs()
    invalid_file = replace(evidence.extraction_request.after_files[0], path="/unsafe.py")
    altered_extraction = replace(evidence.extraction_request, after_files=(invalid_file,))
    altered_evidence = replace(evidence, extraction_request=altered_extraction)

    with pytest.raises(ContinuityError):
        service.freeze(
            ContinuityAttempt(key, 1, "claimed", None, None), request, altered_evidence
        )

    assert not (config.continuity_cas_root / "sha256").exists()
    assert store._connection.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 0
    assert store._connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field",
    ("payload_hash", "evidence_binding_hash", "ingestion_key", "acceptance_id"),
)
def test_forged_request_canonical_linkage_fails_before_cas_or_database_write(
    tmp_path: Path, field: str
) -> None:
    config, store = _prepared_store(tmp_path)
    service = ContinuityService(store, ContinuityCas.open_prepared(config.continuity_cas_root, config.scratch_root, read_only=False))
    key, request, evidence = _typed_inputs()
    forged_request = replace(request, **{field: _hash(f"forged-{field}".encode())})
    forged_key = replace(key, **{field: getattr(forged_request, field)})

    with pytest.raises(ContinuityStoreError) as error:
        service.freeze(
            ContinuityAttempt(forged_key, 1, "claimed", None, None),
            forged_request,
            evidence,
        )

    assert error.value.code == "CONTINUITY_INPUT_INVALID"
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


@pytest.mark.skipif(os.name == "nt", reason="portable path CAS stage hook")
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


@pytest.mark.skipif(os.name == "nt", reason="portable path CAS write hook")
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


class _MemoryNativeHandle:
    def __init__(
        self, path: tuple[str, ...], *, directory: bool, identity: int, delete_on_close: bool = False
    ) -> None:
        self.path = path
        self.directory = directory
        self.identity = identity
        self.delete_on_close = delete_on_close
        self.closed = False


class _MemoryNativeApi:
    """Handle-only test facade; no method accepts a child filesystem path."""

    def __init__(self) -> None:
        self.directories: set[tuple[str, ...]] = {()}
        self.files: dict[tuple[str, ...], bytes] = {}
        self.identities: dict[tuple[str, ...], int] = {(): 1}
        self._next_identity = 2
        self.events: list[tuple[object, ...]] = []
        self.reject_paths: set[tuple[str, ...]] = set()
        self.link_error: OSError | None = None
        self.on_flush: Callable[[], None] | None = None
        self.on_link: Callable[[_MemoryNativeHandle, tuple[str, ...]], None] | None = None
        self.close_fail_paths: set[tuple[str, ...]] = set()
        self.live_handles: list[_MemoryNativeHandle] = []
        self.opened_root_paths: list[Path] = []
        self.fail_root_paths: set[Path] = set()
        self._lock = threading.Lock()

    def open_root(self, _root: Path, *, writable: bool) -> _MemoryNativeHandle:
        self.opened_root_paths.append(_root)
        if _root in self.fail_root_paths:
            raise OSError("trusted parent unavailable")
        self.events.append(("open_root", writable))
        return self._new_handle((), directory=True)

    def open_child(
        self,
        parent: _MemoryNativeHandle,
        name: str,
        *,
        directory: bool,
        create: bool,
        exclusive: bool = False,
        delete_on_close: bool = False,
    ) -> _MemoryNativeHandle:
        assert isinstance(parent, _MemoryNativeHandle)
        assert not parent.closed and "/" not in name and "\\" not in name and name not in {"", ".", ".."}
        path = parent.path + (name,)
        self.events.append(
            ("open_child", parent.path, name, directory, create, exclusive, delete_on_close)
        )
        with self._lock:
            if directory:
                if path not in self.directories:
                    if not create:
                        raise FileNotFoundError(name)
                    self.directories.add(path)
                    self._assign_identity(path)
                return self._new_handle(path, directory=True)
            if path not in self.files:
                if not create:
                    raise FileNotFoundError(name)
                self.files[path] = b""
                self._assign_identity(path)
            elif exclusive:
                raise FileExistsError(name)
        return self._new_handle(path, directory=False, delete_on_close=delete_on_close)

    def revalidate(self, handle: _MemoryNativeHandle, *, directory: bool) -> None:
        assert isinstance(handle, _MemoryNativeHandle)
        self.events.append(("revalidate", handle.path, directory))
        if (
            handle.closed
            or handle.directory is not directory
            or handle.path in self.reject_paths
            or self.identities.get(handle.path) != handle.identity
        ):
            raise OSError("reparse or identity changed")

    def write_all(self, handle: _MemoryNativeHandle, body: bytes) -> None:
        self.events.append(("write", handle.path))
        assert not handle.directory
        with self._lock:
            self.files[handle.path] = body

    def flush(self, handle: _MemoryNativeHandle) -> None:
        self.events.append(("flush", handle.path))
        if self.on_flush is not None:
            self.on_flush()

    def rewind(self, handle: _MemoryNativeHandle) -> None:
        self.events.append(("rewind", handle.path))

    def read_all(self, handle: _MemoryNativeHandle) -> bytes:
        self.events.append(("read", handle.path))
        with self._lock:
            return self.files[handle.path]

    def link(
        self, source: _MemoryNativeHandle, target_parent: _MemoryNativeHandle, target_name: str
    ) -> bool:
        self.events.append(("link", source.path, target_parent.path, target_name))
        if self.link_error is not None:
            raise self.link_error
        target = target_parent.path + (target_name,)
        with self._lock:
            if target in self.files:
                return False
            self.files[target] = self.files[source.path]
            self.identities[target] = source.identity
        if self.on_link is not None:
            self.on_link(source, target)
        return True

    def delete_owned(self, handle: _MemoryNativeHandle) -> None:
        self.events.append(("delete_owned", handle.path))
        assert len(handle.path) >= 2 and handle.path[-2] == ".staging"
        with self._lock:
            self.files.pop(handle.path, None)

    def close(self, handle: _MemoryNativeHandle) -> None:
        self.events.append(("close", handle.path))
        if handle.delete_on_close:
            with self._lock:
                self.files.pop(handle.path, None)
                self.identities.pop(handle.path, None)
        handle.closed = True
        self.live_handles.remove(handle)
        if handle.path in self.close_fail_paths:
            raise OSError("injected close failure")

    def _new_handle(
        self, path: tuple[str, ...], *, directory: bool, delete_on_close: bool = False
    ) -> _MemoryNativeHandle:
        handle = _MemoryNativeHandle(
            path,
            directory=directory,
            identity=self.identities[path],
            delete_on_close=delete_on_close,
        )
        self.live_handles.append(handle)
        return handle

    def _assign_identity(self, path: tuple[str, ...]) -> None:
        self.identities[path] = self._next_identity
        self._next_identity += 1


def _windows_backend_for_test(api: _MemoryNativeApi, root: Path) -> object:
    from devkit_continuity import cas as cas_module

    backend_type = getattr(cas_module, "_WindowsHandleCasBackend", None)
    assert backend_type is not None, "native handle backend is unavailable"
    backend = backend_type(root, api, read_only=False)
    backend.verify_prepared()
    return backend


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_prepared_open_never_touches_scratch_or_path_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    root, scratch = tmp_path / "data" / "continuity-cas", tmp_path / "unused-scratch"
    root.mkdir(parents=True)
    api = _MemoryNativeApi()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Windows native CAS must not use Path/os fallback")

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: api)
    monkeypatch.setattr(cas_module, "_safe_root", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    cas = ContinuityCas.open_prepared(root, scratch, read_only=False)
    assert cas._native_backend is not None
    assert api.opened_root_paths == [root]
    assert scratch not in api.opened_root_paths
    cas.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_prepared_open_leaves_missing_scratch_unused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scratch = tmp_path / "missing-scratch"
    cas = ContinuityCas.open_prepared(config.continuity_cas_root, scratch, read_only=False)
    body, digest = b"no-scratch", _hash(b"no-scratch")
    assert cas.put_verified(digest, len(body), body) == digest
    assert not scratch.exists()
    cas.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_bootstrap_creates_root_relative_to_trusted_parent_without_path_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    data_root = tmp_path / "trusted-data"
    data_root.mkdir()
    root, scratch = data_root / "continuity-cas", tmp_path / "unused-scratch"
    api = _MemoryNativeApi()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Windows native CAS bootstrap must not use Path/os fallback")

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: api)
    monkeypatch.setattr(cas_module, "_safe_root", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    cas = cas_module._bootstrap_cas(root, scratch)
    assert cas._native_backend is not None
    assert api.opened_root_paths == [data_root]
    assert scratch not in api.opened_root_paths
    assert (
        "open_child",
        (),
        "continuity-cas",
        True,
        True,
        False,
        False,
    ) in api.events
    body, digest = b"bootstrap-root", _hash(b"bootstrap-root")
    api.events.clear()
    assert cas.put_verified(digest, len(body), body) == digest
    assert not [event for event in api.events if event[0] == "open_root"]
    assert [handle.path for handle in api.live_handles] == [("continuity-cas",)]
    cas.close()
    assert api.live_handles == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_bootstrap_closes_root_child_when_parent_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    data_root = tmp_path / "trusted-data"
    data_root.mkdir()
    api = _MemoryNativeApi()
    api.close_fail_paths.add(())
    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: api)

    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        cas_module._bootstrap_cas(data_root / "continuity-cas", tmp_path / "unused-scratch")

    assert api.live_handles == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_bootstrap_fails_closed_when_trusted_parent_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    root, scratch = tmp_path / "missing-data" / "continuity-cas", tmp_path / "unused-scratch"
    api = _MemoryNativeApi()
    api.fail_root_paths.add(root.parent)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Windows native CAS bootstrap must not use Path/os fallback")

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: api)
    monkeypatch.setattr(cas_module, "_safe_root", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        cas_module._bootstrap_cas(root, scratch)
    assert api.opened_root_paths == [root.parent]


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_bootstrap_rejects_nonabsolute_root_before_any_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_continuity import cas as cas_module

    api = _MemoryNativeApi()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Windows native CAS bootstrap must not use Path/os fallback")

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: api)
    monkeypatch.setattr(cas_module, "_safe_root", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        cas_module._bootstrap_cas(Path("relative") / "continuity-cas", Path("scratch"))
    assert api.opened_root_paths == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_prepared_open_rejects_nonabsolute_root_before_any_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_continuity import cas as cas_module

    api = _MemoryNativeApi()

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: api)
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        ContinuityCas.open_prepared(Path("relative") / "continuity-cas", Path("scratch"), read_only=True)
    assert api.opened_root_paths == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_cas_loader_absence_fails_closed_without_path_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    root, scratch = tmp_path / "continuity-cas", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        calls.append("path-primitive")
        raise AssertionError("path primitive must not run")

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: None)
    monkeypatch.setattr(cas_module, "_safe_root", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        ContinuityCas.open_prepared(root, scratch, read_only=False)
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_cas_direct_constructor_cannot_enable_path_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    root, scratch = tmp_path / "continuity-cas", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Windows CAS must not use portable path operations")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        ContinuityCas(root, scratch, read_only=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_cas_delegates_without_path_primitives_and_closes_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    root, scratch = tmp_path / "continuity-cas", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    calls: list[tuple[object, ...]] = []

    class Backend:
        def put_verified(self, content_hash: str, byte_length: int, body: bytes) -> str:
            calls.append(("put", content_hash, byte_length, body))
            return content_hash

        def read_verified(self, content_hash: str, byte_length: int) -> bytes:
            calls.append(("read", content_hash, byte_length))
            return b"native"

        def close(self) -> None:
            calls.append(("close",))

    backend = Backend()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("portable path primitive must not run")

    monkeypatch.setattr(cas_module, "_open_native_backend", lambda *_args, **_kwargs: backend, raising=False)
    monkeypatch.setattr(cas_module, "_safe_root", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(cas_module.os, "open", forbidden)
    monkeypatch.setattr(cas_module.os, "link", forbidden)
    digest = _hash(b"native")
    cas = ContinuityCas.open_prepared(root, scratch, read_only=False)
    assert cas.put_verified(digest, 6, b"native") == digest
    assert cas.read_verified(digest, 6) == b"native"
    cas.close()
    assert calls == [("put", digest, 6, b"native"), ("read", digest, 6), ("close",)]


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_cas_native_exceptions_are_always_stable_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    root, scratch = tmp_path / "continuity-cas", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: (_ for _ in ()).throw(RuntimeError("bad loader")))
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        ContinuityCas.open_prepared(root, scratch, read_only=False)

    class Backend:
        def put_verified(self, _content_hash: str, _byte_length: int, _body: bytes) -> str:
            raise RuntimeError("bad native write")

        def read_verified(self, _content_hash: str, _byte_length: int) -> bytes:
            raise RuntimeError("bad native read")

        def close(self) -> None:
            return None

    monkeypatch.setattr(cas_module, "_open_native_backend", lambda *_args, **_kwargs: Backend())
    cas = ContinuityCas.open_prepared(root, scratch, read_only=False)
    digest = _hash(b"body")
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        cas.put_verified(digest, 4, b"body")
    with pytest.raises(ContinuityCasError, match="^CONTINUITY_CAS_UNAVAILABLE$"):
        cas.read_verified(digest, 4)


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_loader_fails_closed_on_non_x64_abi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_continuity import cas as cas_module

    original_sizeof = cas_module.ctypes.sizeof

    def sizeof(value: object) -> int:
        if value is cas_module.ctypes.c_void_p:
            return 4
        return original_sizeof(value)

    monkeypatch.setattr(cas_module.ctypes, "sizeof", sizeof)
    monkeypatch.setattr(
        cas_module,
        "_WindowsNativeApi",
        lambda: pytest.fail("x64-only native ABI must not initialize"),
    )
    assert cas_module._load_windows_native_api() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_concurrent_publish_has_one_target_and_no_owned_stages(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    first = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    second = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"same-body", _hash(b"same-body")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda backend: backend.put_verified(digest, len(body), body),
                (first, second),
            )
        )
    target = ("sha256", digest[7:9], digest[9:11], digest[7:])
    assert results == (digest, digest)
    assert api.files[target] == body
    assert [path for path in api.files if path[:1] == (".staging",)] == []
    stage_opens = [event for event in api.events if event[0] == "open_child" and str(event[2]).endswith(".stage")]
    assert stage_opens and all(event[-1] is True for event in stage_opens)


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_link_failure_cleans_only_owned_stage(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    api.link_error = OSError("injected link failure")
    backend = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    sentinel = ("external", "sentinel")
    api.files[sentinel] = b"untouched"
    body, digest = b"failure", _hash(b"failure")
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.put_verified(digest, len(body), body)
    assert api.files[sentinel] == b"untouched"
    assert [path for path in api.files if path[:1] == (".staging",)] == []
    assert any(event[0] == "delete_owned" for event in api.events)
    api.link_error = None
    assert backend.put_verified(digest, len(body), body) == digest


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_revalidates_relative_tree_and_leaf_before_each_read(
    tmp_path: Path,
) -> None:
    api = _MemoryNativeApi()
    backend = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"revalidate", _hash(b"revalidate")
    assert backend.put_verified(digest, len(body), body) == digest
    target = ("sha256", digest[7:9], digest[9:11], digest[7:])
    api.events.clear()
    assert backend.read_verified(digest, len(body)) == body
    tree_paths = (
        (),
        ("sha256",),
        ("sha256", digest[7:9]),
        ("sha256", digest[7:9], digest[9:11]),
    )
    for path in tree_paths:
        assert ("revalidate", path, True) in api.events
    leaf_verify = ("revalidate", target, False)
    leaf_read = ("read", target)
    assert leaf_verify in api.events and leaf_read in api.events
    assert api.events.index(leaf_verify) < api.events.index(leaf_read)

    api.reject_paths.add(target)
    api.events.clear()
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.read_verified(digest, len(body))
    assert leaf_read not in api.events

    api.reject_paths.clear()
    api.reject_paths.add(())
    api.events.clear()
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.read_verified(digest, len(body))
    assert not [event for event in api.events if event[0] == "open_child"]


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_fails_before_link_when_held_child_identity_changes(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    backend = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"swapped-child", _hash(b"swapped-child")
    target_parent = ("sha256", digest[7:9], digest[9:11])
    sentinel = ("external", "sentinel")
    api.files[sentinel] = b"untouched"

    def swap_child_identity() -> None:
        api.reject_paths.add(target_parent)

    api.on_flush = swap_child_identity
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.put_verified(digest, len(body), body)
    assert not [event for event in api.events if event[0] == "link"]
    assert api.files[sentinel] == b"untouched"
    assert [path for path in api.files if path[:1] == (".staging",)] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_rejects_post_publish_leaf_not_linked_to_owned_stage(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    backend = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"identity-link", _hash(b"identity-link")

    def replace_published_leaf(source: _MemoryNativeHandle, target: tuple[str, ...]) -> None:
        api.identities[target] = source.identity + 1000

    api.on_link = replace_published_leaf
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.put_verified(digest, len(body), body)
    assert [path for path in api.files if path[:1] == (".staging",)] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_readonly_retains_only_read_handles(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    writer = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"readonly", _hash(b"readonly")
    assert writer.put_verified(digest, len(body), body) == digest
    backend_type = type(writer)
    reader = backend_type(tmp_path / "continuity-cas", api, read_only=True)
    reader.verify_prepared()
    assert ("open_root", False) in api.events
    api.events.clear()
    assert reader.read_verified(digest, len(body)) == body
    assert not [event for event in api.events if event[0] == "open_root"]
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_READ_ONLY"):
        reader.put_verified(digest, len(body), body)
    assert not [event for event in api.events if event[0] in {"write", "link"}]
    reader.close()
    writer.close()
    assert api.live_handles == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_closes_every_child_after_close_failure(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    backend = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"close-all", _hash(b"close-all")
    target_parent = ("sha256", digest[7:9], digest[9:11])
    api.close_fail_paths.add(target_parent)
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.put_verified(digest, len(body), body)
    assert [handle.path for handle in api.live_handles] == [()]
    api.close_fail_paths.clear()
    backend.close()
    assert api.live_handles == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_api_uses_relative_nonreparse_nt_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_continuity import cas as cas_module

    native_type = getattr(cas_module, "_WindowsNativeApi", None)
    handle_type = getattr(cas_module, "_WindowsNativeHandle", None)
    assert native_type is not None and handle_type is not None
    api = object.__new__(native_type)
    api._root_volume = 7
    directories = {101}
    query_classes: list[int] = []
    create_calls: list[tuple[int, str, int, int, int, int, int]] = []
    link_calls: list[tuple[int, int, bool, str]] = []

    def raw_value(value: object) -> int:
        return int(ctypes.cast(value, ctypes.c_void_p).value or 0)

    def get_info(handle: object, info_class: int, buffer: object, _size: int) -> bool:
        query_classes.append(info_class)
        value = raw_value(handle)
        if info_class == cas_module._FILE_ID_INFO:
            info = ctypes.cast(buffer, ctypes.POINTER(cas_module._FILE_ID_INFO_VALUE)).contents
            info.VolumeSerialNumber = 7
            for index in range(16):
                info.FileId.Identifier[index] = (value + index + 1) % 255 or 1
        elif info_class == cas_module._FILE_ATTRIBUTE_TAG_INFO:
            info = ctypes.cast(buffer, ctypes.POINTER(cas_module._FILE_ATTRIBUTE_TAG_INFO_VALUE)).contents
            info.FileAttributes = cas_module._FILE_ATTRIBUTE_DIRECTORY if value in directories else 0
            info.ReparseTag = 0
        elif info_class == cas_module._FILE_STANDARD_INFO:
            info = ctypes.cast(buffer, ctypes.POINTER(cas_module._FILE_STANDARD_INFO_VALUE)).contents
            info.DeletePending = 0
        else:
            raise AssertionError(f"unexpected information class {info_class}")
        return True

    def get_volume_info(
        _handle: object,
        _name: object,
        _name_size: int,
        _serial: object,
        _maximum: object,
        _flags: object,
        filesystem: object,
        _filesystem_size: int,
    ) -> bool:
        ctypes.cast(_flags, ctypes.POINTER(cas_module.wintypes.DWORD)).contents.value = 0x00400000
        filesystem.value = "NTFS"
        return True

    def nt_create(
        out_handle: object,
        desired: int,
        object_attributes: object,
        _status: object,
        _allocation: object,
        _attributes: int,
        share: int,
        disposition: int,
        options: int,
        _ea: object,
        _ea_length: int,
    ) -> int:
        attributes = ctypes.cast(
            object_attributes, ctypes.POINTER(cas_module._OBJECT_ATTRIBUTES)
        ).contents
        name = ctypes.wstring_at(
            attributes.ObjectName.contents.Buffer, attributes.ObjectName.contents.Length // 2
        )
        create_calls.append(
            (
                raw_value(attributes.RootDirectory),
                name,
                attributes.Attributes,
                desired,
                share,
                disposition,
                options,
            )
        )
        ctypes.cast(out_handle, ctypes.POINTER(ctypes.c_void_p)).contents.value = 202
        return 0

    def nt_set_information(
        source: object,
        _status: object,
        information: object,
        length: int,
        information_class: int,
    ) -> int:
        assert information_class == cas_module._FILE_LINK_INFORMATION
        raw = ctypes.string_at(information, length)
        header = cas_module._FILE_LINK_INFORMATION_HEADER.from_buffer_copy(raw)
        name = raw[20 : 20 + header.FileNameLength].decode("utf-16-le")
        link_calls.append(
            (raw_value(source), raw_value(header.RootDirectory), bool(header.ReplaceIfExists), name)
        )
        return 0

    api._get_info_ex = get_info
    api._get_volume_info = get_volume_info
    api._nt_create = nt_create
    api._nt_set_information = nt_set_information
    api._close_handle = lambda _handle: True
    parent = handle_type(101, directory=True)
    api.revalidate(parent, directory=True)
    api._check_filesystem(parent)
    monkeypatch.setattr(cas_module.os, "link", lambda *_args, **_kwargs: pytest.fail("path link"))

    stage = api.open_child(
        parent,
        "stage",
        directory=False,
        create=True,
        exclusive=True,
        delete_on_close=True,
    )
    assert create_calls == [
        (
            101,
            "stage",
            cas_module._OBJ_CASE_INSENSITIVE | cas_module._OBJ_DONT_REPARSE,
            cas_module._FILE_READ_DATA
            | cas_module._FILE_READ_ATTRIBUTES
            | cas_module._SYNCHRONIZE
            | cas_module._FILE_WRITE_DATA
            | cas_module._FILE_WRITE_ATTRIBUTES
            | cas_module._DELETE,
            cas_module._FILE_SHARE_READ,
            cas_module._FILE_CREATE,
            cas_module._FILE_NON_DIRECTORY_FILE
            | cas_module._FILE_SYNCHRONOUS_IO_NONALERT
            | cas_module._FILE_OPEN_REPARSE_POINT
            | cas_module._FILE_DELETE_ON_CLOSE,
        )
    ]
    assert set(query_classes) >= {
        cas_module._FILE_ID_INFO,
        cas_module._FILE_ATTRIBUTE_TAG_INFO,
        cas_module._FILE_STANDARD_INFO,
    }
    api.link(stage, parent, "digest")
    assert link_calls == [(202, 101, False, "digest")]
    with pytest.raises(OSError):
        api.open_child(parent, "unsafe/name", directory=True, create=False)
    assert len(create_calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_api_rejects_volume_without_hardlink_capability() -> None:
    from devkit_continuity import cas as cas_module

    native_type = getattr(cas_module, "_WindowsNativeApi", None)
    handle_type = getattr(cas_module, "_WindowsNativeHandle", None)
    assert native_type is not None and handle_type is not None
    api = object.__new__(native_type)

    def get_volume_info(
        _handle: object,
        _name: object,
        _name_size: int,
        _serial: object,
        _maximum: object,
        _flags: object,
        filesystem: object,
        _filesystem_size: int,
    ) -> bool:
        filesystem.value = "NTFS"
        return True

    api._get_volume_info = get_volume_info
    with pytest.raises(OSError):
        api._check_filesystem(handle_type(101, directory=True))


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_native_cas_retains_verified_root_handle_until_close(tmp_path: Path) -> None:
    api = _MemoryNativeApi()
    backend = _windows_backend_for_test(api, tmp_path / "continuity-cas")
    body, digest = b"retained-root", _hash(b"retained-root")
    backend.verify_prepared()
    api.events.clear()

    assert backend.put_verified(digest, len(body), body) == digest
    assert backend.read_verified(digest, len(body)) == body
    assert not [event for event in api.events if event[0] == "open_root"]

    backend.close()
    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_UNAVAILABLE"):
        backend.read_verified(digest, len(body))


@pytest.mark.skipif(os.name != "nt", reason="Windows native CAS backend")
def test_windows_runtime_bootstrap_and_prepared_open_close_native_validation_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devkit_continuity import cas as cas_module

    closes: list[str] = []

    class Backend:
        def put_verified(self, content_hash: str, byte_length: int, body: bytes) -> str:
            return content_hash

        def read_verified(self, content_hash: str, byte_length: int) -> bytes:
            raise FileNotFoundError(content_hash)

        def close(self) -> None:
            closes.append("close")

    monkeypatch.setattr(cas_module, "_load_windows_native_api", lambda: object())
    monkeypatch.setattr(
        cas_module,
        "_open_native_backend",
        lambda *_args, **_kwargs: Backend(),
        raising=False,
    )
    config = _unprepared_config(tmp_path)
    RuntimeBootstrap.run(
        config,
        proof_registry_bootstrap=lambda database: sqlite3.connect(database).close(),
    )
    assert closes == ["close"]

    prepared = ContinuityStore.open_readonly(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    prepared.close()
    assert closes == ["close", "close"]
