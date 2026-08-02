"""Deterministic, local helpers for bounded team orchestration data."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
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
# A host may attest one bounded core request for every task/role pair.  Keep
# the CLI envelope finite while allowing the 16-task plan plus one approved
# global-remediation unit.
MAX_FAST_LANE_ROUTE_REQUEST_BYTES = 32 * 1024
MAX_FAST_LANE_HOST_STATUS_BYTES = 3 * 1024 * 1024
FAST_LANE_SLOT_IDS = ("slot-1", "slot-2", "slot-3")
MAX_FAST_LANE_EXTERNAL_SESSION_ASSIGNMENTS = 9
FAST_LANE_REASONING_EFFORTS = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)
# These are valid snapshots of the scheduler state, not rejected host route
# attestations.  They defer only the affected task/role until the next plan.
_FAST_LANE_DEFERRED_ROUTE_REASONS = frozenset(
    {"dependency_not_ready", "scope_conflict_active"}
)

_TASK_ID = re.compile(r"^(?:[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|FLR1-[0-9a-f]{24})$")
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
_FAST_LANE_EXECUTION_CONTEXT_FIELDS = frozenset(
    {"task_id", "bootstrap_plan", "workspace_input_snapshot_id"}
)
_FAST_LANE_READ_CONTEXT_FIELDS = frozenset(
    {
        "task_id",
        "role",
        "repo",
        "worktree",
        "base_commit",
        "tree",
        "workspace_input_snapshot_id",
        "read_scope",
        "temp_target",
    }
)
_FAST_LANE_READ_ROLES = frozenset({"verification", "prewarm", "review", "design_probe"})
_FAST_LANE_SCHEDULER_FIELDS = frozenset(
    {
        "source_plan_hash",
        "phase",
        "integration_state",
        "lane0_state",
        "completed_tasks",
        "review_ready_candidates",
        "reviewed_candidates",
        "prewarmed_evidence",
        "design_evidence",
        "running_assignments",
        "dispatch_contexts",
        "blocked_task_ids",
        "pending_design_probe_task_ids",
        "slot_epochs",
        "global_remediation",
    }
)
_FAST_LANE_INTEGRATION_FIELDS = frozenset(
    {"commit", "tree", "integration_workspace_snapshot_id"}
)
_FAST_LANE_LANE0_FIELDS = frozenset({"active_task_id", "owned_write_scopes"})
_FAST_LANE_GLOBAL_REMEDIATION_FIELDS = frozenset(
    {
        "round",
        "state",
        "task_id",
        "affected_task_ids",
        "blocker_review_hash",
        "finding_hash",
        "dispatch_receipt",
        "completion_receipt_hash",
    }
)
_FAST_LANE_DISPATCH_CONTEXT_FIELDS = frozenset(
    {
        "context_hash",
        "task_id",
        "role",
        "source_plan_hash",
        "integration_commit",
        "integration_tree",
        "workspace_input_snapshot_id",
        "direct_dependency_result_hashes",
        "direct_contract_hashes",
        "required_evidence",
        "task_node_ids",
        "contract_node_ids",
        "acceptance_constraints",
        "execution_context_hash",
        "bootstrap_plan_hash",
        "base_commit",
        "branch",
        "write_scope_hash",
        "read_context_hash",
        "target_gates_hash",
        "candidate_commit",
        "red_evidence_hashes",
        "green_evidence_hashes",
        "basis_hash",
        "prewarm_evidence_hash",
        "prewarm_revalidation_evidence_hash",
    }
)
_FAST_LANE_ASSIGNMENT_FIELDS = frozenset(
    {
        "slot_id",
        "task_id",
        "role",
        "assignment_epoch",
        "assignment_token",
        "context_hash",
        "model",
        "reasoning_effort",
        "routing_context_hash",
        "routing_result_hash",
        "task_fingerprint",
        "routing_reason_codes",
        "routing_safety_floor_rank",
        "dispatch_receipt",
    }
)
_FAST_LANE_HOST_BINDING_FIELDS = frozenset(
    {
        "workflow_id",
        "task_id",
        "slot_id",
        "assignment_epoch",
        "assignment_token",
        "context_hash",
        "lease_epoch",
        "endpoint",
        "state",
    }
)
_FAST_LANE_HOST_LEASE_FIELDS = frozenset(
    {"task_id", "lease_epoch", "endpoint", "state"}
)
_FAST_LANE_HOST_STATES = frozenset(
    {
        "pending_init",
        "running",
        "completed",
        "done",
        "failed",
        "blocked",
        "expired",
        "interrupted",
    }
)
_FAST_LANE_DISPATCH_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "source_plan_hash",
        "task_id",
        "role",
        "slot_id",
        "assignment_epoch",
        "model",
        "reasoning_effort",
        "routing_context_hash",
        "routing_result_hash",
        "task_fingerprint",
        "routing_reason_codes",
        "routing_safety_floor_rank",
        "routing_input",
        "dispatch_context_hash",
        "target_gates_hash",
        "execution_context_hash",
        "read_context_hash",
        "recovery_of_assignment_token",
    }
)
_FAST_LANE_TERMINAL_RESULT_FIELDS = frozenset(
    {
        "schema",
        "dispatch_receipt",
        "assignment_token",
        "task_id",
        "role",
        "outcome",
        "candidate_commit",
        "candidate_tree",
        "red_evidence_hashes",
        "green_evidence_hashes",
        "evidence_hash",
        "review_hash",
        "input_query_trace_id",
        "checkpoint_id",
        "output_workspace_snapshot_id",
        "output_query_trace_id",
    }
)
_FAST_LANE_COMPLETION_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "terminal_result_hash",
        "workflow_id_hash",
        "task_id",
        "completion_kind",
        "integration_commit",
        "integration_tree",
        "candidate_commit",
        "candidate_tree",
        "integration_proof_hash",
        "workspace_input_snapshot_id",
        "output_workspace_snapshot_id",
        "verification_evidence_hashes",
    }
)
_FAST_LANE_ROLES = frozenset(
    {"execution", "verification", "prewarm", "review", "design_probe"}
)
MAX_FAST_LANE_ROUTING_ENTRIES = (MAX_MANIFEST_UNITS + 1) * len(_FAST_LANE_ROLES)
_FAST_LANE_CORE_ROLE_BY_SCHEDULER_ROLE = {
    "execution": "execution",
    "verification": "verification",
    "prewarm": "prewarm",
    "review": "review",
    "design_probe": "design",
}
_FAST_LANE_HOST_STATUS_FIELDS = frozenset(
    {"workflow_id", "current_leases", "host_bindings", "routing_context"}
)
_FAST_LANE_ROUTING_CONTEXT_FIELDS = frozenset({"schema", "routes"})
_FAST_LANE_ROUTING_ENTRY_FIELDS = frozenset(
    {
        "task_id",
        "scheduler_role",
        "request",
        "trusted_authorization_evidence_hashes",
        "trusted_override_receipt_hashes",
        "trusted_evidence_hashes",
        "coordinator_endpoint_hash",
        "compatibility_floor",
    }
)
_FAST_LANE_REMEDIATION_FIELDS = frozenset(
    {
        "schema",
        "round",
        "task_id",
        "source_plan_hash",
        "blocker_review_hash",
        "finding_hash",
        "severity",
        "affected_task_ids",
        "dependencies",
        "base_integration_commit",
        "base_integration_tree",
        "goal",
        "output_boundary",
        "write_scope",
        "direct_contract_hashes",
        "required_evidence",
        "task_node_ids",
        "contract_node_ids",
        "acceptance_constraints",
        "driver_gate_id",
        "target_gates",
    }
)
_FAST_LANE_COMPLETED_FIELDS = frozenset(
    {
        "task_id",
        "completion_kind",
        "integration_commit",
        "integration_tree",
        "result_hash",
        "terminal_result_hash",
        "terminal_result",
        "completion_receipt_hash",
        "completion_receipt",
    }
)
_FAST_LANE_REVIEW_READY_FIELDS = frozenset(
    {
        "task_id",
        "candidate_commit",
        "candidate_tree",
        "red_evidence_hashes",
        "green_evidence_hashes",
        "terminal_result_hash",
        "terminal_result",
    }
)
_FAST_LANE_REVIEWED_FIELDS = frozenset(
    set(_FAST_LANE_REVIEW_READY_FIELDS)
    | {
        "review_hash",
        "outcome",
        "review_terminal_result_hash",
        "review_terminal_result",
    }
)
_FAST_LANE_PREWARMED_FIELDS = frozenset(
    {
        "task_id",
        "observation_basis_hash",
        "evidence_hash",
        "terminal_result_hash",
        "terminal_result",
        "revalidation_basis_hash",
        "dependency_delta_hash",
        "revalidation_evidence_hash",
    }
)
_FAST_LANE_DESIGN_EVIDENCE_FIELDS = frozenset(
    {
        "task_id",
        "observation_basis_hash",
        "evidence_hash",
        "terminal_result_hash",
        "terminal_result",
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
        "cross_session_dispatch_projection",
        "plan_hash",
    }
)
_FAST_LANE_CROSS_SESSION_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "status",
        "source_plan_hash",
        "workflow_id_hash",
        "local_capacity",
        "local_active_count",
        "local_free",
        "global_main_target",
        "global_main_active",
        "global_main_free",
        "global_main_free_after_local_starts",
        "quota_evidence_hash",
        "quota_snapshot_hash",
        "quota_decision_hash",
        "external_session_count",
        "external_assignment_ids",
        "assignments",
        "reason_codes",
        "projection_hash",
    }
)
_FAST_LANE_EXTERNAL_ASSIGNMENT_FIELDS = frozenset(
    {
        "schema",
        "assignment_id",
        "action",
        "session_state",
        "pool",
        "task_id",
        "role",
        "model",
        "reasoning_effort",
        "route",
        "context_hash",
        "lease_fencing_predecessor",
        "reason",
    }
)
_FAST_LANE_EXTERNAL_LEASE_PREDECESSOR_FIELDS = frozenset(
    {
        "schema",
        "predecessor_hash",
        "source_plan_hash",
        "workflow_id_hash",
        "task_id",
        "role",
        "context_hash",
        "routing_result_hash",
        "quota_evidence_hash",
        "quota_snapshot_hash",
        "quota_decision_hash",
        "ledger_epoch",
        "active_lease_set_hash",
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
        "workspace_id",
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
        "request_hash",
        "matcher_version",
        "target_paths",
        "target_symbols",
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
        "workspace_id": _hash(
            source["workspace_id"], "ImplementationPacket workspace_id"
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
        "request_hash": _hash(
            source["request_hash"], "ImplementationPacket request_hash"
        ),
        "matcher_version": _atlas_text(
            source["matcher_version"],
            "ImplementationPacket matcher_version",
            maximum=64,
        ),
        "target_paths": _normalised_list(
            source["target_paths"],
            "ImplementationPacket target_paths",
            _relative_scope,
        ),
        "target_symbols": _normalised_list(
            source["target_symbols"],
            "ImplementationPacket target_symbols",
            _atlas_text,
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
        or packet["next_action"] != "atlas_render"
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


def _fast_lane_unit_index(
    source_plan: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    units = source_plan.get("units", [])
    return {str(unit["task_id"]): unit for unit in units if isinstance(unit, Mapping)}


def _fast_lane_topology_index(
    units: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    remaining = {
        task_id: set(unit.get("depends_on", [])) for task_id, unit in units.items()
    }
    ordered: list[str] = []
    while remaining:
        ready = sorted(task_id for task_id, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError("fast-lane dependency graph contains a cycle")
        ordered.extend(ready)
        for task_id in ready:
            del remaining[task_id]
        for deps in remaining.values():
            deps.difference_update(ready)
    return {task_id: index for index, task_id in enumerate(ordered)}


def _fast_lane_completed_ids(
    scheduler_state: Mapping[str, Any],
) -> frozenset[str]:
    return frozenset(
        record["task_id"] for record in scheduler_state.get("completed_tasks", [])
    )


def _fast_lane_dependency_ready(
    unit: Mapping[str, Any], completed: frozenset[str]
) -> bool:
    return set(unit.get("depends_on", [])) <= completed


def _fast_lane_conflict_graph(
    units: Mapping[str, Mapping[str, Any]],
    *,
    lane0_scopes: Sequence[str] = (),
    running: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[str]]:
    graph = _conflict_graph(list(units.values()))
    for task_id, unit in units.items():
        if lane0_scopes and _scope_conflicts(unit.get("write_scope", []), lane0_scopes):
            graph.setdefault(task_id, []).append("lane-0")
        for assignment in running:
            if (
                assignment.get("role") != "execution"
                or assignment.get("task_id") == task_id
            ):
                continue
            other = units.get(str(assignment.get("task_id")))
            if other and _scope_conflicts(
                unit.get("write_scope", []), other.get("write_scope", [])
            ):
                graph.setdefault(task_id, []).append(str(assignment.get("task_id")))
    return {
        task_id: sorted(set(neighbors)) for task_id, neighbors in sorted(graph.items())
    }


def _fast_lane_critical_path_distance(
    task_id: str,
    units: Mapping[str, Mapping[str, Any]],
    completed: frozenset[str],
    memo: dict[str, int],
) -> int:
    if task_id in memo:
        return memo[task_id]
    unfinished = [
        dependency
        for dependency in units[task_id].get("depends_on", [])
        if dependency not in completed
    ]
    distance = (
        0
        if not unfinished
        else 1
        + max(
            _fast_lane_critical_path_distance(dependency, units, completed, memo)
            for dependency in unfinished
        )
    )
    memo[task_id] = distance
    return distance


def _fast_lane_ready_items(
    units: Mapping[str, Mapping[str, Any]],
    completed: frozenset[str],
    blocked: frozenset[str],
    running: frozenset[str],
    candidate: frozenset[str],
    reviewed: frozenset[str],
    *,
    conflict_graph: Mapping[str, Sequence[str]] | None = None,
) -> list[Mapping[str, Any]]:
    graph = conflict_graph or _fast_lane_conflict_graph(units)
    ready: list[Mapping[str, Any]] = []
    for task_id, unit in units.items():
        if task_id in completed | blocked | running | candidate | reviewed:
            continue
        if unit.get("unit_kind") == "verification":
            continue
        if _fast_lane_dependency_ready(unit, completed) and "lane-0" not in graph.get(
            task_id, ()
        ):
            ready.append(unit)
    return sorted(ready, key=lambda item: str(item["task_id"]))


def _fast_lane_preferred_prewarms(
    *,
    units: Mapping[str, Mapping[str, Any]],
    completed: frozenset[str],
    running: frozenset[str],
    candidate: frozenset[str],
    reviewed: frozenset[str],
    read_contexts: set[tuple[str, str]],
    source_plan: Mapping[str, Any],
    blocked: frozenset[str] = frozenset(),
) -> list[str]:
    topology = _fast_lane_topology_index(units)
    wave_index = {
        str(unit.get("task_id")): index
        for index, wave in enumerate(source_plan.get("waves", []))
        for unit in wave
        if isinstance(unit, Mapping)
    }
    memo: dict[str, int] = {}
    candidates: list[tuple[tuple[int, int, int, str], str]] = []
    occupied = completed | blocked | running | candidate | reviewed
    for task_id, unit in units.items():
        if task_id in occupied or unit.get("unit_kind") not in {
            None,
            "artifact",
            "code",
        }:
            continue
        if (task_id, "prewarm") not in read_contexts:
            continue
        distance = _fast_lane_critical_path_distance(task_id, units, completed, memo)
        candidates.append(
            (
                (
                    distance,
                    topology.get(task_id, 0),
                    wave_index.get(task_id, 0),
                    task_id,
                ),
                task_id,
            )
        )
    return [task_id for _, task_id in sorted(candidates)]


def _fast_lane_validate_retained_assignments(
    validated: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    return list(validated["scheduler_state"].get("running_assignments", []))


def _fast_lane_select_verification_actions(
    validated: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    source_plan = validated["source_plan"]
    units = _fast_lane_unit_index(source_plan)
    state = validated["scheduler_state"]
    completed = _fast_lane_completed_ids(state)
    blocked = frozenset(state.get("blocked_task_ids", []))
    actions = [
        {**assignment, "action": "retain"}
        for assignment in _fast_lane_validate_retained_assignments(validated)
    ]
    used_slots = {assignment["slot_id"] for assignment in actions}
    free_slots = [slot for slot in FAST_LANE_SLOT_IDS if slot not in used_slots]
    read_contexts = {
        (context["task_id"], context["role"])
        for context in validated.get("read_contexts", [])
    }
    running = {assignment["task_id"] for assignment in actions}
    for task_id, unit in sorted(units.items()):
        if not free_slots:
            break
        if (
            unit.get("unit_kind") != "verification"
            or task_id in completed | blocked | running
            or not _fast_lane_dependency_ready(unit, completed)
            or (task_id, "verification") not in read_contexts
        ):
            continue
        route = _fast_lane_route(validated["routing_context"], unit, "verification")
        if route is None:
            continue
        actions.append(
            _fast_lane_assignment(
                validated, unit, "verification", free_slots.pop(0), route
            )
        )
    remaining = [
        unit
        for task_id, unit in units.items()
        if unit.get("unit_kind") == "verification"
        and task_id not in completed | blocked | {item["task_id"] for item in actions}
    ]
    idle_reason = (
        "WAITING_FOR_DEPENDENCY"
        if any(not _fast_lane_dependency_ready(unit, completed) for unit in remaining)
        else "NO_SAFE_INDEPENDENT_WORK"
    )
    return actions, [
        {"slot_id": slot_id, "reason_code": idle_reason} for slot_id in free_slots
    ]


def _fast_lane_select_remediation_actions(
    validated: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    source_plan = validated["source_plan"]
    units = _fast_lane_unit_index(source_plan)
    state = validated["scheduler_state"]
    completed = _fast_lane_completed_ids(state)
    blocked = frozenset(state.get("blocked_task_ids", []))
    actions = [
        {**assignment, "action": "retain"}
        for assignment in _fast_lane_validate_retained_assignments(validated)
    ]
    used_slots = {assignment["slot_id"] for assignment in actions}
    free_slots = [slot for slot in FAST_LANE_SLOT_IDS if slot not in used_slots]
    running = {assignment["task_id"] for assignment in actions}
    for task_id, unit in sorted(units.items()):
        if not free_slots:
            break
        if (
            unit.get("unit_kind") != "remediation"
            or task_id in completed | blocked | running
            or not _fast_lane_dependency_ready(unit, completed)
        ):
            continue
        route = _fast_lane_route(validated["routing_context"], unit, "execution")
        if route is None:
            continue
        actions.append(
            _fast_lane_assignment(
                validated, unit, "execution", free_slots.pop(0), route
            )
        )
    idle_reason = (
        "WAITING_FOR_DEPENDENCY"
        if any(
            unit.get("unit_kind") == "remediation"
            and unit["task_id"] not in completed
            and not _fast_lane_dependency_ready(unit, completed)
            for unit in units.values()
        )
        else "NO_SAFE_INDEPENDENT_WORK"
    )
    return actions, [
        {"slot_id": slot_id, "reason_code": idle_reason} for slot_id in free_slots
    ]


def _fast_lane_select_actions(
    validated: Mapping[str, Any],
    activation: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    source_plan = validated["source_plan"]
    units = _fast_lane_unit_index(source_plan)
    state = validated["scheduler_state"]
    if state["phase"] == "integration_regression":
        return _fast_lane_select_verification_actions(validated)
    if state["phase"] == "remediation":
        return _fast_lane_select_remediation_actions(validated)
    if state["phase"] != "execution":
        return [], _fast_lane_idle_slots("TERMINAL_PHASE_OWNED_BY_LANE0")
    completed = _fast_lane_completed_ids(state)
    blocked = frozenset(state.get("blocked_task_ids", []))
    running = {
        assignment["task_id"] for assignment in state.get("running_assignments", [])
    }
    candidates = {
        record["task_id"] for record in state.get("review_ready_candidates", [])
    }
    reviewed = {record["task_id"] for record in state.get("reviewed_candidates", [])}
    lane0 = state.get("lane0_state", {})
    lane0_scopes = lane0.get("owned_write_scopes", [])
    graph = _fast_lane_conflict_graph(
        units, lane0_scopes=lane0_scopes, running=state.get("running_assignments", [])
    )
    actions: list[dict[str, Any]] = []
    for assignment in _fast_lane_validate_retained_assignments(validated):
        actions.append({**assignment, "action": "retain"})
    used_slots = {assignment["slot_id"] for assignment in actions}
    free_slots = [slot for slot in FAST_LANE_SLOT_IDS if slot not in used_slots]
    capacity = min(int(source_plan.get("capacity", 0)), len(FAST_LANE_SLOT_IDS))
    writers = sum(1 for assignment in actions if assignment.get("role") == "execution")
    ready = _fast_lane_ready_items(
        units,
        completed,
        blocked,
        frozenset(running),
        frozenset(candidates),
        frozenset(reviewed),
        conflict_graph=graph,
    )
    ready_ids = {str(unit["task_id"]) for unit in ready}
    selected_scopes = [
        units[assignment["task_id"]].get("write_scope", [])
        for assignment in actions
        if assignment.get("role") == "execution" and assignment.get("task_id") in units
    ]
    for unit in ready:
        if not free_slots or writers >= min(capacity, 2):
            break
        if any(
            _scope_conflicts(unit.get("write_scope", []), scope)
            for scope in selected_scopes
        ):
            continue
        route = _fast_lane_route(validated["routing_context"], unit, "execution")
        if route is None:
            continue
        slot_id = free_slots.pop(0)
        assignment = _fast_lane_assignment(validated, unit, "execution", slot_id, route)
        actions.append(assignment)
        selected_scopes.append(unit.get("write_scope", []))
        writers += 1
    read_contexts = {
        (context["task_id"], context["role"])
        for context in validated.get("read_contexts", [])
    }
    review_records = sorted(
        state.get("review_ready_candidates", []), key=lambda item: item["task_id"]
    )
    if free_slots:
        for record in review_records:
            task_id = record["task_id"]
            if (task_id, "review") not in read_contexts or task_id not in units:
                continue
            route = _fast_lane_route(
                validated["routing_context"], units[task_id], "review"
            )
            if route is None:
                continue
            slot_id = free_slots.pop(0)
            assignment = _fast_lane_assignment(
                validated, units[task_id], "review", slot_id, route
            )
            actions.append(assignment)
            break
    design_ids = sorted(state.get("pending_design_probe_task_ids", []))
    if free_slots:
        for task_id in design_ids:
            if (task_id, "design_probe") not in read_contexts or task_id not in units:
                continue
            route = _fast_lane_route(
                validated["routing_context"], units[task_id], "design_probe"
            )
            if route is None:
                continue
            slot_id = free_slots.pop(0)
            assignment = _fast_lane_assignment(
                validated, units[task_id], "design_probe", slot_id, route
            )
            actions.append(assignment)
            break
    scheduled_ids = {str(item["task_id"]) for item in actions}
    prewarm_ids = _fast_lane_preferred_prewarms(
        units=units,
        completed=completed,
        running=frozenset(running | scheduled_ids),
        candidate=frozenset(candidates),
        reviewed=frozenset(reviewed),
        read_contexts=read_contexts,
        source_plan=source_plan,
        blocked=blocked,
    )
    if free_slots and prewarm_ids:
        task_id = prewarm_ids[0]
        unit = units[task_id]
        route = _fast_lane_route(validated["routing_context"], unit, "prewarm")
        if route is not None:
            slot_id = free_slots.pop(0)
            assignment = _fast_lane_assignment(
                validated, unit, "prewarm", slot_id, route
            )
            actions.append(assignment)
    if free_slots and writers < capacity:
        for unit in ready:
            if (
                not free_slots
                or str(unit["task_id"]) in ready_ids
                and any(item.get("task_id") == unit["task_id"] for item in actions)
            ):
                continue
            if any(
                _scope_conflicts(unit.get("write_scope", []), scope)
                for scope in selected_scopes
            ):
                continue
            route = _fast_lane_route(validated["routing_context"], unit, "execution")
            if route is None:
                continue
            slot_id = free_slots.pop(0)
            assignment = _fast_lane_assignment(
                validated, unit, "execution", slot_id, route
            )
            actions.append(assignment)
            selected_scopes.append(unit.get("write_scope", []))
            writers += 1
            break
    remaining_ids = (
        set(units) - completed - blocked - {str(item["task_id"]) for item in actions}
    )
    unroutable = [
        unit
        for task_id, unit in units.items()
        if task_id in remaining_ids
        and unit.get("unit_kind") != "verification"
        and _fast_lane_dependency_ready(unit, completed)
        and _fast_lane_route(validated["routing_context"], unit, "execution") is None
    ]
    if unroutable:
        idle_reason = _fast_lane_route_reason(
            validated["routing_context"], unroutable[0], "execution"
        )
    elif any(
        not _fast_lane_dependency_ready(units[task_id], completed)
        for task_id in remaining_ids
    ):
        idle_reason = "WAITING_FOR_DEPENDENCY"
    elif any(
        any(
            _scope_conflicts(units[task_id].get("write_scope", []), scope)
            for scope in selected_scopes
        )
        for task_id in remaining_ids
    ):
        idle_reason = "WRITE_SCOPE_CONFLICT"
    else:
        idle_reason = "NO_SAFE_INDEPENDENT_WORK"
    return actions, [
        {"slot_id": slot, "reason_code": idle_reason} for slot in free_slots
    ]


def _fast_lane_build_schedule(
    validated: Mapping[str, Any], activation: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _fast_lane_select_actions(validated, activation)


def _fast_lane_assignment(
    validated: Mapping[str, Any],
    unit: Mapping[str, Any],
    role: str,
    slot_id: str,
    route: Mapping[str, Any],
) -> dict[str, Any]:
    state = validated["scheduler_state"]
    epoch = int(state.get("slot_epochs", {}).get(slot_id, 0)) + 1
    task_id = str(unit["task_id"])
    context = _fast_lane_build_dispatch_context(validated, unit, role)
    receipt = {
        "schema": "team-efficiency/fast-lane-dispatch-receipt-v1",
        "source_plan_hash": validated["source_plan_hash"],
        "task_id": task_id,
        "role": role,
        "slot_id": slot_id,
        "assignment_epoch": epoch,
        "model": route["model"],
        "reasoning_effort": route["reasoning_effort"],
        "routing_context_hash": route["routing_context_hash"],
        "routing_result_hash": route["routing_result_hash"],
        "task_fingerprint": route["task_fingerprint"],
        "routing_reason_codes": list(route["routing_reason_codes"]),
        "routing_safety_floor_rank": route["routing_safety_floor_rank"],
        "routing_input": route["routing_input"],
        "dispatch_context_hash": context["context_hash"],
        "target_gates_hash": context["target_gates_hash"],
        "execution_context_hash": context["execution_context_hash"],
        "read_context_hash": context["read_context_hash"],
        "recovery_of_assignment_token": None,
    }
    token = _fast_lane_assignment_token(receipt)
    return {
        "slot_id": slot_id,
        "action": "start",
        "task_id": task_id,
        "role": role,
        "assignment_epoch": epoch,
        "assignment_token": token,
        "context_hash": context["context_hash"],
        "model": route["model"],
        "reasoning_effort": route["reasoning_effort"],
        "routing_context_hash": route["routing_context_hash"],
        "routing_result_hash": route["routing_result_hash"],
        "task_fingerprint": route["task_fingerprint"],
        "routing_reason_codes": list(route["routing_reason_codes"]),
        "routing_safety_floor_rank": route["routing_safety_floor_rank"],
        "dispatch_receipt": receipt,
        "_context": context,
    }


def _fast_lane_build_dispatch_context(
    validated: Mapping[str, Any], unit: Mapping[str, Any], role: str
) -> dict[str, Any]:
    task_id = str(unit["task_id"])
    execution = (
        next(
            (
                item
                for item in validated["execution_contexts"]
                if item["task_id"] == task_id
            ),
            None,
        )
        if role == "execution"
        else None
    )
    read = next(
        (
            item
            for item in validated["read_contexts"]
            if item["task_id"] == task_id and item["role"] == role
        ),
        None,
    )
    target = (
        next(
            (item for item in validated["target_gates"] if item["task_id"] == task_id),
            None,
        )
        if role in {"execution", "verification"}
        else None
    )
    candidate = next(
        (
            item
            for item in validated["scheduler_state"].get("review_ready_candidates", [])
            if item["task_id"] == task_id
        ),
        None,
    )
    prewarm = next(
        (
            item
            for item in validated["scheduler_state"].get("prewarmed_evidence", [])
            if item["task_id"] == task_id
        ),
        None,
    )
    prewarm_is_revalidated = prewarm is not None and all(
        prewarm[field] is not None
        for field in (
            "revalidation_basis_hash",
            "dependency_delta_hash",
            "revalidation_evidence_hash",
        )
    )
    write_scope = [] if role == "verification" else list(unit.get("write_scope", []))
    work_context = execution if execution is not None else read
    review_candidate = candidate if role == "review" else None
    normalized = {
        "task_id": task_id,
        "role": role,
        "source_plan_hash": validated["source_plan_hash"],
        "integration_commit": validated["scheduler_state"]["integration_state"][
            "commit"
        ],
        "integration_tree": validated["scheduler_state"]["integration_state"]["tree"],
        "workspace_input_snapshot_id": (
            None
            if work_context is None
            else work_context["workspace_input_snapshot_id"]
        ),
        "direct_dependency_result_hashes": [],
        "direct_contract_hashes": list(unit.get("direct_contract_hashes", [])),
        "required_evidence": list(unit.get("required_evidence", [])),
        "task_node_ids": list(unit.get("task_node_ids", [])),
        "contract_node_ids": list(unit.get("contract_node_ids", [])),
        "acceptance_constraints": list(unit.get("acceptance_constraints", [])),
        "execution_context_hash": None
        if execution is None
        else _sha256_json(execution),
        "bootstrap_plan_hash": None
        if execution is None
        else _sha256_json(execution["bootstrap_plan"]),
        "base_commit": None
        if execution is None
        else execution["bootstrap_plan"]["base_commit"],
        "branch": None if execution is None else execution["bootstrap_plan"]["branch"],
        "write_scope_hash": _sha256_json(write_scope),
        "read_context_hash": None if read is None else _sha256_json(read),
        "target_gates_hash": None
        if target is None
        else _sha256_json(
            {
                "driver_gate_id": target["driver_gate_id"],
                "target_gates": target["gates"],
            }
        ),
        "candidate_commit": (
            None if review_candidate is None else review_candidate["candidate_commit"]
        ),
        "red_evidence_hashes": (
            []
            if review_candidate is None
            else list(review_candidate["red_evidence_hashes"])
        ),
        "green_evidence_hashes": (
            []
            if review_candidate is None
            else list(review_candidate["green_evidence_hashes"])
        ),
        "basis_hash": _sha256_json(read)
        if role in {"prewarm", "design_probe"} and read is not None
        else None,
        "prewarm_evidence_hash": (
            None
            if role != "execution" or prewarm is None or not prewarm_is_revalidated
            else prewarm["evidence_hash"]
        ),
        "prewarm_revalidation_evidence_hash": (
            None
            if role != "execution" or prewarm is None or not prewarm_is_revalidated
            else prewarm["revalidation_evidence_hash"]
        ),
    }
    return {"context_hash": _sha256_json(normalized), **normalized}


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


def _fast_lane_routing_core() -> Any | None:
    """Load the pure local routing core without giving the scheduler an executor."""

    module_name = "_team_efficiency_fastlane_routing"
    core_path = Path(__file__).with_name("fastlane_routing.py").resolve()
    loaded = sys.modules.get(module_name)
    if (
        loaded is not None
        and Path(str(getattr(loaded, "__file__", ""))).resolve() == core_path
    ):
        return loaded
    try:
        spec = importlib.util.spec_from_file_location(module_name, core_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        sys.modules.pop(module_name, None)
        return None


def _fast_lane_failure_reason(value: object) -> str:
    """Project only bounded core failures into the scheduler's idle vocabulary."""

    if value == "capability_unavailable":
        return "CAPABILITY_UNAVAILABLE"
    if value == "routing_context_missing":
        return "ROUTING_CONTEXT_MISSING"
    if value in {
        "routing_context_duplicate",
        "routing_context_invalid",
        "routing_context_mismatch",
    }:
        return "ROUTING_CONTEXT_INVALID"
    return "ROUTING_REJECTED"


