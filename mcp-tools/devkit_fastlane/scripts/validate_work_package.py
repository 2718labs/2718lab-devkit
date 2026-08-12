"""Validate a layered 2718lab work package.

A package separates the product-facing direction from coordinator metadata and
agent-scoped implementation cards. This validator is diagnostic-only: it
checks shape, not business correctness, live ProjectAuthority, assignment,
worktree creation, or crash-resume eligibility. Those executable boundaries
belong to the Fast Lane compiler and its host bridge.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

PRODUCT_BRIEF = "product-brief.md"
INDEX = "index.md"
TASKS = "tasks"
MAX_PRODUCT_LINES = 120
MAX_INDEX_LINES = 160
MAX_TASK_LINES = 220
PRODUCT_HEADINGS = ("Goal", "Scope", "Direction", "Risk Gate", "Done")
INDEX_HEADINGS = ("Shared Contracts", "Tasks", "Dispatch")
TASK_HEADINGS = ("Goal", "Context", "Write Scope", "Steps", "Acceptance", "Return")
OWNER_RE = re.compile(r"^Owner:\s*([A-Za-z0-9._-]+)\s*$", re.MULTILINE)
TASK_REF_RE = re.compile(r"`(tasks/[A-Za-z0-9._-]+\.md)`")
WRITE_SCOPE_RE = re.compile(
    r"^##\s+Write Scope\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
WRITE_PATH_RE = re.compile(r"^\s*-\s+`([^`]+)`", re.MULTILINE)
READ_ONLY_SCOPE_RE = re.compile(r"^\s*-\s+none\s*$", re.IGNORECASE)
STRICT_INDEX_SEQUENCE = (
    "project_index_sync",
    "workflow_register_task",
    "strict_index=true",
)
STRICT_TASK_SEQUENCE = (
    "project_index_query",
    "trace_id",
    "worktree_checkpoint_create",
    'project_index_sync(bind_as="output")',
    "project_index_query",
    "trace_id",
    'workflow_artifact_register(kind="verification", snapshot_id=...)',
    "workflow_complete",
)
STRICT_READ_ONLY_TASK_SEQUENCE = (
    "project_index_query",
    "trace_id",
    'workflow_artifact_register(kind="verification", snapshot_id=...)',
    "workflow_complete",
)
DOCUMENTATION_SUFFIXES = frozenset({".adoc", ".md", ".mdx", ".rst", ".txt"})
NONTERMINAL_SCOPE_STATES = frozenset(
    {
        "active",
        "blocked",
        "ci_gate",
        "claimed",
        "in_progress",
        "integrating",
        "new",
        "patching",
        "pending",
        "ready",
        "release_gate",
        "reviewing",
        "running",
        "sol_review",
        "triaged",
        "verifying",
    }
)
TERMINAL_SCOPE_STATES = frozenset(
    {"accepted", "cancelled", "completed", "done", "failed", "released"}
)
INTEGRATION_RECORD_FIELDS = (
    "task_id",
    "source_branch",
    "source_worktree",
    "candidate_commit",
    "base_revision",
    "evidence_hash",
    "integration_order",
)
VERIFICATION_LANE_NAMES = frozenset({"core", "extended", "platform"})
HANDOFF_SEQUENCE = (
    "workflow_artifact_register",
    "workflow_message_send",
    "workflow_inbox",
    "workflow_artifact_resolve",
    "workflow_message_ack",
)
RESUME_SEQUENCE = (
    "workflow_endpoint_bind",
    "workflow_inbox",
    "workflow_artifact_resolve",
    "workflow_message_ack",
    "resume_next_action",
)
RESUME_PACKET_FIELDS = frozenset(
    {
        "workflow_id",
        "task_id",
        "lease_epoch",
        "current_endpoint",
        "base_commit",
        "candidate_commit",
        "branch_or_worktree",
        "write_scope_hash",
        "latest_red",
        "latest_green",
        "contract_hashes",
        "evidence_hashes",
        "next_action",
        "redacted",
        "resume_steps",
    }
)
FORBIDDEN_RESUME_FIELDS = frozenset(
    {
        "chat_history",
        "credential",
        "credentials",
        "environment",
        "env",
        "raw_stderr",
        "raw_stdout",
        "source",
        "source_body",
        "stderr",
        "stdout",
    }
)
MAX_RESUME_TEXT_LENGTH = 512
MAX_RESUME_HASHES = 16
MAX_RESUME_ENDPOINT_LENGTH = 256
MAX_RESUME_REF_LENGTH = 260
MAX_RESUME_NEXT_ACTION_LENGTH = 256
MAX_LEASE_EPOCH = 2**63 - 1
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ENDPOINT_RE = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
ARTIFACT_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
BRANCH_OR_WORKTREE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\\: @-]{0,259}$")
FORBIDDEN_RESUME_PAYLOAD_RE = re.compile(
    r"(?:authorization|api[-_]?key|password|passwd|secret|token|stdout|stderr)\s*[:=]"
    r"|traceback \(most recent call last\)"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|(?:^|\s)\[(?:debug|info|warning|error|critical)\]",
    re.IGNORECASE,
)


def _read(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"missing required file: {path.name}")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path.name}: must be UTF-8")
        return ""


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _require_headings(
    path: Path, text: str, headings: tuple[str, ...], errors: list[str]
) -> None:
    for heading in headings:
        if not re.search(
            rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE | re.IGNORECASE
        ):
            errors.append(f"{path.name}: missing heading '## {heading}'")


def _require_ordered_markers(
    path: Path,
    text: str,
    markers: tuple[str, ...],
    errors: list[str],
) -> None:
    cursor = 0
    for marker in markers:
        position = text.find(marker, cursor)
        if position < 0:
            errors.append(
                f"{path.name}: strict index missing or out-of-order marker: {marker}"
            )
            continue
        cursor = position + len(marker)


def _write_scope_paths(text: str) -> tuple[str, ...]:
    section = WRITE_SCOPE_RE.search(text)
    if section is None:
        return ()
    return tuple(WRITE_PATH_RE.findall(section.group("body")))


def _is_read_only_scope(text: str) -> bool:
    section = WRITE_SCOPE_RE.search(text)
    if section is None:
        return False
    return READ_ONLY_SCOPE_RE.fullmatch(section.group("body").strip()) is not None


def _is_documentation_scope(paths: tuple[str, ...]) -> bool:
    if not paths:
        return False
    for value in paths:
        normalized = value.replace("\\", "/").lower()
        suffix = Path(normalized).suffix
        if (
            not normalized.startswith(("docs/", "documentation/"))
            and suffix not in DOCUMENTATION_SUFFIXES
        ):
            return False
    return True


def _normalized_scope_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("\\", "/").strip("/").casefold()
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return None
    return normalized


def _scope_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _validate_sol_review_receipt(
    record: Mapping[str, object], errors: list[str]
) -> None:
    receipt = record.get("review_receipt")
    if not isinstance(receipt, Mapping):
        errors.append("integration record missing review_receipt")
        return
    for field in ("receipt_hash", "reviewer_role", "reviewer_task_id", "decision"):
        value = receipt.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"integration review_receipt missing {field}")
    if not _is_artifact_hash(receipt.get("receipt_hash")):
        errors.append("integration review_receipt has invalid receipt_hash")
    reviewer_role = receipt.get("reviewer_role")
    reviewer_task_id = receipt.get("reviewer_task_id")
    if reviewer_role != "sol" or reviewer_task_id == record.get("task_id"):
        errors.append("integration review_receipt must be from non-worker Sol")
    if receipt.get("decision") != "accepted":
        errors.append("integration review_receipt decision must be accepted")
    for field in (
        "task_id",
        "candidate_commit",
        "source_branch",
        "source_worktree",
        "evidence_hash",
    ):
        if receipt.get(field) != record.get(field):
            errors.append(f"integration review_receipt is not bound to {field}")


def validate_parallel_integration_record(record: Mapping[str, object]) -> list[str]:
    """Validate a local candidate-integration record without touching Git/network."""

    errors: list[str] = []
    if not isinstance(record, Mapping):
        return ["integration record must be a mapping"]
    for field in INTEGRATION_RECORD_FIELDS:
        value = record.get(field)
        if field == "integration_order":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                errors.append("integration record missing positive integration_order")
        elif not isinstance(value, str) or not value:
            errors.append(f"integration record missing {field}")
    _validate_sol_review_receipt(record, errors)
    if (
        record.get("direct_worker_merge") is True
        or record.get("worker_claimed_merge") is True
    ):
        errors.append("workers may not claim a direct merge")

    active = record.get("active_write_scopes", ())
    if isinstance(active, (str, bytes)) or not isinstance(active, Sequence):
        return [*errors, "active_write_scopes must be a sequence"]
    observed: list[tuple[str, str]] = []
    for index, scope in enumerate(active):
        if not isinstance(scope, Mapping):
            errors.append(f"active write scope {index} must be a mapping")
            continue
        state = scope.get("state")
        if isinstance(state, str) and state in TERMINAL_SCOPE_STATES:
            continue
        if not isinstance(state, str) or state not in NONTERMINAL_SCOPE_STATES:
            errors.append(f"active write scope {index} has unknown state: {state}")
        task = scope.get("task")
        paths = scope.get("paths")
        if not isinstance(task, str) or not task:
            errors.append(f"active write scope {index} missing task")
            continue
        if (
            isinstance(paths, (str, bytes))
            or not isinstance(paths, Sequence)
            or not paths
        ):
            errors.append(f"active write scope {index} missing paths")
            continue
        for raw_path in paths:
            path = _normalized_scope_path(raw_path)
            if path is None:
                errors.append(f"active write scope {index} has invalid path")
                continue
            for other_task, other_path in observed:
                if task != other_task and _scope_overlap(path, other_path):
                    errors.append(
                        "overlapping active write scopes: "
                        f"{task}:{path} conflicts with {other_task}:{other_path}"
                    )
            observed.append((task, path))
    return errors


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_bounded_compact_text(value: object, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and FORBIDDEN_RESUME_PAYLOAD_RE.search(value) is None
    )


def _is_artifact_hash(value: object) -> bool:
    return isinstance(value, str) and ARTIFACT_HASH_RE.fullmatch(value) is not None


def _validate_resume_text(
    field: str,
    value: object,
    errors: list[str],
    *,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> None:
    if not isinstance(value, str) or not _is_bounded_compact_text(value, max_length):
        errors.append(f"crash-resume packet has invalid or unbounded {field}")
        return
    if pattern is not None and pattern.fullmatch(value) is None:
        errors.append(f"crash-resume packet has invalid {field}")


def _require_record_text(
    record: Mapping[str, object], field: str, errors: list[str], *, prefix: str
) -> None:
    if not _nonempty_text(record.get(field)):
        errors.append(f"{prefix} missing {field}")


def _require_evidence_hash(
    lane: str, record: Mapping[str, object], errors: list[str]
) -> None:
    if not _nonempty_text(record.get("evidence_hash")):
        errors.append(f"{lane} lane missing evidence_hash")


def validate_verification_lanes(record: Mapping[str, object]) -> list[str]:
    """Validate the bounded core/extended/platform acceptance record."""

    if not isinstance(record, Mapping):
        return ["verification lanes record must be a mapping"]
    errors: list[str] = []
    lanes = record.get("lanes")
    if not isinstance(lanes, Mapping):
        return ["verification lanes record missing lanes mapping"]
    lane_names = set(lanes)
    missing = VERIFICATION_LANE_NAMES.difference(lane_names)
    extra = lane_names.difference(VERIFICATION_LANE_NAMES)
    for name in sorted(missing):
        errors.append(f"verification lanes record missing {name} lane")
    for name in sorted(extra, key=str):
        errors.append(f"verification lanes record has unknown lane: {name}")

    acceptance_requested = record.get("acceptance_requested", False)
    if not isinstance(acceptance_requested, bool):
        errors.append("verification lanes acceptance_requested must be boolean")
    platform_support_claimed = record.get("platform_support_claimed", False)
    if not isinstance(platform_support_claimed, bool):
        errors.append("verification lanes platform_support_claimed must be boolean")

    core = lanes.get("core")
    if not isinstance(core, Mapping):
        errors.append("core verification lane must be a mapping")
    elif core.get("status") != "passed":
        errors.append("core verification lane blocks acceptance: status must be passed")
    else:
        _require_evidence_hash("core", core, errors)

    extended = lanes.get("extended")
    if not isinstance(extended, Mapping):
        errors.append("extended verification lane must be a mapping")
    else:
        extended_status = extended.get("status")
        if extended_status == "passed":
            _require_evidence_hash("extended", extended, errors)
        elif extended_status == "deferred":
            for field in ("evidence_hash", "owner", "release_gate", "timebox"):
                if not _nonempty_text(extended.get(field)):
                    errors.append(f"extended deferred lane missing {field}")
        else:
            errors.append("extended lane must be passed or explicitly deferred")

    platform = lanes.get("platform")
    if not isinstance(platform, Mapping):
        errors.append("platform verification lane must be a mapping")
    else:
        platform_status = platform.get("status")
        if platform_status == "passed":
            _require_evidence_hash("platform", platform, errors)
        elif platform_status == "skipped":
            if not _nonempty_text(platform.get("reason")):
                errors.append("platform skipped lane missing reason")
            if platform_support_claimed is True:
                errors.append(
                    "platform support claim requires platform lane to pass before release"
                )
        else:
            errors.append("platform lane must be passed or honestly skipped")
    return errors


def _validate_ordered_steps(
    steps: object,
    expected: tuple[str, ...],
    errors: list[str],
    *,
    prefix: str,
) -> None:
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        errors.append(f"{prefix} steps must be a sequence")
        return
    if tuple(steps) != expected:
        errors.append(f"{prefix} steps are missing or out of order")


def _validate_resume_summary(field: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"resume packet missing {field} summary")
        return
    if len(value) != 2 or set(value) != {"command", "result"}:
        errors.append(
            f"resume packet {field} summary must contain command and result only"
        )
        return
    for key in ("command", "result"):
        summary_value = value.get(key)
        if not _is_bounded_compact_text(summary_value, MAX_RESUME_TEXT_LENGTH):
            errors.append(
                f"resume packet {field} summary has invalid or unbounded {key}"
            )


def _validate_resume_hashes(field: str, value: object, errors: list[str]) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        errors.append(f"resume packet missing {field}")
        return
    if not value or len(value) > MAX_RESUME_HASHES:
        errors.append(f"resume packet {field} must be a bounded non-empty sequence")
        return
    if not all(_is_artifact_hash(item) for item in value):
        errors.append(f"resume packet {field} must contain only bounded SHA-256 hashes")


def validate_crash_resume_packet(packet: Mapping[str, object]) -> list[str]:
    """Validate a redacted packet used to resume a crashed task endpoint."""

    if not isinstance(packet, Mapping):
        return ["crash-resume packet must be a mapping"]
    errors: list[str] = []
    if len(packet) > len(RESUME_PACKET_FIELDS):
        errors.append("crash-resume packet container is unbounded")
    for field in packet:
        if isinstance(field, str) and field.casefold() in FORBIDDEN_RESUME_FIELDS:
            errors.append(f"crash-resume packet contains forbidden field: {field}")
        elif field not in RESUME_PACKET_FIELDS:
            errors.append(f"crash-resume packet contains unknown field: {field}")
    for field in ("workflow_id", "task_id"):
        _validate_resume_text(
            field,
            packet.get(field),
            errors,
            max_length=128,
            pattern=IDENTITY_RE,
        )
    _validate_resume_text(
        "current_endpoint",
        packet.get("current_endpoint"),
        errors,
        max_length=MAX_RESUME_ENDPOINT_LENGTH,
        pattern=ENDPOINT_RE,
    )
    for field in ("base_commit", "candidate_commit"):
        _validate_resume_text(
            field,
            packet.get(field),
            errors,
            max_length=64,
            pattern=COMMIT_RE,
        )
    _validate_resume_text(
        "branch_or_worktree",
        packet.get("branch_or_worktree"),
        errors,
        max_length=MAX_RESUME_REF_LENGTH,
        pattern=BRANCH_OR_WORKTREE_RE,
    )
    if not _is_artifact_hash(packet.get("write_scope_hash")):
        errors.append("crash-resume packet has invalid write_scope_hash")
    _validate_resume_text(
        "next_action",
        packet.get("next_action"),
        errors,
        max_length=MAX_RESUME_NEXT_ACTION_LENGTH,
    )
    lease_epoch = packet.get("lease_epoch")
    if (
        isinstance(lease_epoch, bool)
        or not isinstance(lease_epoch, int)
        or lease_epoch < 1
        or lease_epoch > MAX_LEASE_EPOCH
    ):
        errors.append("crash-resume packet has invalid bounded lease_epoch")
    if packet.get("redacted") is not True:
        errors.append("crash-resume packet must be explicitly redacted")
    _validate_resume_summary("latest_red", packet.get("latest_red"), errors)
    _validate_resume_summary("latest_green", packet.get("latest_green"), errors)
    _validate_resume_hashes("contract_hashes", packet.get("contract_hashes"), errors)
    _validate_resume_hashes("evidence_hashes", packet.get("evidence_hashes"), errors)
    _validate_ordered_steps(
        packet.get("resume_steps"),
        RESUME_SEQUENCE,
        errors,
        prefix="crash-resume packet",
    )
    return errors


def validate_mcp_handoff_record(record: Mapping[str, object]) -> list[str]:
    """Validate the existing mailbox contract-handoff sequence without I/O."""

    if not isinstance(record, Mapping):
        return ["MCP handoff record must be a mapping"]
    errors: list[str] = []
    if record.get("artifact_kind") != "contract":
        errors.append("MCP handoff artifact_kind must be contract")
    _require_record_text(record, "artifact_hash", errors, prefix="MCP handoff")
    if record.get("interface_frozen") is not True:
        errors.append("MCP handoff requires a frozen public interface")
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping) or set(metadata) != {"kind"}:
        errors.append("MCP handoff metadata must contain only kind")
    elif metadata.get("kind") != "contract":
        errors.append("MCP handoff metadata kind must be contract")
    _validate_ordered_steps(
        record.get("steps"), HANDOFF_SEQUENCE, errors, prefix="MCP handoff"
    )
    return errors


def _validate_product_brief(path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    lines = _line_count(text)
    if lines > MAX_PRODUCT_LINES:
        errors.append(
            f"product-brief.md: {lines} lines exceeds the 120-line product budget"
        )
    if "```" in text:
        errors.append("product-brief.md: code fences belong in task cards or contracts")
    _require_headings(path, text, PRODUCT_HEADINGS, errors)


def _validate_index(root: Path, path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    lines = _line_count(text)
    if lines > MAX_INDEX_LINES:
        errors.append(
            f"index.md: {lines} lines exceeds the 160-line coordinator budget"
        )
    _require_headings(path, text, INDEX_HEADINGS, errors)
    for relative in TASK_REF_RE.findall(text):
        if not (root / relative).is_file():
            errors.append(f"index.md: referenced task does not exist: {relative}")


def _validate_task(path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    lines = _line_count(text)
    if lines > MAX_TASK_LINES:
        errors.append(
            f"{path.name}: {lines} lines exceeds the 220-line task-card budget"
        )

    owner_lines = re.findall(r"^Owner:\s*(.+?)\s*$", text, re.MULTILINE)
    owner = OWNER_RE.search(text)
    if len(owner_lines) != 1 or owner is None:
        errors.append(f"{path.name}: task card must declare exactly one owner")

    _require_headings(path, text, TASK_HEADINGS, errors)
    write_scope = WRITE_SCOPE_RE.search(text)
    if write_scope is None or (
        not WRITE_PATH_RE.search(write_scope.group("body"))
        and not _is_read_only_scope(text)
    ):
        errors.append(f"{path.name}: Write Scope must list at least one exact path")


def _validate_strict_task(path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    if _is_read_only_scope(text):
        _require_ordered_markers(path, text, STRICT_READ_ONLY_TASK_SEQUENCE, errors)
        return

    _require_ordered_markers(path, text, STRICT_TASK_SEQUENCE, errors)

    paths = _write_scope_paths(text)
    if _is_documentation_scope(paths):
        return

    owner_match = OWNER_RE.search(text)
    owner = owner_match.group(1).lower() if owner_match else ""
    if "luna" in owner:
        normalized = " ".join(text.lower().split())
        luna_pair = re.compile(
            r"(?:gpt-5\.6-luna.{0,80}\b(?:low|medium|high|xhigh)\b|"
            r"\b(?:low|medium|high|xhigh)\b.{0,80}gpt-5\.6-luna)"
        )
        if not all(marker in normalized for marker in ("attest", "capability")) or (
            not luna_pair.search(normalized)
        ):
            errors.append(
                f"{path.name}: Luna code dispatch requires attested "
                "gpt-5.6-luna with low, medium, high, or xhigh"
            )
    if "terra" in owner:
        if "gpt-5.6-terra" not in text:
            errors.append(f"{path.name}: Terra code dispatch missing gpt-5.6-terra")
        if "high" not in text and "max" not in text:
            errors.append(
                f"{path.name}: Terra code dispatch missing high or max reasoning"
            )
    if "sol" in owner and ("gpt-5.6-sol" not in text or "high" not in text):
        errors.append(f"{path.name}: Sol High dispatch missing gpt-5.6-sol/high")


def validate_work_package(
    root: Path | str,
    *,
    strict_index: bool = False,
) -> list[str]:
    package_root = Path(root).resolve()
    errors: list[str] = []
    if not package_root.is_dir():
        return [f"work package directory does not exist: {package_root}"]

    _validate_product_brief(package_root / PRODUCT_BRIEF, errors)
    index_path = package_root / INDEX
    _validate_index(package_root, index_path, errors)
    if strict_index:
        index_text = _read(index_path, errors)
        if index_text:
            _require_ordered_markers(
                index_path,
                index_text,
                STRICT_INDEX_SEQUENCE,
                errors,
            )

    tasks_dir = package_root / TASKS
    task_files = sorted(tasks_dir.glob("*.md")) if tasks_dir.is_dir() else []
    if not task_files:
        errors.append("tasks/: at least one agent-scoped task card is required")
    for task in task_files:
        _validate_task(task, errors)
        if strict_index:
            _validate_strict_task(task, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package", type=Path, help="Directory containing product-brief.md and index.md"
    )
    parser.add_argument(
        "--strict-index",
        action="store_true",
        help="Require index-first routing and strict completion gates",
    )
    args = parser.parse_args(argv)
    errors = validate_work_package(args.package, strict_index=args.strict_index)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: layered work package is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
