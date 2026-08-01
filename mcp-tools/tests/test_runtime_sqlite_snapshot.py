from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_runtime import sqlite_snapshot as snapshot_module
from devkit_runtime.sqlite_snapshot import (
    SqliteSnapshotError,
    VerifiedSqliteSnapshot,
    open_verified_sqlite_snapshot,
)


def _durable_file_state(
    root: Path,
) -> dict[str, tuple[bytes, tuple[int, int, int, int, int]]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            (
                path.lstat().st_dev,
                path.lstat().st_ino,
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            ),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_one_wal_snapshot_vends_independent_query_only_connections(
    tmp_path: Path,
) -> None:
    durable = tmp_path / "durable"
    scratch = tmp_path / "scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "shared.sqlite3"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE records(value TEXT NOT NULL)")
        writer.execute("INSERT INTO records(value) VALUES ('from-wal')")
        writer.commit()
        assert Path(str(database) + "-wal").is_file()
        before = _durable_file_state(durable)

        snapshot = open_verified_sqlite_snapshot(database, scratch_root=scratch)
        first = snapshot.connect()
        second = snapshot.connect()
        try:
            assert first.execute("PRAGMA query_only").fetchone() == (1,)
            assert second.execute("PRAGMA query_only").fetchone() == (1,)
            first_database_row = first.execute("PRAGMA database_list").fetchone()
            second_database_row = second.execute("PRAGMA database_list").fetchone()
            assert first_database_row is not None
            assert second_database_row is not None
            first_database = Path(str(first_database_row[2]))
            second_database = Path(str(second_database_row[2]))
            assert first_database == second_database == snapshot.database_path
            assert first.execute("SELECT value FROM records").fetchall() == [
                ("from-wal",)
            ]
            with pytest.raises(sqlite3.OperationalError):
                first.execute("INSERT INTO records(value) VALUES ('forbidden')")

            first.close()
            assert second.execute("SELECT value FROM records").fetchall() == [
                ("from-wal",)
            ]
            assert tuple(scratch.iterdir())
            assert _durable_file_state(durable) == before
        finally:
            first.close()
            second.close()
            snapshot.close()

        assert not tuple(scratch.iterdir())
        assert _durable_file_state(durable) == before
    finally:
        writer.close()


def test_snapshot_is_factory_constructed_only() -> None:
    with pytest.raises(TypeError, match="factory-constructed"):
        VerifiedSqliteSnapshot()


@pytest.mark.parametrize("contents", [None, b"not a sqlite database"])
def test_snapshot_rejects_missing_or_corrupt_database(
    tmp_path: Path,
    contents: bytes | None,
) -> None:
    database = tmp_path / "durable" / "broken.sqlite3"
    scratch = tmp_path / "scratch"
    database.parent.mkdir()
    scratch.mkdir()
    if contents is not None:
        database.write_bytes(contents)

    with pytest.raises(SqliteSnapshotError):
        open_verified_sqlite_snapshot(database, scratch_root=scratch)

    assert not tuple(scratch.iterdir())


def test_default_task_temp_snapshot_removes_only_its_owned_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    task_temp = tmp_path / "task-temp"
    durable.mkdir()
    task_temp.mkdir()
    database = durable / "source.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("CREATE TABLE values_table(value INTEGER NOT NULL)")
        writer.execute("INSERT INTO values_table(value) VALUES (7)")
        writer.commit()
    finally:
        writer.close()
    existing = task_temp / "caller-owned.txt"
    existing.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("CODEX_TASK_TEMP", str(task_temp))

    snapshot = open_verified_sqlite_snapshot(database)
    reader = snapshot.connect()
    try:
        assert reader.execute("SELECT value FROM values_table").fetchone() == (7,)
        assert len(tuple(task_temp.glob(".sqlite-snapshot-root-*"))) == 1
    finally:
        reader.close()
        snapshot.close()

    assert existing.read_text(encoding="utf-8") == "keep"
    assert tuple(task_temp.iterdir()) == (existing,)


