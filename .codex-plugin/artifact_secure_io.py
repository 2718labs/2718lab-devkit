"""Race-resistant source freezing and atomic artifact publication.

The public builder passes paths only at this module's outer edge. Selected source
bytes are authorized and consumed through held operating-system handles, copied
to a private spool, and never reopened by pathname. Unsupported kernel or
filesystem primitives fail closed; there is intentionally no compatibility
fallback.
"""

# ctypes requires mutable ``_fields_`` class attributes by API contract.
# ruff: noqa: RUF012

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, Self, runtime_checkable

IGNORED_DIRECTORIES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".pyright",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "venv",
    }
)
IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
COPY_CHUNK_SIZE = 1024 * 1024
MAX_CONTROL_BYTES = 1024 * 1024

ARTIFACT_SOURCE_UNSAFE = "ARTIFACT_SOURCE_UNSAFE"
ARTIFACT_SOURCE_HARDLINK = "ARTIFACT_SOURCE_HARDLINK"
ARTIFACT_SOURCE_RACE = "ARTIFACT_SOURCE_RACE"
ARTIFACT_SOURCE_CHANGED = "ARTIFACT_SOURCE_CHANGED"
ARTIFACT_OUTPUT_UNSAFE = "ARTIFACT_OUTPUT_UNSAFE"
ARTIFACT_FS_UNSUPPORTED = "ARTIFACT_FS_UNSUPPORTED"
ARTIFACT_ATOMIC_PUBLISH_FAILED = "ARTIFACT_ATOMIC_PUBLISH_FAILED"
ARTIFACT_IO_FAILED = "ARTIFACT_IO_FAILED"


