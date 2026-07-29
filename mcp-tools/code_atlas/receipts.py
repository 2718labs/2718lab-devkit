"""Bounded, immutable execution receipts captured from trusted host hooks.

This module only normalizes already-observed JSON facts.  It never executes a
command, applies a patch, or reads from a target workspace.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import canonical_hash, canonical_json
from .security import MAX_COMMAND_SPEC_BYTES


RAW_RECEIPT_SCHEMA_VERSION = "1"
MAX_CONTEXT_BYTES = 4_096
MAX_IDENTIFIER_BYTES = 256
MAX_OBSERVED_AT_BYTES = 64
MAX_NESTED_RECEIPT_DEPTH = 4
MAX_RECEIPTS_PER_PAYLOAD = 32

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_OBSERVED_AT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_RAW_TOKEN = re.compile(
    r"(?i)(?<![a-z0-9])(?:sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{8,})(?![a-z0-9])"
)
_PEM = re.compile(r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----", re.DOTALL)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s'\"]+")
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE
)
_ASSIGNMENT = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9_])
    (?P<prefix>(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*)
    (?P<value>
        "(?:[^"\\]|\\.)*"
        | '(?:[^'\\]|\\.)*'
        | [^\s;&|]+
    )
    """
)
_SENSITIVE_OPTION = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_-])
    (?:
        --(?:
            api[-_]?key|access[-_]?token|token|password|passwd|secret|
            client[-_]?secret|private[-_]?key|authorization|auth|cookie|user
        )
        | -(?:p|u)
    )
    (?:\s+|=)
    """
)
_SENSITIVE_KEY_VALUE = re.compile(
    r"""(?ix)
    ["']?
    (?:
        (?:x[-_]?)?api[-_]?key|access[-_]?token|token|password|passwd|
        secret(?:[-_]?key)?|client[-_]?secret|private[-_]?key|authorization|
        auth|cookie
    )
    ["']?
    \s*[:=]\s*
    """
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z]:[\\/][^\s'\";&|]*")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/(?:[^\s'\";&|]+(?:/[^\s'\";&|]+)*)?"
)

_DIRECT_TOOLS = {
    "bash": ("claude", "shell"),
    "shell_command": ("codex", "shell"),
    "edit": ("claude", "patch"),
    "write": ("claude", "patch"),
    "apply_patch": ("codex", "patch"),
}
_WRAPPER_TOOLS = frozenset({"code", "exec"})
_RECEIPT_FIELDS = (
    "receipt_id",
    "schema_version",
    "host",
    "session_id_hash",
    "turn_id_hash",
    "tool_use_id",
    "parent_tool_use_id",
    "canonical_tool",
    "command_spec",
    "command_spec_hash",
    "input_hash",
    "exit_code",
    "success",
    "output_hash",
    "workspace_hash",
    "observed_at",
)
_MISSING = object()


class ReceiptError(ValueError):
    """Base error for local receipt persistence failures."""


class ReceiptIntegrityError(ReceiptError):
    """A receipt file or in-memory record is not content-addressed canonical data."""


class ReceiptConflictError(ReceiptError):
    """The same receipt id names non-equivalent receipt content."""


@dataclass(frozen=True, slots=True)
class RawExecutionReceipt(Mapping[str, Any]):
    """A privacy-safe observation of one native command or patch tool result."""

    receipt_id: str
    schema_version: str
    host: str
    session_id_hash: str
    turn_id_hash: str
    tool_use_id: str
    parent_tool_use_id: str
    canonical_tool: str
    command_spec: tuple[str, ...]
    command_spec_hash: str
    input_hash: str
    exit_code: int
    success: bool
    output_hash: str
    workspace_hash: str
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_spec", tuple(self.command_spec))

    def to_dict(self) -> dict[str, Any]:
        """Return the exact canonical JSON fields persisted by the repository."""

        return {
            "receipt_id": self.receipt_id,
            "schema_version": self.schema_version,
            "host": self.host,
            "session_id_hash": self.session_id_hash,
            "turn_id_hash": self.turn_id_hash,
            "tool_use_id": self.tool_use_id,
            "parent_tool_use_id": self.parent_tool_use_id,
            "canonical_tool": self.canonical_tool,
            "command_spec": list(self.command_spec),
            "command_spec_hash": self.command_spec_hash,
            "input_hash": self.input_hash,
            "exit_code": self.exit_code,
            "success": self.success,
            "output_hash": self.output_hash,
            "workspace_hash": self.workspace_hash,
            "observed_at": self.observed_at,
        }

    def __getitem__(self, key: str) -> Any:
        if key not in _RECEIPT_FIELDS:
            raise KeyError(key)
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(_RECEIPT_FIELDS)

    def __len__(self) -> int:
        return len(_RECEIPT_FIELDS)

    def identity_projection(self) -> dict[str, Any]:
        """Return content-addressed fields; observation time is intentionally absent."""

        return {
            "schema_version": self.schema_version,
            "host": self.host,
            "session_id_hash": self.session_id_hash,
            "turn_id_hash": self.turn_id_hash,
            "tool_use_id": self.tool_use_id,
            "parent_tool_use_id": self.parent_tool_use_id,
            "canonical_tool": self.canonical_tool,
            "command_spec": list(self.command_spec),
            "command_spec_hash": self.command_spec_hash,
            "input_hash": self.input_hash,
            "exit_code": self.exit_code,
            "success": self.success,
            "output_hash": self.output_hash,
            "workspace_hash": self.workspace_hash,
        }

    def semantic_projection(self) -> dict[str, Any]:
        """Return host-independent wrapper-free evidence used for equivalence checks."""

        return {
            "schema_version": self.schema_version,
            "host": self.host,
            "session_id_hash": self.session_id_hash,
            "turn_id_hash": self.turn_id_hash,
            "canonical_tool": self.canonical_tool,
            "command_spec": list(self.command_spec),
            "command_spec_hash": self.command_spec_hash,
            "input_hash": self.input_hash,
            "exit_code": self.exit_code,
            "success": self.success,
            "output_hash": self.output_hash,
            "workspace_hash": self.workspace_hash,
        }

    def expected_receipt_id(self) -> str:
        """Return the content address, deliberately excluding ``observed_at``."""

        return canonical_hash(
            {
                "kind": "raw_execution_receipt",
                "schema_version": self.schema_version,
                "receipt": self.identity_projection(),
            }
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RawExecutionReceipt:
        """Decode one exact persisted record and verify its content address."""

        if set(value) != set(_RECEIPT_FIELDS):
            raise ReceiptIntegrityError("receipt_fields_invalid")
        command_spec = value.get("command_spec")
        if not isinstance(command_spec, list):
            raise ReceiptIntegrityError("receipt_fields_invalid")
        receipt = cls(
            receipt_id=value["receipt_id"],
            schema_version=value["schema_version"],
            host=value["host"],
            session_id_hash=value["session_id_hash"],
            turn_id_hash=value["turn_id_hash"],
            tool_use_id=value["tool_use_id"],
            parent_tool_use_id=value["parent_tool_use_id"],
            canonical_tool=value["canonical_tool"],
            command_spec=tuple(command_spec),
            command_spec_hash=value["command_spec_hash"],
            input_hash=value["input_hash"],
            exit_code=value["exit_code"],
            success=value["success"],
            output_hash=value["output_hash"],
            workspace_hash=value["workspace_hash"],
            observed_at=value["observed_at"],
        )
        _validate_receipt(receipt)
        return receipt


def normalize_post_tool_use(
    payload: Mapping[str, Any] | object,
) -> tuple[RawExecutionReceipt, ...]:
    """Normalize one trusted direct or nested PostToolUse payload.

    Invalid, untrusted, unsupported, or over-depth payloads deliberately return
    no evidence.  This preserves a fail-open hook while never manufacturing a
    successful execution fact.
    """

    if not isinstance(payload, Mapping) or not _is_trusted(payload):
        return ()
    try:
        context = _context(payload)
        if context is None:
            return ()
        observed_at = _observed_at(payload)
        if observed_at is None:
            return ()
        explicit_host = _host(payload)
        if _has_host(payload) and explicit_host is None:
            return ()
        state = _NormalizationState()
        _visit(
            payload,
            context=context,
            observed_at=observed_at,
            inherited_host=explicit_host,
            inherited_parent="",
            depth=0,
            state=state,
        )
        if state.invalid:
            return ()
        return tuple(state.receipts)
    except (TypeError, ValueError, UnicodeError):
        return ()


class ReceiptRepository:
    """Immutable local storage for canonical ``RawExecutionReceipt`` records."""

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)

    @property
    def receipt_root(self) -> Path:
        """Return the sole durable directory used by this repository."""

        return self.data_root / "code-atlas-receipts" / "sha256"

    def capture(
        self, payload: Mapping[str, Any] | object
    ) -> tuple[RawExecutionReceipt, ...]:
        """Normalize and immutably persist all supported records from one payload."""

        try:
            normalized = normalize_post_tool_use(payload)
        except (TypeError, ValueError, UnicodeError):
            return ()
        stored: list[RawExecutionReceipt] = []
        for receipt in normalized:
            stored.append(self.store(receipt))
        return tuple(stored)

    def store(self, receipt: RawExecutionReceipt) -> RawExecutionReceipt:
        """Persist one record once, deduplicating only identical identity content."""

        if not isinstance(receipt, RawExecutionReceipt):
            raise ReceiptIntegrityError("receipt_type_invalid")
        path = self._path_for(receipt.receipt_id)
        if path.exists():
            existing = self.read(receipt.receipt_id)
            if existing.identity_projection() == receipt.identity_projection():
                return existing
            raise ReceiptConflictError("receipt_conflict")
        _validate_receipt(receipt)
        body = canonical_json(receipt.to_dict())
        self.receipt_root.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(body)
        except FileExistsError:
            existing = self.read(receipt.receipt_id)
            if existing.identity_projection() == receipt.identity_projection():
                return existing
            raise ReceiptConflictError("receipt_conflict")
        return receipt

    write = store

    def read(self, receipt_id: str) -> RawExecutionReceipt:
        """Read one receipt only if its bytes, schema, and content address verify."""

        path = self._path_for(receipt_id)
        try:
            raw = path.read_text(encoding="utf-8")
            decoded = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReceiptIntegrityError("receipt_read_invalid") from error
        try:
            if not isinstance(decoded, Mapping) or canonical_json(decoded) != raw:
                raise ReceiptIntegrityError("receipt_canonical_invalid")
            receipt = RawExecutionReceipt.from_dict(decoded)
            if receipt.receipt_id != receipt_id:
                raise ReceiptIntegrityError("receipt_id_invalid")
            return receipt
        except ReceiptIntegrityError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeError) as error:
            raise ReceiptIntegrityError("receipt_content_invalid") from error

    def _path_for(self, receipt_id: str) -> Path:
        if not isinstance(receipt_id, str) or not _HASH.fullmatch(receipt_id):
            raise ReceiptIntegrityError("receipt_id_invalid")
        return self.receipt_root / f"{receipt_id[7:]}.json"


@dataclass(slots=True)
class _NormalizationState:
    receipts: list[RawExecutionReceipt]
    invalid: bool

    def __init__(self) -> None:
        self.receipts = []
        self.invalid = False


def _visit(
    node: Mapping[str, Any],
    *,
    context: tuple[str, str, str],
    observed_at: str,
    inherited_host: str | None,
    inherited_parent: str,
    depth: int,
    state: _NormalizationState,
) -> None:
    if state.invalid or depth > MAX_NESTED_RECEIPT_DEPTH:
        state.invalid = True
        return
    name = _tool_name(node)
    if name is None:
        state.invalid = True
        return
    if name in _DIRECT_TOOLS:
        expected_host, canonical_tool = _DIRECT_TOOLS[name]
        local_host = _host(node)
        if _has_host(node) and local_host is None:
            state.invalid = True
            return
        if (
            local_host is not None
            and inherited_host is not None
            and local_host != inherited_host
        ):
            state.invalid = True
            return
        host = local_host or inherited_host or expected_host
        if host != expected_host:
            state.invalid = True
            return
        receipt = _normalize_direct(
            node,
            context=context,
            observed_at=observed_at,
            host=host,
            canonical_tool=canonical_tool,
            inherited_parent=inherited_parent,
        )
        if receipt is None:
            state.invalid = True
            return
        state.receipts.append(receipt)
        if len(state.receipts) > MAX_RECEIPTS_PER_PAYLOAD:
            state.invalid = True
        return
    if name not in _WRAPPER_TOOLS:
        state.invalid = True
        return
    children = _children(node)
    if children is None:
        state.invalid = True
        return
    wrapper_id = _tool_id(node, required=False)
    if wrapper_id is None:
        state.invalid = True
        return
    parent = wrapper_id or inherited_parent
    for child in children:
        _visit(
            child,
            context=context,
            observed_at=observed_at,
            inherited_host=inherited_host,
            inherited_parent=parent,
            depth=depth + 1,
            state=state,
        )


def _normalize_direct(
    node: Mapping[str, Any],
    *,
    context: tuple[str, str, str],
    observed_at: str,
    host: str,
    canonical_tool: str,
    inherited_parent: str,
) -> RawExecutionReceipt | None:
    tool_use_id = _tool_id(node, required=True)
    parent_tool_use_id = _parent_tool_id(node, fallback=inherited_parent)
    tool_input = _tool_input(node)
    response = _tool_response(node)
    status = _execution_status(node, response)
    if (
        tool_use_id is None
        or parent_tool_use_id is None
        or tool_input is None
        or response is _MISSING
        or status is None
    ):
        return None
    if canonical_tool == "shell":
        command = _command(tool_input)
        if command is None:
            return None
        command_spec = _bounded_command_spec(command)
    else:
        command_spec = ()
    try:
        session_id, turn_id, workspace = context
        exit_code, success = status
        provisional = RawExecutionReceipt(
            receipt_id="",
            schema_version=RAW_RECEIPT_SCHEMA_VERSION,
            host=host,
            session_id_hash=_context_hash(session_id),
            turn_id_hash=_context_hash(turn_id),
            tool_use_id=tool_use_id,
            parent_tool_use_id=parent_tool_use_id,
            canonical_tool=canonical_tool,
            command_spec=command_spec,
            command_spec_hash=canonical_hash(command_spec),
            input_hash=canonical_hash(tool_input),
            exit_code=exit_code,
            success=success,
            output_hash=canonical_hash(response),
            workspace_hash=_context_hash(workspace),
            observed_at=observed_at,
        )
        return RawExecutionReceipt(
            receipt_id=provisional.expected_receipt_id(),
            schema_version=provisional.schema_version,
            host=provisional.host,
            session_id_hash=provisional.session_id_hash,
            turn_id_hash=provisional.turn_id_hash,
            tool_use_id=provisional.tool_use_id,
            parent_tool_use_id=provisional.parent_tool_use_id,
            canonical_tool=provisional.canonical_tool,
            command_spec=provisional.command_spec,
            command_spec_hash=provisional.command_spec_hash,
            input_hash=provisional.input_hash,
            exit_code=provisional.exit_code,
            success=provisional.success,
            output_hash=provisional.output_hash,
            workspace_hash=provisional.workspace_hash,
            observed_at=provisional.observed_at,
        )
    except (TypeError, ValueError, UnicodeError):
        return None


def _is_trusted(payload: Mapping[str, Any]) -> bool:
    context = payload.get("context")
    return (
        payload.get("plugin_trusted") is True
        or payload.get("trusted") is True
        or isinstance(context, Mapping)
        and context.get("trusted") is True
    )


def _context(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    context = payload.get("context", _MISSING)
    if context is not _MISSING and not isinstance(context, Mapping):
        return None
    sources: tuple[Mapping[str, Any], ...] = (
        (payload, context) if isinstance(context, Mapping) else (payload,)
    )
    session_id = _first_text(sources, ("session_id", "sessionId"), MAX_CONTEXT_BYTES)
    turn_id = _first_text(sources, ("turn_id", "turnId"), MAX_CONTEXT_BYTES)
    workspace = _first_text(
        sources,
        ("workspace", "cwd", "working_directory", "workingDirectory"),
        MAX_CONTEXT_BYTES,
    )
    if session_id is None or turn_id is None or workspace is None:
        return None
    return session_id, turn_id, workspace


def _observed_at(payload: Mapping[str, Any]) -> str | None:
    value = _first_text(
        (payload,), ("observed_at", "observedAt", "timestamp"), MAX_OBSERVED_AT_BYTES
    )
    if value is None:
        return (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
    return value if _valid_observed_at(value) else None


def _has_host(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in ("host", "host_name", "provider", "client"))


def _host(payload: Mapping[str, Any]) -> str | None:
    value = _first_text(
        (payload,), ("host", "host_name", "provider", "client"), MAX_IDENTIFIER_BYTES
    )
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    if normalized in {"claude", "claudecode"}:
        return "claude"
    if normalized in {"codex", "openaicodex"}:
        return "codex"
    return None


def _tool_name(node: Mapping[str, Any]) -> str | None:
    value = _first_text(
        (node,), ("tool_name", "toolName", "name"), MAX_IDENTIFIER_BYTES
    )
    if value is None:
        tool = node.get("tool")
        if isinstance(tool, Mapping):
            value = _first_text(
                (tool,), ("name", "tool_name", "toolName"), MAX_IDENTIFIER_BYTES
            )
        elif isinstance(tool, str):
            value = _bounded_text(tool, MAX_IDENTIFIER_BYTES)
    if value is None:
        return None
    return value.casefold()


def _tool_id(node: Mapping[str, Any], *, required: bool) -> str | None:
    found = _first_text(
        (node,),
        ("tool_use_id", "toolUseId", "tool_id", "toolId", "call_id", "id"),
        MAX_IDENTIFIER_BYTES,
    )
    if found is None:
        return None if required else ""
    return found if _valid_tool_id(found) else None


def _parent_tool_id(node: Mapping[str, Any], *, fallback: str) -> str | None:
    found = _first_text(
        (node,),
        ("parent_tool_use_id", "parentToolUseId", "parent_tool_id", "parentToolId"),
        MAX_IDENTIFIER_BYTES,
    )
    candidate = fallback if found is None else found
    if not isinstance(candidate, str) or not candidate:
        return ""
    return candidate if _valid_tool_id(candidate) else None


def _tool_input(node: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("tool_input", "toolInput", "input", "arguments"):
        value = node.get(key, _MISSING)
        if value is not _MISSING:
            return value if isinstance(value, Mapping) else None
    tool = node.get("tool")
    if isinstance(tool, Mapping):
        for key in ("input", "arguments"):
            value = tool.get(key, _MISSING)
            if value is not _MISSING:
                return value if isinstance(value, Mapping) else None
    return None


def _tool_response(node: Mapping[str, Any]) -> object:
    for key in (
        "tool_response",
        "toolResponse",
        "tool_output",
        "toolOutput",
        "response",
        "result",
        "output",
    ):
        value = node.get(key, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _execution_status(
    node: Mapping[str, Any], response: object
) -> tuple[int, bool] | None:
    sources: tuple[Mapping[str, Any], ...] = (
        (response, node) if isinstance(response, Mapping) else (node,)
    )
    exit_code = _first_value(
        sources, ("exit_code", "exitCode", "returncode", "return_code")
    )
    success = _first_value(sources, ("success", "ok"))
    if exit_code is not _MISSING:
        if (
            isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or abs(exit_code) > 2**31
        ):
            return None
        derived = exit_code == 0
        if success is not _MISSING and (
            not isinstance(success, bool) or success != derived
        ):
            return None
        return exit_code, derived
    if isinstance(success, bool):
        return (0 if success else 1), success
    return None


def _command(tool_input: Mapping[str, Any]) -> str | None:
    value = _first_value((tool_input,), ("command", "cmd"))
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    return value


def _bounded_command_spec(command: str) -> tuple[str, ...]:
    if len(command.encode("utf-8")) > MAX_COMMAND_SPEC_BYTES:
        return ("[TRUNCATED COMMAND]",)
    if _contains_sensitive_command(command):
        return ("[REDACTED]",)
    redacted = _redact_command(command)
    try:
        parts = tuple(shlex.split(redacted, posix=True))
    except ValueError:
        parts = tuple(redacted.split())
    if not parts:
        return ("[EMPTY COMMAND]",)
    if any(len(part.encode("utf-8")) > MAX_COMMAND_SPEC_BYTES for part in parts):
        return ("[TRUNCATED COMMAND]",)
    if len(canonical_json(parts).encode("utf-8")) > MAX_COMMAND_SPEC_BYTES:
        return ("[TRUNCATED COMMAND]",)
    return parts


def _redact_command(command: str) -> str:
    redacted = _PEM.sub("[REDACTED PEM]", command)
    redacted = _URL_CREDENTIALS.sub(r"\g<prefix>[REDACTED]@", redacted)
    redacted = _BEARER.sub(r"\1[REDACTED]", redacted)
    redacted = _ASSIGNMENT.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = _WINDOWS_ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)
    redacted = _POSIX_ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)
    return _RAW_TOKEN.sub("[REDACTED]", redacted)


def _contains_sensitive_command(command: str) -> bool:
    """Return whether a labeled credential must hide the full command spec.

    A structured command spec is useful for normal commands, but retaining a
    partially redacted command is too easy to get wrong for option, header,
    and JSON credential forms.  Treat those forms as one opaque marker.
    """

    return bool(
        _RAW_TOKEN.search(command)
        or _PEM.search(command)
        or _BEARER.search(command)
        or _URL_CREDENTIALS.search(command)
        or _SENSITIVE_OPTION.search(command)
        or _SENSITIVE_KEY_VALUE.search(command)
    )


def _children(node: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...] | None:
    sources: list[Mapping[str, Any]] = [node]
    response = _tool_response(node)
    if isinstance(response, Mapping):
        sources.append(response)
    for source in sources:
        for key in (
            "results",
            "tool_results",
            "toolResults",
            "children",
            "calls",
            "executions",
        ):
            value = source.get(key, _MISSING)
            if value is _MISSING:
                continue
            if not isinstance(value, Sequence) or isinstance(
                value, (str, bytes, bytearray)
            ):
                return None
            children = tuple(value)
            if not all(isinstance(child, Mapping) for child in children):
                return None
            return children
    return None


def _first_text(
    sources: Sequence[Mapping[str, Any]], keys: Sequence[str], maximum: int
) -> str | None:
    value = _first_value(sources, keys)
    return _bounded_text(value, maximum) if value is not _MISSING else None


def _first_value(sources: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> object:
    for source in sources:
        for key in keys:
            value = source.get(key, _MISSING)
            if value is not _MISSING:
                return value
    return _MISSING


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        if len(value.encode("utf-8")) > maximum:
            return None
    except UnicodeError:
        return None
    return value


def _valid_observed_at(value: str) -> bool:
    if not _OBSERVED_AT.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return True


def _valid_tool_id(value: str) -> bool:
    """Accept host identifiers only when they cannot themselves be credentials."""

    return bool(_TOOL_ID.fullmatch(value)) and not bool(_RAW_TOKEN.search(value))


def _context_hash(value: str) -> str:
    return canonical_hash({"value": value})


def _validate_receipt(receipt: RawExecutionReceipt) -> None:
    try:
        valid = (
            receipt.schema_version == RAW_RECEIPT_SCHEMA_VERSION
            and receipt.host in {"claude", "codex"}
            and receipt.canonical_tool in {"shell", "patch"}
            and bool(_HASH.fullmatch(receipt.receipt_id))
            and all(
                _HASH.fullmatch(value)
                for value in (
                    receipt.session_id_hash,
                    receipt.turn_id_hash,
                    receipt.command_spec_hash,
                    receipt.input_hash,
                    receipt.output_hash,
                    receipt.workspace_hash,
                )
            )
            and _valid_tool_id(receipt.tool_use_id)
            and (
                not receipt.parent_tool_use_id
                or _valid_tool_id(receipt.parent_tool_use_id)
            )
            and not isinstance(receipt.exit_code, bool)
            and isinstance(receipt.exit_code, int)
            and abs(receipt.exit_code) <= 2**31
            and isinstance(receipt.success, bool)
            and receipt.success == (receipt.exit_code == 0)
            and isinstance(receipt.observed_at, str)
            and len(receipt.observed_at.encode("utf-8")) <= MAX_OBSERVED_AT_BYTES
            and _valid_observed_at(receipt.observed_at)
            and all(isinstance(part, str) for part in receipt.command_spec)
            and len(canonical_json(receipt.command_spec).encode("utf-8"))
            <= MAX_COMMAND_SPEC_BYTES
            and canonical_hash(receipt.command_spec) == receipt.command_spec_hash
            and receipt.expected_receipt_id() == receipt.receipt_id
        )
    except (AttributeError, TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ReceiptIntegrityError("receipt_content_invalid")


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_NESTED_RECEIPT_DEPTH",
    "MAX_RECEIPTS_PER_PAYLOAD",
    "RAW_RECEIPT_SCHEMA_VERSION",
    "RawExecutionReceipt",
    "ReceiptConflictError",
    "ReceiptError",
    "ReceiptIntegrityError",
    "ReceiptRepository",
    "normalize_post_tool_use",
]