def _fast_lane_reason_codes(value: object) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if not value or len(value) > 16:
        return None
    normalized: list[str] = []
    for index, item in enumerate(value):
        try:
            code = _label(item, f"routing reason_codes[{index}]")
        except ValueError:
            return None
        if code in normalized:
            return None
        normalized.append(code)
    return normalized


def _fast_lane_core_decision(
    entry: Mapping[str, Any],
    *,
    source_plan_hash: str,
) -> tuple[dict[str, Any] | None, str]:
    """Run one full host-attested request through the owned pure core only."""

    task_id = entry["task_id"]
    scheduler_role = entry["scheduler_role"]
    expected_core_role = _FAST_LANE_CORE_ROLE_BY_SCHEDULER_ROLE[scheduler_role]
    expected_access = (
        "workspace_write" if scheduler_role == "execution" else "read_only"
    )
    raw_request = entry["request"]
    try:
        request = _mapping(raw_request, "routing request")
        task = _mapping(request.get("task"), "routing request.task")
        if task.get("task_id") != task_id or task.get("role") != expected_core_role:
            return None, "routing_context_mismatch"
        core = _fast_lane_routing_core()
        if core is None:
            return None, "routing_context_invalid"
        result = core.route(
            request,
            trusted_authorization_evidence_hashes=entry[
                "trusted_authorization_evidence_hashes"
            ],
            trusted_override_receipt_hashes=entry["trusted_override_receipt_hashes"],
            trusted_evidence_hashes=entry["trusted_evidence_hashes"],
            coordinator_endpoint_hash=entry["coordinator_endpoint_hash"],
            compatibility_floor=entry["compatibility_floor"],
        )
    except (KeyError, TypeError, ValueError):
        return None, "routing_context_invalid"
    if not isinstance(result, Mapping):
        return None, "routing_context_invalid"
    reason_codes = _fast_lane_reason_codes(result.get("reason_codes"))
    if result.get("status") != "resolved":
        return None, (
            "routing_context_invalid" if reason_codes is None else str(reason_codes[0])
        )
    route = result.get("route")
    safety_floor = result.get("safety_floor")
    try:
        route_value = _mapping(route, "core route")
        model = _text(route_value["model"], "core route.model", maximum=64)
        effort = _text(route_value["effort"], "core route.effort", maximum=16)
        if effort == "ultra" or effort not in FAST_LANE_REASONING_EFFORTS:
            return None, "routing_context_invalid"
        floor = _mapping(safety_floor, "core safety_floor")
        floor_rank = floor["rank"]
        if type(floor_rank) is not int or not 10 <= floor_rank <= 110:
            return None, "routing_context_invalid"
        task_fingerprint = _hash(result["task_fingerprint"], "core task_fingerprint")
        render_hash = _hash(result["render_hash"], "core render_hash")
        scheduler_facts = _mapping(
            request["scheduler_facts"], "routing request.scheduler_facts"
        )
    except (KeyError, TypeError, ValueError):
        return None, "routing_context_invalid"
    if (
        result.get("task_id") != task_id
        or result.get("effective_role") != expected_core_role
        or result.get("access") != expected_access
        or reason_codes is None
    ):
        return None, "routing_context_mismatch"
    context_hash = _sha256_json(
        {
            "schema": "team-efficiency/fast-lane-routing-context-binding-v1",
            "source_plan_hash": source_plan_hash,
            "task_id": task_id,
            "scheduler_role": scheduler_role,
            "routing_request_hash": _sha256_json(request),
            "scheduler_facts_hash": _sha256_json(scheduler_facts),
        }
    )
    return (
        {
            "model": model,
            "reasoning_effort": effort,
            "routing_context_hash": context_hash,
            "routing_result_hash": _sha256_json(dict(result)),
            "task_fingerprint": task_fingerprint,
            "routing_reason_codes": reason_codes,
            "routing_safety_floor_rank": floor_rank,
            "routing_render_hash": render_hash,
            # Preserve the bounded, host-attested core input in the durable
            # receipt.  Lifecycle validation replays this historical input,
            # rather than re-evaluating a later scheduler event as if it had
            # been the original dispatch decision.
            "routing_input": json.loads(_canonical_json(entry)),
        },
        "",
    )


def _fast_lane_historical_receipt_route(
    receipt: Mapping[str, Any], *, source_plan_hash: str
) -> dict[str, Any]:
    """Replay the immutable core input that produced a durable receipt.

    A lifecycle pass necessarily has newer scheduler facts than the dispatch
    event.  Replaying the receipt's bounded input preserves that event binding
    while still making every persisted model/effort/hash claim answer to the
    pure routing core.
    """

    routing_input = receipt["routing_input"]
    if (
        routing_input["task_id"] != receipt["task_id"]
        or routing_input["scheduler_role"] != receipt["role"]
    ):
        raise ValueError("dispatch receipt routing key is invalid")
    decision, _reason = _fast_lane_core_decision(
        routing_input, source_plan_hash=source_plan_hash
    )
    if decision is None or any(
        receipt[field] != decision[field]
        for field in (
            "model",
            "reasoning_effort",
            "routing_context_hash",
            "routing_result_hash",
            "task_fingerprint",
            "routing_reason_codes",
            "routing_safety_floor_rank",
        )
    ):
        raise ValueError("dispatch receipt historical route is invalid")
    return decision


def _fast_lane_routing_context(
    value: Mapping[str, Any] | None,
    *,
    source_plan: Mapping[str, Any],
    source_plan_hash: str,
) -> dict[str, Any]:
    """Bind one complete, globally fail-closed core route matrix."""

    units = _fast_lane_unit_index(source_plan)
    expected_keys = {
        (task_id, scheduler_role)
        for task_id in units
        for scheduler_role in _FAST_LANE_ROLES
    }
    decisions: dict[tuple[str, str], dict[str, Any] | None] = {}
    reasons: dict[tuple[str, str], str] = {}
    failure_reasons: list[str] = []
    routes = () if value is None else value["routes"]
    for entry in routes:
        task_id = entry["task_id"]
        scheduler_role = entry["scheduler_role"]
        key = (task_id, scheduler_role)
        if key in decisions:
            decisions[key] = None
            reasons[key] = "routing_context_duplicate"
            failure_reasons.append("routing_context_duplicate")
            continue
        if key not in expected_keys:
            decisions[key] = None
            reasons[key] = "routing_context_mismatch"
            failure_reasons.append("routing_context_mismatch")
            continue
        decision, reason = _fast_lane_core_decision(
            entry, source_plan_hash=source_plan_hash
        )
        decisions[key] = decision
        if decision is None:
            reasons[key] = reason
            if reason not in _FAST_LANE_DEFERRED_ROUTE_REASONS:
                failure_reasons.append(reason)
    for key in sorted(expected_keys - set(decisions)):
        decisions[key] = None
        reasons[key] = "routing_context_missing"
        failure_reasons.append("routing_context_missing")
    return {
        "decisions": decisions,
        "reasons": reasons,
        "default_reason": "routing_context_missing",
        "global_failure_reason": (None if not failure_reasons else failure_reasons[0]),
    }


def _fast_lane_route(
    routing_context: Mapping[str, Any], unit: Mapping[str, Any], role: str
) -> dict[str, Any] | None:
    """Return only a core-resolved, exact host-attested worker route."""

    decision = routing_context["decisions"].get((unit["task_id"], role))
    return dict(decision) if isinstance(decision, Mapping) else None


def _fast_lane_route_reason(
    routing_context: Mapping[str, Any], unit: Mapping[str, Any], role: str
) -> str:
    return _fast_lane_failure_reason(
        routing_context["reasons"].get(
            (unit["task_id"], role), routing_context["default_reason"]
        )
    )


def _fast_lane_unroutable_action(
    validated: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str] | None:
    """Return an eligible action whose exact core route is unavailable.

    The selector deliberately leaves an unresolved route out of its action
    list.  When that leaves every slot idle, this companion check converts the
    absence into the established ``NO_SAFE_WORK`` outcome for every scheduler
    role, rather than quietly emitting an empty active plan.
    """

    source_plan = validated["source_plan"]
    units = _fast_lane_unit_index(source_plan)
    state = validated["scheduler_state"]
    completed = _fast_lane_completed_ids(state)
    blocked = frozenset(state.get("blocked_task_ids", []))
    running = {
        assignment["task_id"] for assignment in state.get("running_assignments", [])
    }
    routing_context = validated["routing_context"]
    phase = state["phase"]
    read_contexts = {
        (context["task_id"], context["role"])
        for context in validated.get("read_contexts", [])
    }

    if phase == "integration_regression":
        for task_id, unit in sorted(units.items()):
            if (
                unit.get("unit_kind") == "verification"
                and task_id not in completed | blocked | running
                and _fast_lane_dependency_ready(unit, completed)
                and (task_id, "verification") in read_contexts
                and _fast_lane_route(routing_context, unit, "verification") is None
            ):
                return unit, "verification"
        return None

    if phase == "remediation":
        for task_id, unit in sorted(units.items()):
            if (
                unit.get("unit_kind") == "remediation"
                and task_id not in completed | blocked | running
                and _fast_lane_dependency_ready(unit, completed)
                and _fast_lane_route(routing_context, unit, "execution") is None
            ):
                return unit, "execution"
        return None

    if phase != "execution":
        return None

    candidates = {
        record["task_id"] for record in state.get("review_ready_candidates", [])
    }
    reviewed = {record["task_id"] for record in state.get("reviewed_candidates", [])}
    graph = _fast_lane_conflict_graph(
        units,
        lane0_scopes=state["lane0_state"]["owned_write_scopes"],
        running=state.get("running_assignments", []),
    )
    for unit in _fast_lane_ready_items(
        units,
        completed,
        blocked,
        frozenset(running),
        frozenset(candidates),
        frozenset(reviewed),
        conflict_graph=graph,
    ):
        if _fast_lane_route(routing_context, unit, "execution") is None:
            return unit, "execution"

    for record in state.get("review_ready_candidates", []):
        task_id = record["task_id"]
        if (
            task_id in units
            and (task_id, "review") in read_contexts
            and _fast_lane_route(routing_context, units[task_id], "review") is None
        ):
            return units[task_id], "review"

    for task_id in state.get("pending_design_probe_task_ids", []):
        if (
            task_id in units
            and (task_id, "design_probe") in read_contexts
            and _fast_lane_route(routing_context, units[task_id], "design_probe")
            is None
        ):
            return units[task_id], "design_probe"

    prewarm_ids = _fast_lane_preferred_prewarms(
        units=units,
        completed=completed,
        running=frozenset(running),
        candidate=frozenset(candidates),
        reviewed=frozenset(reviewed),
        read_contexts=read_contexts,
        source_plan=source_plan,
        blocked=blocked,
    )
    for task_id in prewarm_ids:
        if _fast_lane_route(routing_context, units[task_id], "prewarm") is None:
            return units[task_id], "prewarm"
    return None


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
    if (
        type(green_expected_exit_code) is not int
        or not 0 <= green_expected_exit_code <= 255
    ):
        raise ValueError(f"{field}.green_expected_exit_code is invalid")
    timeout_seconds = gate["timeout_seconds"]
    if (
        type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= MAX_GATE_TIMEOUT_SECONDS
    ):
        raise ValueError(f"{field}.timeout_seconds is invalid")
    red_failure_ids = _fast_lane_failure_ids(
        gate["red_failure_ids"], f"{field}.red_failure_ids"
    )
    expected_fingerprint = _fast_lane_red_failure_fingerprint(gate_id, red_failure_ids)
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
    normalized_targets = sorted(
        normalized_targets, key=lambda target: target["task_id"]
    )

    source_plan = _mapping(source_plan_value, "source plan")
    if _text(source_plan["status"], "source plan.status", maximum=32) != "planned":
        return normalized_targets
    source_kind = _text(
        source_plan["source_kind"], "source plan.source_kind", maximum=64
    )
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
        if source_kind not in {"code_atlas_packet", "task_episode_graph"}:
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
        if source_kind == "code_atlas_packet":
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


