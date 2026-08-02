from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "team_efficiency.py"
ROUTING_SCRIPT = ROOT / "scripts" / "fastlane_routing.py"
CONTRACT = ROOT / "FASTLANE_CONTRACT.md"
sys.path.insert(0, str(ROOT.parents[1] / "mcp-tools"))

from devkit_atlas import (  # noqa: E402
    AtlasEdge,
    AtlasNode,
    ConstraintSpec,
    DependencySpec,
    EdgeRelation,
    GraphQueryResult,
    ImplementationPacket,
    NodeKind,
    SlotSpec,
    TemplateOperation,
    TestSpec,
    canonical_hash,
)
from devkit_atlas.extractors import (  # noqa: E402
    BoundExecutionReceipt,
    ExtractionRequest,
    PythonRecipeExtractor,
)
from devkit_atlas.store import AtlasStore  # noqa: E402
from devkit_runtime.bootstrap import RuntimeBootstrap  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from devkit_runtime.project_checkpoint import (  # noqa: E402
    ProjectCheckpointRuntime,
    open_project_checkpoint_rw,
)
from orchestrator.models import Task, TaskState, Workflow, WorkflowKind  # noqa: E402
from orchestrator.service import OrchestratorService  # noqa: E402
from orchestrator.store import SQLiteStore  # noqa: E402
from project_index.models import SnapshotFile  # noqa: E402
from project_index.service import ProjectIndexService  # noqa: E402

AtlasTestSpec = TestSpec
del TestSpec


def _content_hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _marker_hash(marker: str) -> str:
    return _content_hash(marker.encode("utf-8"))


