"""Canonical JSON helpers shared by Relay plan and state records."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """Encode a JSON-compatible value with stable ordering and no whitespace."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    """Return the prefixed SHA-256 digest of canonical JSON."""

    return f"sha256:{hashlib.sha256(canonical_bytes(value)).hexdigest()}"
