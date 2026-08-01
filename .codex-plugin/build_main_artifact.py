#!/usr/bin/env python3
"""Build the deterministic MCP-only primary plugin artifact."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

# ``spec_from_file_location`` users do not automatically receive this script's
# directory on sys.path. The adjacent stdlib-only security backend is part of
# the standalone builder, so bind exactly that directory before importing it.
_BUILDER_DIRECTORY = str(Path(__file__).resolve().parent)
if _BUILDER_DIRECTORY not in sys.path:
    sys.path.insert(0, _BUILDER_DIRECTORY)

from artifact_secure_io import (
    ARTIFACT_IO_FAILED,
    ARTIFACT_OUTPUT_UNSAFE,
    ArtifactSecureIOError,
    FrozenMember,
    copy_frozen_member,
    get_secure_backend,
)

ALLOWLIST_NAME = "main-artifact-allowlist.json"
ALLOWLIST_SCHEMA = "2718lab-devkit/main-artifact-allowlist-v1"
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ARCHIVE_MODE = stat.S_IFREG | 0o644


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
    payload_bytes: bytes,
) -> tuple[tuple[PurePosixPath, ...], tuple[PurePosixPath, ...]]:
    """Parse one closed-schema allowlist read from its verified source handle."""

    try:
        payload = json.loads(
            payload_bytes.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except UnicodeDecodeError as error:
        raise ArtifactBuildError("artifact allowlist is not valid UTF-8") from error
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
    try:
        return os.path.commonpath((path, root)) == str(root)
    except ValueError:
        return False


def _relative_parts_inside_root(
    root: Path, candidate: Path, *, label: str
) -> tuple[str, ...]:
    absolute = Path(os.path.abspath(candidate))
    if not _is_within(absolute, root) or absolute == root:
        raise ArtifactBuildError(f"{label} must be inside the plugin root")
    relative = PurePosixPath(absolute.relative_to(root).as_posix())
    return tuple(_safe_relative_path(relative.as_posix(), field=label).parts)


def _add_file(
    selected: dict[str, Path],
    aliases: dict[str, str],
    root: Path,
    path: Path,
) -> None:
    """Retain the locked NFC/case-fold archive-name collision contract."""

    archive_name = path.relative_to(root).as_posix()
    folded = unicodedata.normalize("NFC", archive_name).casefold()
    if folded in aliases:
        raise ArtifactBuildError(f"duplicate archive name: {archive_name}")
    selected[archive_name] = path
    aliases[folded] = archive_name


def _validate_member_aliases(members: list[FrozenMember]) -> list[FrozenMember]:
    aliases: dict[str, str] = {}
    for member in members:
        folded = unicodedata.normalize("NFC", member.archive_name).casefold()
        previous = aliases.get(folded)
        if previous is not None:
            raise ArtifactBuildError(f"duplicate archive name: {member.archive_name}")
        aliases[folded] = member.archive_name
    return sorted(members, key=lambda member: member.archive_name)


def _write_deterministic_zip(
    archive_file: BinaryIO,
    spool: BinaryIO,
    members: list[FrozenMember],
) -> None:
    with zipfile.ZipFile(
        archive_file,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for member in members:
            info = zipfile.ZipInfo(member.archive_name, date_time=ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = ARCHIVE_MODE << 16
            with archive.open(info, "w", force_zip64=True) as destination:
                copy_frozen_member(spool, member, destination)


def build_main_artifact(
    plugin_root: Path,
    output: Path,
    *,
    allowlist_path: Path | None = None,
) -> list[str]:
    """Freeze selected sources, build privately, and atomically replace a name."""

    root_path = Path(os.path.abspath(plugin_root))
    output_absolute = Path(os.path.abspath(output))
    if output_absolute.parent == output_absolute:
        raise ArtifactBuildError(
            f"{ARTIFACT_OUTPUT_UNSAFE}: output has no safe destination name"
        )
    if _is_within(output_absolute, root_path):
        raise ArtifactBuildError(
            f"{ARTIFACT_OUTPUT_UNSAFE}: artifact output must be outside the plugin root"
        )
    allowlist_parts = _relative_parts_inside_root(
        root_path,
        allowlist_path or root_path / ".codex-plugin" / ALLOWLIST_NAME,
        label="artifact allowlist",
    )

    try:
        backend = get_secure_backend()
        with (
            backend.open_root(root_path) as source_root,
            backend.open_output_parent(
                output_absolute.parent,
                source_root=source_root,
            ) as publisher,
        ):
            allowlist_payload = source_root.read_control(allowlist_parts)
            files, trees = load_allowlist(allowlist_payload)
            spool = publisher.create_private_spool()
            selected_names: set[str] = set()
            members: list[FrozenMember] = []
            for relative in files:
                archive_name = relative.as_posix()
                if archive_name in selected_names:
                    raise ArtifactBuildError(f"duplicate archive name: {archive_name}")
                selected_names.add(archive_name)
                members.append(
                    source_root.freeze_file(
                        relative.parts,
                        archive_name,
                        spool,
                    )
                )
            for relative in trees:
                members.extend(
                    source_root.freeze_tree(
                        relative.parts,
                        selected_names,
                        spool,
                    )
                )
            members = _validate_member_aliases(members)
            zip_temp = publisher.create_zip_temp()
            _write_deterministic_zip(zip_temp, spool, members)
            publisher.publish(output_absolute.name)
            return [member.archive_name for member in members]
    except ArtifactBuildError:
        raise
    except ArtifactSecureIOError as error:
        raise ArtifactBuildError(str(error)) from error
    except (OSError, zipfile.BadZipFile) as error:
        raise ArtifactBuildError(
            f"{ARTIFACT_IO_FAILED}: private artifact build failed"
        ) from error


def main() -> None:
    """Run the standalone builder and report only its artifact summary."""

    args = parse_args()
    try:
        paths = build_main_artifact(
            args.plugin_root,
            args.output,
            allowlist_path=args.allowlist,
        )
    except ArtifactBuildError as error:
        raise SystemExit(f"artifact build rejected: {error}") from error
    print(f"Wrote {len(paths)} files to {args.output}")


if __name__ == "__main__":
    main()
