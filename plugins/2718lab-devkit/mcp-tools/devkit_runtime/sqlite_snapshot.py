"""Verified WAL-aware read snapshots for invocation-scoped SQLite readers."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final, cast

_DATABASE_NAME = "snapshot.sqlite3"
_CONSTRUCTION_TOKEN: Final = object()


class SqliteSnapshotError(ValueError):
    """Bounded failure raised when a verified snapshot cannot be established."""

    code = "sqlite_snapshot_conflict"

    def __init__(self) -> None:
        super().__init__("sqlite snapshot conflict")


def _lexical_absolute(path: str | Path) -> Path:
    """Normalize ``.``/``..`` without resolving through a possible link."""

    return Path(os.path.abspath(os.fspath(path)))


def _configured_absolute(path: str | Path) -> Path:
    configured = Path(os.fspath(path))
    if not configured.is_absolute():
        raise SqliteSnapshotError()
    return _lexical_absolute(configured)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the attributes that identify a regular file across a read."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _unsafe_file_status(path: Path, value: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0) & 0x400
        or (callable(is_junction) and is_junction(path))
    )


def _capture_safe_path_chain(
    path: Path, *, require_regular: bool = False
) -> tuple[tuple[Path, tuple[int, int, int, int, int]], ...]:
    """Capture every safe component without resolving through a link."""

    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if not parts:
        raise SqliteSnapshotError()
    cursor = Path(parts[0])
    records: list[tuple[Path, tuple[int, int, int, int, int]]] = []
    try:
        for part in parts[1:]:
            cursor /= part
            item = cursor.lstat()
            if _unsafe_file_status(cursor, item):
                raise SqliteSnapshotError()
            records.append((cursor, _file_identity(item)))
        final = absolute.lstat()
    except SqliteSnapshotError:
        raise
    except OSError as exc:
        raise SqliteSnapshotError() from exc
    if require_regular and not stat.S_ISREG(final.st_mode):
        raise SqliteSnapshotError()
    if records and _file_identity(final) != records[-1][1]:
        raise SqliteSnapshotError()
    return tuple(records)


def _assert_safe_existing_path(path: Path, *, require_regular: bool = False) -> None:
    """Reject any linked/reparse component without resolving through it."""

    _capture_safe_path_chain(path, require_regular=require_regular)


def _assert_path_chain_unchanged(
    records: tuple[tuple[Path, tuple[int, int, int, int, int]], ...],
) -> None:
    """Fail closed if a checked component was replaced or modified."""

    for path, identity in records:
        try:
            value = path.lstat()
        except OSError as exc:
            raise SqliteSnapshotError() from exc
        if _unsafe_file_status(path, value) or _file_identity(value) != identity:
            raise SqliteSnapshotError()


def _object_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Return only the stable object identity of a file or directory."""

    return _file_identity(value)[:3]


def _safe_regular_identity(
    path: Path, *, optional: bool = False
) -> tuple[int, int, int, int, int] | None:
    """Read a file identity without resolving links or accepting a race."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise SqliteSnapshotError() from None
    except OSError as exc:
        raise SqliteSnapshotError() from exc
    if _unsafe_file_status(path, before) or not stat.S_ISREG(before.st_mode):
        raise SqliteSnapshotError()
    _assert_safe_existing_path(path, require_regular=True)
    try:
        after = path.lstat()
    except OSError as exc:
        raise SqliteSnapshotError() from exc
    if _file_identity(before) != _file_identity(after):
        raise SqliteSnapshotError()
    return _file_identity(after)


def _snapshot_source_state(
    database: Path,
) -> tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None]:
    """Capture the exact durable DB/WAL generation used by a snapshot."""

    database_identity = _safe_regular_identity(database)
    if database_identity is None:
        raise SqliteSnapshotError()
    return database_identity, _safe_regular_identity(
        Path(str(database) + "-wal"), optional=True
    )


_READONLY_ROOT_PREFIX = ".sqlite-snapshot-root-"
_READONLY_STAGE_PREFIX = ".sqlite-snapshot-"
_READONLY_QUARANTINE_PREFIX = ".sqlite-snapshot-quarantine-"
_SQLITE_SNAPSHOT_FILENAMES = (
    "snapshot.sqlite3",
    "snapshot.sqlite3-journal",
    "snapshot.sqlite3-shm",
    "snapshot.sqlite3-wal",
)


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _capture_safe_directory_chain(
    path: Path,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    """Capture every directory component needed to reach one scratch anchor."""

    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if not parts:
        raise SqliteSnapshotError()
    cursor = Path(parts[0])
    records: list[tuple[Path, tuple[int, int, int]]] = []
    try:
        for part in (None, *parts[1:]):
            if part is not None:
                cursor /= part
            value = cursor.lstat()
            if _unsafe_file_status(cursor, value) or not stat.S_ISDIR(value.st_mode):
                raise SqliteSnapshotError()
            records.append((cursor, _object_identity(value)))
    except SqliteSnapshotError:
        raise
    except OSError as exc:
        raise SqliteSnapshotError() from exc
    return tuple(records)


def _assert_safe_directory_chain(
    records: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> None:
    for path, identity in records:
        try:
            value = path.lstat()
        except OSError as exc:
            raise SqliteSnapshotError() from exc
        if (
            _unsafe_file_status(path, value)
            or not stat.S_ISDIR(value.st_mode)
            or _object_identity(value) != identity
        ):
            raise SqliteSnapshotError()


def _posix_directory_flags() -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise SqliteSnapshotError()
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0)


def _open_posix_directory(name: str | Path, *, directory_fd: int | None = None) -> int:
    try:
        if directory_fd is None:
            return os.open(name, _posix_directory_flags())
        return os.open(name, _posix_directory_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise SqliteSnapshotError() from exc


def _open_posix_directory_chain(
    records: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> list[int]:
    descriptors: list[int] = []
    try:
        for path, identity in records:
            descriptor = (
                _open_posix_directory(path)
                if not descriptors
                else _open_posix_directory(path.name, directory_fd=descriptors[-1])
            )
            value = os.fstat(descriptor)
            if not stat.S_ISDIR(value.st_mode) or _object_identity(value) != identity:
                _close_descriptor(descriptor)
                raise SqliteSnapshotError()
            descriptors.append(descriptor)
        return descriptors
    except Exception:
        while descriptors:
            _close_descriptor(descriptors.pop())
        raise


def _posix_rename_noreplace(
    old_directory_fd: int,
    old_name: str,
    new_directory_fd: int,
    new_name: str,
) -> None:
    """Atomically relocate one basename without replacing a destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            old_directory_fd,
            os.fsencode(old_name),
            new_directory_fd,
            os.fsencode(new_name),
            1,
        )
        == 0
    ):
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, "destination exists", new_name)
    raise OSError(error, "renameat2 failed", new_name)


