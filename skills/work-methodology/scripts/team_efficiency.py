"""Deterministic, local helpers for bounded team orchestration data."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


MAX_PACKET_BYTES = 16 * 1024
MAX_STATUS_BYTES = 32 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_MANIFEST_INPUT_BYTES = 4 * MAX_MANIFEST_BYTES
MAX_WRITE_SCOPES = 32
MAX_STATUS_TASKS = 64
MAX_MANIFEST_UNITS = 16
MAX_LIST_ITEMS = 32
MAX_GRAPH_NODES = 64
MAX_GRAPH_EDGES = 128
MAX_REGISTRATION_CARD_BYTES = 4 * 1024
MAX_GATE_TIMEOUT_SECONDS = 3600
FAST_LANE_SLOT_IDS = ("slot-1", "slot-2", "slot-3")
FAST_LANE_REASONING_EFFORTS = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)

_TASK_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_GIT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_SHA256 = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_ENDPOINT = re.compile(
    r"^/[A-Za-z0-9][A-Za-z0-9._/-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_WINDOWS_ABSOLUTE = re.compile(r"(?i)[a-z]:[\\/]")
_POSIX_ABSOLUTE = re.compile(r"(?:^|\s)/[^\s]+")
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "bearer ",
    "authorization:",
    "private key",
)

_PACKET_FIELDS = frozenset(
    {
        "workflow_id",
        "task_id",
        "lease_epoch",
        "endpoint",
        "base_commit",
        "candidate_commit",
        "worktree_id",
        "write_scope_hash",
        "latest_red",
        "latest_green",
        "contract_hashes",
        "evidence_hashes",
        "next_action",
        "redacted",
    }
)
_BOOTSTRAP_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "task_id",
        "base_commit",
        "branch",
        "write_scope",
        "write_scope_hash",
        "repo",
        "project",
        "worktree",
        "temp_target",
        "command_argv",
        "workflow_fields",
    }
)
_CACHE_FIELDS = frozenset(
    {
        "candidate_commit",
        "candidate_tree",
        "write_scope_hash",
        "argv",
        "toolchain_fingerprint",
        "platform_fingerprint",
        "dependency_lock_hashes",
        "test_lane",
    }
)
_FAST_LANE_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "work_package",
        "target_gates",
        "execution_contexts",
        "read_contexts",
        "remediation_request",
        "scheduler_state",
    }
)
_FAST_LANE_GATE_FIELDS = frozenset(
    {
        "gate_id",
        "argv",
        "red_expected_exit_codes",
        "green_expected_exit_code",
        "timeout_seconds",
        "red_failure_ids",
        "red_failure_fingerprint",
        "acceptance_constraint_hashes",
    }
)
_FAST_LANE_TARGET_GATE_FIELDS = frozenset({"task_id", "driver_gate_id", "gates"})
_FAST_LANE_PLAN_FIELDS = frozenset(
    {
        "schema",
        "status",
        "decision_code",
        "activation",
        "source_plan_hash",
        "phase",
        "main_lane",
        "subagent_capacity",
        "assignments",
        "ready_queue",
        "review_queue",
        "prewarm_queue",
        "design_queue",
        "invalidated_evidence_task_ids",
        "idle_slots",
        "refill_plan",
        "terminal_protocol",
        "workflow_policy",
        "plan_hash",
    }
)
_FAST_LANE_PHASES = frozenset(
    {
        "execution",
        "integration_regression",
        "blocker_review",
        "remediation",
        "acceptance",
        "stopped",
    }
)
_FAST_LANE_SHELL_WRAPPERS = frozenset(
    {"bash", "cmd", "cmd.exe", "fish", "powershell", "pwsh", "sh", "zsh"}
)
_FAST_LANE_SHELL_ARGUMENTS = frozenset(
    {"--command", "--encodedcommand", "-c", "-command", "/c", "/k"}
)
_ROUTES = {
    "routine": "Terra High",
    "moderate": "Terra Max",
    "complex": "Terra Max",
    "exceptional": "Sol High",
}
_ARTIFACT_SOURCE_KINDS = frozenset({"explicit_artifact_boundaries"})
_ATLAS_SOURCE_KINDS = frozenset({"code_atlas_packet", "task_episode_graph"})
_ATLAS_PACKET_FIELDS = frozenset(
    {
        "packet_id",
        "trace_id",
        "workspace",
        "snapshot_id",
        "recipe_id",
        "node_ids",
        "edge_ids",
        "evidence_windows",
        "evidence_hashes",
        "operations",
        "slots",
        "constraints",
        "dependencies",
        "tests",
        "gaps",
        "source_hashes",
        "template_hashes",
        "receipt_hashes",
        "next_action",
    }
)
_ATLAS_GRAPH_FIELDS = frozenset({"nodes", "edges", "truncated"})
_ATLAS_NODE_FIELDS = frozenset(
    {
        "node_id",
        "kind",
        "payload",
        "schema_version",
        "extractor_id",
        "extractor_version",
        "provenance",
        "source_hashes",
        "created_at",
        "superseded_at",
        "quarantine_state",
    }
)
_ATLAS_EDGE_FIELDS = frozenset(
    {
        "edge_id",
        "relation",
        "source_id",
        "target_id",
        "source_kind",
        "target_kind",
        "payload",
        "schema_version",
        "provenance",
        "created_at",
    }
)
_ATLAS_OPERATION_FIELDS = frozenset(
    {"kind", "path_slot", "template_hash", "separator", "target_symbol_slot"}
)
_ATLAS_SLOT_FIELDS = frozenset({"name", "type", "required"})
_ATLAS_CONSTRAINT_FIELDS = frozenset({"kind", "subject", "value"})
_ATLAS_DEPENDENCY_FIELDS = frozenset({"name", "kind", "specifier"})
_ATLAS_TEST_FIELDS = frozenset({"argv", "expected_exit_code"})
_ATLAS_NODE_KINDS = frozenset(
    {
        "TaskEpisode",
        "Intent",
        "Recipe",
        "CodeTemplate",
        "AdaptationSlot",
        "Constraint",
        "Dependency",
        "TestSpec",
        "ExecutionReceipt",
        "SourceEvidence",
        "Language",
        "Framework",
    }
)
_ATLAS_EDGE_RELATIONS = frozenset(
    {
        "SOLVES",
        "DERIVED_FROM",
        "HAS_IMPLEMENTATION",
        "HAS_SLOT",
        "CONSTRAINED_BY",
        "REQUIRES",
        "VERIFIED_BY",
        "CHANGES",
        "TESTS",
        "SUPERSEDES",
        "BUNDLED_AS",
    }
)
_ATLAS_EDGE_ENDPOINTS = {
    "SOLVES": ({"TaskEpisode", "Recipe"}, {"Intent"}),
    "DERIVED_FROM": ({"Recipe"}, {"TaskEpisode", "SourceEvidence"}),
    "HAS_IMPLEMENTATION": ({"Recipe"}, {"CodeTemplate"}),
    "HAS_SLOT": ({"Recipe"}, {"AdaptationSlot"}),
    "CONSTRAINED_BY": ({"Recipe"}, {"Constraint"}),
    "REQUIRES": ({"Recipe"}, {"Dependency", "Framework", "Language"}),
    "VERIFIED_BY": ({"TaskEpisode", "Recipe"}, {"TestSpec", "ExecutionReceipt"}),
    "CHANGES": ({"TaskEpisode"}, {"SourceEvidence"}),
    "TESTS": ({"TestSpec", "SourceEvidence"}, {"SourceEvidence"}),
    "SUPERSEDES": ({"Recipe"}, {"Recipe"}),
    "BUNDLED_AS": ({"Recipe"}, {"SourceEvidence"}),
}


class ContractMismatchError(ValueError):
    """Raised when a producer and consumer contract cannot safely join."""


class AtlasEvidenceError(ValueError):
    """A machine-readable, fail-closed Atlas evidence decision."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value is not canonical JSON") from error


def _json_bytes(value: object) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _sha256_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{field} has unsupported fields")


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field} must be bounded text")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must be single-line text")
    return value


def _reject_sensitive_or_absolute_text(value: str, field: str) -> None:
    folded = value.casefold()
    if any(marker in folded for marker in _SECRET_MARKERS):
        raise ValueError(f"{field} contains sensitive material")
    if (
        _WINDOWS_ABSOLUTE.search(value)
        or "\\\\" in value
        or _POSIX_ABSOLUTE.search(value)
    ):
        raise ValueError(f"{field} contains an absolute path")


def _task_id(value: object, field: str = "task_id") -> str:
    text = _text(value, field, maximum=96)
    if not _TASK_ID.fullmatch(text):
        raise ValueError(f"{field} is invalid")
    return text


def _git_id(value: object, field: str) -> str:
    text = _text(value, field, maximum=64)
    if not _GIT_ID.fullmatch(text):
        raise ValueError(f"{field} must be a full commit identifier")
    return text.lower()


def _hash(value: object, field: str) -> str:
    text = _text(value, field, maximum=80)
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{field} must be a sha256 hash")
    return text.lower()


def _relative_scope(value: object, field: str) -> str:
    text = _text(value, field, maximum=256)
    if text.startswith("/") or "\\" in text or ":" in text:
        raise ValueError(f"{field} must be a bounded relative path")
    parts = text.split("/")
    if any(not _PATH_PART.fullmatch(part) for part in parts):
        raise ValueError(f"{field} must be a bounded relative path")
    return "/".join(parts)


def _label(value: object, field: str) -> str:
    text = _text(value, field, maximum=128)
    if not _LABEL.fullmatch(text) or ".." in text or "//" in text:
        raise ValueError(f"{field} is invalid")
    return text


