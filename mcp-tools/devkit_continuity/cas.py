"""Private, verified content-addressed storage for frozen Continuity bodies."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .canonical import is_hash_id


class ContinuityCasError(RuntimeError):
    """Stable private CAS failure."""


class ContinuityCas:
    def __init__(self, root: Path, scratch_root: Path, *, read_only: bool) -> None:
        self.root, self.scratch_root, self.read_only = root, scratch_root, read_only

    @classmethod
    def bootstrap(cls, root: Path, scratch_root: Path) -> ContinuityCas:
        _safe_root(root, create=True)
        _safe_root(scratch_root, create=True)
        return cls(root, scratch_root, read_only=False)

    @classmethod
    def open_prepared(cls, root: Path, scratch_root: Path, read_only: bool) -> ContinuityCas:
        _safe_root(root, create=False)
        _safe_root(scratch_root, create=False)
        return cls(root, scratch_root, read_only=read_only)

    def put_verified(self, content_hash: str, byte_length: int, body: bytes) -> str:
        if self.read_only:
            raise ContinuityCasError("CONTINUITY_CAS_READ_ONLY")
        _validate(content_hash, byte_length, body)
        target = self._target(content_hash)
        self._verify_target_parent(target)
        if target.exists():
            self._read_target(target, content_hash, byte_length)
            return content_hash
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._verify_target_parent(target)
            stage_directory = self.root / ".staging"
            stage_directory.mkdir(exist_ok=True)
            _safe_root(stage_directory, create=False)
            stage = stage_directory / (content_hash[7:] + ".stage")
            if stage.exists():
                raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
            descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
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
            finally:
                try:
                    stage.unlink()
                except FileNotFoundError:
                    pass
            self._read_target(target, content_hash, byte_length)
            return content_hash
        except ContinuityCasError:
            raise
        except (OSError, ValueError) as error:
            raise ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE") from error

    def read_verified(self, content_hash: str, byte_length: int) -> bytes:
        if not is_hash_id(content_hash) or type(byte_length) is not int or byte_length < 0:
            raise ContinuityCasError("CONTINUITY_CAS_INVALID")
        return self._read_target(self._target(content_hash), content_hash, byte_length)

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


def _validate(content_hash: object, byte_length: object, body: object) -> None:
    if not is_hash_id(content_hash) or type(byte_length) is not int or byte_length < 0 or type(body) is not bytes:
        raise ContinuityCasError("CONTINUITY_CAS_INVALID")
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if digest != content_hash or len(body) != byte_length:
        raise ContinuityCasError("CONTINUITY_CAS_CONFLICT")


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
