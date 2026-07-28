"""Shared validation boundary for paths, fragments, and adaptation slots."""

from __future__ import annotations

import ast
import keyword
import os
import re
from pathlib import Path

from .models import AtlasError

MAX_CHANGED_FILES = 8
MAX_TEMPLATE_BYTES = 65_536
MAX_RECIPE_BYTES = 262_144
MAX_PACKET_BYTES = 524_288
MAX_SLOT_COUNT = 32
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_GRAPH_DEPTH = 4
MAX_COMMAND_SPEC_BYTES = 4_096
GENERATED_COMPONENTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "vendor",
    "dist", "build", "__pycache__",
})

_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_ASSIGNMENT = re.compile(r"(?m)^\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*([^\s]+)")
_RAW_TOKEN = re.compile(r"(?i)(?:^|\s)(?:sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{8,})(?:$|\s)")
_BEARER_OR_PRIVATE_KEY = re.compile(
    r"(?ix)(?:\bauthorization\s*:\s*bearer\s+\S+|-----begin\s+[a-z\s]*private\s+key-----)"
)
_SENSITIVE_KEY_SUFFIXES = (
    "API_KEY", "ACCESS_TOKEN", "TOKEN", "PASSWORD", "PASSWD", "SECRET_ACCESS_KEY",
    "CLIENT_SECRET", "SECRET", "PRIVATE_KEY", "AUTHORIZATION",
)
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")
_QUALIFIED_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


def _contains_credential(text: str) -> bool:
    """Detect credentials without returning or retaining their values."""
    if _RAW_TOKEN.search(text) or _BEARER_OR_PRIVATE_KEY.search(text):
        return True
    for match in _ASSIGNMENT.finditer(text):
        key = match.group(1).upper()
        if any(key == suffix or key.endswith(f"_{suffix}") for suffix in _SENSITIVE_KEY_SUFFIXES):
            return True
    return False


def validate_candidate_path(path: str | os.PathLike[str], workspace: str | os.PathLike[str] | None = None) -> str:
    """Validate and return a normalized, non-generated relative candidate path."""
    raw = os.fspath(path)
    if not raw or raw.strip() != raw or raw.startswith(("/", "\\")) or _DRIVE_PATH.match(raw):
        raise AtlasError("unsafe_path")
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise AtlasError("unsafe_path")
    if any(part.casefold() in GENERATED_COMPONENTS for part in parts):
        raise AtlasError("generated_path")
    filename = parts[-1]
    lowered_filename = filename.casefold()
    if ".generated." in lowered_filename or lowered_filename.endswith("_pb2.py"):
        raise AtlasError("generated_path")
    if workspace is not None:
        root = Path(workspace).resolve()
        candidate = root.joinpath(*parts)
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise AtlasError("unsafe_path") from exc
        cursor = root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise AtlasError("symlink_path")
            try:
                stat_result = cursor.lstat()
            except OSError:
                continue
            if getattr(stat_result, "st_file_attributes", 0) & 0x400:
                raise AtlasError("reparse_path")
            is_junction = getattr(os.path, "isjunction", None)
            if callable(is_junction) and is_junction(cursor):
                raise AtlasError("reparse_path")
    return "/".join(parts)


def validate_fragment(fragment: str | bytes, *, max_bytes: int = MAX_TEMPLATE_BYTES) -> str:
    """Return safe UTF-8 text, rejecting binary, oversize, or secret-bearing input."""
    if isinstance(fragment, bytes):
        if len(fragment) > max_bytes or b"\0" in fragment:
            raise AtlasError("unsafe_fragment")
        try:
            text = fragment.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AtlasError("invalid_utf8") from exc
    elif isinstance(fragment, str):
        if len(fragment.encode("utf-8")) > max_bytes or "\0" in fragment:
            raise AtlasError("unsafe_fragment")
        text = fragment
    else:
        raise AtlasError("invalid_fragment")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise AtlasError("unsafe_fragment")
    if _contains_credential(text):
        raise AtlasError("credential_detected")
    return text


def validate_slot_value(slot_type: str, value: str) -> str:
    """Validate a value against a constrained adaptation-slot type."""
    if not isinstance(value, str):
        raise AtlasError("invalid_slot_value")
    if slot_type == "relative_python_path":
        result = validate_candidate_path(value)
        if not result.endswith(".py"):
            raise AtlasError("invalid_slot_value")
        return result
    if slot_type == "python_identifier":
        if not _IDENTIFIER.fullmatch(value) or keyword.iskeyword(value):
            raise AtlasError("invalid_slot_value")
        return value
    if slot_type == "python_qualified_name":
        if not _QUALIFIED_NAME.fullmatch(value) or any(keyword.iskeyword(part) for part in value.split(".")):
            raise AtlasError("invalid_slot_value")
        return value
    if slot_type == "python_expression":
        try:
            ast.parse(value, mode="eval")
        except SyntaxError as exc:
            raise AtlasError("invalid_slot_value") from exc
        return value
    if slot_type == "python_statement_block":
        try:
            tree = ast.parse(value, mode="exec")
            compile(tree, "<slot>", "exec")
        except SyntaxError as exc:
            raise AtlasError("invalid_slot_value") from exc
        return value
    if slot_type == "single_line_text":
        if "\n" in value or "\r" in value:
            raise AtlasError("invalid_slot_value")
        return value
    raise AtlasError("unknown_slot_type")
