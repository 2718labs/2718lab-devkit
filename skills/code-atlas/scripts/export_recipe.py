"""Export one verified local Code Atlas recipe as an atomic promotion bundle."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[3]
MCP_TOOLS = ROOT / "mcp-tools"
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from code_atlas.canonical import canonical_json, thaw_json  # noqa: E402
from code_atlas.models import EdgeRelation, NodeKind, RecipeManifest  # noqa: E402
from code_atlas.recipes import render_pattern_card  # noqa: E402
from code_atlas.security import MAX_RECIPE_BYTES, MAX_TEMPLATE_BYTES  # noqa: E402
from code_atlas.service import hydrate_local_manifest  # noqa: E402
from code_atlas.store import AtlasStore, StoreConflictError  # noqa: E402


_HASH_PREFIX = "sha256:"
_STAGE_PREFIX = ".code-atlas-stage-"


class PromotionError(ValueError):
    """A deliberately terse CLI failure that never carries untrusted values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _PromotionArgumentParser(argparse.ArgumentParser):
    """Map parser failures to the same secret-free CLI error boundary."""

    def error(self, _message: str) -> None:
        raise PromotionError("promotion_recipe_invalid")


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _object_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Return the stable file-object identity used to detect replacement."""

    return _identity(value)[:3]


def _unsafe_status(path: Path, value: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0) & 0x400
        or (callable(is_junction) and is_junction(path))
    )


def _lexical_absolute(path: str | Path) -> Path:
    """Normalize ``.``/``..`` without resolving through a possible link."""

    return Path(os.path.abspath(os.fspath(path)))


def _safe_existing(
    path: Path, *, directory: bool = False, regular: bool = False
) -> Path:
    """Return an absolute path only after every existing component is safe."""

    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if not parts:
        raise PromotionError("promotion_data_root_unsafe")
    cursor = Path(parts[0])
    try:
        for part in parts[1:]:
            cursor /= part
            value = cursor.lstat()
            if _unsafe_status(cursor, value):
                raise PromotionError("promotion_data_root_unsafe")
        final = absolute.lstat()
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_data_root_unsafe") from exc
    if (directory and not stat.S_ISDIR(final.st_mode)) or (
        regular and not stat.S_ISREG(final.st_mode)
    ):
        raise PromotionError("promotion_data_root_unsafe")
    return absolute


def _safe_output_parent(path: Path) -> Path:
    try:
        absolute = _lexical_absolute(path)
        parts = absolute.parts
        if not parts:
            raise PromotionError("promotion_output_unsafe")
        cursor = Path(parts[0])
        for part in parts[1:]:
            cursor /= part
            value = cursor.lstat()
            if _unsafe_status(cursor, value):
                raise PromotionError("promotion_output_unsafe")
        if not stat.S_ISDIR(absolute.lstat().st_mode):
            raise PromotionError("promotion_output_unsafe")
        return absolute
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc


def _overlaps(left: Path, right: Path) -> bool:
    left_value = _lexical_absolute(left)
    right_value = _lexical_absolute(right)
    try:
        left_text = os.path.normcase(os.fspath(left_value))
        right_text = os.path.normcase(os.fspath(right_value))
        common = os.path.commonpath((left_text, right_text))
        return common in {left_text, right_text}
    except ValueError:
        return False


def _contains_plugin_cache(path: Path) -> bool:
    components = [part.casefold() for part in _lexical_absolute(path).parts]
    return any(
        first == "plugins" and second == "cache"
        for first, second in zip(components, components[1:], strict=False)
    )


def _digest(data: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _manifest_from_store(store: AtlasStore, recipe_id: str) -> RecipeManifest:
    metadata = store.recipe_metadata(recipe_id)
    if (
        metadata is None
        or metadata.get("recipe_id") != recipe_id
        or metadata.get("layer") != "local"
        or metadata.get("state") not in {"", "ready", "READY", None}
    ):
        raise PromotionError("promotion_recipe_invalid")
    graph = store.graph_query(
        (recipe_id,),
        max_nodes=200,
        max_edges=400,
        max_depth=4,
        byte_budget=MAX_RECIPE_BYTES,
    )
    roots = [node for node in graph.nodes if node.node_id == recipe_id]
    if graph.truncated or len(roots) != 1 or roots[0].kind is not NodeKind.RECIPE:
        raise PromotionError("promotion_recipe_invalid")
    try:
        manifest = hydrate_local_manifest(roots[0])
    except Exception as exc:
        raise PromotionError("promotion_recipe_invalid") from exc
    expected = {
        "intent_id": manifest.intent_id,
        "language": manifest.language_name,
        "framework": manifest.framework_name or "",
        "layer": manifest.layer,
        "version": manifest.version,
        "manifest_hash": manifest.manifest_hash,
        "repository_signature": manifest.repository_signature,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise PromotionError("promotion_recipe_invalid")
    if manifest.quarantine_state not in (None, "", "ready", "READY"):
        raise PromotionError("promotion_recipe_invalid")
    template_hashes = tuple(
        sorted({operation.template_hash for operation in manifest.operations})
    )
    if not template_hashes or len(template_hashes) > 8:
        raise PromotionError("promotion_recipe_invalid")
    template_nodes: dict[str, str] = {}
    implementation_edges: set[tuple[str, str]] = set()
    for node in graph.nodes:
        if node.kind is not NodeKind.CODE_TEMPLATE:
            continue
        payload = thaw_json(node.payload)
        if (
            type(payload) is not dict
            or set(payload) != {"template_hash", "kind"}
            or not isinstance(payload["template_hash"], str)
            or not isinstance(payload["kind"], str)
        ):
            raise PromotionError("promotion_recipe_invalid")
        if node.node_id in template_nodes:
            raise PromotionError("promotion_recipe_invalid")
        template_nodes[node.node_id] = payload["template_hash"]
    for edge in graph.edges:
        if edge.relation is EdgeRelation.HAS_IMPLEMENTATION:
            implementation_edges.add((edge.source_id, edge.target_id))
    if len(template_nodes) != len(template_hashes) or set(
        template_nodes.values()
    ) != set(template_hashes):
        raise PromotionError("promotion_recipe_invalid")
    if implementation_edges != {(recipe_id, node_id) for node_id in template_nodes}:
        raise PromotionError("promotion_recipe_invalid")
    return manifest


def _capture_safe_directory_chain(
    path: Path,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    """Capture all existing directory components without link resolution."""

    absolute = _safe_output_parent(path)
    parts = absolute.parts
    cursor = Path(parts[0])
    records: list[tuple[Path, tuple[int, int, int]]] = []
    try:
        root_status = cursor.lstat()
        if _unsafe_status(cursor, root_status) or not stat.S_ISDIR(root_status.st_mode):
            raise PromotionError("promotion_output_unsafe")
        records.append((cursor, _object_identity(root_status)))
        for part in parts[1:]:
            cursor /= part
            value = cursor.lstat()
            if _unsafe_status(cursor, value) or not stat.S_ISDIR(value.st_mode):
                raise PromotionError("promotion_output_unsafe")
            records.append((cursor, _object_identity(value)))
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc
    return tuple(records)


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _posix_directory_flags() -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise PromotionError("promotion_write_failed")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0)


def _open_posix_directory(name: str | Path, *, directory_fd: int | None = None) -> int:
    try:
        if directory_fd is None:
            return os.open(name, _posix_directory_flags())
        return os.open(name, _posix_directory_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise PromotionError("promotion_output_raced") from exc


def _win_kernel32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise PromotionError("promotion_write_failed")
    return ctypes.WinDLL("kernel32", use_last_error=True)


_WIN_GENERIC_READ = 0x80000000
_WIN_DELETE = 0x00010000
_WIN_SHARE_READ = 0x00000001
_WIN_SHARE_WRITE = 0x00000002
_WIN_CREATE_NEW = 1
_WIN_OPEN_EXISTING = 3
_WIN_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _win_close_handle(handle: int | None) -> None:
    if handle is None:
        return
    kernel32 = _win_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def _win_open_directory(path: Path, *, delete: bool = False) -> int:
    """Open one directory itself and retain a no-delete-sharing lease."""

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
    desired = _WIN_GENERIC_READ | (_WIN_DELETE if delete else 0)
    handle = create_file(
        str(path),
        desired,
        _WIN_SHARE_READ | _WIN_SHARE_WRITE,
        None,
        _WIN_OPEN_EXISTING,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _WIN_INVALID_HANDLE:
        raise PromotionError("promotion_output_raced")
    try:
        value = path.lstat()
        if _unsafe_status(path, value) or not stat.S_ISDIR(value.st_mode):
            raise PromotionError("promotion_output_raced")
        return int(handle)
    except Exception:
        _win_close_handle(int(handle))
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


_WIN_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_WIN_FILE_OPEN = 1
_WIN_FILE_CREATE = 2
_WIN_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WIN_FILE_DIRECTORY_FILE = 0x00000001
_WIN_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WIN_FILE_OPEN_REPARSE_POINT = 0x00200000
_WIN_OBJ_CASE_INSENSITIVE = 0x00000040
_WIN_OBJ_DONT_REPARSE = 0x00001000
_WIN_DIRECTORY_ACCESS = 0x00120089


def _win_open_relative_directory(
    parent_handle: int, name: str, *, create: bool
) -> int | None:
    """Open/create one basename below a retained Windows directory handle."""

    if not name or "\\" in name or "/" in name:
        raise PromotionError("promotion_write_failed")
    buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _WinUnicodeString(
        len(name) * ctypes.sizeof(ctypes.c_wchar),
        (len(name) + 1) * ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(buffer, ctypes.c_void_p),
    )
    attributes = _WinObjectAttributes(
        ctypes.sizeof(_WinObjectAttributes),
        ctypes.c_void_p(parent_handle),
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
    status = create_file(
        ctypes.byref(output),
        _WIN_DIRECTORY_ACCESS,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        _WIN_FILE_ATTRIBUTE_NORMAL,
        _WIN_SHARE_READ | _WIN_SHARE_WRITE,
        _WIN_FILE_CREATE if create else _WIN_FILE_OPEN,
        _WIN_FILE_DIRECTORY_FILE
        | _WIN_FILE_SYNCHRONOUS_IO_NONALERT
        | _WIN_FILE_OPEN_REPARSE_POINT,
        None,
        0,
    )
    if status != 0:
        if create and (status & 0xFFFFFFFF) == _WIN_STATUS_OBJECT_NAME_COLLISION:
            return None
        raise PromotionError("promotion_output_raced")
    return int(output.value)


class _WinRenameInfo(ctypes.Structure):
    _fields_ = (
        ("replace_if_exists", ctypes.c_byte),
        ("padding", ctypes.c_byte * 7),
        ("root_directory", ctypes.c_void_p),
        ("file_name_length", ctypes.c_uint32),
        ("file_name", ctypes.c_wchar * 1),
    )


def _win_rename_noreplace(stage_handle: int, destination: Path) -> None:
    """Rename the retained stage handle without following a mutable source path."""

    target = str(destination)
    encoded = target.encode("utf-16-le") + b"\x00\x00"
    size = _WinRenameInfo.file_name.offset + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    info = ctypes.cast(buffer, ctypes.POINTER(_WinRenameInfo)).contents
    info.replace_if_exists = 0
    info.root_directory = None
    # SetFileInformationByHandle consumes this field as a UTF-16 character
    # count on supported Windows runtimes, despite the header's byte wording.
    info.file_name_length = len(target)
    ctypes.memmove(
        ctypes.addressof(buffer) + _WinRenameInfo.file_name.offset,
        encoded,
        len(encoded),
    )
    kernel32 = _win_kernel32()
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_information.restype = ctypes.c_int
    if set_information(ctypes.c_void_p(stage_handle), 3, buffer, size):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise FileExistsError(error, "destination exists", str(destination))
    raise OSError(error, "SetFileInformationByHandle failed", str(destination))


def _win_write_regular(path: Path, data: bytes) -> None:
    """Create one new regular file while its verified parents remain leased."""

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
        0x40000000,
        _WIN_SHARE_READ | _WIN_SHARE_WRITE,
        None,
        _WIN_CREATE_NEW,
        _WIN_FILE_ATTRIBUTE_NORMAL | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _WIN_INVALID_HANDLE:
        raise PromotionError("promotion_write_failed")
    descriptor: int | None = None
    try:
        import msvcrt

        descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY)
        handle = None
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise PromotionError("promotion_write_failed")
            offset += written
        os.fsync(descriptor)
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_write_failed") from exc
    finally:
        _close_descriptor(descriptor)
        if handle is not None:
            _win_close_handle(int(handle))


def _write_file(
    path: Path,
    data: bytes,
    *,
    directory_fd: int | None = None,
    filename: str | None = None,
    windows_locks: tuple[int, ...] = (),
) -> None:
    descriptor: int | None = None
    try:
        if directory_fd is None:
            if not windows_locks:
                raise PromotionError("promotion_write_failed")
            _win_write_regular(path, data)
            return
        if not filename:
            raise PromotionError("promotion_write_failed")
        descriptor = os.open(
            filename,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise PromotionError("promotion_write_failed")
            offset += written
        os.fsync(descriptor)
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_write_failed") from exc
    finally:
        _close_descriptor(descriptor)


def _capture_output_parent(parent: Path) -> tuple[int, int, int]:
    """Validate and identify the current output parent object."""

    safe_parent = _safe_output_parent(parent)
    try:
        value = safe_parent.lstat()
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc
    if _unsafe_status(safe_parent, value) or not stat.S_ISDIR(value.st_mode):
        raise PromotionError("promotion_output_unsafe")
    return _object_identity(value)


def _win_open_regular(path: Path) -> int:
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
        _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _WIN_INVALID_HANDLE:
        raise PromotionError("promotion_write_failed")
    try:
        value = path.lstat()
        if _unsafe_status(path, value) or not stat.S_ISREG(value.st_mode):
            raise PromotionError("promotion_write_failed")
        return int(handle)
    except Exception:
        _win_close_handle(int(handle))
        raise


class _WinDispositionInfo(ctypes.Structure):
    _fields_ = (("delete_file", ctypes.c_byte),)


def _win_open_owned_cleanup_handle(
    path: Path,
    expected: tuple[int, int, int],
    *,
    directory: bool,
) -> int | None:
    """Open an owned object for deletion and authenticate the opened handle.

    ``CreateFileW`` excludes delete sharing once it succeeds.  The identity
    check is deliberately performed through that handle, rather than through
    a later pathname lookup, so an object swapped immediately before opening
    is left in place instead of being deleted.
    """

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
    flags = _WIN_FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _WIN_FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(
        str(path),
        _WIN_GENERIC_READ | _WIN_DELETE,
        _WIN_SHARE_READ | _WIN_SHARE_WRITE,
        None,
        _WIN_OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _WIN_INVALID_HANDLE:
        return None
    descriptor: int | None = None
    try:
        import msvcrt

        # ``open_osfhandle`` transfers ownership to the CRT descriptor.  It
        # also gives us a handle-authenticated ``fstat`` with Python's exact
        # device/inode representation, rather than attempting to reconstruct
        # that representation from FILE_ID_INFO by hand.
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
        handle = None
        value = os.fstat(descriptor)
        if (
            _object_identity(value) != expected
            or (directory and not stat.S_ISDIR(value.st_mode))
            or (not directory and not stat.S_ISREG(value.st_mode))
        ):
            _close_descriptor(descriptor)
            return None
        return descriptor
    except OSError:
        _close_descriptor(descriptor)
        return None
    finally:
        if handle is not None:
            _win_close_handle(int(handle))


def _win_mark_handle_for_delete(handle: int) -> None:
    """Mark an already-authenticated Windows handle for delete-on-close."""

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
        ctypes.c_void_p(handle),
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        return
    error = ctypes.get_last_error()
    raise OSError(error, "SetFileInformationByHandle disposition failed")


def _win_mark_descriptor_for_delete(descriptor: int) -> None:
    import msvcrt

    _win_mark_handle_for_delete(msvcrt.get_osfhandle(descriptor))


class _StageLease:
    """Retain verified directory capabilities for all staging operations."""

    def __init__(
        self,
        parent: Path,
        expected_parent_identity: tuple[int, int, int] | None,
    ) -> None:
        self.parent = _lexical_absolute(parent)
        self.parent_identity = _capture_output_parent(self.parent)
        if (
            expected_parent_identity is not None
            and self.parent_identity != expected_parent_identity
        ):
            raise PromotionError("promotion_output_raced")
        self.stage: Path | None = None
        self.stage_identity: tuple[int, int, int] | None = None
        self._parent_fd: int | None = None
        self._stage_fd: int | None = None
        self._parent_handles: list[int] = []
        self._stage_handle: int | None = None
        if os.name == "posix":
            self._parent_fd = _open_posix_directory(self.parent)
            if _object_identity(os.fstat(self._parent_fd)) != self.parent_identity:
                self.close()
                raise PromotionError("promotion_output_raced")
        elif os.name == "nt":
            try:
                records = _capture_safe_directory_chain(self.parent)
                if records[-1][1] != self.parent_identity:
                    raise PromotionError("promotion_output_raced")
                for path, identity in records:
                    handle = _win_open_directory(path)
                    if _object_identity(path.lstat()) != identity:
                        _win_close_handle(handle)
                        raise PromotionError("promotion_output_raced")
                    self._parent_handles.append(handle)
            except Exception:
                self.close()
                raise
        else:
            raise PromotionError("promotion_write_failed")

    def close(self) -> None:
        _close_descriptor(self._stage_fd)
        self._stage_fd = None
        _close_descriptor(self._parent_fd)
        self._parent_fd = None
        _win_close_handle(self._stage_handle)
        self._stage_handle = None
        while self._parent_handles:
            _win_close_handle(self._parent_handles.pop())

    def _assert_parent(self) -> None:
        try:
            if os.name == "posix":
                if (
                    self._parent_fd is None
                    or _object_identity(os.fstat(self._parent_fd))
                    != self.parent_identity
                ):
                    raise PromotionError("promotion_output_raced")
            if _capture_output_parent(self.parent) != self.parent_identity:
                raise PromotionError("promotion_output_raced")
        except PromotionError:
            raise
        except OSError as exc:
            raise PromotionError("promotion_output_raced") from exc

    def _assert_stage(self) -> None:
        if (
            self.stage is None
            or self.stage_identity is None
            or self.stage.parent != self.parent
        ):
            raise PromotionError("promotion_output_raced")
        self._assert_parent()
        try:
            if os.name == "posix":
                if self._parent_fd is None or self._stage_fd is None:
                    raise PromotionError("promotion_output_raced")
                status = os.stat(
                    self.stage.name,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
                opened = os.fstat(self._stage_fd)
                if _object_identity(opened) != self.stage_identity:
                    raise PromotionError("promotion_output_raced")
            else:
                if self._stage_handle is None:
                    raise PromotionError("promotion_output_raced")
                status = self.stage.lstat()
            if (
                _unsafe_status(self.stage, status)
                or not stat.S_ISDIR(status.st_mode)
                or _object_identity(status) != self.stage_identity
            ):
                raise PromotionError("promotion_output_raced")
        except PromotionError:
            raise
        except OSError as exc:
            raise PromotionError("promotion_output_raced") from exc

    def create_stage(self) -> Path:
        self._assert_parent()
        if os.name == "posix":
            if self._parent_fd is None:
                raise PromotionError("promotion_output_raced")
            for _attempt in range(64):
                name = _STAGE_PREFIX + secrets.token_hex(16)
                try:
                    os.mkdir(name, 0o700, dir_fd=self._parent_fd)
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise PromotionError("promotion_write_failed") from exc
                stage_fd = _open_posix_directory(name, directory_fd=self._parent_fd)
                status = os.fstat(stage_fd)
                if not stat.S_ISDIR(status.st_mode):
                    _close_descriptor(stage_fd)
                    raise PromotionError("promotion_output_raced")
                self._stage_fd = stage_fd
                self.stage = self.parent / name
                self.stage_identity = _object_identity(status)
                return self.stage
            raise PromotionError("promotion_write_failed")
        try:
            stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=self.parent))
            initial = stage.lstat()
            if _unsafe_status(stage, initial) or not stat.S_ISDIR(initial.st_mode):
                raise PromotionError("promotion_output_raced")
            handle = _win_open_directory(stage, delete=True)
            if _object_identity(stage.lstat()) != _object_identity(initial):
                _win_close_handle(handle)
                raise PromotionError("promotion_output_raced")
            self.stage = stage
            self.stage_identity = _object_identity(initial)
            self._stage_handle = handle
            return stage
        except PromotionError:
            raise
        except OSError as exc:
            raise PromotionError("promotion_write_failed") from exc

    @staticmethod
    def _relative_parts(relative: str) -> tuple[tuple[str, ...], str]:
        pieces = tuple(relative.split("/"))
        if (
            not pieces
            or any(not piece or piece in {".", ".."} for piece in pieces)
            or any("\\" in piece for piece in pieces)
        ):
            raise PromotionError("promotion_write_failed")
        return pieces[:-1], pieces[-1]

    def _posix_leaf_directory(
        self,
        relative: str,
        directories: dict[Path, tuple[int, int, int]],
        *,
        create: bool,
    ) -> tuple[int, str]:
        if self.stage is None or self._stage_fd is None:
            raise PromotionError("promotion_output_raced")
        components, filename = self._relative_parts(relative)
        descriptor = os.dup(self._stage_fd)
        current = self.stage
        try:
            for component in components:
                candidate = current / component
                known = directories.get(candidate)
                created = False
                if create and known is None:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                        created = True
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise PromotionError("promotion_write_failed") from exc
                try:
                    value = os.stat(
                        component,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise PromotionError("promotion_write_failed") from exc
                if _unsafe_status(candidate, value) or not stat.S_ISDIR(value.st_mode):
                    raise PromotionError("promotion_write_failed")
                identity = _object_identity(value)
                if known is None:
                    if not created:
                        raise PromotionError("promotion_write_failed")
                    directories[candidate] = identity
                elif identity != known:
                    raise PromotionError("promotion_write_failed")
                next_descriptor = _open_posix_directory(
                    component, directory_fd=descriptor
                )
                if _object_identity(os.fstat(next_descriptor)) != identity:
                    _close_descriptor(next_descriptor)
                    raise PromotionError("promotion_write_failed")
                _close_descriptor(descriptor)
                descriptor = next_descriptor
                current = candidate
            return descriptor, filename
        except Exception:
            _close_descriptor(descriptor)
            raise

    def _win_leaf_directory(
        self,
        relative: str,
        directories: dict[Path, tuple[int, int, int]],
        *,
        create: bool,
    ) -> tuple[tuple[int, ...], str]:
        if self.stage is None or self._stage_handle is None:
            raise PromotionError("promotion_output_raced")
        components, filename = self._relative_parts(relative)
        handles: list[int] = []
        current = self.stage
        parent_handle = self._stage_handle
        try:
            for component in components:
                candidate = current / component
                known = directories.get(candidate)
                handle = _win_open_relative_directory(
                    parent_handle, component, create=create and known is None
                )
                if handle is None:
                    raise PromotionError("promotion_write_failed")
                value = candidate.lstat()
                if _unsafe_status(candidate, value) or not stat.S_ISDIR(value.st_mode):
                    _win_close_handle(handle)
                    raise PromotionError("promotion_write_failed")
                identity = _object_identity(value)
                if known is None:
                    if not create:
                        _win_close_handle(handle)
                        raise PromotionError("promotion_write_failed")
                    directories[candidate] = identity
                elif identity != known:
                    _win_close_handle(handle)
                    raise PromotionError("promotion_write_failed")
                handles.append(handle)
                parent_handle = handle
                current = candidate
            return tuple(handles), filename
        except Exception:
            while handles:
                _win_close_handle(handles.pop())
            raise

    def write(
        self,
        relative: str,
        data: bytes,
        directories: dict[Path, tuple[int, int, int]],
    ) -> tuple[Path, tuple[int, int, int]]:
        self._assert_stage()
        if self.stage is None:
            raise PromotionError("promotion_output_raced")
        target = self.stage.joinpath(*relative.split("/"))
        if os.name == "posix":
            descriptor, filename = self._posix_leaf_directory(
                relative, directories, create=True
            )
            try:
                _write_file(target, data, directory_fd=descriptor, filename=filename)
                value = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
            finally:
                _close_descriptor(descriptor)
        else:
            handles, _filename = self._win_leaf_directory(
                relative, directories, create=True
            )
            try:
                if self._stage_handle is None:
                    raise PromotionError("promotion_output_raced")
                _write_file(
                    target,
                    data,
                    windows_locks=(self._stage_handle, *handles),
                )
                value = target.lstat()
            finally:
                for handle in reversed(handles):
                    _win_close_handle(handle)
        if _unsafe_status(target, value) or not stat.S_ISREG(value.st_mode):
            raise PromotionError("promotion_write_failed")
        return target, _object_identity(value)

    def verify(
        self,
        relative: str,
        expected: bytes,
        identity: tuple[int, int, int],
        directories: dict[Path, tuple[int, int, int]],
    ) -> None:
        self._assert_stage()
        if self.stage is None:
            raise PromotionError("promotion_output_raced")
        target = self.stage.joinpath(*relative.split("/"))
        if os.name == "posix":
            descriptor, filename = self._posix_leaf_directory(
                relative, directories, create=False
            )
            file_descriptor: int | None = None
            try:
                before = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                if (
                    _unsafe_status(target, before)
                    or not stat.S_ISREG(before.st_mode)
                    or _object_identity(before) != identity
                ):
                    raise PromotionError("promotion_write_failed")
                file_descriptor = os.open(
                    filename,
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
                    dir_fd=descriptor,
                )
                if _object_identity(os.fstat(file_descriptor)) != identity:
                    raise PromotionError("promotion_write_failed")
                chunks: list[bytes] = []
                total = 0
                while total <= len(expected):
                    chunk = os.read(file_descriptor, len(expected) + 1 - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                after = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise PromotionError("promotion_write_failed") from exc
            finally:
                _close_descriptor(file_descriptor)
                _close_descriptor(descriptor)
        else:
            handles, _filename = self._win_leaf_directory(
                relative, directories, create=False
            )
            handle: int | None = None
            descriptor: int | None = None
            try:
                before = target.lstat()
                if (
                    _unsafe_status(target, before)
                    or not stat.S_ISREG(before.st_mode)
                    or _object_identity(before) != identity
                ):
                    raise PromotionError("promotion_write_failed")
                handle = _win_open_regular(target)
                after_open = target.lstat()
                if _object_identity(after_open) != identity:
                    raise PromotionError("promotion_write_failed")
                import msvcrt

                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
                handle = None
                chunks = []
                total = 0
                while total <= len(expected):
                    chunk = os.read(descriptor, len(expected) + 1 - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                after = target.lstat()
            except OSError as exc:
                raise PromotionError("promotion_write_failed") from exc
            finally:
                _close_descriptor(descriptor)
                _win_close_handle(handle)
                for directory_handle in reversed(handles):
                    _win_close_handle(directory_handle)
        actual = b"".join(chunks)
        if (
            _object_identity(after) != identity
            or actual != expected
            or _digest(actual) != _digest(expected)
        ):
            raise PromotionError("promotion_write_failed")

    def _posix_cleanup_parent(
        self,
        path: Path,
        directories: dict[Path, tuple[int, int, int]],
    ) -> tuple[int, str] | None:
        """Open the retained parent directory of one owned stage child."""

        if self.stage is None or self._stage_fd is None:
            return None
        descriptor: int | None = None
        try:
            relative = path.relative_to(self.stage).as_posix()
            components, leaf = self._relative_parts(relative)
            descriptor = os.dup(self._stage_fd)
            current = self.stage
            for component in components:
                candidate = current / component
                expected = directories.get(candidate)
                if expected is None:
                    _close_descriptor(descriptor)
                    return None
                value = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if (
                    _unsafe_status(candidate, value)
                    or not stat.S_ISDIR(value.st_mode)
                    or _object_identity(value) != expected
                ):
                    _close_descriptor(descriptor)
                    return None
                next_descriptor = _open_posix_directory(
                    component, directory_fd=descriptor
                )
                if _object_identity(os.fstat(next_descriptor)) != expected:
                    _close_descriptor(next_descriptor)
                    _close_descriptor(descriptor)
                    return None
                _close_descriptor(descriptor)
                descriptor = next_descriptor
                current = candidate
            return descriptor, leaf
        except (OSError, PromotionError):
            _close_descriptor(descriptor)
            return None

    def _cleanup_posix(
        self,
        files: dict[Path, tuple[int, int, int]],
        directories: dict[Path, tuple[int, int, int]],
    ) -> bool:
        """Best-effort cleanup through retained POSIX directory descriptors."""

        if (
            self.stage is None
            or self.stage_identity is None
            or self._stage_fd is None
            or self._parent_fd is None
        ):
            return False
        try:
            if (
                _object_identity(os.fstat(self._stage_fd)) != self.stage_identity
                or _object_identity(os.fstat(self._parent_fd)) != self.parent_identity
            ):
                return False
            for path, identity in sorted(files.items(), key=lambda item: str(item[0])):
                parent = self._posix_cleanup_parent(path, directories)
                if parent is None:
                    return False
                descriptor, name = parent
                try:
                    value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        _unsafe_status(path, value)
                        or not stat.S_ISREG(value.st_mode)
                        or _object_identity(value) != identity
                    ):
                        return False
                    os.unlink(name, dir_fd=descriptor)
                finally:
                    _close_descriptor(descriptor)
            for path, identity in sorted(
                directories.items(),
                key=lambda item: (len(item[0].parts), str(item[0])),
                reverse=True,
            ):
                parent = self._posix_cleanup_parent(path, directories)
                if parent is None:
                    return False
                descriptor, name = parent
                try:
                    value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        _unsafe_status(path, value)
                        or not stat.S_ISDIR(value.st_mode)
                        or _object_identity(value) != identity
                    ):
                        return False
                    os.rmdir(name, dir_fd=descriptor)
                finally:
                    _close_descriptor(descriptor)
            named_stage = os.stat(
                self.stage.name, dir_fd=self._parent_fd, follow_symlinks=False
            )
            if (
                _unsafe_status(self.stage, named_stage)
                or not stat.S_ISDIR(named_stage.st_mode)
                or _object_identity(named_stage) != self.stage_identity
            ):
                return False
            os.rmdir(self.stage.name, dir_fd=self._parent_fd)
            return True
        except (OSError, PromotionError):
            return False

    def cleanup(
        self,
        files: dict[Path, tuple[int, int, int]],
        directories: dict[Path, tuple[int, int, int]],
    ) -> bool:
        """Remove only this lease's verified staging objects before release."""

        if os.name == "posix":
            return self._cleanup_posix(files, directories)
        if os.name != "nt":
            return False
        try:
            self._assert_stage()
            if self._stage_handle is None:
                return False
            for path, identity in sorted(files.items(), key=lambda item: str(item[0])):
                descriptor = _win_open_owned_cleanup_handle(
                    path, identity, directory=False
                )
                if descriptor is None:
                    return False
                try:
                    _win_mark_descriptor_for_delete(descriptor)
                finally:
                    _close_descriptor(descriptor)
            # A directory must be empty before its delete disposition can be
            # committed, so dispose of deepest descendants first.
            for path, identity in sorted(
                directories.items(),
                key=lambda item: (len(item[0].parts), str(item[0])),
                reverse=True,
            ):
                descriptor = _win_open_owned_cleanup_handle(
                    path, identity, directory=True
                )
                if descriptor is None:
                    return False
                try:
                    _win_mark_descriptor_for_delete(descriptor)
                finally:
                    _close_descriptor(descriptor)
            # The stage's original DELETE-capable handle was opened during
            # creation and remains leased, so it is itself the capability for
            # the final deletion.  ``close`` below makes that disposition take
            # effect before it releases the enclosing parent leases.
            _win_mark_handle_for_delete(self._stage_handle)
            return True
        except (OSError, PromotionError):
            return False

    def publish(self, output: Path) -> None:
        self._assert_stage()
        if self.stage is None or output.parent != self.parent or not output.name:
            raise PromotionError("promotion_output_raced")
        if os.name == "posix":
            if self._parent_fd is None:
                raise PromotionError("promotion_output_raced")
            _renameat2_noreplace(
                self._parent_fd, self.stage.name, self._parent_fd, output.name
            )
            return
        if self._stage_handle is None:
            raise PromotionError("promotion_output_raced")
        _win_rename_noreplace(self._stage_handle, output)