def _normalised_list(
    value: object,
    field: str,
    normalizer: Callable[[object, str], str],
    *,
    maximum: int = MAX_LIST_ITEMS,
    required: bool = False,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a list")
    if len(value) > maximum or (required and not value):
        raise ValueError(f"{field} is out of bounds")
    normalized = [normalizer(item, field) for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains duplicates")
    return sorted(normalized)


def _normalised_scopes(value: object, field: str = "write_scope") -> list[str]:
    return _normalised_list(
        value,
        field,
        _relative_scope,
        maximum=MAX_WRITE_SCOPES,
        required=True,
    )


def _branch(value: object) -> str:
    text = _text(value, "branch", maximum=128)
    if (
        not _BRANCH.fullmatch(text)
        or text.upper() == "HEAD"
        or text.startswith((".", "/"))
        or text.endswith((".", "/"))
        or ".." in text
        or "//" in text
        or "/." in text
        or any(part.endswith(".lock") for part in text.split("/"))
    ):
        raise ValueError("branch is invalid")
    return text


def _project_root(project: object) -> tuple[str, Path]:
    project_text = _relative_scope(project, "project")
    root = Path(r"D:\bun\tmp\codex").joinpath(*project_text.split("/"))
    return project_text, root.resolve(strict=False)


def _absolute_path(value: object, field: str) -> Path:
    try:
        path = Path(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{field} must be a path") from error
    if not path.is_absolute():
        raise ValueError(f"{field} must be absolute")
    return path.resolve(strict=False)


def _strictly_below(path: Path, root: Path, field: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{field} must stay below the task root") from error
    if path == root:
        raise ValueError(f"{field} must be below the task root")


def build_bootstrap_plan(
    *,
    task_id: str,
    base_commit: str,
    branch: str,
    write_scope: Sequence[str],
    repo: str | Path,
    project: str,
    worktree: str | Path,
    temp_target: str | Path,
) -> dict[str, Any]:
    """Build a dry-run-only worktree plan without changing repository state."""

    normalized_task = _task_id(task_id)
    normalized_base = _git_id(base_commit, "base_commit")
    normalized_branch = _branch(branch)
    normalized_scope = _normalised_scopes(write_scope)
    normalized_project, root = _project_root(project)
    repo_path = _absolute_path(repo, "repo")
    if not repo_path.is_dir():
        raise ValueError("repo must be an existing directory")
    worktree_path = _absolute_path(worktree, "worktree")
    temp_path = _absolute_path(temp_target, "temp_target")
    _strictly_below(worktree_path, root, "worktree")
    _strictly_below(temp_path, root, "temp_target")

    command_argv = [
        "git",
        "-C",
        str(repo_path),
        "worktree",
        "add",
        "-b",
        normalized_branch,
        str(worktree_path),
        normalized_base,
    ]
    return {
        "schema": "team-efficiency/bootstrap-v1",
        "mode": "dry_run",
        "task_id": normalized_task,
        "base_commit": normalized_base,
        "branch": normalized_branch,
        "write_scope": normalized_scope,
        "write_scope_hash": _sha256_json(normalized_scope),
        "repo": str(repo_path),
        "project": normalized_project,
        "worktree": str(worktree_path),
        "temp_target": str(temp_path),
        "command_argv": command_argv,
        "workflow_fields": [
            "workflow_register_task",
            "workflow_claim",
            "workflow_endpoint_bind",
        ],
    }


def _validated_bootstrap_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _mapping(plan, "bootstrap plan")
    _exact_keys(candidate, _BOOTSTRAP_FIELDS, "bootstrap plan")
    if (
        candidate["mode"] != "dry_run"
        or candidate["schema"] != "team-efficiency/bootstrap-v1"
    ):
        raise ValueError("bootstrap plan is not eligible for apply")
    rebuilt = build_bootstrap_plan(
        task_id=candidate["task_id"],
        base_commit=candidate["base_commit"],
        branch=candidate["branch"],
        write_scope=candidate["write_scope"],
        repo=candidate["repo"],
        project=candidate["project"],
        worktree=candidate["worktree"],
        temp_target=candidate["temp_target"],
    )
    if _canonical_json(candidate) != _canonical_json(rebuilt):
        raise ValueError("bootstrap plan was altered")
    return rebuilt


def _temporary_environment(
    plan: Mapping[str, Any],
) -> tuple[dict[str, str], Path, Path]:
    _, root = _project_root(plan["project"])
    temp_target = _absolute_path(plan["temp_target"], "temp_target")
    _strictly_below(temp_target, root, "temp_target")
    environment = os.environ.copy()
    target_text = str(temp_target)
    for name in ("TEMP", "TMP", "TMPDIR", "CODEX_TASK_TEMP"):
        environment[name] = target_text
    return environment, temp_target, root


def _create_verified_temp_target(temp_target: Path, root: Path) -> Path:
    temp_target.mkdir(parents=True, exist_ok=True)
    verified = temp_target.resolve(strict=True)
    _strictly_below(verified, root, "temp_target")
    if not verified.is_dir():
        raise ValueError("temp_target must be a directory")
    return verified


def _run_git_probe(argv: list[str], *, check: bool, env: Mapping[str, str]) -> None:
    subprocess.run(
        argv,
        check=check,
        env=dict(env),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_worktree_add(argv: list[str], *, check: bool, env: Mapping[str, str]) -> None:
    subprocess.run(argv, check=check, env=dict(env))


def apply_bootstrap_plan(
    plan: Mapping[str, Any],
    *,
    runner: Callable[..., object] | None = None,
    probe_runner: Callable[..., object] | None = None,
) -> dict[str, Any]:
    """Apply the one validated argument-vector operation after an absence check."""

    validated = _validated_bootstrap_plan(plan)
    worktree = Path(validated["worktree"])
    if worktree.exists():
        raise ValueError("worktree target already exists")
    environment, temp_target, root = _temporary_environment(validated)
    selected_probe = probe_runner if probe_runner is not None else _run_git_probe
    selected_probe(
        ["git", "-C", validated["repo"], "rev-parse", "--git-dir"],
        check=True,
        env=environment,
    )
    selected_probe(
        [
            "git",
            "-C",
            validated["repo"],
            "rev-parse",
            "--verify",
            f"{validated['base_commit']}^{{commit}}",
        ],
        check=True,
        env=environment,
    )
    verified_temp_target = _create_verified_temp_target(temp_target, root)
    for name in ("TEMP", "TMP", "TMPDIR", "CODEX_TASK_TEMP"):
        environment[name] = str(verified_temp_target)
    if worktree.exists():
        raise ValueError("worktree target already exists")
    selected_runner = runner if runner is not None else _run_worktree_add
    selected_runner(validated["command_argv"], check=True, env=environment)
    applied = dict(validated)
    applied["mode"] = "applied"
    return applied


def _summary(value: object, field: str) -> dict[str, str]:
    summary = _mapping(value, field)
    _exact_keys(summary, frozenset({"command", "result"}), field)
    command = _text(summary["command"], f"{field}.command", maximum=512)
    result = _text(summary["result"], f"{field}.result", maximum=512)
    _reject_sensitive_or_absolute_text(command, f"{field}.command")
    _reject_sensitive_or_absolute_text(result, f"{field}.result")
    return {"command": command, "result": result}


def _endpoint(value: object) -> str:
    text = _text(value, "endpoint", maximum=160)
    if not _ENDPOINT.fullmatch(text):
        raise ValueError("endpoint is invalid")
    return text


def _worktree_id(value: object) -> str:
    text = _text(value, "worktree_id", maximum=96)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", text):
        raise ValueError("worktree_id is invalid")
    return text


def _validated_packet(packet: object) -> dict[str, Any]:
    source = _mapping(packet, "resume packet")
    _exact_keys(source, _PACKET_FIELDS, "resume packet")
    lease_epoch = source["lease_epoch"]
    if type(lease_epoch) is not int or lease_epoch < 1:
        raise ValueError("lease_epoch must be positive")
    next_action = _text(source["next_action"], "next_action", maximum=512)
    _reject_sensitive_or_absolute_text(next_action, "next_action")
    if source["redacted"] is not True:
        raise ValueError("resume packet must be redacted")
    return {
        "workflow_id": _label(source["workflow_id"], "workflow_id"),
        "task_id": _task_id(source["task_id"]),
        "lease_epoch": lease_epoch,
        "endpoint": _endpoint(source["endpoint"]),
        "base_commit": _git_id(source["base_commit"], "base_commit"),
        "candidate_commit": _git_id(source["candidate_commit"], "candidate_commit"),
        "worktree_id": _worktree_id(source["worktree_id"]),
        "write_scope_hash": _hash(source["write_scope_hash"], "write_scope_hash"),
        "latest_red": _summary(source["latest_red"], "latest_red"),
        "latest_green": _summary(source["latest_green"], "latest_green"),
        "contract_hashes": _normalised_list(
            source["contract_hashes"],
            "contract_hashes",
            _hash,
            required=True,
        ),
        "evidence_hashes": _normalised_list(
            source["evidence_hashes"],
            "evidence_hashes",
            _hash,
            required=True,
        ),
        "next_action": next_action,
        "redacted": True,
    }


def canonical_resume_packet(packet: Mapping[str, Any]) -> str:
    """Validate and return the canonical, redacted JSON resume packet."""

    if len(_json_bytes(packet)) > MAX_PACKET_BYTES:
        raise ValueError("resume packet exceeds its byte budget")
    canonical = _canonical_json(_validated_packet(packet))
    if len(canonical.encode("utf-8")) > MAX_PACKET_BYTES:
        raise ValueError("resume packet exceeds its byte budget")
    return canonical


def parse_resume_packet(payload: bytes | bytearray | str) -> dict[str, Any]:
    """Parse untrusted JSON bytes and return the canonical packet object."""

    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
    else:
        raise ValueError("resume packet must be UTF-8 JSON")
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError("resume packet exceeds its byte budget")
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("resume packet is not valid UTF-8 JSON") from error
    return json.loads(canonical_resume_packet(decoded))


def _status_task(value: object) -> dict[str, str]:
    task = _mapping(value, "workflow task")
    _exact_keys(task, frozenset({"task_id", "state", "branch"}), "workflow task")
    state = _text(task["state"], "state", maximum=32)
    if state not in {"pending_init", "running", "blocked", "done"}:
        raise ValueError("workflow task state is invalid")
    return {
        "task_id": _task_id(task["task_id"]),
        "state": state,
        "branch": _branch(task["branch"]),
    }


def _status_lease(value: object) -> dict[str, Any]:
    lease = _mapping(value, "lease")
    _exact_keys(lease, frozenset({"task_id", "lease_epoch", "endpoint"}), "lease")
    epoch = lease["lease_epoch"]
    if type(epoch) is not int or epoch < 1:
        raise ValueError("lease_epoch must be positive")
    return {
        "task_id": _task_id(lease["task_id"]),
        "lease_epoch": epoch,
        "endpoint": _endpoint(lease["endpoint"]),
    }


def _markdown_cell(value: str) -> str:
    safe = value.replace("\r", " ").replace("\n", " ").replace("\\", "\\\\")
    for marker in ("|", "[", "]", "(", ")", "<", ">", "`", "*", "#"):
        safe = safe.replace(marker, f"\\{marker}")
    return safe


def render_status_markdown(snapshot: Mapping[str, Any]) -> str:
    """Render a compact Markdown Todo view without raw command output."""

    if len(_json_bytes(snapshot)) > MAX_STATUS_BYTES:
        raise ValueError("status snapshot exceeds its byte budget")
    source = _mapping(snapshot, "status snapshot")
    _exact_keys(
        source, frozenset({"workflow", "leases", "resume_packets"}), "status snapshot"
    )
    workflow = _mapping(source["workflow"], "workflow")
    _exact_keys(workflow, frozenset({"workflow_id", "tasks"}), "workflow")
    workflow_id = _label(workflow["workflow_id"], "workflow_id")
    tasks_raw = workflow["tasks"]
    if not isinstance(tasks_raw, Sequence) or isinstance(
        tasks_raw, (str, bytes, bytearray)
    ):
        raise ValueError("workflow tasks must be a list")
    if len(tasks_raw) > MAX_STATUS_TASKS:
        raise ValueError("workflow task count exceeds its limit")
    tasks = sorted(
        (_status_task(task) for task in tasks_raw), key=lambda item: item["task_id"]
    )
    task_ids = [task["task_id"] for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("workflow task ids must be unique")

    leases_raw = source["leases"]
    if not isinstance(leases_raw, Sequence) or isinstance(
        leases_raw, (str, bytes, bytearray)
    ):
        raise ValueError("leases must be a list")
    if len(leases_raw) > MAX_STATUS_TASKS:
        raise ValueError("lease count exceeds its limit")
    leases = [_status_lease(lease) for lease in leases_raw]
    lease_by_task = {lease["task_id"]: lease for lease in leases}
    if len(lease_by_task) != len(leases) or not set(lease_by_task).issubset(task_ids):
        raise ValueError("leases must have unique known task ids")

    packets_raw = source["resume_packets"]
    if not isinstance(packets_raw, Sequence) or isinstance(
        packets_raw, (str, bytes, bytearray)
    ):
        raise ValueError("resume_packets must be a list")
    if len(packets_raw) > MAX_STATUS_TASKS:
        raise ValueError("resume packet count exceeds its limit")
    packets = [json.loads(canonical_resume_packet(packet)) for packet in packets_raw]
    packet_by_task = {packet["task_id"]: packet for packet in packets}
    if len(packet_by_task) != len(packets):
        raise ValueError("resume packets must have unique task ids")
    if any(packet["workflow_id"] != workflow_id for packet in packets):
        raise ValueError("resume packet workflow does not match")
    if not set(packet_by_task).issubset(task_ids):
        raise ValueError("resume packets must have known task ids")

    running = sum(task["state"] == "running" for task in tasks)
    pending = sum(task["state"] == "pending_init" for task in tasks)
    blocked = sum(task["state"] == "blocked" for task in tasks)
    done = sum(task["state"] == "done" for task in tasks)
    lines = [
        f"# Team Todo — {workflow_id}",
        "",
        f"- Active parallel execution: {running}",
        f"- Pending initialization: {pending}",
        f"- Blocked: {blocked}",
        f"- Done: {done}",
        "",
        "| Task | State | Branch | Lease | Latest test evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for task in tasks:
        task_id = task["task_id"]
        lease = lease_by_task.get(task_id)
        lease_text = f"epoch {lease['lease_epoch']}" if lease else "—"
        packet = packet_by_task.get(task_id)
        if packet:
            evidence = ", ".join(packet["evidence_hashes"])
            latest = f"{packet['latest_green']['result']}; {evidence}"
        else:
            latest = "—"
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    task_id,
                    task["state"],
                    task["branch"],
                    lease_text,
                    latest,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _contract(value: object, field: str) -> dict[str, str]:
    contract = _mapping(value, field)
    _exact_keys(contract, frozenset({"schema", "artifact_hash"}), field)
    return {
        "schema": _label(contract["schema"], f"{field}.schema"),
        "artifact_hash": _hash(contract["artifact_hash"], f"{field}.artifact_hash"),
    }


def contract_check(
    producer: Mapping[str, Any], consumer: Mapping[str, Any]
) -> dict[str, str | bool]:
    """Return compatibility only when both declared contract values match exactly."""

    try:
        produced = _contract(producer, "producer contract")
        expected = _contract(consumer, "consumer contract")
    except ValueError as error:
        raise ContractMismatchError("contract is invalid") from error
    if produced != expected:
        raise ContractMismatchError("contract artifact or schema is incompatible")
    return {"compatible": True, "schema": produced["schema"]}


def _fingerprint(value: object) -> dict[str, Any]:
    source = _mapping(value, "cache inputs")
    _exact_keys(source, _CACHE_FIELDS, "cache inputs")
    argv = source["argv"]
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes, bytearray)):
        raise ValueError("argv must be a list")
    if not argv or len(argv) > MAX_LIST_ITEMS:
        raise ValueError("argv is out of bounds")
    normalized_argv = []
    for item in argv:
        text = _text(item, "argv item", maximum=256)
        _reject_sensitive_or_absolute_text(text, "argv item")
        normalized_argv.append(text)
    toolchain = _text(
        source["toolchain_fingerprint"], "toolchain_fingerprint", maximum=256
    )
    platform = _text(
        source["platform_fingerprint"], "platform_fingerprint", maximum=256
    )
    for fingerprint in (toolchain, platform):
        if any(
            marker in fingerprint.casefold()
            for marker in ("partial", "unknown", "unset", "unavailable", "n/a")
        ):
            raise ValueError("cache fingerprint must be complete")
        _reject_sensitive_or_absolute_text(fingerprint, "cache fingerprint")
    locks = _mapping(source["dependency_lock_hashes"], "dependency_lock_hashes")
    if not locks or len(locks) > MAX_LIST_ITEMS:
        raise ValueError("dependency_lock_hashes is out of bounds")
    normalized_locks = {
        _relative_scope(name, "dependency lock name"): _hash(
            digest, "dependency lock hash"
        )
        for name, digest in locks.items()
    }
    if len(normalized_locks) != len(locks):
        raise ValueError("dependency lock names are not unique")
    return {
        "candidate_commit": _git_id(source["candidate_commit"], "candidate_commit"),
        "candidate_tree": _git_id(source["candidate_tree"], "candidate_tree"),
        "write_scope_hash": _hash(source["write_scope_hash"], "write_scope_hash"),
        "argv": normalized_argv,
        "toolchain_fingerprint": toolchain,
        "platform_fingerprint": platform,
        "dependency_lock_hashes": dict(sorted(normalized_locks.items())),
        "test_lane": _label(source["test_lane"], "test_lane"),
    }


def make_cache_metadata(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Create cache metadata for an exact verification fingerprint only."""

    fingerprint = _fingerprint(inputs)
    return {
        "schema": "team-efficiency/cache-v1",
        "cache_key": _sha256_json(fingerprint),
        "fingerprint": fingerprint,
    }


def is_exact_cache_hit(inputs: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
    """Fail closed unless every stored fingerprint value is exactly unchanged."""

    try:
        expected = make_cache_metadata(inputs)
        stored = _mapping(metadata, "cache metadata")
        _exact_keys(
            stored, frozenset({"schema", "cache_key", "fingerprint"}), "cache metadata"
        )
        if stored["schema"] != "team-efficiency/cache-v1":
            return False
        stored_key = _text(stored["cache_key"], "cache_key", maximum=80)
        stored_fingerprint = _fingerprint(stored["fingerprint"])
        return (
            hmac.compare_digest(stored_key, expected["cache_key"])
            and stored_fingerprint == expected["fingerprint"]
        )
    except (TypeError, ValueError):
        return False


def _manifest_text(value: object, field: str, *, maximum: int = 512) -> str:
    text = _text(value, field, maximum=maximum)
    _reject_sensitive_or_absolute_text(text, field)
    return text


def _manifest_source_kind(
    source: Mapping[str, Any],
    fields: frozenset[str],
    *,
    default: str,
    allowed: frozenset[str],
) -> str:
    if set(source) == set(fields):
        return default
    _exact_keys(source, fields | {"source_kind"}, "work-package manifest")
    source_kind = _text(source["source_kind"], "source_kind", maximum=64)
    if source_kind not in allowed:
        raise ValueError("work-package source_kind is invalid")
    return source_kind


def _artifact(value: object) -> dict[str, Any]:
    source = _mapping(value, "artifact boundary")
    _exact_keys(
        source,
        frozenset(
            {
                "task_id",
                "goal",
                "output_boundary",
                "write_scope",
                "depends_on",
                "required_evidence",
                "complexity",
                "execution_contracts",
            }
        ),
        "artifact boundary",
    )
    complexity = _text(source["complexity"], "complexity", maximum=32)
    if complexity not in _ROUTES:
        raise ValueError("artifact complexity is invalid")
    return {
        "task_id": _task_id(source["task_id"]),
        "goal": _manifest_text(source["goal"], "artifact goal"),
        "output_boundary": _manifest_text(source["output_boundary"], "output_boundary"),
        "write_scope": _normalised_scopes(source["write_scope"]),
        "depends_on": _normalised_list(
            source["depends_on"],
            "depends_on",
            lambda item, field: _task_id(item, field),
        ),
        "required_evidence": _normalised_list(
            source["required_evidence"],
            "required_evidence",
            _label,
            required=True,
        ),
        "recommended_route": _ROUTES[complexity],
        "execution_contracts": _normalised_list(
            source["execution_contracts"],
            "execution_contracts",
            _label,
            required=True,
        ),
        "direct_contract_hashes": [],
        "task_node_ids": [],
        "contract_node_ids": [],
    }


def _bounded_records(
    value: object,
    field: str,
    *,
    maximum: int,
    required: bool = False,
) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a list")
    if len(value) > maximum or (required and not value):
        raise ValueError(f"{field} is out of bounds")
    return list(value)


def _atlas_text(
    value: object,
    field: str,
    *,
    maximum: int = 256,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} must be bounded text")
    if not allow_empty and not value:
        raise ValueError(f"{field} must be bounded text")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must be single-line text")
    return value


def _atlas_optional_text(
    value: object, field: str, *, maximum: int = 256
) -> str | None:
    if value is None:
        return None
    return _atlas_text(value, field, maximum=maximum, allow_empty=True)


def _atlas_json(value: object, field: str) -> object:
    try:
        _canonical_json(value)
    except ValueError as error:
        raise ValueError(f"{field} must be JSON data") from error
    return value


def _atlas_hashes(
    value: object,
    field: str,
    *,
    maximum: int = MAX_LIST_ITEMS,
    required: bool = False,
) -> list[str]:
    return _normalised_list(
        value,
        field,
        _hash,
        maximum=maximum,
        required=required,
    )


def _derived_task_id(
    parent_task_id: str,
    source_prefix: str,
    identity: object,
) -> str:
    suffix = hashlib.sha256(_json_bytes(identity)).hexdigest()[:12].upper()
    reserved = len(source_prefix) + len(suffix) + 2
    parent = parent_task_id[: 96 - reserved].rstrip("-")
    return _task_id(f"{parent}-{source_prefix}-{suffix}", "derived task_id")


def _hash_label(namespace: str, digest: str) -> str:
    return f"atlas/{namespace}/{digest.removeprefix('sha256:')[:16]}"


def _atlas_operation(value: object) -> dict[str, str]:
    source = _mapping(value, "ImplementationPacket operation")
    _exact_keys(source, _ATLAS_OPERATION_FIELDS, "ImplementationPacket operation")
    target_symbol = _atlas_text(
        source["target_symbol_slot"],
        "ImplementationPacket operation target_symbol_slot",
        maximum=128,
        allow_empty=True,
    )
    if target_symbol:
        target_symbol = _label(
            target_symbol,
            "ImplementationPacket operation target_symbol_slot",
        )
    return {
        "kind": _atlas_text(
            source["kind"], "ImplementationPacket operation kind", maximum=64
        ),
        "path_slot": _label(
            source["path_slot"], "ImplementationPacket operation path_slot"
        ),
        "template_hash": _hash(
            source["template_hash"], "ImplementationPacket operation template_hash"
        ),
        "separator": _atlas_text(
            source["separator"],
            "ImplementationPacket operation separator",
            maximum=128,
            allow_empty=True,
        ),
        "target_symbol_slot": target_symbol,
    }


def _atlas_slot(value: object) -> dict[str, Any]:
    source = _mapping(value, "ImplementationPacket slot")
    _exact_keys(source, _ATLAS_SLOT_FIELDS, "ImplementationPacket slot")
    if type(source["required"]) is not bool:
        raise ValueError("ImplementationPacket slot required must be a boolean")
    return {
        "name": _label(source["name"], "ImplementationPacket slot name"),
        "type": _atlas_text(
            source["type"], "ImplementationPacket slot type", maximum=128
        ),
        "required": source["required"],
    }


def _atlas_constraint(value: object, field: str) -> dict[str, Any]:
    source = _mapping(value, field)
    _exact_keys(source, _ATLAS_CONSTRAINT_FIELDS, field)
    return {
        "kind": _atlas_text(source["kind"], f"{field} kind", maximum=128),
        "subject": _atlas_text(
            source["subject"],
            f"{field} subject",
            maximum=128,
            allow_empty=True,
        ),
        "value": _atlas_json(source["value"], f"{field} value"),
    }


def _atlas_dependency(value: object) -> dict[str, str]:
    source = _mapping(value, "ImplementationPacket dependency")
    _exact_keys(source, _ATLAS_DEPENDENCY_FIELDS, "ImplementationPacket dependency")
    return {
        "name": _atlas_text(
            source["name"], "ImplementationPacket dependency name", maximum=128
        ),
        "kind": _atlas_text(
            source["kind"], "ImplementationPacket dependency kind", maximum=128
        ),
        "specifier": _atlas_text(
            source["specifier"],
            "ImplementationPacket dependency specifier",
            maximum=256,
            allow_empty=True,
        ),
    }


def _atlas_test(value: object, field: str) -> dict[str, Any]:
    source = _mapping(value, field)
    _exact_keys(source, _ATLAS_TEST_FIELDS, field)
    argv = _bounded_records(
        source["argv"],
        f"{field} argv",
        maximum=MAX_LIST_ITEMS,
        required=True,
    )
    expected_exit_code = source["expected_exit_code"]
    if type(expected_exit_code) is not int:
        raise ValueError(f"{field} expected_exit_code must be an integer")
    return {
        "argv": [_atlas_text(item, f"{field} argv item", maximum=256) for item in argv],
        "expected_exit_code": expected_exit_code,
    }


def _atlas_evidence_window(value: object) -> dict[str, Any]:
    source = _mapping(value, "ImplementationPacket evidence window")
    allowed = frozenset({"path", "content_hash", "start_line", "end_line"})
    if not {"path", "content_hash"} <= set(source) or set(source) - allowed:
        raise ValueError("ImplementationPacket evidence window is invalid")
    result: dict[str, Any] = {
        "path": _relative_scope(
            source["path"], "ImplementationPacket evidence window path"
        ),
        "content_hash": _hash(
            source["content_hash"], "ImplementationPacket evidence window content_hash"
        ),
    }
    start_line = source.get("start_line")
    end_line = source.get("end_line")
    if start_line is not None:
        if type(start_line) is not int or start_line < 1:
            raise ValueError(
                "ImplementationPacket evidence window start_line is invalid"
            )
        result["start_line"] = start_line
    if end_line is not None:
        if type(end_line) is not int or end_line < result.get("start_line", 1):
            raise ValueError("ImplementationPacket evidence window end_line is invalid")
        result["end_line"] = end_line
    return result


def _atlas_packet(value: object) -> dict[str, Any]:
    source = _mapping(value, "ImplementationPacket")
    _exact_keys(source, _ATLAS_PACKET_FIELDS, "ImplementationPacket")
    packet_id = _hash(source["packet_id"], "ImplementationPacket packet_id")
    identity = dict(source)
    del identity["packet_id"]
    if not hmac.compare_digest(packet_id, _sha256_json(identity)):
        raise ValueError("ImplementationPacket packet_id does not match its fields")
    operations = [
        _atlas_operation(item)
        for item in _bounded_records(
            source["operations"],
            "ImplementationPacket operations",
            maximum=MAX_LIST_ITEMS,
        )
    ]
    slots = [
        _atlas_slot(item)
        for item in _bounded_records(
            source["slots"], "ImplementationPacket slots", maximum=MAX_LIST_ITEMS
        )
    ]
    constraints = [
        _atlas_constraint(item, "ImplementationPacket constraint")
        for item in _bounded_records(
            source["constraints"],
            "ImplementationPacket constraints",
            maximum=MAX_LIST_ITEMS,
        )
    ]
    dependencies = [
        _atlas_dependency(item)
        for item in _bounded_records(
            source["dependencies"],
            "ImplementationPacket dependencies",
            maximum=MAX_LIST_ITEMS,
        )
    ]
    tests = [
        _atlas_test(item, "ImplementationPacket test")
        for item in _bounded_records(
            source["tests"], "ImplementationPacket tests", maximum=MAX_LIST_ITEMS
        )
    ]
    gaps = [
        _atlas_text(item, "ImplementationPacket gap", maximum=128)
        for item in _bounded_records(
            source["gaps"], "ImplementationPacket gaps", maximum=MAX_LIST_ITEMS
        )
    ]
    evidence_windows = [
        _atlas_evidence_window(item)
        for item in _bounded_records(
            source["evidence_windows"],
            "ImplementationPacket evidence_windows",
            maximum=MAX_LIST_ITEMS,
        )
    ]
    if len({slot["name"] for slot in slots}) != len(slots):
        raise ValueError("ImplementationPacket slot names must be unique")
    return {
        "packet_id": packet_id,
        "trace_id": _hash(source["trace_id"], "ImplementationPacket trace_id"),
        "workspace": _atlas_text(
            source["workspace"], "ImplementationPacket workspace", maximum=512
        ),
        "snapshot_id": _hash(source["snapshot_id"], "ImplementationPacket snapshot_id"),
        "recipe_id": _hash(source["recipe_id"], "ImplementationPacket recipe_id"),
        "node_ids": _atlas_hashes(
            source["node_ids"], "ImplementationPacket node_ids", maximum=MAX_GRAPH_NODES
        ),
        "edge_ids": _atlas_hashes(
            source["edge_ids"], "ImplementationPacket edge_ids", maximum=MAX_GRAPH_EDGES
        ),
        "evidence_windows": evidence_windows,
        "evidence_hashes": _atlas_hashes(
            source["evidence_hashes"], "ImplementationPacket evidence_hashes"
        ),
        "operations": operations,
        "slots": slots,
        "constraints": constraints,
        "dependencies": dependencies,
        "tests": tests,
        "gaps": sorted(set(gaps)),
        "source_hashes": _atlas_hashes(
            source["source_hashes"], "ImplementationPacket source_hashes"
        ),
        "template_hashes": _atlas_hashes(
            source["template_hashes"], "ImplementationPacket template_hashes"
        ),
        "receipt_hashes": _atlas_hashes(
            source["receipt_hashes"], "ImplementationPacket receipt_hashes"
        ),
        "next_action": _atlas_text(
            source["next_action"], "ImplementationPacket next_action", maximum=128
        ),
    }


def _packet_path_bindings(
    value: object,
    path_slots: set[str],
) -> tuple[dict[str, str] | None, str]:
    source = _mapping(value, "path_bindings")
    if len(source) > MAX_LIST_ITEMS:
        raise ValueError("path_bindings is out of bounds")
    normalized_slots = {_label(slot, "path binding slot") for slot in source}
    if normalized_slots != path_slots:
        return (
            None,
            "ImplementationPacket path bindings do not cover its operation slots.",
        )
    bindings = {
        _label(slot, "path binding slot"): _relative_scope(path, "path binding value")
        for slot, path in source.items()
    }
    if len(bindings) != len(source):
        raise ValueError("path binding slots must be unique")
    return dict(sorted(bindings.items())), ""


def _packet_units(
    packet_value: object,
    bindings_value: object,
    parent_task_id: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    packet = _atlas_packet(packet_value)
    if (
        packet["gaps"]
        or packet["next_action"] != "code_atlas_render"
        or not packet["operations"]
        or not packet["tests"]
        or not packet["node_ids"]
        or not packet["edge_ids"]
        or not packet["evidence_hashes"]
        or not packet["source_hashes"]
        or not packet["receipt_hashes"]
    ):
        return None, "ImplementationPacket does not contain complete verified evidence."
    operation_hashes = {
        operation["template_hash"] for operation in packet["operations"]
    }
    if operation_hashes != set(packet["template_hashes"]):
        return None, "ImplementationPacket template evidence does not match operations."

    used_slots = {operation["path_slot"] for operation in packet["operations"]}
    slots_by_name = {slot["name"]: slot for slot in packet["slots"]}
    if any(
        slot_name not in slots_by_name
        or slots_by_name[slot_name]["type"] != "relative_python_path"
        for slot_name in used_slots
    ):
        return None, "ImplementationPacket lacks verified relative path slots."
    bindings, reason = _packet_path_bindings(bindings_value, used_slots)
    if bindings is None:
        return None, reason
    for constraint in packet["constraints"]:
        if constraint["kind"] != "path_suffix" or constraint["subject"] not in bindings:
            continue
        suffix = constraint["value"]
        if (
            not isinstance(suffix, str)
            or not suffix
            or not bindings[constraint["subject"]].endswith(suffix)
        ):
            return None, "ImplementationPacket path constraints do not prove bindings."

    operations_by_path: dict[str, list[dict[str, str]]] = {}
    for operation in sorted(
        packet["operations"],
        key=lambda item: (item["path_slot"], item["kind"], item["template_hash"]),
    ):
        path = bindings[operation["path_slot"]]
        operations_by_path.setdefault(path, []).append(operation)
    if len(operations_by_path) > MAX_MANIFEST_UNITS:
        raise ValueError("packet derives too many work units")

    task_by_path = {
        path: _derived_task_id(
            parent_task_id,
            "P",
            {"packet_id": packet["packet_id"], "path": path},
        )
        for path in operations_by_path
    }
    evidence = {
        *packet["evidence_hashes"],
        *packet["source_hashes"],
        *packet["template_hashes"],
        *packet["receipt_hashes"],
    }
    evidence.update(window["content_hash"] for window in packet["evidence_windows"])
    direct_contract_hashes = [
        _sha256_json(
            {
                "kind": "code_atlas_packet_execution_contract_v1",
                "packet_id": packet["packet_id"],
                "recipe_id": packet["recipe_id"],
            }
        )
    ]
    execution_contracts = sorted(
        {
            _hash_label("packet", packet["packet_id"]),
            *(_hash_label("contract", item) for item in direct_contract_hashes),
        }
    )
    acceptance_constraints = sorted(_sha256_json(test) for test in packet["tests"])
    units = []
    for path in sorted(operations_by_path):
        units.append(
            {
                "task_id": task_by_path[path],
                "goal": (
                    f"Apply {len(operations_by_path[path])} bounded "
                    f"Code Atlas operation(s) to {path}"
                ),
                "output_boundary": f"file {path}",
                "write_scope": [path],
                "depends_on": [],
                "required_evidence": sorted(evidence),
                "recommended_route": _ROUTES["routine"],
                "execution_contracts": execution_contracts,
                "direct_contract_hashes": direct_contract_hashes,
                "operation_count": len(operations_by_path[path]),
                "acceptance_constraints": acceptance_constraints,
                "task_node_ids": packet["node_ids"],
                "contract_node_ids": [],
                "unit_kind": "code",
            }
        )
    verification_id = _derived_task_id(
        parent_task_id,
        "V",
        {"packet_id": packet["packet_id"], "kind": "verification"},
    )
    units.append(
        {
            "task_id": verification_id,
            "goal": "Run ImplementationPacket verification constraints",
            "output_boundary": f"verification {packet['packet_id']}",
            "write_scope": [],
            "depends_on": sorted(task_by_path.values()),
            "required_evidence": sorted(evidence),
            "recommended_route": _ROUTES["moderate"],
            "execution_contracts": execution_contracts,
            "direct_contract_hashes": direct_contract_hashes,
            "operation_count": 0,
            "acceptance_constraints": acceptance_constraints,
            "task_node_ids": packet["node_ids"],
            "contract_node_ids": [],
            "unit_kind": "verification",
        }
    )
    return units, ""


def _atlas_node(value: object) -> dict[str, Any]:
    source = _mapping(value, "GraphQueryResult node")
    _exact_keys(source, _ATLAS_NODE_FIELDS, "GraphQueryResult node")
    kind = _atlas_text(source["kind"], "GraphQueryResult node kind", maximum=32)
    if kind not in _ATLAS_NODE_KINDS:
        raise ValueError("GraphQueryResult node kind is unsupported")
    provenance = _atlas_text(
        source["provenance"], "GraphQueryResult node provenance", maximum=32
    )
    if provenance not in {"observed", "resolved", "declared"}:
        raise ValueError("GraphQueryResult node provenance is invalid")
    if kind in {
        "TaskEpisode",
        "SourceEvidence",
        "TestSpec",
        "ExecutionReceipt",
    }:
        if provenance != "observed":
            raise AtlasEvidenceError("ATLAS_NODE_UNVERIFIED")
        if source["quarantine_state"] is not None:
            raise AtlasEvidenceError("ATLAS_NODE_QUARANTINED")
        if source["superseded_at"] is not None:
            raise AtlasEvidenceError("ATLAS_NODE_SUPERSEDED")
        if not source["source_hashes"]:
            raise AtlasEvidenceError("ATLAS_SOURCE_HASH_INCOMPLETE")
    payload = _atlas_json(source["payload"], "GraphQueryResult node payload")
    node_id = _hash(source["node_id"], "GraphQueryResult node_id")
    identity = {
        "kind": kind,
        "schema_version": _atlas_text(
            source["schema_version"], "GraphQueryResult node schema_version", maximum=32
        ),
        "extractor_id": _atlas_text(
            source["extractor_id"],
            "GraphQueryResult node extractor_id",
            maximum=128,
            allow_empty=True,
        ),
        "extractor_version": _atlas_text(
            source["extractor_version"],
            "GraphQueryResult node extractor_version",
            maximum=128,
            allow_empty=True,
        ),
        "provenance": provenance,
        "payload": payload,
        "source_hashes": list(source["source_hashes"]),
    }
    if not hmac.compare_digest(node_id, _sha256_json(identity)):
        raise ValueError("GraphQueryResult node_id does not match its fields")
    return {
        "node_id": node_id,
        "kind": kind,
        "payload": payload,
        "source_hashes": _atlas_hashes(
            source["source_hashes"],
            "GraphQueryResult node source_hashes",
            maximum=MAX_LIST_ITEMS,
        ),
        "schema_version": identity["schema_version"],
        "extractor_id": identity["extractor_id"],
        "extractor_version": identity["extractor_version"],
        "provenance": provenance,
        "created_at": _atlas_optional_text(
            source["created_at"], "GraphQueryResult node created_at", maximum=64
        ),
        "superseded_at": _atlas_optional_text(
            source["superseded_at"], "GraphQueryResult node superseded_at", maximum=64
        ),
        "quarantine_state": _atlas_optional_text(
            source["quarantine_state"],
            "GraphQueryResult node quarantine_state",
            maximum=64,
        ),
    }


def _atlas_edge(value: object) -> dict[str, Any]:
    source = _mapping(value, "GraphQueryResult edge")
    _exact_keys(source, _ATLAS_EDGE_FIELDS, "GraphQueryResult edge")
    relation = _atlas_text(
        source["relation"], "GraphQueryResult edge relation", maximum=32
    )
    if relation not in _ATLAS_EDGE_RELATIONS:
        raise ValueError("GraphQueryResult edge relation is unsupported")
    source_kind = _atlas_text(
        source["source_kind"], "GraphQueryResult edge source_kind", maximum=32
    )
    target_kind = _atlas_text(
        source["target_kind"], "GraphQueryResult edge target_kind", maximum=32
    )
    expected_sources, expected_targets = _ATLAS_EDGE_ENDPOINTS[relation]
    if source_kind not in expected_sources or target_kind not in expected_targets:
        raise ValueError("GraphQueryResult edge has invalid endpoint kinds")
    provenance = _atlas_text(
        source["provenance"], "GraphQueryResult edge provenance", maximum=32
    )
    if provenance not in {"observed", "resolved", "declared"}:
        raise ValueError("GraphQueryResult edge provenance is invalid")
    trust_edge = (
        source_kind == "TaskEpisode"
        and relation in {"CHANGES", "VERIFIED_BY", "SOLVES"}
    ) or relation in {"TESTS", "SUPERSEDES"}
    if trust_edge and provenance != "observed":
        raise AtlasEvidenceError("ATLAS_EDGE_UNVERIFIED")
    payload = _atlas_json(source["payload"], "GraphQueryResult edge payload")
    edge_id = _hash(source["edge_id"], "GraphQueryResult edge_id")
    identity = {
        "relation": relation,
        "source_id": source["source_id"],
        "target_id": source["target_id"],
        "schema_version": source["schema_version"],
        "provenance": provenance,
        "payload": payload,
    }
    if not hmac.compare_digest(edge_id, _sha256_json(identity)):
        raise ValueError("GraphQueryResult edge_id does not match its fields")
    return {
        "edge_id": edge_id,
        "relation": relation,
        "source_id": _hash(source["source_id"], "GraphQueryResult edge source_id"),
        "target_id": _hash(source["target_id"], "GraphQueryResult edge target_id"),
        "source_kind": source_kind,
        "target_kind": target_kind,
        "payload": payload,
        "schema_version": _atlas_text(
            source["schema_version"],
            "GraphQueryResult edge schema_version",
            maximum=32,
        ),
        "provenance": provenance,
        "created_at": _atlas_optional_text(
            source["created_at"], "GraphQueryResult edge created_at", maximum=64
        ),
    }


def _atlas_graph(value: object) -> dict[str, Any]:
    source = _mapping(value, "GraphQueryResult")
    _exact_keys(source, _ATLAS_GRAPH_FIELDS, "GraphQueryResult")
    if type(source["truncated"]) is not bool:
        raise ValueError("GraphQueryResult truncated must be a boolean")
    nodes = [
        _atlas_node(item)
        for item in _bounded_records(
            source["nodes"],
            "GraphQueryResult nodes",
            maximum=MAX_GRAPH_NODES,
        )
    ]
    edges = [
        _atlas_edge(item)
        for item in _bounded_records(
            source["edges"],
            "GraphQueryResult edges",
            maximum=MAX_GRAPH_EDGES,
        )
    ]
    nodes_by_id = {node["node_id"]: node for node in nodes}
    if len(nodes_by_id) != len(nodes):
        raise ValueError("GraphQueryResult node ids must be unique")
    if len({edge["edge_id"] for edge in edges}) != len(edges):
        raise ValueError("GraphQueryResult edge ids must be unique")
    for edge in edges:
        if edge["source_id"] not in nodes_by_id or edge["target_id"] not in nodes_by_id:
            raise ValueError("GraphQueryResult edge references an unknown node")
        source_node = nodes_by_id[edge["source_id"]]
        target_node = nodes_by_id[edge["target_id"]]
        if (
            source_node["kind"] != edge["source_kind"]
            or target_node["kind"] != edge["target_kind"]
        ):
            raise ValueError("GraphQueryResult edge node kinds do not match")
    return {
        "truncated": source["truncated"],
        "nodes": sorted(nodes, key=lambda item: item["node_id"]),
        "edges": sorted(edges, key=lambda item: item["edge_id"]),
    }


def _require_observed_node(node: Mapping[str, Any]) -> None:
    if node["provenance"] != "observed":
        raise AtlasEvidenceError("ATLAS_NODE_UNVERIFIED")
    if node["quarantine_state"] is not None:
        raise AtlasEvidenceError("ATLAS_NODE_QUARANTINED")
    if node["superseded_at"] is not None:
        raise AtlasEvidenceError("ATLAS_NODE_SUPERSEDED")
    if not node["source_hashes"]:
        raise AtlasEvidenceError("ATLAS_SOURCE_HASH_INCOMPLETE")


def _require_observed_edge(edge: Mapping[str, Any]) -> None:
    if edge["provenance"] != "observed":
        raise AtlasEvidenceError("ATLAS_EDGE_UNVERIFIED")


def _payload_mapping(
    value: object,
    expected: frozenset[str],
    code: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise AtlasEvidenceError(code)
    return value


def _proof_hash(value: object, code: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    try:
        return _hash(value, code)
    except ValueError:
        raise AtlasEvidenceError(code) from None


def _task_episode_payload(node: Mapping[str, Any]) -> None:
    payload = _payload_mapping(
        node["payload"],
        frozenset(
            {
                "workflow_id_hash",
                "task_id_hash",
                "acceptance_id_hash",
                "workspace_hash",
                "checkpoint_id_hash",
                "input_snapshot_id_hash",
                "output_snapshot_id_hash",
                "task_kind",
            }
        ),
        "ATLAS_TASK_EPISODE_INVALID",
    )
    if payload["task_kind"] != "code":
        raise AtlasEvidenceError("ATLAS_TASK_EPISODE_INVALID")
    for field in (
        "workflow_id_hash",
        "task_id_hash",
        "acceptance_id_hash",
        "workspace_hash",
        "checkpoint_id_hash",
        "input_snapshot_id_hash",
        "output_snapshot_id_hash",
    ):
        _proof_hash(payload[field], "ATLAS_TASK_EPISODE_INVALID")


def _source_evidence_path(node: Mapping[str, Any]) -> str | None:
    _require_observed_node(node)
    payload = node["payload"]
    if not isinstance(payload, Mapping):
        raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID")
    kind = payload.get("kind")
    if kind == "task_change_set":
        summary = _payload_mapping(
            payload,
            frozenset(
                {
                    "kind",
                    "path_count",
                    "input_snapshot_id_hash",
                    "output_snapshot_id_hash",
                }
            ),
            "ATLAS_SOURCE_EVIDENCE_INVALID",
        )
        if type(summary["path_count"]) is not int or summary["path_count"] < 1:
            raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID")
        _proof_hash(
            summary["input_snapshot_id_hash"],
            "ATLAS_SOURCE_EVIDENCE_INVALID",
        )
        _proof_hash(
            summary["output_snapshot_id_hash"],
            "ATLAS_SOURCE_EVIDENCE_INVALID",
        )
        return None
    if kind == "index_node":
        indexed = _payload_mapping(
            payload,
            frozenset(
                {
                    "kind",
                    "path",
                    "node_id_hash",
                    "content_hash",
                    "start_byte",
                    "end_byte",
                    "name_hash",
                }
            ),
            "ATLAS_SOURCE_EVIDENCE_INVALID",
        )
        for field in ("node_id_hash", "content_hash", "name_hash"):
            _proof_hash(indexed[field], "ATLAS_SOURCE_EVIDENCE_INVALID")
        if (
            type(indexed["start_byte"]) is not int
            or type(indexed["end_byte"]) is not int
            or indexed["start_byte"] < 0
            or indexed["end_byte"] < indexed["start_byte"]
        ):
            raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID")
        try:
            return _relative_scope(indexed["path"], "SourceEvidence path")
        except ValueError:
            raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID") from None

    changed = _payload_mapping(
        payload,
        frozenset(
            {
                "path",
                "before_hash",
                "after_hash",
                "before_bytes",
                "after_bytes",
            }
        ),
        "ATLAS_SOURCE_EVIDENCE_INVALID",
    )
    before_hash = _proof_hash(
        changed["before_hash"],
        "ATLAS_SOURCE_EVIDENCE_INVALID",
        allow_empty=True,
    )
    after_hash = _proof_hash(
        changed["after_hash"],
        "ATLAS_SOURCE_EVIDENCE_INVALID",
        allow_empty=True,
    )
    if not before_hash and not after_hash:
        raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID")
    if (
        type(changed["before_bytes"]) is not int
        or type(changed["after_bytes"]) is not int
        or changed["before_bytes"] < 0
        or changed["after_bytes"] < 0
    ):
        raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID")
    try:
        return _relative_scope(changed["path"], "SourceEvidence path")
    except ValueError:
        raise AtlasEvidenceError("ATLAS_SOURCE_EVIDENCE_INVALID") from None


def _test_spec_proof(node: Mapping[str, Any]) -> tuple[str, str]:
    _require_observed_node(node)
    payload = node["payload"]
    if not isinstance(payload, Mapping):
        raise AtlasEvidenceError("ATLAS_VERIFICATION_UNVERIFIED")
    if payload.get("kind") == "bound_verification":
        bound = _payload_mapping(
            payload,
            frozenset({"kind", "expected_exit_code"}),
            "ATLAS_VERIFICATION_UNVERIFIED",
        )
        if (
            type(bound["expected_exit_code"]) is not int
            or bound["expected_exit_code"] != 0
        ):
            raise AtlasEvidenceError("ATLAS_VERIFICATION_UNVERIFIED")
        return "bound_verification", ""
    if payload.get("kind") == "command_receipt":
        command = _payload_mapping(
            payload,
            frozenset({"kind", "command_spec_hash", "expected_exit_code"}),
            "ATLAS_VERIFICATION_UNVERIFIED",
        )
        if (
            type(command["expected_exit_code"]) is not int
            or command["expected_exit_code"] != 0
        ):
            raise AtlasEvidenceError("ATLAS_VERIFICATION_UNVERIFIED")
        return (
            "command_receipt",
            _proof_hash(
                command["command_spec_hash"],
                "ATLAS_VERIFICATION_UNVERIFIED",
            ),
        )
    raise AtlasEvidenceError("ATLAS_VERIFICATION_UNVERIFIED")


def _receipt_proof(node: Mapping[str, Any]) -> tuple[str, str | int]:
    _require_observed_node(node)
    payload = node["payload"]
    if not isinstance(payload, Mapping):
        raise AtlasEvidenceError("ATLAS_RECEIPT_UNVERIFIED")
    if payload.get("kind") == "bound_receipt_summary":
        summary = _payload_mapping(
            payload,
            frozenset({"kind", "receipt_count"}),
            "ATLAS_RECEIPT_UNVERIFIED",
        )
        count = summary["receipt_count"]
        if type(count) is not int or count < 2:
            raise AtlasEvidenceError("ATLAS_RECEIPT_UNVERIFIED")
        return "bound_receipt_summary", count

    receipt = _payload_mapping(
        payload,
        frozenset(
            {
                "receipt_id_hash",
                "kind",
                "command_spec_hash",
                "input_hash",
                "output_hash",
                "exit_code",
                "success",
            }
        ),
        "ATLAS_RECEIPT_UNVERIFIED",
    )
    kind = receipt["kind"]
    if kind not in {"command", "write"}:
        raise AtlasEvidenceError("ATLAS_RECEIPT_UNVERIFIED")
    for field in (
        "receipt_id_hash",
        "command_spec_hash",
        "input_hash",
        "output_hash",
    ):
        _proof_hash(receipt[field], "ATLAS_RECEIPT_UNVERIFIED")
    if (
        receipt["success"] is not True
        or type(receipt["exit_code"]) is not int
        or receipt["exit_code"] != 0
    ):
        raise AtlasEvidenceError("ATLAS_RECEIPT_UNVERIFIED")
    return kind, str(receipt["command_spec_hash"])


def _episode_units(
    graph_value: object,
    parent_task_id: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    graph = _atlas_graph(graph_value)
    if graph["truncated"]:
        raise AtlasEvidenceError("ATLAS_GRAPH_TRUNCATED")
    nodes_by_id = {node["node_id"]: node for node in graph["nodes"]}
    episode_ids = [
        node_id
        for node_id, node in sorted(nodes_by_id.items())
        if node["kind"] == "TaskEpisode"
    ]
    if not episode_ids:
        raise AtlasEvidenceError("ATLAS_TASK_EPISODE_MISSING")
    if len(episode_ids) >= MAX_MANIFEST_UNITS:
        raise AtlasEvidenceError("ATLAS_UNIT_BUDGET_EXCEEDED")

    change_edges: dict[str, list[dict[str, Any]]] = {
        node_id: [] for node_id in episode_ids
    }
    verification_edges: dict[str, list[dict[str, Any]]] = {
        node_id: [] for node_id in episode_ids
    }
    test_edges: dict[str, list[dict[str, Any]]] = {}
    for edge in graph["edges"]:
        relation = edge["relation"]
        source_id = edge["source_id"]
        if relation == "SUPERSEDES":
            _require_observed_edge(edge)
        if relation == "CHANGES" and source_id in change_edges:
            _require_observed_edge(edge)
            change_edges[source_id].append(edge)
        elif relation == "VERIFIED_BY" and source_id in verification_edges:
            _require_observed_edge(edge)
            verification_edges[source_id].append(edge)
        elif relation == "SOLVES" and source_id in change_edges:
            _require_observed_edge(edge)
        elif relation == "TESTS":
            test_edges.setdefault(source_id, []).append(edge)

    scopes_by_episode: dict[str, set[str]] = {}
    evidence_by_episode: dict[str, set[str]] = {}
    acceptance_by_episode: dict[str, set[str]] = {}
    contract_nodes_by_episode: dict[str, list[str]] = {}
    contracts_by_episode: dict[str, list[str]] = {}
    for episode_id in episode_ids:
        episode = nodes_by_id[episode_id]
        _require_observed_node(episode)
        _task_episode_payload(episode)
        scopes: set[str] = set()
        evidence = {episode_id}
        participating_node_ids = {episode_id}
        participating_edge_ids: set[str] = set()
        changed_source_ids: set[str] = set()
        for edge in change_edges[episode_id]:
            source = nodes_by_id[edge["target_id"]]
            path = _source_evidence_path(source)
            if path is not None:
                scopes.add(path)
            changed_source_ids.add(source["node_id"])
            participating_node_ids.add(source["node_id"])
            participating_edge_ids.add(edge["edge_id"])
            evidence.add(source["node_id"])
            evidence.add(edge["edge_id"])
        if not scopes:
            raise AtlasEvidenceError("ATLAS_CHANGED_PATH_UNPROVEN")

        acceptance: set[str] = set()
        verification_node_ids: set[str] = set()
        command_receipt_hashes: set[str] = set()
        command_test_hashes: set[str] = set()
        receipt_kinds: list[str] = []
        receipt_summaries: list[int] = []
        bound_verification = False
        test_node_ids: set[str] = set()
        for edge in verification_edges[episode_id]:
            verification = nodes_by_id[edge["target_id"]]
            if verification["kind"] == "TestSpec":
                proof_kind, proof_hash = _test_spec_proof(verification)
                test_node_ids.add(verification["node_id"])
                if proof_kind == "bound_verification":
                    bound_verification = True
                else:
                    command_test_hashes.add(proof_hash)
            elif verification["kind"] == "ExecutionReceipt":
                proof_kind, proof_value = _receipt_proof(verification)
                if proof_kind == "bound_receipt_summary":
                    receipt_summaries.append(int(proof_value))
                else:
                    receipt_kinds.append(proof_kind)
                    if proof_kind == "command":
                        command_receipt_hashes.add(str(proof_value))
            else:
                raise AtlasEvidenceError("ATLAS_VERIFICATION_UNVERIFIED")
            verification_node_ids.add(verification["node_id"])
            participating_node_ids.add(verification["node_id"])
            participating_edge_ids.add(edge["edge_id"])
            acceptance.add(verification["node_id"])
            evidence.add(verification["node_id"])
            evidence.add(edge["edge_id"])
        if (
            not acceptance
            or not bound_verification
            or len(receipt_summaries) != 1
            or receipt_summaries[0] != len(receipt_kinds)
            or "command" not in receipt_kinds
            or "write" not in receipt_kinds
            or command_test_hashes != command_receipt_hashes
        ):
            raise AtlasEvidenceError("ATLAS_RECEIPT_UNVERIFIED")

        for test_node_id in test_node_ids:
            covered = set()
            for edge in test_edges.get(test_node_id, []):
                if edge["target_id"] not in changed_source_ids:
                    continue
                _require_observed_edge(edge)
                covered.add(edge["target_id"])
                participating_edge_ids.add(edge["edge_id"])
                evidence.add(edge["edge_id"])
            if covered != changed_source_ids:
                raise AtlasEvidenceError("ATLAS_VERIFICATION_UNVERIFIED")

        contract_hash = _sha256_json(
            {
                "kind": "code_atlas_task_episode_execution_contract_v1",
                "task_episode_node_id": episode_id,
                "node_ids": sorted(participating_node_ids),
                "edge_ids": sorted(participating_edge_ids),
            }
        )
        scopes_by_episode[episode_id] = scopes
        evidence_by_episode[episode_id] = evidence
        acceptance_by_episode[episode_id] = acceptance
        contract_nodes_by_episode[episode_id] = sorted(verification_node_ids)
        contracts_by_episode[episode_id] = [contract_hash]

    task_by_episode = {
        node_id: _derived_task_id(
            parent_task_id,
            "E",
            {
                "execution_contract_hash": contracts_by_episode[node_id][0],
                "task_episode_node_id": node_id,
            },
        )
        for node_id in episode_ids
    }
    units = []
    for node_id in episode_ids:
        execution_contracts = {
            _hash_label("episode", node_id),
            *(
                _hash_label("contract", contract_hash)
                for contract_hash in contracts_by_episode[node_id]
            ),
        }
        units.append(
            {
                "task_id": task_by_episode[node_id],
                "goal": f"Execute verified TaskEpisode {node_id[:24]}",
                "output_boundary": f"TaskEpisode {node_id}",
                "write_scope": sorted(scopes_by_episode[node_id]),
                "depends_on": [],
                "required_evidence": sorted(evidence_by_episode[node_id]),
                "recommended_route": _ROUTES["routine"],
                "execution_contracts": sorted(execution_contracts),
                "direct_contract_hashes": sorted(contracts_by_episode[node_id]),
                "task_node_ids": [node_id],
                "contract_node_ids": contract_nodes_by_episode[node_id],
                "acceptance_constraints": sorted(acceptance_by_episode[node_id]),
                "unit_kind": "code",
            }
        )
    all_evidence = sorted(
        {
            evidence
            for episode_id in episode_ids
            for evidence in evidence_by_episode[episode_id]
        }
    )
    all_contracts = sorted(
        {
            contract
            for episode_id in episode_ids
            for contract in contracts_by_episode[episode_id]
        }
    )
    verification_identity = _sha256_json(
        {
            "kind": "code_atlas_graph_verification_execution_contract_v1",
            "direct_contract_hashes": all_contracts,
            "task_episode_node_ids": episode_ids,
        }
    )
    verification_id = _derived_task_id(
        parent_task_id,
        "V",
        {"execution_contract_hash": verification_identity},
    )
    units.append(
        {
            "task_id": verification_id,
            "goal": "Run TaskEpisode graph verification constraints",
            "output_boundary": f"verification {verification_identity}",
            "write_scope": [],
            "depends_on": sorted(task_by_episode.values()),
            "required_evidence": all_evidence,
            "recommended_route": _ROUTES["moderate"],
            "execution_contracts": sorted(
                {
                    _hash_label("graph", verification_identity),
                    *(_hash_label("contract", item) for item in all_contracts),
                }
            ),
            "direct_contract_hashes": all_contracts,
            "task_node_ids": episode_ids,
            "contract_node_ids": sorted(
                {
                    node_id
                    for episode_id in episode_ids
                    for node_id in contract_nodes_by_episode[episode_id]
                }
            ),
            "acceptance_constraints": sorted(
                {
                    constraint
                    for episode_id in episode_ids
                    for constraint in acceptance_by_episode[episode_id]
                }
            ),
            "unit_kind": "verification",
        }
    )
    return units, ""


def _ensure_acyclic(units: Mapping[str, Mapping[str, Any]]) -> None:
    remaining = {task_id: set(unit["depends_on"]) for task_id, unit in units.items()}
    completed: set[str] = set()
    while remaining:
        ready = sorted(
            task_id
            for task_id, dependencies in remaining.items()
            if dependencies <= completed
        )
        if not ready:
            raise ValueError("dependency graph contains a cycle")
        for task_id in ready:
            completed.add(task_id)
            del remaining[task_id]


def _scope_conflicts(left: Sequence[str], right: Sequence[str]) -> bool:
    for first in left:
        first_parts = first.split("/")
        for second in right:
            second_parts = second.split("/")
            shared = min(len(first_parts), len(second_parts))
            if first_parts[:shared] == second_parts[:shared]:
                return True
    return False


def _conflict_graph(units: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    graph = {unit["task_id"]: [] for unit in units}
    for index, left in enumerate(units):
        for right in units[index + 1 :]:
            if _scope_conflicts(left["write_scope"], right["write_scope"]):
                graph[left["task_id"]].append(right["task_id"])
                graph[right["task_id"]].append(left["task_id"])
    return {task_id: sorted(neighbors) for task_id, neighbors in sorted(graph.items())}


def _maximal_ready_wave(
    ready: Sequence[Mapping[str, Any]],
    graph: Mapping[str, Sequence[str]],
    capacity: int,
) -> list[str]:
    ordered = sorted(ready, key=lambda unit: unit["task_id"])
    best: tuple[str, ...] = ()

    def consider(selected: tuple[str, ...]) -> None:
        nonlocal best
        if len(selected) > len(best) or (
            len(selected) == len(best) and selected < best
        ):
            best = selected

    def search(index: int, selected: tuple[str, ...]) -> None:
        if len(selected) == capacity or index == len(ordered):
            consider(selected)
            return
        if len(selected) + len(ordered) - index < len(best):
            return
        candidate = ordered[index]["task_id"]
        if all(candidate not in graph[chosen] for chosen in selected):
            search(index + 1, selected + (candidate,))
        search(index + 1, selected)

    search(0, ())
    return list(best)


_HOST_BINDING_DESCRIPTIONS = {
    "workflow_id": "Existing workflow identifier selected by the orchestration host.",
    "owner": "Claim owner identity selected by the orchestration host.",
    "expires_at": "Lease expiry timestamp selected by the orchestration host.",
    "lease_epoch": "Lease epoch returned by workflow_claim.",
    "host_target": "Canonical collaboration target selected by the orchestration host.",
    "now": "Optional host clock value; omit to use the orchestration service clock.",
    "workspace_root": "Canonical workspace root resolved by the orchestration host.",
    "input_snapshot_id": "Current input snapshot identifier resolved by the host.",
}


def _host_binding(reference: str) -> dict[str, str]:
    return {
        "source": "host",
        "ref": reference,
        "description": _HOST_BINDING_DESCRIPTIONS[reference],
    }


def _registration_plan(
    units: Sequence[Mapping[str, Any]],
    waves: Sequence[Sequence[Mapping[str, Any]]],
    *,
    strict_index: bool,
) -> dict[str, Any]:
    unit_by_id = {unit["task_id"]: unit for unit in units}
    wave_task_ids = [[unit["task_id"] for unit in wave] for wave in waves]
    if any(task_ids != sorted(task_ids) for task_ids in wave_task_ids):
        raise ValueError("execution wave task ids must use stable task-id order")
    registration_order = [task_id for task_ids in wave_task_ids for task_id in task_ids]
    if len(registration_order) != len(unit_by_id) or set(registration_order) != set(
        unit_by_id
    ):
        raise ValueError(
            "registration order must cover every derived unit exactly once"
        )
    registered: set[str] = set()
    for task_id in registration_order:
        if not set(unit_by_id[task_id]["depends_on"]) <= registered:
            raise ValueError("registration order must place dependencies first")
        registered.add(task_id)

    register_steps = []
    task_steps: dict[str, dict[str, Any]] = {}
    for task_id in registration_order:
        unit = unit_by_id[task_id]
        input_hash = _sha256_json(
            {
                "kind": "team_efficiency_task_input_v1",
                "task_id": unit["task_id"],
                "dependencies": unit["depends_on"],
                "write_scope": unit["write_scope"],
                "direct_contract_hashes": unit["direct_contract_hashes"],
                "task_node_ids": unit["task_node_ids"],
                "contract_node_ids": unit["contract_node_ids"],
                "required_evidence": unit["required_evidence"],
            }
        )
        card = _canonical_json(
            {
                "schema": "team-efficiency/task-card-v1",
                "task_id": unit["task_id"],
                "goal": unit["goal"],
                "output_boundary": unit["output_boundary"],
                "unit_kind": unit.get("unit_kind", "artifact"),
                "execution_contracts": unit["execution_contracts"],
            }
        )
        if len(card.encode("utf-8")) > MAX_REGISTRATION_CARD_BYTES:
            raise ValueError("registration card exceeds its byte budget")
        register_steps.append(
            {
                "tool": "workflow_register_task",
                "arguments": {
                    "workflow_id": _host_binding("workflow_id"),
                    "task_id": unit["task_id"],
                    "title": unit["goal"],
                    "owner_role": unit["recommended_route"],
                    "card": card,
                    "dependencies": list(unit["depends_on"]),
                    "write_scope": list(unit["write_scope"]),
                    "direct_contract_hashes": list(unit["direct_contract_hashes"]),
                    "required_evidence": list(unit["required_evidence"]),
                    "input_hash": input_hash,
                    "strict_index": strict_index,
                    "workspace_root": _host_binding("workspace_root"),
                    "input_snapshot_id": _host_binding("input_snapshot_id"),
                    "task_node_ids": list(unit["task_node_ids"]),
                    "contract_node_ids": list(unit["contract_node_ids"]),
                },
                "host_bound_fields": [
                    "input_snapshot_id",
                    "workflow_id",
                    "workspace_root",
                ],
            }
        )
        task_steps[task_id] = {
            "task_id": task_id,
            "workflow_claim": {
                "tool": "workflow_claim",
                "arguments": {
                    "task_id": task_id,
                    "owner": _host_binding("owner"),
                    "expires_at": _host_binding("expires_at"),
                    "host_target": _host_binding("host_target"),
                    "now": _host_binding("now"),
                },
                "host_bound_fields": [
                    "expires_at",
                    "host_target",
                    "now",
                    "owner",
                ],
            },
            "workflow_endpoint_bind": {
                "tool": "workflow_endpoint_bind",
                "arguments": {
                    "workflow_id": _host_binding("workflow_id"),
                    "task_id": task_id,
                    "owner": _host_binding("owner"),
                    "lease_epoch": _host_binding("lease_epoch"),
                    "host_target": _host_binding("host_target"),
                    "now": _host_binding("now"),
                },
                "host_bound_fields": [
                    "host_target",
                    "lease_epoch",
                    "now",
                    "owner",
                    "workflow_id",
                ],
            },
        }

    execution_waves = []
    for index, task_ids in enumerate(wave_task_ids):
        execution_waves.append(
            {
                "wave_index": index + 1,
                "task_ids": task_ids,
                "workflow_ready": {
                    "tool": "workflow_ready",
                    "arguments": {
                        "workflow_id": _host_binding("workflow_id"),
                    },
                    "host_bound_fields": ["workflow_id"],
                },
                "ready_result_policy": {
                    "allow_empty_result": True,
                    "require_exact_task_set": False,
                    "claim_precondition": "READY",
                },
                "task_steps": [task_steps[task_id] for task_id in task_ids],
                "completion_barrier": {
                    "condition": "all_tasks_reach_state",
                    "task_ids": task_ids,
                    "required_state": "DONE",
                    "advance_to_wave_index": (
                        index + 2 if index + 1 < len(wave_task_ids) else None
                    ),
                },
            }
        )
    return {
        "schema": "team-efficiency/workflow-lifecycle-plan-v1",
        "registration_order": registration_order,
        "register_steps": register_steps,
        "execution_waves": execution_waves,
    }


def _needs_design_plan(
    *,
    task_id: str,
    goal: str,
    capacity: int,
    source_kind: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": "team-efficiency/decomposition-plan-v1",
        "status": "needs_design",
        "task_id": task_id,
        "goal": goal,
        "capacity": capacity,
        "source_kind": source_kind,
        "reason": reason,
        "units": [],
        "conflict_graph": {},
        "waves": [],
        "registration_plan": _registration_plan((), (), strict_index=False),
    }


def _scheduled_plan(
    *,
    task_id: str,
    goal: str,
    capacity: int,
    source_kind: str,
    units: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    units = sorted(units, key=lambda unit: unit["task_id"])
    unit_by_id = {unit["task_id"]: unit for unit in units}
    if len(unit_by_id) != len(units):
        raise ValueError("derived task ids must be unique")
    boundaries = [unit["output_boundary"] for unit in units]
    if len(set(boundaries)) != len(boundaries):
        raise ValueError("derived output boundaries must be unique")
    for unit in units:
        dependencies = unit["depends_on"]
        if unit["task_id"] in dependencies or not set(dependencies).issubset(
            unit_by_id
        ):
            raise ValueError("derived dependencies must name other known units")
    _ensure_acyclic(unit_by_id)
    graph = _conflict_graph(units)

    remaining = dict(unit_by_id)
    completed: set[str] = set()
    waves: list[list[dict[str, Any]]] = []
    while remaining:
        ready = [
            unit
            for task_id, unit in sorted(remaining.items())
            if set(unit["depends_on"]) <= completed
        ]
        if not ready:
            raise ValueError("dependency graph cannot be scheduled")
        selected_ids = _maximal_ready_wave(ready, graph, capacity)
        if not selected_ids:
            raise ValueError("ready work cannot be scheduled")
        wave = [remaining.pop(task_id) for task_id in selected_ids]
        waves.append(wave)
        completed.update(selected_ids)

    return {
        "schema": "team-efficiency/decomposition-plan-v1",
        "status": "planned",
        "task_id": task_id,
        "goal": goal,
        "capacity": capacity,
        "source_kind": source_kind,
        "units": units,
        "conflict_graph": graph,
        "waves": waves,
        "registration_plan": _registration_plan(
            units,
            waves,
            strict_index=source_kind in _ATLAS_SOURCE_KINDS,
        ),
    }


def decompose(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Compile manual boundaries or verified Code Atlas evidence into safe waves."""

    source = _mapping(manifest, "work-package manifest")
    common = frozenset({"schema", "task_id", "goal", "capacity", "decomposition"})
    if source.get("schema") != "team-efficiency/work-package-v1":
        raise ValueError("work-package schema is invalid")
    task_id = _task_id(source.get("task_id"))
    goal = _manifest_text(source.get("goal"), "goal")
    capacity = source.get("capacity")
    if type(capacity) is not int or not 1 <= capacity <= MAX_MANIFEST_UNITS:
        raise ValueError("capacity is out of bounds")
    decomposition = _text(source.get("decomposition"), "decomposition", maximum=32)
    try:
        manifest_size = len(_json_bytes(manifest))
    except ValueError:
        if source.get("source_kind") == "task_episode_graph":
            return _needs_design_plan(
                task_id=task_id,
                goal=goal,
                capacity=capacity,
                source_kind="task_episode_graph",
                reason="ATLAS_GRAPH_INVALID",
            )
        raise
    if manifest_size > MAX_MANIFEST_BYTES:
        source_kind = source.get("source_kind")
        if (
            not isinstance(source_kind, str)
            or source_kind not in _ATLAS_SOURCE_KINDS | _ARTIFACT_SOURCE_KINDS
        ):
            source_kind = "none"
        return _needs_design_plan(
            task_id=task_id,
            goal=goal,
            capacity=capacity,
            source_kind=source_kind,
            reason="ATLAS_INPUT_BUDGET_EXCEEDED",
        )

    if decomposition in {"semantic", "needs_design"}:
        source_kind = _manifest_source_kind(
            source,
            common,
            default="none",
            allowed=frozenset({"none"}),
        )
        return _needs_design_plan(
            task_id=task_id,
            goal=goal,
            capacity=capacity,
            source_kind=source_kind,
            reason="Semantic decomposition requires Sol-owned design.",
        )

    if decomposition == "artifact_boundaries":
        expected = common | {"artifacts"}
        source_kind = _manifest_source_kind(
            source,
            expected,
            default="explicit_artifact_boundaries",
            allowed=_ARTIFACT_SOURCE_KINDS,
        )
        artifacts = source["artifacts"]
        if not isinstance(artifacts, Sequence) or isinstance(
            artifacts, (str, bytes, bytearray)
        ):
            raise ValueError("artifacts must be a list")
        if not artifacts or len(artifacts) > MAX_MANIFEST_UNITS:
            raise ValueError("artifact count is out of bounds")
        units = [_artifact(artifact) for artifact in artifacts]
        return _scheduled_plan(
            task_id=task_id,
            goal=goal,
            capacity=capacity,
            source_kind=source_kind,
            units=units,
        )

    if decomposition != "atlas_evidence":
        raise ValueError("work-package decomposition is invalid")
    source_kind = _text(source.get("source_kind"), "source_kind", maximum=64)
    if source_kind not in _ATLAS_SOURCE_KINDS:
        raise ValueError("work-package source_kind is invalid")
    if source_kind == "code_atlas_packet":
        _exact_keys(
            source,
            common | {"source_kind", "packet", "path_bindings"},
            "work-package manifest",
        )
        units, reason = _packet_units(
            source["packet"],
            source["path_bindings"],
            task_id,
        )
    else:
        try:
            expected = common | {"source_kind", "eligible", "graph"}
            if "eligible" not in source:
                raise AtlasEvidenceError("ATLAS_ELIGIBILITY_UNPROVEN")
            _exact_keys(source, expected, "work-package manifest")
            if source["eligible"] is not True:
                if source["eligible"] is False:
                    raise AtlasEvidenceError("ATLAS_EXTRACTION_INELIGIBLE")
                raise AtlasEvidenceError("ATLAS_ELIGIBILITY_UNPROVEN")
            units, reason = _episode_units(source["graph"], task_id)
        except AtlasEvidenceError as error:
            units, reason = None, error.code
        except (KeyError, TypeError, ValueError):
            units, reason = None, "ATLAS_GRAPH_INVALID"
    if units is None:
        return _needs_design_plan(
            task_id=task_id,
            goal=goal,
            capacity=capacity,
            source_kind=source_kind,
            reason=reason,
        )
    return _scheduled_plan(
        task_id=task_id,
        goal=goal,
        capacity=capacity,
        source_kind=source_kind,
        units=units,
    )


def plan_waves(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Alias for the deterministic work-package compiler."""

    return decompose(manifest)


def _fast_lane_effort(value: object) -> str:
    effort = _text(value, "reasoning_effort", maximum=16)
    if effort not in FAST_LANE_REASONING_EFFORTS:
        raise ValueError("reasoning_effort is invalid")
    return effort


def _one_fast_lane_effort(values: object) -> str:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes, bytearray))
        or len(values) != 1
    ):
        raise ValueError("fast-lane requires exactly one reasoning effort")
    return _fast_lane_effort(values[0])


def _fast_lane_activation(
    reasoning_effort: object, enable: object
) -> dict[str, str | None]:
    if type(enable) is not bool:
        raise ValueError("enable must be a boolean")
    effort = _fast_lane_effort(reasoning_effort)
    if effort == "ultra":
        reason = "ultra_auto"
    elif enable:
        reason = "explicit_opt_in"
    else:
        reason = None
    return {"reasoning_effort": effort, "reason": reason}


def _fast_lane_red_failure_fingerprint(
    gate_id: str, failure_ids: Sequence[str]
) -> str | None:
    if not failure_ids:
        return None
    return _sha256_json(
        {
            "schema": "team-efficiency/red-failure-identity-v1",
            "gate_id": gate_id,
            "failure_ids": sorted(failure_ids),
        }
    )


def _fast_lane_argv(value: object, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a list")
    if not value or len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{field} is out of bounds")
    normalized: list[str] = []
    for index, item in enumerate(value):
        token = _text(item, f"{field}[{index}]", maximum=256)
        _reject_sensitive_or_absolute_text(token, f"{field}[{index}]")
        folded = token.casefold()
        if (
            folded in _FAST_LANE_SHELL_WRAPPERS
            or folded in _FAST_LANE_SHELL_ARGUMENTS
            or token.startswith("\\")
            or "=/" in token
            or ".." in token
            or any(ord(character) == 127 for character in token)
        ):
            raise ValueError(f"{field} contains an unsafe command token")
        normalized.append(token)
    return normalized


def _fast_lane_exit_codes(value: object, field: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a list")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{field} is out of bounds")
    normalized: list[int] = []
    for item in value:
        if type(item) is not int or not 1 <= item <= 255:
            raise ValueError(f"{field} must contain nonzero exit codes")
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains duplicates")
    return sorted(normalized)


def _fast_lane_failure_ids(value: object, field: str) -> list[str]:
    def normalize(item: object, nested_field: str) -> str:
        failure_id = _text(item, nested_field, maximum=256)
        _reject_sensitive_or_absolute_text(failure_id, nested_field)
        if (
            failure_id.startswith("\\")
            or ".." in failure_id
            or any(ord(character) == 127 for character in failure_id)
        ):
            raise ValueError(f"{nested_field} is unsafe")
        return failure_id

    return _normalised_list(
        value,
        field,
        normalize,
        maximum=MAX_LIST_ITEMS,
    )


def _validated_fast_lane_gate(value: object, field: str) -> dict[str, Any]:
    gate = _mapping(value, field)
    _exact_keys(gate, _FAST_LANE_GATE_FIELDS, field)
    gate_id = _label(gate["gate_id"], f"{field}.gate_id")
    argv = _fast_lane_argv(gate["argv"], f"{field}.argv")
    red_expected_exit_codes = _fast_lane_exit_codes(
        gate["red_expected_exit_codes"], f"{field}.red_expected_exit_codes"
    )
    green_expected_exit_code = gate["green_expected_exit_code"]
    if type(green_expected_exit_code) is not int or not 0 <= green_expected_exit_code <= 255:
        raise ValueError(f"{field}.green_expected_exit_code is invalid")
    timeout_seconds = gate["timeout_seconds"]
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_GATE_TIMEOUT_SECONDS:
        raise ValueError(f"{field}.timeout_seconds is invalid")
    red_failure_ids = _fast_lane_failure_ids(
        gate["red_failure_ids"], f"{field}.red_failure_ids"
    )
    expected_fingerprint = _fast_lane_red_failure_fingerprint(
        gate_id, red_failure_ids
    )
    fingerprint_value = gate["red_failure_fingerprint"]
    if expected_fingerprint is None:
        if fingerprint_value is not None:
            raise ValueError(f"{field}.red_failure_fingerprint must be null")
        red_failure_fingerprint = None
    else:
        red_failure_fingerprint = _hash(
            fingerprint_value, f"{field}.red_failure_fingerprint"
        )
        if red_failure_fingerprint != expected_fingerprint:
            raise ValueError(f"{field}.red_failure_fingerprint is invalid")
    return {
        "gate_id": gate_id,
        "argv": argv,
        "red_expected_exit_codes": red_expected_exit_codes,
        "green_expected_exit_code": green_expected_exit_code,
        "timeout_seconds": timeout_seconds,
        "red_failure_ids": red_failure_ids,
        "red_failure_fingerprint": red_failure_fingerprint,
        "acceptance_constraint_hashes": _normalised_list(
            gate["acceptance_constraint_hashes"],
            f"{field}.acceptance_constraint_hashes",
            _hash,
            maximum=MAX_LIST_ITEMS,
        ),
    }


def _validated_fast_lane_target_gates(
    value: object, source_plan_value: object
) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("target_gates must be a list")
    if not value or len(value) > MAX_MANIFEST_UNITS:
        raise ValueError("target_gates is out of bounds")
    normalized_targets: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        field = f"target_gates[{index}]"
        target = _mapping(item, field)
        _exact_keys(target, _FAST_LANE_TARGET_GATE_FIELDS, field)
        task_id = _task_id(target["task_id"], f"{field}.task_id")
        driver_value = target["driver_gate_id"]
        driver_gate_id = (
            None
            if driver_value is None
            else _label(driver_value, f"{field}.driver_gate_id")
        )
        gates_value = target["gates"]
        if not isinstance(gates_value, Sequence) or isinstance(
            gates_value, (str, bytes, bytearray)
        ):
            raise ValueError(f"{field}.gates must be a list")
        if not gates_value or len(gates_value) > MAX_LIST_ITEMS:
            raise ValueError(f"{field}.gates is out of bounds")
        gates = [
            _validated_fast_lane_gate(gate, f"{field}.gates[{gate_index}]")
            for gate_index, gate in enumerate(gates_value)
        ]
        gate_ids = [gate["gate_id"] for gate in gates]
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError(f"{field}.gates contains duplicate gate ids")
        gates = sorted(gates, key=lambda gate: gate["gate_id"])
        if driver_gate_id is not None and driver_gate_id not in set(gate_ids):
            raise ValueError(f"{field}.driver_gate_id is not a declared gate")
        normalized_targets.append(
            {
                "task_id": task_id,
                "driver_gate_id": driver_gate_id,
                "gates": gates,
            }
        )
    target_ids = [target["task_id"] for target in normalized_targets]
    if len(set(target_ids)) != len(target_ids):
        raise ValueError("target_gates contains duplicate task ids")
    normalized_targets = sorted(normalized_targets, key=lambda target: target["task_id"])

    source_plan = _mapping(source_plan_value, "source plan")
    if _text(source_plan["status"], "source plan.status", maximum=32) != "planned":
        return normalized_targets
    source_kind = _text(source_plan["source_kind"], "source plan.source_kind", maximum=64)
    if source_kind == "task_episode_graph":
        raise ValueError("ATLAS_GATE_UNVERIFIED")
    units_value = source_plan["units"]
    if not isinstance(units_value, Sequence) or isinstance(
        units_value, (str, bytes, bytearray)
    ):
        raise ValueError("source plan.units must be a list")
    units_by_task_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(units_value):
        unit = _mapping(item, f"source plan.units[{index}]")
        unit_task_id = _task_id(unit["task_id"], f"source plan.units[{index}].task_id")
        if unit_task_id in units_by_task_id:
            raise ValueError("source plan contains duplicate task ids")
        units_by_task_id[unit_task_id] = unit
    if sorted(target_ids) != sorted(units_by_task_id):
        raise ValueError("target_gates must match the source plan task set")

    for target in normalized_targets:
        unit = units_by_task_id[target["task_id"]]
        gates = target["gates"]
        driver_gate_id = target["driver_gate_id"]
        if source_kind == "explicit_artifact_boundaries":
            if (
                len(gates) != 1
                or driver_gate_id != gates[0]["gate_id"]
                or gates[0]["acceptance_constraint_hashes"]
                or not gates[0]["red_expected_exit_codes"]
                or not gates[0]["red_failure_ids"]
                or gates[0]["red_failure_fingerprint"] is None
            ):
                raise ValueError("manual artifact target gate is invalid")
            continue
        if source_kind != "code_atlas_packet":
            raise ValueError("ATLAS_GATE_UNVERIFIED")
        expected_acceptance = _normalised_list(
            unit["acceptance_constraints"],
            f"source plan unit {target['task_id']}.acceptance_constraints",
            _hash,
            maximum=MAX_LIST_ITEMS,
            required=True,
        )
        gate_acceptance = [
            constraint
            for gate in gates
            for constraint in gate["acceptance_constraint_hashes"]
        ]
        if (
            len(set(gate_acceptance)) != len(gate_acceptance)
            or sorted(gate_acceptance) != expected_acceptance
        ):
            raise ValueError("ATLAS_GATE_UNVERIFIED")
        for gate in gates:
            verified_test_hash = _sha256_json(
                {
                    "argv": gate["argv"],
                    "expected_exit_code": gate["green_expected_exit_code"],
                }
            )
            if verified_test_hash not in expected_acceptance:
                raise ValueError("ATLAS_GATE_UNVERIFIED")
            if gate["acceptance_constraint_hashes"] != [verified_test_hash]:
                raise ValueError("ATLAS_GATE_UNVERIFIED")
        unit_kind = _text(unit["unit_kind"], "source plan unit.unit_kind", maximum=32)
        if unit_kind == "verification":
            if driver_gate_id is not None or any(
                gate["red_expected_exit_codes"]
                or gate["red_failure_ids"]
                or gate["red_failure_fingerprint"] is not None
                for gate in gates
            ):
                raise ValueError("verification target gate must not define RED state")
        elif driver_gate_id is None:
            raise ValueError("packet code target requires a driver gate")
        else:
            driver_gate = next(
                gate for gate in gates if gate["gate_id"] == driver_gate_id
            )
            if (
                not driver_gate["red_expected_exit_codes"]
                or not driver_gate["red_failure_ids"]
                or driver_gate["red_failure_fingerprint"] is None
            ):
                raise ValueError("packet driver gate must define RED identity")
    return normalized_targets


def _validated_fast_lane_request(request: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _mapping(request, "fast-lane request")
    _exact_keys(candidate, _FAST_LANE_REQUEST_FIELDS, "fast-lane request")
    if candidate["schema"] != "team-efficiency/fast-lane-request-v1":
        raise ValueError("fast-lane request schema is invalid")
    if len(_json_bytes(candidate)) > MAX_MANIFEST_INPUT_BYTES:
        raise ValueError("fast-lane request exceeds its byte budget")
    source_plan = decompose(candidate["work_package"])
    return {
        "source_plan": source_plan,
        "source_plan_hash": _sha256_json(source_plan),
        "target_gates": _validated_fast_lane_target_gates(
            candidate["target_gates"], source_plan
        ),
        "execution_contexts": candidate["execution_contexts"],
        "read_contexts": candidate["read_contexts"],
        "remediation_request": candidate["remediation_request"],
        "scheduler_state": candidate["scheduler_state"],
    }


def _fast_lane_phase(value: object) -> str:
    scheduler_state = _mapping(value, "scheduler_state")
    phase = _text(scheduler_state.get("phase"), "scheduler_state.phase", maximum=32)
    if phase not in _FAST_LANE_PHASES:
        raise ValueError("fast-lane phase is invalid")
    return phase


def _fast_lane_idle_slots(reason_code: str) -> list[dict[str, str]]:
    return [
        {"slot_id": slot_id, "reason_code": reason_code}
        for slot_id in FAST_LANE_SLOT_IDS
    ]


def _fast_lane_main_lane(
    activation: Mapping[str, Any],
    *,
    next_action: str | None,
) -> dict[str, Any]:
    return {
        "lane_id": "lane-0",
        "model": "gpt-5.6-sol",
        "reasoning_effort": activation["reasoning_effort"],
        "next_action": next_action,
        "design_owner": "main-sol",
        "parallel_design": {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "ultra",
            "max_concurrent": 1,
        },
        "owned_write_scopes": [],
        "excluded_write_scopes": [],
    }


def _fast_lane_refill_plan() -> dict[str, Any]:
    return {
        "trigger": "slot_terminal_event",
        "dispatch_at": "next_host_dispatch_boundary",
        "priority": [
            "restore_two_safe_execution_slots",
            "declared_verification_unit",
            "candidate_review",
            "lane0_approved_design_probe",
            "dependency_prewarmer",
            "third_safe_execution",
        ],
        "polling": False,
    }


def _fast_lane_terminal_protocol(source_plan: Mapping[str, Any]) -> dict[str, Any]:
    verification_unit_task_ids = [
        unit["task_id"]
        for unit in source_plan["units"]
        if unit.get("unit_kind") == "verification"
    ]
    return {
        "owner": "lane0_and_work_methodology_skill",
        "compiler_schedules_declared_verification_units": True,
        "compiler_schedules_ad_hoc_terminal_slots": False,
        "verification_unit_task_ids": verification_unit_task_ids,
        "integration_regression_passes": 1,
        "blocker_reviews": 1,
        "global_targeted_remediation_rounds": 1,
        "wide_or_shared_scope_remediation": "stop_for_lane0",
    }


def _fast_lane_workflow_policy() -> dict[str, Any]:
    return {
        "owner": "work_methodology_skill",
        "boundary_operations": [],
        "conditional_operations": [],
        "operation_set_is_closed_capability_list": False,
        "mid_item_status_polling": False,
        "recovery_status_reads": "start_or_recovery_boundary_only",
        "release_tool_available": False,
    }


def _render_fast_lane_status(
    validated: Mapping[str, Any],
    activation: Mapping[str, Any],
    *,
    status: str,
    decision_code: str,
    idle_reason: str,
    next_action: str | None,
) -> dict[str, Any]:
    source_plan = _mapping(validated["source_plan"], "source plan")
    result: dict[str, Any] = {
        "schema": "team-efficiency/fast-lane-plan-v1",
        "status": status,
        "decision_code": decision_code,
        "activation": dict(activation),
        "source_plan_hash": validated["source_plan_hash"],
        "phase": _fast_lane_phase(validated["scheduler_state"]),
        "main_lane": _fast_lane_main_lane(activation, next_action=next_action),
        "subagent_capacity": len(FAST_LANE_SLOT_IDS),
        "assignments": [],
        "ready_queue": [],
        "review_queue": [],
        "prewarm_queue": [],
        "design_queue": [],
        "invalidated_evidence_task_ids": [],
        "idle_slots": _fast_lane_idle_slots(idle_reason),
        "refill_plan": _fast_lane_refill_plan(),
        "terminal_protocol": _fast_lane_terminal_protocol(source_plan),
        "workflow_policy": _fast_lane_workflow_policy(),
    }
    result["plan_hash"] = _sha256_json(result)
    _exact_keys(result, _FAST_LANE_PLAN_FIELDS, "fast-lane plan")
    if len(_json_bytes(result)) > MAX_MANIFEST_BYTES:
        raise ValueError("fast-lane plan exceeds its byte budget")
    return result


def _fast_lane_needs_design_plan(
    validated: Mapping[str, Any], activation: Mapping[str, Any]
) -> dict[str, Any]:
    return _render_fast_lane_status(
        validated,
        activation,
        status="needs_design",
        decision_code="WORK_PACKAGE_NEEDS_DESIGN",
        idle_reason="WORK_PACKAGE_NEEDS_DESIGN",
        next_action="design_required",
    )


def _fast_lane_stopped_plan(
    validated: Mapping[str, Any], activation: Mapping[str, Any]
) -> dict[str, Any]:
    return _render_fast_lane_status(
        validated,
        activation,
        status="stopped",
        decision_code="AUTOMATION_STOPPED",
        idle_reason="AUTOMATION_STOPPED",
        next_action=None,
    )


def _render_fast_lane_plan(
    validated: Mapping[str, Any], activation: Mapping[str, Any]
) -> dict[str, Any]:
    source_plan = _mapping(validated["source_plan"], "source plan")
    if source_plan["status"] == "needs_design":
        return _fast_lane_needs_design_plan(validated, activation)
    if _fast_lane_phase(validated["scheduler_state"]) == "stopped":
        return _fast_lane_stopped_plan(validated, activation)
    if activation["reason"] is None:
        return _render_fast_lane_status(
            validated,
            activation,
            status="inactive",
            decision_code="EXPLICIT_OPT_IN_REQUIRED",
            idle_reason="OPT_IN_REQUIRED",
            next_action=None,
        )
    contexts = validated["execution_contexts"]
    has_execution_contexts = isinstance(contexts, Sequence) and not isinstance(
        contexts, (str, bytes, bytearray)
    ) and bool(contexts)
    return _render_fast_lane_status(
        validated,
        activation,
        status="blocked",
        decision_code="NO_SAFE_WORK",
        idle_reason=(
            "NO_SAFE_INDEPENDENT_WORK"
            if has_execution_contexts
            else "EXECUTION_CONTEXT_MISSING"
        ),
        next_action=None,
    )


def compile_fast_lane(
    request: Mapping[str, Any],
    *,
    reasoning_effort: str,
    enable: bool = False,
) -> dict[str, Any]:
    activation = _fast_lane_activation(reasoning_effort, enable)
    validated = _validated_fast_lane_request(request)
    return _render_fast_lane_plan(validated, activation)


def _read_json(path_text: str, *, maximum: int) -> Any:
    path = Path(path_text)
    payload = path.read_bytes()
    if len(payload) > maximum:
        raise ValueError("input exceeds its byte budget")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("input is not valid UTF-8 JSON") from error


def _print_json(value: object) -> None:
    print(_canonical_json(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--task-id", required=True)
    bootstrap.add_argument("--base-commit", required=True)
    bootstrap.add_argument("--branch", required=True)
    bootstrap.add_argument("--write-scope", action="append", required=True)
    bootstrap.add_argument("--repo", required=True)
    bootstrap.add_argument("--project", required=True)
    bootstrap.add_argument("--worktree", required=True)
    bootstrap.add_argument("--temp-target", required=True)
    bootstrap.add_argument("--apply", action="store_true")

    for command in ("resume-packet", "status", "cache-key", "decompose", "plan-waves"):
        item = commands.add_parser(command)
        item.add_argument("--input", required=True)

    fast_lane = commands.add_parser("fast-lane")
    fast_lane.add_argument("--input", required=True)
    fast_lane.add_argument("--reasoning-effort", action="append", default=[])
    fast_lane.add_argument("--enable", action="store_true")

    contract = commands.add_parser("contract-check")
    contract.add_argument("--producer", required=True)
    contract.add_argument("--consumer", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "bootstrap":
            plan = build_bootstrap_plan(
                task_id=args.task_id,
                base_commit=args.base_commit,
                branch=args.branch,
                write_scope=args.write_scope,
                repo=args.repo,
                project=args.project,
                worktree=args.worktree,
                temp_target=args.temp_target,
            )
            _print_json(apply_bootstrap_plan(plan) if args.apply else plan)
        elif args.command == "resume-packet":
            packet = _read_json(args.input, maximum=MAX_PACKET_BYTES)
            print(canonical_resume_packet(packet))
        elif args.command == "status":
            snapshot = _read_json(args.input, maximum=MAX_STATUS_BYTES)
            print(render_status_markdown(snapshot), end="")
        elif args.command == "contract-check":
            producer = _read_json(args.producer, maximum=MAX_PACKET_BYTES)
            consumer = _read_json(args.consumer, maximum=MAX_PACKET_BYTES)
            _print_json(contract_check(producer, consumer))
        elif args.command == "cache-key":
            inputs = _read_json(args.input, maximum=MAX_PACKET_BYTES)
            _print_json(make_cache_metadata(inputs))
        elif args.command == "fast-lane":
            request = _read_json(args.input, maximum=MAX_MANIFEST_INPUT_BYTES)
            result = compile_fast_lane(
                _mapping(request, "fast-lane request"),
                reasoning_effort=_one_fast_lane_effort(args.reasoning_effort),
                enable=args.enable,
            )
            _print_json(result)
        else:
            manifest = _read_json(args.input, maximum=MAX_MANIFEST_INPUT_BYTES)
            _print_json(decompose(manifest))
    except (ContractMismatchError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