def _fast_lane_integration_state(value: object) -> dict[str, Any]:
    scheduler_state = _mapping(value, "scheduler_state")
    integration = _mapping(
        scheduler_state.get("integration_state"), "scheduler_state.integration_state"
    )
    commit = _git_id(
        integration.get("commit"), "scheduler_state.integration_state.commit"
    )
    tree = _git_id(integration.get("tree"), "scheduler_state.integration_state.tree")
    snapshot_value = integration.get("integration_workspace_snapshot_id")
    snapshot = (
        None
        if snapshot_value is None
        else _hash(
            snapshot_value,
            "scheduler_state.integration_state.integration_workspace_snapshot_id",
        )
    )
    return {
        "commit": commit,
        "tree": tree,
        "integration_workspace_snapshot_id": snapshot,
    }


def _validated_fast_lane_execution_context(
    value: object,
    unit: Mapping[str, Any],
    integration_state: Mapping[str, Any],
) -> dict[str, Any]:
    context = _mapping(value, "execution context")
    _exact_keys(context, _FAST_LANE_EXECUTION_CONTEXT_FIELDS, "execution context")
    task_id = _task_id(context["task_id"], "execution context.task_id")
    plan = _validated_bootstrap_plan(context["bootstrap_plan"])
    if task_id != plan["task_id"]:
        raise ValueError("execution context task does not match bootstrap plan")

    unit_scope = _normalised_scopes(
        unit.get("write_scope"), f"execution context {task_id}.write_scope"
    )
    if plan["write_scope"] != unit_scope:
        raise ValueError("execution context write scope does not match source unit")
    if plan["base_commit"] != integration_state["commit"]:
        raise ValueError("execution context base does not match integration commit")

    snapshot_value = context["workspace_input_snapshot_id"]
    snapshot = (
        None
        if snapshot_value is None
        else _hash(snapshot_value, "execution context.workspace_input_snapshot_id")
    )
    strict_index = unit.get("strict_index")
    if strict_index is not None and type(strict_index) is not bool:
        raise ValueError("execution context strict_index is invalid")
    if strict_index is True and snapshot is None:
        raise ValueError("strict execution context requires an input snapshot")
    return {
        "task_id": task_id,
        "bootstrap_plan": plan,
        "workspace_input_snapshot_id": snapshot,
    }


def _validated_fast_lane_read_context(
    value: object,
    unit: Mapping[str, Any],
    integration_state: Mapping[str, Any],
) -> dict[str, Any]:
    context = _mapping(value, "read context")
    _exact_keys(context, _FAST_LANE_READ_CONTEXT_FIELDS, "read context")
    task_id = _task_id(context["task_id"], "read context.task_id")
    role = _text(context["role"], "read context.role", maximum=32)
    if role not in _FAST_LANE_READ_ROLES:
        raise ValueError("read context role is invalid")
    repo = _absolute_path(context["repo"], "read context.repo")
    worktree = _absolute_path(context["worktree"], "read context.worktree")
    if worktree == repo:
        raise ValueError("read context worktree must differ from repo")
    base_commit = _git_id(context["base_commit"], "read context.base_commit")
    tree = _git_id(context["tree"], "read context.tree")
    snapshot = _hash(
        context["workspace_input_snapshot_id"],
        "read context.workspace_input_snapshot_id",
    )
    read_scope = _normalised_scopes(context["read_scope"], "read context.read_scope")
    temp_target = _absolute_path(context["temp_target"], "read context.temp_target")

    unit_kind = unit.get("unit_kind")
    if role == "verification" and unit_kind is not None and unit_kind != "verification":
        raise ValueError("verification read context must bind a verification unit")
    if role == "verification":
        if base_commit != integration_state["commit"]:
            raise ValueError("verification read context commit is stale")
        if tree != integration_state["tree"]:
            raise ValueError("verification read context tree is stale")
    return {
        "task_id": task_id,
        "role": role,
        "repo": str(repo),
        "worktree": str(worktree),
        "base_commit": base_commit,
        "tree": tree,
        "workspace_input_snapshot_id": snapshot,
        "read_scope": read_scope,
        "temp_target": str(temp_target),
    }


def _fast_lane_path_identity(value: str | Path) -> str:
    return str(value).casefold()


