"""Focused retention contracts for the local disposable project index."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from project_index.service import ProjectIndexService  # noqa: E402
from project_index.store import ProjectIndexStore  # noqa: E402


def _service(tmp_path: Path) -> ProjectIndexService:
    store = ProjectIndexStore.bootstrap(tmp_path / "project-index.sqlite3")
    return ProjectIndexService.from_prepared_store(store)


def _snapshots(service: ProjectIndexService, root: Path) -> tuple[str, ...]:
    workspace_id = service.project_index_register(root)
    snapshots: list[str] = []
    for value in ("one", "two", "three", "four"):
        (root / "module.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        snapshots.append(service.sync(workspace_id).snapshot_id)
    return tuple(snapshots)


def test_retention_preview_and_apply_keep_two_latest_and_query_receipts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    service = _service(tmp_path)
    workspace_id = service.project_index_register(root)

    (root / "module.py").write_text("VALUE = 'one'\n", encoding="utf-8")
    first = service.sync(workspace_id)
    service.query(workspace_id, first.snapshot_id, "VALUE", source_lines=0)
    snapshots = [first.snapshot_id]
    for value in ("two", "three", "four"):
        (root / "module.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        snapshots.append(service.sync(workspace_id).snapshot_id)

    preview = service.preview_retention()

    assert preview.blocked_reason is None
    assert tuple(candidate.snapshot_id for candidate in preview.candidates) == (
        snapshots[1],
    )
    assert preview.candidates[0].estimated_row_count > 0
    assert preview.candidates[0].estimated_reclaimable_bytes > 0
    assert service._store.get_snapshot(snapshots[1]) is not None
    blob_count_before = service._store.blob_count()

    applied = service.apply_retention(preview.preview_id)

    assert applied.blocked_reason is None
    assert applied.deleted_snapshot_ids == (snapshots[1],)
    assert service._store.blob_count() < blob_count_before
    assert (
        service._store._connection.execute(
            """
        SELECT COUNT(*) FROM project_index_parse_cache AS cache
        WHERE NOT EXISTS (
            SELECT 1 FROM project_index_snapshot_files AS file
            WHERE file.content_hash = cache.content_hash
        )
          AND NOT EXISTS (
            SELECT 1 FROM project_index_snapshot_packages AS package
            WHERE package.manifest_hash = cache.content_hash
        )
        """
        ).fetchone()[0]
        == 0
    )
    assert (
        service._store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    )
    assert service._store.get_snapshot(snapshots[0]) is not None
    assert service._store.get_snapshot(snapshots[1]) is None
    assert service._store.get_snapshot(snapshots[2]) is not None
    assert service._store.get_snapshot(snapshots[3]) is not None
    service.close()


def test_retention_fails_closed_when_query_receipt_reference_table_is_unreadable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    service = _service(tmp_path)
    _snapshots(service, root)

    service._store._connection.execute("DROP TABLE project_index_query_receipts")
    preview = service.preview_retention()

    assert preview.preview_id is None
    assert preview.candidates == ()
    assert preview.blocked_reason == "RETENTION_REFERENCE_SCHEMA_UNAVAILABLE"
    service.close()


def test_retention_apply_rejects_a_preview_changed_by_a_later_sync(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    service = _service(tmp_path)
    workspace_id = service.project_index_register(root)
    for value in ("one", "two", "three"):
        (root / "module.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        service.sync(workspace_id)

    preview = service.preview_retention()
    (root / "module.py").write_text("VALUE = 'four'\n", encoding="utf-8")
    service.sync(workspace_id)

    applied = service.apply_retention(preview.preview_id)

    assert applied.deleted_snapshot_ids == ()
    assert applied.deleted_row_count == 0
    assert applied.blocked_reason == "RETENTION_PREVIEW_STALE"
    service.close()


def test_retention_fails_closed_on_unknown_external_snapshot_reference_columns(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    service = _service(tmp_path)
    snapshots = _snapshots(service, root)
    service._store._connection.execute(
        "CREATE TABLE checkpoint_records (checkpoint_id TEXT, snapshot_id TEXT)"
    )
    service._store._connection.execute(
        "INSERT INTO checkpoint_records VALUES (?, ?)",
        ("checkpoint-1", snapshots[0]),
    )
    service._store._connection.commit()

    preview = service.preview_retention()

    assert preview.preview_id is None
    assert preview.candidates == ()
    assert preview.protected_snapshot_ids == ()
    assert preview.blocked_reason == "RETENTION_REFERENCE_SCHEMA_UNAVAILABLE"
    service.close()


def test_retention_uses_the_largest_oldest_prefix_within_hash_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    service = _service(tmp_path)
    workspace_id = service.project_index_register(root)
    snapshots: list[str] = []
    for value in ("one", "two", "three", "four", "five"):
        (root / "module.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        snapshots.append(service.sync(workspace_id).snapshot_id)
    service._store._RETENTION_MAX_HASHES = 1

    preview = service.preview_retention()

    assert preview.blocked_reason is None
    assert tuple(candidate.snapshot_id for candidate in preview.candidates) == (
        snapshots[0],
    )
    service.close()


def test_legacy_store_compaction_requires_explicit_space_checked_rewrite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project-index.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.execute("CREATE TABLE legacy_marker(value TEXT)")
    legacy.close()
    store = ProjectIndexStore.bootstrap(database)
    assert store._connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0

    blocked = store.compact_storage()
    compacted = store.compact_storage(allow_full_rewrite=True)

    assert blocked.blocked_reason == "RETENTION_FULL_REWRITE_REQUIRED"
    assert compacted.blocked_reason is None
    assert compacted.auto_vacuum_mode == "incremental"
    assert store._connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    store.close()
