"""Canonical JSON encoding and immutable JSON-value helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


FrozenJson = Any


class FrozenObject(tuple[tuple[str, FrozenJson], ...]):
    """Tuple-backed immutable JSON object, distinct from JSON arrays."""


class FrozenArray(tuple[FrozenJson, ...]):
    """Tuple-backed immutable JSON array, distinct from JSON objects."""


def _pairs_are_object(value: tuple[Any, ...]) -> bool:
    return all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value)


def freeze_json(value: Any) -> FrozenJson:
    """Recursively make a JSON-like value immutable."""
    if isinstance(value, Mapping):
        return FrozenObject(sorted(((str(key), freeze_json(item)) for key, item in value.items()), key=lambda item: item[0]))
    if isinstance(value, tuple) and _pairs_are_object(value):
        return FrozenObject(sorted(((key, freeze_json(item)) for key, item in value), key=lambda item: item[0]))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return FrozenArray(freeze_json(item) for item in value)
    return value


def thaw_json(value: FrozenJson) -> Any:
    """Return plain JSON containers at an explicit serialization boundary."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, FrozenObject):
        return {key: thaw_json(item) for key, item in value}
    if isinstance(value, FrozenArray):
        return [thaw_json(item) for item in value]
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Encode JSON deterministically and reject non-finite numeric values."""
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    """Return the SHA-256 hash of canonical UTF-8 JSON."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonical_id(
    kind: str,
    payload: Any,
    *,
    schema_version: str = "1",
    extractor_id: str = "",
    extractor_version: str = "",
    provenance: str = "declared",
    source_hashes: tuple[str, ...] | list[str] = (),
) -> str:
    """Hash exactly the immutable node identity fields."""
    return canonical_hash({
        "kind": kind,
        "schema_version": schema_version,
        "extractor_id": extractor_id,
        "extractor_version": extractor_version,
        "provenance": provenance,
        "payload": payload,
        "source_hashes": list(source_hashes),
    })


_INTENT_SEPARATORS = re.compile(r"[^a-z0-9.]+")


def normalize_intent_id(value: str) -> str:
    """Normalize a human intent label to a stable dotted identifier."""
    normalized = _INTENT_SEPARATORS.sub("-", value.strip().casefold())
    return re.sub(r"\.{2,}", ".", normalized).strip("-.")