def _require_posix_rename_noreplace() -> None:
    """Reject a POSIX scratch lifecycle before writes without atomic rename."""

    try:
        _posix_rename_noreplace(-1, "", -1, "")
    except OSError as exc:
        if exc.errno in {errno.EBADF, errno.ENOENT, errno.EINVAL}:
            return
    raise SqliteSnapshotError()


def _posix_private_directory(value: os.stat_result) -> bool:
    mode = stat.S_IMODE(value.st_mode)
    get_effective_user_id = getattr(os, "geteuid", None)
    if not callable(get_effective_user_id):
        return False
    owner_id = cast(Callable[[], int], get_effective_user_id)()
    return bool(
        stat.S_ISDIR(value.st_mode) and mode & 0o077 == 0 and value.st_uid == owner_id
    )


def _assert_posix_quarantine_parent(descriptor: int) -> None:
    """Require a parent that protects a lease-owned quarantine entry."""

    try:
        value = os.fstat(descriptor)
    except OSError as exc:
        raise SqliteSnapshotError() from exc
    if not stat.S_ISDIR(value.st_mode):
        raise SqliteSnapshotError()
    mode = stat.S_IMODE(value.st_mode)
    if mode & 0o022 and not value.st_mode & stat.S_ISVTX:
        raise SqliteSnapshotError()


_WIN_GENERIC_READ = 0x80000000
_WIN_DELETE = 0x00010000
_WIN_SHARE_READ = 0x00000001
_WIN_SHARE_WRITE = 0x00000002
_WIN_OPEN_EXISTING = 3
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_INVALID_HANDLE = ctypes.c_void_p(-1).value
_WIN_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_WIN_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_WIN_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_WIN_FILE_OPEN = 1
_WIN_FILE_CREATE = 2
_WIN_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WIN_FILE_DIRECTORY_FILE = 0x00000001
_WIN_FILE_NON_DIRECTORY_FILE = 0x00000040
_WIN_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WIN_FILE_OPEN_REPARSE_POINT = 0x00200000
_WIN_OBJ_CASE_INSENSITIVE = 0x00000040
_WIN_OBJ_DONT_REPARSE = 0x00001000
_WIN_DIRECTORY_ACCESS = 0x00120089
_WIN_FILE_GENERIC_READ = 0x00120089
_WIN_FILE_GENERIC_WRITE = 0x00120116


def _win_kernel32() -> Any:
    if os.name != "nt":
        raise SqliteSnapshotError()
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _win_close_handle(handle: int | None) -> None:
    if handle is None:
        return
    kernel32 = _win_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def _win_descriptor_handle(descriptor: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(descriptor))


def _win_open_verified_directory(path: Path, expected: tuple[int, int, int]) -> int:
    """Open a path component and pin it with no delete sharing."""

    kernel32 = _win_kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        _WIN_GENERIC_READ,
        _WIN_SHARE_READ | _WIN_SHARE_WRITE,
        None,
        _WIN_OPEN_EXISTING,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in {None, _WIN_INVALID_HANDLE}:
        raise SqliteSnapshotError()
    descriptor: int | None = None
    try:
        import msvcrt

        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
        handle = None
        opened = os.fstat(descriptor)
        status = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _object_identity(opened) != expected
            or _unsafe_file_status(path, status)
            or not stat.S_ISDIR(status.st_mode)
            or _object_identity(status) != expected
        ):
            raise SqliteSnapshotError()
        return descriptor
    except SqliteSnapshotError:
        _close_descriptor(descriptor)
        raise
    except OSError as exc:
        _close_descriptor(descriptor)
        raise SqliteSnapshotError() from exc
    finally:
        if handle is not None:
            _win_close_handle(int(handle))


