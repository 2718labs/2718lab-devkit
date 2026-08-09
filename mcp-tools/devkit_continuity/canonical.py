"""Strict canonical JSON and domain-separated Continuity identities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

_HASH_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MANIFEST_SCHEMA_V1 = "continuity-frozen-view/v1"
_MANIFEST_SCHEMA_V2 = "continuity-frozen-view/v2"
_KEY_DOMAIN = "continuity-key/v1"
_MANIFEST_DOMAIN = "continuity-manifest/v1"
_CAS_ROOT_DOMAIN = "continuity-cas-root/v1"
_VIEW_DOMAIN = "continuity-view/v1"
_RECEIPT_DOMAIN = "continuity-receipt/v1"


def is_hash_id(value: object) -> bool:
    """Return whether value is the only supported content-address identifier."""
    return isinstance(value, str) and _HASH_ID.fullmatch(value) is not None


def canonical_json(value: Any) -> str:
    """Encode a strictly JSON-compatible value as canonical UTF-8 text."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    """Return the SHA-256 identity of canonical UTF-8 JSON."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def manifest_identity(manifest: Mapping[str, Any]) -> str:
    """Return the domain-separated identity of a frozen-view manifest."""
    return _domain_identity(_MANIFEST_DOMAIN, manifest)


def key_identity(key: Any) -> str:
    """Return the domain-separated identity of a Continuity key."""
    return _domain_identity(_KEY_DOMAIN, _key_value(key))


def cas_root_identity(entries: Sequence[Any]) -> str:
    """Return the domain-separated CAS root for already ordered entries."""
    return _domain_identity(
        _CAS_ROOT_DOMAIN,
        [
            _entry_value(entry)
            for entry in entries
        ],
    )


def view_identity(manifest_hash: str, cas_root_hash: str) -> str:
    """Return the domain-separated identity of a frozen view."""
    _require_hash(manifest_hash)
    _require_hash(cas_root_hash)
    return _domain_identity(
        _VIEW_DOMAIN,
        {"manifest_hash": manifest_hash, "cas_root_hash": cas_root_hash},
    )


def receipt_identity(key: Any, view_id: str, kind: str) -> str:
    """Return the domain-separated identity of a Continuity receipt."""
    _require_hash(view_id)
    _require_identifier(kind)
    return _domain_identity(
        _RECEIPT_DOMAIN,
        {"key": _key_value(key), "view_id": view_id, "kind": kind},
    )


def canonical_frozen_view_manifest(
    key: Any,
    entries: Sequence[Any],
    *,
    input_snapshot_ids: Sequence[str] = (),
    output_snapshot_ids: Sequence[str] = (),
    checkpoint_ids: Sequence[str] = (),
    query_ids: Sequence[str] = (),
    verification_artifact_hashes: Sequence[str] = (),
    execution_receipt_ids: Sequence[str] = (),
    request_hash: str | None = None,
    evidence_hash: str | None = None,
    changed_nodes: Sequence[Any] = (),
    coverage_gaps: Sequence[Any] = (),
    execution_receipts: Sequence[Any] = (),
    replay_metadata: Any | None = None,
) -> dict[str, Any]:
    """Build the complete ordered frozen-view manifest payload.

    Metadata-free views remain byte-for-byte compatible with the historical v1
    format. A typed replay context creates the explicit v2 format instead.
    """
    entry_values = tuple(_entry_value(entry) for entry in entries)
    if tuple(sorted(entry_values, key=_entry_sort_key)) != entry_values:
        raise ValueError("ENTRIES_NOT_CANONICAL")
    if len({(item["role"], item["path"]) for item in entry_values}) != len(
        entry_values
    ):
        raise ValueError("ENTRIES_DUPLICATE")
    artifact_hashes = tuple(verification_artifact_hashes)
    if any(not is_hash_id(item) for item in artifact_hashes):
        raise ValueError("HASH_ID_INVALID")
    _require_optional_hash(request_hash)
    _require_optional_hash(evidence_hash)
    manifest = {
        "schema": _MANIFEST_SCHEMA_V1 if replay_metadata is None else _MANIFEST_SCHEMA_V2,
        "key": _key_value(key),
        "entries": list(entry_values),
        "input_snapshot_ids": _identifier_list(input_snapshot_ids),
        "output_snapshot_ids": _identifier_list(output_snapshot_ids),
        "checkpoint_ids": _identifier_list(checkpoint_ids),
        "query_ids": _identifier_list(query_ids),
        "verification_artifact_hashes": list(artifact_hashes),
        "execution_receipt_ids": _identifier_list(execution_receipt_ids),
        "request_hash": request_hash,
        "evidence_hash": evidence_hash,
        "changed_nodes": _metadata_values(changed_nodes, "CHANGED_NODE_INVALID"),
        "coverage_gaps": _metadata_values(coverage_gaps, "COVERAGE_GAP_INVALID"),
        "execution_receipts": _metadata_values(
            execution_receipts, "EXECUTION_RECEIPT_INVALID"
        ),
    }
    if replay_metadata is not None:
        manifest["replay_metadata"] = _metadata_value(
            replay_metadata, "REPLAY_METADATA_INVALID"
        )
    return manifest


def _domain_identity(domain: str, payload: Any) -> str:
    return canonical_hash({"domain": domain, "payload": payload})


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            _require_identifier(value, allow_empty=True)
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("SCALAR_INVALID")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("OBJECT_KEY_INVALID")
            _require_identifier(key, allow_empty=True)
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    raise ValueError("SCALAR_INVALID")


def _key_value(key: Any) -> dict[str, Any]:
    to_dict = getattr(key, "to_dict", None)
    if not callable(to_dict):
        raise ValueError("KEY_INVALID")
    value = to_dict()
    if not isinstance(value, dict):
        raise ValueError("KEY_INVALID")
    return value


def _entry_value(entry: Any) -> dict[str, Any]:
    to_dict = getattr(entry, "to_dict", None)
    if not callable(to_dict):
        raise ValueError("ENTRY_INVALID")
    value = to_dict()
    if not isinstance(value, dict):
        raise ValueError("ENTRY_INVALID")
    return value


def _metadata_values(values: Sequence[Any], code: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            raise ValueError(code)
        item = to_dict()
        if not isinstance(item, dict):
            raise ValueError(code)
        result.append(item)
    return result


def _metadata_value(value: Any, code: str) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        raise ValueError(code)
    item = to_dict()
    if not isinstance(item, dict):
        raise ValueError(code)
    return item


def _entry_sort_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return entry["role"], entry["path"], entry["content_hash"]


def _identifier_list(values: Sequence[str]) -> list[str]:
    result = list(values)
    for value in result:
        _require_identifier(value)
    return result


def _require_hash(value: object) -> None:
    if not is_hash_id(value):
        raise ValueError("HASH_ID_INVALID")


def _require_optional_hash(value: object) -> None:
    if value is not None:
        _require_hash(value)


def _require_identifier(value: object, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("SCALAR_INVALID")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("SCALAR_INVALID")
