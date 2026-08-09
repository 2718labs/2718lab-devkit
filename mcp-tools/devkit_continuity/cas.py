"""Private, verified content-addressed storage for frozen Continuity bodies."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

from .canonical import is_hash_id


class ContinuityCasError(RuntimeError):
    """Stable private CAS failure."""


class ContinuityCas:
    def __init__(
        self,
        root: Path,
        scratch_root: Path,
        *,
        read_only: bool,
        native_backend: Any | None = None,
    ) -> None:
        if os.name == "nt" and native_backend is None:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
        self.root, self.scratch_root, self.read_only = root, scratch_root, read_only
        self._native_backend = native_backend

    @classmethod
    def open_prepared(cls, root: Path, scratch_root: Path, read_only: bool) -> ContinuityCas:
        if os.name == "nt":
            try:
                backend = _open_native_backend(root, read_only=read_only)
            except ContinuityCasError:
                raise
            except Exception as error:
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
            if backend is None:
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
            return cls(root, scratch_root, read_only=read_only, native_backend=backend)
        _safe_root(root, create=False)
        _safe_root(scratch_root, create=False)
        if not root.is_dir() or not scratch_root.is_dir():
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
        return cls(root, scratch_root, read_only=read_only)

    def put_verified(self, content_hash: str, byte_length: int, body: bytes) -> str:
        if self.read_only:
            raise ContinuityCasError("CONTINUITY_CAS_READ_ONLY")
        _validate(content_hash, byte_length, body)
        if self._native_backend is not None:
            try:
                return self._native_backend.put_verified(content_hash, byte_length, body)
            except ContinuityCasError:
                raise
            except Exception as error:
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
        target = self._target(content_hash)
        self._verify_target_parent(target)
        if target.exists():
            self._read_target(target, content_hash, byte_length)
            return content_hash
        stage: Path | None = None
        stage_owned = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._verify_target_parent(target)
            stage_directory = self.root / ".staging"
            stage_directory.mkdir(exist_ok=True)
            _safe_root(stage_directory, create=False)
            stage = stage_directory / (
                content_hash[7:] + "." + secrets.token_hex(16) + ".stage"
            )
            descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            stage_owned = True
            try:
                os.write(descriptor, body)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._read_stage(stage, content_hash, byte_length)
            try:
                self._verify_target_parent(target)
                os.link(stage, target)
            except FileExistsError:
                self._read_target(target, content_hash, byte_length)
            except OSError as error:
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
            self._read_target(target, content_hash, byte_length)
            return content_hash
        except ContinuityCasError:
            raise
        except (OSError, ValueError) as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
        finally:
            if stage is not None and stage_owned:
                try:
                    _safe_root(stage.parent, create=False)
                    stage.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error

    def read_verified(self, content_hash: str, byte_length: int) -> bytes:
        if not is_hash_id(content_hash) or type(byte_length) is not int or byte_length < 0:
            raise ContinuityCasError("CONTINUITY_CAS_INVALID")
        if self._native_backend is not None:
            try:
                return self._native_backend.read_verified(content_hash, byte_length)
            except ContinuityCasError:
                raise
            except Exception as error:
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
        return self._read_target(self._target(content_hash), content_hash, byte_length)

    def close(self) -> None:
        if self._native_backend is not None:
            try:
                self._native_backend.close()
            except Exception as error:
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error

    def _target(self, content_hash: str) -> Path:
        if not is_hash_id(content_hash):
            raise ContinuityCasError("CONTINUITY_CAS_INVALID")
        digest = content_hash[7:]
        return self.root / "sha256" / digest[:2] / digest[2:4] / digest

    def _read_target(self, path: Path, content_hash: str, byte_length: int) -> bytes:
        self._verify_target_parent(path)
        return self._read_verified_file(path, content_hash, byte_length)

    def _read_stage(self, path: Path, content_hash: str, byte_length: int) -> bytes:
        _safe_root(self.root, create=False)
        _safe_root(path.parent, create=False)
        return self._read_verified_file(path, content_hash, byte_length)

    def _verify_target_parent(self, target: Path) -> None:
        digest = target.name
        if len(digest) != 64 or target != self._target("sha256:" + digest):
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
        for part in (self.root, self.root / "sha256", target.parent.parent, target.parent):
            _safe_root(part, create=False)

    @staticmethod
    def _read_verified_file(path: Path, content_hash: str, byte_length: int) -> bytes:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or _is_link(path, status):
                    raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
            finally:
                os.close(descriptor)
        except FileNotFoundError as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
        except OSError as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
        _validate(content_hash, byte_length, body)
        return body


class _WindowsHandleCasBackend:
    """Windows-only CAS operations rooted in verified directory handles."""

    def __init__(
        self, root: Path, api: Any, *, read_only: bool, create_root: bool = False
    ) -> None:
        self._root, self._api, self._read_only, self._create_root = (
            root,
            api,
            read_only,
            create_root,
        )
        self._root_handle: Any | None = None

    def verify_prepared(self) -> None:
        if self._root_handle is not None:
            self._api.revalidate(self._root_handle, directory=True)
            return
        if not self._root.is_absolute():
            raise OSError("native CAS root must be absolute")
        root = (
            self._open_root_relative_to_trusted_parent()
            if self._create_root
            else self._api.open_root(self._root, writable=not self._read_only)
        )
        try:
            self._api.revalidate(root, directory=True)
            self._root_handle = root
        finally:
            if self._root_handle is not root:
                self._api.close(root)

    def _open_root_relative_to_trusted_parent(self) -> Any:
        """Create the CAS root only below the host-trusted private data-root handle."""
        if not self._root.is_absolute():
            raise OSError("native CAS root must be absolute")
        root_name = self._root.name
        _require_component(root_name)
        parent = self._api.open_root(self._root.parent, writable=True)
        root: Any | None = None
        transferred = False
        try:
            self._api.revalidate(parent, directory=True)
            root = self._api.open_child(
                parent,
                root_name,
                directory=True,
                create=True,
                exclusive=False,
            )
            self._api.revalidate(root, directory=True)
            self._api.close(parent)
            transferred = True
            return root
        finally:
            if not transferred:
                if root is not None:
                    try:
                        self._api.close(root)
                    except Exception:
                        pass
                try:
                    self._api.close(parent)
                except Exception:
                    pass

    def close(self) -> None:
        """Close the verified root; operations own their child handles."""
        root = self._root_handle
        if root is not None:
            self._api.close(root)
            self._root_handle = None

    def put_verified(self, content_hash: str, byte_length: int, body: bytes) -> str:
        if self._read_only:
            raise ContinuityCasError("CONTINUITY_CAS_READ_ONLY")
        _validate(content_hash, byte_length, body)
        digest = _digest_component(content_hash)
        try:
            with self._tree(digest, create=True) as (handles, staging, target_parent):
                existing = self._try_read_target(handles, target_parent, digest, content_hash, byte_length)
                if existing is not None:
                    return content_hash
                stage = None
                try:
                    stage_name = f"{digest}.{secrets.token_hex(16)}.stage"
                    stage = self._api.open_child(
                        staging,
                        stage_name,
                        directory=False,
                        create=True,
                        exclusive=True,
                        delete_on_close=True,
                    )
                    self._api.revalidate(stage, directory=False)
                    self._api.write_all(stage, body)
                    self._api.flush(stage)
                    self._api.rewind(stage)
                    self._read_handle(stage, content_hash, byte_length)
                    self._revalidate_tree(handles)
                    published = self._api.link(stage, target_parent, digest)
                    expected_identity = getattr(stage, "identity", None) if published else None
                    self._read_target(
                        handles,
                        target_parent,
                        digest,
                        content_hash,
                        byte_length,
                        expected_identity=expected_identity,
                    )
                    return content_hash
                finally:
                    if stage is not None:
                        try:
                            self._api.delete_owned(stage)
                        finally:
                            self._api.close(stage)
        except ContinuityCasError:
            raise
        except Exception as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error

    def read_verified(self, content_hash: str, byte_length: int) -> bytes:
        if not is_hash_id(content_hash) or type(byte_length) is not int or byte_length < 0:
            raise ContinuityCasError("CONTINUITY_CAS_INVALID")
        digest = _digest_component(content_hash)
        try:
            with self._tree(digest, create=False) as (handles, _staging, target_parent):
                return self._read_target(handles, target_parent, digest, content_hash, byte_length)
        except ContinuityCasError:
            raise
        except Exception as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error

    @contextmanager
    def _tree(self, digest: str, *, create: bool) -> Iterator[tuple[tuple[Any, ...], Any | None, Any]]:
        handles: list[Any] = []
        root = self._root_handle
        if root is None:
            raise OSError("native CAS root handle is closed")
        try:
            self._api.revalidate(root, directory=True)
            staging = (
                self._open_directory(root, ".staging", create=True) if create else None
            )
            if staging is not None:
                handles.append(staging)
            sha256 = self._open_directory(root, "sha256", create=create)
            handles.append(sha256)
            first = self._open_directory(sha256, digest[:2], create=create)
            handles.append(first)
            target_parent = self._open_directory(first, digest[2:4], create=create)
            handles.append(target_parent)
            tree = (root, *handles)
            self._revalidate_tree(tree)
            yield tree, staging, target_parent
        except BaseException:
            self._close_children(handles, suppress_errors=True)
            raise
        else:
            self._close_children(handles, suppress_errors=False)

    def _close_children(self, handles: list[Any], *, suppress_errors: bool) -> None:
        first_error: Exception | None = None
        for handle in reversed(handles):
            try:
                self._api.close(handle)
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None and not suppress_errors:
            raise first_error

    def _open_directory(self, parent: Any, name: str, *, create: bool) -> Any:
        handle = self._api.open_child(
            parent, name, directory=True, create=create, exclusive=False
        )
        try:
            self._api.revalidate(handle, directory=True)
            return handle
        except Exception:
            self._api.close(handle)
            raise

    def _revalidate_tree(self, handles: tuple[Any, ...] | list[Any]) -> None:
        for handle in handles:
            self._api.revalidate(handle, directory=True)

    def _try_read_target(
        self,
        handles: tuple[Any, ...],
        target_parent: Any,
        digest: str,
        content_hash: str,
        byte_length: int,
    ) -> bytes | None:
        try:
            return self._read_target(handles, target_parent, digest, content_hash, byte_length)
        except FileNotFoundError:
            return None

    def _read_target(
        self,
        handles: tuple[Any, ...],
        target_parent: Any,
        digest: str,
        content_hash: str,
        byte_length: int,
        *,
        expected_identity: Any | None = None,
    ) -> bytes:
        self._revalidate_tree(handles)
        target = self._api.open_child(
            target_parent, digest, directory=False, create=False, exclusive=False
        )
        try:
            body = self._read_handle(target, content_hash, byte_length)
            if expected_identity is not None and getattr(target, "identity", None) != expected_identity:
                raise OSError("published CAS target identity changed")
            return body
        finally:
            self._api.close(target)

    def _read_handle(self, handle: Any, content_hash: str, byte_length: int) -> bytes:
        self._api.revalidate(handle, directory=False)
        body = self._api.read_all(handle)
        _validate(content_hash, byte_length, body)
        return body


def _digest_component(content_hash: str) -> str:
    if not is_hash_id(content_hash):
        raise ContinuityCasError("CONTINUITY_CAS_INVALID")
    return content_hash[7:]


if os.name == "nt":
    _FILE_READ_DATA = 0x0001
    _FILE_WRITE_DATA = 0x0002
    _FILE_ADD_FILE = 0x0002
    _FILE_ADD_SUBDIRECTORY = 0x0004
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_SUPPORTS_HARD_LINKS = 0x00400000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _OPEN_EXISTING = 3
    _FILE_OPEN = 0x00000001
    _FILE_CREATE = 0x00000002
    _FILE_OPEN_IF = 0x00000003
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_DELETE_ON_CLOSE = 0x00001000
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _OBJ_DONT_REPARSE = 0x00001000
    _FILE_STANDARD_INFO = 1
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _FILE_ID_INFO = 18
    _FILE_LINK_INFORMATION = 11
    _FILE_DISPOSITION_INFORMATION = 13
    _STATUS_OBJECT_NAME_COLLISION = ctypes.c_long(0xC0000035).value
    _STATUS_OBJECT_NAME_NOT_FOUND = ctypes.c_long(0xC0000034).value
    _STATUS_OBJECT_PATH_NOT_FOUND = ctypes.c_long(0xC000003A).value
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_long), ("Information", ctypes.c_size_t)]

    class _FILE_ID_128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FILE_ID_INFO_VALUE(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FILE_ID_128),
        ]

    class _FILE_ATTRIBUTE_TAG_INFO_VALUE(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _FILE_STANDARD_INFO_VALUE(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", ctypes.c_ubyte),
            ("Directory", ctypes.c_ubyte),
            ("_padding", ctypes.c_ushort),
        ]

    class _FILE_LINK_INFORMATION_HEADER(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("_padding", ctypes.c_ubyte * 7),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.ULONG),
        ]

    class _FILE_DISPOSITION_INFORMATION_VALUE(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    class _NativeNtStatus(OSError):
        def __init__(self, status: int) -> None:
            super().__init__(status, "native NT status")
            self.status = status

    class _WindowsNativeHandle:
        def __init__(self, value: int, *, directory: bool) -> None:
            self.value = value
            self.directory = directory
            self.identity: tuple[int, bytes] | None = None
            self.closed = False

    class _WindowsNativeApi:
        """Minimal ctypes facade: only the initial trusted data-root opening receives a path.

        The host must establish and protect the private data root (the CAS root's
        parent) before this first absolute open.  Bootstrap creates or verifies
        the CAS root relative to that trusted parent HANDLE.  Protection starts
        below the verified CAS root HANDLE; it does not protect adversarial
        ancestors while the initial trusted-parent path is parsed.
        """

        def __init__(self) -> None:
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            self._bind_functions()
            self._root_volume: int | None = None

        def _bind_functions(self) -> None:
            self._create_file = self._kernel32.CreateFileW
            self._create_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            self._create_file.restype = wintypes.HANDLE
            self._close_handle = self._kernel32.CloseHandle
            self._close_handle.argtypes = [wintypes.HANDLE]
            self._close_handle.restype = wintypes.BOOL
            self._get_info_ex = self._kernel32.GetFileInformationByHandleEx
            self._get_info_ex.argtypes = [
                wintypes.HANDLE,
                wintypes.INT,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            self._get_info_ex.restype = wintypes.BOOL
            self._get_volume_info = self._kernel32.GetVolumeInformationByHandleW
            self._get_volume_info.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPWSTR,
                wintypes.DWORD,
            ]
            self._get_volume_info.restype = wintypes.BOOL
            self._write_file = self._kernel32.WriteFile
            self._write_file.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ]
            self._write_file.restype = wintypes.BOOL
            self._read_file = self._kernel32.ReadFile
            self._read_file.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ]
            self._read_file.restype = wintypes.BOOL
            self._flush = self._kernel32.FlushFileBuffers
            self._flush.argtypes = [wintypes.HANDLE]
            self._flush.restype = wintypes.BOOL
            self._set_pointer = self._kernel32.SetFilePointerEx
            self._set_pointer.argtypes = [
                wintypes.HANDLE,
                ctypes.c_longlong,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            self._set_pointer.restype = wintypes.BOOL
            self._nt_create = self._ntdll.NtCreateFile
            self._nt_create.argtypes = [
                ctypes.POINTER(wintypes.HANDLE),
                wintypes.DWORD,
                ctypes.POINTER(_OBJECT_ATTRIBUTES),
                ctypes.POINTER(_IO_STATUS_BLOCK),
                ctypes.c_void_p,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                ctypes.c_void_p,
                wintypes.ULONG,
            ]
            self._nt_create.restype = ctypes.c_long
            self._nt_set_information = self._ntdll.NtSetInformationFile
            self._nt_set_information.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_IO_STATUS_BLOCK),
                ctypes.c_void_p,
                wintypes.ULONG,
                wintypes.ULONG,
            ]
            self._nt_set_information.restype = ctypes.c_long

        def open_root(self, root: Path, *, writable: bool) -> _WindowsNativeHandle:
            desired = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
            if writable:
                desired |= _FILE_ADD_FILE | _FILE_ADD_SUBDIRECTORY
            raw = self._create_file(
                str(root),
                desired,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            value = ctypes.cast(raw, ctypes.c_void_p).value
            if value in {None, _INVALID_HANDLE_VALUE}:
                self._raise_last_error()
            handle = _WindowsNativeHandle(int(value), directory=True)
            try:
                self.revalidate(handle, directory=True)
                self._check_filesystem(handle)
                self._root_volume = handle.identity[0] if handle.identity is not None else None
                return handle
            except Exception:
                self.close(handle)
                raise

        def open_child(
            self,
            parent: _WindowsNativeHandle,
            name: str,
            *,
            directory: bool,
            create: bool,
            exclusive: bool = False,
            delete_on_close: bool = False,
        ) -> _WindowsNativeHandle:
            self.revalidate(parent, directory=True)
            _require_component(name)
            text = ctypes.create_unicode_buffer(name)
            encoded = name.encode("utf-16-le")
            unicode_name = _UNICODE_STRING(len(encoded), len(encoded), ctypes.cast(text, wintypes.LPWSTR))
            attributes = _OBJECT_ATTRIBUTES(
                ctypes.sizeof(_OBJECT_ATTRIBUTES),
                wintypes.HANDLE(parent.value),
                ctypes.pointer(unicode_name),
                _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
                None,
                None,
            )
            status_block = _IO_STATUS_BLOCK()
            raw = wintypes.HANDLE()
            if directory:
                desired = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                if create:
                    desired |= _FILE_ADD_FILE | _FILE_ADD_SUBDIRECTORY
                disposition = _FILE_OPEN_IF if create else _FILE_OPEN
                options = (
                    _FILE_DIRECTORY_FILE
                    | _FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_REPARSE_POINT
                )
                file_attributes = _FILE_ATTRIBUTE_DIRECTORY
            else:
                desired = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                if create:
                    desired |= _FILE_WRITE_DATA | _FILE_WRITE_ATTRIBUTES | _DELETE
                disposition = _FILE_CREATE if exclusive else (_FILE_OPEN_IF if create else _FILE_OPEN)
                options = (
                    _FILE_NON_DIRECTORY_FILE
                    | _FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_REPARSE_POINT
                )
                if delete_on_close:
                    if not create:
                        raise OSError("native CAS delete-on-close requires an owned stage")
                    options |= _FILE_DELETE_ON_CLOSE
                file_attributes = _FILE_ATTRIBUTE_NORMAL
            share = _FILE_SHARE_READ if delete_on_close else _FILE_SHARE_READ | _FILE_SHARE_WRITE
            status = self._nt_create(
                ctypes.byref(raw),
                desired,
                ctypes.byref(attributes),
                ctypes.byref(status_block),
                None,
                file_attributes,
                share,
                disposition,
                options,
                None,
                0,
            )
            if status < 0:
                if int(status) in {
                    _STATUS_OBJECT_NAME_NOT_FOUND,
                    _STATUS_OBJECT_PATH_NOT_FOUND,
                }:
                    raise FileNotFoundError(name)
                raise _NativeNtStatus(int(status))
            value = ctypes.cast(raw, ctypes.c_void_p).value
            if value in {None, _INVALID_HANDLE_VALUE}:
                raise OSError("native returned invalid handle")
            handle = _WindowsNativeHandle(int(value), directory=directory)
            try:
                self.revalidate(handle, directory=directory)
                return handle
            except Exception:
                self.close(handle)
                raise

        def revalidate(self, handle: _WindowsNativeHandle, *, directory: bool) -> None:
            if handle.closed or handle.directory is not directory:
                raise OSError("native handle type changed")
            identity, attributes, delete_pending = self._inspect(handle)
            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or delete_pending:
                raise OSError("unsafe native handle")
            if bool(attributes & _FILE_ATTRIBUTE_DIRECTORY) is not directory:
                raise OSError("native handle directory mismatch")
            if identity[0] == 0 or not any(identity[1]):
                raise OSError("native handle identity unavailable")
            if self._root_volume is not None and identity[0] != self._root_volume:
                raise OSError("native handle volume mismatch")
            if handle.identity is not None and handle.identity != identity:
                raise OSError("native handle identity changed")
            handle.identity = identity

        def write_all(self, handle: _WindowsNativeHandle, body: bytes) -> None:
            self.revalidate(handle, directory=False)
            buffer = ctypes.create_string_buffer(body)
            offset = 0
            while offset < len(body):
                written = wintypes.DWORD()
                size = min(len(body) - offset, 0xFFFFFFFF)
                if not self._write_file(
                    wintypes.HANDLE(handle.value),
                    ctypes.byref(buffer, offset),
                    size,
                    ctypes.byref(written),
                    None,
                ):
                    self._raise_last_error()
                if written.value == 0:
                    raise OSError("native CAS short write")
                offset += written.value

        def flush(self, handle: _WindowsNativeHandle) -> None:
            self.revalidate(handle, directory=False)
            if not self._flush(wintypes.HANDLE(handle.value)):
                self._raise_last_error()

        def rewind(self, handle: _WindowsNativeHandle) -> None:
            self.revalidate(handle, directory=False)
            if not self._set_pointer(wintypes.HANDLE(handle.value), 0, None, 0):
                self._raise_last_error()

        def read_all(self, handle: _WindowsNativeHandle) -> bytes:
            self.revalidate(handle, directory=False)
            self.rewind(handle)
            chunks: list[bytes] = []
            while True:
                buffer = ctypes.create_string_buffer(65536)
                count = wintypes.DWORD()
                if not self._read_file(
                    wintypes.HANDLE(handle.value),
                    buffer,
                    ctypes.sizeof(buffer),
                    ctypes.byref(count),
                    None,
                ):
                    self._raise_last_error()
                if count.value == 0:
                    break
                chunks.append(buffer.raw[: count.value])
            return b"".join(chunks)

        def link(
            self,
            source: _WindowsNativeHandle,
            target_parent: _WindowsNativeHandle,
            target_name: str,
        ) -> bool:
            self.revalidate(source, directory=False)
            self.revalidate(target_parent, directory=True)
            _require_component(target_name)
            encoded = target_name.encode("utf-16-le")
            size = 20 + len(encoded)
            buffer = ctypes.create_string_buffer(size)
            header = _FILE_LINK_INFORMATION_HEADER.from_buffer(buffer)
            header.ReplaceIfExists = 0
            header.RootDirectory = wintypes.HANDLE(target_parent.value)
            header.FileNameLength = len(encoded)
            ctypes.memmove(ctypes.addressof(buffer) + 20, encoded, len(encoded))
            status_block = _IO_STATUS_BLOCK()
            status = self._nt_set_information(
                wintypes.HANDLE(source.value),
                ctypes.byref(status_block),
                buffer,
                size,
                _FILE_LINK_INFORMATION,
            )
            if status < 0:
                if int(status) == _STATUS_OBJECT_NAME_COLLISION:
                    return False
                raise _NativeNtStatus(int(status))
            return True

        def delete_owned(self, handle: _WindowsNativeHandle) -> None:
            self.revalidate(handle, directory=False)
            value = _FILE_DISPOSITION_INFORMATION_VALUE(1)
            status_block = _IO_STATUS_BLOCK()
            status = self._nt_set_information(
                wintypes.HANDLE(handle.value),
                ctypes.byref(status_block),
                ctypes.byref(value),
                ctypes.sizeof(value),
                _FILE_DISPOSITION_INFORMATION,
            )
            if status < 0:
                raise _NativeNtStatus(int(status))

        def close(self, handle: _WindowsNativeHandle) -> None:
            if handle.closed:
                return
            if not self._close_handle(wintypes.HANDLE(handle.value)):
                self._raise_last_error()
            handle.closed = True

        def _inspect(self, handle: _WindowsNativeHandle) -> tuple[tuple[int, bytes], int, bool]:
            file_id = _FILE_ID_INFO_VALUE()
            attributes = _FILE_ATTRIBUTE_TAG_INFO_VALUE()
            standard = _FILE_STANDARD_INFO_VALUE()
            native_handle = wintypes.HANDLE(handle.value)
            if not self._get_info_ex(
                native_handle,
                _FILE_ID_INFO,
                ctypes.byref(file_id),
                ctypes.sizeof(file_id),
            ):
                self._raise_last_error()
            if not self._get_info_ex(
                native_handle,
                _FILE_ATTRIBUTE_TAG_INFO,
                ctypes.byref(attributes),
                ctypes.sizeof(attributes),
            ):
                self._raise_last_error()
            if not self._get_info_ex(
                native_handle,
                _FILE_STANDARD_INFO,
                ctypes.byref(standard),
                ctypes.sizeof(standard),
            ):
                self._raise_last_error()
            return (
                (int(file_id.VolumeSerialNumber), bytes(file_id.FileId.Identifier)),
                int(attributes.FileAttributes),
                bool(standard.DeletePending),
            )

        def _check_filesystem(self, handle: _WindowsNativeHandle) -> None:
            filesystem = ctypes.create_unicode_buffer(32)
            serial = wintypes.DWORD()
            maximum = wintypes.DWORD()
            flags = wintypes.DWORD()
            if not self._get_volume_info(
                wintypes.HANDLE(handle.value),
                None,
                0,
                ctypes.byref(serial),
                ctypes.byref(maximum),
                ctypes.byref(flags),
                filesystem,
                len(filesystem),
            ):
                self._raise_last_error()
            if (
                filesystem.value.upper() not in {"NTFS", "REFS"}
                or not flags.value & _FILE_SUPPORTS_HARD_LINKS
            ):
                raise OSError("unsupported CAS filesystem")

        @staticmethod
        def _raise_last_error() -> None:
            raise OSError(ctypes.get_last_error(), "native Windows CAS I/O failed")


def _require_component(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise OSError("unsafe native CAS component")


def _load_windows_native_api() -> Any | None:
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    try:
        return _WindowsNativeApi()
    except Exception:
        return None


def _open_native_backend(
    root: Path, *, read_only: bool, create_root: bool = False
) -> Any | None:
    api = _load_windows_native_api()
    if api is None:
        return None
    backend = _WindowsHandleCasBackend(root, api, read_only=read_only, create_root=create_root)
    try:
        backend.verify_prepared()
        return backend
    except Exception:
        backend.close()
        raise


def _validate(content_hash: object, byte_length: object, body: object) -> None:
    if not is_hash_id(content_hash) or type(byte_length) is not int or byte_length < 0 or type(body) is not bytes:
        raise ContinuityCasError("CONTINUITY_CAS_INVALID")
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if digest != content_hash or len(body) != byte_length:
        raise ContinuityCasError("CONTINUITY_CAS_CONFLICT")


def _bootstrap_cas(root: Path, scratch_root: Path) -> ContinuityCas:
    """Runtime-private CAS creation seam; ordinary openers never create."""
    if os.name == "nt":
        try:
            backend = _open_native_backend(root, read_only=False, create_root=True)
        except ContinuityCasError:
            raise
        except Exception as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error
        if backend is None:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
        return ContinuityCas(root, scratch_root, read_only=False, native_backend=backend)
    _safe_root(root, create=True)
    _safe_root(scratch_root, create=True)
    return ContinuityCas(root, scratch_root, read_only=False)


def _safe_root(path: Path, *, create: bool) -> None:
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        for part in (path, *path.parents):
            try:
                status = part.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(status.st_mode) or _is_link(part, status):
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
    except (OSError, ValueError) as error:
        if isinstance(error, ContinuityCasError):
            raise
        raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error


def _is_link(path: Path, status: os.stat_result) -> bool:
    if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & 0x400:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))