def _open_windows_directory_chain(
    records: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> list[int]:
    descriptors: list[int] = []
    try:
        for path, identity in records:
            descriptors.append(_win_open_verified_directory(path, identity))
        return descriptors
    except Exception:
        while descriptors:
            _close_descriptor(descriptors.pop())
        raise


class _WinUnicodeString(ctypes.Structure):
    _fields_ = (
        ("length", ctypes.c_ushort),
        ("maximum_length", ctypes.c_ushort),
        ("buffer", ctypes.c_void_p),
    )


class _WinObjectAttributes(ctypes.Structure):
    _fields_ = (
        ("length", ctypes.c_ulong),
        ("root_directory", ctypes.c_void_p),
        ("object_name", ctypes.POINTER(_WinUnicodeString)),
        ("attributes", ctypes.c_ulong),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    )


class _WinIoStatusUnion(ctypes.Union):
    _fields_ = (("status", ctypes.c_long), ("pointer", ctypes.c_void_p))


class _WinIoStatusBlock(ctypes.Structure):
    _fields_ = (("status", _WinIoStatusUnion), ("information", ctypes.c_size_t))


class _WinDispositionInfo(ctypes.Structure):
    _fields_ = (("delete_file", ctypes.c_byte),)


def _win_open_relative(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    directory: bool,
    delete: bool = False,
    write: bool = False,
    optional: bool = False,
) -> int | None:
    """Open/create one basename below a retained Windows directory handle."""

    if not name or "\\" in name or "/" in name:
        raise SqliteSnapshotError()
    buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _WinUnicodeString(
        len(name) * ctypes.sizeof(ctypes.c_wchar),
        (len(name) + 1) * ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(buffer, ctypes.c_void_p),
    )
    attributes = _WinObjectAttributes(
        ctypes.sizeof(_WinObjectAttributes),
        ctypes.c_void_p(_win_descriptor_handle(parent_descriptor)),
        ctypes.pointer(unicode_name),
        _WIN_OBJ_CASE_INSENSITIVE | _WIN_OBJ_DONT_REPARSE,
        None,
        None,
    )
    status_block = _WinIoStatusBlock()
    output = ctypes.c_void_p()
    ntdll = ctypes.WinDLL("ntdll")
    create_file = ntdll.NtCreateFile
    create_file.argtypes = (
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_WinObjectAttributes),
        ctypes.POINTER(_WinIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    create_file.restype = ctypes.c_long
    access = (
        (_WIN_DIRECTORY_ACCESS if directory else _WIN_FILE_GENERIC_READ)
        | (_WIN_FILE_GENERIC_WRITE if write else 0)
        | (_WIN_DELETE if delete else 0)
    )
    status = create_file(
        ctypes.byref(output),
        access,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        _WIN_FILE_ATTRIBUTE_NORMAL,
        _WIN_SHARE_READ | _WIN_SHARE_WRITE,
        _WIN_FILE_CREATE if create else _WIN_FILE_OPEN,
        (_WIN_FILE_DIRECTORY_FILE if directory else _WIN_FILE_NON_DIRECTORY_FILE)
        | _WIN_FILE_SYNCHRONOUS_IO_NONALERT
        | _WIN_FILE_OPEN_REPARSE_POINT,
        None,
        0,
    )
    if status != 0:
        if create and (status & 0xFFFFFFFF) == _WIN_STATUS_OBJECT_NAME_COLLISION:
            return None
        if (
            not create
            and optional
            and (status & 0xFFFFFFFF)
            in {_WIN_STATUS_OBJECT_NAME_NOT_FOUND, _WIN_STATUS_OBJECT_PATH_NOT_FOUND}
        ):
            return None
        raise SqliteSnapshotError()
    descriptor: int | None = None
    try:
        import msvcrt

        output_value = output.value
        if output_value is None:
            raise SqliteSnapshotError()
        descriptor = msvcrt.open_osfhandle(
            int(output_value), os.O_WRONLY if write else os.O_RDONLY
        )
        output = ctypes.c_void_p()
        return descriptor
    except OSError as exc:
        _close_descriptor(descriptor)
        raise SqliteSnapshotError() from exc
    finally:
        if output.value is not None:
            _win_close_handle(int(output.value))


def _win_mark_descriptor_for_delete(descriptor: int) -> None:
    kernel32 = _win_kernel32()
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_information.restype = ctypes.c_int
    disposition = _WinDispositionInfo(1)
    if set_information(
        ctypes.c_void_p(_win_descriptor_handle(descriptor)),
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        return
    raise SqliteSnapshotError()


class _ScratchLease:
    """Capability lease for the complete private SQLite snapshot lifecycle."""

    def __init__(self, scratch_parent: Path, *, owns_scratch: bool) -> None:
        self.anchor = _lexical_absolute(scratch_parent)
        self._directory_chain = _capture_safe_directory_chain(self.anchor)
        self.anchor_identity = self._directory_chain[-1][1]
        self._chain_descriptors: list[int] = []
        self._anchor_descriptor: int | None = None
        self._scratch_descriptor: int | None = None
        self._stage_descriptor: int | None = None
        self._owns_scratch = owns_scratch
        self._scratch_is_anchor = False
        self._scratch_name: str | None = None
        self._stage_name: str | None = None
        self.scratch: Path | None = None
        self.scratch_identity: tuple[int, int, int] | None = None
        self.stage: Path | None = None
        self.stage_identity: tuple[int, int, int] | None = None
        self._files: dict[str, tuple[int, int, int]] = {}
        self._snapshot_file_descriptors: dict[str, int] = {}
        try:
            if os.name == "posix":
                self._chain_descriptors = _open_posix_directory_chain(
                    self._directory_chain
                )
            elif os.name == "nt":
                self._chain_descriptors = _open_windows_directory_chain(
                    self._directory_chain
                )
            else:
                raise SqliteSnapshotError()
            self._anchor_descriptor = self._chain_descriptors[-1]
            if os.name == "posix":
                _assert_posix_quarantine_parent(self._anchor_descriptor)
                _require_posix_rename_noreplace()
            if not owns_scratch:
                self.scratch = self.anchor
                self.scratch_identity = self.anchor_identity
                self._scratch_descriptor = self._anchor_descriptor
                self._scratch_is_anchor = True
        except Exception:
            self.close()
            raise

    @staticmethod
    def _new_name(prefix: str) -> str:
        return prefix + secrets.token_hex(16)

    @staticmethod
    def _descriptor_identity(descriptor: int | None) -> tuple[int, int, int]:
        if descriptor is None:
            raise SqliteSnapshotError()
        try:
            return _object_identity(os.fstat(descriptor))
        except OSError as exc:
            raise SqliteSnapshotError() from exc

    def _assert_anchor(self) -> None:
        if self._descriptor_identity(self._anchor_descriptor) != self.anchor_identity:
            raise SqliteSnapshotError()
        _assert_safe_directory_chain(self._directory_chain)

    def _assert_scratch(self) -> None:
        if self.scratch is None or self.scratch_identity is None:
            raise SqliteSnapshotError()
        self._assert_anchor()
        if self._descriptor_identity(self._scratch_descriptor) != self.scratch_identity:
            raise SqliteSnapshotError()
        try:
            status = self.scratch.lstat()
        except OSError as exc:
            raise SqliteSnapshotError() from exc
        if (
            _unsafe_file_status(self.scratch, status)
            or not stat.S_ISDIR(status.st_mode)
            or _object_identity(status) != self.scratch_identity
        ):
            raise SqliteSnapshotError()

    def _assert_stage(self) -> None:
        if (
            self.stage is None
            or self.stage_identity is None
            or self._stage_name is None
            or self.scratch is None
            or self.stage.parent != self.scratch
        ):
            raise SqliteSnapshotError()
        self._assert_scratch()
        if self._descriptor_identity(self._stage_descriptor) != self.stage_identity:
            raise SqliteSnapshotError()
        try:
            status = self.stage.lstat()
        except OSError as exc:
            raise SqliteSnapshotError() from exc
        if (
            _unsafe_file_status(self.stage, status)
            or not stat.S_ISDIR(status.st_mode)
            or _object_identity(status) != self.stage_identity
        ):
            raise SqliteSnapshotError()

    def create_directory(self, prefix: str, *, owned_scratch: bool) -> Path:
        if owned_scratch:
            if self.scratch is not None:
                raise SqliteSnapshotError()
            self._assert_anchor()
            parent = self.anchor
            parent_descriptor = self._anchor_descriptor
        else:
            if self.stage is not None:
                raise SqliteSnapshotError()
            self._assert_scratch()
            parent = self.scratch
            parent_descriptor = self._scratch_descriptor
        if parent is None or parent_descriptor is None:
            raise SqliteSnapshotError()
        for _attempt in range(64):
            name = self._new_name(prefix)
            descriptor: int | None = None
            try:
                if os.name == "posix":
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
                    except FileExistsError:
                        continue
                    descriptor = _open_posix_directory(
                        name, directory_fd=parent_descriptor
                    )
                else:
                    descriptor = _win_open_relative(
                        parent_descriptor,
                        name,
                        create=True,
                        directory=True,
                        delete=True,
                    )
                    if descriptor is None:
                        continue
                status = os.fstat(descriptor)
                if not stat.S_ISDIR(status.st_mode) or (
                    os.name == "posix" and not _posix_private_directory(status)
                ):
                    raise SqliteSnapshotError()
                identity = _object_identity(status)
                created = parent / name
                if owned_scratch:
                    self.scratch = created
                    self.scratch_identity = identity
                    self._scratch_descriptor = descriptor
                    self._scratch_name = name
                else:
                    self.stage = created
                    self.stage_identity = identity
                    self._stage_descriptor = descriptor
                    self._stage_name = name
                return created
            except SqliteSnapshotError:
                _close_descriptor(descriptor)
                raise
            except OSError as exc:
                _close_descriptor(descriptor)
                raise SqliteSnapshotError() from exc
        raise SqliteSnapshotError()

    def create_file(self, name: str) -> int:
        if not name or "/" in name or "\\" in name:
            raise SqliteSnapshotError()
        self._assert_stage()
        if self._stage_descriptor is None:
            raise SqliteSnapshotError()
        try:
            if os.name == "posix":
                return os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                    dir_fd=self._stage_descriptor,
                )
            descriptor = _win_open_relative(
                self._stage_descriptor,
                name,
                create=True,
                directory=False,
                write=True,
            )
            if descriptor is None:
                raise SqliteSnapshotError()
            return descriptor
        except SqliteSnapshotError:
            raise
        except OSError as exc:
            raise SqliteSnapshotError() from exc

    def record_file(self, name: str, identity: tuple[int, int, int]) -> None:
        self._assert_stage()
        if name in self._files:
            raise SqliteSnapshotError()
        self._files[name] = identity

    def retain_snapshot_file(self, name: str, identity: tuple[int, int, int]) -> None:
        """Keep the pathname SQLite will open from being replaced on Windows."""

        if name in self._snapshot_file_descriptors:
            raise SqliteSnapshotError()
        self._assert_stage()
        if self._stage_descriptor is None:
            raise SqliteSnapshotError()
        descriptor: int | None = None
        try:
            if os.name == "posix":
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
                    dir_fd=self._stage_descriptor,
                )
            else:
                descriptor = _win_open_relative(
                    self._stage_descriptor,
                    name,
                    create=False,
                    directory=False,
                )
                if descriptor is None:
                    raise SqliteSnapshotError()
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or _object_identity(status) != identity:
                raise SqliteSnapshotError()
            self._snapshot_file_descriptors[name] = descriptor
            descriptor = None
        except SqliteSnapshotError:
            raise
        except OSError as exc:
            raise SqliteSnapshotError() from exc
        finally:
            _close_descriptor(descriptor)

    def release_snapshot_file_locks(self) -> None:
        while self._snapshot_file_descriptors:
            _close_descriptor(self._snapshot_file_descriptors.popitem()[1])

    def _capture_regular_file(
        self, name: str, *, optional: bool
    ) -> tuple[int, int, int] | None:
        if not name or "/" in name or "\\" in name:
            raise SqliteSnapshotError()
        self._assert_stage()
        if self._stage_descriptor is None:
            raise SqliteSnapshotError()
        descriptor: int | None = None
        try:
            if os.name == "posix":
                try:
                    status = os.stat(
                        name,
                        dir_fd=self._stage_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if optional:
                        return None
                    raise SqliteSnapshotError() from None
                if not stat.S_ISREG(status.st_mode):
                    raise SqliteSnapshotError()
                identity = _object_identity(status)
            else:
                descriptor = _win_open_relative(
                    self._stage_descriptor,
                    name,
                    create=False,
                    directory=False,
                    optional=optional,
                )
                if descriptor is None:
                    return None
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise SqliteSnapshotError()
                identity = _object_identity(status)
            self._files[name] = identity
            return identity
        except SqliteSnapshotError:
            raise
        except OSError as exc:
            raise SqliteSnapshotError() from exc
        finally:
            _close_descriptor(descriptor)

    def capture_sqlite_snapshot_files(self) -> None:
        """Record only the fixed SQLite sidecars that this snapshot may create."""

        for name in _SQLITE_SNAPSHOT_FILENAMES:
            self._capture_regular_file(name, optional=True)

    def assert_ready_for_sqlite(self) -> None:
        """Reassert pathname and retained-capability identities before SQLite opens."""

        self._assert_stage()
        for name, descriptor in self._snapshot_file_descriptors.items():
            expected = self._files.get(name)
            if expected is None or self._descriptor_identity(descriptor) != expected:
                raise SqliteSnapshotError()

    def _create_posix_quarantine(
        self, parent_descriptor: int
    ) -> tuple[str, int] | None:
        """Create a private directory used only after an atomic move."""

        try:
            _assert_posix_quarantine_parent(parent_descriptor)
        except SqliteSnapshotError:
            return None
        for _attempt in range(64):
            name = _READONLY_QUARANTINE_PREFIX + secrets.token_hex(16)
            descriptor: int | None = None
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            except OSError:
                return None
            try:
                descriptor = _open_posix_directory(name, directory_fd=parent_descriptor)
                if not _posix_private_directory(os.fstat(descriptor)):
                    _close_descriptor(descriptor)
                    return None
                result = descriptor
                descriptor = None
                return name, result
            except (OSError, SqliteSnapshotError):
                _close_descriptor(descriptor)
                return None
        return None

    def _move_posix_directory_to_quarantine(
        self,
        parent_descriptor: int,
        source_name: str,
        source_descriptor: int,
        expected_identity: tuple[int, int, int],
    ) -> tuple[str, int, str, int] | None:
        """Move one named directory before authenticating it by retained fd."""

        quarantine = self._create_posix_quarantine(parent_descriptor)
        if quarantine is None:
            return None
        quarantine_name, quarantine_descriptor = quarantine
        for _attempt in range(64):
            moved_name = "entry-" + secrets.token_hex(16)
            moved_descriptor: int | None = None
            try:
                _posix_rename_noreplace(
                    parent_descriptor,
                    source_name,
                    quarantine_descriptor,
                    moved_name,
                )
                moved_descriptor = _open_posix_directory(
                    moved_name, directory_fd=quarantine_descriptor
                )
                if (
                    self._descriptor_identity(moved_descriptor) != expected_identity
                    or self._descriptor_identity(source_descriptor) != expected_identity
                ):
                    _close_descriptor(moved_descriptor)
                    _close_descriptor(quarantine_descriptor)
                    return None
                result = (
                    quarantine_name,
                    quarantine_descriptor,
                    moved_name,
                    moved_descriptor,
                )
                quarantine_descriptor = None
                moved_descriptor = None
                return result
            except FileExistsError:
                continue
            except (OSError, SqliteSnapshotError):
                _close_descriptor(moved_descriptor)
                _close_descriptor(quarantine_descriptor)
                return None
        _close_descriptor(quarantine_descriptor)
        return None

    @staticmethod
    def _open_posix_quarantined_regular(
        parent_descriptor: int,
        name: str,
        expected_identity: tuple[int, int, int],
    ) -> int | None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                dir_fd=parent_descriptor,
            )
            value = os.fstat(descriptor)
            if (
                not stat.S_ISREG(value.st_mode)
                or _object_identity(value) != expected_identity
            ):
                _close_descriptor(descriptor)
                return None
            result = descriptor
            descriptor = None
            return result
        except OSError:
            _close_descriptor(descriptor)
            return None

    def _cleanup_stage_posix_quarantined(self) -> bool:
        """Authenticate a moved stage before deleting only private entries."""

        if (
            self._stage_descriptor is None
            or self._scratch_descriptor is None
            or self._stage_name is None
            or self.stage_identity is None
        ):
            return False
        quarantine_descriptor: int | None = None
        moved_descriptor: int | None = None
        try:
            if self._descriptor_identity(self._stage_descriptor) != self.stage_identity:
                return False
            moved = self._move_posix_directory_to_quarantine(
                self._scratch_descriptor,
                self._stage_name,
                self._stage_descriptor,
                self.stage_identity,
            )
            if moved is None:
                return False
            (
                quarantine_name,
                quarantine_descriptor,
                moved_name,
                moved_descriptor,
            ) = moved
            for name, identity in self._files.items():
                file_descriptor = self._open_posix_quarantined_regular(
                    moved_descriptor, name, identity
                )
                if file_descriptor is None:
                    return False
                try:
                    # The source name no longer resolves in scratch.  This
                    # deletion is scoped below the 0700 quarantine boundary
                    # after descriptor identity authentication.
                    os.unlink(name, dir_fd=moved_descriptor)
                finally:
                    _close_descriptor(file_descriptor)
            os.rmdir(moved_name, dir_fd=quarantine_descriptor)
            _close_descriptor(moved_descriptor)
            moved_descriptor = None
            os.rmdir(quarantine_name, dir_fd=self._scratch_descriptor)
            _close_descriptor(quarantine_descriptor)
            quarantine_descriptor = None
        except (OSError, SqliteSnapshotError):
            return False
        finally:
            _close_descriptor(moved_descriptor)
            _close_descriptor(quarantine_descriptor)
        _close_descriptor(self._stage_descriptor)
        self._stage_descriptor = None
        self.stage = None
        self.stage_identity = None
        self._stage_name = None
        return True

    def _cleanup_stage_windows(self) -> bool:
        if self._stage_descriptor is None or self.stage_identity is None:
            return False
        try:
            if self._descriptor_identity(self._stage_descriptor) != self.stage_identity:
                return False
            for name, identity in self._files.items():
                descriptor = _win_open_relative(
                    self._stage_descriptor,
                    name,
                    create=False,
                    directory=False,
                    delete=True,
                )
                if descriptor is None:
                    return False
                try:
                    status = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(status.st_mode)
                        or _object_identity(status) != identity
                    ):
                        return False
                    _win_mark_descriptor_for_delete(descriptor)
                finally:
                    _close_descriptor(descriptor)
            _win_mark_descriptor_for_delete(self._stage_descriptor)
        except (OSError, SqliteSnapshotError):
            return False
        _close_descriptor(self._stage_descriptor)
        self._stage_descriptor = None
        self.stage = None
        self.stage_identity = None
        self._stage_name = None
        return True

    def _cleanup_owned_scratch_posix_quarantined(self) -> bool:
        """Remove an empty owned scratch root only after an atomic move."""

        if (
            self._scratch_descriptor is None
            or self._anchor_descriptor is None
            or self._scratch_name is None
            or self.scratch_identity is None
        ):
            return False
        quarantine_descriptor: int | None = None
        moved_descriptor: int | None = None
        try:
            if (
                self._descriptor_identity(self._scratch_descriptor)
                != self.scratch_identity
            ):
                return False
            moved = self._move_posix_directory_to_quarantine(
                self._anchor_descriptor,
                self._scratch_name,
                self._scratch_descriptor,
                self.scratch_identity,
            )
            if moved is None:
                return False
            (
                quarantine_name,
                quarantine_descriptor,
                moved_name,
                moved_descriptor,
            ) = moved
            os.rmdir(moved_name, dir_fd=quarantine_descriptor)
            _close_descriptor(moved_descriptor)
            moved_descriptor = None
            os.rmdir(quarantine_name, dir_fd=self._anchor_descriptor)
            _close_descriptor(quarantine_descriptor)
            quarantine_descriptor = None
        except (OSError, SqliteSnapshotError):
            return False
        finally:
            _close_descriptor(moved_descriptor)
            _close_descriptor(quarantine_descriptor)
        _close_descriptor(self._scratch_descriptor)
        self._scratch_descriptor = None
        self.scratch = None
        self.scratch_identity = None
        self._scratch_name = None
        return True

    def _cleanup_owned_scratch_windows(self) -> bool:
        if self._scratch_descriptor is None or self.scratch_identity is None:
            return False
        try:
            if (
                self._descriptor_identity(self._scratch_descriptor)
                != self.scratch_identity
            ):
                return False
            _win_mark_descriptor_for_delete(self._scratch_descriptor)
        except (OSError, SqliteSnapshotError):
            return False
        _close_descriptor(self._scratch_descriptor)
        self._scratch_descriptor = None
        self.scratch = None
        self.scratch_identity = None
        self._scratch_name = None
        return True

    def cleanup(self) -> None:
        """Clean normal owned objects through retained directory capabilities only."""

        if self._stage_descriptor is not None:
            if os.name == "posix":
                if not self._cleanup_stage_posix_quarantined():
                    return
            elif os.name == "nt":
                if not self._cleanup_stage_windows():
                    return
            else:
                return
        if self._owns_scratch and self._scratch_descriptor is not None:
            if os.name == "posix":
                self._cleanup_owned_scratch_posix_quarantined()
            elif os.name == "nt":
                self._cleanup_owned_scratch_windows()

    def close(self) -> None:
        self.release_snapshot_file_locks()
        _close_descriptor(self._stage_descriptor)
        self._stage_descriptor = None
        if self._owns_scratch and not self._scratch_is_anchor:
            _close_descriptor(self._scratch_descriptor)
        self._scratch_descriptor = None
        while self._chain_descriptors:
            _close_descriptor(self._chain_descriptors.pop())
        self._anchor_descriptor = None