class ArtifactSecureIOError(OSError):
    """One stable, fail-closed secure artifact I/O error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FrozenMember:
    """A source member frozen into an exact private-spool byte range."""

    archive_name: str
    spool_offset: int
    size: int
    sha256: str


@runtime_checkable
class TrustedRootHandle(Protocol):
    """Handle-authorized source-root interface consumed by the builder."""

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def read_control(self, relative_parts: Sequence[str]) -> bytes: ...

    def freeze_file(
        self,
        relative_parts: Sequence[str],
        archive_name: str,
        spool: BinaryIO,
    ) -> FrozenMember: ...

    def freeze_tree(
        self,
        relative_parts: Sequence[str],
        selected_names: set[str],
        spool: BinaryIO,
    ) -> list[FrozenMember]: ...


@runtime_checkable
class AtomicOutputPublisher(Protocol):
    """Private staging and same-directory name-replacement interface."""

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def create_private_spool(self) -> BinaryIO: ...

    def create_zip_temp(self) -> BinaryIO: ...

    def publish(self, destination_name: str) -> None: ...


@runtime_checkable
class SecureArtifactBackend(Protocol):
    """Platform boundary for secure source and output handles."""

    def open_root(self, path: Path) -> TrustedRootHandle: ...

    def open_output_parent(
        self,
        path: Path,
        *,
        source_root: TrustedRootHandle,
    ) -> AtomicOutputPublisher: ...


def _validate_parts(parts: Sequence[str]) -> tuple[str, ...]:
    validated = tuple(parts)
    if not validated:
        raise ArtifactSecureIOError(
            ARTIFACT_SOURCE_UNSAFE,
            "empty source-relative path",
        )
    for part in validated:
        if (
            not part
            or part in {".", ".."}
            or "/" in part
            or "\\" in part
            or ":" in part
            or "\0" in part
        ):
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_UNSAFE,
                "unsafe source path component",
            )
    return validated


def _validate_destination_name(name: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\0" in name
    ):
        raise ArtifactSecureIOError(
            ARTIFACT_OUTPUT_UNSAFE,
            "destination must be one safe filename component",
        )
    return name


def _same_or_descendant(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath((candidate, root)) == root
    except ValueError:
        return False


def _copy_source_to_spool(
    source: BinaryIO,
    spool: BinaryIO,
    archive_name: str,
) -> FrozenMember:
    spool.seek(0, os.SEEK_END)
    offset = spool.tell()
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(COPY_CHUNK_SIZE)
        if not chunk:
            break
        written = spool.write(chunk)
        if written != len(chunk):
            raise ArtifactSecureIOError(
                ARTIFACT_IO_FAILED,
                "private spool write was incomplete",
            )
        digest.update(chunk)
        size += len(chunk)
    spool.flush()
    return FrozenMember(archive_name, offset, size, digest.hexdigest())


def copy_frozen_member(
    spool: BinaryIO,
    member: FrozenMember,
    destination: BinaryIO,
) -> None:
    """Copy and re-hash one exact frozen range into an open ZIP member."""

    spool.seek(member.spool_offset)
    remaining = member.size
    digest = hashlib.sha256()
    while remaining:
        chunk = spool.read(min(COPY_CHUNK_SIZE, remaining))
        if not chunk:
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_CHANGED,
                "private spool ended before the recorded member range",
            )
        destination.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    if digest.hexdigest() != member.sha256:
        raise ArtifactSecureIOError(
            ARTIFACT_SOURCE_CHANGED,
            "private spool member hash changed",
        )


# ------------------------------ Linux openat2 ------------------------------


if sys.platform == "linux":

    class _OpenHow(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint64),
            ("mode", ctypes.c_uint64),
            ("resolve", ctypes.c_uint64),
        ]


_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_OPENAT2_RESOLVE = (
    _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_XDEV
)
_OPENAT2_SYSCALL = 437


@dataclass(frozen=True, slots=True)
class _PosixIdentity:
    device: int
    inode: int
    mode_type: int
    links: int
    size: int
    change_ns: int
    write_ns: int


def _posix_identity(metadata: os.stat_result) -> _PosixIdentity:
    if metadata.st_ino <= 0:
        raise ArtifactSecureIOError(
            ARTIFACT_FS_UNSUPPORTED,
            "filesystem returned an unstable zero inode",
        )
    return _PosixIdentity(
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
    )


def _same_posix_object(left: _PosixIdentity, right: _PosixIdentity) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and left.mode_type == right.mode_type
    )


class _LinuxRoot:
    def __init__(self, path: Path) -> None:
        if sys.platform != "linux":
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "Linux openat2 backend requested on another platform",
            )
        machine = os.uname().machine.casefold()
        if machine not in {"x86_64", "amd64", "aarch64", "arm64", "riscv64"}:
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                f"openat2 syscall number is not locked for {machine}",
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            self._fd = os.open(path, flags)
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_UNSAFE,
                "cannot open the plugin root without following links",
            ) from error
        metadata = os.fstat(self._fd)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(self._fd)
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_UNSAFE,
                "plugin root is not a directory",
            )
        _posix_identity(metadata)
        self.final_path = os.path.realpath(path)
        self._closed = False
        self._probe_openat2()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    def _probe_openat2(self) -> None:
        probe = self._openat2(self._fd, ".", directory=True, probe=True)
        os.close(probe)

    @staticmethod
    def _openat2(
        parent_fd: int,
        name: str,
        *,
        directory: bool,
        probe: bool = False,
    ) -> int:
        if sys.platform != "linux":
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "openat2 unavailable",
            )
        _validate_parts((name,)) if not probe else None
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        flags |= os.O_DIRECTORY if directory else os.O_NONBLOCK
        how = _OpenHow(flags=flags, mode=0, resolve=_OPENAT2_RESOLVE)
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.syscall(
            _OPENAT2_SYSCALL,
            parent_fd,
            os.fsencode(name),
            ctypes.byref(how),
            ctypes.sizeof(how),
        )
        if result < 0:
            error_number = ctypes.get_errno()
            if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    "kernel cannot enforce required openat2 resolve flags",
                )
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_UNSAFE,
                f"secure relative open failed with errno {error_number}",
            )
        return int(result)

    @staticmethod
    def _entry_stat(parent_fd: int, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_RACE,
                "source directory entry disappeared during authorization",
            ) from error

    def _open_parent(self, parts: Sequence[str]) -> tuple[list[int], int, str]:
        validated = _validate_parts(parts)
        held: list[int] = []
        parent = self._fd
        try:
            for component in validated[:-1]:
                expected = _posix_identity(self._entry_stat(parent, component))
                child = self._openat2(parent, component, directory=True)
                actual = _posix_identity(os.fstat(child))
                if expected != actual or actual.mode_type != stat.S_IFDIR:
                    os.close(child)
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_RACE,
                        "source directory identity changed during traversal",
                    )
                held.append(child)
                parent = child
            return held, parent, validated[-1]
        except BaseException:
            for descriptor in reversed(held):
                os.close(descriptor)
            raise

    def _open_regular(
        self,
        parent_fd: int,
        name: str,
    ) -> tuple[BinaryIO, _PosixIdentity]:
        expected = _posix_identity(self._entry_stat(parent_fd, name))
        descriptor = self._openat2(parent_fd, name, directory=False)
        try:
            actual = _posix_identity(os.fstat(descriptor))
            if actual.mode_type != stat.S_IFREG:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_UNSAFE,
                    "selected source is not a regular file",
                )
            if actual.links != 1:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_HARDLINK,
                    "selected source link count is not exactly one",
                )
            if expected != actual:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_RACE,
                    "opened source differs from its enumerated entry",
                )
            return os.fdopen(descriptor, "rb", closefd=True), actual
        except BaseException:
            os.close(descriptor)
            raise

    def _verify_regular(
        self,
        source: BinaryIO,
        before: _PosixIdentity,
        parent_fd: int,
        name: str,
    ) -> None:
        after = _posix_identity(os.fstat(source.fileno()))
        entry = _posix_identity(self._entry_stat(parent_fd, name))
        if after != before:
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_CHANGED,
                "source metadata changed while bytes were frozen",
            )
        if entry != before:
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_RACE,
                "source directory entry changed while bytes were frozen",
            )

    def read_control(self, relative_parts: Sequence[str]) -> bytes:
        held, parent, name = self._open_parent(relative_parts)
        try:
            source, before = self._open_regular(parent, name)
            with source:
                payload = source.read(MAX_CONTROL_BYTES + 1)
                if len(payload) > MAX_CONTROL_BYTES:
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_UNSAFE,
                        "artifact allowlist exceeds the control-file limit",
                    )
                self._verify_regular(source, before, parent, name)
                return payload
        finally:
            for descriptor in reversed(held):
                os.close(descriptor)

    def freeze_file(
        self,
        relative_parts: Sequence[str],
        archive_name: str,
        spool: BinaryIO,
    ) -> FrozenMember:
        held, parent, name = self._open_parent(relative_parts)
        try:
            source, before = self._open_regular(parent, name)
            with source:
                member = _copy_source_to_spool(source, spool, archive_name)
                self._verify_regular(source, before, parent, name)
                return member
        finally:
            for descriptor in reversed(held):
                os.close(descriptor)

    def _freeze_directory(
        self,
        directory_fd: int,
        prefix: tuple[str, ...],
        selected_names: set[str],
        spool: BinaryIO,
    ) -> list[FrozenMember]:
        members: list[FrozenMember] = []
        try:
            with os.scandir(directory_fd) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_SOURCE_UNSAFE,
                "cannot enumerate selected source tree by handle",
            ) from error
        for name in names:
            _validate_parts((name,))
            entry_metadata = self._entry_stat(directory_fd, name)
            entry_type = stat.S_IFMT(entry_metadata.st_mode)
            archive_parts = (*prefix, name)
            archive_name = "/".join(archive_parts)
            if entry_type == stat.S_IFDIR:
                if name in IGNORED_DIRECTORIES:
                    continue
                child = self._openat2(directory_fd, name, directory=True)
                try:
                    expected = _posix_identity(entry_metadata)
                    actual = _posix_identity(os.fstat(child))
                    if expected != actual:
                        raise ArtifactSecureIOError(
                            ARTIFACT_SOURCE_RACE,
                            "selected directory changed during enumeration",
                        )
                    members.extend(
                        self._freeze_directory(
                            child,
                            archive_parts,
                            selected_names,
                            spool,
                        )
                    )
                    if _posix_identity(os.fstat(child)) != actual:
                        raise ArtifactSecureIOError(
                            ARTIFACT_SOURCE_RACE,
                            "selected directory changed during traversal",
                        )
                finally:
                    os.close(child)
            elif entry_type == stat.S_IFREG:
                if Path(name).suffix.casefold() in IGNORED_SUFFIXES:
                    continue
                if archive_name in selected_names:
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_UNSAFE,
                        "duplicate archive name",
                    )
                selected_names.add(archive_name)
                source, before = self._open_regular(directory_fd, name)
                with source:
                    member = _copy_source_to_spool(source, spool, archive_name)
                    self._verify_regular(source, before, directory_fd, name)
                    members.append(member)
            else:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_UNSAFE,
                    "selected tree contains a non-regular entry",
                )
        return members

    def freeze_tree(
        self,
        relative_parts: Sequence[str],
        selected_names: set[str],
        spool: BinaryIO,
    ) -> list[FrozenMember]:
        held, parent, name = self._open_parent(relative_parts)
        directory: int | None = None
        try:
            expected = _posix_identity(self._entry_stat(parent, name))
            directory = self._openat2(parent, name, directory=True)
            actual = _posix_identity(os.fstat(directory))
            if expected != actual or actual.mode_type != stat.S_IFDIR:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_RACE,
                    "allowlisted tree changed during secure open",
                )
            return self._freeze_directory(
                directory,
                tuple(_validate_parts(relative_parts)),
                selected_names,
                spool,
            )
        finally:
            if directory is not None:
                os.close(directory)
            for descriptor in reversed(held):
                os.close(descriptor)


class _PosixPublisher:
    def __init__(self, path: Path, source_root: _LinuxRoot) -> None:
        if sys.platform != "linux":
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "POSIX publisher requires Linux",
            )
        parent_path = os.path.abspath(path)
        if not os.path.isdir(parent_path):
            raise ArtifactSecureIOError(
                ARTIFACT_OUTPUT_UNSAFE,
                "output parent must already exist",
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            self._parent_fd = os.open(parent_path, flags)
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_OUTPUT_UNSAFE,
                "cannot pin output parent without following links",
            ) from error
        parent_identity = _posix_identity(os.fstat(self._parent_fd))
        if parent_identity.mode_type != stat.S_IFDIR:
            os.close(self._parent_fd)
            raise ArtifactSecureIOError(
                ARTIFACT_OUTPUT_UNSAFE,
                "output parent is not a directory",
            )
        final_parent = os.path.realpath(parent_path)
        if _same_or_descendant(final_parent, source_root.final_path):
            os.close(self._parent_fd)
            raise ArtifactSecureIOError(
                ARTIFACT_OUTPUT_UNSAFE,
                "output parent must be outside the plugin root",
            )
        self._stage_name = f".artifact-stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(self._stage_name, 0o700, dir_fd=self._parent_fd)
            self._stage_fd = os.open(self._stage_name, flags, dir_fd=self._parent_fd)
        except OSError as error:
            os.close(self._parent_fd)
            raise ArtifactSecureIOError(
                ARTIFACT_OUTPUT_UNSAFE,
                "cannot create and pin private staging directory",
            ) from error
        stage_metadata = os.fstat(self._stage_fd)
        if stat.S_IMODE(stage_metadata.st_mode) & 0o077:
            self._close_descriptors()
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "private staging permissions are not exclusive",
            )
        self._stage_identity = _posix_identity(stage_metadata)
        self._spool_name: str | None = None
        self._zip_name: str | None = None
        self._spool_file: BinaryIO | None = None
        self._spool_identity: _PosixIdentity | None = None
        self._zip_file: BinaryIO | None = None
        self._zip_identity: _PosixIdentity | None = None
        self._published = False
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self._cleanup()
        except ArtifactSecureIOError:
            if exc is None:
                raise

    def _create_private_file(self, prefix: str) -> tuple[str, BinaryIO]:
        name = f"{prefix}-{secrets.token_hex(16)}"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._stage_fd)
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_OUTPUT_UNSAFE,
                "cannot exclusively create private staging file",
            ) from error
        return name, os.fdopen(descriptor, "w+b", closefd=True)

    def create_private_spool(self) -> BinaryIO:
        if self._spool_file is not None:
            raise ArtifactSecureIOError(
                ARTIFACT_IO_FAILED,
                "private spool already exists",
            )
        self._spool_name, self._spool_file = self._create_private_file("spool")
        self._spool_identity = _posix_identity(os.fstat(self._spool_file.fileno()))
        return self._spool_file

    def create_zip_temp(self) -> BinaryIO:
        if self._zip_file is not None:
            raise ArtifactSecureIOError(
                ARTIFACT_IO_FAILED,
                "private ZIP temp already exists",
            )
        self._zip_name, self._zip_file = self._create_private_file("artifact")
        self._zip_identity = _posix_identity(os.fstat(self._zip_file.fileno()))
        return self._zip_file

    def publish(self, destination_name: str) -> None:
        destination_name = _validate_destination_name(destination_name)
        if (
            self._zip_file is None
            or self._zip_name is None
            or self._zip_identity is None
        ):
            raise ArtifactSecureIOError(
                ARTIFACT_ATOMIC_PUBLISH_FAILED,
                "ZIP temp was not created",
            )
        try:
            self._zip_file.flush()
            os.fsync(self._zip_file.fileno())
            handle_identity = _posix_identity(os.fstat(self._zip_file.fileno()))
            entry_identity = _posix_identity(
                os.stat(
                    self._zip_name,
                    dir_fd=self._stage_fd,
                    follow_symlinks=False,
                )
            )
            # Size and timestamps are expected to change while the private ZIP
            # is being written.  The pinned object identity must remain stable;
            # comparing the full metadata snapshot here would reject every
            # non-empty artifact on Linux after its first write.
            if (
                not _same_posix_object(handle_identity, self._zip_identity)
                or not _same_posix_object(entry_identity, handle_identity)
            ):
                raise ArtifactSecureIOError(
                    ARTIFACT_ATOMIC_PUBLISH_FAILED,
                    "private ZIP temp identity changed before publication",
                )
            os.replace(
                self._zip_name,
                destination_name,
                src_dir_fd=self._stage_fd,
                dst_dir_fd=self._parent_fd,
            )
            self._published = True
            os.fsync(self._parent_fd)
        except ArtifactSecureIOError:
            raise
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_ATOMIC_PUBLISH_FAILED,
                "same-directory atomic publication failed",
            ) from error

    def _unlink_owned(self, name: str | None, expected: _PosixIdentity | None) -> None:
        if name is None:
            return
        try:
            metadata = os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if expected is not None and not _same_posix_object(
            _posix_identity(metadata), expected
        ):
            raise ArtifactSecureIOError(
                ARTIFACT_IO_FAILED,
                "refusing to clean up a staging entry with changed identity",
            )
        os.unlink(name, dir_fd=self._stage_fd)

    def _cleanup(self) -> None:
        if self._closed:
            return
        if self._zip_file is not None:
            self._zip_file.close()
        if self._spool_file is not None:
            self._spool_file.close()
        try:
            if not self._published:
                self._unlink_owned(self._zip_name, self._zip_identity)
            self._unlink_owned(self._spool_name, self._spool_identity)
            stage_now = _posix_identity(os.fstat(self._stage_fd))
            if not _same_posix_object(stage_now, self._stage_identity):
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    "refusing to clean up a changed staging directory",
                )
            os.close(self._stage_fd)
            os.rmdir(self._stage_name, dir_fd=self._parent_fd)
            os.close(self._parent_fd)
            self._closed = True
        except OSError as error:
            raise ArtifactSecureIOError(
                ARTIFACT_IO_FAILED,
                "private staging cleanup failed",
            ) from error

    def _close_descriptors(self) -> None:
        os.close(self._stage_fd)
        os.close(self._parent_fd)


class LinuxOpenAt2Backend:
    """Linux backend with kernel-enforced beneath/no-link/no-cross-mount opens."""

    def open_root(self, path: Path) -> _LinuxRoot:
        return _LinuxRoot(path)

    def open_output_parent(
        self,
        path: Path,
        *,
        source_root: TrustedRootHandle,
    ) -> _PosixPublisher:
        if not isinstance(source_root, _LinuxRoot):
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "Linux output publisher requires a Linux source root",
            )
        return _PosixPublisher(path, source_root)


# --------------------------- Windows NT handles ----------------------------


if os.name == "nt":
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IOStatusBlockUnion(ctypes.Union):
        _fields_ = [("Status", ctypes.c_long), ("Pointer", wintypes.LPVOID)]

    class _IOStatusBlock(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("value", _IOStatusBlockUnion), ("Information", ctypes.c_size_t)]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_uint64),
            ("FileId", _FileId128),
        ]

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_int64),
            ("EndOfFile", ctypes.c_int64),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        ]

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", wintypes.DWORD),
        ]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]


@dataclass(frozen=True, slots=True)
class _WindowsIdentity:
    volume: int
    file_id: bytes
    links: int
    size: int
    directory: bool
    delete_pending: bool
    created: int
    written: int
    changed: int
    attributes: int
    reparse_tag: int


@dataclass(frozen=True, slots=True)
class _WindowsDirEntry:
    name: str
    file_id: bytes
    attributes: int
    reparse_tag: int


if os.name == "nt":

    class _WinAPI:
        FILE_READ_DATA = 0x0001
        FILE_LIST_DIRECTORY = 0x0001
        FILE_ADD_FILE = 0x0002
        FILE_TRAVERSE = 0x0020
        FILE_DELETE_CHILD = 0x0040
        FILE_READ_ATTRIBUTES = 0x0080
        SYNCHRONIZE = 0x00100000
        DELETE = 0x00010000
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        FILE_OPEN = 0x00000001
        FILE_NON_DIRECTORY_FILE = 0x00000040
        FILE_DIRECTORY_FILE = 0x00000001
        FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
        FILE_OPEN_REPARSE_POINT = 0x00200000
        OBJ_CASE_INSENSITIVE = 0x00000040
        OPEN_EXISTING = 3
        CREATE_NEW = 1
        FILE_ATTRIBUTE_NORMAL = 0x00000080
        FILE_ATTRIBUTE_TEMPORARY = 0x00000100
        FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
        FILE_FLAG_WRITE_THROUGH = 0x80000000
        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_BASIC_INFO = 0
        FILE_STANDARD_INFO = 1
        FILE_ATTRIBUTE_TAG_INFO = 9
        FILE_ID_INFO = 18
        FILE_ID_EXTD_DIRECTORY_INFO = 19
        FILE_ID_EXTD_DIRECTORY_RESTART_INFO = 20
        FILE_RENAME_INFORMATION_EX = 65
        FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
        FILE_RENAME_POSIX_SEMANTICS = 0x00000002
        ERROR_NO_MORE_FILES = 18
        ERROR_INVALID_PARAMETER = 87
        ERROR_INVALID_FUNCTION = 1
        ERROR_NOT_SUPPORTED = 50
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        def __init__(self) -> None:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

            self.kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(_SecurityAttributes),
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            self.kernel32.CreateFileW.restype = wintypes.HANDLE
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel32.CloseHandle.restype = wintypes.BOOL
            self.kernel32.GetFileInformationByHandleEx.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
            self.kernel32.GetFinalPathNameByHandleW.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            self.kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
            self.kernel32.GetVolumeInformationByHandleW.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPWSTR,
                wintypes.DWORD,
            ]
            self.kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
            self.kernel32.CreateDirectoryW.argtypes = [
                wintypes.LPCWSTR,
                ctypes.POINTER(_SecurityAttributes),
            ]
            self.kernel32.CreateDirectoryW.restype = wintypes.BOOL
            self.ntdll.NtCreateFile.argtypes = [
                ctypes.POINTER(wintypes.HANDLE),
                wintypes.DWORD,
                ctypes.POINTER(_ObjectAttributes),
                ctypes.POINTER(_IOStatusBlock),
                ctypes.POINTER(ctypes.c_int64),
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.LPVOID,
                wintypes.ULONG,
            ]
            self.ntdll.NtCreateFile.restype = ctypes.c_long
            self.ntdll.NtSetInformationFile.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_IOStatusBlock),
                wintypes.LPVOID,
                wintypes.ULONG,
                ctypes.c_int,
            ]
            self.ntdll.NtSetInformationFile.restype = ctypes.c_long
            self.ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
            self.ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
            self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.ULONG),
            ]
            self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
            self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            self.kernel32.LocalFree.restype = wintypes.HLOCAL

        def close(self, handle: int) -> None:
            if handle and handle != self.INVALID_HANDLE_VALUE:
                self.kernel32.CloseHandle(handle)

        def open_path_directory(
            self,
            path: Path,
            *,
            code: str,
            writable: bool = False,
            enumerate_access: bool = True,
            share_writes: bool = False,
            share_deletes: bool = False,
        ) -> int:
            access = self.FILE_TRAVERSE | self.FILE_READ_ATTRIBUTES
            if enumerate_access:
                access |= self.FILE_LIST_DIRECTORY
            if writable:
                access |= self.FILE_ADD_FILE | self.FILE_DELETE_CHILD
            share = self.FILE_SHARE_READ
            if share_writes:
                share |= self.FILE_SHARE_WRITE
            if share_deletes:
                share |= self.FILE_SHARE_DELETE
            handle = self.kernel32.CreateFileW(
                str(path),
                access,
                share,
                None,
                self.OPEN_EXISTING,
                self.FILE_FLAG_BACKUP_SEMANTICS | self.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if handle == self.INVALID_HANDLE_VALUE:
                error = ctypes.get_last_error()
                raise ArtifactSecureIOError(
                    code, f"directory open failed with winerror {error}"
                )
            return int(handle)

        def open_relative(
            self,
            parent_handle: int,
            name: str,
            *,
            directory: bool,
            code: str = ARTIFACT_SOURCE_UNSAFE,
        ) -> int:
            _validate_parts((name,))
            name_buffer = ctypes.create_unicode_buffer(name)
            byte_length = len(name.encode("utf-16-le"))
            unicode_name = _UnicodeString(
                byte_length,
                byte_length,
                ctypes.cast(name_buffer, wintypes.LPWSTR),
            )
            attributes = _ObjectAttributes(
                ctypes.sizeof(_ObjectAttributes),
                parent_handle,
                ctypes.pointer(unicode_name),
                self.OBJ_CASE_INSENSITIVE,
                None,
                None,
            )
            status_block = _IOStatusBlock()
            result_handle = wintypes.HANDLE()
            access = self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
            access |= self.FILE_LIST_DIRECTORY if directory else self.FILE_READ_DATA
            options = self.FILE_OPEN_REPARSE_POINT | self.FILE_SYNCHRONOUS_IO_NONALERT
            options |= (
                self.FILE_DIRECTORY_FILE if directory else self.FILE_NON_DIRECTORY_FILE
            )
            status = self.ntdll.NtCreateFile(
                ctypes.byref(result_handle),
                access,
                ctypes.byref(attributes),
                ctypes.byref(status_block),
                None,
                self.FILE_ATTRIBUTE_NORMAL,
                self.FILE_SHARE_READ,
                self.FILE_OPEN,
                options,
                None,
                0,
            )
            if status < 0:
                winerror = self.ntdll.RtlNtStatusToDosError(status)
                raise ArtifactSecureIOError(
                    code,
                    f"NtCreateFile relative open failed with winerror {winerror}",
                )
            return int(result_handle.value)

        def open_relative_metadata(self, parent_handle: int, name: str) -> int:
            _validate_parts((name,))
            name_buffer = ctypes.create_unicode_buffer(name)
            byte_length = len(name.encode("utf-16-le"))
            unicode_name = _UnicodeString(
                byte_length,
                byte_length,
                ctypes.cast(name_buffer, wintypes.LPWSTR),
            )
            attributes = _ObjectAttributes(
                ctypes.sizeof(_ObjectAttributes),
                parent_handle,
                ctypes.pointer(unicode_name),
                self.OBJ_CASE_INSENSITIVE,
                None,
                None,
            )
            status_block = _IOStatusBlock()
            result_handle = wintypes.HANDLE()
            status = self.ntdll.NtCreateFile(
                ctypes.byref(result_handle),
                self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE,
                ctypes.byref(attributes),
                ctypes.byref(status_block),
                None,
                self.FILE_ATTRIBUTE_NORMAL,
                self.FILE_SHARE_READ | self.FILE_SHARE_WRITE | self.FILE_SHARE_DELETE,
                self.FILE_OPEN,
                self.FILE_OPEN_REPARSE_POINT
                | self.FILE_SYNCHRONOUS_IO_NONALERT
                | self.FILE_NON_DIRECTORY_FILE,
                None,
                0,
            )
            if status < 0:
                winerror = self.ntdll.RtlNtStatusToDosError(status)
                raise ArtifactSecureIOError(
                    ARTIFACT_ATOMIC_PUBLISH_FAILED,
                    f"published entry metadata open failed with winerror {winerror}",
                )
            return int(result_handle.value)

        def query(
            self, handle: int, info_class: int, structure: ctypes.Structure
        ) -> None:
            if not self.kernel32.GetFileInformationByHandleEx(
                handle,
                info_class,
                ctypes.byref(structure),
                ctypes.sizeof(structure),
            ):
                error = ctypes.get_last_error()
                if error in {
                    self.ERROR_INVALID_PARAMETER,
                    self.ERROR_INVALID_FUNCTION,
                    self.ERROR_NOT_SUPPORTED,
                }:
                    raise ArtifactSecureIOError(
                        ARTIFACT_FS_UNSUPPORTED,
                        f"required file information class is unsupported ({error})",
                    )
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    f"handle metadata query failed with winerror {error}",
                )

        def identity(self, handle: int) -> _WindowsIdentity:
            file_id = _FileIdInfo()
            standard = _FileStandardInfo()
            basic = _FileBasicInfo()
            tag = _FileAttributeTagInfo()
            self.query(handle, self.FILE_ID_INFO, file_id)
            self.query(handle, self.FILE_STANDARD_INFO, standard)
            self.query(handle, self.FILE_BASIC_INFO, basic)
            self.query(handle, self.FILE_ATTRIBUTE_TAG_INFO, tag)
            identifier = bytes(file_id.FileId.Identifier)
            if not any(identifier):
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    "filesystem returned a zero file ID",
                )
            return _WindowsIdentity(
                int(file_id.VolumeSerialNumber),
                identifier,
                int(standard.NumberOfLinks),
                int(standard.EndOfFile),
                bool(standard.Directory),
                bool(standard.DeletePending),
                int(basic.CreationTime),
                int(basic.LastWriteTime),
                int(basic.ChangeTime),
                int(tag.FileAttributes),
                int(tag.ReparseTag),
            )

        def validate_identity(
            self,
            identity: _WindowsIdentity,
            *,
            directory: bool,
        ) -> None:
            if identity.directory != directory or identity.delete_pending:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_UNSAFE,
                    "selected handle type or delete-pending state is unsafe",
                )
            if (
                identity.attributes & self.FILE_ATTRIBUTE_REPARSE_POINT
                or identity.reparse_tag
            ):
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_UNSAFE,
                    "selected handle is a reparse point",
                )

        def final_path(self, handle: int) -> str:
            size = self.kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
            if not size:
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    "cannot resolve held root/output handle for ancestry comparison",
                )
            buffer = ctypes.create_unicode_buffer(size + 1)
            written = self.kernel32.GetFinalPathNameByHandleW(
                handle, buffer, len(buffer), 0
            )
            if not written or written >= len(buffer):
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    "held handle final path query was unstable",
                )
            value = buffer.value
            value = value.removeprefix("\\\\?\\")
            return os.path.normcase(os.path.abspath(value))

        def filesystem(self, handle: int) -> str:
            filesystem = ctypes.create_unicode_buffer(64)
            if not self.kernel32.GetVolumeInformationByHandleW(
                handle,
                None,
                0,
                None,
                None,
                None,
                filesystem,
                len(filesystem),
            ):
                error = ctypes.get_last_error()
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    f"cannot identify filesystem with winerror {error}",
                )
            value = filesystem.value.casefold()
            if value not in {"ntfs", "refs"}:
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    f"unsupported Windows filesystem: {filesystem.value}",
                )
            return value

        def enumerate(self, handle: int) -> list[_WindowsDirEntry]:
            entries: list[_WindowsDirEntry] = []
            first = True
            while True:
                buffer = ctypes.create_string_buffer(64 * 1024)
                info_class = (
                    self.FILE_ID_EXTD_DIRECTORY_RESTART_INFO
                    if first
                    else self.FILE_ID_EXTD_DIRECTORY_INFO
                )
                first = False
                if not self.kernel32.GetFileInformationByHandleEx(
                    handle,
                    info_class,
                    buffer,
                    len(buffer),
                ):
                    error = ctypes.get_last_error()
                    if error == self.ERROR_NO_MORE_FILES:
                        break
                    if error in {
                        self.ERROR_INVALID_PARAMETER,
                        self.ERROR_INVALID_FUNCTION,
                        self.ERROR_NOT_SUPPORTED,
                    }:
                        raise ArtifactSecureIOError(
                            ARTIFACT_FS_UNSUPPORTED,
                            f"extended directory enumeration unsupported ({error})",
                        )
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_UNSAFE,
                        f"directory enumeration failed with winerror {error}",
                    )
                offset = 0
                while True:
                    next_offset = int.from_bytes(buffer[offset : offset + 4], "little")
                    attributes = int.from_bytes(
                        buffer[offset + 56 : offset + 60], "little"
                    )
                    name_length = int.from_bytes(
                        buffer[offset + 60 : offset + 64], "little"
                    )
                    reparse_tag = int.from_bytes(
                        buffer[offset + 68 : offset + 72], "little"
                    )
                    file_id = bytes(buffer[offset + 72 : offset + 88])
                    name_bytes = bytes(buffer[offset + 88 : offset + 88 + name_length])
                    try:
                        name = name_bytes.decode("utf-16-le", errors="strict")
                    except UnicodeDecodeError as error:
                        raise ArtifactSecureIOError(
                            ARTIFACT_FS_UNSUPPORTED,
                            "directory enumeration returned an invalid UTF-16 name",
                        ) from error
                    if name not in {".", ".."}:
                        if not any(file_id):
                            raise ArtifactSecureIOError(
                                ARTIFACT_FS_UNSUPPORTED,
                                "directory enumeration returned a zero file ID",
                            )
                        entries.append(
                            _WindowsDirEntry(name, file_id, attributes, reparse_tag)
                        )
                    if next_offset == 0:
                        break
                    offset += next_offset
                    if offset + 88 > len(buffer):
                        raise ArtifactSecureIOError(
                            ARTIFACT_FS_UNSUPPORTED,
                            "directory enumeration buffer offsets are invalid",
                        )
            return entries

        def entry(self, handle: int, name: str) -> _WindowsDirEntry:
            matches = [entry for entry in self.enumerate(handle) if entry.name == name]
            if len(matches) != 1:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_RACE,
                    "source directory entry is absent or ambiguous",
                )
            return matches[0]

        def create_private_directory(self, path: Path) -> None:
            descriptor = wintypes.LPVOID()
            if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;FA;;;OW)",
                1,
                ctypes.byref(descriptor),
                None,
            ):
                error = ctypes.get_last_error()
                raise ArtifactSecureIOError(
                    ARTIFACT_FS_UNSUPPORTED,
                    f"cannot construct owner-only staging DACL ({error})",
                )
            attributes = _SecurityAttributes(
                ctypes.sizeof(_SecurityAttributes),
                descriptor,
                False,
            )
            try:
                if not self.kernel32.CreateDirectoryW(
                    str(path), ctypes.byref(attributes)
                ):
                    error = ctypes.get_last_error()
                    raise ArtifactSecureIOError(
                        ARTIFACT_OUTPUT_UNSAFE,
                        f"cannot exclusively create private staging directory ({error})",
                    )
            finally:
                self.kernel32.LocalFree(descriptor)

        def create_private_file(self, path: Path, *, delete_access: bool) -> int:
            access = self.GENERIC_READ | self.GENERIC_WRITE
            if delete_access:
                access |= self.DELETE
            handle = self.kernel32.CreateFileW(
                str(path),
                access,
                self.FILE_SHARE_READ,
                None,
                self.CREATE_NEW,
                self.FILE_ATTRIBUTE_TEMPORARY | self.FILE_FLAG_WRITE_THROUGH,
                None,
            )
            if handle == self.INVALID_HANDLE_VALUE:
                error = ctypes.get_last_error()
                raise ArtifactSecureIOError(
                    ARTIFACT_OUTPUT_UNSAFE,
                    f"cannot exclusively create private staging file ({error})",
                )
            return int(handle)

        def rename_by_handle(
            self,
            handle: int,
            output_parent_handle: int,
            destination_name: str,
        ) -> None:
            encoded_name = destination_name.encode("utf-16-le")
            # FILE_RENAME_INFO requires sizeof(the one-WCHAR base structure)
            # plus FileNameLength. Three trailing WCHAR slots keep ctypes'
            # dynamic structure at or above that contract after alignment.
            character_count = len(destination_name) + 3

            class _FileRenameInfo(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD),
                    ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD),
                    ("FileName", wintypes.WCHAR * character_count),
                ]

            information = _FileRenameInfo()
            information.Flags = (
                self.FILE_RENAME_REPLACE_IF_EXISTS | self.FILE_RENAME_POSIX_SEMANTICS
            )
            information.RootDirectory = output_parent_handle
            information.FileNameLength = len(encoded_name)
            information.FileName = destination_name
            status_block = _IOStatusBlock()
            status = self.ntdll.NtSetInformationFile(
                handle,
                ctypes.byref(status_block),
                ctypes.cast(ctypes.byref(information), wintypes.LPVOID),
                ctypes.sizeof(information),
                self.FILE_RENAME_INFORMATION_EX,
            )
            if status < 0:
                error = int(self.ntdll.RtlNtStatusToDosError(status))
                code = (
                    ARTIFACT_FS_UNSUPPORTED
                    if error
                    in {
                        self.ERROR_INVALID_PARAMETER,
                        self.ERROR_INVALID_FUNCTION,
                        self.ERROR_NOT_SUPPORTED,
                    }
                    else ARTIFACT_ATOMIC_PUBLISH_FAILED
                )
                raise ArtifactSecureIOError(
                    code,
                    f"FileRenameInformationEx publication failed with winerror {error}",
                )


if os.name == "nt":
    _WIN = _WinAPI()


if os.name == "nt":

    class _WindowsRoot:
        def __init__(self, path: Path) -> None:
            self._handle = _WIN.open_path_directory(path, code=ARTIFACT_SOURCE_UNSAFE)
            try:
                _WIN.filesystem(self._handle)
                identity = _WIN.identity(self._handle)
                _WIN.validate_identity(identity, directory=True)
                self.final_path = _WIN.final_path(self._handle)
            except BaseException:
                _WIN.close(self._handle)
                raise
            self._closed = False

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            if not self._closed:
                _WIN.close(self._handle)
                self._closed = True

        def _open_parent(self, parts: Sequence[str]) -> tuple[list[int], int, str]:
            validated = _validate_parts(parts)
            held: list[int] = []
            parent = self._handle
            try:
                for component in validated[:-1]:
                    expected = _WIN.entry(parent, component)
                    if (
                        expected.attributes & _WIN.FILE_ATTRIBUTE_REPARSE_POINT
                        or expected.reparse_tag
                    ):
                        raise ArtifactSecureIOError(
                            ARTIFACT_SOURCE_UNSAFE,
                            "source directory entry is a reparse point",
                        )
                    child = _WIN.open_relative(parent, component, directory=True)
                    identity = _WIN.identity(child)
                    _WIN.validate_identity(identity, directory=True)
                    if identity.file_id != expected.file_id:
                        _WIN.close(child)
                        raise ArtifactSecureIOError(
                            ARTIFACT_SOURCE_RACE,
                            "source directory identity changed during traversal",
                        )
                    held.append(child)
                    parent = child
                return held, parent, validated[-1]
            except BaseException:
                for handle in reversed(held):
                    _WIN.close(handle)
                raise

        def _open_regular(
            self,
            parent_handle: int,
            name: str,
        ) -> tuple[BinaryIO, _WindowsIdentity]:
            expected = _WIN.entry(parent_handle, name)
            if (
                expected.attributes & _WIN.FILE_ATTRIBUTE_REPARSE_POINT
                or expected.reparse_tag
            ):
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_UNSAFE,
                    "selected source entry is a reparse point",
                )
            handle = _WIN.open_relative(parent_handle, name, directory=False)
            try:
                identity = _WIN.identity(handle)
                _WIN.validate_identity(identity, directory=False)
                if identity.file_id != expected.file_id:
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_RACE,
                        "opened source differs from enumerated entry",
                    )
                if identity.links != 1:
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_HARDLINK,
                        "selected source link count is not exactly one",
                    )
                import msvcrt

                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                handle = 0
                return os.fdopen(descriptor, "rb", closefd=True), identity
            except BaseException:
                _WIN.close(handle)
                raise

        @staticmethod
        def _source_handle(source: BinaryIO) -> int:
            import msvcrt

            return int(msvcrt.get_osfhandle(source.fileno()))

        def _verify_regular(
            self,
            source: BinaryIO,
            before: _WindowsIdentity,
            parent_handle: int,
            name: str,
        ) -> None:
            after = _WIN.identity(self._source_handle(source))
            entry = _WIN.entry(parent_handle, name)
            if after != before:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_CHANGED,
                    "source metadata changed while bytes were frozen",
                )
            if entry.file_id != before.file_id:
                raise ArtifactSecureIOError(
                    ARTIFACT_SOURCE_RACE,
                    "source directory entry changed while bytes were frozen",
                )

        def read_control(self, relative_parts: Sequence[str]) -> bytes:
            held, parent, name = self._open_parent(relative_parts)
            try:
                source, before = self._open_regular(parent, name)
                with source:
                    payload = source.read(MAX_CONTROL_BYTES + 1)
                    if len(payload) > MAX_CONTROL_BYTES:
                        raise ArtifactSecureIOError(
                            ARTIFACT_SOURCE_UNSAFE,
                            "artifact allowlist exceeds the control-file limit",
                        )
                    self._verify_regular(source, before, parent, name)
                    return payload
            finally:
                for handle in reversed(held):
                    _WIN.close(handle)

        def freeze_file(
            self,
            relative_parts: Sequence[str],
            archive_name: str,
            spool: BinaryIO,
        ) -> FrozenMember:
            held, parent, name = self._open_parent(relative_parts)
            try:
                source, before = self._open_regular(parent, name)
                with source:
                    member = _copy_source_to_spool(source, spool, archive_name)
                    self._verify_regular(source, before, parent, name)
                    return member
            finally:
                for handle in reversed(held):
                    _WIN.close(handle)

        def _freeze_directory(
            self,
            directory_handle: int,
            prefix: tuple[str, ...],
            selected_names: set[str],
            spool: BinaryIO,
        ) -> list[FrozenMember]:
            members: list[FrozenMember] = []
            entries = sorted(
                _WIN.enumerate(directory_handle), key=lambda entry: entry.name
            )
            for entry in entries:
                _validate_parts((entry.name,))
                if (
                    entry.attributes & _WIN.FILE_ATTRIBUTE_REPARSE_POINT
                    or entry.reparse_tag
                ):
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_UNSAFE,
                        "selected tree contains a reparse point",
                    )
                archive_parts = (*prefix, entry.name)
                archive_name = "/".join(archive_parts)
                is_directory = bool(entry.attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
                if is_directory:
                    if entry.name in IGNORED_DIRECTORIES:
                        continue
                    child = _WIN.open_relative(
                        directory_handle,
                        entry.name,
                        directory=True,
                    )
                    try:
                        before = _WIN.identity(child)
                        _WIN.validate_identity(before, directory=True)
                        if before.file_id != entry.file_id:
                            raise ArtifactSecureIOError(
                                ARTIFACT_SOURCE_RACE,
                                "selected directory changed during enumeration",
                            )
                        members.extend(
                            self._freeze_directory(
                                child,
                                archive_parts,
                                selected_names,
                                spool,
                            )
                        )
                        if _WIN.identity(child) != before:
                            raise ArtifactSecureIOError(
                                ARTIFACT_SOURCE_RACE,
                                "selected directory changed during traversal",
                            )
                    finally:
                        _WIN.close(child)
                else:
                    if Path(entry.name).suffix.casefold() in IGNORED_SUFFIXES:
                        continue
                    if archive_name in selected_names:
                        raise ArtifactSecureIOError(
                            ARTIFACT_SOURCE_UNSAFE,
                            "duplicate archive name",
                        )
                    selected_names.add(archive_name)
                    source, before = self._open_regular(directory_handle, entry.name)
                    with source:
                        member = _copy_source_to_spool(source, spool, archive_name)
                        self._verify_regular(
                            source,
                            before,
                            directory_handle,
                            entry.name,
                        )
                        members.append(member)
            return members

        def freeze_tree(
            self,
            relative_parts: Sequence[str],
            selected_names: set[str],
            spool: BinaryIO,
        ) -> list[FrozenMember]:
            held, parent, name = self._open_parent(relative_parts)
            directory = 0
            try:
                expected = _WIN.entry(parent, name)
                directory = _WIN.open_relative(parent, name, directory=True)
                identity = _WIN.identity(directory)
                _WIN.validate_identity(identity, directory=True)
                if identity.file_id != expected.file_id:
                    raise ArtifactSecureIOError(
                        ARTIFACT_SOURCE_RACE,
                        "allowlisted tree changed during secure open",
                    )
                return self._freeze_directory(
                    directory,
                    tuple(_validate_parts(relative_parts)),
                    selected_names,
                    spool,
                )
            finally:
                _WIN.close(directory)
                for handle in reversed(held):
                    _WIN.close(handle)


if os.name == "nt":

    class _WindowsPublisher:
        def __init__(self, path: Path, source_root: _WindowsRoot) -> None:
            if not path.is_dir():
                raise ArtifactSecureIOError(
                    ARTIFACT_OUTPUT_UNSAFE,
                    "output parent must already exist",
                )
            self._parent_handle = _WIN.open_path_directory(
                path,
                code=ARTIFACT_OUTPUT_UNSAFE,
                enumerate_access=False,
                share_writes=True,
            )
            try:
                _WIN.filesystem(self._parent_handle)
                identity = _WIN.identity(self._parent_handle)
                _WIN.validate_identity(identity, directory=True)
                parent_final = _WIN.final_path(self._parent_handle)
                if _same_or_descendant(parent_final, source_root.final_path):
                    raise ArtifactSecureIOError(
                        ARTIFACT_OUTPUT_UNSAFE,
                        "output parent must be outside the plugin root",
                    )
                self._parent_path = Path(parent_final)
                self._stage_name = f".artifact-stage-{secrets.token_hex(16)}"
                self._stage_path = self._parent_path / self._stage_name
                _WIN.create_private_directory(self._stage_path)
                self._stage_handle = _WIN.open_path_directory(
                    self._stage_path,
                    code=ARTIFACT_OUTPUT_UNSAFE,
                )
                self._stage_identity = _WIN.identity(self._stage_handle)
                _WIN.validate_identity(self._stage_identity, directory=True)
            except BaseException:
                _WIN.close(self._parent_handle)
                raise
            self._spool_path: Path | None = None
            self._zip_path: Path | None = None
            self._spool_file: BinaryIO | None = None
            self._spool_identity: _WindowsIdentity | None = None
            self._zip_file: BinaryIO | None = None
            self._zip_identity: _WindowsIdentity | None = None
            self._published = False
            self._closed = False

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            try:
                self._cleanup()
            except ArtifactSecureIOError:
                if exc is None:
                    raise

        @staticmethod
        def _file_from_handle(handle: int) -> BinaryIO:
            import msvcrt

            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            return os.fdopen(descriptor, "w+b", closefd=True)

        @staticmethod
        def _native_handle(file: BinaryIO) -> int:
            import msvcrt

            return int(msvcrt.get_osfhandle(file.fileno()))

        def _create_private_file(
            self, prefix: str, *, delete_access: bool
        ) -> tuple[Path, BinaryIO]:
            path = self._stage_path / f"{prefix}-{secrets.token_hex(16)}"
            handle = _WIN.create_private_file(path, delete_access=delete_access)
            try:
                return path, self._file_from_handle(handle)
            except BaseException:
                _WIN.close(handle)
                raise

        def create_private_spool(self) -> BinaryIO:
            if self._spool_file is not None:
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    "private spool already exists",
                )
            self._spool_path, self._spool_file = self._create_private_file(
                "spool",
                delete_access=False,
            )
            self._spool_identity = _WIN.identity(self._native_handle(self._spool_file))
            return self._spool_file

        def create_zip_temp(self) -> BinaryIO:
            if self._zip_file is not None:
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    "private ZIP temp already exists",
                )
            self._zip_path, self._zip_file = self._create_private_file(
                "artifact",
                delete_access=True,
            )
            self._zip_identity = _WIN.identity(self._native_handle(self._zip_file))
            return self._zip_file

        def publish(self, destination_name: str) -> None:
            destination_name = _validate_destination_name(destination_name)
            if self._zip_file is None or self._zip_identity is None:
                raise ArtifactSecureIOError(
                    ARTIFACT_ATOMIC_PUBLISH_FAILED,
                    "ZIP temp was not created",
                )
            handle = self._native_handle(self._zip_file)
            try:
                self._zip_file.flush()
                os.fsync(self._zip_file.fileno())
                current_identity = _WIN.identity(handle)
                if (
                    current_identity.volume != self._zip_identity.volume
                    or current_identity.file_id != self._zip_identity.file_id
                ):
                    raise ArtifactSecureIOError(
                        ARTIFACT_ATOMIC_PUBLISH_FAILED,
                        "private ZIP temp identity changed before publication",
                    )
                if self._zip_path is None:
                    raise ArtifactSecureIOError(
                        ARTIFACT_ATOMIC_PUBLISH_FAILED,
                        "private ZIP temp has no retained staging name",
                    )
                staged_entry = _WIN.entry(self._stage_handle, self._zip_path.name)
                if staged_entry.file_id != current_identity.file_id:
                    raise ArtifactSecureIOError(
                        ARTIFACT_ATOMIC_PUBLISH_FAILED,
                        "private ZIP temp entry was substituted before publication",
                    )
                self._zip_identity = current_identity
                _WIN.rename_by_handle(handle, self._parent_handle, destination_name)
                self._published = True
                final_handle = _WIN.open_relative_metadata(
                    self._parent_handle,
                    destination_name,
                )
                try:
                    final_identity = _WIN.identity(final_handle)
                    if final_identity.file_id != self._zip_identity.file_id:
                        raise ArtifactSecureIOError(
                            ARTIFACT_ATOMIC_PUBLISH_FAILED,
                            "published directory entry identity does not match ZIP temp",
                        )
                finally:
                    _WIN.close(final_handle)
            except ArtifactSecureIOError:
                raise
            except OSError as error:
                raise ArtifactSecureIOError(
                    ARTIFACT_ATOMIC_PUBLISH_FAILED,
                    "Windows atomic publication failed",
                ) from error

        def _unlink_owned(
            self,
            path: Path | None,
            expected: _WindowsIdentity | None,
        ) -> None:
            if path is None:
                return
            matches = [
                entry
                for entry in _WIN.enumerate(self._stage_handle)
                if entry.name == path.name
            ]
            if not matches:
                return
            if len(matches) != 1 or expected is None:
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    "refusing cleanup without one retained staging identity",
                )
            if matches[0].file_id != expected.file_id:
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    "refusing to clean up a substituted staging entry",
                )
            path.unlink()

        def _cleanup(self) -> None:
            if self._closed:
                return
            if self._zip_file is not None:
                self._zip_file.close()
            if self._spool_file is not None:
                self._spool_file.close()
            try:
                if not self._published:
                    self._unlink_owned(self._zip_path, self._zip_identity)
                self._unlink_owned(self._spool_path, self._spool_identity)
                stage_now = _WIN.identity(self._stage_handle)
                if (
                    stage_now.volume != self._stage_identity.volume
                    or stage_now.file_id != self._stage_identity.file_id
                    or not stage_now.directory
                    or stage_now.reparse_tag
                ):
                    raise ArtifactSecureIOError(
                        ARTIFACT_IO_FAILED,
                        "refusing to clean up a changed staging directory",
                    )
                _WIN.close(self._stage_handle)
                _WIN.close(self._parent_handle)
                # The output-parent handle deliberately denies delete sharing
                # while the build is live. Release both verified authorities
                # before removing the now-empty invocation-owned directory.
                self._stage_path.rmdir()
                self._closed = True
            except OSError as error:
                raise ArtifactSecureIOError(
                    ARTIFACT_IO_FAILED,
                    "private Windows staging cleanup failed "
                    f"({error!r}, winerror={error.winerror}, errno={error.errno})",
                ) from error


class WindowsNtHandleBackend:
    """Windows local-NTFS/ReFS backend using root-relative NtCreateFile."""

    def open_root(self, path: Path) -> TrustedRootHandle:
        if os.name != "nt":
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "Windows backend requested on another platform",
            )
        return _WindowsRoot(path)

    def open_output_parent(
        self,
        path: Path,
        *,
        source_root: TrustedRootHandle,
    ) -> AtomicOutputPublisher:
        if os.name != "nt" or not isinstance(source_root, _WindowsRoot):
            raise ArtifactSecureIOError(
                ARTIFACT_FS_UNSUPPORTED,
                "Windows output publisher requires a Windows source root",
            )
        return _WindowsPublisher(path, source_root)


def get_secure_backend() -> SecureArtifactBackend:
    """Select only a backend that can enforce the frozen platform contract."""

    if os.name == "nt":
        return WindowsNtHandleBackend()
    if sys.platform == "linux":
        return LinuxOpenAt2Backend()
    raise ArtifactSecureIOError(
        ARTIFACT_FS_UNSUPPORTED,
        f"no secure artifact backend for platform {sys.platform}",
    )
