#!/usr/bin/env python3
"""Fail-open PostToolUse capture of bounded Code Atlas execution receipts.

The installed PostToolUse adapter is the trust boundary.  It constructs an
in-process capture context from explicit host envelope fields and local time;
payload ``trusted`` flags, serialized contexts, and evidence keys are ignored.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_MCP_TOOLS = _PLUGIN_ROOT / "mcp-tools"
_MAX_STDIN_BYTES = 1_048_576


def _load_receipts() -> tuple[Any, Any]:
    if str(_MCP_TOOLS) not in sys.path:
        sys.path.insert(0, str(_MCP_TOOLS))
    from code_atlas.receipts import HostCaptureContext, ReceiptRepository

    return HostCaptureContext, ReceiptRepository


def _resolve_data_root(environ: Mapping[str, str] | None = None) -> Path | None:
    values = os.environ if environ is None else environ
    candidate: Path | None = None
    if values.get("BUGKILLER_HOME"):
        candidate = Path(values["BUGKILLER_HOME"])
    elif values.get("PLUGIN_DATA"):
        candidate = Path(values["PLUGIN_DATA"])
    elif values.get("CODEX_HOME"):
        candidate = Path(values["CODEX_HOME"]) / "bugkiller"
    if candidate is None or not candidate.is_absolute():
        return None
    try:
        root = candidate.expanduser().resolve()
        plugin_root = _PLUGIN_ROOT.resolve()
    except (OSError, RuntimeError):
        return None
    if root == plugin_root or plugin_root in root.parents:
        return None
    folded = tuple(part.casefold() for part in root.parts)
    if any(
        folded[index : index + 2] == ("plugins", "cache")
        for index in range(len(folded) - 1)
    ):
        return None
    return root


def _read_payload() -> Mapping[str, Any] | None:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _capture_context(payload: Mapping[str, Any], context_type: Any) -> Any | None:
    raw_host = payload.get("host")
    if not isinstance(raw_host, str):
        return None
    normalized_host = "".join(
        character for character in raw_host.casefold() if character.isalnum()
    )
    host = {
        "claude": "claude",
        "claudecode": "claude",
        "codex": "codex",
        "openaicodex": "codex",
    }.get(normalized_host)
    if host is None:
        return None
    nested = payload.get("context")
    sources = (payload, nested) if isinstance(nested, Mapping) else (payload,)
    session_id = _first_string(sources, ("session_id", "sessionId"))
    turn_id = _first_string(sources, ("turn_id", "turnId"))
    workspace = _first_string(
        sources,
        ("workspace", "cwd", "working_directory", "workingDirectory"),
    )
    if session_id is None or turn_id is None or workspace is None:
        return None
    observed_at = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    return context_type(
        host=host,
        session_id=session_id,
        turn_id=turn_id,
        workspace=workspace,
        observed_at=observed_at,
    )


def _first_string(
    sources: tuple[Mapping[str, Any], ...], keys: tuple[str, ...]
) -> str | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value and "\x00" not in value:
                return value
    return None


def main() -> int:
    """Capture trusted input when possible; every failure remains silent and zero."""

    try:
        payload = _read_payload()
        root = _resolve_data_root()
        if payload is None or root is None:
            return 0
        HostCaptureContext, ReceiptRepository = _load_receipts()
        capture_context = _capture_context(payload, HostCaptureContext)
        if capture_context is None:
            return 0
        ReceiptRepository(root).capture(payload, capture_context=capture_context)
    except BaseException:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