def _create_readonly_directory(
    lease: _ScratchLease,
    prefix: str,
    *,
    owned_scratch: bool,
) -> Path:
    """Create one random private scratch directory beneath a retained lease."""

    return lease.create_directory(prefix, owned_scratch=owned_scratch)


class _SnapshotDestination:
    def __init__(self, lease: _ScratchLease, name: str) -> None:
        self.lease = lease
        self.name = name


def _copy_snapshot_file(
    source: Path, destination: _SnapshotDestination
) -> tuple[int, int, int, int, int]:
    """Copy a stable source into a capability-relative exclusive scratch file."""

    before = _safe_regular_identity(source)
    if before is None:
        raise SqliteSnapshotError()
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    destination_identity: tuple[int, int, int] | None = None
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != before:
            raise SqliteSnapshotError()
        destination_descriptor = destination.lease.create_file(destination.name)
        while True:
            chunk = os.read(source_descriptor, 65_536)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if written <= 0:
                    raise SqliteSnapshotError()
                offset += written
        if _file_identity(os.fstat(source_descriptor)) != before:
            raise SqliteSnapshotError()
        destination_status = os.fstat(destination_descriptor)
        if not stat.S_ISREG(destination_status.st_mode):
            raise SqliteSnapshotError()
        destination_identity = _object_identity(destination_status)
        os.fsync(destination_descriptor)
    except SqliteSnapshotError:
        raise
    except OSError as exc:
        raise SqliteSnapshotError() from exc
    finally:
        _close_descriptor(destination_descriptor)
        _close_descriptor(source_descriptor)
    if destination_identity is None:
        raise SqliteSnapshotError()
    destination.lease.record_file(destination.name, destination_identity)
    destination.lease.retain_snapshot_file(destination.name, destination_identity)
    if _safe_regular_identity(source) != before:
        raise SqliteSnapshotError()
    return before


