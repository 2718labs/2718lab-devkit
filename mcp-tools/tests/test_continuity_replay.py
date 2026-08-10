"""Offline Continuity replay contracts (CP-D2)."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import devkit_continuity.store as continuity_store_module  # noqa: E402
from devkit_atlas.extractors import BoundExecutionReceipt as AtlasReceipt  # noqa: E402
from devkit_atlas.extractors import ExtractionRequest
from devkit_atlas.service import (  # noqa: E402
    AcceptedAtlasProjectionEvidence,
    AcceptedAtlasProjectionRequest,
)
from devkit_continuity.canonical import canonical_hash  # noqa: E402
from devkit_continuity.cas import ContinuityCas, ContinuityCasError  # noqa: E402
from devkit_continuity.models import (  # noqa: E402
    ContinuityKey,
    ContinuityReceipt,
    FrozenEntry,
    FrozenView,
)
from devkit_continuity.service import ContinuityService  # noqa: E402
from devkit_continuity.store import ContinuityStore, ContinuityStoreError  # noqa: E402
from devkit_runtime.atlas_acceptance import (
    ProductionAcceptanceEvidenceReader,  # noqa: E402
)
from devkit_runtime.bootstrap import RuntimeBootstrap  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from project_index.checkpoints import CheckpointFile  # noqa: E402
from project_index.models import CoverageGap, IndexNode, SnapshotFile  # noqa: E402
from project_index.service import ProjectIndexService  # noqa: E402


def _hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _config(tmp_path: Path) -> RuntimeConfig:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(tmp_path / "data"), "CODEX_TASK_TEMP": str(scratch)}
    )
    RuntimeBootstrap.run(
        config,
        proof_registry_bootstrap=lambda database: sqlite3.connect(database).close(),
    )
    return config


def _typed_inputs() -> tuple[
    ContinuityKey,
    AcceptedAtlasProjectionRequest,
    AcceptedAtlasProjectionEvidence,
]:
    request = AcceptedAtlasProjectionRequest.create(
        workflow_id="workflow",
        code_task_id="task",
        code_task_version=1,
        input_snapshot_id="input",
        output_snapshot_id="output",
        indexed_diff_hash=_hash(b"diff"),
        intent_id="intent",
        language="python",
        framework="pytest",
        checkpoint_id="checkpoint",
        checkpoint_hash=_hash(b"checkpoint"),
        output_query_trace_id="query",
        verification_artifact_hashes=(_hash(b"artifact"),),
        execution_receipt_ids=(_hash(b"receipt"),),
    )
    before = b"before"
    after = b"after"
    extraction = ExtractionRequest(
        "workflow",
        "task",
        request.acceptance_id,
        "code",
        "intent",
        _hash(b"workspace"),
        "checkpoint",
        "input",
        "output",
        ("src",),
        (CheckpointFile("src/a.py", _hash(before), before),),
        (SnapshotFile("src/a.py", _hash(after), after),),
        (
            IndexNode(
                "node",
                "function",
                "src/a.py",
                "run",
                "pkg.run",
                1,
                1,
                _hash(after),
            ),
        ),
        (CoverageGap("src/a.py", "PARSER_GAP", "gap"),),
        (
            AtlasReceipt(
                _hash(b"receipt"),
                "command",
                "workflow",
                "task",
                request.acceptance_id,
                _hash(b"workspace"),
                "output",
                ("python",),
                canonical_hash(("python",)),
                _hash(b"input"),
                _hash(b"output"),
                0,
                True,
            ),
        ),
    )
    evidence = AcceptedAtlasProjectionEvidence(
        1,
        "python",
        "pytest",
        request.checkpoint_hash,
        request.indexed_diff_hash,
        "query",
        request.verification_artifact_hashes,
        extraction,
    )
    return (
        ContinuityKey(
            request.workflow_id,
            request.code_task_id,
            request.code_task_version,
            request.acceptance_id,
            request.ingestion_key,
            request.payload_hash,
            request.evidence_binding_hash,
        ),
        request,
        evidence,
    )


def _frozen_service(
    tmp_path: Path, *, publish: bool
) -> tuple[RuntimeConfig, ContinuityService, ContinuityKey, FrozenView]:
    config = _config(tmp_path)
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    service = ContinuityService(store, cas)
    key, request, evidence = _typed_inputs()
    attempt = service.claim_or_reuse(key)
    view = service.freeze(attempt, request, evidence)
    if publish:
        service.publish(attempt, view)
    return config, service, key, view


def _deny(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("offline replay must not consult live evidence")


def _data_root_snapshot(data_root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(data_root).as_posix()
            for path in data_root.rglob("*")
            if path.is_file()
        )
    )


def _data_root_hashes(data_root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(data_root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in data_root.rglob("*")
            if path.is_file()
        )
    )


def _sqlite_journal_header(database: Path) -> tuple[int, int]:
    with database.open("rb") as source:
        header = source.read(20)
    assert header[:16] == b"SQLite format 3\x00"
    return header[18], header[19]


def test_verify_and_materialize_published_v2_are_reader_free_and_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, service, key, view = _frozen_service(tmp_path, publish=True)
    calls: list[tuple[str, int]] = []
    original = service.cas.read_verified

    def read_verified(content_hash: str, byte_length: int) -> bytes:
        calls.append((content_hash, byte_length))
        return original(content_hash, byte_length)

    monkeypatch.setattr(service.cas, "read_verified", read_verified)
    monkeypatch.setattr(ProductionAcceptanceEvidenceReader, "rebuild", _deny)
    monkeypatch.setattr(ProductionAcceptanceEvidenceReader, "read", _deny)
    monkeypatch.setattr(ProjectIndexService, "read_snapshot_files", _deny)

    assert service.verify_replay(key) == view
    replay = service.materialize_replay(key)

    assert replay.attempt.state == "published"
    assert replay.view == view
    assert type(replay.request) is AcceptedAtlasProjectionRequest
    assert type(replay.evidence) is AcceptedAtlasProjectionEvidence
    assert type(replay.extraction) is ExtractionRequest
    assert replay.request.ingestion_key == key.ingestion_key
    assert replay.evidence.extraction_request == replay.extraction
    assert replay.extraction.before_files[0].body == b"before"
    assert replay.extraction.after_files[0].body == b"after"
    assert calls == [
        (entry.content_hash, entry.byte_length)
        for _ in range(2)
        for entry in view.entries
    ]


def test_verify_replay_fails_closed_when_a_cas_body_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, service, key, _view = _frozen_service(tmp_path, publish=True)

    def mismatched(_content_hash: str, _byte_length: int) -> bytes:
        raise ContinuityCasError("CONTINUITY_CAS_CONFLICT")

    monkeypatch.setattr(service.cas, "read_verified", mismatched)

    with pytest.raises(ContinuityCasError, match="CONTINUITY_CAS_CONFLICT"):
        service.verify_replay(key)


def test_frozen_v2_candidate_replays_without_a_pointer(tmp_path: Path) -> None:
    _config, service, key, view = _frozen_service(tmp_path, publish=False)

    assert service.find_replay_candidate(
        key.workflow_id, key.code_task_id, key.acceptance_id, key.ingestion_key
    ) == key
    replay = service.materialize_replay(key)

    assert replay.attempt.state == "frozen"
    assert replay.view == view
    assert service.store.pointer_for(key) is None


def test_delete_journal_readonly_replay_adds_no_data_root_sidecars_or_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, writer, key, view = _frozen_service(tmp_path, publish=True)
    writer.store.close()
    writer.cas.close()
    before = _data_root_snapshot(config.data_root)
    hashes_before = _data_root_hashes(config.data_root)
    assert _sqlite_journal_header(config.continuity_database) == (1, 1)
    assert "continuity.sqlite3-shm" not in before
    assert "continuity.sqlite3-wal" not in before
    uri_calls: list[str] = []
    original_connect = continuity_store_module.sqlite3.connect

    def track_readonly_uri(*args: object, **kwargs: object) -> sqlite3.Connection:
        uri_calls.append(str(args[0]))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(
        continuity_store_module.sqlite3,
        "connect",
        track_readonly_uri,
    )

    reader_store = ContinuityStore.open_readonly(
        config.continuity_database,
        config.continuity_cas_root,
        config.scratch_root,
    )
    reader_cas = ContinuityCas.open_prepared(
        config.continuity_cas_root,
        config.scratch_root,
        read_only=True,
    )
    try:
        assert uri_calls == [
            f"file:{config.continuity_database.as_posix()}?mode=ro&cache=private"
        ]
        assert _data_root_snapshot(config.data_root) == before
        assert _data_root_hashes(config.data_root) == hashes_before
        replay = ContinuityService(reader_store, reader_cas).materialize_replay(key)
        assert replay.view == view
        assert _data_root_snapshot(config.data_root) == before
        assert _data_root_hashes(config.data_root) == hashes_before
    finally:
        reader_store.close()
        reader_cas.close()


def test_readonly_replay_fails_closed_when_uncheckpointed_wal_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    writer = ContinuityService(store, cas)
    assert store._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"  # noqa: SLF001
    store._connection.execute("PRAGMA wal_autocheckpoint=0")  # noqa: SLF001
    key, request, evidence = _typed_inputs()
    attempt = writer.claim_or_reuse(key)
    writer.publish(attempt, writer.freeze(attempt, request, evidence))
    wal = config.continuity_database.with_name(
        f"{config.continuity_database.name}-wal"
    )
    shm = config.continuity_database.with_name(
        f"{config.continuity_database.name}-shm"
    )
    assert wal.is_file() and wal.stat().st_size > 0
    assert shm.is_file() and shm.stat().st_size > 0
    assert _sqlite_journal_header(config.continuity_database) == (2, 2)
    before = _data_root_snapshot(config.data_root)
    connect_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def unexpected_sqlite_connect(
        *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        connect_calls.append((args, kwargs))
        raise AssertionError("visible WAL must fail before opening SQLite")

    monkeypatch.setattr(
        continuity_store_module.sqlite3,
        "connect",
        unexpected_sqlite_connect,
    )

    try:
        with pytest.raises(ContinuityStoreError, match="CONTINUITY_STORE_UNPREPARED"):
            ContinuityStore.open_readonly(
                config.continuity_database,
                config.continuity_cas_root,
                config.scratch_root,
            )
        assert connect_calls == []
        assert _data_root_snapshot(config.data_root) == before
    finally:
        store.close()
        cas.close()


def test_readonly_replay_rechecks_for_wal_created_during_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, writer, _key, _view = _frozen_service(tmp_path, publish=True)
    writer.store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # noqa: SLF001
    writer.store.close()
    writer.cas.close()
    wal = config.continuity_database.with_name(
        f"{config.continuity_database.name}-wal"
    )
    before = _data_root_snapshot(config.data_root)
    assert "continuity.sqlite3-wal" not in before
    original_connect = continuity_store_module.sqlite3.connect

    def create_wal_during_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        wal.write_bytes(b"")
        return connection

    monkeypatch.setattr(
        continuity_store_module.sqlite3,
        "connect",
        create_wal_during_connection,
    )

    with pytest.raises(ContinuityStoreError, match="CONTINUITY_STORE_UNPREPARED"):
        ContinuityStore.open_readonly(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )

    assert set(_data_root_snapshot(config.data_root)) == set(before) | {
        "continuity.sqlite3-wal"
    }


def test_legacy_candidate_is_not_allowed_to_fall_back_to_live_evidence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    service = ContinuityService(store, cas)
    key = ContinuityKey(
        "workflow", "legacy", 1, "acceptance", "ingestion", _hash(b"payload"), _hash(b"binding")
    )
    body = b"legacy"
    view = FrozenView.create(
        key=key,
        entries=(FrozenEntry("after_file", "src/a.py", _hash(body), len(body)),),
    )
    attempt = service.claim_or_reuse(key)
    store.freeze_attempt_atomic(
        attempt,
        view,
        ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen"),
    )

    with pytest.raises(ContinuityStoreError, match="CONTINUITY_REPLAY_CONFLICT"):
        service.find_replay_candidate(
            key.workflow_id, key.code_task_id, key.acceptance_id, key.ingestion_key
        )


def test_ambiguous_four_identifier_candidate_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = ContinuityStore.open_readwrite(
        config.continuity_database, config.continuity_cas_root, config.scratch_root
    )
    cas = ContinuityCas.open_prepared(
        config.continuity_cas_root, config.scratch_root, read_only=False
    )
    service = ContinuityService(store, cas)
    for suffix in (b"one", b"two"):
        key = ContinuityKey(
            "workflow",
            "task",
            1,
            "acceptance",
            "ingestion",
            _hash(b"payload-" + suffix),
            _hash(b"binding-" + suffix),
        )
        body = b"body-" + suffix
        view = FrozenView.create(
            key=key,
            entries=(FrozenEntry("after_file", "src/a.py", _hash(body), len(body)),),
        )
        attempt = service.claim_or_reuse(key)
        store.freeze_attempt_atomic(
            attempt,
            view,
            ContinuityReceipt.create(key=key, view_id=view.view_id, kind="frozen"),
        )

    with pytest.raises(ContinuityStoreError, match="CONTINUITY_REPLAY_AMBIGUOUS"):
        service.find_replay_candidate("workflow", "task", "acceptance", "ingestion")