def _renameat2_noreplace(
    old_directory_fd: int,
    old_name: str,
    new_directory_fd: int,
    new_name: str,
) -> None:
    """Publish basename-only entries through retained POSIX directory FDs."""

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


def _atomic_noreplace_directory(
    stage: Path,
    destination: Path,
    *,
    capability: _StageLease | None = None,
) -> None:
    """Atomically publish a directory only if the destination is absent."""

    if capability is not None:
        capability.publish(destination)
        return
    if os.name in {"nt", "posix"}:
        raise OSError(errno.ENOSYS, "retained directory capability is required")
    raise OSError(errno.ENOSYS, "atomic no-replace promotion is unavailable")


def _build_files(store: AtlasStore, manifest: RecipeManifest) -> dict[str, bytes]:
    templates: dict[str, bytes] = {}
    total = 0
    for template_hash in sorted({item.template_hash for item in manifest.operations}):
        try:
            body = store.read_blob_verified(template_hash, max_bytes=MAX_TEMPLATE_BYTES)
        except StoreConflictError as exc:
            raise PromotionError("promotion_recipe_invalid") from exc
        total += len(body)
        if total > MAX_RECIPE_BYTES:
            raise PromotionError("promotion_recipe_invalid")
        templates[f"templates/sha256/{template_hash.removeprefix(_HASH_PREFIX)}"] = body
    files = {
        "manifest.json": canonical_json(manifest.to_dict()).encode("utf-8") + b"\n",
        "pattern-card.md": render_pattern_card(manifest).encode("utf-8"),
        **templates,
    }
    if len(files) + 1 > 11:
        raise PromotionError("promotion_recipe_invalid")
    records = [
        {"path": path, "sha256": _digest(body), "size": len(body)}
        for path, body in sorted(files.items())
    ]
    receipt = {
        "schema_version": "1",
        "recipe_id": manifest.recipe_id,
        "manifest_hash": manifest.manifest_hash,
        "records": records,
    }
    files["promotion-receipt.json"] = canonical_json(receipt).encode("utf-8") + b"\n"
    return files


