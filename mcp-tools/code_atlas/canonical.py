"""Canonical JSON encoding and immutable JSON-value helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any


FrozenJson = Any


def freeze_json(value: Any) -> FrozenJson:
    """Recursively make a JSON-like value immutable."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: FrozenJson) -> Any:
    """Return plain JSON containers at an explicit serialization boundary."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
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


def canonical_id(value: Any) -> str:
    """Return the canonical identifier for content-addressed Atlas data."""
    return canonical_hash(value)


_INTENT_SEPARATORS = re.compile(r"[^a-z0-9]+")


def normalize_intent_id(value: str) -> str:
    """Normalize a human intent label to a stable dotted identifier."""
    normalized = _INTENT_SEPARATORS.sub("-", value.strip().casefold()).strip("-")
    return normalized.replace("-", ".", 1) if "." not in normalized and "-" in normalized else normalized