def load_fastlane_routing():
    """Load the pure routing core exactly as the scheduler adapter does."""

    spec = importlib.util.spec_from_file_location("fastlane_routing", ROUTING_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fastlane_routing"] = module
    spec.loader.exec_module(module)
    return module


def _receipt_binding_hash(
    *,
    kind: str,
    request: ExtractionRequest,
    snapshot_id: str,
    files: tuple[SnapshotFile, ...],
) -> str:
    return canonical_hash(
        {
            "kind": kind,
            "workflow_id": request.workflow_id,
            "task_id": request.task_id,
            "acceptance_id": request.acceptance_id,
            "workspace_hash": request.workspace_hash,
            "checkpoint_id": request.checkpoint_id,
            "snapshot_id": snapshot_id,
            "write_scope": sorted(request.write_scope),
            "files": sorted([[item.path, item.content_hash] for item in files]),
        }
    )


def _extractor_request(
    *,
    path: str = "src/atlas_guard.py",
    marker: str = "primary",
    command_success: bool = True,
    command_exit_code: int = 0,
    complete_receipt_hashes: bool = True,
) -> ExtractionRequest:
    body = b"def atlas_guard(value: int) -> bool:\n    return value > 0\n"
    after_files = (SnapshotFile(path, _content_hash(body), body),)
    request = ExtractionRequest(
        workflow_id=f"workflow-{marker}",
        task_id=f"task-{marker}",
        acceptance_id=f"acceptance-{marker}",
        task_kind="code",
        intent_id=f"python.atlas-guard-{marker}",
        workspace_hash=_marker_hash(f"workspace-{marker}"),
        checkpoint_id=f"checkpoint-{marker}",
        input_snapshot_id=_marker_hash(f"input-{marker}"),
        output_snapshot_id=_marker_hash(f"output-{marker}"),
        write_scope=(path,),
        before_files=(),
        after_files=after_files,
        changed_nodes=(),
        coverage_gaps=(),
        execution_receipts=(),
    )
    input_hash = _receipt_binding_hash(
        kind="atlas-extraction-input-v1",
        request=request,
        snapshot_id=request.input_snapshot_id,
        files=(),
    )
    output_hash = _receipt_binding_hash(
        kind="atlas-extraction-output-v1",
        request=request,
        snapshot_id=request.output_snapshot_id,
        files=after_files,
    )
    if not complete_receipt_hashes:
        output_hash = ""

    def receipt(
        receipt_id: str,
        kind: str,
        command_spec: tuple[str, ...],
        *,
        success: bool = True,
        exit_code: int = 0,
    ) -> BoundExecutionReceipt:
        return BoundExecutionReceipt(
            receipt_id=receipt_id,
            kind=kind,
            workflow_id=request.workflow_id,
            task_id=request.task_id,
            acceptance_id=request.acceptance_id,
            workspace_hash=request.workspace_hash,
            output_snapshot_id=request.output_snapshot_id,
            command_spec=command_spec,
            command_spec_hash=canonical_hash(command_spec),
            input_hash=input_hash,
            output_hash=output_hash,
            exit_code=exit_code,
            success=success,
        )

    return replace(
        request,
        execution_receipts=(
            receipt(f"write-{marker}", "write", ()),
            receipt(
                f"command-{marker}",
                "command",
                ("python", "-m", "pytest"),
                success=command_success,
                exit_code=command_exit_code,
            ),
        ),
    )


def load_efficiency():
    if not SCRIPT.is_file():
        raise AssertionError(f"team efficiency helper is missing: {SCRIPT}")
    spec = importlib.util.spec_from_file_location("team_efficiency", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class TeamEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._fastlane_task_root = os.environ.pop("CODEX_FASTLANE_TASK_ROOT", None)
        task_temp = Path(os.environ["CODEX_TASK_TEMP"])
        task_temp = task_temp.resolve(strict=False)
        if not task_temp.is_absolute():
            raise AssertionError("CODEX_TASK_TEMP must be an absolute path")
        task_temp.mkdir(parents=True, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(dir=task_temp)
        self.temp = Path(self._temporary_directory.name)
        self.safe_root = task_temp
        self.fast_lane_task_root = (Path(r"D:\bun\tmp\codex") / self.temp.name).resolve(
            strict=False
        )
        self.fast_lane_task_root.mkdir(parents=True, exist_ok=True)
        self.project = self.fast_lane_task_root.relative_to(
            r"D:\bun\tmp\codex"
        ).as_posix()
        self.repo = (
            Path(r"D:\bun\tmp\codex\2718-devkit\worktrees") / "atlas12b-team-efficiency"
        )
        self._stores: list[SQLiteStore] = []
        self._index_services: list[ProjectIndexService] = []
        self._project_runtimes: list[ProjectCheckpointRuntime] = []

    def tearDown(self) -> None:
        for runtime in reversed(self._project_runtimes):
            runtime.close()
        for index_service in reversed(self._index_services):
            index_service.close()
        for store in reversed(self._stores):
            store.close()
        self._temporary_directory.cleanup()
        if self._fastlane_task_root is not None:
            os.environ["CODEX_FASTLANE_TASK_ROOT"] = self._fastlane_task_root

    def orchestrator(
        self,
        database_name: str,
        workflow_id: str,
        *,
        index_service: ProjectIndexService | None = None,
    ) -> tuple[SQLiteStore, OrchestratorService]:
        store = SQLiteStore(self.temp / database_name)
        self._stores.append(store)
        service = OrchestratorService(store, index_service=index_service)
        service.create_workflow(
            Workflow(
                workflow_id,
                WorkflowKind.DAG,
                "Lifecycle regression",
                "Execute the generated lifecycle plan against durable SQLite state.",
            )
        )
        return store, service

    def resolve_host_arguments(
        self,
        descriptor: dict[str, object],
        bindings: dict[str, object],
    ) -> dict[str, object]:
        self.assertEqual(
            {"tool", "arguments", "host_bound_fields"},
            set(descriptor),
        )
        arguments = copy.deepcopy(descriptor["arguments"])
        self.assertIsInstance(arguments, dict)
        for field in descriptor["host_bound_fields"]:
            reference = arguments[field]
            self.assertEqual("host", reference["source"])
            self.assertEqual(field, reference["ref"])
            self.assertTrue(reference["description"])
            arguments[field] = bindings[field]
        return arguments

    def execute_lifecycle(
        self,
        *,
        plan: dict[str, object],
        store: SQLiteStore,
        service: OrchestratorService,
        workflow_id: str,
        workspace_id: str | None = None,
        input_snapshot_id: str = "snapshot-lifecycle",
        index_service: ProjectIndexService | None = None,
    ) -> list[list[str]]:
        lifecycle = plan["registration_plan"]
        effective_workspace_id = (
            workspace_id
            if workspace_id is not None
            else canonical_hash({"kind": "lifecycle_workspace"})
        )
        base_bindings: dict[str, object] = {
            "workflow_id": workflow_id,
            "workspace_root": effective_workspace_id,
            "input_snapshot_id": input_snapshot_id,
        }
        for descriptor in lifecycle["register_steps"]:
            arguments = self.resolve_host_arguments(descriptor, base_bindings)
            service.register_task(
                Task(
                    id=arguments["task_id"],
                    workflow_id=arguments["workflow_id"],
                    title=arguments["title"],
                    owner_role=arguments["owner_role"],
                    dependencies=tuple(arguments["dependencies"]),
                    write_scope=tuple(arguments["write_scope"]),
                ),
                card=arguments["card"],
                direct_contract_hashes=tuple(arguments["direct_contract_hashes"]),
                required_evidence=tuple(arguments["required_evidence"]),
                input_hash=arguments["input_hash"],
                strict_index=arguments["strict_index"],
                workspace_id=arguments["workspace_root"],
                input_snapshot_id=arguments["input_snapshot_id"],
                task_node_ids=tuple(arguments["task_node_ids"]),
                contract_node_ids=tuple(arguments["contract_node_ids"]),
            )

        ready_results: list[list[str]] = []
        for wave_offset, wave in enumerate(lifecycle["execution_waves"]):
            ready_arguments = self.resolve_host_arguments(
                wave["workflow_ready"],
                base_bindings,
            )
            ready_results.append(
                [task.id for task in service.ready_wave(ready_arguments["workflow_id"])]
            )
            for task_step in wave["task_steps"]:
                task_id = task_step["task_id"]
                slug = task_id.lower().replace("-", "_")
                task_bindings = {
                    **base_bindings,
                    "owner": f"owner-{slug}",
                    "expires_at": "2030-01-01T00:10:00+00:00",
                    "host_target": f"/root/{slug}",
                    "now": "2030-01-01T00:00:00+00:00",
                }
                claim = self.resolve_host_arguments(
                    task_step["workflow_claim"],
                    task_bindings,
                )
                running, lease = service.claim_task(
                    claim["task_id"],
                    claim["owner"],
                    expires_at=claim["expires_at"],
                    host_target=claim["host_target"],
                    now=claim["now"],
                )
                task_bindings["lease_epoch"] = lease.epoch
                bind = self.resolve_host_arguments(
                    task_step["workflow_endpoint_bind"],
                    task_bindings,
                )
                bound = service.bind_endpoint(
                    bind["workflow_id"],
                    bind["task_id"],
                    owner=bind["owner"],
                    epoch=bind["lease_epoch"],
                    host_target=bind["host_target"],
                    now=bind["now"],
                )
                self.assertEqual(bind["host_target"], bound.host_target)
                strict_binding = store.get_index_binding(task_id)
                if strict_binding is not None:
                    self.assertIsNotNone(index_service)
                    self.assertIsNotNone(workspace_id)
                    assert workspace_id is not None
                    if running.write_scope:
                        service.record_checkpoint(
                            task_id,
                            owner=lease.owner,
                            epoch=lease.epoch,
                            checkpoint_id=canonical_hash(
                                {"kind": "lifecycle_checkpoint", "task_id": task_id}
                            ),
                            now=task_bindings["now"],
                        )
                        service.record_output_snapshot(
                            task_id,
                            owner=lease.owner,
                            epoch=lease.epoch,
                            snapshot_id=strict_binding.input_snapshot_id,
                            diff_hash=canonical_hash(
                                {"kind": "lifecycle_diff", "task_id": task_id}
                            ),
                            now=task_bindings["now"],
                        )
                    query = index_service.query(
                        workspace_id,
                        strict_binding.input_snapshot_id,
                        f"lifecycle {task_id}",
                        max_nodes=1,
                        max_depth=0,
                        source_lines=0,
                    )
                    service.record_index_query(
                        workflow_id,
                        task_id,
                        owner=lease.owner,
                        epoch=lease.epoch,
                        trace_id=query.trace_id,
                        snapshot_id=strict_binding.input_snapshot_id,
                        miss_escape_used=False,
                        now=task_bindings["now"],
                    )
                    service.register_artifact(
                        workflow_id,
                        task_id,
                        owner=lease.owner,
                        epoch=lease.epoch,
                        kind="verification",
                        content_hash=canonical_hash(
                            {"kind": "lifecycle_verification", "task_id": task_id}
                        ),
                        safe_path=f"evidence/{task_id}.json",
                        size=1,
                        redaction_version="r1",
                        snapshot_id=strict_binding.input_snapshot_id,
                        now=task_bindings["now"],
                    )
                done = service.complete_task(
                    task_id,
                    expected_version=running.version,
                    owner=lease.owner,
                    epoch=lease.epoch,
                    now=task_bindings["now"],
                )
                self.assertEqual(TaskState.DONE, done.state)

            barrier = wave["completion_barrier"]
            self.assertEqual("all_tasks_reach_state", barrier["condition"])
            self.assertEqual(wave["task_ids"], barrier["task_ids"])
            self.assertEqual("DONE", barrier["required_state"])
            expected_next = (
                wave_offset + 2
                if wave_offset + 1 < len(lifecycle["execution_waves"])
                else None
            )
            self.assertEqual(expected_next, barrier["advance_to_wave_index"])
            states = {
                task.id: task.state
                for task in store.list_tasks(workflow_id)
                if task.id in barrier["task_ids"]
            }
            self.assertEqual(
                {TaskState.DONE},
                set(states.values()),
                "the next lifecycle wave must wait for every barrier task",
            )
        return ready_results

    def bootstrap_kwargs(self) -> dict[str, object]:
        return {
            "task_id": "ATLAS-12B",
            "base_commit": "a" * 40,
            "branch": "feature/code-atlas-v1-team-efficiency",
            "write_scope": ["skills/work-methodology/scripts/team_efficiency.py"],
            "repo": self.repo,
            "project": self.project,
            "worktree": self.fast_lane_task_root
            / "worktrees"
            / "atlas12b-team-efficiency",
            "temp_target": self.fast_lane_task_root / "tasks" / "atlas12b",
        }

    def resume_packet(self) -> dict[str, object]:
        return {
            "workflow_id": "atlas-v03",
            "task_id": "ATLAS-12B",
            "lease_epoch": 2,
            "endpoint": "/root/atlas12b_worker",
            "base_commit": "a" * 40,
            "candidate_commit": "b" * 40,
            "worktree_id": "atlas12b-team-efficiency",
            "write_scope_hash": f"sha256:{'c' * 64}",
            "latest_red": {
                "command": "python -m unittest focused",
                "result": "1 failed",
            },
            "latest_green": {
                "command": "python -m unittest focused",
                "result": "12 passed",
            },
            "contract_hashes": [f"sha256:{'d' * 64}"],
            "evidence_hashes": [f"sha256:{'e' * 64}"],
            "next_action": "run the focused verification lane",
            "redacted": True,
        }

    def cache_inputs(self) -> dict[str, object]:
        return {
            "candidate_commit": "a" * 40,
            "candidate_tree": "b" * 40,
            "write_scope_hash": f"sha256:{'c' * 64}",
            "argv": ["python", "-m", "unittest", "tests.test_team_efficiency"],
            "toolchain_fingerprint": "python-3.12.4;ruff-0.8.2",
            "platform_fingerprint": "windows-amd64",
            "dependency_lock_hashes": {"uv.lock": f"sha256:{'d' * 64}"},
            "test_lane": "core",
        }

    def decomposition_manifest(self) -> dict[str, object]:
        return {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Ship a bounded team-efficiency helper",
            "capacity": 2,
            "decomposition": "artifact_boundaries",
            "source_kind": "explicit_artifact_boundaries",
            "artifacts": [
                {
                    "task_id": "ATLAS-12B-A",
                    "goal": "Implement the helper",
                    "output_boundary": "helper module",
                    "write_scope": [
                        "skills/work-methodology/scripts/team_efficiency.py"
                    ],
                    "depends_on": [],
                    "required_evidence": ["focused-helper-tests"],
                    "complexity": "routine",
                    "execution_contracts": ["contracts/helper-api"],
                },
                {
                    "task_id": "ATLAS-12B-B",
                    "goal": "Document the helper",
                    "output_boundary": "operator reference",
                    "write_scope": [
                        "skills/work-methodology/references/efficiency-automation.md"
                    ],
                    "depends_on": [],
                    "required_evidence": ["reference-review"],
                    "complexity": "moderate",
                    "execution_contracts": ["contracts/helper-api"],
                },
                {
                    "task_id": "ATLAS-12B-C",
                    "goal": "Validate the integrated helper",
                    "output_boundary": "verification record",
                    "write_scope": [
                        "skills/work-methodology/tests/test_team_efficiency.py"
                    ],
                    "depends_on": ["ATLAS-12B-A"],
                    "required_evidence": ["focused-team-efficiency"],
                    "complexity": "moderate",
                    "execution_contracts": ["contracts/helper-api"],
                },
            ],
        }

    def fast_lane_request(
        self,
        helper,
        *,
        include_contexts: bool = True,
        work_package: dict[str, object] | None = None,
    ) -> dict[str, object]:
        work_package = copy.deepcopy(
            self.decomposition_manifest() if work_package is None else work_package
        )
        source_plan = helper.decompose(work_package)
        integration_commit = "a" * 40
        integration_tree = "b" * 40
        target_gates: list[dict[str, object]] = []
        execution_contexts: list[dict[str, object]] = []

        for unit in source_plan["units"]:
            task_id = unit["task_id"]
            task_slug = task_id.lower()
            failure_ids = [
                "tests.test_team_efficiency.TeamEfficiencyTests."
                f"test_fast_lane_{task_slug}_driver"
            ]
            target_gates.append(
                {
                    "task_id": task_id,
                    "driver_gate_id": "focused",
                    "gates": [
                        {
                            "gate_id": "focused",
                            "argv": [
                                "python",
                                "-m",
                                "unittest",
                                "tests.test_team_efficiency",
                            ],
                            "red_expected_exit_codes": [1],
                            "green_expected_exit_code": 0,
                            "timeout_seconds": 300,
                            "red_failure_ids": failure_ids,
                            "red_failure_fingerprint": helper._sha256_json(
                                {
                                    "schema": "team-efficiency/red-failure-identity-v1",
                                    "gate_id": "focused",
                                    "failure_ids": failure_ids,
                                }
                            ),
                            "acceptance_constraint_hashes": [],
                        }
                    ],
                }
            )
            execution_contexts.append(
                {
                    "task_id": task_id,
                    "bootstrap_plan": helper.build_bootstrap_plan(
                        task_id=task_id,
                        base_commit=integration_commit,
                        branch=f"codex/fast-lane-{task_slug}",
                        write_scope=unit["write_scope"],
                        repo=self.repo,
                        project=self.project,
                        worktree=(
                            self.fast_lane_task_root
                            / "worktrees"
                            / f"fast-lane-{task_slug}"
                        ),
                        temp_target=(
                            self.fast_lane_task_root
                            / "tasks"
                            / f"fast-lane-{task_slug}"
                        ),
                    ),
                    "workspace_input_snapshot_id": self.fast_lane_execution_snapshot_id(
                        helper, task_id
                    ),
                }
            )

        return {
            "schema": "team-efficiency/fast-lane-request-v1",
            "work_package": work_package,
            "target_gates": target_gates,
            "execution_contexts": execution_contexts if include_contexts else [],
            "read_contexts": [],
            "remediation_request": None,
            "scheduler_state": {
                "source_plan_hash": None,
                "phase": "execution",
                "integration_state": {
                    "commit": integration_commit,
                    "tree": integration_tree,
                    "integration_workspace_snapshot_id": None,
                },
                "lane0_state": {
                    "active_task_id": None,
                    "owned_write_scopes": [],
                },
                "completed_tasks": [],
                "review_ready_candidates": [],
                "reviewed_candidates": [],
                "prewarmed_evidence": [],
                "design_evidence": [],
                "running_assignments": [],
                "dispatch_contexts": [],
                "blocked_task_ids": [],
                "pending_design_probe_task_ids": [],
                "slot_epochs": {
                    "slot-1": 0,
                    "slot-2": 0,
                    "slot-3": 0,
                },
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
            },
        }

    def fast_lane_contexts_empty_request(self, helper) -> dict[str, object]:
        return self.fast_lane_request(helper, include_contexts=False)

    def fast_lane_gate(
        self,
        *,
        gate_id: str = "focused",
        argv: list[str] | None = None,
        red_expected_exit_codes: list[int] | None = None,
        green_exit_code: int = 0,
        red_failure_ids: list[str] | None = None,
        acceptance_constraint_hashes: list[str] | None = None,
    ) -> dict[str, object]:
        normalized_red_codes = (
            [1] if red_expected_exit_codes is None else red_expected_exit_codes
        )
        normalized_failure_ids = (
            ["tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_driver"]
            if red_failure_ids is None
            else red_failure_ids
        )
        red_failure_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "schema": "team-efficiency/red-failure-identity-v1",
                        "gate_id": gate_id,
                        "failure_ids": sorted(normalized_failure_ids),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if normalized_failure_ids
            else None
        )
        return {
            "gate_id": gate_id,
            "argv": (
                [
                    "python",
                    "-m",
                    "unittest",
                    "tests.test_team_efficiency",
                ]
                if argv is None
                else argv
            ),
            "red_expected_exit_codes": normalized_red_codes,
            "green_expected_exit_code": green_exit_code,
            "timeout_seconds": 300,
            "red_failure_ids": normalized_failure_ids,
            "red_failure_fingerprint": red_failure_fingerprint,
            "acceptance_constraint_hashes": (
                []
                if acceptance_constraint_hashes is None
                else acceptance_constraint_hashes
            ),
        }

    def fast_lane_execution_context(
        self,
        helper,
        *,
        task_id: str,
        bootstrap_task_id: str | None = None,
        base_commit: str,
        write_scope: list[str],
        branch: str | None = None,
        worktree: str | Path | None = None,
        temp_target: str | Path | None = None,
    ) -> dict[str, object]:
        task_slug = task_id.lower()
        return {
            "task_id": task_id,
            "bootstrap_plan": helper.build_bootstrap_plan(
                task_id=(task_id if bootstrap_task_id is None else bootstrap_task_id),
                base_commit=base_commit,
                branch=(f"codex/fast-lane-{task_slug}" if branch is None else branch),
                write_scope=write_scope,
                repo=self.repo,
                project=self.project,
                worktree=(
                    self.fast_lane_task_root / "worktrees" / f"fast-lane-{task_slug}"
                    if worktree is None
                    else worktree
                ),
                temp_target=(
                    self.fast_lane_task_root / "tasks" / f"fast-lane-{task_slug}"
                    if temp_target is None
                    else temp_target
                ),
            ),
            "workspace_input_snapshot_id": self.fast_lane_execution_snapshot_id(
                helper, task_id
            ),
        }

    def fast_lane_execution_snapshot_id(self, helper, task_id: str) -> str:
        return helper._sha256_json({"snapshot": task_id, "role": "execution"})

    def fast_lane_host_status(
        self,
        helper,
        request: dict[str, object],
        *,
        capabilities: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Build the bounded, attested core-input mapping for one scheduler event."""

        core = load_fastlane_routing()
        state = request["scheduler_state"]
        source_plan = helper.decompose(request["work_package"])
        source_plan_hash = helper._sha256_json(source_plan)
        remediation = helper._validated_fast_lane_remediation_request(
            request["remediation_request"],
            source_plan=source_plan,
            source_plan_hash=source_plan_hash,
            integration_state=helper._fast_lane_integration_state(state),
        )
        if remediation is not None and not remediation.get("_automation_stopped"):
            source_plan = helper._fast_lane_source_with_remediation(
                source_plan, remediation
            )
        completed = {str(record["task_id"]) for record in state["completed_tasks"]}
        running = {
            (str(assignment["task_id"]), str(assignment["role"])): assignment
            for assignment in state["running_assignments"]
        }
        capability_report = copy.deepcopy(
            {
                "schema": "2718lab-devkit/host-capabilities-v1",
                "host_id_hash": _marker_hash("fast-lane-host"),
                "capability_epoch": 1,
                "total_slots": 8,
                "model_slot_limits": {
                    "luna": 4,
                    "terra": 4,
                    "sol": 4,
                    "spark": 1,
                },
                "models": [
                    {
                        "model_id": "gpt-5.3-codex-spark",
                        "status": "available",
                        "efforts": ["medium"],
                    },
                    {
                        "model_id": "gpt-5.6-luna",
                        "status": "available",
                        "efforts": ["low", "medium", "high", "xhigh"],
                    },
                    {
                        "model_id": "gpt-5.6-sol",
                        "status": "available",
                        "efforts": ["high", "xhigh", "max"],
                    },
                    {
                        "model_id": "gpt-5.6-terra",
                        "status": "available",
                        "efforts": ["medium", "high", "xhigh", "max"],
                    },
                ],
                "entitlements": ["spark_preview"],
            }
            if capabilities is None
            else capabilities
        )
        policy = core.load_policy()
        policy_hash = core.policy_hash(policy)
        routes: list[dict[str, object]] = []
        core_roles = {
            "execution": "execution",
            "verification": "verification",
            "prewarm": "prewarm",
            "review": "review",
            "design_probe": "design",
        }
        for unit in source_plan["units"]:
            task_id = str(unit["task_id"])
            legacy = {
                "schema": "2718lab-devkit/legacy-route-input-v1",
                "compatibility_version": 1,
                "complexity": None,
                "recommended_route": unit["recommended_route"],
            }
            adaptation = core.adapt_legacy_profile(legacy, task_id=task_id)
            for scheduler_role, core_role in core_roles.items():
                dependency_ids = (
                    []
                    if scheduler_role in {"prewarm", "design_probe"}
                    else list(unit["depends_on"])
                )
                dependency_without_hash = {
                    "schema": "2718lab-devkit/dependency-state-v1",
                    "graph_epoch": 1,
                    "direct_dependency_ids": dependency_ids,
                    "completed_dependency_ids": sorted(
                        set(dependency_ids).intersection(completed)
                    ),
                }
                dependency_state = {
                    **dependency_without_hash,
                    "dependency_state_hash": helper._sha256_json(
                        dependency_without_hash
                    ),
                }
                task = copy.deepcopy(adaptation.profile)
                read_only = scheduler_role != "execution"
                task.update(
                    {
                        "role": core_role,
                        "access": "read_only" if read_only else "workspace_write",
                        "write_scope_count": 0 if read_only else 1,
                        "write_scope_breadth": "none" if read_only else "single_file",
                        "profile_evidence_hash": helper._sha256_json(
                            {
                                "task_id": task_id,
                                "scheduler_role": scheduler_role,
                                "kind": "routing-profile",
                            }
                        ),
                    }
                )
                assignment = running.get((task_id, scheduler_role))
                lease_epoch = (
                    0 if assignment is None else int(assignment["assignment_epoch"])
                )
                scheduler_facts = {
                    "event_seq": max(1, 1 + sum(state["slot_epochs"].values())),
                    "route_epoch": 1,
                    "override_epoch": 0,
                    "recovery_epoch": 0,
                    "ready_event_seq": max(1, 1 + sum(state["slot_epochs"].values())),
                    "dispatch_cause": "task_ready",
                    "transport_state": "connected",
                    "execution_state": (
                        "active" if assignment is not None else "unknown"
                    ),
                    "lease_state": (
                        "active" if assignment is not None else "unclaimed"
                    ),
                    "evidence_state": "none",
                    "lease_epoch": lease_epoch,
                    "recovery_probe_count_epoch": 0,
                    "fence_count_epoch": 0,
                    "fenced_replacement_count_task": 0,
                }
                routes.append(
                    {
                        "task_id": task_id,
                        "scheduler_role": scheduler_role,
                        "request": {
                            "schema": "2718lab-devkit/fastlane-routing-request-v3",
                            "policy_hash": policy_hash,
                            "task": task,
                            "dependency_state": dependency_state,
                            "scope_state": {
                                "schema": "2718lab-devkit/scope-state-v1",
                                "scope_epoch": 1,
                                "owned_scope_hash": _marker_hash(f"scope:{task_id}"),
                                "conflicting_task_ids": [],
                                "active_writer_task_ids": [],
                            },
                            "scheduler_facts": scheduler_facts,
                            "host_capabilities": copy.deepcopy(capability_report),
                            "override_receipt": None,
                            "legacy": legacy,
                        },
                        "trusted_authorization_evidence_hashes": [],
                        "trusted_override_receipt_hashes": [],
                        "trusted_evidence_hashes": [],
                        "coordinator_endpoint_hash": None,
                        "compatibility_floor": adaptation.floor_rank,
                    }
                )
        workflow_id = "fast-lane-workflow"
        leases: list[dict[str, object]] = []
        bindings: list[dict[str, object]] = []
        for assignment in state["running_assignments"]:
            task_id = str(assignment["task_id"])
            endpoint = f"/fast-lane/{task_id.lower()}"
            leases.append(
                {
                    "task_id": task_id,
                    "lease_epoch": assignment["assignment_epoch"],
                    "endpoint": endpoint,
                    "state": "running",
                }
            )
            bindings.append(
                {
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "slot_id": assignment["slot_id"],
                    "assignment_epoch": assignment["assignment_epoch"],
                    "assignment_token": assignment["assignment_token"],
                    "context_hash": assignment["context_hash"],
                    "lease_epoch": assignment["assignment_epoch"],
                    "endpoint": endpoint,
                    "state": "running",
                }
            )
        return {
            "workflow_id": workflow_id,
            "current_leases": leases,
            "host_bindings": bindings,
            "routing_context": {
                "schema": "team-efficiency/fast-lane-routing-context-v1",
                "routes": routes,
            },
        }

    def compile_fast_lane(
        self,
        helper,
        request: dict[str, object],
        *,
        reasoning_effort: str,
        enable: bool = False,
        host_status: dict[str, object] | None = None,
    ) -> dict[str, object]:
        default_status = self.fast_lane_host_status(helper, request)
        if host_status is None:
            effective_status = default_status
        else:
            effective_status = copy.deepcopy(host_status)
            effective_status.setdefault(
                "routing_context", default_status["routing_context"]
            )
        return helper.compile_fast_lane(
            request,
            reasoning_effort=reasoning_effort,
            enable=enable,
            host_status=effective_status,
        )

    def fully_bound_fast_lane_manual_request(self, helper) -> dict[str, object]:
        request = self.fast_lane_contexts_empty_request(helper)
        source_plan = helper.decompose(request["work_package"])
        base_commit = request["scheduler_state"]["integration_state"]["commit"]
        request["execution_contexts"] = [
            self.fast_lane_execution_context(
                helper,
                task_id=unit["task_id"],
                base_commit=base_commit,
                write_scope=list(unit["write_scope"]),
            )
            for unit in source_plan["units"]
        ]
        return request

    def fast_lane_schedule_request(self, helper) -> dict[str, object]:
        work_package = self.decomposition_manifest()
        work_package["capacity"] = 3
        work_package["artifacts"] = [
            {
                "task_id": "FAST-LANE-ROUTINE",
                "goal": "Implement routine work",
                "output_boundary": "routine writer",
                "write_scope": ["src/fast_lane/routine.py"],
                "depends_on": [],
                "required_evidence": ["routine-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
            {
                "task_id": "FAST-LANE-MODERATE",
                "goal": "Implement moderate work",
                "output_boundary": "moderate writer",
                "write_scope": ["src/fast_lane/moderate.py"],
                "depends_on": [],
                "required_evidence": ["moderate-proof"],
                "complexity": "moderate",
                "execution_contracts": ["contracts/fast-lane"],
            },
            {
                "task_id": "FAST-LANE-FUTURE",
                "goal": "Prepare the future work",
                "output_boundary": "future writer",
                "write_scope": ["src/fast_lane/future.py"],
                "depends_on": ["FAST-LANE-MODERATE"],
                "required_evidence": ["future-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
        ]
        request = self.fast_lane_request(helper, work_package=work_package)
        request["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                task_id="FAST-LANE-FUTURE",
                role="prewarm",
                read_scope=["src/fast_lane/future.py"],
            )
        ]
        return request

    def fast_lane_code_atlas_request(self, helper) -> dict[str, object]:
        work_package = self.code_atlas_manifest()
        source_plan = helper.decompose(work_package)
        integration_commit = "a" * 40
        integration_tree = "b" * 40
        packet_test = work_package["packet"]["tests"][0]
        request = self.fast_lane_contexts_empty_request(helper)
        request["work_package"] = work_package
        request["target_gates"] = []
        request["execution_contexts"] = []
        request["read_contexts"] = []
        request["scheduler_state"]["integration_state"] = {
            "commit": integration_commit,
            "tree": integration_tree,
            "integration_workspace_snapshot_id": None,
        }
        for unit in source_plan["units"]:
            is_verification = unit["unit_kind"] == "verification"
            request["target_gates"].append(
                {
                    "task_id": unit["task_id"],
                    "driver_gate_id": None if is_verification else "focused",
                    "gates": [
                        self.fast_lane_gate(
                            argv=list(packet_test["argv"]),
                            red_expected_exit_codes=[] if is_verification else [1],
                            green_exit_code=packet_test["expected_exit_code"],
                            red_failure_ids=[] if is_verification else None,
                            acceptance_constraint_hashes=list(
                                unit["acceptance_constraints"]
                            ),
                        )
                    ],
                }
            )
            if not is_verification:
                request["execution_contexts"].append(
                    self.fast_lane_execution_context(
                        helper,
                        task_id=unit["task_id"],
                        base_commit=integration_commit,
                        write_scope=list(unit["write_scope"]),
                    )
                )
        code_units = [
            unit for unit in source_plan["units"] if unit["unit_kind"] == "code"
        ]
        verification_unit = next(
            unit for unit in source_plan["units"] if unit["unit_kind"] == "verification"
        )
        for unit, role in (
            (code_units[2], "prewarm"),
            (verification_unit, "verification"),
        ):
            task_slug = unit["task_id"].lower()
            request["read_contexts"].append(
                self.fast_lane_read_context(
                    helper,
                    task_id=unit["task_id"],
                    role=role,
                    worktree=(
                        self.fast_lane_task_root
                        / "worktrees"
                        / f"fast-lane-read-{task_slug}"
                    ),
                    temp_target=(
                        self.fast_lane_task_root
                        / "tasks"
                        / f"fast-lane-read-{task_slug}"
                    ),
                    read_scope=list(unit["write_scope"]) or ["src/service.py"],
                )
            )
        return request

    def fast_lane_read_context(
        self,
        helper,
        *,
        task_id: str = "ATLAS-12B-A",
        role: str = "prewarm",
        project: str | None = None,
        repo: str | Path | None = None,
        worktree: str | Path | None = None,
        temp_target: str | Path | None = None,
        base_commit: str = "a" * 40,
        tree: str = "b" * 40,
        workspace_input_snapshot_id: str | None = None,
        read_scope: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "task_id": task_id,
            "role": role,
            "project": self.project if project is None else project,
            "task_root_hash": helper._fastlane_task_root_hash(
                helper._configured_fastlane_task_root()
            ),
            "repo": str(self.repo if repo is None else repo),
            "worktree": str(
                self.fast_lane_task_root / "worktrees" / "fast-lane-read"
                if worktree is None
                else worktree
            ),
            "base_commit": base_commit,
            "tree": tree,
            "workspace_input_snapshot_id": (
                helper._sha256_json({"snapshot": task_id})
                if workspace_input_snapshot_id is None
                else workspace_input_snapshot_id
            ),
            "read_scope": (
                ["mcp-tools/devkit_fastlane/scripts/team_efficiency.py"]
                if read_scope is None
                else read_scope
            ),
            "temp_target": str(
                self.fast_lane_task_root / "tasks" / "fast-lane-read"
                if temp_target is None
                else temp_target
            ),
        }

    def fast_lane_running_request(self, helper) -> dict[str, object]:
        request = self.fully_bound_fast_lane_manual_request(helper)
        source_plan = helper.decompose(request["work_package"])
        unit = source_plan["units"][0]
        assignment, dispatch_context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=unit["task_id"]
        )
        request["scheduler_state"]["source_plan_hash"] = validated["source_plan_hash"]
        request["scheduler_state"]["slot_epochs"]["slot-1"] = 1
        request["scheduler_state"]["running_assignments"] = [assignment]
        request["scheduler_state"]["dispatch_contexts"] = [dispatch_context]
        return request

    def fast_lane_assignment_for(
        self,
        helper,
        request: dict[str, object],
        *,
        task_id: str,
        role: str = "execution",
        slot_id: str = "slot-1",
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        host_status = self.fast_lane_host_status(helper, request)
        route_entry = next(
            entry
            for entry in host_status["routing_context"]["routes"]
            if entry["task_id"] == task_id and entry["scheduler_role"] == role
        )
        dependency_state = route_entry["request"]["dependency_state"]
        dependency_state["completed_dependency_ids"] = list(
            dependency_state["direct_dependency_ids"]
        )
        dependency_state_without_hash = dict(dependency_state)
        dependency_state_without_hash.pop("dependency_state_hash")
        dependency_state["dependency_state_hash"] = helper._sha256_json(
            dependency_state_without_hash
        )
        try:
            validated = helper._validated_fast_lane_request(
                copy.deepcopy(request),
                host_routing_context=host_status["routing_context"],
            )
        except ValueError as error:
            self.fail(f"fast-lane assignment fixture must validate: {error}")
        unit = next(
            item
            for item in validated["source_plan"]["units"]
            if item["task_id"] == task_id
        )
        resolved_route = helper._fast_lane_route(
            validated["routing_context"], unit, role
        )
        self.assertIsNotNone(resolved_route)
        assignment = helper._fast_lane_assignment(
            validated, unit, role, slot_id, resolved_route
        )
        context = assignment.pop("_context")
        assignment.pop("action")
        return assignment, context, validated

    def fast_lane_terminal_result(
        self,
        helper,
        assignment: dict[str, object],
        *,
        outcome: str | None = None,
        candidate_commit: str = "c" * 40,
        candidate_tree: str = "d" * 40,
    ) -> dict[str, object]:
        task_id = str(assignment["task_id"])
        role = str(assignment["role"])

        def marker(label: str) -> str:
            return helper._sha256_json(
                {"terminal": label, "task_id": task_id, "role": role}
            )

        defaults = {
            "execution": "candidate",
            "verification": "verified",
            "prewarm": "evidence",
            "review": "pass",
            "design_probe": "evidence",
        }
        resolved_outcome = defaults[role] if outcome is None else outcome
        candidate = role == "execution" and resolved_outcome == "candidate"
        verified = role == "verification" and resolved_outcome == "verified"
        observed = (
            role in {"prewarm", "design_probe"} and resolved_outcome == "evidence"
        )
        reviewed = role == "review" and resolved_outcome == "pass"
        return {
            "schema": "team-efficiency/fast-lane-terminal-result-v1",
            "dispatch_receipt": copy.deepcopy(assignment["dispatch_receipt"]),
            "assignment_token": assignment["assignment_token"],
            "task_id": task_id,
            "role": role,
            "outcome": resolved_outcome,
            "candidate_commit": candidate_commit if candidate else None,
            "candidate_tree": candidate_tree if candidate else None,
            "red_evidence_hashes": [marker("red")] if candidate else [],
            "green_evidence_hashes": [marker("green")] if candidate or verified else [],
            "evidence_hash": marker("evidence") if verified or observed else None,
            "review_hash": marker("review") if reviewed else None,
            "input_query_trace_id": marker("input-query")
            if candidate or verified
            else None,
            "checkpoint_id": marker("checkpoint") if candidate else None,
            "output_workspace_snapshot_id": marker("output-snapshot")
            if candidate
            else None,
            "output_query_trace_id": marker("output-query") if candidate else None,
        }

    def fast_lane_completion_receipt(
        self,
        helper,
        terminal_result: dict[str, object],
        *,
        completion_kind: str = "integrated_candidate",
        integration_commit: str = "a" * 40,
        integration_tree: str = "b" * 40,
        workspace_input_snapshot_id: str | None = None,
    ) -> dict[str, object]:
        writer = completion_kind == "integrated_candidate"
        task_id = str(terminal_result["task_id"])
        snapshot = workspace_input_snapshot_id
        if snapshot is None:
            snapshot = (
                self.fast_lane_execution_snapshot_id(helper, task_id)
                if writer
                else helper._sha256_json({"snapshot": task_id})
            )
        return {
            "schema": "team-efficiency/fast-lane-completion-receipt-v1",
            "terminal_result_hash": helper._sha256_json(terminal_result),
            "workflow_id_hash": helper._sha256_json(
                {"workflow": terminal_result["task_id"]}
            ),
            "task_id": terminal_result["task_id"],
            "completion_kind": completion_kind,
            "integration_commit": integration_commit,
            "integration_tree": integration_tree,
            "candidate_commit": terminal_result["candidate_commit"] if writer else None,
            "candidate_tree": terminal_result["candidate_tree"] if writer else None,
            "integration_proof_hash": helper._sha256_json(
                {"integration": terminal_result["task_id"]}
            )
            if writer
            else None,
            "workspace_input_snapshot_id": snapshot,
            "output_workspace_snapshot_id": (
                terminal_result["output_workspace_snapshot_id"] if writer else None
            ),
            "verification_evidence_hashes": list(
                terminal_result["green_evidence_hashes"]
            ),
        }

    def fast_lane_completed_record(
        self,
        helper,
        terminal_result: dict[str, object],
        *,
        completion_kind: str = "integrated_candidate",
        workspace_input_snapshot_id: str | None = None,
    ) -> dict[str, object]:
        receipt = self.fast_lane_completion_receipt(
            helper,
            terminal_result,
            completion_kind=completion_kind,
            workspace_input_snapshot_id=workspace_input_snapshot_id,
        )
        receipt_hash = helper._sha256_json(receipt)
        return {
            "task_id": terminal_result["task_id"],
            "completion_kind": completion_kind,
            "integration_commit": receipt["integration_commit"],
            "integration_tree": receipt["integration_tree"],
            "result_hash": receipt_hash,
            "terminal_result_hash": helper._sha256_json(terminal_result),
            "terminal_result": terminal_result,
            "completion_receipt_hash": receipt_hash,
            "completion_receipt": receipt,
        }

    def fast_lane_remediation_request(
        self,
        helper,
        request: dict[str, object],
        *,
        write_scope: list[str] | None = None,
        round_value: int = 1,
    ) -> dict[str, object]:
        source_plan = helper.decompose(request["work_package"])
        source_plan_hash = helper._sha256_json(source_plan)
        source_unit = source_plan["units"][0]
        blocker_review_hash = helper._sha256_json({"blocker": "one"})
        seed = helper._sha256_json(
            {
                "schema": "fast-lane-remediation-id-v1",
                "source_plan_hash": source_plan_hash,
                "blocker_review_hash": blocker_review_hash,
                "round": 1,
            }
        )
        task_id = "FLR1-" + seed.removeprefix("sha256:")[:24]
        scope = list(source_unit["write_scope"] if write_scope is None else write_scope)
        gate = self.fast_lane_gate(gate_id="remediation-focused")
        return {
            "schema": "team-efficiency/fast-lane-remediation-request-v1",
            "round": round_value,
            "task_id": task_id,
            "source_plan_hash": source_plan_hash,
            "blocker_review_hash": blocker_review_hash,
            "finding_hash": helper._sha256_json({"finding": "one"}),
            "severity": "important",
            "affected_task_ids": [source_unit["task_id"]],
            "dependencies": [source_unit["task_id"]],
            "base_integration_commit": request["scheduler_state"]["integration_state"][
                "commit"
            ],
            "base_integration_tree": request["scheduler_state"]["integration_state"][
                "tree"
            ],
            "goal": "Apply one bounded approved remediation",
            "output_boundary": "bounded remediation candidate",
            "write_scope": scope,
            "direct_contract_hashes": list(source_unit["direct_contract_hashes"]),
            "required_evidence": ["focused-remediation-test"],
            "task_node_ids": [helper._sha256_json({"remediation": task_id})],
            "contract_node_ids": [],
            "acceptance_constraints": [],
            "driver_gate_id": "remediation-focused",
            "target_gates": [gate],
        }

    def run_fast_lane_cli(
        self,
        helper,
        arguments: list[str],
    ) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            try:
                exit_code = helper.main(arguments)
            except SystemExit as error:
                exit_code = error.code
        return exit_code, output.getvalue(), errors.getvalue()

    def code_atlas_manifest(self) -> dict[str, object]:
        def digest(marker: str) -> str:
            return f"sha256:{marker * 64}"

        operations = (
            TemplateOperation("create_python_file", "service_primary", digest("4")),
            TemplateOperation("append_python_nodes", "service_secondary", digest("5")),
            TemplateOperation("create_python_file", "schema", digest("6")),
            TemplateOperation("create_python_file", "tests", digest("7")),
            TemplateOperation("create_python_file", "operator_notes", digest("8")),
        )
        slots = tuple(
            SlotSpec(name, "relative_python_path")
            for name in (
                "service_primary",
                "service_secondary",
                "schema",
                "tests",
                "operator_notes",
            )
        )
        provisional = ImplementationPacket(
            packet_id="",
            trace_id=digest("1"),
            workspace_id=digest("f"),
            snapshot_id=digest("0"),
            recipe_id=digest("2"),
            node_ids=(digest("b"),),
            edge_ids=(digest("c"),),
            evidence_windows=(
                {
                    "path": "src/existing.py",
                    "content_hash": digest("3"),
                    "start_line": 1,
                    "end_line": 4,
                },
            ),
            evidence_hashes=(digest("3"),),
            operations=operations,
            slots=slots,
            constraints=(ConstraintSpec("path_suffix", "schema", ".py"),),
            dependencies=(DependencySpec("pytest", "python", ">=8"),),
            tests=(
                AtlasTestSpec(
                    ("python", "-m", "pytest", "tests/test_service.py"),
                    0,
                ),
            ),
            gaps=(),
            source_hashes=(digest("d"),),
            template_hashes=tuple(
                sorted({operation.template_hash for operation in operations})
            ),
            receipt_hashes=(digest("e"),),
            next_action="atlas_render",
            request_hash=digest("a"),
            matcher_version="atlas-matcher/v1",
            target_paths=(
                "docs/service.md",
                "src/schema.py",
                "src/service.py",
                "tests/test_service.py",
            ),
            target_symbols=(),
        )
        packet_payload = provisional.to_dict()
        del packet_payload["packet_id"]
        packet = replace(provisional, packet_id=canonical_hash(packet_payload))
        return {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Derive bounded code work from an implementation packet",
            "capacity": 4,
            "decomposition": "atlas_evidence",
            "source_kind": "code_atlas_packet",
            "packet": packet.to_dict(),
            "path_bindings": {
                "service_primary": "src/service.py",
                "service_secondary": "src/service.py",
                "schema": "src/schema.py",
                "tests": "tests/test_service.py",
                "operator_notes": "docs/service.md",
            },
        }

    def extractor_episode_manifest(
        self,
        *,
        command_success: bool = True,
        command_exit_code: int = 0,
        complete_receipt_hashes: bool = True,
        eligible_override: bool | None = None,
    ) -> dict[str, object]:
        result = PythonRecipeExtractor().extract(
            _extractor_request(
                command_success=command_success,
                command_exit_code=command_exit_code,
                complete_receipt_hashes=complete_receipt_hashes,
            )
        )
        graph = GraphQueryResult(result.nodes, result.edges, False)
        return {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Compile one real extractor TaskEpisode",
            "capacity": 2,
            "decomposition": "atlas_evidence",
            "source_kind": "task_episode_graph",
            "eligible": (
                result.eligible if eligible_override is None else eligible_override
            ),
            "graph": graph.to_dict(),
        }

    def declared_edge_episode_manifest(self) -> dict[str, object]:
        """Model one external downgrade; the extractor result remains untouched."""
        result = PythonRecipeExtractor().extract(_extractor_request())
        nodes_by_id = {node.node_id: node for node in result.nodes}
        trusted = next(
            edge for edge in result.edges if edge.relation is EdgeRelation.CHANGES
        )
        declared = AtlasEdge.create(
            trusted.relation,
            nodes_by_id[trusted.source_id],
            nodes_by_id[trusted.target_id],
            payload=trusted.to_dict()["payload"],
            schema_version=trusted.schema_version,
            provenance="declared",
            created_at=trusted.created_at,
        )
        graph = GraphQueryResult(
            result.nodes,
            tuple(
                declared if edge.edge_id == trusted.edge_id else edge
                for edge in result.edges
            ),
            False,
        )
        return {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Reject an externally downgraded extractor edge",
            "capacity": 2,
            "decomposition": "atlas_evidence",
            "source_kind": "task_episode_graph",
            "eligible": result.eligible,
            "graph": graph.to_dict(),
        }

    def task_episode_manifest(
        self,
        *,
        docs_path: str = "docs/core.py",
        supersedes: bool = False,
    ) -> dict[str, object]:
        results = (
            PythonRecipeExtractor().extract(
                _extractor_request(path="src/core.py", marker="build")
            ),
            PythonRecipeExtractor().extract(
                _extractor_request(path=docs_path, marker="docs")
            ),
        )
        self.assertTrue(all(result.eligible for result in results))
        nodes_by_id = {
            node.node_id: node for result in results for node in result.nodes
        }
        edges = [edge for result in results for edge in result.edges]
        if supersedes:
            recipe_build = nodes_by_id[results[0].manifest.recipe_id]
            recipe_docs = nodes_by_id[results[1].manifest.recipe_id]
            # External lineage-consumer input only: the extractor never emits it.
            edges.append(
                AtlasEdge.create(
                    EdgeRelation.SUPERSEDES,
                    recipe_docs,
                    recipe_build,
                    provenance="observed",
                )
            )
        graph = GraphQueryResult(
            tuple(sorted(nodes_by_id.values(), key=lambda node: node.node_id)),
            tuple(sorted(edges, key=lambda edge: edge.edge_id)),
            False,
        )
        return {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Derive bounded work from a TaskEpisode graph",
            "capacity": 2,
            "decomposition": "atlas_evidence",
            "source_kind": "task_episode_graph",
            "eligible": True,
            "graph": graph.to_dict(),
        }

    def test_fast_lane_ultra_auto_activation(self) -> None:
        helper = load_efficiency()

        activation = helper._fast_lane_activation("ultra", False)

        self.assertEqual(
            {"reasoning_effort": "ultra", "reason": "ultra_auto"},
            activation,
        )

    def test_fast_lane_lower_effort_requires_explicit_enable(self) -> None:
        helper = load_efficiency()

        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(
                    {"reasoning_effort": effort, "reason": "explicit_opt_in"},
                    helper._fast_lane_activation(effort, True),
                )

    def test_fast_lane_ultra_emits_lane_zero_and_three_useful_slots(self) -> None:
        helper = load_efficiency()

        result = self.compile_fast_lane(
            helper, self.fast_lane_schedule_request(helper), reasoning_effort="ultra"
        )

        self.assertEqual("active", result["status"])
        self.assertEqual("FAST_LANE_ACTIVE", result["decision_code"])
        self.assertEqual(
            {"reasoning_effort": "ultra", "reason": "ultra_auto"},
            result["activation"],
        )
        self.assertEqual("lane-0", result["main_lane"]["lane_id"])
        self.assertEqual("gpt-5.6-sol", result["main_lane"]["model"])
        self.assertEqual(3, result["subagent_capacity"])
        self.assertEqual(3, len(result["assignments"]))
        self.assertEqual(
            {"FAST-LANE-ROUTINE", "FAST-LANE-MODERATE", "FAST-LANE-FUTURE"},
            {item["task_id"] for item in result["assignments"]},
        )
        self.assertEqual(
            {"execution", "prewarm"},
            {item["role"] for item in result["assignments"]},
        )
        self.assertEqual(
            {
                ("FAST-LANE-ROUTINE", "gpt-5.6-terra", "high"),
                ("FAST-LANE-MODERATE", "gpt-5.6-terra", "max"),
                ("FAST-LANE-FUTURE", "gpt-5.6-terra", "high"),
            },
            {
                (item["task_id"], item["model"], item["reasoning_effort"])
                for item in result["assignments"]
            },
        )
        self.assertEqual([], result["idle_slots"])

    def test_fast_lane_public_explicit_opt_in_activates_lower_efforts(self) -> None:
        helper = load_efficiency()
        for effort in ("medium", "high", "max"):
            with self.subTest(effort=effort):
                result = self.compile_fast_lane(
                    helper,
                    self.fast_lane_schedule_request(helper),
                    reasoning_effort=effort,
                    enable=True,
                )

                self.assertEqual(
                    {"reasoning_effort": effort, "reason": "explicit_opt_in"},
                    result["activation"],
                )
                self.assertEqual("active", result["status"])
                self.assertEqual(effort, result["main_lane"]["reasoning_effort"])

    def test_fast_lane_routes_are_core_attested_and_legacy_display_is_unchanged(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")
        assignments = {item["task_id"]: item for item in result["assignments"]}
        self.assertEqual(
            "Terra High", assignments["FAST-LANE-ROUTINE"]["recommended_route"]
        )
        self.assertEqual(
            "Terra Max", assignments["FAST-LANE-MODERATE"]["recommended_route"]
        )
        self.assertEqual(
            ("gpt-5.6-terra", "high"),
            (
                assignments["FAST-LANE-ROUTINE"]["model"],
                assignments["FAST-LANE-ROUTINE"]["reasoning_effort"],
            ),
        )

        self.assertEqual(
            ("gpt-5.6-terra", "max"),
            (
                assignments["FAST-LANE-MODERATE"]["model"],
                assignments["FAST-LANE-MODERATE"]["reasoning_effort"],
            ),
        )
        for assignment in assignments.values():
            with self.subTest(task_id=assignment["task_id"]):
                receipt = assignment["dispatch_receipt"]
                self.assertNotEqual("ultra", assignment["reasoning_effort"])
                self.assertEqual(assignment["model"], receipt["model"])
                self.assertEqual(
                    assignment["reasoning_effort"],
                    receipt["reasoning_effort"],
                )
                self.assertEqual(
                    assignment["routing_result_hash"],
                    receipt["routing_result_hash"],
                )
                self.assertEqual(
                    assignment["task_id"],
                    receipt["routing_input"]["task_id"],
                )
                self.assertEqual(
                    assignment["role"],
                    receipt["routing_input"]["scheduler_role"],
                )

        exceptional_package = self.decomposition_manifest()
        exceptional_package["artifacts"] = [
            {
                "task_id": "FAST-LANE-EXCEPTIONAL",
                "goal": "Own exceptional architecture",
                "output_boundary": "exceptional design",
                "write_scope": ["src/fast_lane/exceptional.py"],
                "depends_on": [],
                "required_evidence": ["design-proof"],
                "complexity": "exceptional",
                "execution_contracts": ["contracts/fast-lane"],
            }
        ]
        result = self.compile_fast_lane(
            helper,
            self.fast_lane_request(helper, work_package=exceptional_package),
            reasoning_effort="ultra",
        )
        self.assertEqual("active", result["status"])
        exceptional = next(
            item
            for item in result["assignments"]
            if item["task_id"] == "FAST-LANE-EXCEPTIONAL"
        )
        self.assertEqual("execution", exceptional["role"])
        self.assertEqual("gpt-5.6-sol", exceptional["model"])
        self.assertEqual("high", exceptional["reasoning_effort"])
        self.assertNotEqual("ultra", exceptional["reasoning_effort"])

    def test_fast_lane_assignment_freezes_explicit_host_route(self) -> None:
        helper = load_efficiency()
        result = self.compile_fast_lane(
            helper, self.fast_lane_schedule_request(helper), reasoning_effort="ultra"
        )
        assignment = next(
            item for item in result["assignments"] if item["action"] == "start"
        )

        self.assertEqual(
            {
                "schema": "team-efficiency/fast-lane-host-dispatch-v1",
                "kind": "codex.spawn_agent",
                "model": assignment["model"],
                "reasoning_effort": assignment["reasoning_effort"],
                "inherit_current_session_model": False,
                "require_explicit_route": True,
                "missing_route_action": "reject",
            },
            assignment["host_dispatch"],
        )

    def test_fast_lane_assignment_prepares_one_bounded_index_context(self) -> None:
        helper = load_efficiency()
        result = self.compile_fast_lane(
            helper, self.fast_lane_schedule_request(helper), reasoning_effort="ultra"
        )
        assignment = next(
            item for item in result["assignments"] if item["action"] == "start"
        )
        index_context = assignment["index_context"]

        self.assertEqual(
            "team-efficiency/fast-lane-index-context-v1", index_context["schema"]
        )
        self.assertEqual("host_prepared", index_context["mode"])
        self.assertEqual(
            assignment["workspace_input_snapshot_id"],
            index_context["input_snapshot_id"],
        )
        self.assertEqual(assignment["write_scope"], index_context["scope"])
        self.assertEqual(
            assignment["task_node_ids"], index_context["anchors"]["task_node_ids"]
        )
        self.assertEqual(
            assignment["contract_node_ids"],
            index_context["anchors"]["contract_node_ids"],
        )
        self.assertEqual("query_once", index_context["input_query"]["action"])
        self.assertEqual("query_once", index_context["output_query"]["action"])
        self.assertEqual("consume_only", index_context["worker"]["action"])
        self.assertFalse(index_context["worker"]["mid_item_polling"])
        self.assertEqual(
            [
                "project_index_register",
                "project_index_sync",
                "project_index_status",
                "project_index_query",
            ],
            index_context["worker"]["prohibited_operations"],
        )
        self.assertEqual(
            index_context["context_hash"],
            helper._sha256_json(
                {
                    key: value
                    for key, value in index_context.items()
                    if key != "context_hash"
                }
            ),
        )

    def test_fast_lane_consumes_attested_core_routes_and_fails_closed(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        routes = host_status["routing_context"]["routes"]
        luna_entry = next(
            entry
            for entry in routes
            if entry["task_id"] == "FAST-LANE-ROUTINE"
            and entry["scheduler_role"] == "execution"
        )
        luna_entry["request"]["legacy"] = None
        luna_entry["compatibility_floor"] = None

        result = self.compile_fast_lane(
            helper,
            request,
            reasoning_effort="ultra",
            host_status=host_status,
        )

        routine = next(
            assignment
            for assignment in result["assignments"]
            if assignment["task_id"] == "FAST-LANE-ROUTINE"
        )
        self.assertEqual("gpt-5.6-luna", routine["model"])
        self.assertNotEqual("ultra", routine["reasoning_effort"])
        self.assertIn("routing_result_hash", routine["dispatch_receipt"])
        self.assertIn("routing_context_hash", routine["dispatch_receipt"])
        self.assertIn("task_fingerprint", routine["dispatch_receipt"])
        self.assertTrue(routine["routing_reason_codes"])

        unavailable_status = copy.deepcopy(host_status)
        for entry in unavailable_status["routing_context"]["routes"]:
            entry["request"]["host_capabilities"]["models"] = []
        unavailable = self.compile_fast_lane(
            helper,
            request,
            reasoning_effort="ultra",
            host_status=unavailable_status,
        )
        self.assertEqual("blocked", unavailable["status"])
        self.assertEqual("NO_SAFE_WORK", unavailable["decision_code"])
        self.assertEqual([], unavailable["assignments"])
        self.assertTrue(
            all(
                slot["reason_code"] == "CAPABILITY_UNAVAILABLE"
                for slot in unavailable["idle_slots"]
            )
        )

        missing_context = copy.deepcopy(host_status)
        missing_context["routing_context"] = None
        duplicate_context = copy.deepcopy(host_status)
        duplicate_context["routing_context"]["routes"].extend(
            copy.deepcopy(duplicate_context["routing_context"]["routes"])
        )
        unknown_context = copy.deepcopy(host_status)
        for entry in unknown_context["routing_context"]["routes"]:
            entry["task_id"] = "FAST-LANE-UNKNOWN"
        mismatched_context = copy.deepcopy(host_status)
        for entry in mismatched_context["routing_context"]["routes"]:
            entry["request"]["task"]["role"] = "integration"

        for name, invalid_status in (
            ("missing", missing_context),
            ("duplicate", duplicate_context),
            ("unknown", unknown_context),
            ("task_role_mismatch", mismatched_context),
        ):
            with self.subTest(name=name):
                failed_closed = self.compile_fast_lane(
                    helper,
                    request,
                    reasoning_effort="ultra",
                    host_status=invalid_status,
                )
                self.assertEqual("blocked", failed_closed["status"])
                self.assertEqual("NO_SAFE_WORK", failed_closed["decision_code"])
                self.assertEqual([], failed_closed["assignments"])

    def test_fast_lane_rejects_any_partial_invalid_routing_context_globally(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        route = next(
            entry
            for entry in host_status["routing_context"]["routes"]
            if entry["task_id"] == "FAST-LANE-ROUTINE"
            and entry["scheduler_role"] == "execution"
        )

        missing = copy.deepcopy(host_status)
        missing["routing_context"]["routes"] = [
            entry
            for entry in missing["routing_context"]["routes"]
            if not (
                entry["task_id"] == route["task_id"]
                and entry["scheduler_role"] == route["scheduler_role"]
            )
        ]
        duplicate = copy.deepcopy(host_status)
        duplicate["routing_context"]["routes"].append(copy.deepcopy(route))
        task_role_mismatch = copy.deepcopy(host_status)
        next(
            entry
            for entry in task_role_mismatch["routing_context"]["routes"]
            if entry["task_id"] == route["task_id"]
            and entry["scheduler_role"] == route["scheduler_role"]
        )["request"]["task"]["role"] = "integration"
        capability_unavailable = copy.deepcopy(host_status)
        next(
            entry
            for entry in capability_unavailable["routing_context"]["routes"]
            if entry["task_id"] == route["task_id"]
            and entry["scheduler_role"] == route["scheduler_role"]
        )["request"]["host_capabilities"]["models"] = []
        extra_unknown = copy.deepcopy(host_status)
        unknown_entry = copy.deepcopy(route)
        unknown_entry["task_id"] = "FAST-LANE-UNKNOWN"
        unknown_entry["request"]["task"]["task_id"] = "FAST-LANE-UNKNOWN"
        extra_unknown["routing_context"]["routes"].append(unknown_entry)

        for name, invalid_status in (
            ("missing", missing),
            ("duplicate", duplicate),
            ("task_role_mismatch", task_role_mismatch),
            ("capability_unavailable", capability_unavailable),
            ("extra_unknown", extra_unknown),
        ):
            with self.subTest(name=name):
                result = self.compile_fast_lane(
                    helper,
                    request,
                    reasoning_effort="ultra",
                    host_status=invalid_status,
                )
                self.assertEqual("blocked", result["status"])
                self.assertEqual("NO_SAFE_WORK", result["decision_code"])
                self.assertEqual([], result["assignments"])
                self.assertEqual([], result["ready_queue"])
                self.assertEqual([], result["review_queue"])
                self.assertEqual([], result["prewarm_queue"])
                self.assertEqual([], result["design_queue"])

    def test_fast_lane_fails_closed_for_an_unroutable_verification_action(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_code_atlas_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        validated = helper._validated_fast_lane_request(
            copy.deepcopy(request),
            host_routing_context=host_status["routing_context"],
        )
        verification = next(
            unit
            for unit in validated["source_plan"]["units"]
            if unit["unit_kind"] == "verification"
        )
        routing_context = copy.deepcopy(validated["routing_context"])
        route_key = (verification["task_id"], "verification")
        routing_context["decisions"][route_key] = None
        routing_context["reasons"][route_key] = "capability_unavailable"
        scheduler_state = {
            **validated["scheduler_state"],
            "phase": "integration_regression",
            "completed_tasks": [
                {"task_id": task_id} for task_id in verification["depends_on"]
            ],
        }

        result = helper._render_fast_lane_plan(
            {
                **validated,
                "scheduler_state": scheduler_state,
                "routing_context": routing_context,
            },
            helper._fast_lane_activation("ultra", False),
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("NO_SAFE_WORK", result["decision_code"])
        self.assertEqual([], result["assignments"])
        self.assertTrue(
            all(
                slot["reason_code"] == "CAPABILITY_UNAVAILABLE"
                for slot in result["idle_slots"]
            )
        )

    def test_fast_lane_routing_context_allows_the_bounded_remediation_unit(
        self,
    ) -> None:
        helper = load_efficiency()
        host_status = self.fast_lane_host_status(
            helper, self.fast_lane_schedule_request(helper)
        )
        routing_context = host_status["routing_context"]
        maximum_routes = (helper.MAX_MANIFEST_UNITS + 1) * len(helper._FAST_LANE_ROLES)
        routing_context["routes"] = [
            copy.deepcopy(routing_context["routes"][0]) for _ in range(maximum_routes)
        ]

        normalized = helper._validated_fast_lane_routing_context(routing_context)
        self.assertEqual(maximum_routes, len(normalized["routes"]))

        routing_context["routes"].append(copy.deepcopy(routing_context["routes"][0]))
        with self.assertRaises(ValueError):
            helper._validated_fast_lane_routing_context(routing_context)

    def test_fast_lane_blocks_lane_zero_and_scope_conflicts(self) -> None:
        helper = load_efficiency()
        lane0_request = self.fast_lane_schedule_request(helper)
        lane0_request["scheduler_state"]["lane0_state"] = {
            "active_task_id": "FAST-LANE-DESIGN",
            "owned_write_scopes": ["src/fast_lane"],
        }
        lane0_request["scheduler_state"]["source_plan_hash"] = helper._sha256_json(
            helper.decompose(lane0_request["work_package"])
        )
        lane0_request["read_contexts"] = []
        lane0_result = self.compile_fast_lane(
            helper, lane0_request, reasoning_effort="ultra"
        )
        self.assertEqual([], lane0_result["assignments"])
        self.assertTrue(
            all(
                item["reason_code"] == "LANE0_SCOPE_CONFLICT"
                for item in lane0_result["idle_slots"]
            )
        )

        conflict_package = self.decomposition_manifest()
        conflict_package["capacity"] = 3
        conflict_package["artifacts"] = [
            {
                "task_id": "FAST-LANE-PARENT",
                "goal": "Write a parent scope",
                "output_boundary": "parent writer",
                "write_scope": ["src/fast_lane"],
                "depends_on": [],
                "required_evidence": ["parent-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
            {
                "task_id": "FAST-LANE-CHILD",
                "goal": "Write a descendant scope",
                "output_boundary": "child writer",
                "write_scope": ["src/fast_lane/child.py"],
                "depends_on": [],
                "required_evidence": ["child-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
        ]
        conflict_result = self.compile_fast_lane(
            helper,
            self.fast_lane_request(helper, work_package=conflict_package),
            reasoning_effort="ultra",
        )
        writers = [
            item
            for item in conflict_result["assignments"]
            if item["role"] == "execution"
        ]
        self.assertLessEqual(len(writers), 1)
        self.assertTrue(
            any(
                item["reason_code"] == "WRITE_SCOPE_CONFLICT"
                for item in conflict_result["idle_slots"]
            )
            or len(writers) == 1
        )

    def test_fast_lane_refills_only_terminal_slot_and_retains_live_assignments(
        self,
    ) -> None:
        helper = load_efficiency()
        work_package = self.decomposition_manifest()
        work_package["capacity"] = 3
        work_package["artifacts"] = [
            {
                "task_id": task_id,
                "goal": f"Write {task_id}",
                "output_boundary": f"artifact {task_id}",
                "write_scope": [f"src/fast_lane/{suffix}.py"],
                "depends_on": [],
                "required_evidence": [f"{suffix}-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            }
            for task_id, suffix in (
                ("FAST-LANE-A", "a"),
                ("FAST-LANE-B", "b"),
                ("FAST-LANE-C", "c"),
            )
        ]
        request = self.fast_lane_request(helper, work_package=work_package)
        host_status = self.fast_lane_host_status(helper, request)
        validated = helper._validated_fast_lane_request(
            copy.deepcopy(request),
            host_routing_context=host_status["routing_context"],
        )
        units = {unit["task_id"]: unit for unit in validated["source_plan"]["units"]}

        def running_assignment(
            task_id: str, slot_id: str
        ) -> tuple[dict[str, object], dict[str, object]]:
            route = helper._fast_lane_route(
                validated["routing_context"], units[task_id], "execution"
            )
            self.assertIsNotNone(route)
            assignment = helper._fast_lane_assignment(
                validated,
                units[task_id],
                "execution",
                slot_id,
                route,
            )
            context = assignment.pop("_context")
            assignment.pop("action")
            return assignment, context

        retained, retained_context = running_assignment("FAST-LANE-A", "slot-1")
        terminal, terminal_context = running_assignment("FAST-LANE-B", "slot-2")
        terminal_result = self.fast_lane_terminal_result(helper, terminal)
        completed_record = self.fast_lane_completed_record(helper, terminal_result)
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "completed_tasks": [completed_record],
                "running_assignments": [retained],
                "dispatch_contexts": [retained_context, terminal_context],
                "slot_epochs": {"slot-1": 1, "slot-2": 1, "slot-3": 0},
            }
        )

        result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")

        retained = [
            item for item in result["assignments"] if item["task_id"] == "FAST-LANE-A"
        ]
        started = [item for item in result["assignments"] if item["action"] == "start"]
        self.assertEqual(1, len(retained))
        self.assertEqual("retain", retained[0]["action"])
        self.assertEqual(
            request["scheduler_state"]["running_assignments"][0]["assignment_token"],
            retained[0]["assignment_token"],
        )
        self.assertEqual(1, len(started))
        self.assertEqual("slot-2", started[0]["slot_id"])
        self.assertEqual(2, started[0]["assignment_epoch"])
        self.assertEqual("FAST-LANE-C", started[0]["task_id"])
        self.assertEqual(terminal["assignment_epoch"], 1)

    def test_fast_lane_host_slot_occupancy_excludes_nonrunning_states(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)
        assignment = copy.deepcopy(request["scheduler_state"]["running_assignments"][0])
        binding = {
            "workflow_id": "FASTLANE-20260730",
            "task_id": assignment["task_id"],
            "slot_id": assignment["slot_id"],
            "assignment_epoch": assignment["assignment_epoch"],
            "assignment_token": assignment["assignment_token"],
            "context_hash": assignment["context_hash"],
            "lease_epoch": 7,
            "endpoint": "/root/fastlane_task5_writer",
            "state": "running",
        }
        lease = {
            "task_id": assignment["task_id"],
            "lease_epoch": 7,
            "endpoint": "/root/fastlane_task5_writer",
            "state": "running",
        }

        for state in (
            "completed",
            "failed",
            "blocked",
            "expired",
            "interrupted",
            "pending_init",
        ):
            with self.subTest(state=state):
                inactive = {**binding, "state": state}
                audit = helper._fast_lane_host_slot_occupancy_audit(
                    workflow_id="FASTLANE-20260730",
                    source_plan_hash=request["scheduler_state"]["source_plan_hash"],
                    phase="execution",
                    running_assignments=[assignment],
                    host_bindings=[inactive],
                    current_leases=[lease],
                )
                self.assertEqual([], audit["active_slot_ids"])
                self.assertEqual(
                    ["slot-1", "slot-2", "slot-3"], audit["vacant_slot_ids"]
                )

    def test_fast_lane_host_slot_occupancy_requires_matching_lease_endpoint_and_assignment(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)
        assignment = copy.deepcopy(request["scheduler_state"]["running_assignments"][0])
        binding = {
            "workflow_id": "FASTLANE-20260730",
            "task_id": assignment["task_id"],
            "slot_id": assignment["slot_id"],
            "assignment_epoch": assignment["assignment_epoch"],
            "assignment_token": assignment["assignment_token"],
            "context_hash": assignment["context_hash"],
            "lease_epoch": 7,
            "endpoint": "/root/fastlane_task5_writer",
            "state": "running",
        }
        lease = {
            "task_id": assignment["task_id"],
            "lease_epoch": 7,
            "endpoint": "/root/fastlane_task5_writer",
            "state": "running",
        }

        invalid_bindings = (
            ("task_id", "FAST-LANE-FORGED"),
            ("slot_id", "slot-2"),
            ("assignment_epoch", 8),
            ("assignment_token", "sha256:" + "0" * 64),
            ("context_hash", "sha256:" + "1" * 64),
            ("lease_epoch", 8),
            ("endpoint", "/root/other_worker"),
        )
        for field, value in invalid_bindings:
            with self.subTest(field=field):
                invalid = {**binding, field: value}
                audit = helper._fast_lane_host_slot_occupancy_audit(
                    workflow_id="FASTLANE-20260730",
                    source_plan_hash=request["scheduler_state"]["source_plan_hash"],
                    phase="execution",
                    running_assignments=[assignment],
                    host_bindings=[invalid],
                    current_leases=[lease],
                )
                self.assertEqual([], audit["active_slot_ids"])
                self.assertIn("slot-1", audit["vacant_slot_ids"])

    def test_fast_lane_host_slot_audit_emits_deterministic_next_boundary_refill(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)
        assignment = copy.deepcopy(request["scheduler_state"]["running_assignments"][0])
        binding = {
            "workflow_id": "FASTLANE-20260730",
            "task_id": assignment["task_id"],
            "slot_id": assignment["slot_id"],
            "assignment_epoch": assignment["assignment_epoch"],
            "assignment_token": assignment["assignment_token"],
            "context_hash": assignment["context_hash"],
            "lease_epoch": 7,
            "endpoint": "/root/fastlane_task5_writer",
            "state": "running",
        }
        lease = {
            "task_id": assignment["task_id"],
            "lease_epoch": 7,
            "endpoint": "/root/fastlane_task5_writer",
            "state": "running",
        }

        first = helper._fast_lane_host_slot_occupancy_audit(
            workflow_id="FASTLANE-20260730",
            source_plan_hash=request["scheduler_state"]["source_plan_hash"],
            phase="execution",
            running_assignments=[assignment],
            host_bindings=[binding],
            current_leases=[lease],
        )
        second = helper._fast_lane_host_slot_occupancy_audit(
            workflow_id="FASTLANE-20260730",
            source_plan_hash=request["scheduler_state"]["source_plan_hash"],
            phase="execution",
            running_assignments=[assignment],
            host_bindings=[binding],
            current_leases=[lease],
        )
        self.assertEqual(first, second)
        self.assertEqual(["slot-1"], first["active_slot_ids"])
        self.assertEqual(["slot-2", "slot-3"], first["vacant_slot_ids"])
        self.assertEqual(
            {
                "schema": "team-efficiency/fast-lane-refill-trigger-v1",
                "source_plan_hash": request["scheduler_state"]["source_plan_hash"],
                "phase": "execution",
                "active_slot_ids": ["slot-1"],
                "vacant_slot_ids": ["slot-2", "slot-3"],
                "reason": "under_capacity_true_running_slots",
                "dispatch_at": "next_host_dispatch_boundary",
            },
            first["refill_trigger"],
        )
        self.assertEqual(
            helper._sha256_json(first["refill_trigger"]), first["refill_trigger_hash"]
        )

    def test_fast_lane_host_slot_audit_filters_stale_assignments_before_refill(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)
        assignment = request["scheduler_state"]["running_assignments"][0]
        host_status = {
            "workflow_id": "FASTLANE-20260730",
            "current_leases": [
                {
                    "task_id": assignment["task_id"],
                    "lease_epoch": 7,
                    "endpoint": "/root/fastlane_task5_writer",
                    "state": "completed",
                }
            ],
            "host_bindings": [
                {
                    "workflow_id": "FASTLANE-20260730",
                    "task_id": assignment["task_id"],
                    "slot_id": assignment["slot_id"],
                    "assignment_epoch": assignment["assignment_epoch"],
                    "assignment_token": assignment["assignment_token"],
                    "context_hash": assignment["context_hash"],
                    "lease_epoch": 7,
                    "endpoint": "/root/fastlane_task5_writer",
                    "state": "completed",
                }
            ],
        }

        result = self.compile_fast_lane(
            helper, request, reasoning_effort="ultra", host_status=host_status
        )

        self.assertEqual("active", result["status"])
        self.assertEqual("slot-1", result["assignments"][0]["slot_id"])
        self.assertEqual("start", result["assignments"][0]["action"])
        self.assertEqual(2, result["assignments"][0]["assignment_epoch"])
        occupancy = result["refill_plan"]["occupancy_audit"]
        self.assertEqual([], occupancy["active_slot_ids"])
        self.assertEqual(["slot-1", "slot-2", "slot-3"], occupancy["vacant_slot_ids"])

    def test_fast_lane_prewarm_critical_path_is_deterministic(self) -> None:
        helper = load_efficiency()
        code_units = {
            "FAST-LANE-CODE-A": {
                "task_id": "FAST-LANE-CODE-A",
                "depends_on": [],
                "unit_kind": "code",
            },
            "FAST-LANE-CODE-B": {
                "task_id": "FAST-LANE-CODE-B",
                "depends_on": [],
                "unit_kind": "code",
            },
            "FAST-LANE-CODE-C": {
                "task_id": "FAST-LANE-CODE-C",
                "depends_on": ["FAST-LANE-CODE-A", "FAST-LANE-CODE-B"],
                "unit_kind": "code",
            },
            "FAST-LANE-CODE-D": {
                "task_id": "FAST-LANE-CODE-D",
                "depends_on": ["FAST-LANE-CODE-A", "FAST-LANE-CODE-B"],
                "unit_kind": "code",
            },
        }
        code_source_plan = {
            "waves": [
                [
                    {"task_id": "FAST-LANE-CODE-A"},
                    {"task_id": "FAST-LANE-CODE-B"},
                ],
                [
                    {"task_id": "FAST-LANE-CODE-C"},
                    {"task_id": "FAST-LANE-CODE-D"},
                ],
            ]
        }
        self.assertEqual(
            1,
            helper._fast_lane_critical_path_distance(
                "FAST-LANE-CODE-C", code_units, frozenset(), {}
            ),
        )
        self.assertEqual(
            ["FAST-LANE-CODE-C", "FAST-LANE-CODE-D"],
            helper._fast_lane_preferred_prewarms(
                units=code_units,
                completed=frozenset(),
                running=frozenset(),
                candidate=frozenset(),
                reviewed=frozenset(),
                read_contexts={
                    ("FAST-LANE-CODE-C", "prewarm"),
                    ("FAST-LANE-CODE-D", "prewarm"),
                },
                source_plan=code_source_plan,
            ),
        )
        work_package = self.decomposition_manifest()
        work_package["capacity"] = 3
        work_package["artifacts"] = [
            {
                "task_id": "FAST-LANE-A",
                "goal": "Build the first parent",
                "output_boundary": "parent A",
                "write_scope": ["src/fast_lane/a.py"],
                "depends_on": [],
                "required_evidence": ["a-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
            {
                "task_id": "FAST-LANE-B",
                "goal": "Build the second parent",
                "output_boundary": "parent B",
                "write_scope": ["src/fast_lane/b.py"],
                "depends_on": [],
                "required_evidence": ["b-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
            {
                "task_id": "FAST-LANE-C",
                "goal": "Prewarm the first child",
                "output_boundary": "child C",
                "write_scope": ["src/fast_lane/c.py"],
                "depends_on": ["FAST-LANE-A", "FAST-LANE-B"],
                "required_evidence": ["c-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
            {
                "task_id": "FAST-LANE-D",
                "goal": "Prewarm the second child",
                "output_boundary": "child D",
                "write_scope": ["src/fast_lane/d.py"],
                "depends_on": ["FAST-LANE-A", "FAST-LANE-B"],
                "required_evidence": ["d-proof"],
                "complexity": "routine",
                "execution_contracts": ["contracts/fast-lane"],
            },
        ]
        request = self.fast_lane_request(helper, work_package=work_package)
        request["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                task_id=task_id,
                role="prewarm",
                worktree=self.fast_lane_task_root / "worktrees" / task_id.lower(),
                temp_target=self.fast_lane_task_root / "tasks" / task_id.lower(),
                read_scope=[f"src/fast_lane/{task_id[-1].lower()}.py"],
            )
            for task_id in ("FAST-LANE-C", "FAST-LANE-D")
        ]
        result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")
        prewarms = [item for item in result["assignments"] if item["role"] == "prewarm"]
        self.assertEqual(1, len(prewarms))
        self.assertEqual("FAST-LANE-C", prewarms[0]["task_id"])

        atlas_request = self.fast_lane_code_atlas_request(helper)
        atlas_source_plan = helper.decompose(atlas_request["work_package"])
        expected_prewarm_id = [
            unit["task_id"]
            for unit in atlas_source_plan["units"]
            if unit["unit_kind"] == "code"
        ][2]
        verification_id = next(
            unit["task_id"]
            for unit in atlas_source_plan["units"]
            if unit["unit_kind"] == "verification"
        )
        atlas_result = self.compile_fast_lane(
            helper, atlas_request, reasoning_effort="ultra"
        )
        atlas_prewarms = [
            item for item in atlas_result["assignments"] if item["role"] == "prewarm"
        ]
        self.assertEqual(
            [expected_prewarm_id], [item["task_id"] for item in atlas_prewarms]
        )
        self.assertEqual(
            [verification_id],
            atlas_result["terminal_protocol"]["verification_unit_task_ids"],
        )
        self.assertEqual(
            [
                unit["task_id"]
                for unit in atlas_source_plan["units"]
                if unit["unit_kind"] == "code"
            ][3:],
            [item["task_id"] for item in atlas_result["ready_queue"]],
        )
        self.assertTrue(
            all("slot_id" not in item for item in atlas_result["ready_queue"])
        )

    def test_fast_lane_ultra_context_ineligible_is_blocked(self) -> None:
        helper = load_efficiency()

        result = self.compile_fast_lane(
            helper,
            self.fast_lane_contexts_empty_request(helper),
            reasoning_effort="ultra",
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("NO_SAFE_WORK", result["decision_code"])
        self.assertEqual("ultra_auto", result["activation"]["reason"])
        self.assertEqual([], result["assignments"])

    def test_fast_lane_lower_effort_without_enable_is_exactly_inactive(self) -> None:
        helper = load_efficiency()

        result = self.compile_fast_lane(
            helper,
            self.fast_lane_contexts_empty_request(helper),
            reasoning_effort="max",
            enable=False,
        )

        self.assertEqual("inactive", result["status"])
        self.assertEqual("EXPLICIT_OPT_IN_REQUIRED", result["decision_code"])
        self.assertEqual([], result["assignments"])
        self.assertEqual(
            {"slot-1", "slot-2", "slot-3"},
            {item["slot_id"] for item in result["idle_slots"]},
        )
        self.assertTrue(
            all(
                item["reason_code"] == "OPT_IN_REQUIRED"
                for item in result["idle_slots"]
            )
        )

    def test_fast_lane_cli_effort_errors_are_stable(self) -> None:
        helper = load_efficiency()
        request_path = self.temp / "fast-lane-request.json"
        request_path.write_text(
            json.dumps(self.fast_lane_contexts_empty_request(helper)),
            encoding="utf-8",
        )
        embedded_effort_path = self.temp / "fast-lane-embedded-effort.json"
        embedded_effort = self.fast_lane_contexts_empty_request(helper)
        embedded_effort["reasoning_effort"] = "ultra"
        embedded_effort_path.write_text(json.dumps(embedded_effort), encoding="utf-8")

        cases = (
            ("missing", ["fast-lane", "--input", str(request_path)]),
            (
                "unknown",
                [
                    "fast-lane",
                    "--input",
                    str(request_path),
                    "--reasoning-effort",
                    "unsupported",
                ],
            ),
            (
                "duplicate",
                [
                    "fast-lane",
                    "--input",
                    str(request_path),
                    "--reasoning-effort",
                    "ultra",
                    "--reasoning-effort",
                    "max",
                ],
            ),
            (
                "embedded",
                [
                    "fast-lane",
                    "--input",
                    str(embedded_effort_path),
                    "--reasoning-effort",
                    "ultra",
                ],
            ),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                exit_code, output, errors = self.run_fast_lane_cli(helper, arguments)
                self.assertEqual(2, exit_code)
                self.assertEqual("", output)
                self.assertTrue(errors.startswith("error: "))
                self.assertLessEqual(len(errors), 256)

    def test_fast_lane_cli_uses_explicit_dispatch_not_decompose_fallback(self) -> None:
        helper = load_efficiency()
        request_path = self.temp / "fast-lane-dispatch.json"
        request_path.write_text(
            json.dumps(self.fast_lane_contexts_empty_request(helper)),
            encoding="utf-8",
        )

        exit_code, output, errors = self.run_fast_lane_cli(
            helper,
            [
                "fast-lane",
                "--input",
                str(request_path),
                "--reasoning-effort",
                "ultra",
            ],
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("", errors)
        result = json.loads(output)
        self.assertEqual("team-efficiency/fast-lane-plan-v1", result["schema"])
        self.assertEqual("blocked", result["status"])
        self.assertEqual("NO_SAFE_WORK", result["decision_code"])

    def test_fast_lane_cli_consumes_complete_bounded_host_status(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        request_path = self.temp / "fast-lane-attested-request.json"
        host_status_path = self.temp / "fast-lane-host-status.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        host_status_text = json.dumps(host_status)
        self.assertGreater(len(host_status_text.encode("utf-8")), 32 * 1024)
        host_status_path.write_text(host_status_text, encoding="utf-8")

        exit_code, output, errors = self.run_fast_lane_cli(
            helper,
            [
                "fast-lane",
                "--input",
                str(request_path),
                "--host-status",
                str(host_status_path),
                "--reasoning-effort",
                "ultra",
            ],
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("", errors)
        result = json.loads(output)
        self.assertEqual("active", result["status"])
        self.assertTrue(result["assignments"])
        self.assertTrue(
            all(
                assignment["reasoning_effort"] != "ultra"
                for assignment in result["assignments"]
            )
        )

        malformed_status = copy.deepcopy(host_status)
        malformed_status.pop("routing_context")
        malformed_path = self.temp / "fast-lane-malformed-host-status.json"
        malformed_path.write_text(json.dumps(malformed_status), encoding="utf-8")
        exit_code, output, errors = self.run_fast_lane_cli(
            helper,
            [
                "fast-lane",
                "--input",
                str(request_path),
                "--host-status",
                str(malformed_path),
                "--reasoning-effort",
                "ultra",
            ],
        )
        self.assertEqual(2, exit_code)
        self.assertEqual("", output)
        self.assertTrue(errors.startswith("error: "))

    def test_fast_lane_cli_fails_closed_for_partial_or_unknown_routes(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        request_path = self.temp / "fast-lane-global-fail-closed-request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        route = next(
            entry
            for entry in host_status["routing_context"]["routes"]
            if entry["task_id"] == "FAST-LANE-ROUTINE"
            and entry["scheduler_role"] == "execution"
        )

        partial = copy.deepcopy(host_status)
        partial["routing_context"]["routes"] = [
            entry
            for entry in partial["routing_context"]["routes"]
            if not (
                entry["task_id"] == route["task_id"]
                and entry["scheduler_role"] == route["scheduler_role"]
            )
        ]
        unknown = copy.deepcopy(host_status)
        unknown_entry = copy.deepcopy(route)
        unknown_entry["task_id"] = "FAST-LANE-UNKNOWN"
        unknown_entry["request"]["task"]["task_id"] = "FAST-LANE-UNKNOWN"
        unknown["routing_context"]["routes"].append(unknown_entry)

        for name, invalid_status in (("partial", partial), ("unknown", unknown)):
            with self.subTest(name=name):
                status_path = self.temp / f"fast-lane-{name}-host-status.json"
                status_path.write_text(json.dumps(invalid_status), encoding="utf-8")
                exit_code, output, errors = self.run_fast_lane_cli(
                    helper,
                    [
                        "fast-lane",
                        "--input",
                        str(request_path),
                        "--host-status",
                        str(status_path),
                        "--reasoning-effort",
                        "ultra",
                    ],
                )

                self.assertEqual(0, exit_code)
                self.assertEqual("", errors)
                result = json.loads(output)
                self.assertEqual("blocked", result["status"])
                self.assertEqual("NO_SAFE_WORK", result["decision_code"])
                self.assertEqual([], result["assignments"])

    def test_fast_lane_top_level_fields_are_exact(self) -> None:
        helper = load_efficiency()
        expected_fields = {
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
        needs_design_request = self.fast_lane_contexts_empty_request(helper)
        needs_design_request["work_package"] = {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Choose architecture before implementation",
            "capacity": 2,
            "decomposition": "semantic",
        }
        cases = (
            (
                "inactive",
                self.compile_fast_lane(
                    helper,
                    self.fast_lane_contexts_empty_request(helper),
                    reasoning_effort="max",
                    enable=False,
                ),
                "inactive",
                "EXPLICIT_OPT_IN_REQUIRED",
                "OPT_IN_REQUIRED",
                None,
                None,
            ),
            (
                "blocked",
                self.compile_fast_lane(
                    helper,
                    self.fast_lane_contexts_empty_request(helper),
                    reasoning_effort="ultra",
                ),
                "blocked",
                "NO_SAFE_WORK",
                "EXECUTION_CONTEXT_MISSING",
                "ultra_auto",
                None,
            ),
            (
                "needs_design",
                self.compile_fast_lane(
                    helper,
                    needs_design_request,
                    reasoning_effort="ultra",
                ),
                "needs_design",
                "WORK_PACKAGE_NEEDS_DESIGN",
                "WORK_PACKAGE_NEEDS_DESIGN",
                "ultra_auto",
                "design_required",
            ),
        )
        for (
            name,
            result,
            status,
            decision_code,
            idle_reason,
            activation_reason,
            next_action,
        ) in cases:
            with self.subTest(name=name):
                self.assertEqual(expected_fields, set(result))
                self.assertEqual(status, result["status"])
                self.assertEqual(decision_code, result["decision_code"])
                self.assertEqual(
                    {"reasoning_effort", "reason"}, set(result["activation"])
                )
                self.assertEqual(activation_reason, result["activation"]["reason"])
                self.assertEqual(
                    {
                        "lane_id",
                        "model",
                        "reasoning_effort",
                        "next_action",
                        "design_owner",
                        "parallel_design",
                        "owned_write_scopes",
                        "excluded_write_scopes",
                    },
                    set(result["main_lane"]),
                )
                self.assertEqual(
                    {
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "ultra",
                        "max_concurrent": 1,
                    },
                    result["main_lane"]["parallel_design"],
                )
                self.assertEqual(next_action, result["main_lane"]["next_action"])
                self.assertEqual([], result["assignments"])
                self.assertEqual([], result["ready_queue"])
                self.assertEqual([], result["review_queue"])
                self.assertEqual([], result["prewarm_queue"])
                self.assertEqual([], result["design_queue"])
                self.assertEqual(
                    {
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
                    },
                    {
                        field: result["refill_plan"][field]
                        for field in (
                            "trigger",
                            "dispatch_at",
                            "priority",
                            "polling",
                        )
                    },
                )
                self.assertEqual(
                    {
                        "trigger",
                        "dispatch_at",
                        "priority",
                        "polling",
                        "occupancy_audit",
                    },
                    set(result["refill_plan"]),
                )
                occupancy = result["refill_plan"]["occupancy_audit"]
                self.assertEqual(
                    {
                        "active_slot_ids",
                        "vacant_slot_ids",
                        "refill_trigger",
                        "refill_trigger_hash",
                    },
                    set(occupancy),
                )
                self.assertEqual([], occupancy["active_slot_ids"])
                self.assertEqual(
                    ["slot-1", "slot-2", "slot-3"],
                    occupancy["vacant_slot_ids"],
                )
                self.assertEqual(
                    {
                        "owner",
                        "compiler_schedules_declared_verification_units",
                        "compiler_schedules_ad_hoc_terminal_slots",
                        "verification_unit_task_ids",
                        "integration_regression_passes",
                        "blocker_reviews",
                        "global_targeted_remediation_rounds",
                        "wide_or_shared_scope_remediation",
                    },
                    set(result["terminal_protocol"]),
                )
                self.assertEqual(
                    "lane0_and_work_methodology_skill",
                    result["terminal_protocol"]["owner"],
                )
                self.assertTrue(
                    result["terminal_protocol"][
                        "compiler_schedules_declared_verification_units"
                    ]
                )
                self.assertFalse(
                    result["terminal_protocol"][
                        "compiler_schedules_ad_hoc_terminal_slots"
                    ]
                )
                self.assertEqual(
                    1, result["terminal_protocol"]["integration_regression_passes"]
                )
                self.assertEqual(1, result["terminal_protocol"]["blocker_reviews"])
                self.assertEqual(
                    1,
                    result["terminal_protocol"]["global_targeted_remediation_rounds"],
                )
                self.assertEqual(
                    "stop_for_lane0",
                    result["terminal_protocol"]["wide_or_shared_scope_remediation"],
                )
                self.assertEqual(
                    {
                        "owner",
                        "boundary_operations",
                        "conditional_operations",
                        "operation_set_is_closed_capability_list",
                        "mid_item_status_polling",
                        "recovery_status_reads",
                        "release_tool_available",
                        "index_protocol",
                        "dispatch_protocol",
                        "cross_session_protocol",
                    },
                    set(result["workflow_policy"]),
                )
                self.assertEqual(
                    "work_methodology_skill", result["workflow_policy"]["owner"]
                )
                self.assertEqual(
                    [
                        "strict_writer_start",
                        "strict_writer_execution_and_completion_preparation",
                        "strict_writer_completion",
                        "read_only_verification_lifecycle",
                        "lease_recovery_without_bound_output",
                        "lease_recovery_with_valid_bound_output",
                    ],
                    [
                        item["boundary"]
                        for item in result["workflow_policy"]["boundary_operations"]
                    ],
                )
                self.assertEqual(
                    [
                        {
                            "condition": "claim_host_target_unavailable_or_rebind_required",
                            "operation": "workflow_endpoint_bind",
                        }
                    ],
                    result["workflow_policy"]["conditional_operations"],
                )
                self.assertFalse(
                    result["workflow_policy"]["operation_set_is_closed_capability_list"]
                )
                self.assertFalse(result["workflow_policy"]["mid_item_status_polling"])
                self.assertEqual(
                    "start_or_recovery_boundary_only",
                    result["workflow_policy"]["recovery_status_reads"],
                )
                self.assertFalse(result["workflow_policy"]["release_tool_available"])
                self.assertEqual(
                    [
                        {"slot_id": "slot-1", "reason_code": idle_reason},
                        {"slot_id": "slot-2", "reason_code": idle_reason},
                        {"slot_id": "slot-3", "reason_code": idle_reason},
                    ],
                    result["idle_slots"],
                )
                self.assertEqual(
                    {"slot-1", "slot-2", "slot-3"},
                    {item["slot_id"] for item in result["idle_slots"]},
                )
                self.assertEqual(
                    result["plan_hash"],
                    helper._sha256_json(
                        {
                            key: value
                            for key, value in result.items()
                            if key != "plan_hash"
                        }
                    ),
                )
                self.assertLessEqual(
                    len(helper._json_bytes(result)), helper.MAX_MANIFEST_BYTES
                )
        self.assertEqual(
            "design_required",
            cases[2][1]["main_lane"]["next_action"],
        )

    def test_fast_lane_rejects_non_boolean_enable(self) -> None:
        helper = load_efficiency()

        for invalid_enable in (1, 0, None, "true"):
            with self.subTest(enable=repr(invalid_enable)):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(
                        helper,
                        self.fast_lane_contexts_empty_request(helper),
                        reasoning_effort="max",
                        enable=invalid_enable,
                    )

    def test_fast_lane_request_rejects_noncanonical_or_oversized_values(self) -> None:
        helper = load_efficiency()
        noncanonical = self.fast_lane_contexts_empty_request(helper)
        noncanonical["scheduler_state"]["source_plan_hash"] = float("nan")
        oversized = self.fast_lane_contexts_empty_request(helper)
        oversized["work_package"]["goal"] = "x" * (helper.MAX_MANIFEST_INPUT_BYTES + 1)
        self.assertGreater(
            len(helper._json_bytes(oversized)), helper.MAX_MANIFEST_INPUT_BYTES
        )

        for name, request in (
            ("nan", noncanonical),
            ("oversized", oversized),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, request, reasoning_effort="ultra")

    def test_fast_lane_request_shell_requires_exact_top_level_fields(self) -> None:
        helper = load_efficiency()
        missing_read_contexts = self.fast_lane_contexts_empty_request(helper)
        del missing_read_contexts["read_contexts"]
        unexpected_field = self.fast_lane_contexts_empty_request(helper)
        unexpected_field["unexpected"] = True

        for name, request in (
            ("missing_read_contexts", missing_read_contexts),
            ("unexpected", unexpected_field),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, request, reasoning_effort="ultra")

    def test_fast_lane_rejects_nontext_phase_in_api_and_cli(self) -> None:
        helper = load_efficiency()

        for name, invalid_phase in (("list", []), ("object", {})):
            request = self.fast_lane_contexts_empty_request(helper)
            request["scheduler_state"]["phase"] = invalid_phase
            with self.subTest(api=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, request, reasoning_effort="ultra")

            request_path = self.temp / f"fast-lane-invalid-phase-{name}.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            with self.subTest(cli=name):
                exit_code, output, errors = self.run_fast_lane_cli(
                    helper,
                    [
                        "fast-lane",
                        "--input",
                        str(request_path),
                        "--reasoning-effort",
                        "ultra",
                    ],
                )
                self.assertEqual(2, exit_code)
                self.assertEqual("", output)
                self.assertTrue(errors.startswith("error: "))

    def test_fast_lane_validates_exact_target_gates_and_driver_identity(self) -> None:
        helper = load_efficiency()
        expected_gate_fields = {
            "gate_id",
            "argv",
            "red_expected_exit_codes",
            "green_expected_exit_code",
            "timeout_seconds",
            "red_failure_ids",
            "red_failure_fingerprint",
            "acceptance_constraint_hashes",
        }
        expected_target_fields = {"task_id", "driver_gate_id", "gates"}
        request = self.fast_lane_contexts_empty_request(helper)
        target = request["target_gates"][0]
        target["driver_gate_id"] = "focused"
        target["gates"] = [self.fast_lane_gate()]

        with self.subTest(case="valid_driver_is_normalized"):
            validated = helper._validated_fast_lane_request(request)
            normalized_target = validated["target_gates"][0]
            self.assertEqual(expected_target_fields, set(normalized_target))
            self.assertEqual(expected_gate_fields, set(normalized_target["gates"][0]))
            self.assertEqual(
                request["target_gates"][0]["gates"][0]["red_failure_fingerprint"],
                normalized_target["gates"][0]["red_failure_fingerprint"],
            )

        invalid_requests: list[tuple[str, dict[str, object]]] = []
        missing_key = copy.deepcopy(request)
        del missing_key["target_gates"][0]["gates"][0]["timeout_seconds"]
        invalid_requests.append(("missing_gate_key", missing_key))

        extra_key = copy.deepcopy(request)
        extra_key["target_gates"][0]["gates"][0]["unexpected"] = True
        invalid_requests.append(("extra_gate_key", extra_key))

        missing_target_key = copy.deepcopy(request)
        del missing_target_key["target_gates"][0]["driver_gate_id"]
        invalid_requests.append(("missing_target_key", missing_target_key))

        extra_target_key = copy.deepcopy(request)
        extra_target_key["target_gates"][0]["unexpected"] = True
        invalid_requests.append(("extra_target_key", extra_target_key))

        no_driver = copy.deepcopy(request)
        no_driver["target_gates"][0]["driver_gate_id"] = None
        invalid_requests.append(("no_driver", no_driver))

        duplicate_task_record = copy.deepcopy(request)
        duplicate_task_record["target_gates"].append(
            copy.deepcopy(duplicate_task_record["target_gates"][0])
        )
        invalid_requests.append(("duplicate_task_record", duplicate_task_record))

        unknown_driver_gate = copy.deepcopy(request)
        unknown_driver_gate["target_gates"][0]["driver_gate_id"] = "missing"
        invalid_requests.append(("unknown_driver_gate", unknown_driver_gate))

        zero_red_code = copy.deepcopy(request)
        zero_red_code["target_gates"][0]["gates"][0]["red_expected_exit_codes"] = [0]
        invalid_requests.append(("zero_red_code", zero_red_code))

        mismatched_fingerprint = copy.deepcopy(request)
        mismatched_fingerprint["target_gates"][0]["gates"][0][
            "red_failure_fingerprint"
        ] = "sha256:" + ("0" * 64)
        invalid_requests.append(("mismatched_fingerprint", mismatched_fingerprint))

        for name, invalid_request in invalid_requests:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(
                        helper, invalid_request, reasoning_effort="ultra"
                    )

        atlas_manifest = self.code_atlas_manifest()
        atlas_plan = helper.decompose(atlas_manifest)
        atlas_test = atlas_manifest["packet"]["tests"][0]
        verification_request = self.fast_lane_contexts_empty_request(helper)
        verification_request["work_package"] = atlas_manifest
        verification_request["target_gates"] = []
        for unit in atlas_plan["units"]:
            is_verification = unit["unit_kind"] == "verification"
            gate = self.fast_lane_gate(
                argv=atlas_test["argv"],
                red_expected_exit_codes=[] if is_verification else [1],
                green_exit_code=atlas_test["expected_exit_code"],
                red_failure_ids=[] if is_verification else None,
                acceptance_constraint_hashes=list(unit["acceptance_constraints"]),
            )
            verification_request["target_gates"].append(
                {
                    "task_id": unit["task_id"],
                    "driver_gate_id": None if is_verification else "focused",
                    "gates": [gate],
                }
            )
        with self.subTest(case="verification_red_fields_empty_is_valid"):
            baseline = self.compile_fast_lane(
                helper, copy.deepcopy(verification_request), reasoning_effort="ultra"
            )
            self.assertEqual("team-efficiency/fast-lane-plan-v1", baseline["schema"])

        invalid_verification_request = copy.deepcopy(verification_request)
        verification_target = next(
            target
            for target in invalid_verification_request["target_gates"]
            if target["driver_gate_id"] is None
        )
        verification_target["gates"][0]["red_expected_exit_codes"] = [1]
        verification_target["gates"][0]["red_failure_ids"] = [
            "tests.test_team_efficiency.TeamEfficiencyTests.test_verification"
        ]
        verification_target["gates"][0]["red_failure_fingerprint"] = (
            self.fast_lane_gate(
                red_failure_ids=verification_target["gates"][0]["red_failure_ids"]
            )["red_failure_fingerprint"]
        )
        with self.subTest(case="verification_red_fields_must_be_empty"):
            with self.assertRaises(ValueError):
                self.compile_fast_lane(
                    helper, invalid_verification_request, reasoning_effort="ultra"
                )

    def test_fast_lane_rejects_unsafe_manual_gate_inputs(self) -> None:
        helper = load_efficiency()

        def local_red_fingerprint(gate_id: str, failure_ids: list[str]) -> str:
            payload = json.dumps(
                {
                    "schema": "team-efficiency/red-failure-identity-v1",
                    "gate_id": gate_id,
                    "failure_ids": sorted(failure_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return "sha256:" + hashlib.sha256(payload).hexdigest()

        absolute_option = self.fast_lane_contexts_empty_request(helper)
        absolute_option["target_gates"][0]["gates"][0]["argv"].append(
            "--file=/etc/passwd"
        )
        with self.subTest(case="argv_embedded_posix_absolute"):
            with self.assertRaises(ValueError):
                self.compile_fast_lane(
                    helper, absolute_option, reasoning_effort="ultra"
                )

        for name, failure_id in (
            ("secret", "secret=token"),
            ("absolute", "/etc/passwd"),
            ("traversal", "../outside"),
        ):
            with self.subTest(case=name):
                request = self.fast_lane_contexts_empty_request(helper)
                gate = request["target_gates"][0]["gates"][0]
                gate["red_failure_ids"] = [failure_id]
                gate["red_failure_fingerprint"] = local_red_fingerprint(
                    gate["gate_id"], gate["red_failure_ids"]
                )
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, request, reasoning_effort="ultra")

    def test_fast_lane_rejects_ambiguous_packet_gate_coverage(self) -> None:
        helper = load_efficiency()

        def local_hash(value: object) -> str:
            payload = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return "sha256:" + hashlib.sha256(payload).hexdigest()

        manifest = self.code_atlas_manifest()
        packet = manifest["packet"]
        packet["tests"].append(
            {
                "argv": ["python", "-m", "pytest", "tests/test_secondary.py"],
                "expected_exit_code": 0,
            }
        )
        packet_identity = copy.deepcopy(packet)
        del packet_identity["packet_id"]
        packet["packet_id"] = local_hash(packet_identity)
        plan = helper.decompose(manifest)
        test_hashes = [
            local_hash(
                {
                    "argv": test_spec["argv"],
                    "expected_exit_code": test_spec["expected_exit_code"],
                }
            )
            for test_spec in packet["tests"]
        ]
        request = self.fast_lane_contexts_empty_request(helper)
        request["work_package"] = manifest
        request["target_gates"] = []
        for unit in plan["units"]:
            is_verification = unit["unit_kind"] == "verification"
            gates = []
            for gate_id, test_spec, test_hash in zip(
                ("focused", "coverage"), packet["tests"], test_hashes
            ):
                gates.append(
                    self.fast_lane_gate(
                        gate_id=gate_id,
                        argv=list(test_spec["argv"]),
                        red_expected_exit_codes=[] if is_verification else [1],
                        green_exit_code=test_spec["expected_exit_code"],
                        red_failure_ids=[] if is_verification else None,
                        acceptance_constraint_hashes=[test_hash],
                    )
                )
            request["target_gates"].append(
                {
                    "task_id": unit["task_id"],
                    "driver_gate_id": None if is_verification else "focused",
                    "gates": gates,
                }
            )

        with self.subTest(case="two_test_specs_have_exact_two_gate_baseline"):
            result = self.compile_fast_lane(
                helper, copy.deepcopy(request), reasoning_effort="ultra"
            )
            self.assertEqual("blocked", result["status"])

        overclaimed = copy.deepcopy(request)
        overclaimed_first_gate = copy.deepcopy(
            overclaimed["target_gates"][0]["gates"][0]
        )
        overclaimed_first_gate["acceptance_constraint_hashes"] = sorted(test_hashes)
        overclaimed["target_gates"][0]["gates"] = [overclaimed_first_gate]
        missing_coverage = copy.deepcopy(request)
        missing_coverage["target_gates"][0]["gates"][1][
            "acceptance_constraint_hashes"
        ] = []
        command_mismatch = copy.deepcopy(request)
        command_mismatch["target_gates"][0]["gates"][0]["argv"] = [
            "python",
            "-m",
            "pytest",
            "tests/not_verified.py",
        ]
        for name, invalid_request in (
            ("single_gate_overclaims_two_constraints", overclaimed),
            ("missing_coverage", missing_coverage),
            ("command_mismatch", command_mismatch),
        ):
            with self.subTest(case=name):
                with self.assertRaisesRegex(ValueError, r"^ATLAS_GATE_UNVERIFIED$"):
                    self.compile_fast_lane(
                        helper, invalid_request, reasoning_effort="ultra"
                    )

        request_path = self.temp / "fast-lane-atlas-gate-regression.json"
        request_path.write_text(json.dumps(overclaimed), encoding="utf-8")
        exit_code, output, errors = self.run_fast_lane_cli(
            helper,
            [
                "fast-lane",
                "--input",
                str(request_path),
                "--reasoning-effort",
                "ultra",
            ],
        )
        self.assertEqual(2, exit_code)
        self.assertEqual("", output)
        self.assertEqual("error: ATLAS_GATE_UNVERIFIED\n", errors)

    def test_fast_lane_rejects_unsafe_gates_and_unbound_contexts(self) -> None:
        helper = load_efficiency()
        baseline = self.fully_bound_fast_lane_manual_request(helper)
        for context in baseline["execution_contexts"]:
            self.assertEqual(
                {"task_id", "bootstrap_plan", "workspace_input_snapshot_id"},
                set(context),
            )
            self.assertEqual(
                self.fast_lane_execution_snapshot_id(helper, context["task_id"]),
                context["workspace_input_snapshot_id"],
            )
        baseline_result = self.compile_fast_lane(
            helper, copy.deepcopy(baseline), reasoning_effort="ultra"
        )
        self.assertEqual("team-efficiency/fast-lane-plan-v1", baseline_result["schema"])

        unsafe_argv_cases = (
            ("cmd_wrapper", ["cmd", "/c", "echo unsafe"]),
            ("sh_wrapper", ["sh", "-c", "echo unsafe"]),
            ("windows_absolute", ["python", r"C:\Windows\System32\cmd.exe"]),
            ("posix_absolute", ["python", "/etc/passwd"]),
            ("traversal", ["python", "../outside"]),
            ("embedded_posix_absolute", ["python", "--file=/etc/passwd"]),
            ("secret_marker", ["python", "secret=token"]),
            ("empty_component", ["python", ""]),
            ("empty", []),
            ("too_many", ["python"] * 33),
            ("too_long", ["python", "x" * 257]),
        )
        for name, argv in unsafe_argv_cases:
            with self.subTest(case=name):
                request = copy.deepcopy(baseline)
                request["target_gates"][0]["gates"][0]["argv"] = argv
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, request, reasoning_effort="ultra")

        source_plan = helper.decompose(baseline["work_package"])
        first_unit, second_unit = source_plan["units"][:2]
        base_commit = baseline["scheduler_state"]["integration_state"]["commit"]
        first_context = baseline["execution_contexts"][0]
        context_cases: list[tuple[str, dict[str, object]]] = []

        task_mismatch = copy.deepcopy(baseline)
        task_mismatch["execution_contexts"][0] = self.fast_lane_execution_context(
            helper,
            task_id=first_unit["task_id"],
            bootstrap_task_id=second_unit["task_id"],
            base_commit=base_commit,
            write_scope=list(first_unit["write_scope"]),
        )
        context_cases.append(("task_mismatch", task_mismatch))

        base_mismatch = copy.deepcopy(baseline)
        base_mismatch["execution_contexts"][0] = self.fast_lane_execution_context(
            helper,
            task_id=first_unit["task_id"],
            base_commit="b" * 40,
            write_scope=list(first_unit["write_scope"]),
        )
        context_cases.append(("base_mismatch", base_mismatch))

        scope_mismatch = copy.deepcopy(baseline)
        scope_mismatch["execution_contexts"][0] = self.fast_lane_execution_context(
            helper,
            task_id=first_unit["task_id"],
            base_commit=base_commit,
            write_scope=["mcp-tools/devkit_fastlane/references/context-unrelated.md"],
        )
        context_cases.append(("scope_mismatch", scope_mismatch))

        duplicate_branch = copy.deepcopy(baseline)
        duplicate_branch["execution_contexts"][1] = self.fast_lane_execution_context(
            helper,
            task_id=second_unit["task_id"],
            base_commit=base_commit,
            write_scope=list(second_unit["write_scope"]),
            branch=first_context["bootstrap_plan"]["branch"],
        )
        context_cases.append(("duplicate_branch", duplicate_branch))

        duplicate_worktree = copy.deepcopy(baseline)
        duplicate_worktree["execution_contexts"][1] = self.fast_lane_execution_context(
            helper,
            task_id=second_unit["task_id"],
            base_commit=base_commit,
            write_scope=list(second_unit["write_scope"]),
            worktree=first_context["bootstrap_plan"]["worktree"],
        )
        context_cases.append(("duplicate_worktree", duplicate_worktree))

        duplicate_temp = copy.deepcopy(baseline)
        duplicate_temp["execution_contexts"][1] = self.fast_lane_execution_context(
            helper,
            task_id=second_unit["task_id"],
            base_commit=base_commit,
            write_scope=list(second_unit["write_scope"]),
            temp_target=first_context["bootstrap_plan"]["temp_target"],
        )
        context_cases.append(("duplicate_temp", duplicate_temp))

        missing_execution_binding = copy.deepcopy(baseline)
        del missing_execution_binding["execution_contexts"][0]
        context_cases.append(("missing_execution_binding", missing_execution_binding))

        for name, invalid_request in context_cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(
                        helper, invalid_request, reasoning_effort="ultra"
                    )

    def test_fast_lane_context_anchor_uniqueness_and_path_redaction(self) -> None:
        helper = load_efficiency()
        baseline = self.fully_bound_fast_lane_manual_request(helper)
        read_context = self.fast_lane_read_context(helper)
        baseline["read_contexts"] = [read_context]

        result = self.compile_fast_lane(
            helper, copy.deepcopy(baseline), reasoning_effort="ultra"
        )
        rendered = json.dumps(result, sort_keys=True)
        for context_path in (
            self.repo,
            read_context["worktree"],
            read_context["temp_target"],
        ):
            self.assertNotIn(str(context_path), rendered)
        self.assertNotIn('"bootstrap_plan"', rendered)
        self.assertNotIn("command_argv", rendered)

        source_plan = helper.decompose(baseline["work_package"])
        first_unit = source_plan["units"][0]
        integration = baseline["scheduler_state"]["integration_state"]
        verification_read = self.fast_lane_read_context(
            helper,
            task_id=first_unit["task_id"],
            role="verification",
            base_commit=integration["commit"],
            tree=integration["tree"],
        )

        invalid_cases: list[tuple[str, dict[str, object]]] = []

        anchor_mismatch = copy.deepcopy(baseline)
        anchor_mismatch["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                repo=self.safe_root / "other-repository",
            )
        ]
        invalid_cases.append(("anchor_mismatch", anchor_mismatch))

        worktree_is_repo = copy.deepcopy(baseline)
        worktree_is_repo["read_contexts"] = [
            self.fast_lane_read_context(helper, worktree=self.repo)
        ]
        invalid_cases.append(("worktree_is_repo", worktree_is_repo))

        outside_task_root_worktree = copy.deepcopy(baseline)
        outside_task_root_worktree["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                worktree=self.safe_root / "outside-fast-lane-read-worktree",
            )
        ]
        invalid_cases.append(("outside_task_root_worktree", outside_task_root_worktree))

        execution_worktree_collision = copy.deepcopy(baseline)
        execution_worktree_collision["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                worktree=baseline["execution_contexts"][0]["bootstrap_plan"][
                    "worktree"
                ],
            )
        ]
        invalid_cases.append(
            ("execution_worktree_collision", execution_worktree_collision)
        )

        execution_temp_collision = copy.deepcopy(baseline)
        execution_temp_collision["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                temp_target=baseline["execution_contexts"][0]["bootstrap_plan"][
                    "temp_target"
                ],
            )
        ]
        invalid_cases.append(("execution_temp_collision", execution_temp_collision))

        temp_target_is_task_root = copy.deepcopy(baseline)
        temp_target_is_task_root["read_contexts"] = [
            self.fast_lane_read_context(helper, temp_target=self.fast_lane_task_root)
        ]
        invalid_cases.append(("temp_target_is_task_root", temp_target_is_task_root))

        duplicate_read_targets = copy.deepcopy(baseline)
        duplicate_read_targets["read_contexts"] = [
            self.fast_lane_read_context(helper),
            self.fast_lane_read_context(
                helper,
                task_id="ATLAS-12B-B",
                worktree=self.fast_lane_task_root / "worktrees" / "fast-lane-read",
                temp_target=self.fast_lane_task_root / "tasks" / "fast-lane-read",
            ),
        ]
        invalid_cases.append(("duplicate_read_targets", duplicate_read_targets))

        verification_commit_mismatch = copy.deepcopy(baseline)
        verification_read["base_commit"] = "c" * 40
        verification_commit_mismatch["read_contexts"] = [verification_read]
        invalid_cases.append(
            ("verification_commit_mismatch", verification_commit_mismatch)
        )

        verification_tree_mismatch = copy.deepcopy(baseline)
        verification_read = self.fast_lane_read_context(
            helper,
            task_id=first_unit["task_id"],
            role="verification",
            base_commit=integration["commit"],
            tree="c" * 40,
        )
        verification_tree_mismatch["read_contexts"] = [verification_read]
        invalid_cases.append(("verification_tree_mismatch", verification_tree_mismatch))

        verification_snapshot_missing = copy.deepcopy(baseline)
        verification_read = self.fast_lane_read_context(
            helper,
            task_id=first_unit["task_id"],
            role="verification",
            base_commit=integration["commit"],
            tree=integration["tree"],
            workspace_input_snapshot_id=None,
        )
        verification_read["workspace_input_snapshot_id"] = None
        verification_snapshot_missing["read_contexts"] = [verification_read]
        invalid_cases.append(
            ("verification_snapshot_missing", verification_snapshot_missing)
        )

        verification_context_missing = copy.deepcopy(baseline)
        verification_context_missing["scheduler_state"]["phase"] = (
            "integration_regression"
        )
        verification_context_missing["read_contexts"] = []
        invalid_cases.append(
            ("verification_context_missing", verification_context_missing)
        )

        for name, invalid_request in invalid_cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(
                        helper, invalid_request, reasoning_effort="ultra"
                    )

    def test_fast_lane_standalone_read_context_is_bound_to_its_project_root(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_contexts_empty_request(helper)
        source_plan = helper.decompose(request["work_package"])
        context = self.fast_lane_read_context(
            helper,
            worktree=self.fast_lane_task_root / "worktrees" / "standalone-read",
            temp_target=self.fast_lane_task_root / "tasks" / "standalone-read",
        )

        execution, reads = helper._validated_fast_lane_contexts(
            [],
            [context],
            source_plan,
            request["scheduler_state"],
        )

        self.assertEqual([], execution)
        self.assertEqual(self.project, reads[0]["project"])
        self.assertEqual(context["task_root_hash"], reads[0]["task_root_hash"])

        sibling = self.fast_lane_task_root.parent / "sibling-fast-lane-project"
        outside = self.fast_lane_read_context(
            helper,
            worktree=sibling / "worktrees" / "standalone-read",
            temp_target=sibling / "tasks" / "standalone-read",
        )
        with self.assertRaises(ValueError):
            helper._validated_fast_lane_contexts(
                [],
                [outside],
                source_plan,
                request["scheduler_state"],
            )

        alias = copy.deepcopy(context)
        alias["worktree"] = str(
            self.fast_lane_task_root / "worktrees" / "standalone-read."
        )
        with self.assertRaises(ValueError):
            helper._validated_fast_lane_contexts(
                [],
                [alias],
                source_plan,
                request["scheduler_state"],
            )

    def test_fast_lane_standalone_read_context_rejects_a_task_root_change(
        self,
    ) -> None:
        helper = load_efficiency()
        replacement_root = self.temp / "standalone-read-root"
        configured_root = replacement_root / "bound-project"
        configured_root.mkdir(parents=True)
        project = "bound-project"
        project_root = configured_root / project
        request = self.fast_lane_contexts_empty_request(helper)
        source_plan = helper.decompose(request["work_package"])

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(configured_root)},
        ):
            context = self.fast_lane_read_context(
                helper,
                project=project,
                worktree=project_root / "worktrees" / "standalone-read",
                temp_target=project_root / "tasks" / "standalone-read",
            )
            helper._validated_fast_lane_contexts(
                [],
                [context],
                source_plan,
                request["scheduler_state"],
            )

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(replacement_root)},
        ):
            with self.assertRaises(ValueError):
                helper._validated_fast_lane_contexts(
                    [],
                    [context],
                    source_plan,
                    request["scheduler_state"],
                )

    def test_fast_lane_has_no_external_side_effect(self) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)

        with (
            mock.patch.object(
                helper.subprocess,
                "run",
                side_effect=AssertionError("compile must not run subprocesses"),
            ),
            mock.patch.object(
                helper,
                "apply_bootstrap_plan",
                side_effect=AssertionError("compile must not apply bootstrap plans"),
            ),
            mock.patch.object(
                helper.Path,
                "mkdir",
                side_effect=AssertionError("compile must not create directories"),
            ),
        ):
            result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")

        self.assertEqual("active", result["status"])

    def test_fast_lane_running_ledger_and_assignment_token_are_exact(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)

        result = self.compile_fast_lane(
            helper, copy.deepcopy(request), reasoning_effort="ultra"
        )
        self.assertEqual("active", result["status"])
        self.assertEqual("FAST_LANE_ACTIVE", result["decision_code"])
        self.assertEqual(2, len(result["assignments"]))
        source_assignment = request["scheduler_state"]["running_assignments"][0]
        retained = next(
            item
            for item in result["assignments"]
            if item["task_id"] == source_assignment["task_id"]
        )
        self.assertEqual("retain", retained["action"])
        self.assertEqual(
            source_assignment["assignment_token"], retained["assignment_token"]
        )
        self.assertEqual(
            source_assignment["dispatch_receipt"], retained["dispatch_receipt"]
        )
        self.assertEqual("execution", retained["role"])
        self.assertEqual("gpt-5.6-terra", retained["model"])
        self.assertEqual("high", retained["reasoning_effort"])
        self.assertEqual(
            source_assignment["assignment_token"],
            helper._sha256_json(source_assignment["dispatch_receipt"]),
        )
        self.assertNotIn(str(self.repo), json.dumps(result, sort_keys=True))

        invalid_requests: list[tuple[str, dict[str, object]]] = []

        forged_token = copy.deepcopy(request)
        forged_token["scheduler_state"]["running_assignments"][0][
            "assignment_token"
        ] = "sha256:" + "0" * 64
        invalid_requests.append(("forged_token", forged_token))

        receipt_role = copy.deepcopy(request)
        assignment = receipt_role["scheduler_state"]["running_assignments"][0]
        assignment["role"] = "review"
        assignment["dispatch_receipt"]["role"] = "review"
        assignment["assignment_token"] = helper._sha256_json(
            assignment["dispatch_receipt"]
        )
        invalid_requests.append(("receipt_role", receipt_role))

        context_hash = copy.deepcopy(request)
        context_hash["scheduler_state"]["dispatch_contexts"][0]["context_hash"] = (
            "sha256:" + "1" * 64
        )
        invalid_requests.append(("context_hash", context_hash))

        duplicate_lifecycle = copy.deepcopy(request)
        duplicate_lifecycle["scheduler_state"]["blocked_task_ids"] = [
            duplicate_lifecycle["scheduler_state"]["running_assignments"][0]["task_id"]
        ]
        invalid_requests.append(("duplicate_lifecycle", duplicate_lifecycle))

        unknown_scheduler_field = copy.deepcopy(request)
        unknown_scheduler_field["scheduler_state"]["unexpected"] = True
        invalid_requests.append(("unknown_scheduler_field", unknown_scheduler_field))

        for name, invalid_request in invalid_requests:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(
                        helper, invalid_request, reasoning_effort="ultra"
                    )

    def test_fast_lane_rejects_malformed_remediation_request(self) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        request["remediation_request"] = {}

        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, request, reasoning_effort="ultra")

    def test_fast_lane_rejects_invalid_or_overlapping_scheduler_state(self) -> None:
        helper = load_efficiency()

        with self.subTest("initial_null_source_hash_uses_derived_hash_for_host_audit"):
            pristine = self.fully_bound_fast_lane_manual_request(helper)
            try:
                result = self.compile_fast_lane(
                    helper,
                    pristine,
                    reasoning_effort="ultra",
                    host_status={
                        "workflow_id": "FASTLANE-20260730",
                        "current_leases": [],
                        "host_bindings": [],
                    },
                )
            except ValueError as error:
                self.fail(
                    f"initial null scheduler hash must support host audit: {error}"
                )
            audit = result["refill_plan"]["occupancy_audit"]
            self.assertEqual([], audit["active_slot_ids"])
            self.assertEqual(["slot-1", "slot-2", "slot-3"], audit["vacant_slot_ids"])
            self.assertEqual(
                "next_host_dispatch_boundary",
                audit["refill_trigger"]["dispatch_at"],
            )

        request = self.fully_bound_fast_lane_manual_request(helper)
        source_task_id = helper.decompose(request["work_package"])["units"][0][
            "task_id"
        ]
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=source_task_id
        )
        terminal = self.fast_lane_terminal_result(helper, assignment)
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "completed_tasks": [self.fast_lane_completed_record(helper, terminal)],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
                "lane0_state": {
                    "active_task_id": source_task_id,
                    "owned_write_scopes": [],
                },
            }
        )
        with self.subTest("lane_zero_cannot_reopen_completed_work"):
            with self.assertRaises(ValueError):
                self.compile_fast_lane(helper, request, reasoning_effort="ultra")

    def test_fast_lane_candidate_never_unlocks_before_lane_zero_completion(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id="FAST-LANE-MODERATE"
        )
        terminal = self.fast_lane_terminal_result(helper, assignment)
        candidate = {
            "task_id": "FAST-LANE-MODERATE",
            "candidate_commit": terminal["candidate_commit"],
            "candidate_tree": terminal["candidate_tree"],
            "red_evidence_hashes": terminal["red_evidence_hashes"],
            "green_evidence_hashes": terminal["green_evidence_hashes"],
            "terminal_result_hash": helper._sha256_json(terminal),
            "terminal_result": terminal,
        }
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "review_ready_candidates": [candidate],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")
        self.assertNotIn(
            "FAST-LANE-FUTURE",
            [
                item["task_id"]
                for item in result["assignments"]
                if item["role"] == "execution"
            ],
        )
        self.assertNotIn(
            "FAST-LANE-FUTURE",
            [item["task_id"] for item in result["ready_queue"]],
        )

        cross_role = copy.deepcopy(request)
        cross_role_terminal = cross_role["scheduler_state"]["review_ready_candidates"][
            0
        ]["terminal_result"]
        cross_role_terminal["role"] = "prewarm"
        cross_role["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result_hash"
        ] = helper._sha256_json(cross_role_terminal)
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, cross_role, reasoning_effort="ultra")

    def test_fast_lane_rejects_forged_stale_duplicate_or_cross_role_tokens(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        source_plan = helper.decompose(request["work_package"])
        task_id = source_plan["units"][0]["task_id"]
        other_task_id = source_plan["units"][1]["task_id"]
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=task_id
        )
        terminal = self.fast_lane_terminal_result(helper, assignment)
        candidate = {
            "task_id": task_id,
            "candidate_commit": terminal["candidate_commit"],
            "candidate_tree": terminal["candidate_tree"],
            "red_evidence_hashes": terminal["red_evidence_hashes"],
            "green_evidence_hashes": terminal["green_evidence_hashes"],
            "terminal_result_hash": helper._sha256_json(terminal),
            "terminal_result": terminal,
        }
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "review_ready_candidates": [candidate],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )

        forged = copy.deepcopy(request)
        forged_terminal = forged["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result"
        ]
        forged_terminal["assignment_token"] = "sha256:" + "0" * 64
        forged["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result_hash"
        ] = helper._sha256_json(forged_terminal)

        stale = copy.deepcopy(request)
        stale_terminal = stale["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result"
        ]
        stale_terminal["dispatch_receipt"]["source_plan_hash"] = "sha256:" + "1" * 64
        stale["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result_hash"
        ] = helper._sha256_json(stale_terminal)

        cross_role = copy.deepcopy(request)
        cross_role_terminal = cross_role["scheduler_state"]["review_ready_candidates"][
            0
        ]["terminal_result"]
        cross_role_terminal["role"] = "review"
        cross_role["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result_hash"
        ] = helper._sha256_json(cross_role_terminal)

        forged_recovery = copy.deepcopy(request)
        recovered_terminal = forged_recovery["scheduler_state"][
            "review_ready_candidates"
        ][0]["terminal_result"]
        recovered_receipt = recovered_terminal["dispatch_receipt"]
        recovered_receipt["assignment_epoch"] = 2
        recovered_receipt["recovery_of_assignment_token"] = "sha256:" + "2" * 64
        recovered_terminal["assignment_token"] = helper._sha256_json(recovered_receipt)
        forged_recovery["scheduler_state"]["slot_epochs"]["slot-1"] = 2
        forged_recovery["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result_hash"
        ] = helper._sha256_json(recovered_terminal)

        stale_epoch = copy.deepcopy(request)
        stale_epoch["scheduler_state"]["slot_epochs"]["slot-1"] = 2

        stale_running = self.fast_lane_running_request(helper)
        stale_running["scheduler_state"]["slot_epochs"]["slot-1"] = 2

        duplicate = copy.deepcopy(request)
        duplicate_terminal = duplicate["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result"
        ]
        duplicate["scheduler_state"]["prewarmed_evidence"] = [
            {
                "task_id": other_task_id,
                "observation_basis_hash": helper._sha256_json({"basis": "duplicate"}),
                "evidence_hash": helper._sha256_json({"evidence": "duplicate"}),
                "terminal_result_hash": helper._sha256_json(duplicate_terminal),
                "terminal_result": duplicate_terminal,
                "revalidation_basis_hash": None,
                "dependency_delta_hash": None,
                "revalidation_evidence_hash": None,
            }
        ]

        for name, invalid in (
            ("forged", forged),
            ("stale", stale),
            ("cross_role", cross_role),
            ("forged_recovery", forged_recovery),
            ("stale_terminal_epoch", stale_epoch),
            ("stale_running_epoch", stale_running),
            ("duplicate", duplicate),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, invalid, reasoning_effort="ultra")

    def test_fast_lane_rejects_resigned_terminal_receipt_with_wrong_route(self) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        task_id = helper.decompose(request["work_package"])["units"][0]["task_id"]
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=task_id
        )
        terminal_result = self.fast_lane_terminal_result(helper, assignment)
        candidate = {
            "task_id": task_id,
            "candidate_commit": terminal_result["candidate_commit"],
            "candidate_tree": terminal_result["candidate_tree"],
            "red_evidence_hashes": terminal_result["red_evidence_hashes"],
            "green_evidence_hashes": terminal_result["green_evidence_hashes"],
            "terminal_result_hash": helper._sha256_json(terminal_result),
            "terminal_result": terminal_result,
        }
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "review_ready_candidates": [candidate],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        self.compile_fast_lane(helper, copy.deepcopy(request), reasoning_effort="ultra")

        forged = copy.deepcopy(request)
        forged_terminal = forged["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result"
        ]
        forged_receipt = forged_terminal["dispatch_receipt"]
        forged_receipt["reasoning_effort"] = "low"
        forged_terminal["assignment_token"] = helper._sha256_json(forged_receipt)
        forged["scheduler_state"]["review_ready_candidates"][0][
            "terminal_result_hash"
        ] = helper._sha256_json(forged_terminal)

        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged, reasoning_effort="ultra")

    def test_fast_lane_rejects_resigned_running_receipt_with_wrong_route(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)
        self.compile_fast_lane(helper, copy.deepcopy(request), reasoning_effort="ultra")

        forged = copy.deepcopy(request)
        assignment = forged["scheduler_state"]["running_assignments"][0]
        receipt = assignment["dispatch_receipt"]
        assignment["reasoning_effort"] = "low"
        receipt["reasoning_effort"] = "low"
        assignment["assignment_token"] = helper._sha256_json(receipt)

        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged, reasoning_effort="ultra")

    def test_fast_lane_rejects_completion_snapshot_not_bound_to_dispatch_context(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        task_id = helper.decompose(request["work_package"])["units"][0]["task_id"]
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=task_id
        )
        terminal_result = self.fast_lane_terminal_result(helper, assignment)
        completed = self.fast_lane_completed_record(helper, terminal_result)
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "completed_tasks": [completed],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        self.compile_fast_lane(helper, copy.deepcopy(request), reasoning_effort="ultra")

        forged = copy.deepcopy(request)
        completed = forged["scheduler_state"]["completed_tasks"][0]
        receipt = completed["completion_receipt"]
        receipt["workspace_input_snapshot_id"] = helper._sha256_json(
            {"snapshot": "forged"}
        )
        receipt_hash = helper._sha256_json(receipt)
        completed["completion_receipt_hash"] = receipt_hash
        completed["result_hash"] = receipt_hash

        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged, reasoning_effort="ultra")

    def test_fast_lane_rejects_verification_completion_not_bound_to_read_context(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_code_atlas_request(helper)
        verification_unit = next(
            unit
            for unit in helper.decompose(request["work_package"])["units"]
            if unit["unit_kind"] == "verification"
        )
        assignment, context, validated = self.fast_lane_assignment_for(
            helper,
            request,
            task_id=verification_unit["task_id"],
            role="verification",
        )
        terminal_result = self.fast_lane_terminal_result(helper, assignment)
        completed = self.fast_lane_completed_record(
            helper,
            terminal_result,
            completion_kind="verification_evidence",
        )
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "phase": "integration_regression",
                "completed_tasks": [completed],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        self.compile_fast_lane(helper, copy.deepcopy(request), reasoning_effort="ultra")

        for name, updates in (
            (
                "snapshot",
                {
                    "workspace_input_snapshot_id": helper._sha256_json(
                        {"snapshot": "forged"}
                    )
                },
            ),
            (
                "integration_commit",
                {"integration_commit": "c" * 40},
            ),
            (
                "integration_tree",
                {"integration_tree": "c" * 40},
            ),
        ):
            with self.subTest(case=name):
                forged = copy.deepcopy(request)
                completed = forged["scheduler_state"]["completed_tasks"][0]
                receipt = completed["completion_receipt"]
                receipt.update(updates)
                receipt_hash = helper._sha256_json(receipt)
                completed["completion_receipt_hash"] = receipt_hash
                completed["result_hash"] = receipt_hash
                if name in {"integration_commit", "integration_tree"}:
                    completed["integration_commit"] = receipt["integration_commit"]
                    completed["integration_tree"] = receipt["integration_tree"]
                with self.assertRaises(ValueError):
                    self.compile_fast_lane(helper, forged, reasoning_effort="ultra")

    def test_fast_lane_rejects_forged_prewarm_dispatch_bindings(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        source_plan = helper.decompose(request["work_package"])
        future = next(
            unit
            for unit in source_plan["units"]
            if unit["task_id"] == "FAST-LANE-FUTURE"
        )
        request["execution_contexts"] = [
            self.fast_lane_execution_context(
                helper,
                task_id=unit["task_id"],
                base_commit=request["scheduler_state"]["integration_state"]["commit"],
                write_scope=list(unit["write_scope"]),
            )
            for unit in source_plan["units"]
        ]
        request["scheduler_state"]["source_plan_hash"] = helper._sha256_json(
            source_plan
        )
        prewarm_assignment, prewarm_context, validated = self.fast_lane_assignment_for(
            helper,
            request,
            task_id=future["task_id"],
            role="prewarm",
        )
        prewarm_terminal = self.fast_lane_terminal_result(helper, prewarm_assignment)
        prewarm_record = {
            "task_id": future["task_id"],
            "observation_basis_hash": prewarm_context["basis_hash"],
            "evidence_hash": prewarm_terminal["evidence_hash"],
            "terminal_result_hash": helper._sha256_json(prewarm_terminal),
            "terminal_result": prewarm_terminal,
            "revalidation_basis_hash": helper._sha256_json(
                {"basis": future["task_id"]}
            ),
            "dependency_delta_hash": helper._sha256_json({"delta": future["task_id"]}),
            "revalidation_evidence_hash": helper._sha256_json(
                {"revalidated": future["task_id"]}
            ),
        }
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "prewarmed_evidence": [prewarm_record],
                "dispatch_contexts": [prewarm_context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        _, execution_context, _ = self.fast_lane_assignment_for(
            helper,
            request,
            task_id=future["task_id"],
            role="execution",
            slot_id="slot-2",
        )
        request["scheduler_state"]["dispatch_contexts"] = [
            prewarm_context,
            execution_context,
        ]
        request["scheduler_state"]["slot_epochs"] = {
            "slot-1": 1,
            "slot-2": 1,
            "slot-3": 0,
        }
        self.compile_fast_lane(helper, copy.deepcopy(request), reasoning_effort="ultra")

        forged = copy.deepcopy(request)
        forged_context = next(
            item
            for item in forged["scheduler_state"]["dispatch_contexts"]
            if item["role"] == "execution"
        )
        forged_context["prewarm_evidence_hash"] = helper._sha256_json(
            {"forged": "prewarm"}
        )
        context_without_hash = dict(forged_context)
        context_without_hash.pop("context_hash")
        forged_context["context_hash"] = helper._sha256_json(context_without_hash)
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged, reasoning_effort="ultra")

    def test_fast_lane_review_must_bind_the_candidate_context(self) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        source_plan = helper.decompose(request["work_package"])
        task_id = source_plan["units"][0]["task_id"]
        assignment, execution_context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=task_id
        )
        candidate_terminal = self.fast_lane_terminal_result(helper, assignment)
        candidate = {
            "task_id": task_id,
            "candidate_commit": candidate_terminal["candidate_commit"],
            "candidate_tree": candidate_terminal["candidate_tree"],
            "red_evidence_hashes": candidate_terminal["red_evidence_hashes"],
            "green_evidence_hashes": candidate_terminal["green_evidence_hashes"],
            "terminal_result_hash": helper._sha256_json(candidate_terminal),
            "terminal_result": candidate_terminal,
        }
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "review_ready_candidates": [candidate],
                "dispatch_contexts": [execution_context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        request["read_contexts"] = [
            self.fast_lane_read_context(
                helper,
                task_id=task_id,
                role="review",
                read_scope=list(source_plan["units"][0]["write_scope"]),
            )
        ]
        review_assignment, review_context, _ = self.fast_lane_assignment_for(
            helper,
            request,
            task_id=task_id,
            role="review",
            slot_id="slot-2",
        )
        review_terminal = self.fast_lane_terminal_result(helper, review_assignment)
        reviewed = {
            "task_id": task_id,
            "candidate_commit": candidate_terminal["candidate_commit"],
            "candidate_tree": candidate_terminal["candidate_tree"],
            "red_evidence_hashes": candidate_terminal["red_evidence_hashes"],
            "green_evidence_hashes": candidate_terminal["green_evidence_hashes"],
            "review_hash": review_terminal["review_hash"],
            "outcome": "pass",
            "terminal_result_hash": helper._sha256_json(candidate_terminal),
            "terminal_result": candidate_terminal,
            "review_terminal_result_hash": helper._sha256_json(review_terminal),
            "review_terminal_result": review_terminal,
        }
        request["scheduler_state"].update(
            {
                "review_ready_candidates": [],
                "reviewed_candidates": [reviewed],
                "dispatch_contexts": [execution_context, review_context],
                "slot_epochs": {"slot-1": 1, "slot-2": 1, "slot-3": 0},
            }
        )
        self.compile_fast_lane(helper, request, reasoning_effort="ultra")

        forged = copy.deepcopy(request)
        forged_context = forged["scheduler_state"]["dispatch_contexts"][1]
        forged_context["candidate_commit"] = "e" * 40
        forged_context["context_hash"] = helper._sha256_json(
            {
                key: value
                for key, value in forged_context.items()
                if key != "context_hash"
            }
        )
        forged_terminal = forged["scheduler_state"]["reviewed_candidates"][0][
            "review_terminal_result"
        ]
        forged_terminal["dispatch_receipt"]["dispatch_context_hash"] = forged_context[
            "context_hash"
        ]
        forged_terminal["assignment_token"] = helper._sha256_json(
            forged_terminal["dispatch_receipt"]
        )
        forged["scheduler_state"]["reviewed_candidates"][0][
            "review_terminal_result_hash"
        ] = helper._sha256_json(forged_terminal)
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged, reasoning_effort="ultra")

    def test_fast_lane_packet_and_episode_verification_is_declared(self) -> None:
        helper = load_efficiency()
        packet = self.fast_lane_code_atlas_request(helper)
        packet_source = helper.decompose(packet["work_package"])
        code_units = [
            unit for unit in packet_source["units"] if unit["unit_kind"] == "code"
        ]
        contexts: list[dict[str, object]] = []
        completed: list[dict[str, object]] = []
        for unit in code_units:
            assignment, context, _ = self.fast_lane_assignment_for(
                helper, packet, task_id=unit["task_id"]
            )
            contexts.append(context)
            completed.append(
                self.fast_lane_completed_record(
                    helper, self.fast_lane_terminal_result(helper, assignment)
                )
            )
        packet["scheduler_state"].update(
            {
                "source_plan_hash": helper._sha256_json(packet_source),
                "phase": "integration_regression",
                "completed_tasks": completed,
                "dispatch_contexts": contexts,
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        try:
            packet_result = self.compile_fast_lane(
                helper, packet, reasoning_effort="ultra"
            )
        except ValueError as error:
            self.fail(f"declared packet verification must compile: {error}")
        self.assertEqual("integration_regression", packet_result["phase"])
        self.assertEqual(1, len(packet_result["assignments"]))
        verification = packet_result["assignments"][0]
        self.assertEqual("verification", verification["role"])
        self.assertEqual("read_only", verification["access"])
        self.assertEqual([], verification["write_scope"])
        self.assertIsNone(verification["execution_context_hash"])
        self.assertIsNotNone(verification["read_context_hash"])
        self.assertIsNone(verification["driver_gate_id"])
        self.assertTrue(verification["target_gates"])
        self.assertEqual([], packet_result["ready_queue"])
        self.assertEqual([], packet_result["review_queue"])
        self.assertEqual([], packet_result["prewarm_queue"])
        self.assertEqual([], packet_result["design_queue"])

        queued_execution = self.fast_lane_code_atlas_request(helper)
        queued_execution["scheduler_state"]["phase"] = "integration_regression"
        queued_result = self.compile_fast_lane(
            helper, queued_execution, reasoning_effort="ultra"
        )
        self.assertEqual([], queued_result["assignments"])
        self.assertEqual([], queued_result["ready_queue"])
        self.assertEqual([], queued_result["review_queue"])
        self.assertEqual([], queued_result["prewarm_queue"])
        self.assertEqual([], queued_result["design_queue"])

        episode_source = helper.decompose(self.extractor_episode_manifest())
        episode_targets = []
        for unit in episode_source["units"]:
            gate = self.fast_lane_gate(
                gate_id="declared",
                red_expected_exit_codes=(
                    [] if unit["unit_kind"] == "verification" else [1]
                ),
                red_failure_ids=([] if unit["unit_kind"] == "verification" else None),
                acceptance_constraint_hashes=list(unit["acceptance_constraints"]),
            )
            episode_targets.append(
                {
                    "task_id": unit["task_id"],
                    "driver_gate_id": (
                        None if unit["unit_kind"] == "verification" else "declared"
                    ),
                    "gates": [gate],
                }
            )
        episode_gates = helper._validated_fast_lane_target_gates(
            episode_targets, episode_source
        )
        self.assertEqual(
            [unit["task_id"] for unit in episode_source["units"]],
            [target["task_id"] for target in episode_gates],
        )

    def test_fast_lane_allows_one_narrow_remediation_then_stops(self) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        source_plan = helper.decompose(request["work_package"])
        source_task_id = source_plan["units"][0]["task_id"]
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=source_task_id
        )
        completed = self.fast_lane_completed_record(
            helper, self.fast_lane_terminal_result(helper, assignment)
        )
        remediation = self.fast_lane_remediation_request(helper, request)
        try:
            remediation_context = self.fast_lane_execution_context(
                helper,
                task_id=remediation["task_id"],
                base_commit=request["scheduler_state"]["integration_state"]["commit"],
                branch=f"codex/{remediation['task_id'].lower()}",
                write_scope=list(remediation["write_scope"]),
            )
        except ValueError as error:
            self.fail(f"remediation execution context must be accepted: {error}")
        request["execution_contexts"].append(remediation_context)
        request["remediation_request"] = remediation
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "phase": "remediation",
                "completed_tasks": [completed],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
                "global_remediation": {
                    "round": 1,
                    "state": "approved",
                    "task_id": remediation["task_id"],
                    "affected_task_ids": [source_task_id],
                    "blocker_review_hash": remediation["blocker_review_hash"],
                    "finding_hash": remediation["finding_hash"],
                    "dispatch_receipt": None,
                    "completion_receipt_hash": None,
                },
            }
        )
        try:
            result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")
        except ValueError as error:
            self.fail(f"narrow remediation must compile once: {error}")
        self.assertEqual("active", result["status"])
        self.assertEqual(
            [remediation["task_id"]],
            [item["task_id"] for item in result["assignments"]],
        )

        broad = copy.deepcopy(request)
        broad["remediation_request"]["write_scope"] = ["src"]
        broad_result = self.compile_fast_lane(helper, broad, reasoning_effort="ultra")
        self.assertEqual("stopped", broad_result["status"])
        self.assertEqual("AUTOMATION_STOPPED", broad_result["decision_code"])
        self.assertEqual([], broad_result["assignments"])

        round_two = copy.deepcopy(request)
        round_two["remediation_request"]["round"] = 2
        round_two_result = self.compile_fast_lane(
            helper, round_two, reasoning_effort="ultra"
        )
        self.assertEqual("stopped", round_two_result["status"])
        self.assertEqual([], round_two_result["assignments"])

    def test_fast_lane_rejects_unbound_global_remediation_receipts(self) -> None:
        helper = load_efficiency()
        request = self.fully_bound_fast_lane_manual_request(helper)
        source_plan = helper.decompose(request["work_package"])
        source_task_id = source_plan["units"][0]["task_id"]
        assignment, context, validated = self.fast_lane_assignment_for(
            helper, request, task_id=source_task_id
        )
        completed = self.fast_lane_completed_record(
            helper, self.fast_lane_terminal_result(helper, assignment)
        )
        remediation = self.fast_lane_remediation_request(helper, request)
        remediation_context = self.fast_lane_execution_context(
            helper,
            task_id=remediation["task_id"],
            base_commit=request["scheduler_state"]["integration_state"]["commit"],
            branch=f"codex/{remediation['task_id'].lower()}",
            write_scope=list(remediation["write_scope"]),
        )
        request["execution_contexts"].append(remediation_context)
        request["remediation_request"] = remediation
        request["scheduler_state"].update(
            {
                "source_plan_hash": validated["source_plan_hash"],
                "phase": "remediation",
                "completed_tasks": [completed],
                "dispatch_contexts": [context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
                "global_remediation": {
                    "round": 1,
                    "state": "approved",
                    "task_id": remediation["task_id"],
                    "affected_task_ids": [source_task_id],
                    "blocker_review_hash": remediation["blocker_review_hash"],
                    "finding_hash": remediation["finding_hash"],
                    "dispatch_receipt": None,
                    "completion_receipt_hash": None,
                },
            }
        )
        self.compile_fast_lane(helper, copy.deepcopy(request), reasoning_effort="ultra")

        remediation_assignment, remediation_dispatch_context, _ = (
            self.fast_lane_assignment_for(
                helper,
                request,
                task_id=remediation["task_id"],
                slot_id="slot-2",
            )
        )
        running = copy.deepcopy(request)
        running["scheduler_state"]["running_assignments"] = [remediation_assignment]
        running["scheduler_state"]["dispatch_contexts"] = [
            context,
            remediation_dispatch_context,
        ]
        running["scheduler_state"]["slot_epochs"]["slot-2"] = 1
        running_global = running["scheduler_state"]["global_remediation"]
        running_global["state"] = "running"
        running_global["dispatch_receipt"] = copy.deepcopy(
            remediation_assignment["dispatch_receipt"]
        )
        self.compile_fast_lane(helper, copy.deepcopy(running), reasoning_effort="ultra")

        forged_running = copy.deepcopy(running)
        forged_running["scheduler_state"]["global_remediation"]["dispatch_receipt"] = {
            "forged": "receipt"
        }
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged_running, reasoning_effort="ultra")

        remediation_terminal = self.fast_lane_terminal_result(
            helper, remediation_assignment
        )
        remediation_completed = self.fast_lane_completed_record(
            helper, remediation_terminal
        )
        completed_request = copy.deepcopy(running)
        completed_request["scheduler_state"]["running_assignments"] = []
        completed_request["scheduler_state"]["completed_tasks"] = [
            completed,
            remediation_completed,
        ]
        completed_global = completed_request["scheduler_state"]["global_remediation"]
        completed_global["state"] = "completed"
        completed_global["dispatch_receipt"] = copy.deepcopy(
            remediation_terminal["dispatch_receipt"]
        )
        completed_global["completion_receipt_hash"] = remediation_completed[
            "completion_receipt_hash"
        ]
        self.compile_fast_lane(
            helper, copy.deepcopy(completed_request), reasoning_effort="ultra"
        )

        forged_completed = copy.deepcopy(completed_request)
        forged_completed["scheduler_state"]["global_remediation"][
            "completion_receipt_hash"
        ] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged_completed, reasoning_effort="ultra")

    def test_fast_lane_terminal_protocol_is_bounded(self) -> None:
        helper = load_efficiency()
        for phase in ("blocker_review", "acceptance"):
            request = self.fully_bound_fast_lane_manual_request(helper)
            request["scheduler_state"]["phase"] = phase
            with self.subTest(phase=phase):
                result = self.compile_fast_lane(
                    helper, request, reasoning_effort="ultra"
                )
                self.assertEqual("active", result["status"])
                self.assertEqual(
                    "TERMINAL_PROTOCOL_OWNED_BY_LANE0", result["decision_code"]
                )
                self.assertEqual([], result["assignments"])
                self.assertEqual([], result["ready_queue"])
                self.assertEqual([], result["review_queue"])
                self.assertEqual([], result["prewarm_queue"])
                self.assertEqual([], result["design_queue"])
                self.assertEqual(
                    [
                        {
                            "slot_id": "slot-1",
                            "reason_code": "TERMINAL_PHASE_OWNED_BY_LANE0",
                        },
                        {
                            "slot_id": "slot-2",
                            "reason_code": "TERMINAL_PHASE_OWNED_BY_LANE0",
                        },
                        {
                            "slot_id": "slot-3",
                            "reason_code": "TERMINAL_PHASE_OWNED_BY_LANE0",
                        },
                    ],
                    result["idle_slots"],
                )
                self.assertEqual(
                    1, result["terminal_protocol"]["integration_regression_passes"]
                )
                self.assertEqual(1, result["terminal_protocol"]["blocker_reviews"])
                self.assertEqual(
                    1,
                    result["terminal_protocol"]["global_targeted_remediation_rounds"],
                )

    def test_fast_lane_recovery_branches_are_phase_aware(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_running_request(helper)
        old_assignment = copy.deepcopy(
            request["scheduler_state"]["running_assignments"][0]
        )
        recovered = copy.deepcopy(old_assignment)
        recovered_receipt = recovered["dispatch_receipt"]
        recovered_receipt["assignment_epoch"] = 2
        recovered_receipt["recovery_of_assignment_token"] = old_assignment[
            "assignment_token"
        ]
        recovered["assignment_epoch"] = 2
        recovered["assignment_token"] = helper._sha256_json(recovered_receipt)
        request["scheduler_state"]["running_assignments"] = [recovered]
        request["scheduler_state"]["slot_epochs"]["slot-1"] = 2
        result = self.compile_fast_lane(helper, request, reasoning_effort="ultra")
        self.assertEqual(
            recovered["assignment_token"], result["assignments"][0]["assignment_token"]
        )

        forged_predecessor = copy.deepcopy(request)
        forged_receipt = forged_predecessor["scheduler_state"]["running_assignments"][
            0
        ]["dispatch_receipt"]
        forged_receipt["recovery_of_assignment_token"] = "sha256:" + "0" * 64
        forged_predecessor["scheduler_state"]["running_assignments"][0][
            "assignment_token"
        ] = helper._sha256_json(forged_receipt)
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, forged_predecessor, reasoning_effort="ultra")

        wrong_phase = copy.deepcopy(request)
        wrong_phase["scheduler_state"]["phase"] = "integration_regression"
        with self.assertRaises(ValueError):
            self.compile_fast_lane(helper, wrong_phase, reasoning_effort="ultra")

    def test_fast_lane_workflow_policy_is_exact_and_ordered(self) -> None:
        helper = load_efficiency()
        result = self.compile_fast_lane(
            helper,
            self.fully_bound_fast_lane_manual_request(helper),
            reasoning_effort="ultra",
        )
        self.assertEqual(
            {
                "owner": "work_methodology_skill",
                "boundary_operations": [
                    {
                        "boundary": "strict_writer_start",
                        "roles": ["execution"],
                        "operations": [
                            "project_index_prepare_input_context",
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
                            "worker_consume_index_context",
                            "worktree_checkpoint_create_before_first_write",
                            "native_scoped_write_and_target_gates",
                            "project_index_finalize_output_context",
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
                            "project_index_prepare_input_context",
                            "workflow_create_if_absent",
                            "workflow_register_task_strict_index",
                            "workflow_ready",
                            "host_spawn_exact_route",
                            "workflow_claim_with_host_target",
                            "worker_consume_index_context",
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
                            "rebind_input_index_context_once",
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
                            "rebind_output_index_context_once",
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
                "index_protocol": {
                    "schema": "team-efficiency/fast-lane-index-protocol-v1",
                    "owner": "host",
                    "assignment_field": "index_context",
                    "preparation": "one_bounded_packet_per_assignment",
                    "input_query": "once_at_dispatch_boundary",
                    "output_query": "once_at_terminal_boundary",
                    "worker_action": "consume_only",
                    "worker_operations": [],
                    "mid_item_polling": False,
                    "missing_context_action": "stop",
                },
                "dispatch_protocol": {
                    "schema": "team-efficiency/fast-lane-dispatch-protocol-v1",
                    "tool": "collaboration.spawn_agent",
                    "model_source": "assignment.host_dispatch.model",
                    "reasoning_effort_source": "assignment.host_dispatch.reasoning_effort",
                    "inherit_current_session_model": False,
                    "require_explicit_route": True,
                    "missing_route_action": "reject",
                },
                "cross_session_protocol": {
                    "schema": "team-efficiency/fast-lane-cross-session-protocol-v1",
                    "projection_field": "cross_session_dispatch_projection",
                    "selection_authority": "compiler",
                    "trigger_status": "external_session_required",
                    "trigger_action": "dispatch_all",
                    "target": "independent_codex_session",
                    "not_required_action": "dispatch_none",
                    "blocked_action": "stop",
                    "llm_choice": False,
                },
            },
            result["workflow_policy"],
        )

    def test_fast_lane_phase_and_role_shapes_are_exact(self) -> None:
        helper = load_efficiency()
        result = self.compile_fast_lane(
            helper, self.fast_lane_schedule_request(helper), reasoning_effort="ultra"
        )
        expected_assignment_fields = {
            "slot_id",
            "action",
            "assignment_epoch",
            "assignment_token",
            "dispatch_receipt",
            "task_id",
            "goal",
            "output_boundary",
            "unit_kind",
            "operation_count",
            "recommended_route",
            "role",
            "model",
            "reasoning_effort",
            "host_dispatch",
            "index_context",
            "routing_context_hash",
            "routing_result_hash",
            "task_fingerprint",
            "routing_reason_codes",
            "routing_safety_floor_rank",
            "access",
            "context_hash",
            "execution_context_hash",
            "read_context_hash",
            "workspace_input_snapshot_id",
            "read_base_commit",
            "read_tree",
            "base_commit",
            "bootstrap_plan_hash",
            "branch",
            "write_scope_hash",
            "write_scope",
            "depends_on",
            "unmet_dependencies",
            "required_evidence",
            "execution_contracts",
            "direct_contract_hashes",
            "task_node_ids",
            "contract_node_ids",
            "acceptance_constraints",
            "driver_gate_id",
            "target_gates",
            "candidate_commit",
            "basis_hash",
        }
        for assignment in result["assignments"]:
            with self.subTest(role=assignment["role"]):
                self.assertEqual(expected_assignment_fields, set(assignment))
        execution = next(
            assignment
            for assignment in result["assignments"]
            if assignment["role"] == "execution"
        )
        self.assertEqual("exclusive_write", execution["access"])
        self.assertIsNotNone(execution["execution_context_hash"])
        self.assertIsNone(execution["read_context_hash"])
        self.assertIsNone(execution["read_base_commit"])
        self.assertIsNone(execution["read_tree"])
        self.assertIsNotNone(execution["driver_gate_id"])
        self.assertTrue(execution["target_gates"])
        prewarm = next(
            assignment
            for assignment in result["assignments"]
            if assignment["role"] == "prewarm"
        )
        self.assertEqual("read_only", prewarm["access"])
        self.assertIsNone(prewarm["execution_context_hash"])
        self.assertIsNotNone(prewarm["read_context_hash"])
        self.assertIsNotNone(prewarm["read_base_commit"])
        self.assertIsNotNone(prewarm["read_tree"])
        self.assertIsNone(prewarm["driver_gate_id"])
        self.assertEqual([], prewarm["target_gates"])
        self.assertIsNotNone(prewarm["basis_hash"])

        stale_prewarm = self.fast_lane_schedule_request(helper)
        prewarm_assignment, prewarm_context, prewarm_validated = (
            self.fast_lane_assignment_for(
                helper,
                stale_prewarm,
                task_id="FAST-LANE-FUTURE",
                role="prewarm",
            )
        )
        parent_assignment, parent_context, _ = self.fast_lane_assignment_for(
            helper, stale_prewarm, task_id="FAST-LANE-MODERATE"
        )
        prewarm_terminal = self.fast_lane_terminal_result(helper, prewarm_assignment)
        stale_prewarm["scheduler_state"].update(
            {
                "source_plan_hash": prewarm_validated["source_plan_hash"],
                "completed_tasks": [
                    self.fast_lane_completed_record(
                        helper,
                        self.fast_lane_terminal_result(helper, parent_assignment),
                    )
                ],
                "prewarmed_evidence": [
                    {
                        "task_id": "FAST-LANE-FUTURE",
                        "observation_basis_hash": prewarm_context["basis_hash"],
                        "evidence_hash": prewarm_terminal["evidence_hash"],
                        "terminal_result_hash": helper._sha256_json(prewarm_terminal),
                        "terminal_result": prewarm_terminal,
                        "revalidation_basis_hash": None,
                        "dependency_delta_hash": None,
                        "revalidation_evidence_hash": None,
                    }
                ],
                "dispatch_contexts": [parent_context, prewarm_context],
                "slot_epochs": {"slot-1": 1, "slot-2": 0, "slot-3": 0},
            }
        )
        stale_prewarm_result = self.compile_fast_lane(
            helper, stale_prewarm, reasoning_effort="ultra"
        )
        self.assertEqual(
            ["FAST-LANE-FUTURE"],
            stale_prewarm_result["invalidated_evidence_task_ids"],
        )

        terminal = self.fully_bound_fast_lane_manual_request(helper)
        terminal["scheduler_state"]["phase"] = "acceptance"
        terminal_result = self.compile_fast_lane(
            helper, terminal, reasoning_effort="ultra"
        )
        self.assertEqual("active", terminal_result["status"])
        self.assertEqual(
            "TERMINAL_PROTOCOL_OWNED_BY_LANE0", terminal_result["decision_code"]
        )
        self.assertEqual([], terminal_result["assignments"])

        stopped = self.fully_bound_fast_lane_manual_request(helper)
        stopped["scheduler_state"]["phase"] = "stopped"
        stopped_result = self.compile_fast_lane(
            helper, stopped, reasoning_effort="ultra"
        )
        self.assertEqual("stopped", stopped_result["status"])
        self.assertEqual("AUTOMATION_STOPPED", stopped_result["decision_code"])
        self.assertEqual([], stopped_result["assignments"])

    def test_legacy_decompose_golden_bytes_are_unchanged(self) -> None:
        helper = load_efficiency()

        payload = canonical_bytes(helper.decompose(self.decomposition_manifest()))
        waves_payload = canonical_bytes(
            helper.plan_waves(self.decomposition_manifest())
        )

        self.assertEqual(payload, waves_payload)
        self.assertEqual(11730, len(payload))
        self.assertEqual(
            "f736b99d55bc562252d1fd6a98fb0f2d12813b0b50ba518f88d4f12c298a7775",
            hashlib.sha256(payload).hexdigest(),
        )

    def test_contract_documents_ultra_auto_policy(self) -> None:
        document = CONTRACT.read_text(encoding="utf-8")

        for expected in (
            "--host-status <fast-lane-host-status.json>",
            "Ultra automatic activation",
            "--enable",
            "协调器 lane 保有设计、集成、风险决策和最终验收责任",
            'action="start"',
            'action="retain"',
            "terminal",
            "only after a terminal event",
            "no commentary polling",
            "no safe useful work",
            "routing_context_hash",
            "routing_result_hash",
            "NO_SAFE_WORK",
            "worker effort 禁止 `ultra`",
            "prewarm 始终是独立的只读证据角色",
            "归档不是 adapter 操作",
            "当前 bootstrap/read-context",
            "CODEX_FASTLANE_TASK_ROOT",
            "task_root_hash",
            "--quota-state-path",
            "默认 `D:\\bun\\tmp\\codex`",
            "不得为卷根",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, document)

    def test_efficiency_reference_documents_host_contract(self) -> None:
        document = (ROOT / "references" / "efficiency-automation.md").read_text(
            encoding="utf-8"
        )

        for expected in (
            "team-efficiency/fast-lane-request-v1",
            "team-efficiency/fast-lane-plan-v1",
            "ultra_auto",
            "explicit_opt_in",
            "--host-status <fast-lane-host-status.json>",
            "Scheduler adapter boundary",
            "routing_context_hash",
            "routing_result_hash",
            "NO_SAFE_WORK",
            "non-`ultra` effort",
            "after coordinator-lane acceptance",
            "C-drive temporary roots are forbidden",
            "target gates",
            "canonical repo anchor",
            "trusted shared integration worktree",
            "git worktree add",
            "Git common directory",
            "post-apply attestation",
            "parked endpoint bootstrap",
            "inert",
            "inert dispatch descriptors",
            "inert projection",
            "do not call host dispatch APIs",
            "no model call",
            "agent spawn",
            "remote service",
            "Git mutation",
            "workflow call",
            "one regression",
            "one blocker review",
            "one global remediation",
            "bootstrap/read-context boundary",
            "CODEX_FASTLANE_TASK_ROOT",
            "task_root_hash",
            "--quota-state-path",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, document)

    def test_bootstrap_defaults_to_a_dry_run_with_only_safe_git_argv(self) -> None:
        helper = load_efficiency()

        plan = helper.build_bootstrap_plan(**self.bootstrap_kwargs())

        expected_target = str(self.bootstrap_kwargs()["worktree"])
        self.assertEqual("dry_run", plan["mode"])
        self.assertEqual("team-efficiency/bootstrap-v1", plan["schema"])
        self.assertNotIn("task_root", plan)
        self.assertNotIn("task_root_hash", plan)
        self.assertEqual(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-b",
                "feature/code-atlas-v1-team-efficiency",
                expected_target,
                "a" * 40,
            ],
            plan["command_argv"],
        )
        self.assertEqual(
            {"workflow_register_task", "workflow_claim", "workflow_endpoint_bind"},
            set(plan["workflow_fields"]),
        )

    def test_bootstrap_uses_the_configured_fastlane_task_root_and_rejects_a_change(
        self,
    ) -> None:
        helper = load_efficiency()
        replacement_root = self.temp / "configured-fastlane-root"
        configured_root = replacement_root / "custom-root-project"
        configured_root.mkdir(parents=True)
        project = "custom-root-project"
        project_root = configured_root / project
        target = self.bootstrap_kwargs()
        target.update(
            {
                "project": project,
                "worktree": project_root / "worktrees" / "atlas12b-team-efficiency",
                "temp_target": project_root / "tasks" / "atlas12b",
            }
        )
        calls: list[tuple[list[str], bool, dict[str, str]]] = []
        probes: list[tuple[list[str], bool, dict[str, str]]] = []

        def runner(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            calls.append((argv, check, env))

        def probe_runner(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            probes.append((argv, check, env))

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(configured_root)},
        ):
            plan = helper.build_bootstrap_plan(**target)
            self.assertEqual("team-efficiency/bootstrap-v2", plan["schema"])
            self.assertIsInstance(plan["task_root_hash"], str)
            self.assertNotIn("task_root", plan)
            self.assertEqual(
                str(project_root.resolve()), plan["temp_target"].rsplit("\\", 2)[0]
            )
            applied = helper.apply_bootstrap_plan(
                plan,
                runner=runner,
                probe_runner=probe_runner,
            )

        self.assertEqual("applied", applied["mode"])
        self.assertEqual(1, len(calls))
        for environment in [calls[0][2], *(env for _argv, _check, env in probes)]:
            self.assertEqual(str(plan["temp_target"]), environment["CODEX_TASK_TEMP"])
            self.assertEqual(
                str(Path(plan["temp_target"]) / "pycache"),
                environment["PYTHONPYCACHEPREFIX"],
            )
            self.assertEqual(
                str(Path(plan["temp_target"]) / "uv-cache"),
                environment["UV_CACHE_DIR"],
            )

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(replacement_root)},
        ):
            with self.assertRaises(ValueError):
                helper.apply_bootstrap_plan(
                    plan,
                    runner=runner,
                    probe_runner=probe_runner,
                )

        self.assertEqual(1, len(calls))

    def test_bootstrap_rejects_an_invalid_configured_fastlane_task_root(self) -> None:
        helper = load_efficiency()
        file_root = self.temp / "configured-root-file"
        file_root.write_text("not a directory", encoding="utf-8")
        invalid_roots = (
            "",
            "relative-root",
            r"D:drive-relative",
            r"C:\\fastlane-root",
            r"\\\\server\\share",
            r"D:\.. ",
            r"D:\ ",
            str(self.temp / "missing-fastlane-root"),
            str(file_root),
        )

        for configured_root in invalid_roots:
            with self.subTest(configured_root=configured_root):
                with mock.patch.dict(
                    os.environ,
                    {"CODEX_FASTLANE_TASK_ROOT": configured_root},
                ):
                    with self.assertRaises(ValueError):
                        helper.build_bootstrap_plan(**self.bootstrap_kwargs())

    def test_bootstrap_rejects_win32_normalization_aliases_in_root_bound_paths(
        self,
    ) -> None:
        helper = load_efficiency()
        configured_root = self.temp / "configured-fastlane-root"
        configured_root.mkdir()
        project = "win32-alias-project"
        project_root = configured_root / project
        target = self.bootstrap_kwargs()
        target.update(
            {
                "project": project,
                "temp_target": project_root / "tasks" / "atlas12b",
            }
        )

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(configured_root)},
        ):
            for alias in ("worktree. ", "CON", "aux.txt"):
                with self.subTest(alias=alias):
                    target["worktree"] = project_root / "worktrees" / alias
                    with self.assertRaises(ValueError):
                        helper.build_bootstrap_plan(**target)

    def test_bootstrap_rejects_lexical_reparse_before_canonicalization(
        self,
    ) -> None:
        helper = load_efficiency()
        configured_root = self.temp / "configured-fastlane-root"
        project_root = configured_root / "lexical-reparse-project"
        lexical = project_root / "alias" / "worktree"
        canonical = project_root / "real" / "worktree"

        def absolute_path(
            _value: object,
            _field: str,
            *,
            root_bound: bool = False,
            resolve_path: bool = True,
        ) -> Path:
            self.assertTrue(root_bound)
            return canonical if resolve_path else lexical

        def lexical_reparse(path: Path, *, missing_ok: bool = False) -> bool:
            return path == project_root / "alias"

        with (
            mock.patch.object(helper, "_absolute_path", side_effect=absolute_path),
            mock.patch.object(
                helper,
                "_path_has_reparse_point",
                side_effect=lexical_reparse,
            ),
        ):
            with self.assertRaises(ValueError):
                helper._root_bound_path(
                    lexical,
                    "worktree",
                    task_root=configured_root,
                    project_root=project_root,
                )

    def test_bootstrap_rejects_a_reparse_project_before_build_and_apply(self) -> None:
        helper = load_efficiency()
        configured_root = self.temp / "configured-fastlane-root"
        configured_root.mkdir()
        project = "reparse-project"
        project_root = configured_root / project
        target = self.bootstrap_kwargs()
        target.update(
            {
                "project": project,
                "worktree": project_root / "worktrees" / "atlas12b-team-efficiency",
                "temp_target": project_root / "tasks" / "atlas12b",
            }
        )

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(configured_root)},
        ):
            plan = helper.build_bootstrap_plan(**target)
            project_root.mkdir()

            def reparse_project(path: Path, *, missing_ok: bool = False) -> bool:
                return path == project_root

            calls: list[tuple[list[str], bool, dict[str, str]]] = []
            with mock.patch.object(
                helper,
                "_path_has_reparse_point",
                side_effect=reparse_project,
            ):
                with self.assertRaises(ValueError):
                    helper.build_bootstrap_plan(**target)
                with self.assertRaises(ValueError):
                    helper.apply_bootstrap_plan(
                        plan,
                        runner=lambda argv, *, check, env: calls.append(
                            (argv, check, env)
                        ),
                        probe_runner=lambda argv, *, check, env: None,
                    )

        self.assertEqual([], calls)

    def test_bootstrap_apply_rechecks_root_bound_descendants_before_mutation(
        self,
    ) -> None:
        helper = load_efficiency()
        configured_root = self.temp / "configured-fastlane-root"
        configured_root.mkdir()
        project = "race-checked-project"
        project_root = configured_root / project
        target = self.bootstrap_kwargs()
        target.update(
            {
                "project": project,
                "worktree": project_root / "worktrees" / "atlas12b-team-efficiency",
                "temp_target": project_root / "tasks" / "atlas12b",
            }
        )
        calls: list[tuple[list[str], bool, dict[str, str]]] = []

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(configured_root)},
        ):
            plan = helper.build_bootstrap_plan(**target)

            def reparse_worktree_parent(
                path: Path,
                *,
                missing_ok: bool = False,
            ) -> bool:
                return path == project_root / "worktrees"

            with mock.patch.object(
                helper,
                "_path_has_reparse_point",
                side_effect=reparse_worktree_parent,
            ):
                with self.assertRaises(ValueError):
                    helper.apply_bootstrap_plan(
                        plan,
                        runner=lambda argv, *, check, env: calls.append(
                            (argv, check, env)
                        ),
                        probe_runner=lambda argv, *, check, env: None,
                    )

        self.assertEqual([], calls)

    def test_bootstrap_apply_pins_root_bound_directories_through_runner(self) -> None:
        helper = load_efficiency()
        configured_root = self.temp / "configured-fastlane-root"
        configured_root.mkdir()
        project = "pinned-project"
        project_root = configured_root / project
        target = self.bootstrap_kwargs()
        target.update(
            {
                "project": project,
                "worktree": project_root / "worktrees" / "atlas12b-team-efficiency",
                "temp_target": project_root / "tasks" / "atlas12b",
            }
        )
        pinned: list[tuple[Path, str]] = []
        calls: list[tuple[list[str], bool, dict[str, str]]] = []

        def pin(path: Path, field: str) -> None:
            pinned.append((path, field))
            return None

        def runner(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            pinned_paths = {path for path, _field in pinned}
            self.assertIn(configured_root, pinned_paths)
            self.assertIn(project_root, pinned_paths)
            self.assertIn(project_root / "tasks" / "atlas12b", pinned_paths)
            self.assertIn(project_root / "worktrees", pinned_paths)
            self.assertIn(
                project_root / "worktrees" / "atlas12b-team-efficiency",
                pinned_paths,
            )
            self.assertTrue(
                (project_root / "worktrees" / "atlas12b-team-efficiency").is_dir()
            )
            self.assertEqual(
                [],
                list(
                    (project_root / "worktrees" / "atlas12b-team-efficiency").iterdir()
                ),
            )
            calls.append((argv, check, env))

        with mock.patch.dict(
            os.environ,
            {"CODEX_FASTLANE_TASK_ROOT": str(configured_root)},
        ):
            plan = helper.build_bootstrap_plan(**target)
            with mock.patch.object(
                helper,
                "_pin_windows_directory",
                side_effect=pin,
            ):
                helper.apply_bootstrap_plan(
                    plan,
                    runner=runner,
                    probe_runner=lambda argv, *, check, env: None,
                )

        self.assertEqual(1, len(calls))

    def test_bootstrap_is_portable_to_any_compliant_codex_project_root(self) -> None:
        helper = load_efficiency()
        codex_root = Path(r"D:\bun\tmp\codex").resolve(strict=False)
        project_root = self.fast_lane_task_root / "portable-project" / "nested-root"
        project = project_root.resolve(strict=False).relative_to(codex_root).as_posix()
        worktree = project_root / "worktrees" / "portable-atlas"
        temp_target = project_root / "tasks" / "portable-atlas"

        plan = helper.build_bootstrap_plan(
            task_id="ATLAS-12B",
            base_commit="a" * 40,
            branch="feature/code-atlas-v1-team-efficiency",
            write_scope=["mcp-tools/devkit_fastlane/scripts/team_efficiency.py"],
            repo=self.repo,
            project=project,
            worktree=worktree,
            temp_target=temp_target,
        )

        self.assertEqual(project, plan["project"])
        self.assertEqual(str(worktree.resolve(strict=False)), plan["worktree"])
        self.assertEqual(str(temp_target.resolve(strict=False)), plan["temp_target"])

    def test_bootstrap_apply_uses_the_validated_argument_vector_only(self) -> None:
        helper = load_efficiency()
        apply_target = self.bootstrap_kwargs()
        apply_target["worktree"] = self.fast_lane_task_root / "apply-worktree"
        plan = helper.build_bootstrap_plan(**apply_target)
        calls: list[tuple[list[str], bool, dict[str, str]]] = []
        probes: list[tuple[list[str], bool, dict[str, str]]] = []

        def runner(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            calls.append((argv, check, env))

        def probe_runner(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            probes.append((argv, check, env))

        applied = helper.apply_bootstrap_plan(
            plan,
            runner=runner,
            probe_runner=probe_runner,
        )

        self.assertEqual("applied", applied["mode"])
        self.assertEqual([(plan["command_argv"], True, calls[0][2])], calls)
        self.assertEqual(
            [
                ["git", "-C", str(self.repo), "rev-parse", "--git-dir"],
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "rev-parse",
                    "--verify",
                    f"{'a' * 40}^{{commit}}",
                ],
            ],
            [argv for argv, _check, _env in probes],
        )
        for environment in [calls[0][2], *(env for _argv, _check, env in probes)]:
            for name in ("TEMP", "TMP", "TMPDIR", "CODEX_TASK_TEMP"):
                self.assertEqual(str(plan["temp_target"]), environment[name])
        self.assertTrue(Path(plan["temp_target"]).is_dir())

    def test_bootstrap_apply_stops_before_worktree_add_when_preflight_fails(
        self,
    ) -> None:
        helper = load_efficiency()
        apply_target = self.bootstrap_kwargs()
        apply_target["worktree"] = self.fast_lane_task_root / "failed-apply-worktree"
        plan = helper.build_bootstrap_plan(**apply_target)
        applied: list[list[str]] = []

        def failed_probe(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            raise subprocess.CalledProcessError(1, argv)

        def runner(argv: list[str], *, check: bool, env: dict[str, str]) -> None:
            applied.append(argv)

        with self.assertRaises(subprocess.CalledProcessError):
            helper.apply_bootstrap_plan(
                plan,
                runner=runner,
                probe_runner=failed_probe,
            )

        self.assertEqual([], applied)

    def test_bootstrap_rejects_traversal_and_existing_target_before_apply(self) -> None:
        helper = load_efficiency()
        unsafe_branch = self.bootstrap_kwargs()
        unsafe_branch["branch"] = "feature/../escape"
        with self.assertRaises(ValueError):
            helper.build_bootstrap_plan(**unsafe_branch)

        unsafe_scope = self.bootstrap_kwargs()
        unsafe_scope["write_scope"] = ["../outside.py"]
        with self.assertRaises(ValueError):
            helper.build_bootstrap_plan(**unsafe_scope)

        escaped_target = self.bootstrap_kwargs()
        escaped_target["worktree"] = self.fast_lane_task_root.parent.parent / "outside"
        with self.assertRaises(ValueError):
            helper.build_bootstrap_plan(**escaped_target)

        existing_target = self.bootstrap_kwargs()
        existing_target["worktree"] = self.fast_lane_task_root / "existing-worktree"
        plan = helper.build_bootstrap_plan(**existing_target)
        target = Path(plan["worktree"])
        target.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            helper.apply_bootstrap_plan(plan, runner=lambda *_args, **_kwargs: None)

    def test_resume_packet_is_canonical_bounded_and_secret_safe(self) -> None:
        helper = load_efficiency()
        packet = self.resume_packet()

        first = helper.canonical_resume_packet(packet)
        second = helper.canonical_resume_packet(dict(reversed(packet.items())))

        self.assertEqual(first, second)
        self.assertEqual(packet, json.loads(first))
        secret_packet = copy.deepcopy(packet)
        secret_packet["token"] = "not allowed"
        with self.assertRaises(ValueError):
            helper.canonical_resume_packet(secret_packet)

        path_packet = copy.deepcopy(packet)
        path_packet["next_action"] = r"read C:\outside\raw.log"
        with self.assertRaises(ValueError):
            helper.canonical_resume_packet(path_packet)

        oversized = json.dumps(packet).encode("utf-8") + b" " * helper.MAX_PACKET_BYTES
        with self.assertRaises(ValueError):
            helper.parse_resume_packet(oversized)

        raw_summary = copy.deepcopy(packet)
        raw_summary["latest_green"]["output"] = "unbounded test output"
        with self.assertRaises(ValueError):
            helper.canonical_resume_packet(raw_summary)

    def test_status_markdown_keeps_pending_initialization_out_of_active_count(
        self,
    ) -> None:
        helper = load_efficiency()
        packet = self.resume_packet()
        snapshot = {
            "workflow": {
                "workflow_id": "atlas-v03",
                "tasks": [
                    {
                        "task_id": "ATLAS-12A",
                        "state": "pending_init",
                        "branch": "feature/atlas12a",
                    },
                    {
                        "task_id": "ATLAS-12B",
                        "state": "running",
                        "branch": "feature/atlas12b",
                    },
                    {
                        "task_id": "ATLAS-12C",
                        "state": "blocked",
                        "branch": "feature/atlas12c",
                    },
                    {
                        "task_id": "ATLAS-12D",
                        "state": "done",
                        "branch": "feature/atlas12d",
                    },
                ],
            },
            "leases": [
                {
                    "task_id": "ATLAS-12B",
                    "lease_epoch": 2,
                    "endpoint": "/root/atlas12b_worker",
                }
            ],
            "resume_packets": [packet],
        }

        markdown = helper.render_status_markdown(snapshot)

        self.assertIn("Active parallel execution: 1", markdown)
        self.assertIn("Pending initialization: 1", markdown)
        self.assertIn("| ATLAS-12A | pending_init |", markdown)
        self.assertIn("| ATLAS-12B | running |", markdown)
        self.assertIn(f"sha256:{'e' * 64}", markdown)
        self.assertNotIn("python -m unittest focused", markdown)

        injected = copy.deepcopy(snapshot)
        injected["resume_packets"][0]["latest_green"]["result"] = (
            "12 passed | [details](not-a-link)"
        )
        rendered = helper.render_status_markdown(injected)
        self.assertIn("12 passed \\| \\[details\\]\\(not-a-link\\)", rendered)

        reordered = copy.deepcopy(snapshot)
        reordered["workflow"]["tasks"].reverse()
        self.assertEqual(markdown, helper.render_status_markdown(reordered))

    def test_contract_check_fails_closed_on_artifact_or_schema_mismatch(self) -> None:
        helper = load_efficiency()
        producer = {
            "schema": "team-efficiency/v1",
            "artifact_hash": f"sha256:{'a' * 64}",
        }
        consumer = dict(producer)

        self.assertEqual(
            {"compatible": True, "schema": "team-efficiency/v1"},
            helper.contract_check(producer, consumer),
        )
        wrong_hash = dict(consumer)
        wrong_hash["artifact_hash"] = f"sha256:{'b' * 64}"
        with self.assertRaises(helper.ContractMismatchError):
            helper.contract_check(producer, wrong_hash)

        wrong_schema = dict(consumer)
        wrong_schema["schema"] = "team-efficiency/v2"
        with self.assertRaises(helper.ContractMismatchError):
            helper.contract_check(producer, wrong_schema)

    def test_cache_metadata_requires_an_exact_complete_fingerprint(self) -> None:
        helper = load_efficiency()
        inputs = self.cache_inputs()

        metadata = helper.make_cache_metadata(inputs)
        reordered = dict(reversed(inputs.items()))
        self.assertEqual(metadata, helper.make_cache_metadata(reordered))
        self.assertTrue(helper.is_exact_cache_hit(inputs, metadata))

        changed_tree = copy.deepcopy(inputs)
        changed_tree["candidate_tree"] = "f" * 40
        self.assertFalse(helper.is_exact_cache_hit(changed_tree, metadata))

        incomplete = copy.deepcopy(inputs)
        incomplete["platform_fingerprint"] = "partial-windows"
        with self.assertRaises(ValueError):
            helper.make_cache_metadata(incomplete)

        tampered = copy.deepcopy(metadata)
        tampered["fingerprint"]["test_lane"] = "extended"
        self.assertFalse(helper.is_exact_cache_hit(inputs, tampered))

    def test_decompose_emits_maximal_deterministic_capacity_bounded_waves(self) -> None:
        helper = load_efficiency()
        manifest = self.decomposition_manifest()

        plan = helper.decompose(manifest)
        reordered = copy.deepcopy(manifest)
        reordered["artifacts"].reverse()

        self.assertEqual(plan, helper.plan_waves(reordered))
        self.assertEqual("planned", plan["status"])
        self.assertEqual("explicit_artifact_boundaries", plan["source_kind"])
        self.assertEqual(
            [
                ["ATLAS-12B-A", "ATLAS-12B-B"],
                ["ATLAS-12B-C"],
            ],
            [[unit["task_id"] for unit in wave] for wave in plan["waves"]],
        )
        self.assertEqual("Terra High", plan["waves"][0][0]["recommended_route"])
        self.assertEqual("Terra Max", plan["waves"][0][1]["recommended_route"])
        self.assertTrue(
            all(not unit["direct_contract_hashes"] for unit in plan["units"])
        )

    def test_packet_mode_derives_stable_units_without_declared_artifacts(self) -> None:
        helper = load_efficiency()
        manifest = self.code_atlas_manifest()

        plan = helper.decompose(manifest)
        reordered = copy.deepcopy(manifest)
        reordered["packet"] = dict(reversed(reordered["packet"].items()))
        reordered["path_bindings"] = dict(reversed(reordered["path_bindings"].items()))

        self.assertEqual(plan, helper.plan_waves(reordered))
        self.assertEqual(
            set(ImplementationPacket.__dataclass_fields__),
            set(manifest["packet"]),
        )
        self.assertEqual(
            set(TemplateOperation.__dataclass_fields__),
            set(manifest["packet"]["operations"][0]),
        )
        self.assertEqual(
            set(SlotSpec.__dataclass_fields__),
            set(manifest["packet"]["slots"][0]),
        )
        self.assertEqual(
            set(ConstraintSpec.__dataclass_fields__),
            set(manifest["packet"]["constraints"][0]),
        )
        self.assertEqual(
            set(DependencySpec.__dataclass_fields__),
            set(manifest["packet"]["dependencies"][0]),
        )
        self.assertEqual(
            set(AtlasTestSpec.__dataclass_fields__),
            set(manifest["packet"]["tests"][0]),
        )
        self.assertEqual("planned", plan["status"])
        self.assertEqual("code_atlas_packet", plan["source_kind"])
        self.assertNotIn("artifacts", manifest)
        self.assertEqual(5, len(plan["units"]))
        by_scope = {
            unit["write_scope"][0]: unit
            for unit in plan["units"]
            if unit["write_scope"]
        }
        service = by_scope["src/service.py"]
        verification = next(
            unit for unit in plan["units"] if unit["unit_kind"] == "verification"
        )
        self.assertEqual(2, service["operation_count"])
        self.assertIn(f"sha256:{'4' * 64}", service["required_evidence"])
        self.assertIn(f"sha256:{'5' * 64}", service["required_evidence"])
        self.assertEqual(
            {unit["task_id"] for unit in plan["units"] if unit["unit_kind"] == "code"},
            set(verification["depends_on"]),
        )
        self.assertEqual(1, len(verification["acceptance_constraints"]))
        expected_contract = canonical_hash(
            {
                "kind": "code_atlas_packet_execution_contract_v1",
                "packet_id": manifest["packet"]["packet_id"],
                "recipe_id": manifest["packet"]["recipe_id"],
            }
        )
        self.assertEqual(
            [expected_contract],
            service["direct_contract_hashes"],
        )
        self.assertEqual(
            service["direct_contract_hashes"],
            verification["direct_contract_hashes"],
        )
        self.assertEqual(
            {
                "docs/service.md",
                "src/schema.py",
                "src/service.py",
                "tests/test_service.py",
            },
            {unit["write_scope"][0] for unit in plan["waves"][0]},
        )
        self.assertEqual(
            ["verification"],
            [unit["unit_kind"] for unit in plan["waves"][1]],
        )
        self.assertEqual(
            len(plan["units"]),
            len({unit["task_id"] for unit in plan["units"]}),
        )
        self.assertEqual(
            len(plan["units"]),
            len({unit["output_boundary"] for unit in plan["units"]}),
        )
        for unit in plan["units"]:
            self.assertTrue(unit["execution_contracts"])
            self.assertTrue(
                unit["task_id"].startswith(("ATLAS-12B-P-", "ATLAS-12B-V-"))
            )

    def test_packet_mode_fails_closed_on_missing_or_private_evidence(self) -> None:
        helper = load_efficiency()

        missing = self.code_atlas_manifest()
        missing["packet"]["evidence_hashes"] = []
        packet_payload = dict(missing["packet"])
        del packet_payload["packet_id"]
        missing["packet"]["packet_id"] = canonical_hash(packet_payload)
        self.assertEqual("needs_design", helper.decompose(missing)["status"])

        unbound = self.code_atlas_manifest()
        del unbound["path_bindings"]["tests"]
        self.assertEqual("needs_design", helper.decompose(unbound)["status"])

        unsafe = self.code_atlas_manifest()
        unsafe["path_bindings"]["tests"] = "../outside.py"
        with self.assertRaises(ValueError):
            helper.decompose(unsafe)

        raw_source = self.code_atlas_manifest()
        raw_source["packet"]["source"] = "private implementation source"
        with self.assertRaises(ValueError):
            helper.decompose(raw_source)

        invented_contracts = self.code_atlas_manifest()
        invented_contracts["packet"]["execution_contract_hashes"] = [
            f"sha256:{'a' * 64}"
        ]
        with self.assertRaises(ValueError):
            helper.decompose(invented_contracts)

        raw_template = self.code_atlas_manifest()
        raw_template["packet"]["operations"][0]["template_content"] = "render me"
        with self.assertRaises(ValueError):
            helper.decompose(raw_template)

        command_output = self.code_atlas_manifest()
        command_output["packet"]["tests"][0]["command_output"] = "1 passed"
        with self.assertRaises(ValueError):
            helper.decompose(command_output)

    def test_task_episode_mode_derives_graph_units_dependencies_and_conflicts(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.task_episode_manifest()

        plan = helper.decompose(manifest)
        reordered = copy.deepcopy(manifest)
        reordered["graph"]["nodes"].reverse()
        reordered["graph"]["edges"].reverse()

        self.assertEqual(plan, helper.plan_waves(reordered))
        self.assertEqual(
            set(GraphQueryResult.__dataclass_fields__),
            set(manifest["graph"]),
        )
        self.assertEqual(
            set(AtlasNode.__dataclass_fields__),
            set(manifest["graph"]["nodes"][0]),
        )
        self.assertEqual(
            set(AtlasEdge.__dataclass_fields__),
            set(manifest["graph"]["edges"][0]),
        )
        self.assertTrue(
            {"TaskEpisode", "AdaptationSlot", "SourceEvidence"}
            <= {node["kind"] for node in manifest["graph"]["nodes"]}
        )
        self.assertTrue(
            {"CHANGES", "REQUIRES", "VERIFIED_BY"}
            <= {edge["relation"] for edge in manifest["graph"]["edges"]}
        )
        self.assertEqual("planned", plan["status"])
        self.assertEqual("task_episode_graph", plan["source_kind"])
        self.assertNotIn("artifacts", manifest)
        self.assertEqual(
            {"docs/core.py", "src/core.py"},
            {unit["write_scope"][0] for unit in plan["waves"][0]},
        )
        self.assertEqual(
            ["verification"],
            [unit["unit_kind"] for unit in plan["waves"][1]],
        )
        by_scope = {
            unit["write_scope"][0]: unit
            for unit in plan["units"]
            if unit["write_scope"]
        }
        verification = next(
            unit for unit in plan["units"] if unit["unit_kind"] == "verification"
        )
        self.assertEqual(
            {by_scope["src/core.py"]["task_id"], by_scope["docs/core.py"]["task_id"]},
            set(verification["depends_on"]),
        )
        self.assertEqual("Terra Max", verification["recommended_route"])
        core_contracts = by_scope["src/core.py"]["direct_contract_hashes"]
        docs_contracts = by_scope["docs/core.py"]["direct_contract_hashes"]
        self.assertEqual(1, len(core_contracts))
        self.assertEqual(1, len(docs_contracts))
        self.assertEqual([], by_scope["src/core.py"]["depends_on"])
        self.assertEqual([], by_scope["docs/core.py"]["depends_on"])
        self.assertNotEqual(core_contracts, docs_contracts)
        self.assertEqual(
            sorted(core_contracts + docs_contracts),
            verification["direct_contract_hashes"],
        )
        for unit in plan["units"]:
            self.assertTrue(
                unit["task_id"].startswith(("ATLAS-12B-E-", "ATLAS-12B-V-"))
            )
            self.assertTrue(unit["required_evidence"])
            self.assertTrue(unit["execution_contracts"])

        conflict = self.task_episode_manifest(docs_path="src/core.py")
        conflict_plan = helper.decompose(conflict)
        conflict_units = [
            unit for unit in conflict_plan["units"] if unit["unit_kind"] == "code"
        ]
        self.assertEqual(2, len(conflict_units))
        build_id, docs_id = sorted(unit["task_id"] for unit in conflict_units)
        self.assertIn(build_id, conflict_plan["conflict_graph"][docs_id])
        self.assertFalse(
            any(
                {build_id, docs_id} <= {unit["task_id"] for unit in wave}
                for wave in conflict_plan["waves"]
            )
        )
        self.assertEqual("verification", conflict_plan["waves"][-1][0]["unit_kind"])

    def test_external_lineage_supersedes_does_not_create_episode_ordering(self) -> None:
        helper = load_efficiency()
        dependent = helper.decompose(self.task_episode_manifest(supersedes=True))

        self.assertTrue(
            all(
                not unit["depends_on"]
                for unit in dependent["units"]
                if unit["unit_kind"] == "code"
            ),
            "External Recipe SUPERSEDES evidence must not invent TaskEpisode task order",
        )

    def test_one_declared_extractor_edge_fails_closed(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.declared_edge_episode_manifest()
        payload_kinds = {
            node["payload"].get("kind")
            for node in manifest["graph"]["nodes"]
            if isinstance(node["payload"], dict)
        }

        self.assertTrue(manifest["eligible"])
        self.assertTrue(
            {
                "bound_verification",
                "command_receipt",
                "bound_receipt_summary",
                "command",
                "write",
            }
            <= payload_kinds
        )
        self.assertEqual(
            "ATLAS_EDGE_UNVERIFIED",
            helper.decompose(manifest)["reason"],
        )

    def test_real_extractor_store_graph_decomposes_and_executes_full_lifecycle(
        self,
    ) -> None:
        helper = load_efficiency()
        indexed_path = "src/atlas_guard.py"
        request = _extractor_request(path=indexed_path)
        self.assertEqual(1, len(request.after_files))
        after_file = request.after_files[0]
        self.assertEqual(indexed_path, after_file.path)
        repository_root = self.temp / "atlas-e2e-repository"
        workspace_root = self.temp / "atlas-e2e-workspace"
        subprocess.run(
            ["git", "init", str(repository_root)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(repository_root), "config", "user.name", "e2e test"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "config",
                "user.email",
                "e2e@example.invalid",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "commit",
                "--allow-empty",
                "-m",
                "initial",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "worktree",
                "add",
                "--detach",
                str(workspace_root),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        workspace_file = workspace_root / indexed_path
        workspace_file.parent.mkdir(parents=True, exist_ok=True)
        workspace_file.write_bytes(after_file.body)
        result = PythonRecipeExtractor().extract(request)
        self.assertTrue(result.eligible)
        self.assertTrue(result.episode_id)

        atlas_store = AtlasStore(
            self.temp / "atlas-e2e.sqlite", self.temp / "atlas-cas"
        )
        try:
            atlas_store.put_nodes(result.nodes)
            atlas_store.put_edges(result.edges)
            graph = atlas_store.graph_query((result.episode_id,))
        finally:
            atlas_store.close()

        self.assertFalse(graph.truncated)
        self.assertEqual(result.nodes, graph.nodes)
        self.assertEqual(result.edges, graph.edges)
        manifest = {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Compile one persisted extractor TaskEpisode",
            "capacity": 2,
            "decomposition": "atlas_evidence",
            "source_kind": "task_episode_graph",
            "eligible": result.eligible,
            "graph": graph.to_dict(),
        }

        plan = helper.decompose(manifest)

        self.assertEqual("planned", plan["status"])
        code_unit = next(unit for unit in plan["units"] if unit["unit_kind"] == "code")
        self.assertEqual([indexed_path], code_unit["write_scope"])
        self.assertTrue(code_unit["contract_node_ids"])
        registration = json.dumps(plan["registration_plan"], sort_keys=True)
        for sensitive_identity in (
            _marker_hash("workspace-primary"),
            _marker_hash("input-primary"),
            _marker_hash("output-primary"),
        ):
            self.assertNotIn(sensitive_identity, registration)
        runtime_config = RuntimeConfig(
            data_root=self.temp / "orchestrator-runtime",
            scratch_root=self.temp / "orchestrator-runtime-scratch",
        )
        RuntimeBootstrap.run(runtime_config)
        project_runtime = open_project_checkpoint_rw(
            runtime_config.project_index_database,
            runtime_config.checkpoint_cas_root,
            scratch_root=runtime_config.scratch_root,
        )
        self._project_runtimes.append(project_runtime)
        index_service = project_runtime.project_index
        workspace_id = index_service.project_index_register(workspace_root)
        snapshot = index_service.sync(workspace_id, include_paths=(indexed_path,))
        self.assertEqual(1, snapshot.file_count)
        snapshot_facts = index_service.snapshot_facts(
            workspace_id, snapshot.snapshot_id
        )
        (indexed_file,) = index_service.read_snapshot_files(
            workspace_id,
            snapshot.snapshot_id,
            (indexed_path,),
            byte_budget=64 * 1024,
        )
        source_evidence = next(
            node
            for node in graph.nodes
            if node.kind is NodeKind.SOURCE_EVIDENCE
            and node.to_dict()["payload"].get("path") == indexed_path
        )
        source_payload = source_evidence.to_dict()["payload"]
        self.assertEqual(
            ((indexed_path, indexed_file.content_hash),), snapshot_facts.file_hashes
        )
        self.assertEqual(after_file.body, indexed_file.body)
        self.assertEqual(after_file.content_hash, indexed_file.content_hash)
        self.assertEqual(source_payload["after_hash"], after_file.content_hash)
        self.assertEqual(source_payload["after_hash"], indexed_file.content_hash)
        self.assertEqual(_content_hash(indexed_file.body), indexed_file.content_hash)
        workflow_id = "extractor-e2e-lifecycle"
        durable_store, service = self.orchestrator(
            "extractor-e2e-lifecycle.sqlite",
            workflow_id,
            index_service=index_service,
        )
        self.execute_lifecycle(
            plan=plan,
            store=durable_store,
            service=service,
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            input_snapshot_id=snapshot.snapshot_id,
            index_service=index_service,
        )
        self.assertEqual(
            {TaskState.DONE},
            {task.state for task in durable_store.list_tasks(workflow_id)},
        )

    def test_graph_trust_failures_return_machine_readable_needs_design(
        self,
    ) -> None:
        helper = load_efficiency()

        ineligible = self.extractor_episode_manifest(
            command_success=False,
        )
        self.assertEqual(
            "ATLAS_EXTRACTION_INELIGIBLE",
            helper.decompose(ineligible)["reason"],
        )

        failed_receipt = self.extractor_episode_manifest(
            command_success=False,
            eligible_override=True,
        )
        self.assertEqual(
            "ATLAS_RECEIPT_UNVERIFIED",
            helper.decompose(failed_receipt)["reason"],
        )

        incomplete_hash = self.extractor_episode_manifest(
            complete_receipt_hashes=False,
            eligible_override=True,
        )
        self.assertEqual(
            "ATLAS_RECEIPT_UNVERIFIED",
            helper.decompose(incomplete_hash)["reason"],
        )

        bad_exit = self.extractor_episode_manifest(
            command_exit_code=1,
            eligible_override=True,
        )
        self.assertEqual(
            "ATLAS_RECEIPT_UNVERIFIED",
            helper.decompose(bad_exit)["reason"],
        )

        declared_node = self.extractor_episode_manifest()
        episode = next(
            node
            for node in declared_node["graph"]["nodes"]
            if node["kind"] == "TaskEpisode"
        )
        episode["provenance"] = "declared"
        self.assertEqual(
            "ATLAS_NODE_UNVERIFIED",
            helper.decompose(declared_node)["reason"],
        )

        declared_source = self.extractor_episode_manifest()
        source_node = next(
            node
            for node in declared_source["graph"]["nodes"]
            if node["kind"] == "SourceEvidence"
        )
        source_node["provenance"] = "declared"
        self.assertEqual(
            "ATLAS_NODE_UNVERIFIED",
            helper.decompose(declared_source)["reason"],
        )

        quarantined = self.extractor_episode_manifest()
        episode = next(
            node
            for node in quarantined["graph"]["nodes"]
            if node["kind"] == "TaskEpisode"
        )
        episode["quarantine_state"] = "review"
        self.assertEqual(
            "ATLAS_NODE_QUARANTINED",
            helper.decompose(quarantined)["reason"],
        )

        superseded = self.extractor_episode_manifest()
        episode = next(
            node
            for node in superseded["graph"]["nodes"]
            if node["kind"] == "TaskEpisode"
        )
        episode["superseded_at"] = "2026-07-29T00:00:00Z"
        self.assertEqual(
            "ATLAS_NODE_SUPERSEDED",
            helper.decompose(superseded)["reason"],
        )

        for node_kind, field, value, reason in (
            (
                "SourceEvidence",
                "quarantine_state",
                "review",
                "ATLAS_NODE_QUARANTINED",
            ),
            (
                "TestSpec",
                "superseded_at",
                "2026-07-29T00:00:00Z",
                "ATLAS_NODE_SUPERSEDED",
            ),
            (
                "ExecutionReceipt",
                "quarantine_state",
                "review",
                "ATLAS_NODE_QUARANTINED",
            ),
        ):
            with self.subTest(node_kind=node_kind, field=field):
                untrusted = self.extractor_episode_manifest()
                participant = next(
                    node
                    for node in untrusted["graph"]["nodes"]
                    if node["kind"] == node_kind
                )
                participant[field] = value
                self.assertEqual(
                    reason,
                    helper.decompose(untrusted)["reason"],
                )

    def test_graph_execution_contract_hash_ignores_time_metadata_only(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.extractor_episode_manifest()
        first = helper.decompose(manifest)
        changed_times = copy.deepcopy(manifest)
        for node in changed_times["graph"]["nodes"]:
            node["created_at"] = "2026-07-29T01:02:03Z"
        for edge in changed_times["graph"]["edges"]:
            edge["created_at"] = "2026-07-29T04:05:06Z"

        second = helper.decompose(changed_times)

        first_contracts = sorted(
            unit["direct_contract_hashes"] for unit in first["units"]
        )
        second_contracts = sorted(
            unit["direct_contract_hashes"] for unit in second["units"]
        )
        self.assertEqual(first_contracts, second_contracts)

    def test_graph_input_budget_accepts_default_query_size_and_fails_closed(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.task_episode_manifest()

        self.assertGreaterEqual(helper.MAX_MANIFEST_BYTES, 65_536)
        self.assertGreater(
            len(json.dumps(manifest).encode("utf-8")),
            34_291,
        )
        self.assertLessEqual(
            len(json.dumps(manifest).encode("utf-8")),
            helper.MAX_MANIFEST_BYTES,
        )
        self.assertEqual("planned", helper.decompose(manifest)["status"])

        oversized = copy.deepcopy(manifest)
        oversized["padding"] = "x" * helper.MAX_MANIFEST_BYTES
        plan = helper.decompose(oversized)
        self.assertEqual("needs_design", plan["status"])
        self.assertEqual("ATLAS_INPUT_BUDGET_EXCEEDED", plan["reason"])
        oversized_path = self.temp / "oversized-graph.json"
        oversized_path.write_text(json.dumps(oversized), encoding="utf-8")
        output = io.StringIO()
        errors = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            exit_code = helper.main(["decompose", "--input", str(oversized_path)])
        self.assertEqual(0, exit_code)
        self.assertEqual("", errors.getvalue())
        self.assertEqual(
            "ATLAS_INPUT_BUDGET_EXCEEDED",
            json.loads(output.getvalue())["reason"],
        )

    def test_registration_plan_registers_dependencies_before_dependents(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.decomposition_manifest()
        manifest["artifacts"][0]["depends_on"] = ["ATLAS-12B-B"]
        manifest["artifacts"] = manifest["artifacts"][:2]
        plan = helper.decompose(manifest)
        workflow_id = "dependency-registration"
        store, service = self.orchestrator(
            "dependency-registration.sqlite",
            workflow_id,
        )
        lifecycle = plan["registration_plan"]

        self.assertEqual(
            ["ATLAS-12B-B", "ATLAS-12B-A"],
            lifecycle["registration_order"],
        )
        self.assertEqual(
            lifecycle["registration_order"],
            [
                descriptor["arguments"]["task_id"]
                for descriptor in lifecycle["register_steps"]
            ],
        )
        self.assertEqual(
            [["ATLAS-12B-B"], ["ATLAS-12B-A"]],
            [[unit["task_id"] for unit in wave] for wave in plan["waves"]],
        )
        self.assertEqual(
            [["ATLAS-12B-B"], ["ATLAS-12B-A"]],
            [wave["task_ids"] for wave in lifecycle["execution_waves"]],
        )
        self.assertEqual(
            [["ATLAS-12B-B"], ["ATLAS-12B-A"]],
            self.execute_lifecycle(
                plan=plan,
                store=store,
                service=service,
                workflow_id=workflow_id,
            ),
        )

    def test_lifecycle_ready_allows_capacity_split_tasks_already_ready(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.decomposition_manifest()
        manifest["artifacts"][2]["depends_on"] = []
        plan = helper.decompose(manifest)
        workflow_id = "capacity-split-ready"
        store, service = self.orchestrator(
            "capacity-split-ready.sqlite",
            workflow_id,
        )
        lifecycle = plan["registration_plan"]

        self.assertEqual(
            [["ATLAS-12B-A", "ATLAS-12B-B"], ["ATLAS-12B-C"]],
            [wave["task_ids"] for wave in lifecycle["execution_waves"]],
        )
        for wave in lifecycle["execution_waves"]:
            self.assertEqual(
                {
                    "allow_empty_result": True,
                    "require_exact_task_set": False,
                    "claim_precondition": "READY",
                },
                wave["ready_result_policy"],
            )
        self.assertEqual(
            [
                ["ATLAS-12B-A", "ATLAS-12B-B", "ATLAS-12B-C"],
                [],
            ],
            self.execute_lifecycle(
                plan=plan,
                store=store,
                service=service,
                workflow_id=workflow_id,
            ),
        )

    def test_planned_units_emit_versioned_executable_registration_calls(
        self,
    ) -> None:
        helper = load_efficiency()

        manifest = self.code_atlas_manifest()
        plan = helper.decompose(manifest)

        registration = plan["registration_plan"]
        self.assertEqual(
            "team-efficiency/workflow-lifecycle-plan-v1",
            registration["schema"],
        )
        top_level_waves = [[unit["task_id"] for unit in wave] for wave in plan["waves"]]
        self.assertEqual(
            [task_id for wave in top_level_waves for task_id in wave],
            registration["registration_order"],
        )
        self.assertEqual(
            top_level_waves,
            [wave["task_ids"] for wave in registration["execution_waves"]],
        )
        self.assertEqual(len(plan["units"]), len(registration["register_steps"]))
        first = registration["register_steps"][0]
        self.assertEqual(
            {"tool", "arguments", "host_bound_fields"},
            set(first),
        )
        self.assertEqual(
            "workflow_register_task",
            first["tool"],
        )
        register_signature = {
            "workflow_id",
            "task_id",
            "title",
            "owner_role",
            "card",
            "dependencies",
            "write_scope",
            "direct_contract_hashes",
            "required_evidence",
            "input_hash",
            "strict_index",
            "workspace_root",
            "input_snapshot_id",
            "task_node_ids",
            "contract_node_ids",
        }
        self.assertEqual(register_signature, set(first["arguments"]))
        self.assertIsInstance(
            first["arguments"]["card"],
            str,
        )
        server_tree = ast.parse(
            (ROOT.parents[1] / "mcp-tools" / "server.py").read_text(encoding="utf-8")
        )
        internal_workflow_signatures = {
            "workflow_register_task": register_signature,
            "workflow_ready": {"workflow_id"},
            "workflow_claim": {
                "task_id",
                "owner",
                "expires_at",
                "host_target",
                "now",
            },
            "workflow_endpoint_bind": {
                "workflow_id",
                "task_id",
                "owner",
                "lease_epoch",
                "host_target",
                "now",
            },
        }
        internal_workflow_required = {
            "workflow_register_task": {
                "workflow_id",
                "task_id",
                "title",
                "owner_role",
                "card",
            },
            "workflow_ready": {"workflow_id"},
            "workflow_claim": {"task_id", "owner", "expires_at"},
            "workflow_endpoint_bind": {
                "workflow_id",
                "task_id",
                "owner",
                "lease_epoch",
                "host_target",
            },
        }
        public_workflow_functions = {
            node.name
            for node in server_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in internal_workflow_signatures
        }
        self.assertEqual(set(), public_workflow_functions)
        self.assertEqual(
            {"workflow_id"}, internal_workflow_signatures["workflow_ready"]
        )
        self.assertEqual({"workflow_id"}, internal_workflow_required["workflow_ready"])
        self.assertEqual(
            {
                "workflow_id",
                "task_id",
                "title",
                "owner_role",
                "card",
            },
            internal_workflow_required["workflow_register_task"],
        )
        self.assertEqual(
            {"task_id", "owner", "expires_at"},
            internal_workflow_required["workflow_claim"],
        )
        self.assertEqual(
            {"workflow_id", "task_id", "owner", "lease_epoch", "host_target"},
            internal_workflow_required["workflow_endpoint_bind"],
        )
        unit_by_id = {unit["task_id"]: unit for unit in plan["units"]}
        registered: set[str] = set()
        for register in registration["register_steps"]:
            arguments = register["arguments"]
            task_id = arguments["task_id"]
            unit = unit_by_id[task_id]
            self.assertEqual("workflow_register_task", register["tool"])
            self.assertEqual(
                internal_workflow_signatures["workflow_register_task"],
                set(arguments),
            )
            self.assertEqual(
                sorted(register["host_bound_fields"]),
                register["host_bound_fields"],
            )
            self.assertTrue(
                set(unit["depends_on"]) <= registered,
                "register_steps must be a dependency-first topological order",
            )
            registered.add(task_id)
            self.assertEqual(unit["depends_on"], arguments["dependencies"])
            self.assertEqual(unit["write_scope"], arguments["write_scope"])
            self.assertEqual(
                unit["direct_contract_hashes"],
                arguments["direct_contract_hashes"],
            )
            self.assertEqual(unit["task_node_ids"], arguments["task_node_ids"])
            self.assertEqual(
                unit["contract_node_ids"],
                arguments["contract_node_ids"],
            )
            self.assertEqual(
                unit["required_evidence"],
                arguments["required_evidence"],
            )
            self.assertTrue(arguments["strict_index"])
            self.assertRegex(arguments["input_hash"], r"^sha256:[0-9a-f]{64}$")
            card = json.loads(arguments["card"])
            self.assertEqual(
                {
                    "schema",
                    "task_id",
                    "goal",
                    "output_boundary",
                    "unit_kind",
                    "execution_contracts",
                },
                set(card),
            )
            self.assertLessEqual(
                len(arguments["card"].encode("utf-8")),
                helper.MAX_REGISTRATION_CARD_BYTES,
            )
            for field in ("workspace_root", "input_snapshot_id"):
                self.assertIn(field, register["host_bound_fields"])
                self.assertEqual("host", arguments[field]["source"])
                self.assertEqual(field, arguments[field]["ref"])
                self.assertTrue(arguments[field]["description"])

        for wave_index, wave in enumerate(registration["execution_waves"], start=1):
            self.assertEqual(wave_index, wave["wave_index"])
            self.assertEqual(sorted(wave["task_ids"]), wave["task_ids"])
            ready = wave["workflow_ready"]
            self.assertEqual("workflow_ready", ready["tool"])
            self.assertEqual(
                internal_workflow_signatures["workflow_ready"],
                set(ready["arguments"]),
            )
            self.assertEqual(["workflow_id"], ready["host_bound_fields"])
            self.assertEqual(
                {
                    "allow_empty_result": True,
                    "require_exact_task_set": False,
                    "claim_precondition": "READY",
                },
                wave["ready_result_policy"],
            )
            self.assertEqual(
                wave["task_ids"],
                [step["task_id"] for step in wave["task_steps"]],
            )
            barrier = wave["completion_barrier"]
            self.assertEqual("all_tasks_reach_state", barrier["condition"])
            self.assertEqual(wave["task_ids"], barrier["task_ids"])
            self.assertEqual("DONE", barrier["required_state"])
            self.assertEqual(
                wave_index + 1
                if wave_index < len(registration["execution_waves"])
                else None,
                barrier["advance_to_wave_index"],
            )
            for task_step in wave["task_steps"]:
                for operation_name, fields in (
                    (
                        "workflow_claim",
                        ("owner", "expires_at", "host_target", "now"),
                    ),
                    (
                        "workflow_endpoint_bind",
                        ("owner", "lease_epoch", "host_target", "now"),
                    ),
                ):
                    operation_descriptor = task_step[operation_name]
                    self.assertEqual(operation_name, operation_descriptor["tool"])
                    self.assertEqual(
                        internal_workflow_signatures[operation_name],
                        set(operation_descriptor["arguments"]),
                    )
                    self.assertEqual(
                        sorted(operation_descriptor["host_bound_fields"]),
                        operation_descriptor["host_bound_fields"],
                    )
                    operation = operation_descriptor["arguments"]
                    self.assertEqual(task_step["task_id"], operation["task_id"])
                    for field in fields:
                        self.assertIn(field, operation_descriptor["host_bound_fields"])
                        self.assertEqual("host", operation[field]["source"])
                        self.assertEqual(field, operation[field]["ref"])
                        self.assertTrue(operation[field]["description"])

        rendered = json.dumps(registration, sort_keys=True)
        self.assertNotIn(manifest["packet"]["trace_id"], rendered)
        self.assertNotIn(manifest["packet"]["snapshot_id"], rendered)
        self.assertNotIn(manifest["packet"]["workspace_id"], rendered)
        self.assertNotIn(str(self.repo), rendered)

    def test_task_episode_mode_needs_design_or_fails_closed_without_graph_proof(
        self,
    ) -> None:
        helper = load_efficiency()

        missing_evidence = self.task_episode_manifest()
        missing_evidence["graph"]["edges"] = [
            edge
            for edge in missing_evidence["graph"]["edges"]
            if edge["relation"] != "VERIFIED_BY"
        ]
        self.assertEqual("needs_design", helper.decompose(missing_evidence)["status"])

        truncated = self.task_episode_manifest()
        truncated["graph"]["truncated"] = True
        self.assertEqual("needs_design", helper.decompose(truncated)["status"])

        unknown_relation = self.task_episode_manifest()
        unknown_relation["graph"]["edges"][0]["relation"] = "MAYBE_RELATED"
        self.assertEqual("needs_design", helper.decompose(unknown_relation)["status"])

        unsafe_scope = self.task_episode_manifest()
        path_source = next(
            node
            for node in unsafe_scope["graph"]["nodes"]
            if node["kind"] == "SourceEvidence" and node["payload"].get("path")
        )
        path_source["payload"]["path"] = "../outside.py"
        self.assertEqual("needs_design", helper.decompose(unsafe_scope)["status"])

        invented_scope = self.task_episode_manifest()
        invented_scope["path_bindings"] = {"docs_path": "docs/core.md"}
        self.assertEqual("needs_design", helper.decompose(invented_scope)["status"])

        command_output = self.task_episode_manifest()
        test_node = next(
            node
            for node in command_output["graph"]["nodes"]
            if node["kind"] == "TestSpec"
        )
        test_node["payload"]["command_output"] = "1 passed"
        self.assertEqual("needs_design", helper.decompose(command_output)["status"])

    def test_decompose_serializes_overlapping_scopes_without_losing_parallelism(
        self,
    ) -> None:
        helper = load_efficiency()
        manifest = self.decomposition_manifest()
        manifest["capacity"] = 3
        manifest["artifacts"][1]["write_scope"] = ["skills/work-methodology/scripts"]
        manifest["artifacts"][2]["depends_on"] = []

        plan = helper.decompose(manifest)
        first_wave = {unit["task_id"] for unit in plan["waves"][0]}

        self.assertEqual(2, len(first_wave))
        self.assertIn("ATLAS-12B-A", plan["conflict_graph"]["ATLAS-12B-B"])
        self.assertNotIn({"ATLAS-12B-A", "ATLAS-12B-B"}, [first_wave])

    def test_decompose_rejects_invalid_dependency_graphs_and_write_scopes(self) -> None:
        helper = load_efficiency()

        unknown = self.decomposition_manifest()
        unknown["artifacts"][0]["depends_on"] = ["ATLAS-12B-X"]
        with self.assertRaises(ValueError):
            helper.decompose(unknown)

        cycle = self.decomposition_manifest()
        cycle["artifacts"][0]["depends_on"] = ["ATLAS-12B-C"]
        with self.assertRaises(ValueError):
            helper.decompose(cycle)

        unsafe_path = self.decomposition_manifest()
        unsafe_path["artifacts"][0]["write_scope"] = ["../outside.py"]
        with self.assertRaises(ValueError):
            helper.decompose(unsafe_path)

    def test_decompose_requires_sol_design_for_semantic_splits(self) -> None:
        helper = load_efficiency()
        manifest = {
            "schema": "team-efficiency/work-package-v1",
            "task_id": "ATLAS-12B",
            "goal": "Choose an architecture before artifact boundaries exist",
            "capacity": 2,
            "decomposition": "semantic",
        }

        plan = helper.decompose(manifest)

        self.assertEqual("needs_design", plan["status"])
        self.assertEqual([], plan["units"])
        self.assertEqual([], plan["waves"])

    def test_cli_canonicalizes_resume_packet_and_skill_links_the_reference(
        self,
    ) -> None:
        helper = load_efficiency()
        packet_path = self.temp / "resume.json"
        packet_path.write_text(json.dumps(self.resume_packet()), encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = helper.main(["resume-packet", "--input", str(packet_path)])

        self.assertEqual(0, exit_code)
        self.assertEqual(self.resume_packet(), json.loads(output.getvalue()))
        self.assertIn("efficiency-automation.md", CONTRACT.read_text(encoding="utf-8"))

    def test_cli_decomposes_packet_and_episode_evidence_without_artifacts(
        self,
    ) -> None:
        helper = load_efficiency()

        for name, manifest in (
            ("packet", self.code_atlas_manifest()),
            ("episode", self.task_episode_manifest()),
        ):
            with self.subTest(name=name):
                manifest_path = self.temp / f"{name}.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                output = io.StringIO()

                with contextlib.redirect_stdout(output):
                    exit_code = helper.main(
                        ["decompose", "--input", str(manifest_path)]
                    )

                self.assertEqual(0, exit_code)
                self.assertEqual(
                    helper.decompose(manifest), json.loads(output.getvalue())
                )

    def test_fast_lane_quota_context_fails_closed_before_any_new_start(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        legacy = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
        )
        self.assertTrue(
            any(item["action"] == "start" for item in legacy["assignments"])
        )

        enforced = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
            quota_request={},
            quota_trusted_key_resolver=lambda _key_id: None,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=(),
            quota_verified_lease_scope_bindings=(),
        )

        self.assertIn("quota_balance", enforced["refill_plan"])
        self.assertFalse(
            any(item["action"] == "start" for item in enforced["assignments"])
        )
        self.assertEqual(
            "usage_unknown", enforced["refill_plan"]["quota_balance"]["status"]
        )

    def test_fast_lane_quota_adapter_rejects_non_sequence_assignments(self) -> None:
        helper = load_efficiency()
        with self.assertRaises(TypeError):
            helper._apply_fast_lane_quota_balance(
                {"assignments": None},
                quota_request={},
                quota_decision={},
            )

    def test_fast_lane_cli_parses_quota_context_as_a_fail_closed_host_preview(
        self,
    ) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        request_path = self.temp / "fast-lane-quota-request.json"
        host_status_path = self.temp / "fast-lane-quota-host-status.json"
        quota_path = self.temp / "fast-lane-quota-context.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        host_status_path.write_text(json.dumps(host_status), encoding="utf-8")
        quota_path.write_text("{}", encoding="utf-8")

        exit_code, output, errors = self.run_fast_lane_cli(
            helper,
            [
                "fast-lane",
                "--input",
                str(request_path),
                "--host-status",
                str(host_status_path),
                "--quota-input",
                str(quota_path),
                "--quota-evaluation-time",
                "2026-08-01T15:10:00Z",
                "--reasoning-effort",
                "ultra",
            ],
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("", errors)
        result = json.loads(output)
        self.assertEqual(
            "usage_unknown", result["refill_plan"]["quota_balance"]["status"]
        )
        self.assertFalse(
            any(item["action"] == "start" for item in result["assignments"])
        )

    def test_fast_lane_live_quota_source_failure_is_fail_closed(self) -> None:
        helper = load_efficiency()
        request = self.fast_lane_schedule_request(helper)
        host_status = self.fast_lane_host_status(helper, request)
        request_path = self.temp / "fast-lane-live-request.json"
        host_status_path = self.temp / "fast-lane-live-host-status.json"
        quota_path = self.temp / "fast-lane-live-quota.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        host_status_path.write_text(json.dumps(host_status), encoding="utf-8")
        quota_path.write_text(
            json.dumps(
                {
                    "snapshot": {
                        "capacity": {
                            "ledger_epoch": 1,
                            "global_main_active": 0,
                            "global_spark_active": 0,
                            "host_main_active": 0,
                            "host_spark_active": 0,
                            "host_main_cap": 3,
                            "host_spark_cap": 1,
                            "active_lease_set_hash": "sha256:" + "a" * 64,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        exit_code, output, errors = self.run_fast_lane_cli(
            helper,
            [
                "fast-lane",
                "--input",
                str(request_path),
                "--host-status",
                str(host_status_path),
                "--quota-input",
                str(quota_path),
                "--live-quota",
                "--codex-executable",
                str(self.temp / "missing-codex"),
                "--reasoning-effort",
                "ultra",
            ],
        )

        self.assertEqual(0, exit_code)
        self.assertIn("quota source unavailable", errors)
        result = json.loads(output)
        self.assertEqual(
            "usage_unknown", result["refill_plan"]["quota_balance"]["status"]
        )
        self.assertFalse(
            any(item["action"] == "start" for item in result["assignments"])
        )

    def fast_lane_signed_quota_request(
        self,
        helper,
        assignments: list[dict[str, object]],
        *,
        main_used: int = 950_000,
        global_main_active: int = 0,
        host_main_active: int = 0,
        global_spark_active: int = 1,
        valid_until_utc_z: str = "2026-08-01T15:10:30Z",
    ) -> tuple[dict[str, object], object, set[str], set[str]]:
        quota = helper._fast_lane_quota_module()
        key = b"team-efficiency-cross-session-quota-key"
        key_id = helper._sha256_json({"kind": "quota-key"})
        source_id = helper._sha256_json({"kind": "quota-source"})
        period_id = helper._sha256_json({"kind": "quota-period"})
        lease_set = helper._sha256_json({"kind": "quota-lease-set"})
        capacity = {
            "ledger_epoch": 9,
            "global_main_active": global_main_active,
            "global_spark_active": global_spark_active,
            "host_main_active": host_main_active,
            "host_spark_active": 0,
            "host_main_cap": 3,
            "host_spark_cap": 1,
            "active_lease_set_hash": lease_set,
        }
        snapshot_unsigned = {
            "schema": "2718lab-devkit/host-quota-snapshot-v1",
            "source": {
                "kind": "codex_host_usage_snapshot",
                "source_id_hash": source_id,
                "key_id": key_id,
            },
            "snapshot_seq": 1,
            "observed_at_utc_z": "2026-08-01T15:09:00Z",
            "valid_until_utc_z": valid_until_utc_z,
            "sample_window_seconds": 300,
            "main": {
                "period_id_hash": period_id,
                "used_ppm": main_used,
                "delta_ppm_300s": 0,
            },
            "spark": {
                "period_id_hash": period_id,
                "used_ppm": 100_000,
                "delta_ppm_300s": 0,
            },
            "capacity": capacity,
        }
        snapshot_hash = quota._hash(snapshot_unsigned)
        signed_snapshot = {**snapshot_unsigned, "snapshot_hash": snapshot_hash}
        snapshot = {
            **signed_snapshot,
            "signature": {
                "algorithm": "hmac-sha256",
                "value": hmac.new(
                    key,
                    quota._canonical_json(signed_snapshot).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest(),
            },
        }
        candidates: list[dict[str, object]] = []
        for index, assignment in enumerate(assignments):
            candidate = {
                "candidate_id": helper._sha256_json({"candidate": index}),
                "workflow_key": helper._sha256_json({"workflow": index}),
                "task_key": helper._sha256_json({"task": assignment["task_id"]}),
                "pool": "main",
                "scheduler_role": assignment["role"],
                "route_lock": {
                    "result_hash": assignment["routing_result_hash"],
                    "task_fingerprint": assignment["task_fingerprint"],
                    "lane": "terra",
                    "model": assignment["model"],
                    "effort": assignment["reasoning_effort"],
                    "safety_floor_rank": assignment["routing_safety_floor_rank"],
                },
                "task_lease_epoch": assignment["assignment_epoch"],
                "assignment_epoch": assignment["assignment_epoch"],
                "assignment_token": assignment["assignment_token"],
                "host_id_hash": helper._sha256_json({"host": "local"}),
                "local_slot_id": assignment["slot_id"],
                "write_scope_hash": assignment["write_scope_hash"],
                "input_snapshot_id": assignment["workspace_input_snapshot_id"],
                "spark_binding": None,
            }
            candidates.append(candidate)
        request = {
            "schema": "2718lab-devkit/fastlane-quota-balance-request-v1",
            "policy_hash": quota._hash(quota._policy()),
            "snapshot": snapshot,
            "candidates": candidates,
            "receipts": [],
            "prior_audit_hash": None,
        }
        request["request_hash"] = quota._normalized_request_hash(request)
        route_hashes = {
            str(candidate["route_lock"]["result_hash"]) for candidate in candidates
        }
        lease_bindings = {
            quota._candidate_binding_hash(candidate) for candidate in candidates
        }

        def resolver(value: str) -> bytes | None:
            return key if value == key_id else None

        provisional = quota.compile_quota_balance(
            request,
            trusted_key_resolver=resolver,
            evaluation_time_utc_z="2026-08-01T15:10:00Z",
            verified_route_result_hashes=route_hashes,
            verified_lease_scope_bindings=lease_bindings,
        )
        receipts: list[dict[str, object]] = []
        for index, candidate in enumerate(candidates):
            receipt_unsigned = {
                "schema": "2718lab-devkit/global-admission-receipt-v1",
                "admission_id": helper._sha256_json({"admission": index}),
                "candidate_id": candidate["candidate_id"],
                "decision_hash": provisional["decision_hash"],
                "pool": "main",
                "ledger_epoch_before": capacity["ledger_epoch"],
                "ledger_epoch_after": capacity["ledger_epoch"] + 1,
                "active_lease_set_hash_before": lease_set,
                "active_lease_set_hash_after": helper._sha256_json(
                    {"lease-set-after": index}
                ),
                "route_result_hash": candidate["route_lock"]["result_hash"],
                "task_lease_epoch": candidate["task_lease_epoch"],
                "assignment_epoch": candidate["assignment_epoch"],
                "assignment_token": candidate["assignment_token"],
                "host_id_hash": candidate["host_id_hash"],
                "local_slot_id": candidate["local_slot_id"],
                "write_scope_hash": candidate["write_scope_hash"],
                "input_snapshot_id": candidate["input_snapshot_id"],
                "issued_at_utc_z": "2026-08-01T15:09:00Z",
                "expires_at_utc_z": "2026-08-01T15:10:30Z",
                "prior_receipt_hash": None,
            }
            receipt_hash = quota._hash(receipt_unsigned)
            signed_receipt = {**receipt_unsigned, "receipt_hash": receipt_hash}
            receipts.append(
                {
                    **signed_receipt,
                    "signature": {
                        "algorithm": "hmac-sha256",
                        "key_id": key_id,
                        "value": hmac.new(
                            key,
                            quota._canonical_json(signed_receipt).encode("utf-8"),
                            hashlib.sha256,
                        ).hexdigest(),
                    },
                }
            )
        request["receipts"] = receipts
        request["request_hash"] = quota._normalized_request_hash(request)
        return request, resolver, route_hashes, lease_bindings

    def test_fast_lane_projects_deterministic_fenced_external_sessions(self) -> None:
        helper = load_efficiency()
        work_package = self.decomposition_manifest()
        work_package["capacity"] = 6
        work_package["artifacts"] = [
            {
                "task_id": f"FAST-LANE-CROSS-{index}",
                "goal": f"Implement cross-session unit {index}",
                "output_boundary": f"cross-session unit {index}",
                "write_scope": [f"src/fast_lane/cross_{index}.py"],
                "depends_on": [],
                "required_evidence": [f"cross-proof-{index}"],
                "complexity": "moderate",
                "execution_contracts": ["contracts/fast-lane"],
            }
            for index in range(1, 7)
        ]
        request = self.fast_lane_request(helper, work_package=work_package)
        host_status = self.fast_lane_host_status(helper, request)
        local = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
        )
        local_starts = [
            item for item in local["assignments"] if item["action"] == "start"
        ]
        self.assertEqual(3, len(local_starts))
        quota_request, resolver, route_hashes, lease_bindings = (
            self.fast_lane_signed_quota_request(helper, local_starts)
        )

        result = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
            quota_request=quota_request,
            quota_trusted_key_resolver=resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=route_hashes,
            quota_verified_lease_scope_bindings=lease_bindings,
        )
        repeated = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
            quota_request=quota_request,
            quota_trusted_key_resolver=resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=route_hashes,
            quota_verified_lease_scope_bindings=lease_bindings,
        )

        projection = result["cross_session_dispatch_projection"]
        self.assertEqual(projection, repeated["cross_session_dispatch_projection"])
        self.assertEqual("external_session_required", projection["status"])
        self.assertEqual(
            {
                "schema": "team-efficiency/fast-lane-cross-session-policy-v1",
                "selection_authority": "compiler",
                "action": "dispatch_all",
                "target": "independent_codex_session",
                "llm_choice": False,
            },
            projection["dispatch_policy"],
        )
        self.assertEqual(3, projection["local_capacity"])
        self.assertEqual(0, projection["local_active_count"])
        self.assertEqual(6, projection["global_main_target"])
        self.assertEqual(6, projection["global_main_free"])
        self.assertEqual(
            "all_non_spark_agents_across_sessions", projection["main_pool_scope"]
        )
        self.assertEqual(3, projection["external_agent_count"])
        self.assertEqual(
            projection["global_main_free"],
            len(local_starts) + projection["external_agent_count"],
        )
        self.assertEqual(
            projection["external_assignment_ids"],
            [item["assignment_id"] for item in projection["assignments"]],
        )
        for assignment in projection["assignments"]:
            self.assertEqual("external_session_required", assignment["action"])
            self.assertEqual("not_created", assignment["session_state"])
            self.assertTrue(assignment["worktree_required"])
            self.assertEqual(
                {
                    "schema": "team-efficiency/fast-lane-host-dispatch-v1",
                    "kind": "codex.spawn_agent",
                    "model": assignment["model"],
                    "reasoning_effort": assignment["reasoning_effort"],
                    "inherit_current_session_model": False,
                    "require_explicit_route": True,
                    "missing_route_action": "reject",
                },
                assignment["host_dispatch"],
            )
            self.assertEqual(
                assignment["task_id"], assignment["index_context"]["task_id"]
            )
            self.assertEqual(assignment["role"], assignment["index_context"]["role"])
            self.assertEqual(
                assignment["context_hash"],
                assignment["index_context"]["dispatch_context_hash"],
            )
            self.assertEqual(
                assignment["context_hash"],
                assignment["lease_fencing_predecessor"]["context_hash"],
            )
            self.assertTrue(
                assignment["lease_fencing_predecessor"]["predecessor_hash"].startswith(
                    "sha256:"
                )
            )
            self.assertNotIn("host_target", assignment)

    def test_fast_lane_cross_session_projection_never_bypasses_local_or_host_fences(
        self,
    ) -> None:
        helper = load_efficiency()

        def request_with_units(count: int) -> dict[str, object]:
            work_package = self.decomposition_manifest()
            work_package["capacity"] = min(16, count)
            work_package["artifacts"] = [
                {
                    "task_id": f"FAST-LANE-EXTERNAL-{index}",
                    "goal": f"External capacity unit {index}",
                    "output_boundary": f"external capacity unit {index}",
                    "write_scope": [f"src/fast_lane/external_{index}.py"],
                    "depends_on": [],
                    "required_evidence": [f"external-proof-{index}"],
                    "complexity": "moderate",
                    "execution_contracts": ["contracts/fast-lane"],
                }
                for index in range(1, count + 1)
            ]
            return self.fast_lane_request(helper, work_package=work_package)

        under_capacity_request = request_with_units(2)
        under_capacity_host = self.fast_lane_host_status(helper, under_capacity_request)
        under_capacity_local = helper.compile_fast_lane(
            under_capacity_request,
            reasoning_effort="ultra",
            host_status=under_capacity_host,
        )
        under_capacity_starts = [
            item
            for item in under_capacity_local["assignments"]
            if item["action"] == "start"
        ]
        under_quota, under_resolver, under_routes, under_bindings = (
            self.fast_lane_signed_quota_request(helper, under_capacity_starts)
        )
        under_capacity = helper.compile_fast_lane(
            under_capacity_request,
            reasoning_effort="ultra",
            host_status=under_capacity_host,
            quota_request=under_quota,
            quota_trusted_key_resolver=under_resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=under_routes,
            quota_verified_lease_scope_bindings=under_bindings,
        )["cross_session_dispatch_projection"]
        self.assertEqual("not_required", under_capacity["status"])
        self.assertEqual("dispatch_none", under_capacity["dispatch_policy"]["action"])
        self.assertEqual(0, under_capacity["external_agent_count"])
        self.assertIn("local_capacity_available", under_capacity["reason_codes"])

        request = request_with_units(6)
        host_status = self.fast_lane_host_status(helper, request)
        local = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
        )
        local_starts = [
            item for item in local["assignments"] if item["action"] == "start"
        ]
        quota_request, resolver, route_hashes, lease_bindings = (
            self.fast_lane_signed_quota_request(helper, local_starts)
        )
        unavailable = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
            quota_request=quota_request,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=route_hashes,
            quota_verified_lease_scope_bindings=lease_bindings,
        )["cross_session_dispatch_projection"]
        self.assertEqual("blocked", unavailable["status"])
        self.assertEqual("stop", unavailable["dispatch_policy"]["action"])
        self.assertEqual(0, unavailable["external_agent_count"])
        self.assertIn("quota_usage_unknown", unavailable["reason_codes"])

        stale_quota, stale_resolver, stale_routes, stale_bindings = (
            self.fast_lane_signed_quota_request(
                helper,
                local_starts,
                valid_until_utc_z="2026-08-01T15:09:30Z",
            )
        )
        stale = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
            quota_request=stale_quota,
            quota_trusted_key_resolver=stale_resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=stale_routes,
            quota_verified_lease_scope_bindings=stale_bindings,
        )["cross_session_dispatch_projection"]
        self.assertEqual("blocked", stale["status"])
        self.assertEqual(0, stale["external_agent_count"])
        self.assertIn("quota_snapshot_stale", stale["reason_codes"])

        fenced_quota, fenced_resolver, fenced_routes, fenced_bindings = (
            self.fast_lane_signed_quota_request(
                helper,
                local_starts[:2],
                host_main_active=1,
                global_main_active=1,
            )
        )
        fenced = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=host_status,
            quota_request=fenced_quota,
            quota_trusted_key_resolver=fenced_resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=fenced_routes,
            quota_verified_lease_scope_bindings=fenced_bindings,
        )["cross_session_dispatch_projection"]
        self.assertEqual("blocked", fenced["status"])
        self.assertEqual(0, fenced["external_agent_count"])
        self.assertIn("quota_host_status_fenced", fenced["reason_codes"])

        foreign_host = copy.deepcopy(host_status)
        foreign_host["routing_context"]["routes"].pop()
        foreign = helper.compile_fast_lane(
            request,
            reasoning_effort="ultra",
            host_status=foreign_host,
            quota_request=quota_request,
            quota_trusted_key_resolver=resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=route_hashes,
            quota_verified_lease_scope_bindings=lease_bindings,
        )["cross_session_dispatch_projection"]
        self.assertEqual("blocked", foreign["status"])
        self.assertEqual(0, foreign["external_agent_count"])
        self.assertIn("no_safe_work", foreign["reason_codes"])

        over_bound_request = request_with_units(13)
        over_bound_host = self.fast_lane_host_status(helper, over_bound_request)
        over_bound_local = helper.compile_fast_lane(
            over_bound_request,
            reasoning_effort="ultra",
            host_status=over_bound_host,
        )
        over_bound_starts = [
            item
            for item in over_bound_local["assignments"]
            if item["action"] == "start"
        ]
        (
            over_bound_quota,
            over_bound_resolver,
            over_bound_routes,
            over_bound_bindings,
        ) = self.fast_lane_signed_quota_request(helper, over_bound_starts)
        over_bound = helper.compile_fast_lane(
            over_bound_request,
            reasoning_effort="ultra",
            host_status=over_bound_host,
            quota_request=over_bound_quota,
            quota_trusted_key_resolver=over_bound_resolver,
            quota_evaluation_time_utc_z="2026-08-01T15:10:00Z",
            quota_verified_route_result_hashes=over_bound_routes,
            quota_verified_lease_scope_bindings=over_bound_bindings,
        )["cross_session_dispatch_projection"]
        self.assertEqual("blocked", over_bound["status"])
        self.assertEqual(0, over_bound["external_agent_count"])
        self.assertIn("external_assignment_limit_exceeded", over_bound["reason_codes"])


if __name__ == "__main__":
    unittest.main()