def _write_bundle(
    parent: Path,
    output: Path,
    files: dict[str, bytes],
    *,
    expected_parent_identity: tuple[int, int, int] | None = None,
) -> None:
    stage: Path | None = None
    parent_identity: tuple[int, int, int] | None = None
    stage_identity: tuple[int, int, int] | None = None
    owned_files: dict[Path, tuple[int, int, int]] = {}
    owned_directories: dict[Path, tuple[int, int, int]] = {}
    lease: _StageLease | None = None
    try:
        parent = _lexical_absolute(parent)
        output = _lexical_absolute(output)
        if output.parent != parent or not output.name:
            raise PromotionError("promotion_output_unsafe")
        lease = _StageLease(parent, expected_parent_identity)
        parent_identity = lease.parent_identity
        stage = lease.create_stage()
        stage_identity = lease.stage_identity
        if stage_identity is None:
            raise PromotionError("promotion_output_raced")
        for relative, body in sorted(files.items()):
            target, identity = lease.write(relative, body, owned_directories)
            owned_files[target] = identity
        for relative, expected in files.items():
            target = stage.joinpath(*relative.split("/"))
            identity = owned_files.get(target)
            if identity is None:
                raise PromotionError("promotion_write_failed")
            lease.verify(relative, expected, identity, owned_directories)
        lease._assert_stage()
        if os.name == "posix" and lease._stage_fd is not None:
            os.fsync(lease._stage_fd)
        try:
            _atomic_noreplace_directory(stage, output, capability=lease)
        except FileExistsError as exc:
            raise PromotionError("promotion_output_raced") from exc
        except OSError as exc:
            raise PromotionError("promotion_write_failed") from exc
        stage = None
        try:
            if _capture_output_parent(parent) != parent_identity:
                raise PromotionError("promotion_output_raced")
            output_status = output.lstat()
        except PromotionError as exc:
            raise PromotionError("promotion_output_raced") from exc
        except OSError as exc:
            raise PromotionError("promotion_output_raced") from exc
        if (
            _unsafe_status(output, output_status)
            or not stat.S_ISDIR(output_status.st_mode)
            or _object_identity(output_status) != stage_identity
        ):
            raise PromotionError("promotion_output_raced")
        if os.name == "posix" and lease._parent_fd is not None:
            os.fsync(lease._parent_fd)
    finally:
        if lease is not None:
            if stage is not None:
                lease.cleanup(owned_files, owned_directories)
            lease.close()


