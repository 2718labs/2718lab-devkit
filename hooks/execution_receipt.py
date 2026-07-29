#!/usr/bin/env python3
"""Fail-open PostToolUse capture of bounded Code Atlas execution receipts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_MCP_TOOLS = _PLUGIN_ROOT / "mcp-tools"
_MAX_STDIN_BYTES = 1_048_576


def _load_receipts() -> tuple[Any, Any]:
    if str(_MCP_TOOLS) not in sys.path:
        sys.path.insert(0, str(_MCP_TOOLS))
    from code_atlas.receipts import ReceiptRepository, normalize_post_tool_use

    return ReceiptRepository, normalize_post_tool_use


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


def main() -> int:
    """Capture trusted input when possible; every failure remains silent and zero."""

    try:
        payload = _read_payload()
        root = _resolve_data_root()
        if payload is None or root is None:
            return 0
        ReceiptRepository, normalize_post_tool_use = _load_receipts()
        if not normalize_post_tool_use(payload):
            return 0
        ReceiptRepository(root).capture(payload)
    except BaseException:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