def _validated_fast_lane_contexts(
    execution_value: object,
    read_value: object,
    source_plan: Mapping[str, Any],
    scheduler_state: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    execution_records = _bounded_records(
        execution_value,
        "execution_contexts",
        maximum=MAX_MANIFEST_UNITS,
    )
    read_records = _bounded_records(
        read_value,
        "read_contexts",
        maximum=MAX_MANIFEST_UNITS,
    )
    if source_plan.get("status") != "planned":
        if execution_records or read_records:
            raise ValueError("contexts require a planned source")
        return [], []

    units_value = source_plan.get("units")
    units = _bounded_records(
        units_value, "source plan.units", maximum=MAX_MANIFEST_UNITS
    )
    units_by_task_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(units):
        unit = _mapping(item, f"source plan.units[{index}]")
        task_id = _task_id(unit.get("task_id"), f"source plan.units[{index}].task_id")
        if task_id in units_by_task_id:
            raise ValueError("source plan contains duplicate task ids")
        units_by_task_id[task_id] = unit
    integration_state = _fast_lane_integration_state(scheduler_state)

    execution_contexts: list[dict[str, Any]] = []
    read_contexts: list[dict[str, Any]] = []
    execution_task_ids: set[str] = set()
    read_keys: set[tuple[str, str]] = set()
    branches: set[str] = set()
    worktrees: set[str] = set()
    temp_targets: set[str] = set()
    repo_anchor: str | None = None
    project_roots: list[Path] = []

    for index, record in enumerate(execution_records):
        field = f"execution_contexts[{index}]"
        context = _mapping(record, field)
        task_id = _task_id(context.get("task_id"), f"{field}.task_id")
        if task_id not in units_by_task_id:
            raise ValueError("execution context task is unknown")
        if task_id in execution_task_ids:
            raise ValueError("execution_contexts contains duplicate task ids")
        normalized = _validated_fast_lane_execution_context(
            record,
            units_by_task_id[task_id],
            integration_state,
        )
        plan = normalized["bootstrap_plan"]
        repo = _absolute_path(plan["repo"], f"{field}.bootstrap_plan.repo")
        worktree = _absolute_path(plan["worktree"], f"{field}.bootstrap_plan.worktree")
        temp_target = _absolute_path(
            plan["temp_target"], f"{field}.bootstrap_plan.temp_target"
        )
        repo_id = _fast_lane_path_identity(repo)
        if repo_anchor is None:
            repo_anchor = repo_id
        elif repo_anchor != repo_id:
            raise ValueError("fast-lane contexts must share one repo anchor")
        worktree_id = _fast_lane_path_identity(worktree)
        temp_id = _fast_lane_path_identity(temp_target)
        if worktree_id == repo_anchor:
            raise ValueError("execution context worktree must differ from repo")
        if worktree_id in worktrees:
            raise ValueError("fast-lane contexts contain duplicate worktrees")
        if temp_id in temp_targets:
            raise ValueError("fast-lane contexts contain duplicate temp targets")
        if temp_id == worktree_id:
            raise ValueError("fast-lane context worktree and temp target must differ")
        branch = plan["branch"].casefold()
        if branch in branches:
            raise ValueError("fast-lane contexts contain duplicate branches")
        branches.add(branch)
        worktrees.add(worktree_id)
        temp_targets.add(temp_id)
        _, project_root = _project_root(plan["project"])
        project_roots.append(project_root)
        execution_task_ids.add(task_id)
        execution_contexts.append(normalized)

    execution_capable_ids = {
        task_id
        for task_id, unit in units_by_task_id.items()
        if unit.get("unit_kind") != "verification"
    }
    if execution_records and execution_task_ids != execution_capable_ids:
        raise ValueError("execution contexts must cover the source task set")

    codex_root = Path(r"D:\bun\tmp\codex").resolve(strict=False)
    for index, record in enumerate(read_records):
        field = f"read_contexts[{index}]"
        context = _mapping(record, field)
        task_id = _task_id(context.get("task_id"), f"{field}.task_id")
        if task_id not in units_by_task_id:
            raise ValueError("read context task is unknown")
        normalized = _validated_fast_lane_read_context(
            record,
            units_by_task_id[task_id],
            integration_state,
        )
        role = normalized["role"]
        key = (task_id, role)
        if key in read_keys:
            raise ValueError("read_contexts contains duplicate task roles")
        read_keys.add(key)
        repo = _absolute_path(normalized["repo"], f"{field}.repo")
        worktree = _absolute_path(normalized["worktree"], f"{field}.worktree")
        temp_target = _absolute_path(normalized["temp_target"], f"{field}.temp_target")
        repo_id = _fast_lane_path_identity(repo)
        if repo_anchor is None:
            repo_anchor = repo_id
        elif repo_anchor != repo_id:
            raise ValueError("fast-lane contexts must share one repo anchor")
        worktree_id = _fast_lane_path_identity(worktree)
        temp_id = _fast_lane_path_identity(temp_target)
        if worktree_id == repo_anchor:
            raise ValueError("read context worktree must differ from repo")
        if worktree_id in worktrees:
            raise ValueError("fast-lane contexts contain duplicate worktrees")
        if temp_id in temp_targets:
            raise ValueError("fast-lane contexts contain duplicate temp targets")
        if temp_id == worktree_id:
            raise ValueError("fast-lane context worktree and temp target must differ")
        roots = project_roots or [codex_root]
        if not any(
            temp_target != root and temp_target.is_relative_to(root) for root in roots
        ):
            raise ValueError("read context temp target must stay below the task root")
        worktrees.add(worktree_id)
        temp_targets.add(temp_id)
        read_contexts.append(normalized)
    phase = _text(scheduler_state.get("phase"), "scheduler_state.phase", maximum=32)
    if phase == "integration_regression":
        verification_contexts = [
            context for context in read_contexts if context["role"] == "verification"
        ]
        if len(verification_contexts) != 1:
            raise ValueError(
                "integration regression requires one verification read context"
            )
    return execution_contexts, read_contexts


def _fast_lane_optional_hash(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _hash(value, field)


def _fast_lane_optional_git_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _git_id(value, field)


def _fast_lane_hash_list(value: object, field: str) -> list[str]:
    return _normalised_list(value, field, _hash, maximum=MAX_LIST_ITEMS)


def _fast_lane_label_list(value: object, field: str) -> list[str]:
    def normalise(item: object, item_field: str) -> str:
        if isinstance(item, str) and _SHA256.fullmatch(item):
            return _hash(item, item_field)
        return _label(item, item_field)

    return _normalised_list(value, field, normalise, maximum=MAX_LIST_ITEMS)


def _validated_fast_lane_dispatch_context(
    value: object,
    *,
    source_plan_hash: str,
    task_ids: set[str],
) -> dict[str, Any]:
    record = _mapping(value, "scheduler_state.dispatch_contexts item")
    _exact_keys(record, _FAST_LANE_DISPATCH_CONTEXT_FIELDS, "dispatch context")
    task_id = _task_id(record["task_id"], "dispatch context.task_id")
    if task_id not in task_ids:
        raise ValueError("dispatch context task is unknown")
    role = _text(record["role"], "dispatch context.role", maximum=32)
    if role not in {"execution", "verification", "prewarm", "review", "design_probe"}:
        raise ValueError("dispatch context role is invalid")
    if (
        _hash(record["source_plan_hash"], "dispatch context.source_plan_hash")
        != source_plan_hash
    ):
        raise ValueError("dispatch context source plan is stale")
    normalized: dict[str, Any] = {
        "task_id": task_id,
        "role": role,
        "source_plan_hash": source_plan_hash,
        "integration_commit": _git_id(
            record["integration_commit"], "dispatch context.integration_commit"
        ),
        "integration_tree": _git_id(
            record["integration_tree"], "dispatch context.integration_tree"
        ),
        "workspace_input_snapshot_id": _fast_lane_optional_hash(
            record["workspace_input_snapshot_id"],
            "dispatch context.workspace_input_snapshot_id",
        ),
        "direct_dependency_result_hashes": _fast_lane_hash_list(
            record["direct_dependency_result_hashes"],
            "dispatch context.direct_dependency_result_hashes",
        ),
        "direct_contract_hashes": _fast_lane_hash_list(
            record["direct_contract_hashes"],
            "dispatch context.direct_contract_hashes",
        ),
        "required_evidence": _fast_lane_label_list(
            record["required_evidence"], "dispatch context.required_evidence"
        ),
        "task_node_ids": _fast_lane_hash_list(
            record["task_node_ids"], "dispatch context.task_node_ids"
        ),
        "contract_node_ids": _fast_lane_hash_list(
            record["contract_node_ids"], "dispatch context.contract_node_ids"
        ),
        "acceptance_constraints": _fast_lane_hash_list(
            record["acceptance_constraints"],
            "dispatch context.acceptance_constraints",
        ),
        "execution_context_hash": _fast_lane_optional_hash(
            record["execution_context_hash"],
            "dispatch context.execution_context_hash",
        ),
        "bootstrap_plan_hash": _fast_lane_optional_hash(
            record["bootstrap_plan_hash"], "dispatch context.bootstrap_plan_hash"
        ),
        "base_commit": _fast_lane_optional_git_id(
            record["base_commit"], "dispatch context.base_commit"
        ),
        "branch": None if record["branch"] is None else _branch(record["branch"]),
        "write_scope_hash": _fast_lane_optional_hash(
            record["write_scope_hash"], "dispatch context.write_scope_hash"
        ),
        "read_context_hash": _fast_lane_optional_hash(
            record["read_context_hash"], "dispatch context.read_context_hash"
        ),
        "target_gates_hash": _fast_lane_optional_hash(
            record["target_gates_hash"], "dispatch context.target_gates_hash"
        ),
        "candidate_commit": _fast_lane_optional_git_id(
            record["candidate_commit"], "dispatch context.candidate_commit"
        ),
        "red_evidence_hashes": _fast_lane_hash_list(
            record["red_evidence_hashes"], "dispatch context.red_evidence_hashes"
        ),
        "green_evidence_hashes": _fast_lane_hash_list(
            record["green_evidence_hashes"],
            "dispatch context.green_evidence_hashes",
        ),
        "basis_hash": _fast_lane_optional_hash(
            record["basis_hash"], "dispatch context.basis_hash"
        ),
        "prewarm_evidence_hash": _fast_lane_optional_hash(
            record["prewarm_evidence_hash"],
            "dispatch context.prewarm_evidence_hash",
        ),
        "prewarm_revalidation_evidence_hash": _fast_lane_optional_hash(
            record["prewarm_revalidation_evidence_hash"],
            "dispatch context.prewarm_revalidation_evidence_hash",
        ),
    }
    supplied_hash = _hash(record["context_hash"], "dispatch context.context_hash")
    if supplied_hash != _sha256_json(normalized):
        raise ValueError("dispatch context hash is invalid")
    normalized["context_hash"] = supplied_hash
    return normalized


def _validated_fast_lane_dispatch_receipt(value: object) -> dict[str, Any]:
    receipt = _mapping(value, "dispatch receipt")
    _exact_keys(receipt, _FAST_LANE_DISPATCH_RECEIPT_FIELDS, "dispatch receipt")
    if receipt["schema"] != "team-efficiency/fast-lane-dispatch-receipt-v1":
        raise ValueError("dispatch receipt schema is invalid")
    role = _text(receipt["role"], "dispatch receipt.role", maximum=32)
    if role not in {"execution", "verification", "prewarm", "review", "design_probe"}:
        raise ValueError("dispatch receipt role is invalid")
    epoch = receipt["assignment_epoch"]
    if type(epoch) is not int or epoch <= 0:
        raise ValueError("dispatch receipt epoch is invalid")
    normalized = {
        "schema": receipt["schema"],
        "source_plan_hash": _hash(
            receipt["source_plan_hash"], "dispatch receipt.source_plan_hash"
        ),
        "task_id": _task_id(receipt["task_id"], "dispatch receipt.task_id"),
        "role": role,
        "slot_id": _label(receipt["slot_id"], "dispatch receipt.slot_id"),
        "assignment_epoch": epoch,
        "model": _text(receipt["model"], "dispatch receipt.model", maximum=64),
        "reasoning_effort": _text(
            receipt["reasoning_effort"], "dispatch receipt.reasoning_effort", maximum=16
        ),
        "routing_context_hash": _hash(
            receipt["routing_context_hash"], "dispatch receipt.routing_context_hash"
        ),
        "routing_result_hash": _hash(
            receipt["routing_result_hash"], "dispatch receipt.routing_result_hash"
        ),
        "task_fingerprint": _hash(
            receipt["task_fingerprint"], "dispatch receipt.task_fingerprint"
        ),
        "routing_reason_codes": _fast_lane_reason_codes(
            receipt["routing_reason_codes"]
        ),
        "routing_safety_floor_rank": receipt["routing_safety_floor_rank"],
        "routing_input": _validated_fast_lane_routing_entry(
            receipt["routing_input"], "dispatch receipt.routing_input"
        ),
        "dispatch_context_hash": _hash(
            receipt["dispatch_context_hash"],
            "dispatch receipt.dispatch_context_hash",
        ),
        "target_gates_hash": _fast_lane_optional_hash(
            receipt["target_gates_hash"], "dispatch receipt.target_gates_hash"
        ),
        "execution_context_hash": _fast_lane_optional_hash(
            receipt["execution_context_hash"],
            "dispatch receipt.execution_context_hash",
        ),
        "read_context_hash": _fast_lane_optional_hash(
            receipt["read_context_hash"], "dispatch receipt.read_context_hash"
        ),
        "recovery_of_assignment_token": _fast_lane_optional_hash(
            receipt["recovery_of_assignment_token"],
            "dispatch receipt.recovery_of_assignment_token",
        ),
    }
    if normalized["reasoning_effort"] not in FAST_LANE_REASONING_EFFORTS:
        raise ValueError("dispatch receipt reasoning effort is invalid")
    if normalized["reasoning_effort"] == "ultra":
        raise ValueError("dispatch receipt cannot route ultra")
    if normalized["routing_reason_codes"] is None:
        raise ValueError("dispatch receipt routing reasons are invalid")
    if (
        type(normalized["routing_safety_floor_rank"]) is not int
        or not 10 <= normalized["routing_safety_floor_rank"] <= 110
    ):
        raise ValueError("dispatch receipt routing floor is invalid")
    return normalized


def _fast_lane_assignment_token(receipt: Mapping[str, Any]) -> str:
    return _sha256_json(dict(receipt))


def _validated_fast_lane_terminal_result(
    value: object,
    *,
    source_plan_hash: str,
    task_ids: set[str],
    units_by_task_id: Mapping[str, Mapping[str, Any]],
    context_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    slot_epochs: Mapping[str, int],
    routing_context: Mapping[str, Any],
) -> dict[str, Any]:
    result = _mapping(value, "terminal result")
    _exact_keys(result, _FAST_LANE_TERMINAL_RESULT_FIELDS, "terminal result")
    if result["schema"] != "team-efficiency/fast-lane-terminal-result-v1":
        raise ValueError("terminal result schema is invalid")
    task_id = _task_id(result["task_id"], "terminal result.task_id")
    if task_id not in task_ids:
        raise ValueError("terminal result task is unknown")
    if task_id not in units_by_task_id:
        raise ValueError("terminal result source unit is unknown")
    role = _text(result["role"], "terminal result.role", maximum=32)
    if role not in _FAST_LANE_ROLES:
        raise ValueError("terminal result role is invalid")
    receipt = _validated_fast_lane_dispatch_receipt(result["dispatch_receipt"])
    if (
        receipt["source_plan_hash"] != source_plan_hash
        or receipt["task_id"] != task_id
        or receipt["role"] != role
        or receipt["slot_id"] not in FAST_LANE_SLOT_IDS
        or receipt["assignment_epoch"] != slot_epochs[receipt["slot_id"]]
    ):
        raise ValueError("terminal result receipt is stale")
    assignment_token = _hash(
        result["assignment_token"], "terminal result.assignment_token"
    )
    if assignment_token != _fast_lane_assignment_token(receipt):
        raise ValueError("terminal result token is invalid")
    recovery_of = receipt["recovery_of_assignment_token"]
    if recovery_of is not None:
        if (
            role not in {"execution", "verification"}
            or receipt["assignment_epoch"] <= 1
        ):
            raise ValueError("terminal result recovery is invalid")
        predecessor = dict(receipt)
        predecessor["assignment_epoch"] = receipt["assignment_epoch"] - 1
        predecessor["recovery_of_assignment_token"] = None
        if recovery_of != _fast_lane_assignment_token(predecessor):
            raise ValueError("terminal result recovery predecessor is invalid")
    context = context_by_key.get((task_id, role))
    if context is None:
        raise ValueError("terminal result context is unknown")
    if (
        receipt["dispatch_context_hash"] != context["context_hash"]
        or receipt["target_gates_hash"] != context["target_gates_hash"]
        or receipt["execution_context_hash"] != context["execution_context_hash"]
        or receipt["read_context_hash"] != context["read_context_hash"]
    ):
        raise ValueError("terminal result receipt is not bound to its context")
    _fast_lane_historical_receipt_route(receipt, source_plan_hash=source_plan_hash)

    outcome = _text(result["outcome"], "terminal result.outcome", maximum=32)
    candidate_commit = _fast_lane_optional_git_id(
        result["candidate_commit"], "terminal result.candidate_commit"
    )
    candidate_tree = _fast_lane_optional_git_id(
        result["candidate_tree"], "terminal result.candidate_tree"
    )
    red_evidence_hashes = _fast_lane_hash_list(
        result["red_evidence_hashes"], "terminal result.red_evidence_hashes"
    )
    green_evidence_hashes = _fast_lane_hash_list(
        result["green_evidence_hashes"], "terminal result.green_evidence_hashes"
    )
    evidence_hash = _fast_lane_optional_hash(
        result["evidence_hash"], "terminal result.evidence_hash"
    )
    review_hash = _fast_lane_optional_hash(
        result["review_hash"], "terminal result.review_hash"
    )
    input_query_trace_id = _fast_lane_optional_hash(
        result["input_query_trace_id"], "terminal result.input_query_trace_id"
    )
    checkpoint_id = _fast_lane_optional_hash(
        result["checkpoint_id"], "terminal result.checkpoint_id"
    )
    output_workspace_snapshot_id = _fast_lane_optional_hash(
        result["output_workspace_snapshot_id"],
        "terminal result.output_workspace_snapshot_id",
    )
    output_query_trace_id = _fast_lane_optional_hash(
        result["output_query_trace_id"], "terminal result.output_query_trace_id"
    )
    normalized = {
        "schema": result["schema"],
        "dispatch_receipt": receipt,
        "assignment_token": assignment_token,
        "task_id": task_id,
        "role": role,
        "outcome": outcome,
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "red_evidence_hashes": red_evidence_hashes,
        "green_evidence_hashes": green_evidence_hashes,
        "evidence_hash": evidence_hash,
        "review_hash": review_hash,
        "input_query_trace_id": input_query_trace_id,
        "checkpoint_id": checkpoint_id,
        "output_workspace_snapshot_id": output_workspace_snapshot_id,
        "output_query_trace_id": output_query_trace_id,
    }

    def no_success_evidence() -> bool:
        return (
            candidate_commit is None
            and candidate_tree is None
            and not red_evidence_hashes
            and not green_evidence_hashes
            and evidence_hash is None
            and review_hash is None
            and input_query_trace_id is None
            and checkpoint_id is None
            and output_workspace_snapshot_id is None
            and output_query_trace_id is None
        )

    if role == "execution" and outcome == "candidate":
        valid = (
            candidate_commit is not None
            and candidate_tree is not None
            and bool(red_evidence_hashes)
            and bool(green_evidence_hashes)
            and evidence_hash is None
            and review_hash is None
            and input_query_trace_id is not None
            and checkpoint_id is not None
            and output_workspace_snapshot_id is not None
            and output_query_trace_id is not None
        )
    elif role == "verification" and outcome == "verified":
        valid = (
            candidate_commit is None
            and candidate_tree is None
            and not red_evidence_hashes
            and bool(green_evidence_hashes)
            and evidence_hash is not None
            and review_hash is None
            and input_query_trace_id is not None
            and checkpoint_id is None
            and output_workspace_snapshot_id is None
            and output_query_trace_id is None
        )
    elif role in {"prewarm", "design_probe"} and outcome == "evidence":
        valid = (
            candidate_commit is None
            and candidate_tree is None
            and not red_evidence_hashes
            and not green_evidence_hashes
            and evidence_hash is not None
            and review_hash is None
            and input_query_trace_id is None
            and checkpoint_id is None
            and output_workspace_snapshot_id is None
            and output_query_trace_id is None
        )
    elif role == "review" and outcome == "pass":
        valid = (
            candidate_commit is None
            and candidate_tree is None
            and not red_evidence_hashes
            and not green_evidence_hashes
            and evidence_hash is None
            and review_hash is not None
            and input_query_trace_id is None
            and checkpoint_id is None
            and output_workspace_snapshot_id is None
            and output_query_trace_id is None
        )
    elif outcome in {"blocked", "failed", "obsolete"}:
        valid = no_success_evidence()
    else:
        valid = False
    if not valid:
        raise ValueError("terminal result outcome is invalid for its role")
    if _canonical_json(result) != _canonical_json(normalized):
        raise ValueError("terminal result is not canonical")
    return normalized


def _validated_fast_lane_completion_receipt(
    value: object,
    *,
    terminal_result: Mapping[str, Any],
    completion_kind: str,
    dispatch_context: Mapping[str, Any],
    verification_read_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    receipt = _mapping(value, "completion receipt")
    _exact_keys(receipt, _FAST_LANE_COMPLETION_RECEIPT_FIELDS, "completion receipt")
    if receipt["schema"] != "team-efficiency/fast-lane-completion-receipt-v1":
        raise ValueError("completion receipt schema is invalid")
    terminal_hash = _hash(
        receipt["terminal_result_hash"], "completion receipt.terminal_result_hash"
    )
    if terminal_hash != _sha256_json(dict(terminal_result)):
        raise ValueError("completion receipt terminal result is invalid")
    workflow_id_hash = _hash(
        receipt["workflow_id_hash"], "completion receipt.workflow_id_hash"
    )
    task_id = _task_id(receipt["task_id"], "completion receipt.task_id")
    if task_id != terminal_result["task_id"]:
        raise ValueError("completion receipt task is invalid")
    receipt_kind = _text(
        receipt["completion_kind"], "completion receipt.completion_kind", maximum=32
    )
    if receipt_kind != completion_kind:
        raise ValueError("completion receipt kind is invalid")
    integration_commit = _git_id(
        receipt["integration_commit"], "completion receipt.integration_commit"
    )
    integration_tree = _git_id(
        receipt["integration_tree"], "completion receipt.integration_tree"
    )
    candidate_commit = _fast_lane_optional_git_id(
        receipt["candidate_commit"], "completion receipt.candidate_commit"
    )
    candidate_tree = _fast_lane_optional_git_id(
        receipt["candidate_tree"], "completion receipt.candidate_tree"
    )
    integration_proof_hash = _fast_lane_optional_hash(
        receipt["integration_proof_hash"], "completion receipt.integration_proof_hash"
    )
    workspace_input_snapshot_id = _hash(
        receipt["workspace_input_snapshot_id"],
        "completion receipt.workspace_input_snapshot_id",
    )
    if workspace_input_snapshot_id != dispatch_context["workspace_input_snapshot_id"]:
        raise ValueError(
            "completion receipt input snapshot is not bound to its context"
        )
    output_workspace_snapshot_id = _fast_lane_optional_hash(
        receipt["output_workspace_snapshot_id"],
        "completion receipt.output_workspace_snapshot_id",
    )
    verification_evidence_hashes = _fast_lane_hash_list(
        receipt["verification_evidence_hashes"],
        "completion receipt.verification_evidence_hashes",
    )
    if completion_kind == "integrated_candidate":
        valid = (
            terminal_result["role"] == "execution"
            and terminal_result["outcome"] == "candidate"
            and candidate_commit == terminal_result["candidate_commit"]
            and candidate_tree == terminal_result["candidate_tree"]
            and integration_proof_hash is not None
            and output_workspace_snapshot_id
            == terminal_result["output_workspace_snapshot_id"]
            and verification_evidence_hashes == terminal_result["green_evidence_hashes"]
        )
    else:
        valid = (
            terminal_result["role"] == "verification"
            and terminal_result["outcome"] == "verified"
            and candidate_commit is None
            and candidate_tree is None
            and integration_proof_hash is None
            and output_workspace_snapshot_id is None
            and verification_evidence_hashes == terminal_result["green_evidence_hashes"]
        )
        if (
            verification_read_context is None
            or integration_commit != verification_read_context["base_commit"]
            or integration_tree != verification_read_context["tree"]
        ):
            raise ValueError(
                "verification completion receipt is not bound to its read context"
            )
    if not valid:
        raise ValueError("completion receipt is not bound to its terminal result")
    normalized = {
        "schema": receipt["schema"],
        "terminal_result_hash": terminal_hash,
        "workflow_id_hash": workflow_id_hash,
        "task_id": task_id,
        "completion_kind": receipt_kind,
        "integration_commit": integration_commit,
        "integration_tree": integration_tree,
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "integration_proof_hash": integration_proof_hash,
        "workspace_input_snapshot_id": workspace_input_snapshot_id,
        "output_workspace_snapshot_id": output_workspace_snapshot_id,
        "verification_evidence_hashes": verification_evidence_hashes,
    }
    if _canonical_json(receipt) != _canonical_json(normalized):
        raise ValueError("completion receipt is not canonical")
    return normalized


def _validated_fast_lane_lifecycle_records(
    *,
    completed_tasks: list[dict[str, Any]],
    review_ready_candidates: list[dict[str, Any]],
    reviewed_candidates: list[dict[str, Any]],
    prewarmed_evidence: list[dict[str, Any]],
    design_evidence: list[dict[str, Any]],
    source_plan_hash: str,
    task_ids: set[str],
    units_by_task_id: Mapping[str, Mapping[str, Any]],
    context_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    read_context_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    slot_epochs: Mapping[str, int],
    routing_context: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    used_tokens: set[str] = set()

    def terminal(value: object) -> dict[str, Any]:
        normalized = _validated_fast_lane_terminal_result(
            value,
            source_plan_hash=source_plan_hash,
            task_ids=task_ids,
            units_by_task_id=units_by_task_id,
            context_by_key=context_by_key,
            slot_epochs=slot_epochs,
            routing_context=routing_context,
        )
        token = normalized["assignment_token"]
        if token in used_tokens:
            raise ValueError("terminal result token is duplicated")
        used_tokens.add(token)
        return normalized

    normalized_completed: list[dict[str, Any]] = []
    for record in completed_tasks:
        terminal_result = terminal(record["terminal_result"])
        if terminal_result["task_id"] != record["task_id"] or record[
            "terminal_result_hash"
        ] != _sha256_json(terminal_result):
            raise ValueError("completed terminal result is invalid")
        completion_receipt = _validated_fast_lane_completion_receipt(
            record["completion_receipt"],
            terminal_result=terminal_result,
            completion_kind=record["completion_kind"],
            dispatch_context=context_by_key[
                (terminal_result["task_id"], terminal_result["role"])
            ],
            verification_read_context=read_context_by_key.get(
                (terminal_result["task_id"], "verification")
            ),
        )
        completion_receipt_hash = _sha256_json(completion_receipt)
        if (
            record["completion_receipt_hash"] != completion_receipt_hash
            or record["result_hash"] != completion_receipt_hash
            or record["integration_commit"] != completion_receipt["integration_commit"]
            or record["integration_tree"] != completion_receipt["integration_tree"]
        ):
            raise ValueError("completed receipt is invalid")
        normalized_completed.append(
            {
                **record,
                "terminal_result_hash": _sha256_json(terminal_result),
                "terminal_result": terminal_result,
                "completion_receipt_hash": completion_receipt_hash,
                "completion_receipt": completion_receipt,
            }
        )

    normalized_candidates: list[dict[str, Any]] = []
    for record in review_ready_candidates:
        terminal_result = terminal(record["terminal_result"])
        if (
            terminal_result["task_id"] != record["task_id"]
            or terminal_result["role"] != "execution"
            or terminal_result["outcome"] != "candidate"
            or record["candidate_commit"] != terminal_result["candidate_commit"]
            or record["candidate_tree"] != terminal_result["candidate_tree"]
            or record["red_evidence_hashes"] != terminal_result["red_evidence_hashes"]
            or record["green_evidence_hashes"]
            != terminal_result["green_evidence_hashes"]
            or record["terminal_result_hash"] != _sha256_json(terminal_result)
        ):
            raise ValueError("review-ready candidate is invalid")
        normalized_candidates.append(
            {
                **record,
                "terminal_result_hash": _sha256_json(terminal_result),
                "terminal_result": terminal_result,
            }
        )

    normalized_reviewed: list[dict[str, Any]] = []
    for record in reviewed_candidates:
        terminal_result = terminal(record["terminal_result"])
        review_terminal = terminal(record["review_terminal_result"])
        if (
            terminal_result["task_id"] != record["task_id"]
            or terminal_result["role"] != "execution"
            or terminal_result["outcome"] != "candidate"
            or review_terminal["task_id"] != record["task_id"]
            or review_terminal["role"] != "review"
            or review_terminal["outcome"] != "pass"
            or record["candidate_commit"] != terminal_result["candidate_commit"]
            or record["candidate_tree"] != terminal_result["candidate_tree"]
            or record["red_evidence_hashes"] != terminal_result["red_evidence_hashes"]
            or record["green_evidence_hashes"]
            != terminal_result["green_evidence_hashes"]
            or record["review_hash"] != review_terminal["review_hash"]
            or record["terminal_result_hash"] != _sha256_json(terminal_result)
            or record["review_terminal_result_hash"] != _sha256_json(review_terminal)
            or (review_context := context_by_key.get((record["task_id"], "review")))
            is None
            or review_context["candidate_commit"] != terminal_result["candidate_commit"]
            or review_context["red_evidence_hashes"]
            != terminal_result["red_evidence_hashes"]
            or review_context["green_evidence_hashes"]
            != terminal_result["green_evidence_hashes"]
        ):
            raise ValueError("reviewed candidate is invalid")
        normalized_reviewed.append(
            {
                **record,
                "terminal_result_hash": _sha256_json(terminal_result),
                "terminal_result": terminal_result,
                "review_terminal_result_hash": _sha256_json(review_terminal),
                "review_terminal_result": review_terminal,
            }
        )

    def annotation(records: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
        normalized_records: list[dict[str, Any]] = []
        for record in records:
            terminal_result = terminal(record["terminal_result"])
            context = context_by_key.get((record["task_id"], role))
            if (
                terminal_result["task_id"] != record["task_id"]
                or terminal_result["role"] != role
                or terminal_result["outcome"] != "evidence"
                or terminal_result["evidence_hash"] != record["evidence_hash"]
                or record["terminal_result_hash"] != _sha256_json(terminal_result)
                or context is None
                or context["basis_hash"] != record["observation_basis_hash"]
            ):
                raise ValueError("annotation evidence is invalid")
            normalized_records.append(
                {
                    **record,
                    "terminal_result_hash": _sha256_json(terminal_result),
                    "terminal_result": terminal_result,
                }
            )
        return normalized_records

    return (
        normalized_completed,
        normalized_candidates,
        normalized_reviewed,
        annotation(prewarmed_evidence, "prewarm"),
        annotation(design_evidence, "design_probe"),
    )


def _validated_fast_lane_remediation_request(
    value: object,
    *,
    source_plan: Mapping[str, Any],
    source_plan_hash: str,
    integration_state: Mapping[str, Any],
) -> dict[str, Any] | None:
    if value is None:
        return None
    request = _mapping(value, "remediation_request")
    _exact_keys(request, _FAST_LANE_REMEDIATION_FIELDS, "remediation_request")
    if request["schema"] != "team-efficiency/fast-lane-remediation-request-v1":
        raise ValueError("remediation request schema is invalid")
    round_value = request["round"]
    if round_value != 1:
        return {"_automation_stopped": True}
    blocker_hash = _hash(request["blocker_review_hash"], "remediation blocker hash")
    finding_hash = _hash(request["finding_hash"], "remediation finding hash")
    if (
        _hash(request["source_plan_hash"], "remediation source plan hash")
        != source_plan_hash
    ):
        raise ValueError("remediation source plan is stale")
    seed = _sha256_json(
        {
            "schema": "fast-lane-remediation-id-v1",
            "source_plan_hash": source_plan_hash,
            "blocker_review_hash": blocker_hash,
            "round": 1,
        }
    )
    task_id = _text(request["task_id"], "remediation task_id", maximum=32)
    expected_task_id = "FLR1-" + seed.split(":", 1)[1][:24]
    if task_id != expected_task_id:
        raise ValueError("remediation task id is not deterministic")
    severity = _text(request["severity"], "remediation severity", maximum=16)
    if severity not in {"critical", "important"}:
        raise ValueError("remediation severity is invalid")
    source_units = {
        _task_id(unit["task_id"], "source plan task_id"): unit
        for unit in source_plan.get("units", [])
    }
    source_ids = set(source_units)
    affected = _normalised_list(
        request["affected_task_ids"],
        "remediation affected_task_ids",
        _task_id,
        required=True,
    )
    dependencies = _normalised_list(
        request["dependencies"], "remediation dependencies", _task_id, required=True
    )
    if any(item not in source_ids for item in affected + dependencies):
        raise ValueError("remediation dependency is unknown")
    if any(item not in dependencies for item in affected):
        raise ValueError("remediation affected task is not a dependency")
    if (
        _git_id(request["base_integration_commit"], "remediation base commit")
        != integration_state["commit"]
    ):
        raise ValueError("remediation base commit is stale")
    if (
        _git_id(request["base_integration_tree"], "remediation base tree")
        != integration_state["tree"]
    ):
        raise ValueError("remediation base tree is stale")
    goal = _text(request["goal"], "remediation goal", maximum=256)
    output_boundary = _text(
        request["output_boundary"], "remediation output boundary", maximum=256
    )
    write_scope = _normalised_scopes(request["write_scope"], "remediation write_scope")
    if (
        len(affected) != 1
        or (
            task_id in source_ids
            and source_units[task_id].get("unit_kind") != "remediation"
        )
        or source_units[affected[0]].get("unit_kind") == "verification"
        or any(
            not any(
                scope == parent or scope.startswith(parent + "/")
                for parent in source_units[affected[0]].get("write_scope", [])
            )
            for scope in write_scope
        )
    ):
        return {"_automation_stopped": True}
    direct_contract_hashes = _fast_lane_hash_list(
        request["direct_contract_hashes"], "remediation direct_contract_hashes"
    )
    required_evidence = _fast_lane_label_list(
        request["required_evidence"], "remediation required_evidence"
    )
    task_node_ids = _fast_lane_hash_list(
        request["task_node_ids"], "remediation task_node_ids"
    )
    if not task_node_ids:
        raise ValueError("remediation task node evidence is required")
    contract_node_ids = _fast_lane_hash_list(
        request["contract_node_ids"], "remediation contract_node_ids"
    )
    acceptance_constraints = _fast_lane_hash_list(
        request["acceptance_constraints"], "remediation acceptance_constraints"
    )
    driver_gate_id = _label(request["driver_gate_id"], "remediation driver_gate_id")
    target_gates = request["target_gates"]
    if (
        not isinstance(target_gates, Sequence)
        or isinstance(target_gates, (str, bytes, bytearray))
        or len(target_gates) != 1
    ):
        raise ValueError("remediation target gates must contain one gate")
    gate = _validated_fast_lane_gate(target_gates[0], "remediation target_gates[0]")
    if gate["gate_id"] != driver_gate_id:
        raise ValueError("remediation driver gate is missing")
    return {
        "schema": request["schema"],
        "round": 1,
        "task_id": task_id,
        "source_plan_hash": source_plan_hash,
        "blocker_review_hash": blocker_hash,
        "finding_hash": finding_hash,
        "severity": severity,
        "affected_task_ids": affected,
        "dependencies": dependencies,
        "base_integration_commit": integration_state["commit"],
        "base_integration_tree": integration_state["tree"],
        "goal": goal,
        "output_boundary": output_boundary,
        "write_scope": write_scope,
        "direct_contract_hashes": direct_contract_hashes,
        "required_evidence": required_evidence,
        "task_node_ids": task_node_ids,
        "contract_node_ids": contract_node_ids,
        "acceptance_constraints": acceptance_constraints,
        "driver_gate_id": driver_gate_id,
        "target_gates": [gate],
    }


def _fast_lane_remediation_unit(remediation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": remediation["task_id"],
        "goal": remediation["goal"],
        "output_boundary": remediation["output_boundary"],
        "write_scope": list(remediation["write_scope"]),
        "depends_on": list(remediation["dependencies"]),
        "required_evidence": list(remediation["required_evidence"]),
        "recommended_route": "Terra Max",
        "execution_contracts": [],
        "direct_contract_hashes": list(remediation["direct_contract_hashes"]),
        "task_node_ids": list(remediation["task_node_ids"]),
        "contract_node_ids": list(remediation["contract_node_ids"]),
        "acceptance_constraints": list(remediation["acceptance_constraints"]),
        "unit_kind": "remediation",
        "operation_count": 0,
    }


def _fast_lane_source_with_remediation(
    source_plan: Mapping[str, Any], remediation: Mapping[str, Any]
) -> dict[str, Any]:
    unit = _fast_lane_remediation_unit(remediation)
    return {
        **source_plan,
        "units": [*source_plan["units"], unit],
        "waves": [*source_plan.get("waves", []), [unit]],
    }


def _fast_lane_embedded_record(
    value: object, hash_value: object, field: str
) -> dict[str, Any]:
    embedded = dict(_mapping(value, field))
    supplied_hash = _hash(hash_value, f"{field}_hash")
    if supplied_hash != _sha256_json(embedded):
        raise ValueError(f"{field} hash is invalid")
    return embedded


def _validated_fast_lane_completed_tasks(value: object) -> list[dict[str, Any]]:
    records = _bounded_records(
        value, "scheduler_state.completed_tasks", maximum=MAX_MANIFEST_UNITS + 1
    )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        field = f"completed_tasks[{index}]"
        record = _mapping(item, field)
        _exact_keys(record, _FAST_LANE_COMPLETED_FIELDS, field)
        completion_kind = _text(
            record["completion_kind"], f"{field}.completion_kind", maximum=32
        )
        if completion_kind not in {"integrated_candidate", "verification_evidence"}:
            raise ValueError("completion kind is invalid")
        terminal_result = _fast_lane_embedded_record(
            record["terminal_result"],
            record["terminal_result_hash"],
            f"{field}.terminal_result",
        )
        completion_receipt = _fast_lane_embedded_record(
            record["completion_receipt"],
            record["completion_receipt_hash"],
            f"{field}.completion_receipt",
        )
        completion_receipt_hash = _sha256_json(completion_receipt)
        result_hash = _hash(record["result_hash"], f"{field}.result_hash")
        if result_hash != completion_receipt_hash:
            raise ValueError("completed result hash must bind the completion receipt")
        normalized.append(
            {
                "task_id": _task_id(record["task_id"], f"{field}.task_id"),
                "completion_kind": completion_kind,
                "integration_commit": _git_id(
                    record["integration_commit"], f"{field}.integration_commit"
                ),
                "integration_tree": _git_id(
                    record["integration_tree"], f"{field}.integration_tree"
                ),
                "result_hash": result_hash,
                "terminal_result_hash": _sha256_json(terminal_result),
                "terminal_result": terminal_result,
                "completion_receipt_hash": completion_receipt_hash,
                "completion_receipt": completion_receipt,
            }
        )
    task_ids = [record["task_id"] for record in normalized]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("completed tasks contain duplicates")
    return sorted(normalized, key=lambda record: record["task_id"])


def _validated_fast_lane_review_candidates(
    value: object, *, reviewed: bool
) -> list[dict[str, Any]]:
    field_name = "reviewed_candidates" if reviewed else "review_ready_candidates"
    expected = (
        _FAST_LANE_REVIEWED_FIELDS if reviewed else _FAST_LANE_REVIEW_READY_FIELDS
    )
    records = _bounded_records(
        value, f"scheduler_state.{field_name}", maximum=MAX_MANIFEST_UNITS
    )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        field = f"{field_name}[{index}]"
        record = _mapping(item, field)
        _exact_keys(record, expected, field)
        terminal_result = _fast_lane_embedded_record(
            record["terminal_result"],
            record["terminal_result_hash"],
            f"{field}.terminal_result",
        )
        candidate = {
            "task_id": _task_id(record["task_id"], f"{field}.task_id"),
            "candidate_commit": _git_id(
                record["candidate_commit"], f"{field}.candidate_commit"
            ),
            "candidate_tree": _git_id(
                record["candidate_tree"], f"{field}.candidate_tree"
            ),
            "red_evidence_hashes": _fast_lane_hash_list(
                record["red_evidence_hashes"], f"{field}.red_evidence_hashes"
            ),
            "green_evidence_hashes": _fast_lane_hash_list(
                record["green_evidence_hashes"], f"{field}.green_evidence_hashes"
            ),
            "terminal_result_hash": _sha256_json(terminal_result),
            "terminal_result": terminal_result,
        }
        if reviewed:
            outcome = _text(record["outcome"], f"{field}.outcome", maximum=16)
            if outcome != "pass":
                raise ValueError("reviewed candidate outcome must be pass")
            review_terminal = _fast_lane_embedded_record(
                record["review_terminal_result"],
                record["review_terminal_result_hash"],
                f"{field}.review_terminal_result",
            )
            candidate.update(
                {
                    "review_hash": _hash(record["review_hash"], f"{field}.review_hash"),
                    "outcome": outcome,
                    "review_terminal_result_hash": _sha256_json(review_terminal),
                    "review_terminal_result": review_terminal,
                }
            )
        normalized.append(candidate)
    task_ids = [record["task_id"] for record in normalized]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError(f"{field_name} contains duplicates")
    return sorted(normalized, key=lambda record: record["task_id"])


def _validated_fast_lane_annotation_evidence(
    value: object, *, prewarm: bool
) -> list[dict[str, Any]]:
    field_name = "prewarmed_evidence" if prewarm else "design_evidence"
    expected = (
        _FAST_LANE_PREWARMED_FIELDS if prewarm else _FAST_LANE_DESIGN_EVIDENCE_FIELDS
    )
    records = _bounded_records(
        value, f"scheduler_state.{field_name}", maximum=MAX_MANIFEST_UNITS
    )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        field = f"{field_name}[{index}]"
        record = _mapping(item, field)
        _exact_keys(record, expected, field)
        terminal_result = _fast_lane_embedded_record(
            record["terminal_result"],
            record["terminal_result_hash"],
            f"{field}.terminal_result",
        )
        evidence = {
            "task_id": _task_id(record["task_id"], f"{field}.task_id"),
            "observation_basis_hash": _hash(
                record["observation_basis_hash"], f"{field}.observation_basis_hash"
            ),
            "evidence_hash": _hash(record["evidence_hash"], f"{field}.evidence_hash"),
            "terminal_result_hash": _sha256_json(terminal_result),
            "terminal_result": terminal_result,
        }
        if prewarm:
            optional = [
                _fast_lane_optional_hash(record[name], f"{field}.{name}")
                for name in (
                    "revalidation_basis_hash",
                    "dependency_delta_hash",
                    "revalidation_evidence_hash",
                )
            ]
            if any(value is None for value in optional) and any(
                value is not None for value in optional
            ):
                raise ValueError("prewarm revalidation hashes must be jointly present")
            evidence.update(
                dict(
                    zip(
                        (
                            "revalidation_basis_hash",
                            "dependency_delta_hash",
                            "revalidation_evidence_hash",
                        ),
                        optional,
                        strict=True,
                    )
                )
            )
        normalized.append(evidence)
    task_ids = [record["task_id"] for record in normalized]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError(f"{field_name} contains duplicates")
    return sorted(normalized, key=lambda record: record["task_id"])


def _validated_fast_lane_scheduler_state(
    value: object,
    *,
    source_plan: Mapping[str, Any],
    source_plan_hash: str,
    execution_contexts: Sequence[Mapping[str, Any]],
    read_contexts: Sequence[Mapping[str, Any]],
    target_gates: Sequence[Mapping[str, Any]],
    remediation_request_value: object,
    routing_context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = _mapping(value, "scheduler_state")
    _exact_keys(state, _FAST_LANE_SCHEDULER_FIELDS, "scheduler_state")
    source_state_hash = state["source_plan_hash"]
    if (
        source_state_hash is not None
        and _hash(source_state_hash, "scheduler_state.source_plan_hash")
        != source_plan_hash
    ):
        raise ValueError("scheduler source plan is stale")
    phase = _text(state["phase"], "scheduler_state.phase", maximum=32)
    if phase not in _FAST_LANE_PHASES:
        raise ValueError("fast-lane phase is invalid")
    integration = _mapping(
        state["integration_state"], "scheduler_state.integration_state"
    )
    _exact_keys(
        integration, _FAST_LANE_INTEGRATION_FIELDS, "scheduler integration state"
    )
    integration_state = _fast_lane_integration_state(state)
    lane0 = _mapping(state["lane0_state"], "scheduler_state.lane0_state")
    _exact_keys(lane0, _FAST_LANE_LANE0_FIELDS, "scheduler lane0 state")
    active_task_id = lane0["active_task_id"]
    if active_task_id is not None:
        active_task_id = _task_id(active_task_id, "scheduler lane0 active_task_id")
    owned_write_scopes = _normalised_list(
        lane0["owned_write_scopes"],
        "scheduler lane0 owned_write_scopes",
        _relative_scope,
        maximum=MAX_WRITE_SCOPES,
    )

    remediation = _validated_fast_lane_remediation_request(
        remediation_request_value,
        source_plan=source_plan,
        source_plan_hash=source_plan_hash,
        integration_state=integration_state,
    )
    units_by_task_id: dict[str, Mapping[str, Any]] = {}
    for index, source_unit in enumerate(source_plan.get("units", [])):
        unit = _mapping(source_unit, f"source plan.units[{index}]")
        task_id = _task_id(unit["task_id"], f"source plan.units[{index}].task_id")
        if task_id in units_by_task_id:
            raise ValueError("source plan contains duplicate task ids")
        units_by_task_id[task_id] = unit
    task_ids = set(units_by_task_id)
    if remediation is not None and remediation["task_id"] not in task_ids:
        raise ValueError("remediation task is missing from the source plan")
    completed_tasks = _validated_fast_lane_completed_tasks(state["completed_tasks"])
    review_ready_candidates = _validated_fast_lane_review_candidates(
        state["review_ready_candidates"], reviewed=False
    )
    reviewed_candidates = _validated_fast_lane_review_candidates(
        state["reviewed_candidates"], reviewed=True
    )
    prewarmed_evidence = _validated_fast_lane_annotation_evidence(
        state["prewarmed_evidence"], prewarm=True
    )
    design_evidence = _validated_fast_lane_annotation_evidence(
        state["design_evidence"], prewarm=False
    )
    for collection in (
        completed_tasks,
        review_ready_candidates,
        reviewed_candidates,
        prewarmed_evidence,
        design_evidence,
    ):
        if any(record["task_id"] not in task_ids for record in collection):
            raise ValueError("scheduler lifecycle record references an unknown task")
    dispatch_contexts_value = _bounded_records(
        state["dispatch_contexts"],
        "scheduler_state.dispatch_contexts",
        maximum=MAX_MANIFEST_UNITS,
    )
    dispatch_contexts = [
        _validated_fast_lane_dispatch_context(
            item, source_plan_hash=source_plan_hash, task_ids=task_ids
        )
        for item in dispatch_contexts_value
    ]
    context_by_key = {
        (item["task_id"], item["role"]): item for item in dispatch_contexts
    }
    if len(context_by_key) != len(dispatch_contexts):
        raise ValueError("dispatch contexts contain duplicates")
    execution_by_task = {context["task_id"]: context for context in execution_contexts}
    prewarm_by_task = {record["task_id"]: record for record in prewarmed_evidence}
    read_by_key = {
        (context["task_id"], context["role"]): context for context in read_contexts
    }
    gates_by_task = {target["task_id"]: target for target in target_gates}
    for context in dispatch_contexts:
        task_id = context["task_id"]
        role = context["role"]
        if role == "execution":
            execution_context = execution_by_task.get(task_id)
            if execution_context is None:
                raise ValueError("execution dispatch context has no execution context")
            plan = execution_context["bootstrap_plan"]
            target = gates_by_task.get(task_id)
            expected_target_hash = (
                None
                if target is None
                else _sha256_json(
                    {
                        "driver_gate_id": target["driver_gate_id"],
                        "target_gates": target["gates"],
                    }
                )
            )
            if (
                context["execution_context_hash"] != _sha256_json(execution_context)
                or context["bootstrap_plan_hash"] != _sha256_json(plan)
                or context["base_commit"] != plan["base_commit"]
                or context["branch"] != plan["branch"]
                or context["write_scope_hash"] != _sha256_json(plan["write_scope"])
                or context["workspace_input_snapshot_id"]
                != execution_context["workspace_input_snapshot_id"]
                or context["target_gates_hash"] != expected_target_hash
                or context["read_context_hash"] is not None
            ):
                raise ValueError("execution dispatch context is not bound")
            prewarm = prewarm_by_task.get(task_id)
            expected_prewarm_hashes = (None, None)
            if prewarm is not None and all(
                prewarm[field] is not None
                for field in (
                    "revalidation_basis_hash",
                    "dependency_delta_hash",
                    "revalidation_evidence_hash",
                )
            ):
                expected_prewarm_hashes = (
                    prewarm["evidence_hash"],
                    prewarm["revalidation_evidence_hash"],
                )
            if (
                context["prewarm_evidence_hash"],
                context["prewarm_revalidation_evidence_hash"],
            ) != expected_prewarm_hashes:
                raise ValueError("execution dispatch prewarm evidence is not bound")
        else:
            read_context = read_by_key.get((task_id, role))
            if read_context is None:
                raise ValueError("read-only dispatch context has no read context")
            if (
                context["read_context_hash"] != _sha256_json(read_context)
                or context["workspace_input_snapshot_id"]
                != read_context["workspace_input_snapshot_id"]
                or context["execution_context_hash"] is not None
                or context["bootstrap_plan_hash"] is not None
                or context["base_commit"] is not None
                or context["branch"] is not None
            ):
                raise ValueError("read-only dispatch context is not bound")

    slot_epochs_value = _mapping(state["slot_epochs"], "scheduler_state.slot_epochs")
    if set(slot_epochs_value) != set(FAST_LANE_SLOT_IDS):
        raise ValueError("scheduler slot epochs are not exact")
    slot_epochs: dict[str, int] = {}
    for slot_id in FAST_LANE_SLOT_IDS:
        epoch = slot_epochs_value[slot_id]
        if type(epoch) is not int or epoch < 0:
            raise ValueError("scheduler slot epoch is invalid")
        slot_epochs[slot_id] = epoch

    running_value = _bounded_records(
        state["running_assignments"],
        "scheduler_state.running_assignments",
        maximum=len(FAST_LANE_SLOT_IDS),
    )
    running: list[dict[str, Any]] = []
    seen_slots: set[str] = set()
    seen_tasks: set[str] = set()
    seen_tokens: set[str] = set()
    for index, item in enumerate(running_value):
        assignment = _mapping(item, f"running_assignments[{index}]")
        _exact_keys(
            assignment, _FAST_LANE_ASSIGNMENT_FIELDS, f"running_assignments[{index}]"
        )
        slot_id = _label(assignment["slot_id"], f"running_assignments[{index}].slot_id")
        if slot_id not in FAST_LANE_SLOT_IDS or slot_id in seen_slots:
            raise ValueError("running assignment slot is invalid or duplicated")
        task_id = _task_id(
            assignment["task_id"], f"running_assignments[{index}].task_id"
        )
        role = _text(
            assignment["role"], f"running_assignments[{index}].role", maximum=32
        )
        if task_id not in task_ids or task_id in seen_tasks:
            raise ValueError("running assignment task is invalid or duplicated")
        epoch = assignment["assignment_epoch"]
        if type(epoch) is not int or epoch <= 0 or epoch != slot_epochs[slot_id]:
            raise ValueError("running assignment epoch is invalid")
        receipt = _validated_fast_lane_dispatch_receipt(assignment["dispatch_receipt"])
        if (
            receipt["task_id"] != task_id
            or receipt["role"] != role
            or receipt["slot_id"] != slot_id
            or receipt["assignment_epoch"] != epoch
        ):
            raise ValueError("running assignment receipt does not bind assignment")
        if role not in _FAST_LANE_ROLES:
            raise ValueError("running assignment role is invalid")
        if task_id not in units_by_task_id:
            raise ValueError("running assignment source unit is unknown")
        if (
            (phase == "integration_regression" and role != "verification")
            or (phase in {"blocker_review", "acceptance", "stopped"})
            or (phase == "remediation" and role != "execution")
            or (phase == "execution" and role == "verification")
        ):
            raise ValueError("running assignment role is invalid for the phase")
        token = _hash(
            assignment["assignment_token"],
            f"running_assignments[{index}].assignment_token",
        )
        if token != _fast_lane_assignment_token(receipt) or token in seen_tokens:
            raise ValueError("running assignment token is invalid or duplicated")
        recovery_of = receipt["recovery_of_assignment_token"]
        if recovery_of is not None:
            if role not in {"execution", "verification"} or epoch <= 1:
                raise ValueError("running assignment recovery is invalid")
            predecessor = dict(receipt)
            predecessor["assignment_epoch"] = epoch - 1
            predecessor["recovery_of_assignment_token"] = None
            if recovery_of != _fast_lane_assignment_token(predecessor):
                raise ValueError("running assignment recovery predecessor is invalid")
        context = context_by_key.get((task_id, role))
        if context is None or context["context_hash"] != _hash(
            assignment["context_hash"], "running assignment.context_hash"
        ):
            raise ValueError("running assignment context is not ledgered")
        if receipt["dispatch_context_hash"] != context["context_hash"]:
            raise ValueError("running assignment receipt context is stale")
        model = _text(
            assignment["model"], f"running_assignments[{index}].model", maximum=64
        )
        effort = _text(
            assignment["reasoning_effort"],
            f"running_assignments[{index}].reasoning_effort",
            maximum=16,
        )
        routing_context_hash = _hash(
            assignment["routing_context_hash"],
            f"running_assignments[{index}].routing_context_hash",
        )
        routing_result_hash = _hash(
            assignment["routing_result_hash"],
            f"running_assignments[{index}].routing_result_hash",
        )
        task_fingerprint = _hash(
            assignment["task_fingerprint"],
            f"running_assignments[{index}].task_fingerprint",
        )
        routing_reason_codes = _fast_lane_reason_codes(
            assignment["routing_reason_codes"]
        )
        routing_safety_floor_rank = assignment["routing_safety_floor_rank"]
        if (
            routing_reason_codes is None
            or type(routing_safety_floor_rank) is not int
            or not 10 <= routing_safety_floor_rank <= 110
        ):
            raise ValueError("running assignment routing fields are invalid")
        if (
            receipt["source_plan_hash"] != source_plan_hash
            or receipt["model"] != model
            or receipt["reasoning_effort"] != effort
            or receipt["routing_context_hash"] != routing_context_hash
            or receipt["routing_result_hash"] != routing_result_hash
            or receipt["task_fingerprint"] != task_fingerprint
            or receipt["routing_reason_codes"] != routing_reason_codes
            or receipt["routing_safety_floor_rank"] != routing_safety_floor_rank
            or receipt["target_gates_hash"] != context["target_gates_hash"]
            or receipt["execution_context_hash"] != context["execution_context_hash"]
            or receipt["read_context_hash"] != context["read_context_hash"]
        ):
            raise ValueError("running assignment receipt fields disagree")
        _fast_lane_historical_receipt_route(receipt, source_plan_hash=source_plan_hash)
        normalized_assignment = dict(assignment)
        normalized_assignment.update(
            {
                "slot_id": slot_id,
                "task_id": task_id,
                "role": role,
                "assignment_epoch": epoch,
                "assignment_token": token,
                "context_hash": context["context_hash"],
                "model": model,
                "reasoning_effort": effort,
                "routing_context_hash": receipt["routing_context_hash"],
                "routing_result_hash": receipt["routing_result_hash"],
                "task_fingerprint": receipt["task_fingerprint"],
                "routing_reason_codes": list(receipt["routing_reason_codes"]),
                "routing_safety_floor_rank": receipt["routing_safety_floor_rank"],
                "dispatch_receipt": receipt,
            }
        )
        if normalized_assignment["reasoning_effort"] not in FAST_LANE_REASONING_EFFORTS:
            raise ValueError("running assignment reasoning effort is invalid")
        running.append(normalized_assignment)
        seen_slots.add(slot_id)
        seen_tasks.add(task_id)
        seen_tokens.add(token)

    (
        completed_tasks,
        review_ready_candidates,
        reviewed_candidates,
        prewarmed_evidence,
        design_evidence,
    ) = _validated_fast_lane_lifecycle_records(
        completed_tasks=completed_tasks,
        review_ready_candidates=review_ready_candidates,
        reviewed_candidates=reviewed_candidates,
        prewarmed_evidence=prewarmed_evidence,
        design_evidence=design_evidence,
        source_plan_hash=source_plan_hash,
        task_ids=task_ids,
        units_by_task_id=units_by_task_id,
        context_by_key=context_by_key,
        read_context_by_key=read_by_key,
        slot_epochs=slot_epochs,
        routing_context=routing_context,
    )

    blocked_task_ids = _normalised_list(
        state["blocked_task_ids"],
        "scheduler_state.blocked_task_ids",
        _task_id,
        maximum=MAX_MANIFEST_UNITS,
    )
    if any(task_id not in task_ids for task_id in blocked_task_ids):
        raise ValueError("blocked task is unknown")
    if seen_tasks.intersection(blocked_task_ids):
        raise ValueError("running and blocked task sets overlap")
    pending_design_probe_task_ids = _normalised_list(
        state["pending_design_probe_task_ids"],
        "scheduler_state.pending_design_probe_task_ids",
        _task_id,
        maximum=1,
    )
    if any(task_id not in task_ids for task_id in pending_design_probe_task_ids):
        raise ValueError("pending design probe task is unknown")
    completed_ids = {record["task_id"] for record in completed_tasks}
    review_ready_ids = {record["task_id"] for record in review_ready_candidates}
    reviewed_ids = {record["task_id"] for record in reviewed_candidates}
    if active_task_id is not None and active_task_id in (
        completed_ids
        | review_ready_ids
        | reviewed_ids
        | seen_tasks
        | set(blocked_task_ids)
    ):
        raise ValueError("lane 0 and a subagent cannot own the same task")
    exclusive_sets = [
        completed_ids,
        review_ready_ids,
        reviewed_ids,
        seen_tasks,
        set(blocked_task_ids),
    ]
    for index, left in enumerate(exclusive_sets):
        for right in exclusive_sets[index + 1 :]:
            if left.intersection(right):
                raise ValueError("scheduler lifecycle task sets overlap")
    prewarmed_ids = {record["task_id"] for record in prewarmed_evidence}
    design_ids = {record["task_id"] for record in design_evidence}
    if prewarmed_ids.intersection(design_ids):
        raise ValueError("prewarm and design evidence overlap")

    global_state = _mapping(
        state["global_remediation"], "scheduler_state.global_remediation"
    )
    _exact_keys(
        global_state, _FAST_LANE_GLOBAL_REMEDIATION_FIELDS, "global remediation"
    )
    round_value = global_state["round"]
    if type(round_value) is not int or round_value not in {0, 1}:
        raise ValueError("global remediation round is invalid")
    global_state_normalized = dict(global_state)
    remediation_state = _text(
        global_state["state"], "global remediation.state", maximum=32
    )
    if remediation_state not in {
        "not_requested",
        "approved",
        "running",
        "completed",
        "stopped",
    }:
        raise ValueError("global remediation state is invalid")
    global_state_normalized["state"] = remediation_state
    if round_value == 0:
        if (
            any(
                global_state[key] is not None
                for key in (
                    "task_id",
                    "blocker_review_hash",
                    "finding_hash",
                    "dispatch_receipt",
                    "completion_receipt_hash",
                )
            )
            or global_state["affected_task_ids"]
        ):
            raise ValueError("round-zero remediation state must be empty")
    else:
        if remediation_state == "not_requested":
            raise ValueError("round-one remediation state is not requested")
        task_id = _text(
            global_state["task_id"], "global remediation.task_id", maximum=32
        )
        if remediation is None or task_id != remediation["task_id"]:
            raise ValueError("global remediation request is missing")
        global_state_normalized["task_id"] = task_id
        global_state_normalized["affected_task_ids"] = _normalised_list(
            global_state["affected_task_ids"],
            "global remediation.affected_task_ids",
            _task_id,
            required=True,
        )
        global_state_normalized["blocker_review_hash"] = _hash(
            global_state["blocker_review_hash"],
            "global remediation.blocker_review_hash",
        )
        global_state_normalized["finding_hash"] = _hash(
            global_state["finding_hash"], "global remediation.finding_hash"
        )
        if (
            global_state_normalized["affected_task_ids"]
            != remediation["affected_task_ids"]
            or global_state_normalized["blocker_review_hash"]
            != remediation["blocker_review_hash"]
            or global_state_normalized["finding_hash"] != remediation["finding_hash"]
            or not set(remediation["affected_task_ids"])
            <= {record["task_id"] for record in completed_tasks}
        ):
            raise ValueError("global remediation is not bound to completed work")

        running_remediation = next(
            (assignment for assignment in running if assignment["task_id"] == task_id),
            None,
        )
        if remediation_state == "approved":
            if (
                global_state["dispatch_receipt"] is not None
                or global_state["completion_receipt_hash"] is not None
                or running_remediation is not None
            ):
                raise ValueError(
                    "approved remediation must not carry execution evidence"
                )
        elif remediation_state == "running":
            if global_state["completion_receipt_hash"] is not None:
                raise ValueError("running remediation cannot carry completion evidence")
            if global_state["dispatch_receipt"] is None or running_remediation is None:
                raise ValueError("running remediation dispatch evidence is missing")
            dispatch_receipt = _validated_fast_lane_dispatch_receipt(
                global_state["dispatch_receipt"]
            )
            if dispatch_receipt != running_remediation["dispatch_receipt"]:
                raise ValueError("running remediation dispatch receipt is not bound")
            global_state_normalized["dispatch_receipt"] = dispatch_receipt
        elif remediation_state == "completed":
            if global_state["dispatch_receipt"] is None:
                raise ValueError("completed remediation dispatch evidence is missing")
            completion_record = next(
                (record for record in completed_tasks if record["task_id"] == task_id),
                None,
            )
            if completion_record is None:
                raise ValueError("completed remediation result is missing")
            dispatch_receipt = _validated_fast_lane_dispatch_receipt(
                global_state["dispatch_receipt"]
            )
            terminal_receipt = completion_record["terminal_result"]["dispatch_receipt"]
            if dispatch_receipt != terminal_receipt:
                raise ValueError("completed remediation dispatch receipt is not bound")
            completion_receipt_hash = _hash(
                global_state["completion_receipt_hash"],
                "global remediation.completion_receipt_hash",
            )
            if completion_receipt_hash != completion_record["completion_receipt_hash"]:
                raise ValueError("completed remediation receipt hash is not bound")
            global_state_normalized["dispatch_receipt"] = dispatch_receipt
            global_state_normalized["completion_receipt_hash"] = completion_receipt_hash
        else:
            if (
                global_state["dispatch_receipt"] is not None
                or global_state["completion_receipt_hash"] is not None
                or running_remediation is not None
            ):
                raise ValueError(
                    "stopped remediation must not carry execution evidence"
                )

    has_persisted_state = bool(
        completed_tasks
        or review_ready_candidates
        or reviewed_candidates
        or prewarmed_evidence
        or design_evidence
        or running
        or dispatch_contexts
        or blocked_task_ids
        or pending_design_probe_task_ids
        or active_task_id
        or any(slot_epochs.values())
        or round_value
    )
    if has_persisted_state and source_state_hash is None:
        raise ValueError("persisted scheduler state requires a source plan hash")

    normalized = dict(state)
    normalized.update(
        {
            "source_plan_hash": None if source_state_hash is None else source_plan_hash,
            "phase": phase,
            "integration_state": integration_state,
            "lane0_state": {
                "active_task_id": active_task_id,
                "owned_write_scopes": owned_write_scopes,
            },
            "completed_tasks": completed_tasks,
            "review_ready_candidates": review_ready_candidates,
            "reviewed_candidates": reviewed_candidates,
            "prewarmed_evidence": prewarmed_evidence,
            "design_evidence": design_evidence,
            "dispatch_contexts": dispatch_contexts,
            "running_assignments": running,
            "blocked_task_ids": blocked_task_ids,
            "pending_design_probe_task_ids": pending_design_probe_task_ids,
            "slot_epochs": slot_epochs,
            "global_remediation": global_state_normalized,
        }
    )
    return normalized, remediation


def _validated_fast_lane_request(
    request: Mapping[str, Any], *, host_routing_context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    candidate = _mapping(request, "fast-lane request")
    _exact_keys(candidate, _FAST_LANE_REQUEST_FIELDS, "fast-lane request")
    if candidate["schema"] != "team-efficiency/fast-lane-request-v1":
        raise ValueError("fast-lane request schema is invalid")
    if len(_json_bytes(candidate)) > MAX_MANIFEST_INPUT_BYTES:
        raise ValueError("fast-lane request exceeds its byte budget")
    source_plan = decompose(candidate["work_package"])
    source_plan_hash = _sha256_json(source_plan)
    target_gates = _validated_fast_lane_target_gates(
        candidate["target_gates"], source_plan
    )
    integration_state = _fast_lane_integration_state(candidate["scheduler_state"])
    remediation = _validated_fast_lane_remediation_request(
        candidate["remediation_request"],
        source_plan=source_plan,
        source_plan_hash=source_plan_hash,
        integration_state=integration_state,
    )
    if remediation is not None and remediation.get("_automation_stopped"):
        routing_context = _fast_lane_routing_context(
            host_routing_context,
            source_plan=source_plan,
            source_plan_hash=source_plan_hash,
        )
        raw_state = _mapping(candidate["scheduler_state"], "scheduler_state")
        stopped_state = {
            "source_plan_hash": source_plan_hash,
            "phase": "stopped",
            "integration_state": raw_state["integration_state"],
            "lane0_state": {"active_task_id": None, "owned_write_scopes": []},
            "completed_tasks": [],
            "review_ready_candidates": [],
            "reviewed_candidates": [],
            "prewarmed_evidence": [],
            "design_evidence": [],
            "running_assignments": [],
            "dispatch_contexts": [],
            "blocked_task_ids": [],
            "pending_design_probe_task_ids": [],
            "slot_epochs": {slot_id: 0 for slot_id in FAST_LANE_SLOT_IDS},
            "global_remediation": {
                "round": 0,
                "state": "not_requested",
                "task_id": None,
                "affected_task_ids": [],
                "blocker_review_hash": None,
                "finding_hash": None,
                "dispatch_receipt": None,
                "completion_receipt_hash": None,
            },
        }
        scheduler_state, _ = _validated_fast_lane_scheduler_state(
            stopped_state,
            source_plan=source_plan,
            source_plan_hash=source_plan_hash,
            execution_contexts=[],
            read_contexts=[],
            target_gates=target_gates,
            remediation_request_value=None,
            routing_context=routing_context,
        )
        return {
            "source_plan": source_plan,
            "source_plan_hash": source_plan_hash,
            "target_gates": target_gates,
            "execution_contexts": [],
            "read_contexts": [],
            "remediation_request": None,
            "scheduler_state": scheduler_state,
            "routing_context": routing_context,
            "automation_stopped": True,
        }

    effective_source_plan = source_plan
    if remediation is not None:
        effective_source_plan = _fast_lane_source_with_remediation(
            source_plan, remediation
        )
        target_gates = [
            *target_gates,
            {
                "task_id": remediation["task_id"],
                "driver_gate_id": remediation["driver_gate_id"],
                "gates": remediation["target_gates"],
            },
        ]
    routing_context = _fast_lane_routing_context(
        host_routing_context,
        source_plan=effective_source_plan,
        source_plan_hash=source_plan_hash,
    )
    execution_contexts, read_contexts = _validated_fast_lane_contexts(
        candidate["execution_contexts"],
        candidate["read_contexts"],
        effective_source_plan,
        candidate["scheduler_state"],
    )
    scheduler_state, remediation_request = _validated_fast_lane_scheduler_state(
        candidate["scheduler_state"],
        source_plan=effective_source_plan,
        source_plan_hash=source_plan_hash,
        execution_contexts=execution_contexts,
        read_contexts=read_contexts,
        target_gates=target_gates,
        remediation_request_value=candidate["remediation_request"],
        routing_context=routing_context,
    )
    return {
        "source_plan": effective_source_plan,
        "source_plan_hash": source_plan_hash,
        "target_gates": target_gates,
        "execution_contexts": execution_contexts,
        "read_contexts": read_contexts,
        "remediation_request": remediation_request,
        "scheduler_state": scheduler_state,
        "routing_context": routing_context,
        "automation_stopped": False,
    }


def _fast_lane_phase(value: object) -> str:
    scheduler_state = _mapping(value, "scheduler_state")
    phase = _text(scheduler_state.get("phase"), "scheduler_state.phase", maximum=32)
    if phase not in _FAST_LANE_PHASES:
        raise ValueError("fast-lane phase is invalid")
    return phase


def _fast_lane_idle_slots(
    reason_code: str, assigned_slots: Sequence[str] = ()
) -> list[dict[str, str]]:
    assigned = set(assigned_slots)
    return [
        {"slot_id": slot_id, "reason_code": reason_code}
        for slot_id in FAST_LANE_SLOT_IDS
        if slot_id not in assigned
    ]


def _fast_lane_main_lane(
    activation: Mapping[str, Any],
    *,
    next_action: str | None,
    owned_write_scopes: Sequence[str] = (),
    excluded_write_scopes: Sequence[str] = (),
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
        "owned_write_scopes": list(owned_write_scopes),
        "excluded_write_scopes": list(excluded_write_scopes),
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


def _fast_lane_host_slot_occupancy_audit(
    *,
    workflow_id: str,
    source_plan_hash: str,
    phase: str,
    running_assignments: Sequence[Mapping[str, Any]],
    host_bindings: Sequence[Mapping[str, Any]],
    current_leases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Count only host slots backed by one live, mutually bound lease.

    The compiler cannot observe a worker process directly. A slot is therefore
    occupied only when the scheduler assignment, host binding, and current
    workflow lease agree on every identity field. Any disagreement is treated
    as vacant and produces a deterministic next-boundary refill trigger.
    """

    workflow = _label(workflow_id, "workflow_id")
    source_hash = _hash(source_plan_hash, "source_plan_hash")
    phase_text = _text(phase, "phase", maximum=32)
    if phase_text not in _FAST_LANE_PHASES:
        raise ValueError("fast-lane phase is invalid")

    def normalise_lease(value: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            _exact_keys(value, _FAST_LANE_HOST_LEASE_FIELDS, "host lease")
            state = _text(value["state"], "host lease.state", maximum=32)
            if state not in _FAST_LANE_HOST_STATES:
                return None
            epoch = value["lease_epoch"]
            if type(epoch) is not int or epoch < 1:
                return None
            return {
                "task_id": _task_id(value["task_id"], "host lease.task_id"),
                "lease_epoch": epoch,
                "endpoint": _endpoint(value["endpoint"]),
                "state": state,
            }
        except (KeyError, TypeError, ValueError):
            return None

    lease_by_task: dict[str, list[dict[str, Any]]] = {}
    for raw_lease in current_leases:
        if not isinstance(raw_lease, Mapping):
            continue
        lease = normalise_lease(raw_lease)
        if lease is not None:
            lease_by_task.setdefault(lease["task_id"], []).append(lease)

    assignment_by_slot: dict[str, list[Mapping[str, Any]]] = {}
    for assignment in running_assignments:
        if not isinstance(assignment, Mapping):
            continue
        try:
            slot_id = _label(assignment["slot_id"], "assignment.slot_id")
        except (KeyError, TypeError, ValueError):
            continue
        if slot_id in FAST_LANE_SLOT_IDS:
            assignment_by_slot.setdefault(slot_id, []).append(assignment)

    binding_by_slot: dict[str, list[dict[str, Any]]] = {}
    for raw_binding in host_bindings:
        if not isinstance(raw_binding, Mapping):
            continue
        try:
            _exact_keys(raw_binding, _FAST_LANE_HOST_BINDING_FIELDS, "host binding")
            state = _text(raw_binding["state"], "host binding.state", maximum=32)
            if state not in _FAST_LANE_HOST_STATES:
                continue
            epoch = raw_binding["assignment_epoch"]
            lease_epoch = raw_binding["lease_epoch"]
            if (
                type(epoch) is not int
                or epoch < 1
                or type(lease_epoch) is not int
                or lease_epoch < 1
            ):
                continue
            binding = {
                "workflow_id": _label(
                    raw_binding["workflow_id"], "host binding.workflow_id"
                ),
                "task_id": _task_id(raw_binding["task_id"], "host binding.task_id"),
                "slot_id": _label(raw_binding["slot_id"], "host binding.slot_id"),
                "assignment_epoch": epoch,
                "assignment_token": _hash(
                    raw_binding["assignment_token"], "host binding.assignment_token"
                ),
                "context_hash": _hash(
                    raw_binding["context_hash"], "host binding.context_hash"
                ),
                "lease_epoch": lease_epoch,
                "endpoint": _endpoint(raw_binding["endpoint"]),
                "state": state,
            }
        except (KeyError, TypeError, ValueError):
            continue
        if binding["slot_id"] in FAST_LANE_SLOT_IDS:
            binding_by_slot.setdefault(binding["slot_id"], []).append(binding)

    active_slots: list[str] = []
    for slot_id in FAST_LANE_SLOT_IDS:
        bindings = binding_by_slot.get(slot_id, [])
        assignments = assignment_by_slot.get(slot_id, [])
        if len(bindings) != 1 or len(assignments) != 1:
            continue
        binding = bindings[0]
        assignment = assignments[0]
        if binding["workflow_id"] != workflow or binding["state"] != "running":
            continue
        try:
            assignment_task_id = _task_id(assignment["task_id"], "assignment.task_id")
            assignment_epoch = assignment["assignment_epoch"]
            assignment_token = _hash(
                assignment["assignment_token"], "assignment.assignment_token"
            )
            assignment_context_hash = _hash(
                assignment["context_hash"], "assignment.context_hash"
            )
            receipt = _mapping(assignment["dispatch_receipt"], "dispatch receipt")
            receipt_token = _fast_lane_assignment_token(receipt)
            receipt_context_hash = _hash(
                receipt["dispatch_context_hash"],
                "dispatch receipt.dispatch_context_hash",
            )
        except (KeyError, TypeError, ValueError):
            continue
        if (
            binding["task_id"] != assignment_task_id
            or binding["assignment_epoch"] != assignment_epoch
            or binding["assignment_token"] != assignment_token
            or binding["context_hash"] != assignment_context_hash
            or assignment_token != receipt_token
            or assignment_context_hash != receipt_context_hash
        ):
            continue
        leases = lease_by_task.get(binding["task_id"], [])
        if len(leases) != 1:
            continue
        lease = leases[0]
        if (
            lease["state"] != "running"
            or lease["lease_epoch"] != binding["lease_epoch"]
            or lease["endpoint"] != binding["endpoint"]
        ):
            continue
        active_slots.append(slot_id)

    active_slot_ids = [slot for slot in FAST_LANE_SLOT_IDS if slot in active_slots]
    vacant_slot_ids = [slot for slot in FAST_LANE_SLOT_IDS if slot not in active_slots]
    trigger: dict[str, Any] | None = None
    if vacant_slot_ids:
        trigger = {
            "schema": "team-efficiency/fast-lane-refill-trigger-v1",
            "source_plan_hash": source_hash,
            "phase": phase_text,
            "active_slot_ids": active_slot_ids,
            "vacant_slot_ids": vacant_slot_ids,
            "reason": "under_capacity_true_running_slots",
            "dispatch_at": "next_host_dispatch_boundary",
        }
    return {
        "active_slot_ids": active_slot_ids,
        "vacant_slot_ids": vacant_slot_ids,
        "refill_trigger": trigger,
        "refill_trigger_hash": None if trigger is None else _sha256_json(trigger),
    }


def _validated_fast_lane_routing_entry(value: object, field: str) -> dict[str, Any]:
    """Normalize one bounded complete core request for a scheduler key."""

    entry = _mapping(value, field)
    _exact_keys(entry, _FAST_LANE_ROUTING_ENTRY_FIELDS, field)
    task_id = _task_id(entry["task_id"], f"{field}.task_id")
    scheduler_role = _text(
        entry["scheduler_role"], f"{field}.scheduler_role", maximum=32
    )
    if scheduler_role not in _FAST_LANE_ROLES:
        raise ValueError(f"{field}.scheduler_role is invalid")
    request = _mapping(entry["request"], f"{field}.request")
    if len(_json_bytes(request)) > MAX_FAST_LANE_ROUTE_REQUEST_BYTES:
        raise ValueError(f"{field}.request exceeds its byte budget")
    request_text = _canonical_json(request)
    _reject_sensitive_or_absolute_text(request_text, f"{field}.request")
    compatibility_floor = entry["compatibility_floor"]
    if compatibility_floor is not None and (
        type(compatibility_floor) is not int or not 10 <= compatibility_floor <= 110
    ):
        raise ValueError(f"{field}.compatibility_floor is invalid")
    return {
        "task_id": task_id,
        "scheduler_role": scheduler_role,
        "request": json.loads(request_text),
        "trusted_authorization_evidence_hashes": _fast_lane_hash_list(
            entry["trusted_authorization_evidence_hashes"],
            f"{field}.trusted_authorization_evidence_hashes",
        ),
        "trusted_override_receipt_hashes": _fast_lane_hash_list(
            entry["trusted_override_receipt_hashes"],
            f"{field}.trusted_override_receipt_hashes",
        ),
        "trusted_evidence_hashes": _fast_lane_hash_list(
            entry["trusted_evidence_hashes"],
            f"{field}.trusted_evidence_hashes",
        ),
        "coordinator_endpoint_hash": _fast_lane_optional_hash(
            entry["coordinator_endpoint_hash"],
            f"{field}.coordinator_endpoint_hash",
        ),
        "compatibility_floor": compatibility_floor,
    }


def _validated_fast_lane_routing_context(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    context = _mapping(value, "host routing context")
    _exact_keys(context, _FAST_LANE_ROUTING_CONTEXT_FIELDS, "host routing context")
    if context["schema"] != "team-efficiency/fast-lane-routing-context-v1":
        raise ValueError("host routing context schema is invalid")
    routes_value = context["routes"]
    if not isinstance(routes_value, Sequence) or isinstance(
        routes_value, (str, bytes, bytearray)
    ):
        raise TypeError("host routing context.routes must be a list")
    if len(routes_value) > MAX_FAST_LANE_ROUTING_ENTRIES:
        raise ValueError("host routing context.routes exceeds its bound")
    routes: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(routes_value):
        routes.append(
            _validated_fast_lane_routing_entry(
                raw_entry, f"host routing context.routes[{index}]"
            )
        )
    return {"routes": routes}


def _validated_fast_lane_host_status(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "host status")
    _exact_keys(
        source,
        _FAST_LANE_HOST_STATUS_FIELDS,
        "host status",
    )
    workflow_id = _label(source["workflow_id"], "host status.workflow_id")
    current_leases = source["current_leases"]
    host_bindings = source["host_bindings"]
    for field, entries in (
        ("current_leases", current_leases),
        ("host_bindings", host_bindings),
    ):
        if not isinstance(entries, Sequence) or isinstance(
            entries, (str, bytes, bytearray)
        ):
            raise TypeError(f"host status.{field} must be a list")
        if len(entries) > len(FAST_LANE_SLOT_IDS):
            raise ValueError(f"host status.{field} exceeds slot capacity")
    return {
        "workflow_id": workflow_id,
        "current_leases": list(current_leases),
        "host_bindings": list(host_bindings),
        "routing_context": _validated_fast_lane_routing_context(
            source["routing_context"]
        ),
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
        "boundary_operations": [
            {
                "boundary": "strict_writer_start",
                "roles": ["execution"],
                "operations": [
                    "project_index_sync_input_worker_worktree",
                    "workflow_create_if_absent",
                    "workflow_register_task_strict_index",
                    "workflow_ready",
                    "host_spawn_exact_route",
                    "workflow_claim_with_host_target",
                ],
            },
            {
                "boundary": "strict_writer_execution_and_completion_preparation",
                "roles": ["execution"],
                "operations": [
                    "project_index_query_input",
                    "worktree_checkpoint_create_before_first_write",
                    "native_scoped_write_and_target_gates",
                    "project_index_sync_output_worker_worktree",
                    "project_index_query_output",
                ],
            },
            {
                "boundary": "strict_writer_completion",
                "roles": ["execution"],
                "operations": [
                    "host_attest_and_lane0_integrate",
                    "workflow_artifact_register_completion_receipt_at_output_snapshot",
                    "workflow_complete_with_completion_receipt_hash",
                ],
            },
            {
                "boundary": "read_only_verification_lifecycle",
                "roles": ["verification"],
                "operations": [
                    "project_index_sync_input_read_worktree",
                    "workflow_create_if_absent",
                    "workflow_register_task_strict_index",
                    "workflow_ready",
                    "host_spawn_exact_route",
                    "workflow_claim_with_host_target",
                    "project_index_query_input",
                    "run_all_green_target_gates",
                    "workflow_artifact_register_completion_receipt_at_input_snapshot",
                    "workflow_complete_with_completion_receipt_hash",
                ],
            },
            {
                "boundary": "lease_recovery_without_bound_output",
                "roles": ["execution", "verification"],
                "operations": [
                    "workflow_status_once_for_recovery",
                    "verify_bound_input_snapshot_current_or_stop",
                    "workflow_claim_new_lease_epoch",
                    "reuse_predecessor_dispatch_context",
                    "issue_new_dispatch_receipt_and_token",
                    "reject_old_epoch_receipt_and_token",
                    "reestablish_current_input_query_and_required_write_evidence",
                ],
            },
            {
                "boundary": "lease_recovery_with_valid_bound_output",
                "roles": ["execution"],
                "operations": [
                    "require_host_persisted_attested_output_snapshot",
                    "workflow_status_once_for_recovery",
                    "workflow_claim_new_lease_epoch",
                    "reuse_predecessor_dispatch_context",
                    "issue_new_dispatch_receipt_and_token",
                    "reject_old_epoch_receipt_and_token",
                    "verify_workspace_matches_bound_output_snapshot",
                    "reregister_new_lease_output_query_and_verification_evidence",
                    "continue_host_attested_completion_without_new_input_checkpoint",
                ],
            },
        ],
        "conditional_operations": [
            {
                "condition": "claim_host_target_unavailable_or_rebind_required",
                "operation": "workflow_endpoint_bind",
            }
        ],
        "operation_set_is_closed_capability_list": False,
        "mid_item_status_polling": False,
        "recovery_status_reads": "start_or_recovery_boundary_only",
        "release_tool_available": False,
    }


def _fast_lane_assignment_output(
    validated: Mapping[str, Any], assignment: Mapping[str, Any]
) -> dict[str, Any]:
    source_plan = _mapping(validated["source_plan"], "source plan")
    task_id = _task_id(assignment["task_id"], "assignment.task_id")
    role = _text(assignment["role"], "assignment.role", maximum=32)
    unit = next(
        (
            unit
            for unit in source_plan.get("units", [])
            if isinstance(unit, Mapping) and unit.get("task_id") == task_id
        ),
        None,
    )
    if unit is None:
        raise ValueError("assignment task is not in the source plan")
    target = next(
        (
            target
            for target in validated["target_gates"]
            if target["task_id"] == task_id
        ),
        None,
    )
    supplied_context = assignment.get("_context")
    context = (
        supplied_context
        if isinstance(supplied_context, Mapping)
        else next(
            (
                context
                for context in validated["scheduler_state"]["dispatch_contexts"]
                if context["task_id"] == task_id and context["role"] == role
            ),
            None,
        )
    )
    if context is None:
        raise ValueError("assignment context is not available")
    receipt = assignment["dispatch_receipt"]
    execution_context_hash = context["execution_context_hash"]
    read_context_hash = context["read_context_hash"]
    write_scope = list(unit.get("write_scope", []))
    completed = _fast_lane_completed_ids(validated["scheduler_state"])
    role_target = target if role in {"execution", "verification"} else None
    output: dict[str, Any] = {
        "slot_id": assignment["slot_id"],
        "action": "retain",
        "assignment_epoch": assignment["assignment_epoch"],
        "assignment_token": assignment["assignment_token"],
        "dispatch_receipt": receipt,
        "task_id": task_id,
        "goal": unit["goal"],
        "output_boundary": unit["output_boundary"],
        "unit_kind": unit.get("unit_kind", "artifact"),
        "operation_count": unit.get("operation_count", 0),
        "recommended_route": unit["recommended_route"],
        "role": role,
        "model": assignment["model"],
        "reasoning_effort": assignment["reasoning_effort"],
        "routing_context_hash": assignment["routing_context_hash"],
        "routing_result_hash": assignment["routing_result_hash"],
        "task_fingerprint": assignment["task_fingerprint"],
        "routing_reason_codes": list(assignment["routing_reason_codes"]),
        "routing_safety_floor_rank": assignment["routing_safety_floor_rank"],
        "access": "exclusive_write" if role == "execution" else "read_only",
        "context_hash": assignment["context_hash"],
        "execution_context_hash": execution_context_hash,
        "read_context_hash": read_context_hash,
        "workspace_input_snapshot_id": context["workspace_input_snapshot_id"],
        "read_base_commit": context["integration_commit"]
        if role != "execution"
        else None,
        "read_tree": context["integration_tree"] if role != "execution" else None,
        "base_commit": context["base_commit"],
        "bootstrap_plan_hash": context["bootstrap_plan_hash"],
        "branch": context["branch"],
        "write_scope_hash": context["write_scope_hash"],
        "write_scope": write_scope,
        "depends_on": list(unit.get("depends_on", [])),
        "unmet_dependencies": sorted(
            dependency
            for dependency in unit.get("depends_on", [])
            if dependency not in completed
        ),
        "required_evidence": list(unit.get("required_evidence", [])),
        "execution_contracts": list(unit.get("execution_contracts", [])),
        "direct_contract_hashes": list(context["direct_contract_hashes"]),
        "task_node_ids": list(context["task_node_ids"]),
        "contract_node_ids": list(context["contract_node_ids"]),
        "acceptance_constraints": list(context["acceptance_constraints"]),
        "driver_gate_id": None
        if role_target is None
        else role_target["driver_gate_id"],
        "target_gates": [] if role_target is None else list(role_target["gates"]),
        "candidate_commit": context["candidate_commit"],
        "basis_hash": context["basis_hash"],
    }
    return output


def _fast_lane_queue_item(
    validated: Mapping[str, Any],
    unit: Mapping[str, Any],
    role: str,
    route: Mapping[str, Any],
) -> dict[str, Any]:
    assignment = _fast_lane_assignment(validated, unit, role, "slot-1", route)
    output = _fast_lane_assignment_output(validated, assignment)
    return {
        key: value
        for key, value in output.items()
        if key
        not in {
            "slot_id",
            "action",
            "assignment_epoch",
            "assignment_token",
            "dispatch_receipt",
        }
    }


def _fast_lane_queues(
    validated: Mapping[str, Any],
    assignments: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    source_plan = validated["source_plan"]
    units = _fast_lane_unit_index(source_plan)
    state = validated["scheduler_state"]
    completed = _fast_lane_completed_ids(state)
    blocked = frozenset(state.get("blocked_task_ids", []))
    running = {
        assignment["task_id"] for assignment in state.get("running_assignments", [])
    }
    candidates = {
        record["task_id"] for record in state.get("review_ready_candidates", [])
    }
    reviewed = {record["task_id"] for record in state.get("reviewed_candidates", [])}
    read_contexts = {
        (context["task_id"], context["role"]) for context in validated["read_contexts"]
    }
    assigned_keys = {
        (assignment["task_id"], assignment["role"]) for assignment in assignments
    }
    assigned_task_ids = {task_id for task_id, _ in assigned_keys}
    phase = state["phase"]
    if phase == "integration_regression":
        ready_queue = [
            _fast_lane_queue_item(validated, unit, "verification", route)
            for task_id, unit in sorted(units.items())
            if unit.get("unit_kind") == "verification"
            and task_id
            not in completed | blocked | frozenset(running) | assigned_task_ids
            and _fast_lane_dependency_ready(unit, completed)
            and (task_id, "verification") in read_contexts
            and (
                route := _fast_lane_route(
                    validated["routing_context"], unit, "verification"
                )
            )
            is not None
        ]
        return ready_queue, [], [], []
    if phase == "remediation":
        ready_queue = [
            _fast_lane_queue_item(validated, unit, "execution", route)
            for task_id, unit in sorted(units.items())
            if unit.get("unit_kind") == "remediation"
            and task_id
            not in completed | blocked | frozenset(running) | assigned_task_ids
            and _fast_lane_dependency_ready(unit, completed)
            and (
                route := _fast_lane_route(
                    validated["routing_context"], unit, "execution"
                )
            )
            is not None
        ]
        return ready_queue, [], [], []
    if phase != "execution":
        return [], [], [], []
    graph = _fast_lane_conflict_graph(
        units,
        lane0_scopes=state["lane0_state"]["owned_write_scopes"],
        running=state["running_assignments"],
    )
    ready_queue = [
        _fast_lane_queue_item(validated, unit, "execution", route)
        for unit in _fast_lane_ready_items(
            units,
            completed,
            blocked,
            frozenset(running),
            frozenset(candidates),
            frozenset(reviewed),
            conflict_graph=graph,
        )
        if (route := _fast_lane_route(validated["routing_context"], unit, "execution"))
        is not None
        and unit["task_id"] not in assigned_task_ids
    ]
    review_queue = [
        _fast_lane_queue_item(
            validated,
            units[record["task_id"]],
            "review",
            route,
        )
        for record in state.get("review_ready_candidates", [])
        if record["task_id"] in units
        and (record["task_id"], "review") in read_contexts
        and (record["task_id"], "review") not in assigned_keys
        and (
            route := _fast_lane_route(
                validated["routing_context"], units[record["task_id"]], "review"
            )
        )
        is not None
    ]
    occupied = frozenset(running | {task_id for task_id, _ in assigned_keys})
    prewarm_queue = [
        _fast_lane_queue_item(
            validated,
            units[task_id],
            "prewarm",
            route,
        )
        for task_id in _fast_lane_preferred_prewarms(
            units=units,
            completed=completed,
            running=occupied,
            candidate=frozenset(candidates),
            reviewed=frozenset(reviewed),
            read_contexts=read_contexts,
            source_plan=source_plan,
            blocked=blocked,
        )
        if (task_id, "prewarm") not in assigned_keys
        and (
            route := _fast_lane_route(
                validated["routing_context"], units[task_id], "prewarm"
            )
        )
        is not None
    ]
    design_queue = [
        _fast_lane_queue_item(
            validated,
            units[task_id],
            "design_probe",
            route,
        )
        for task_id in sorted(state.get("pending_design_probe_task_ids", []))
        if task_id in units
        and (task_id, "design_probe") in read_contexts
        and (task_id, "design_probe") not in assigned_keys
        and (
            route := _fast_lane_route(
                validated["routing_context"], units[task_id], "design_probe"
            )
        )
        is not None
    ]
    return ready_queue, review_queue, prewarm_queue, design_queue


def _fast_lane_invalidated_evidence_task_ids(
    validated: Mapping[str, Any],
) -> list[str]:
    units = _fast_lane_unit_index(validated["source_plan"])
    completed = _fast_lane_completed_ids(validated["scheduler_state"])
    invalidated = {
        record["task_id"]
        for record in validated["scheduler_state"].get("prewarmed_evidence", [])
        if record["task_id"] in units
        and _fast_lane_dependency_ready(units[record["task_id"]], completed)
        and any(
            record[field] is None
            for field in (
                "revalidation_basis_hash",
                "dependency_delta_hash",
                "revalidation_evidence_hash",
            )
        )
    }
    return sorted(invalidated)


def _render_fast_lane_status(
    validated: Mapping[str, Any],
    activation: Mapping[str, Any],
    *,
    status: str,
    decision_code: str,
    idle_reason: str,
    next_action: str | None,
    planned_assignments: Sequence[Mapping[str, Any]] | None = None,
    idle_slots: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    source_plan = _mapping(validated["source_plan"], "source plan")
    running_assignments = list(
        validated["scheduler_state"]["running_assignments"]
        if planned_assignments is None
        else planned_assignments
    )
    assignments = [
        {
            **_fast_lane_assignment_output(validated, assignment),
            "action": assignment.get("action", "retain"),
        }
        for assignment in running_assignments
    ]
    owned_scopes = list(
        validated["scheduler_state"]["lane0_state"].get("owned_write_scopes", [])
    )
    unit_by_task_id = _fast_lane_unit_index(source_plan)
    excluded_scopes = sorted(
        {
            scope
            for assignment in running_assignments
            if assignment.get("role") == "execution"
            for scope in unit_by_task_id.get(str(assignment.get("task_id")), {}).get(
                "write_scope", []
            )
        }
    )
    if status == "active" and decision_code == "FAST_LANE_ACTIVE":
        ready_queue, review_queue, prewarm_queue, design_queue = _fast_lane_queues(
            validated, running_assignments
        )
    else:
        ready_queue = []
        review_queue = []
        prewarm_queue = []
        design_queue = []
    result: dict[str, Any] = {
        "schema": "team-efficiency/fast-lane-plan-v1",
        "status": status,
        "decision_code": decision_code,
        "activation": dict(activation),
        "source_plan_hash": validated["source_plan_hash"],
        "phase": _fast_lane_phase(validated["scheduler_state"]),
        "main_lane": _fast_lane_main_lane(
            activation,
            next_action=next_action,
            owned_write_scopes=owned_scopes,
            excluded_write_scopes=excluded_scopes,
        ),
        "subagent_capacity": len(FAST_LANE_SLOT_IDS),
        "assignments": assignments,
        "ready_queue": ready_queue,
        "review_queue": review_queue,
        "prewarm_queue": prewarm_queue,
        "design_queue": design_queue,
        "invalidated_evidence_task_ids": _fast_lane_invalidated_evidence_task_ids(
            validated
        ),
        "idle_slots": list(idle_slots)
        if idle_slots is not None
        else _fast_lane_idle_slots(
            idle_reason, [assignment["slot_id"] for assignment in running_assignments]
        ),
        "refill_plan": _fast_lane_refill_plan(),
        "terminal_protocol": _fast_lane_terminal_protocol(source_plan),
        "workflow_policy": _fast_lane_workflow_policy(),
        "cross_session_dispatch_projection": _fast_lane_cross_session_projection(
            {"source_plan_hash": validated["source_plan_hash"]},
            reference_result={},
            host_status=None,
            occupancy=None,
            quota_evidence=_fast_lane_main_capacity_evidence_unknown(
                "quota_usage_unknown"
            ),
        ),
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
    routing_failure = validated["routing_context"].get("global_failure_reason")
    if routing_failure is not None:
        idle_reason = _fast_lane_failure_reason(routing_failure)
        return _render_fast_lane_status(
            validated,
            activation,
            status="blocked",
            decision_code="NO_SAFE_WORK",
            idle_reason=idle_reason,
            next_action=None,
            planned_assignments=[],
            idle_slots=_fast_lane_idle_slots(idle_reason),
        )
    if _fast_lane_phase(validated["scheduler_state"]) in {
        "blocker_review",
        "acceptance",
    }:
        return _render_fast_lane_status(
            validated,
            activation,
            status="active",
            decision_code="TERMINAL_PROTOCOL_OWNED_BY_LANE0",
            idle_reason="TERMINAL_PHASE_OWNED_BY_LANE0",
            next_action=None,
            planned_assignments=[],
            idle_slots=_fast_lane_idle_slots("TERMINAL_PHASE_OWNED_BY_LANE0"),
        )
    if (
        validated["scheduler_state"]["running_assignments"]
        or validated["execution_contexts"]
    ):
        actions, idle_slots = _fast_lane_build_schedule(validated, activation)
        if actions:
            return _render_fast_lane_status(
                validated,
                activation,
                status="active",
                decision_code="FAST_LANE_ACTIVE",
                idle_reason="NO_SAFE_INDEPENDENT_WORK",
                next_action="adjudicate_and_integrate",
                planned_assignments=actions,
                idle_slots=idle_slots,
            )
        lane0_scopes = validated["scheduler_state"]["lane0_state"]["owned_write_scopes"]
        source_units = validated["source_plan"].get("units", [])
        if lane0_scopes and any(
            _scope_conflicts(unit.get("write_scope", []), lane0_scopes)
            for unit in source_units
            if unit.get("write_scope")
        ):
            reason = "LANE0_SCOPE_CONFLICT"
        elif any(
            _scope_conflicts(left.get("write_scope", []), right.get("write_scope", []))
            for index, left in enumerate(source_units)
            for right in source_units[index + 1 :]
            if left.get("write_scope") and right.get("write_scope")
        ):
            reason = "WRITE_SCOPE_CONFLICT"
        else:
            unroutable = _fast_lane_unroutable_action(validated)
            if unroutable is not None:
                unit, role = unroutable
                return _render_fast_lane_status(
                    validated,
                    activation,
                    status="blocked",
                    decision_code="NO_SAFE_WORK",
                    idle_reason=_fast_lane_route_reason(
                        validated["routing_context"], unit, role
                    ),
                    next_action=None,
                    planned_assignments=[],
                )
            reason = "NO_SAFE_INDEPENDENT_WORK"
        return _render_fast_lane_status(
            validated,
            activation,
            status="active",
            decision_code="FAST_LANE_ACTIVE",
            idle_reason=reason,
            next_action="adjudicate_and_integrate",
            planned_assignments=[],
            idle_slots=_fast_lane_idle_slots(reason),
        )
    contexts = validated["execution_contexts"]
    has_execution_contexts = (
        isinstance(contexts, Sequence)
        and not isinstance(contexts, (str, bytes, bytearray))
        and bool(contexts)
    )
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


def _fast_lane_quota_module() -> Any:
    script = Path(__file__).with_name("fastlane_quota_balance.py")
    spec = importlib.util.spec_from_file_location("fastlane_quota_balance", script)
    if spec is None or spec.loader is None:
        raise ValueError("quota balance module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fastlane_quota_balance"] = module
    spec.loader.exec_module(module)
    return module


def _codex_account_quota_module() -> Any:
    script = Path(__file__).with_name("codex_account_quota.py")
    spec = importlib.util.spec_from_file_location("codex_account_quota", script)
    if spec is None or spec.loader is None:
        raise ValueError("Codex account quota provider is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["codex_account_quota"] = module
    spec.loader.exec_module(module)
    return module


def _utc_now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unavailable_quota_key(_key_id: str) -> bytes | None:
    return None


def _fast_lane_quota_unknown() -> dict[str, Any]:
    decision = {
        "schema": "2718lab-devkit/fastlane-quota-balance-result-v1",
        "status": "usage_unknown",
        "snapshot_hash": None,
        "ledger_epoch": None,
        "main_pressure": "unknown",
        "global_main_target": 6,
        "main_proposal_ids": [],
        "spark_proposal_ids": [],
        "admitted_candidate_ids": [],
        "held_candidate_ids": [],
        "route_lock_hashes": [],
        "reason_codes": ["quota_usage_unknown"],
        "audit_event_hash": "",
    }
    decision["decision_hash"] = _sha256_json(decision)
    decision["audit_event_hash"] = _sha256_json(
        {key: value for key, value in decision.items() if key != "audit_event_hash"}
    )
    return decision


def _fast_lane_quota_decision(
    quota_request: Mapping[str, Any],
    *,
    trusted_key_resolver: Callable[[str], bytes | None] | None,
    evaluation_time_utc_z: str | None,
    verified_route_result_hashes: Iterable[str],
    verified_lease_scope_bindings: Iterable[str],
) -> dict[str, Any]:
    if trusted_key_resolver is None or not callable(trusted_key_resolver):
        return _fast_lane_quota_unknown()
    if not isinstance(evaluation_time_utc_z, str):
        return _fast_lane_quota_unknown()
    try:
        module = _fast_lane_quota_module()
        value = module.compile_quota_balance(
            quota_request,
            trusted_key_resolver=trusted_key_resolver,
            evaluation_time_utc_z=evaluation_time_utc_z,
            verified_route_result_hashes=verified_route_result_hashes,
            verified_lease_scope_bindings=verified_lease_scope_bindings,
        )
    except (OSError, TypeError, ValueError):
        return _fast_lane_quota_unknown()
    if not isinstance(value, Mapping):
        return _fast_lane_quota_unknown()
    return dict(value)


def _fast_lane_quota_start_allowed(
    assignment: Mapping[str, Any],
    quota_request: Mapping[str, Any],
    quota_decision: Mapping[str, Any],
) -> bool:
    if assignment.get("action") != "start":
        return True
    admitted = quota_decision.get("admitted_candidate_ids")
    candidates = quota_request.get("candidates")
    if not isinstance(admitted, Sequence) or isinstance(
        admitted, (str, bytes, bytearray)
    ):
        return False
    if not isinstance(candidates, Sequence) or isinstance(
        candidates, (str, bytes, bytearray)
    ):
        return False
    admitted_ids = {item for item in admitted if isinstance(item, str)}
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, Mapping):
            continue
        if raw_candidate.get("candidate_id") not in admitted_ids:
            continue
        route = raw_candidate.get("route_lock")
        if not isinstance(route, Mapping):
            continue
        if (
            route.get("result_hash") == assignment.get("routing_result_hash")
            and raw_candidate.get("assignment_epoch")
            == assignment.get("assignment_epoch")
            and raw_candidate.get("assignment_token")
            == assignment.get("assignment_token")
            and raw_candidate.get("local_slot_id") == assignment.get("slot_id")
            and raw_candidate.get("write_scope_hash")
            == assignment.get("write_scope_hash")
            and raw_candidate.get("input_snapshot_id")
            == assignment.get("workspace_input_snapshot_id")
        ):
            return True
    return False


def _apply_fast_lane_quota_balance(
    result: Mapping[str, Any],
    *,
    quota_request: Mapping[str, Any],
    quota_decision: Mapping[str, Any],
) -> dict[str, Any]:
    assignments = result.get("assignments")
    if not isinstance(assignments, Sequence) or isinstance(
        assignments, (str, bytes, bytearray)
    ):
        raise TypeError("fast-lane assignments are invalid")
    filtered_assignments = [
        dict(assignment)
        for assignment in assignments
        if isinstance(assignment, Mapping)
        and _fast_lane_quota_start_allowed(assignment, quota_request, quota_decision)
    ]
    refill_plan = _mapping(result["refill_plan"], "fast-lane refill plan")
    updated = {
        **result,
        "assignments": filtered_assignments,
        "refill_plan": {**refill_plan, "quota_balance": dict(quota_decision)},
    }
    updated["plan_hash"] = _sha256_json(
        {key: value for key, value in updated.items() if key != "plan_hash"}
    )
    _exact_keys(updated, _FAST_LANE_PLAN_FIELDS, "fast-lane plan")
    if len(_json_bytes(updated)) > MAX_MANIFEST_BYTES:
        raise ValueError("fast-lane plan exceeds its byte budget")
    return updated


def _fast_lane_main_capacity_evidence_unknown(reason: str) -> dict[str, Any]:
    evidence = {
        "schema": "2718lab-devkit/fastlane-main-capacity-evidence-v1",
        "status": "blocked",
        "snapshot_hash": None,
        "decision_hash": None,
        "ledger_epoch": None,
        "global_main_target": None,
        "global_main_active": None,
        "global_main_free": None,
        "host_main_active": None,
        "active_lease_set_hash": None,
        "reason_codes": [reason],
    }
    return {**evidence, "evidence_hash": _sha256_json(evidence)}


def _validated_fast_lane_main_capacity_evidence(value: object) -> dict[str, Any]:
    evidence = _mapping(value, "main capacity evidence")
    expected = frozenset(
        {
            "schema",
            "status",
            "snapshot_hash",
            "decision_hash",
            "ledger_epoch",
            "global_main_target",
            "global_main_active",
            "global_main_free",
            "host_main_active",
            "active_lease_set_hash",
            "reason_codes",
            "evidence_hash",
        }
    )
    _exact_keys(evidence, expected, "main capacity evidence")
    if evidence["schema"] != "2718lab-devkit/fastlane-main-capacity-evidence-v1":
        raise ValueError("main capacity evidence schema is invalid")
    status = _text(evidence["status"], "main capacity evidence.status", maximum=32)
    if status not in {"resolved", "blocked"}:
        raise ValueError("main capacity evidence status is invalid")
    reasons = _fast_lane_reason_codes(evidence["reason_codes"])
    if reasons is None:
        raise ValueError("main capacity evidence reasons are invalid")

    def optional_hash(field: str) -> str | None:
        item = evidence[field]
        return None if item is None else _hash(item, f"main capacity evidence.{field}")

    def optional_int(field: str, maximum: int) -> int | None:
        item = evidence[field]
        if item is None:
            return None
        if type(item) is not int or not 0 <= item <= maximum:
            raise ValueError(f"main capacity evidence.{field} is invalid")
        return item

    normalized = {
        "schema": evidence["schema"],
        "status": status,
        "snapshot_hash": optional_hash("snapshot_hash"),
        "decision_hash": optional_hash("decision_hash"),
        "ledger_epoch": optional_int("ledger_epoch", 2**63 - 1),
        "global_main_target": optional_int("global_main_target", 12),
        "global_main_active": optional_int("global_main_active", 12),
        "global_main_free": optional_int("global_main_free", 12),
        "host_main_active": optional_int("host_main_active", 8),
        "active_lease_set_hash": optional_hash("active_lease_set_hash"),
        "reason_codes": reasons,
    }
    if normalized["status"] == "resolved":
        if (
            normalized["global_main_target"] not in {6, 8, 10, 12}
            or normalized["global_main_active"] is None
            or normalized["host_main_active"] is None
            or normalized["ledger_epoch"] is None
            or normalized["snapshot_hash"] is None
            or normalized["decision_hash"] is None
            or normalized["active_lease_set_hash"] is None
        ):
            raise ValueError("resolved main capacity evidence is incomplete")
        expected_free = max(
            0,
            normalized["global_main_target"] - normalized["global_main_active"],
        )
        if normalized["global_main_free"] != expected_free:
            raise ValueError("main capacity evidence free count is invalid")
    supplied_hash = _hash(
        evidence["evidence_hash"], "main capacity evidence.evidence_hash"
    )
    if supplied_hash != _sha256_json(normalized):
        raise ValueError("main capacity evidence hash is invalid")
    return {**normalized, "evidence_hash": supplied_hash}


def _fast_lane_main_capacity_evidence(
    quota_request: Mapping[str, Any] | None,
    *,
    trusted_key_resolver: Callable[[str], bytes | None] | None,
    evaluation_time_utc_z: str | None,
    verified_route_result_hashes: Iterable[str],
    verified_lease_scope_bindings: Iterable[str],
) -> dict[str, Any]:
    if quota_request is None:
        return _fast_lane_main_capacity_evidence_unknown("quota_usage_unknown")
    if trusted_key_resolver is None or not callable(trusted_key_resolver):
        return _fast_lane_main_capacity_evidence_unknown("quota_usage_unknown")
    if not isinstance(evaluation_time_utc_z, str):
        return _fast_lane_main_capacity_evidence_unknown("quota_usage_unknown")
    try:
        module = _fast_lane_quota_module()
        evidence = module.compile_main_capacity_evidence(
            quota_request,
            trusted_key_resolver=trusted_key_resolver,
            evaluation_time_utc_z=evaluation_time_utc_z,
            verified_route_result_hashes=verified_route_result_hashes,
            verified_lease_scope_bindings=verified_lease_scope_bindings,
        )
        return _validated_fast_lane_main_capacity_evidence(evidence)
    except (OSError, TypeError, ValueError):
        return _fast_lane_main_capacity_evidence_unknown("quota_usage_unknown")


def _fast_lane_cross_session_projection(
    result: Mapping[str, Any],
    *,
    reference_result: Mapping[str, Any],
    host_status: Mapping[str, Any] | None,
    occupancy: Mapping[str, Any] | None,
    quota_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile inert external-session requirements from already-bound evidence."""

    source_plan_hash = _hash(result["source_plan_hash"], "source plan hash")
    local_active_count = (
        None if occupancy is None else len(occupancy.get("active_slot_ids", ()))
    )
    local_free = (
        None
        if local_active_count is None
        else len(FAST_LANE_SLOT_IDS) - local_active_count
    )
    workflow_id_hash = (
        None
        if host_status is None
        else _sha256_json({"workflow_id": host_status["workflow_id"]})
    )
    evidence_hash = quota_evidence.get("evidence_hash")
    snapshot_hash = quota_evidence.get("snapshot_hash")
    decision_hash = quota_evidence.get("decision_hash")
    target = quota_evidence.get("global_main_target")
    global_active = quota_evidence.get("global_main_active")
    global_free = quota_evidence.get("global_main_free")

    def seal(
        status: str,
        reason_codes: Sequence[str],
        assignments: Sequence[Mapping[str, Any]] = (),
        *,
        global_free_after_local_starts: int | None = None,
    ) -> dict[str, Any]:
        normalized_assignments = [dict(item) for item in assignments]
        projection = {
            "schema": "team-efficiency/fast-lane-cross-session-dispatch-projection-v1",
            "status": status,
            "source_plan_hash": source_plan_hash,
            "workflow_id_hash": workflow_id_hash,
            "local_capacity": len(FAST_LANE_SLOT_IDS),
            "local_active_count": local_active_count,
            "local_free": local_free,
            "global_main_target": target,
            "global_main_active": global_active,
            "global_main_free": global_free,
            "global_main_free_after_local_starts": global_free_after_local_starts,
            "quota_evidence_hash": evidence_hash,
            "quota_snapshot_hash": snapshot_hash,
            "quota_decision_hash": decision_hash,
            "external_session_count": len(normalized_assignments),
            "external_assignment_ids": [
                item["assignment_id"] for item in normalized_assignments
            ],
            "assignments": normalized_assignments,
            "reason_codes": sorted(set(reason_codes)),
        }
        projection["projection_hash"] = _sha256_json(projection)
        _exact_keys(
            projection,
            _FAST_LANE_CROSS_SESSION_PROJECTION_FIELDS,
            "cross-session dispatch projection",
        )
        return projection

    if host_status is None or occupancy is None or local_active_count is None:
        return seal("blocked", ("host_status_unavailable",))
    if quota_evidence.get("status") != "resolved":
        reasons = quota_evidence.get("reason_codes")
        return seal(
            "blocked",
            reasons if isinstance(reasons, Sequence) else ("quota_usage_unknown",),
        )
    if local_active_count != quota_evidence.get("host_main_active"):
        return seal("blocked", ("quota_host_status_fenced",))
    if not isinstance(global_active, int) or global_active < local_active_count:
        return seal("blocked", ("quota_global_capacity_fenced",))
    if (
        result.get("status") != "active"
        or result.get("decision_code") != "FAST_LANE_ACTIVE"
    ):
        return seal("blocked", ("no_safe_work",))

    def start_keys(plan: Mapping[str, Any]) -> set[tuple[str, str, str]]:
        values = plan.get("assignments")
        if not isinstance(values, Sequence) or isinstance(
            values, (str, bytes, bytearray)
        ):
            raise TypeError("cross-session assignments are invalid")
        return {
            (
                _task_id(item["task_id"], "cross-session assignment.task_id"),
                _text(item["role"], "cross-session assignment.role", maximum=32),
                _hash(
                    item["assignment_token"],
                    "cross-session assignment.assignment_token",
                ),
            )
            for item in values
            if isinstance(item, Mapping) and item.get("action") == "start"
        }

    try:
        reference_starts = start_keys(reference_result)
        admitted_starts = start_keys(result)
    except (KeyError, TypeError, ValueError):
        return seal("blocked", ("local_assignment_invalid",))
    if reference_starts != admitted_starts:
        return seal("blocked", ("quota_local_admission_incomplete",))
    local_start_count = len(admitted_starts)
    if local_active_count + local_start_count > len(FAST_LANE_SLOT_IDS):
        return seal("blocked", ("local_capacity_overcommitted",))
    if local_active_count + local_start_count < len(FAST_LANE_SLOT_IDS):
        return seal("not_required", ("local_capacity_available",))
    if not isinstance(global_free, int):
        return seal("blocked", ("quota_usage_unknown",))
    global_free_after_starts = max(0, global_free - local_start_count)

    queue = reference_result.get("ready_queue")
    if not isinstance(queue, Sequence) or isinstance(queue, (str, bytes, bytearray)):
        return seal(
            "blocked",
            ("cross_session_queue_invalid",),
            global_free_after_local_starts=global_free_after_starts,
        )
    queued = [item for item in queue if isinstance(item, Mapping)]
    if len(queued) != len(queue):
        return seal(
            "blocked",
            ("cross_session_queue_invalid",),
            global_free_after_local_starts=global_free_after_starts,
        )
    if not queued:
        return seal(
            "not_required",
            ("no_external_session_required",),
            global_free_after_local_starts=global_free_after_starts,
        )
    if len(queued) > MAX_FAST_LANE_EXTERNAL_SESSION_ASSIGNMENTS:
        return seal(
            "blocked",
            ("external_assignment_limit_exceeded",),
            global_free_after_local_starts=global_free_after_starts,
        )
    if len(queued) > global_free_after_starts:
        return seal(
            "blocked",
            ("quota_global_capacity_exhausted",),
            global_free_after_local_starts=global_free_after_starts,
        )

    try:
        external: list[dict[str, Any]] = []
        for item in sorted(queued, key=lambda value: (value["task_id"], value["role"])):
            task_id = _task_id(item["task_id"], "external assignment.task_id")
            role = _text(item["role"], "external assignment.role", maximum=32)
            model = _text(item["model"], "external assignment.model", maximum=64)
            effort = _fast_lane_effort(item["reasoning_effort"])
            if effort == "ultra" or model == "gpt-5.3-codex-spark":
                raise ValueError("external assignment route is not main-pool safe")
            context_hash = _hash(
                item["context_hash"], "external assignment.context_hash"
            )
            route = {
                "model": model,
                "reasoning_effort": effort,
                "routing_context_hash": _hash(
                    item["routing_context_hash"],
                    "external assignment.routing_context_hash",
                ),
                "routing_result_hash": _hash(
                    item["routing_result_hash"],
                    "external assignment.routing_result_hash",
                ),
                "task_fingerprint": _hash(
                    item["task_fingerprint"],
                    "external assignment.task_fingerprint",
                ),
                "routing_reason_codes": _fast_lane_reason_codes(
                    item["routing_reason_codes"]
                ),
                "routing_safety_floor_rank": item["routing_safety_floor_rank"],
            }
            if route["routing_reason_codes"] is None or (
                type(route["routing_safety_floor_rank"]) is not int
                or not 10 <= route["routing_safety_floor_rank"] <= 110
            ):
                raise ValueError("external assignment route is invalid")
            predecessor = {
                "schema": "team-efficiency/fast-lane-external-lease-predecessor-v1",
                "source_plan_hash": source_plan_hash,
                "workflow_id_hash": workflow_id_hash,
                "task_id": task_id,
                "role": role,
                "context_hash": context_hash,
                "routing_result_hash": route["routing_result_hash"],
                "quota_evidence_hash": evidence_hash,
                "quota_snapshot_hash": snapshot_hash,
                "quota_decision_hash": decision_hash,
                "ledger_epoch": quota_evidence["ledger_epoch"],
                "active_lease_set_hash": quota_evidence["active_lease_set_hash"],
            }
            predecessor["predecessor_hash"] = _sha256_json(predecessor)
            _exact_keys(
                predecessor,
                _FAST_LANE_EXTERNAL_LEASE_PREDECESSOR_FIELDS,
                "external lease predecessor",
            )
            assignment = {
                "schema": "team-efficiency/fast-lane-external-session-assignment-v1",
                "action": "external_session_required",
                "session_state": "not_created",
                "pool": "main",
                "task_id": task_id,
                "role": role,
                "model": model,
                "reasoning_effort": effort,
                "route": route,
                "context_hash": context_hash,
                "lease_fencing_predecessor": predecessor,
                "reason": "local_capacity_exhausted",
            }
            assignment["assignment_id"] = _sha256_json(assignment)
            _exact_keys(
                assignment,
                _FAST_LANE_EXTERNAL_ASSIGNMENT_FIELDS,
                "external session assignment",
            )
            external.append(assignment)
    except (KeyError, TypeError, ValueError):
        return seal(
            "blocked",
            ("cross_session_candidate_invalid",),
            global_free_after_local_starts=global_free_after_starts,
        )
    return seal(
        "external_session_required",
        ("local_capacity_exhausted",),
        external,
        global_free_after_local_starts=global_free_after_starts,
    )


def _apply_fast_lane_cross_session_projection(
    result: Mapping[str, Any],
    *,
    reference_result: Mapping[str, Any],
    host_status: Mapping[str, Any] | None,
    occupancy: Mapping[str, Any] | None,
    quota_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    projection = _fast_lane_cross_session_projection(
        result,
        reference_result=reference_result,
        host_status=host_status,
        occupancy=occupancy,
        quota_evidence=quota_evidence,
    )
    updated = {**result, "cross_session_dispatch_projection": projection}
    updated["plan_hash"] = _sha256_json(
        {key: value for key, value in updated.items() if key != "plan_hash"}
    )
    _exact_keys(updated, _FAST_LANE_PLAN_FIELDS, "fast-lane plan")
    if len(_json_bytes(updated)) > MAX_MANIFEST_BYTES:
        raise ValueError("fast-lane plan exceeds its byte budget")
    return updated


def compile_fast_lane(
    request: Mapping[str, Any],
    *,
    reasoning_effort: str,
    enable: bool = False,
    host_status: Mapping[str, Any] | None = None,
    quota_request: Mapping[str, Any] | None = None,
    quota_trusted_key_resolver: Callable[[str], bytes | None] | None = None,
    quota_evaluation_time_utc_z: str | None = None,
    quota_verified_route_result_hashes: Iterable[str] = (),
    quota_verified_lease_scope_bindings: Iterable[str] = (),
) -> dict[str, Any]:
    activation = _fast_lane_activation(reasoning_effort, enable)
    status = (
        None if host_status is None else _validated_fast_lane_host_status(host_status)
    )
    validated = _validated_fast_lane_request(
        request,
        host_routing_context=(None if status is None else status["routing_context"]),
    )
    occupancy: dict[str, Any] | None = None
    if status is not None:
        scheduler_state = validated["scheduler_state"]
        occupancy = _fast_lane_host_slot_occupancy_audit(
            workflow_id=status["workflow_id"],
            source_plan_hash=validated["source_plan_hash"],
            phase=scheduler_state["phase"],
            running_assignments=scheduler_state["running_assignments"],
            host_bindings=status["host_bindings"],
            current_leases=status["current_leases"],
        )
        active_slots = set(occupancy["active_slot_ids"])
        filtered_assignments = [
            assignment
            for assignment in scheduler_state["running_assignments"]
            if assignment["slot_id"] in active_slots
        ]
        if len(filtered_assignments) != len(scheduler_state["running_assignments"]):
            validated = {
                **validated,
                "scheduler_state": {
                    **scheduler_state,
                    "running_assignments": filtered_assignments,
                },
            }
    result = _render_fast_lane_plan(validated, activation)
    if occupancy is not None:
        refill_plan = {
            **result["refill_plan"],
            "occupancy_audit": occupancy,
        }
        result = {**result, "refill_plan": refill_plan}
        result["plan_hash"] = _sha256_json(
            {key: value for key, value in result.items() if key != "plan_hash"}
        )
        _exact_keys(result, _FAST_LANE_PLAN_FIELDS, "fast-lane plan")
        if len(_json_bytes(result)) > MAX_MANIFEST_BYTES:
            raise ValueError("fast-lane plan exceeds its byte budget")
    reference_result = result
    quota_evidence = _fast_lane_main_capacity_evidence(
        quota_request,
        trusted_key_resolver=quota_trusted_key_resolver,
        evaluation_time_utc_z=quota_evaluation_time_utc_z,
        verified_route_result_hashes=quota_verified_route_result_hashes,
        verified_lease_scope_bindings=quota_verified_lease_scope_bindings,
    )
    if quota_request is not None:
        decision = _fast_lane_quota_decision(
            quota_request,
            trusted_key_resolver=quota_trusted_key_resolver,
            evaluation_time_utc_z=quota_evaluation_time_utc_z,
            verified_route_result_hashes=quota_verified_route_result_hashes,
            verified_lease_scope_bindings=quota_verified_lease_scope_bindings,
        )
        result = _apply_fast_lane_quota_balance(
            result,
            quota_request=quota_request,
            quota_decision=decision,
        )
    return _apply_fast_lane_cross_session_projection(
        result,
        reference_result=reference_result,
        host_status=status,
        occupancy=occupancy,
        quota_evidence=quota_evidence,
    )


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
    fast_lane.add_argument("--host-status")
    fast_lane.add_argument("--quota-input")
    fast_lane.add_argument("--quota-evaluation-time")
    fast_lane.add_argument(
        "--live-quota",
        action="store_true",
        help="read the official local Codex app-server quota source",
    )
    fast_lane.add_argument("--codex-executable")
    fast_lane.add_argument("--quota-state-path")
    fast_lane.add_argument("--quota-timeout", type=float, default=8.0)
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
            host_status = (
                None
                if args.host_status is None
                else _mapping(
                    _read_json(
                        args.host_status,
                        maximum=MAX_FAST_LANE_HOST_STATUS_BYTES,
                    ),
                    "host status",
                )
            )
            if args.quota_input is None and args.quota_evaluation_time is not None:
                raise ValueError("quota evaluation time requires quota input")
            if (
                args.quota_input is not None
                and args.quota_evaluation_time is None
                and not args.live_quota
            ):
                raise ValueError("quota input requires an evaluation time")
            quota_request = (
                None
                if args.quota_input is None
                else _mapping(
                    _read_json(
                        args.quota_input,
                        maximum=MAX_FAST_LANE_HOST_STATUS_BYTES,
                    ),
                    "quota request",
                )
            )
            quota_resolver = None
            quota_evaluation_time = args.quota_evaluation_time
            if args.live_quota:
                if quota_request is None:
                    raise ValueError("live quota requires quota input")
                quota_module = _codex_account_quota_module()
                try:
                    base_snapshot = _mapping(
                        quota_request.get("snapshot"), "quota request snapshot"
                    )
                    capacity = _mapping(
                        base_snapshot.get("capacity"), "quota request capacity"
                    )
                    provider = quota_module.CodexQuotaProvider(
                        executable=args.codex_executable,
                        timeout_seconds=args.quota_timeout,
                        state_path=(
                            None
                            if args.quota_state_path is None
                            else Path(args.quota_state_path)
                        ),
                    )
                    evidence = provider.read(capacity=capacity)
                    quota_request = quota_module.attach_snapshot(
                        quota_request, evidence
                    )
                    quota_resolver = evidence.key_resolver
                    if quota_evaluation_time is None:
                        quota_evaluation_time = str(
                            evidence.snapshot["observed_at_utc_z"]
                        )
                except quota_module.CodexQuotaError:
                    # A live source failure is a safe scheduler result, not a
                    # reason to invent a percentage or start new work.
                    print(
                        "quota source unavailable; using usage_unknown",
                        file=sys.stderr,
                    )
                    quota_resolver = _unavailable_quota_key
                    if quota_evaluation_time is None:
                        quota_evaluation_time = _utc_now_z()
            result = compile_fast_lane(
                _mapping(request, "fast-lane request"),
                reasoning_effort=_one_fast_lane_effort(args.reasoning_effort),
                enable=args.enable,
                host_status=host_status,
                quota_request=quota_request,
                quota_trusted_key_resolver=quota_resolver,
                quota_evaluation_time_utc_z=quota_evaluation_time,
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