@pytest.mark.parametrize("explicit_scratch", [True, False])
def test_snapshot_rejects_relative_scratch_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_scratch: bool,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "source.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("CREATE TABLE records(value INTEGER NOT NULL)")
        writer.commit()
    finally:
        writer.close()
    relative_scratch = tmp_path / "relative-scratch"
    relative_scratch.mkdir()
    monkeypatch.chdir(tmp_path)
    if not explicit_scratch:
        monkeypatch.setenv("CODEX_TASK_TEMP", "relative-scratch")

    with pytest.raises(SqliteSnapshotError):
        if explicit_scratch:
            open_verified_sqlite_snapshot(
                database,
                scratch_root=Path("relative-scratch"),
            )
        else:
            open_verified_sqlite_snapshot(database)

    assert not tuple(relative_scratch.iterdir())


def test_snapshot_fails_closed_when_source_database_identity_changes_after_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    scratch = tmp_path / "scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "source.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("CREATE TABLE records(value INTEGER NOT NULL)")
        writer.commit()
    finally:
        writer.close()
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    original_copy = snapshot_module._copy_snapshot_file
    swapped = False

    def replace_after_copy(
        source: Path, destination: snapshot_module._SnapshotDestination
    ) -> tuple[int, int, int, int, int]:
        nonlocal swapped
        copied = original_copy(source, destination)
        if source == database:
            swapped = True
            os.replace(replacement, database)
        return copied

    monkeypatch.setattr(snapshot_module, "_copy_snapshot_file", replace_after_copy)

    with pytest.raises(SqliteSnapshotError):
        open_verified_sqlite_snapshot(database, scratch_root=scratch)

    assert swapped is True
    assert not tuple(scratch.iterdir())


def test_snapshot_fails_closed_when_live_wal_identity_changes_after_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    scratch = tmp_path / "scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "source.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE records(value INTEGER NOT NULL)")
        writer.execute("INSERT INTO records(value) VALUES (1)")
        writer.commit()
        wal = Path(str(database) + "-wal")
        original_copy = snapshot_module._copy_snapshot_file
        mutated = False

        def replace_after_copy(
            source: Path, destination: snapshot_module._SnapshotDestination
        ) -> tuple[int, int, int, int, int]:
            nonlocal mutated
            copied = original_copy(source, destination)
            if source == wal:
                mutated = True
                with wal.open("ab") as stream:
                    stream.write(b"\x00")
            return copied

        monkeypatch.setattr(snapshot_module, "_copy_snapshot_file", replace_after_copy)

        with pytest.raises(SqliteSnapshotError):
            open_verified_sqlite_snapshot(database, scratch_root=scratch)

        assert mutated is True
        assert not tuple(scratch.iterdir())
    finally:
        writer.close()


def test_cleanup_never_deletes_a_snapshot_file_replaced_before_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    scratch = tmp_path / "scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "source.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("CREATE TABLE records(value INTEGER NOT NULL)")
        writer.commit()
    finally:
        writer.close()

    snapshot = open_verified_sqlite_snapshot(database, scratch_root=scratch)
    stage = next(scratch.glob(".sqlite-snapshot-*"))
    copied_database = stage / "snapshot.sqlite3"
    expected_snapshot = copied_database.read_bytes()
    parked = tmp_path / "parked-snapshot.sqlite3"
    attacker = b"caller-owned replacement\n"
    original_cleanup = snapshot_module._ScratchLease.cleanup
    raced = False

    def replace_before_cleanup(lease: snapshot_module._ScratchLease) -> None:
        nonlocal raced
        if not raced:
            raced = True
            os.replace(copied_database, parked)
            copied_database.write_bytes(attacker)
        original_cleanup(lease)

    monkeypatch.setattr(
        snapshot_module._ScratchLease,
        "cleanup",
        replace_before_cleanup,
    )

    snapshot.close()

    assert raced is True
    assert parked.read_bytes() == expected_snapshot
    quarantined = tuple(scratch.rglob("snapshot.sqlite3"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == attacker