def _paths_overlap(left: Path, right: Path) -> bool:
    """Check lexical containment without resolving through a possible link."""

    left = _lexical_absolute(left)
    right = _lexical_absolute(right)
    try:
        left_value = os.path.normcase(os.fspath(left))
        right_value = os.path.normcase(os.fspath(right))
        common = os.path.commonpath((left_value, right_value))
        return common in {left_value, right_value}
    except ValueError:
        return False


def _default_readonly_scratch_parent() -> Path:
    """Choose an already-configured scratch parent without probing it."""

    configured_task_temp = os.environ.get("CODEX_TASK_TEMP")
    if configured_task_temp:
        return _configured_absolute(configured_task_temp)
    for variable in ("TMPDIR", "TEMP", "TMP"):
        configured_temp = os.environ.get(variable)
        if configured_temp:
            return _configured_absolute(configured_temp)
    raise SqliteSnapshotError()


class _SnapshotConnection(sqlite3.Connection):
    _snapshot_owner: VerifiedSqliteSnapshot | None = None
    _snapshot_released = False

    def _attach(self, owner: VerifiedSqliteSnapshot) -> None:
        self._snapshot_owner = owner
        self._snapshot_released = False

    def close(self) -> None:
        if self._snapshot_released:
            return
        self._snapshot_released = True
        owner = self._snapshot_owner
        self._snapshot_owner = None
        try:
            super().close()
        finally:
            if owner is not None:
                owner._connection_closed(self)


