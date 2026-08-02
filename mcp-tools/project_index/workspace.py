"""Path validation and opaque identity helpers for registered workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

from .models import IndexError


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WORKSPACE_ID_PREFIX = "sha256:"
_WORKSPACE_ID_LENGTH = len(_WORKSPACE_ID_PREFIX) + 64


def is_workspace_id(value: object) -> bool:
    """Return whether value is a valid opaque workspace identifier."""
    if not isinstance(value, str) or len(value) != _WORKSPACE_ID_LENGTH:
        return False
    if not value.startswith(_WORKSPACE_ID_PREFIX):
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


def canonical_workspace_root(workspace_root: str | os.PathLike[str]) -> Path:
    """Resolve one direct registration input without admitting aliases."""
    if not isinstance(workspace_root, (str, os.PathLike)):
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    try:
        supplied = Path(workspace_root)
    except (TypeError, ValueError) as exc:
        raise IndexError(
            "UNSAFE_WORKSPACE", "workspace registration was rejected"
        ) from exc
    if not supplied.is_absolute():
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    lexical = supplied.absolute()
    _reject_unsafe_ancestors(lexical)
    try:
        root = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise IndexError(
            "UNSAFE_WORKSPACE", "workspace registration was rejected"
        ) from exc
    if not root.is_dir() or _path_key(lexical) != _path_key(root):
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    _reject_unsafe_ancestors(root)
    return root


def workspace_identity(root: Path) -> str:
    """Capture stable root and repository identities for one registration."""
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise IndexError(
            "UNSAFE_WORKSPACE", "workspace registration was rejected"
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode) or _unsafe_path(root):
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    device = int(root_stat.st_dev)
    inode = int(root_stat.st_ino)
    if device < 0 or inode <= 0:
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    repository_identity = _repository_identity(root)
    return f"{device}:{inode}{repository_identity}"


def _repository_identity(root: Path) -> str:
    """Return an opaque Git common-directory identity when the root is a repository."""
    marker = root / ".git"
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise IndexError(
            "UNSAFE_WORKSPACE", "workspace registration was rejected"
        ) from exc
    except OSError:
        if marker.exists() or marker.is_symlink():
            raise IndexError(
                "UNSAFE_WORKSPACE", "workspace registration was rejected"
            ) from None
        return ""
    if completed.returncode != 0:
        if marker.exists() or marker.is_symlink():
            raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
        return ""
    common_directories = completed.stdout.splitlines()
    if len(common_directories) != 1 or not common_directories[0]:
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    try:
        common_directory = Path(common_directories[0]).resolve(strict=True)
        common_stat = common_directory.stat(follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise IndexError(
            "UNSAFE_WORKSPACE", "workspace registration was rejected"
        ) from exc
    if (
        not stat.S_ISDIR(common_stat.st_mode)
        or _unsafe_path(common_directory)
        or int(common_stat.st_dev) < 0
        or int(common_stat.st_ino) <= 0
    ):
        raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
    return f":repo={int(common_stat.st_dev)}:{int(common_stat.st_ino)}"


def workspace_id_for_root(root: Path) -> str:
    """Return a deterministic opaque identifier without retaining root text."""
    return _workspace_identifier(_normalized_root_text(root))


def workspace_id_for_serialized_path(workspace_root: str) -> str:
    """Derive the matching opaque id for a historical stored root path."""
    return _workspace_identifier(_normalized_root_text(Path(workspace_root)))


def workspace_paths_match(left: str | Path, right: str | Path) -> bool:
    """Compare stored and live canonical roots without exposing either."""
    return _path_key(Path(left)) == _path_key(Path(right))


def _workspace_identifier(normalized_root: str) -> str:
    data = json.dumps(
        {"format": "project-index-workspace-v1", "root": normalized_root},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _normalized_root_text(root: Path) -> str:
    return os.path.normcase(os.path.normpath(str(root))).replace("\\", "/")


def _reject_unsafe_ancestors(path: Path) -> None:
    current = path
    while True:
        if _unsafe_path(current):
            raise IndexError("UNSAFE_WORKSPACE", "workspace registration was rejected")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _unsafe_path(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return True


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))