def export_recipe(data_root: Path, recipe_id: str, output: Path) -> None:
    durable_root = _safe_existing(data_root, directory=True)
    if _overlaps(durable_root, ROOT):
        raise PromotionError("promotion_data_root_unsafe")
    if not isinstance(recipe_id, str) or not (
        recipe_id.startswith(_HASH_PREFIX)
        and len(recipe_id) == len(_HASH_PREFIX) + 64
        and all(character in "0123456789abcdef" for character in recipe_id[7:])
    ):
        raise PromotionError("promotion_recipe_invalid")
    parent = _safe_output_parent(output.parent)
    initial_parent_identity = _capture_output_parent(parent)
    final = _lexical_absolute(output)
    if not final.name or _contains_plugin_cache(final):
        raise PromotionError("promotion_output_unsafe")
    if _overlaps(final, durable_root) or _overlaps(final, ROOT):
        raise PromotionError("promotion_output_unsafe")
    try:
        existing = final.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc
    if existing is not None:
        if _unsafe_status(final, existing):
            raise PromotionError("promotion_output_unsafe")
        raise PromotionError("promotion_output_exists")
    try:
        store = AtlasStore.open_readonly(
            durable_root / "code-atlas.sqlite3",
            durable_root / "code-atlas-cas",
        )
    except StoreConflictError as exc:
        raise PromotionError("promotion_db_open_failed") from exc
    try:
        try:
            manifest = _manifest_from_store(store, recipe_id)
            files = _build_files(store, manifest)
        except PromotionError:
            raise
        except (OSError, StoreConflictError, TypeError, ValueError) as exc:
            raise PromotionError("promotion_recipe_invalid") from exc
    finally:
        store.close()
    if _capture_output_parent(parent) != initial_parent_identity:
        raise PromotionError("promotion_output_raced")
    _write_bundle(
        parent,
        final,
        files,
        expected_parent_identity=initial_parent_identity,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _PromotionArgumentParser(prog="export_recipe.py")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--recipe-id", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    try:
        values = _parser().parse_args(None if argv is None else list(argv))
        export_recipe(Path(values.data_root), values.recipe_id, Path(values.output))
    except PromotionError as error:
        print(error.code, file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError):
        print("promotion_write_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