class VerifiedSqliteSnapshot:
    """One identity-verified DB/WAL copy that vends query-only connections."""

    def __init__(
        self,
        token: object = None,
        *,
        lease: _ScratchLease | None = None,
    ) -> None:
        if token is not _CONSTRUCTION_TOKEN or lease is None:
            raise TypeError("VerifiedSqliteSnapshot is factory-constructed")
        if lease.stage is None:
            raise SqliteSnapshotError()
        self._lease = lease
        self._database_path = lease.stage / _DATABASE_NAME
        self._connections: set[_SnapshotConnection] = set()
        self._keeper: _SnapshotConnection | None = None
        self._closed = False

    @property
    def database_path(self) -> Path:
        """Return the private copied database path for internal store binding."""

        return self._database_path

    def _open_connection(self) -> _SnapshotConnection:
        if self._closed:
            raise SqliteSnapshotError()
        self._lease.assert_ready_for_sqlite()
        uri = self._database_path.as_uri() + "?mode=ro"
        connection: _SnapshotConnection | None = None
        try:
            raw = sqlite3.connect(
                uri,
                uri=True,
                timeout=30.0,
                factory=_SnapshotConnection,
            )
            connection = cast(_SnapshotConnection, raw)
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise SqliteSnapshotError()
            connection._attach(self)
            self._connections.add(connection)
            return connection
        except SqliteSnapshotError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise SqliteSnapshotError() from exc

    def _initialize(self) -> None:
        keeper = self._open_connection()
        try:
            if keeper.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
                raise SqliteSnapshotError()
        except Exception:
            keeper.close()
            raise
        self._keeper = keeper

    def connect(self) -> sqlite3.Connection:
        """Open an independently closeable query-only connection."""

        return self._open_connection()

    def _connection_closed(self, connection: _SnapshotConnection) -> None:
        self._connections.discard(connection)
        if self._keeper is connection:
            self._keeper = None

    def close(self) -> None:
        """Close all remaining readers, then clean only identity-owned scratch."""

        if self._closed:
            return
        self._closed = True
        for connection in tuple(self._connections):
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._connections.clear()
        self._keeper = None
        self._lease.release_snapshot_file_locks()
        try:
            self._lease.cleanup()
        finally:
            self._lease.close()

    def __enter__(self) -> VerifiedSqliteSnapshot:
        if self._closed:
            raise SqliteSnapshotError()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def open_verified_sqlite_snapshot(
    database_path: str | Path,
    *,
    scratch_root: str | Path | None = None,
    protected_roots: Iterable[str | Path] = (),
) -> VerifiedSqliteSnapshot:
    """Copy one stable DB+WAL generation into private non-durable scratch."""

    database = _lexical_absolute(database_path)
    snapshot: VerifiedSqliteSnapshot | None = None
    lease: _ScratchLease | None = None
    try:
        database_chain = _capture_safe_path_chain(database, require_regular=True)
        scratch_parent = (
            _default_readonly_scratch_parent()
            if scratch_root is None
            else _configured_absolute(scratch_root)
        )
        forbidden = (
            database.parent,
            *tuple(_lexical_absolute(item) for item in protected_roots),
        )
        for root in forbidden:
            if _paths_overlap(scratch_parent, root):
                raise SqliteSnapshotError()
            _assert_safe_existing_path(root)
        _assert_safe_existing_path(scratch_parent)
        if not stat.S_ISDIR(scratch_parent.lstat().st_mode):
            raise SqliteSnapshotError()
        _assert_path_chain_unchanged(database_chain)

        source_state = _snapshot_source_state(database)
        owns_scratch = scratch_root is None
        lease = _ScratchLease(scratch_parent, owns_scratch=owns_scratch)
        if owns_scratch:
            scratch = _create_readonly_directory(
                lease,
                _READONLY_ROOT_PREFIX,
                owned_scratch=True,
            )
        else:
            scratch = lease.scratch
        if scratch is None:
            raise SqliteSnapshotError()
        stage = _create_readonly_directory(
            lease,
            _READONLY_STAGE_PREFIX,
            owned_scratch=False,
        )
        _copy_snapshot_file(
            database,
            _SnapshotDestination(lease, _DATABASE_NAME),
        )
        if source_state[1] is not None:
            _copy_snapshot_file(
                Path(str(database) + "-wal"),
                _SnapshotDestination(lease, _DATABASE_NAME + "-wal"),
            )
        if _snapshot_source_state(database) != source_state:
            raise SqliteSnapshotError()
        _assert_path_chain_unchanged(database_chain)
        lease.assert_ready_for_sqlite()

        snapshot = VerifiedSqliteSnapshot(_CONSTRUCTION_TOKEN, lease=lease)
        snapshot._initialize()
        if _snapshot_source_state(database) != source_state:
            raise SqliteSnapshotError()
        _assert_path_chain_unchanged(database_chain)
        lease.capture_sqlite_snapshot_files()
        lease.assert_ready_for_sqlite()
        if snapshot.database_path != stage / _DATABASE_NAME:
            raise SqliteSnapshotError()
        return snapshot
    except Exception as exc:
        if snapshot is not None:
            try:
                snapshot.close()
            except Exception:
                pass
        elif lease is not None:
            lease.release_snapshot_file_locks()
            try:
                lease.cleanup()
            finally:
                lease.close()
        if isinstance(exc, SqliteSnapshotError):
            raise
        raise SqliteSnapshotError() from exc
