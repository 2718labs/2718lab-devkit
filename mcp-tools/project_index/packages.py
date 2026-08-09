"""Pure discovery of snapshot-bound package manifest descriptors."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from .extractors import SourceFile
from .models import CoverageGap, PackageDescriptor

_PACKAGE_DESCRIPTOR_FORMAT = "project-index-package-v1"
_MANIFEST_SPECS = {
    "pyproject.toml": ("python", "toml"),
    "package.json": ("node", "json"),
    "Cargo.toml": ("cargo", "toml"),
}


def discover_packages(
    workspace_id: str, files: Sequence[SourceFile]
) -> tuple[tuple[PackageDescriptor, ...], tuple[CoverageGap, ...]]:
    """Return deterministic descriptors and explicit gaps for known manifests.

    This deliberately has no package-manager workspace or dependency semantics:
    a descriptor is only one manifest's declared package boundary.
    """

    descriptors: list[PackageDescriptor] = []
    gaps: list[CoverageGap] = []
    for source in sorted(files, key=lambda item: item.path):
        spec = _MANIFEST_SPECS.get(PurePosixPath(source.path).name)
        if spec is None:
            continue
        ecosystem, parser = spec
        if source.text is None:
            gaps.append(
                CoverageGap(
                    source.path,
                    "PACKAGE_MANIFEST_INVALID",
                    "package manifest is not valid UTF-8 text",
                )
            )
            continue
        value = _parse_manifest(source.path, source.text, parser, gaps)
        if value is None:
            continue
        name = _package_name(source.path, ecosystem, value, gaps)
        if name is None:
            continue
        root_path = _root_path(source.path)
        manifest_hash = source.content_hash
        descriptors.append(
            PackageDescriptor(
                package_id=_package_id(
                    workspace_id,
                    ecosystem,
                    name,
                    root_path,
                    source.path,
                    manifest_hash,
                ),
                ecosystem=ecosystem,
                name=name,
                root_path=root_path,
                manifest_path=source.path,
                manifest_hash=manifest_hash,
            )
        )
    return (
        tuple(
            sorted(descriptors, key=package_descriptor_sort_key)
        ),
        tuple(sorted(set(gaps), key=lambda item: (item.path, item.code, item.message))),
    )


def package_descriptor_sort_key(
    descriptor: PackageDescriptor,
) -> tuple[str, str, str, str]:
    """Return the canonical snapshot ordering for package descriptors."""

    return (
        descriptor.root_path,
        descriptor.manifest_path,
        descriptor.ecosystem,
        descriptor.package_id,
    )


def _parse_manifest(
    path: str, text: str, parser: str, gaps: list[CoverageGap]
) -> Mapping[str, Any] | None:
    try:
        value = json.loads(text) if parser == "json" else tomllib.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError):
        gaps.append(
            CoverageGap(
                path,
                "PACKAGE_MANIFEST_INVALID",
                "package manifest could not be parsed",
            )
        )
        return None
    if not isinstance(value, Mapping):
        gaps.append(
            CoverageGap(
                path,
                "PACKAGE_MANIFEST_INVALID",
                "package manifest must be a mapping",
            )
        )
        return None
    return value


def _package_name(
    path: str,
    ecosystem: str,
    value: Mapping[str, Any],
    gaps: list[CoverageGap],
) -> str | None:
    if ecosystem == "python":
        declared = value.get("project")
    elif ecosystem == "cargo":
        declared = value.get("package")
    else:
        declared = value
    if not isinstance(declared, Mapping):
        gaps.append(
            CoverageGap(
                path,
                "PACKAGE_MANIFEST_UNSUPPORTED",
                "package manifest does not declare a supported package table",
            )
        )
        return None
    raw_name = declared.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        gaps.append(
            CoverageGap(
                path,
                "PACKAGE_MANIFEST_UNSUPPORTED",
                "package manifest does not declare a package name",
            )
        )
        return None
    return raw_name.strip()


def _root_path(manifest_path: str) -> str:
    parent = PurePosixPath(manifest_path).parent.as_posix()
    return "" if parent == "." else parent


def _package_id(
    workspace_id: str,
    ecosystem: str,
    name: str,
    root_path: str,
    manifest_path: str,
    manifest_hash: str,
) -> str:
    payload = json.dumps(
        {
            "ecosystem": ecosystem,
            "format": _PACKAGE_DESCRIPTOR_FORMAT,
            "manifest_hash": manifest_hash,
            "manifest_path": manifest_path,
            "name": name,
            "root_path": root_path,
            "workspace_id": workspace_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
