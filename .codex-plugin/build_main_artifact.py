#!/usr/bin/env python3
"""Build the deterministic MCP-only primary plugin artifact."""

from __future__ import annotations

import argparse
import json
import os
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

ALLOWLIST_NAME = "main-artifact-allowlist.json"
ALLOWLIST_SCHEMA = "2718lab-devkit/main-artifact-allowlist-v1"
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ARCHIVE_MODE = stat.S_IFREG | 0o644
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
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


class ArtifactBuildError(ValueError):
    """A stable fail-closed artifact selection error."""


def parse_args() -> argparse.Namespace:
    """Parse the standalone artifact builder command line."""

    parser = argparse.ArgumentParser(
        description="Build the MCP-only primary plugin artifact."
    )
    parser.add_argument(
        "--root",
        "--plugin-root",
        dest="plugin_root",
        type=Path,
        required=True,
        help="Plugin root directory",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        help="Explicit allowlist (defaults inside the plugin root)",
    )
    parser.add_argument("--output", type=Path, required=True, help="ZIP artifact path")
    return parser.parse_args()


def _safe_relative_path(value: object, *, field: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or "\0" in value
        or ":" in value
    ):
        raise ArtifactBuildError(f"allowlist field `{field}` contains an unsafe path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ArtifactBuildError(f"allowlist field `{field}` contains an unsafe path")
    return relative


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ArtifactBuildError(f"artifact allowlist has duplicate key: {key}")
        seen.add(key)
        values[key] = value
    return values


def _read_paths(payload: dict[str, object], field: str) -> tuple[PurePosixPath, ...]:
    raw_paths = payload.get(field)
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ArtifactBuildError(
            f"artifact allowlist field `{field}` must be a non-empty array"
        )
    paths = tuple(_safe_relative_path(value, field=field) for value in raw_paths)
    canonical = tuple(path.as_posix() for path in paths)
    if len(set(canonical)) != len(canonical):
        raise ArtifactBuildError(f"artifact allowlist field `{field}` has duplicates")
    return paths


def load_allowlist(
    path: Path,
) -> tuple[tuple[PurePosixPath, ...], tuple[PurePosixPath, ...]]:
    """Load one closed-schema allowlist without accepting aliases or globs."""

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except OSError as error:
        raise ArtifactBuildError(f"cannot read artifact allowlist: {path}") from error
    except json.JSONDecodeError as error:
        raise ArtifactBuildError("artifact allowlist is not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"schema", "files", "trees"}:
        raise ArtifactBuildError("artifact allowlist fields are invalid")
    if payload["schema"] != ALLOWLIST_SCHEMA:
        raise ArtifactBuildError("artifact allowlist schema is invalid")
    files = _read_paths(payload, "files")
    trees = _read_paths(payload, "trees")
    combined = tuple(path.as_posix() for path in (*files, *trees))
    if len(set(combined)) != len(combined):
        raise ArtifactBuildError("artifact allowlist has duplicate archive roots")
    return files, trees


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ArtifactBuildError(
            f"selected artifact path is unavailable: {path}"
        ) from error
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode) or attributes & REPARSE_POINT:
        raise ArtifactBuildError(f"selected artifact path is a reparse point: {path}")
    return metadata


def _selected_path(root: Path, relative: PurePosixPath) -> tuple[Path, os.stat_result]:
    current = root
    metadata: os.stat_result | None = None
    for part in relative.parts:
        current /= part
        metadata = _safe_lstat(current)
    if metadata is None:
        raise ArtifactBuildError("selected artifact path is empty")
    try:
        resolved = current.resolve(strict=True)
    except OSError as error:
        raise ArtifactBuildError(
            f"selected artifact path is unavailable: {current}"
        ) from error
    if not _is_within(resolved, root):
        raise ArtifactBuildError(
            f"selected artifact path escapes plugin root: {current}"
        )
    return current, metadata


def _add_file(
    selected: dict[str, Path],
    aliases: dict[str, str],
    root: Path,
    path: Path,
) -> None:
    archive_name = path.relative_to(root).as_posix()
    folded = unicodedata.normalize("NFC", archive_name).casefold()
    if folded in aliases:
        raise ArtifactBuildError(f"duplicate archive name: {archive_name}")
    selected[archive_name] = path
    aliases[folded] = archive_name


def _enumerate_tree(
    selected: dict[str, Path],
    aliases: dict[str, str],
    root: Path,
    directory: Path,
) -> None:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as error:
        raise ArtifactBuildError(
            f"cannot enumerate selected tree: {directory}"
        ) from error
    for entry in entries:
        path = Path(entry.path)
        metadata = _safe_lstat(path)
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ArtifactBuildError(
                f"selected artifact path is unavailable: {path}"
            ) from error
        if not _is_within(resolved, root):
            raise ArtifactBuildError(
                f"selected artifact path escapes plugin root: {path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            if path.name in IGNORED_DIRECTORIES:
                continue
            _enumerate_tree(selected, aliases, root, path)
        elif stat.S_ISREG(metadata.st_mode):
            if path.suffix.casefold() in IGNORED_SUFFIXES:
                continue
            _add_file(selected, aliases, root, path)
        else:
            raise ArtifactBuildError(
                f"selected artifact path is not a regular file: {path}"
            )


def artifact_paths(root: Path, allowlist_path: Path) -> list[tuple[str, Path]]:
    """Return closed, validated archive-name/source-path pairs."""

    files, trees = load_allowlist(allowlist_path)
    selected: dict[str, Path] = {}
    aliases: dict[str, str] = {}
    for relative in files:
        path, metadata = _selected_path(root, relative)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactBuildError(f"allowlisted file is not regular: {relative}")
        _add_file(selected, aliases, root, path)
    for relative in trees:
        path, metadata = _selected_path(root, relative)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactBuildError(f"allowlisted tree is not a directory: {relative}")
        _enumerate_tree(selected, aliases, root, path)
    return sorted(selected.items())


def _root_path(plugin_root: Path) -> Path:
    metadata = _safe_lstat(plugin_root)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactBuildError(f"plugin root is not a directory: {plugin_root}")
    try:
        return plugin_root.resolve(strict=True)
    except OSError as error:
        raise ArtifactBuildError(
            f"plugin root is unavailable: {plugin_root}"
        ) from error


def _inside_root_path(root: Path, candidate: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(candidate))
    if not _is_within(absolute, root):
        raise ArtifactBuildError(f"{label} must be inside the plugin root")
    relative = PurePosixPath(absolute.relative_to(root).as_posix())
    path, metadata = _selected_path(root, relative)
    if not stat.S_ISREG(metadata.st_mode):
        raise ArtifactBuildError(f"{label} is not a regular file")
    return path


def build_main_artifact(
    plugin_root: Path,
    output: Path,
    *,
    allowlist_path: Path | None = None,
) -> list[str]:
    """Build one deterministic ZIP after validating every selected path."""

    root = _root_path(plugin_root)
    output_absolute = Path(os.path.abspath(output))
    output_resolved = output_absolute.resolve(strict=False)
    if _is_within(output_absolute, root) or _is_within(output_resolved, root):
        raise ArtifactBuildError("artifact output must be outside the plugin root")
    if output_absolute.exists():
        _safe_lstat(output_absolute)

    allowlist = _inside_root_path(
        root,
        allowlist_path or root / ".codex-plugin" / ALLOWLIST_NAME,
        label="artifact allowlist",
    )
    files = artifact_paths(root, allowlist)
    output_absolute.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output_absolute,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for archive_name, source in files:
            info = zipfile.ZipInfo(archive_name, date_time=ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = ARCHIVE_MODE << 16
            archive.writestr(info, source.read_bytes(), compresslevel=9)
    return [archive_name for archive_name, _source in files]


def main() -> None:
    """Run the standalone builder and report only its artifact summary."""

    args = parse_args()
    try:
        paths = build_main_artifact(
            args.plugin_root,
            args.output,
            allowlist_path=args.allowlist,
        )
    except (ArtifactBuildError, OSError) as error:
        raise SystemExit(f"artifact build rejected: {error}") from error
    print(f"Wrote {len(paths)} files to {args.output}")


if __name__ == "__main__":
    main()
