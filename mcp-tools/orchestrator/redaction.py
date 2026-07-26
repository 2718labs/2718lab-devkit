"""Shared bounded evidence redaction and deterministic content hashing."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_PEM_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----", re.DOTALL
)
_AUTH_HEADER = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|x-api-key|api-key|token)\s*:\s*)[^\r\n]+"
)
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key)\s*=\s*(['\"]?)[^\s'\"]+\2"
)


def canonical_json(value: Any) -> str:
    """Return canonical UTF-8 JSON text suitable for stable hashes."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Hash canonical JSON with SHA-256."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact_text(text: str, *, max_chars: int = 16_384) -> str:
    """Redact common credentials then cap stored evidence deterministically."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")

    redacted = _PEM_BLOCK.sub("[REDACTED PEM BLOCK]", text)
    redacted = _AUTH_HEADER.sub(r"\1[REDACTED]", redacted)
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\g<prefix>[REDACTED]@", redacted)
    redacted = _ASSIGNMENT_SECRET.sub(r"\1=[REDACTED]", redacted)
    if len(redacted) <= max_chars:
        return redacted
    marker = "[REDACTED TRUNCATED]"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return redacted[: max_chars - len(marker)] + marker
