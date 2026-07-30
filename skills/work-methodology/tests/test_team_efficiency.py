from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
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


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "team_efficiency.py"
SKILL = ROOT / "SKILL.md"
sys.path.insert(0, str(ROOT.parents[1] / "mcp-tools"))

from code_atlas import (  # noqa: E402
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
    TestSpec as AtlasTestSpec,
    canonical_hash,
)
from code_atlas.extractors import (  # noqa: E402
    BoundExecutionReceipt,
    ExtractionRequest,
    PythonRecipeExtractor,
)
from code_atlas.store import AtlasStore  # noqa: E402
from orchestrator.models import Task, TaskState, Workflow, WorkflowKind  # noqa: E402
from orchestrator.service import OrchestratorService  # noqa: E402
from orchestrator.store import SQLiteStore  # noqa: E402
from project_index.models import SnapshotFile  # noqa: E402
from project_index.service import ProjectIndexService  # noqa: E402


def _content_hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _marker_hash(marker: str) -> str:
    return _content_hash(marker.encode("utf-8"))


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
        task_temp = Path(os.environ["CODEX_TASK_TEMP"])
        task_temp.mkdir(parents=True, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(dir=task_temp)
        self.temp = Path(self._temporary_directory.name)
        self.safe_root = task_temp
        codex_temp_root = Path(r"D:\bun\tmp\codex").resolve(strict=False)
        self.project = (
            self.safe_root.resolve(strict=False).relative_to(codex_temp_root).as_posix()
        )
        self.repo = (
            Path(r"D:\bun\tmp\codex\2718-devkit\worktrees") / "atlas12b-team-efficiency"
        )
        self._stores: list[SQLiteStore] = []
        self._index_services: list[ProjectIndexService] = []

    def tearDown(self) -> None:
        for index_service in reversed(self._index_services):
            index_service.close()
        for store in reversed(self._stores):
            store.close()
        self._temporary_directory.cleanup()

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
        workspace_root: Path | None = None,
        input_snapshot_id: str = "snapshot-lifecycle",
        index_service: ProjectIndexService | None = None,
    ) -> list[list[str]]:
        lifecycle = plan["registration_plan"]
        base_bindings: dict[str, object] = {
            "workflow_id": workflow_id,
            "workspace_root": str(
                self.repo if workspace_root is None else workspace_root
            ),
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
                workspace_root=arguments["workspace_root"],
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
                    self.assertIsNotNone(workspace_root)
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
                        workspace_root,
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
            "worktree": self.safe_root / "worktrees" / "atlas12b-team-efficiency",
            "temp_target": self.safe_root / "tasks" / "atlas12b",
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
    ) -> dict[str, object]:
        work_package = copy.deepcopy(self.decomposition_manifest())
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
                        worktree=self.safe_root / "worktrees" / f"fast-lane-{task_slug}",
                        temp_target=self.safe_root / "tasks" / f"fast-lane-{task_slug}",
                    ),
                    "workspace_input_snapshot_id": None,
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
            workspace="2718-devkit",
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
            next_action="code_atlas_render",
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

    def test_fast_lane_ultra_context_ineligible_is_blocked(self) -> None:
        helper = load_efficiency()

        result = helper.compile_fast_lane(
            self.fast_lane_contexts_empty_request(helper),
            reasoning_effort="ultra",
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("NO_SAFE_WORK", result["decision_code"])
        self.assertEqual("ultra_auto", result["activation"]["reason"])
        self.assertEqual([], result["assignments"])

    def test_fast_lane_lower_effort_without_enable_is_exactly_inactive(self) -> None:
        helper = load_efficiency()

        result = helper.compile_fast_lane(
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
                helper.compile_fast_lane(
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
                helper.compile_fast_lane(
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
                helper.compile_fast_lane(
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
                    result["refill_plan"],
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
                    },
                    set(result["workflow_policy"]),
                )
                self.assertEqual(
                    "work_methodology_skill", result["workflow_policy"]["owner"]
                )
                self.assertEqual([], result["workflow_policy"]["boundary_operations"])
                self.assertEqual([], result["workflow_policy"]["conditional_operations"])
                self.assertFalse(
                    result["workflow_policy"][
                        "operation_set_is_closed_capability_list"
                    ]
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
                    helper.compile_fast_lane(
                        self.fast_lane_contexts_empty_request(helper),
                        reasoning_effort="max",
                        enable=invalid_enable,
                    )

    def test_fast_lane_request_rejects_noncanonical_or_oversized_values(self) -> None:
        helper = load_efficiency()
        noncanonical = self.fast_lane_contexts_empty_request(helper)
        noncanonical["scheduler_state"]["source_plan_hash"] = float("nan")
        oversized = self.fast_lane_contexts_empty_request(helper)
        oversized["work_package"]["goal"] = "x" * (
            helper.MAX_MANIFEST_INPUT_BYTES + 1
        )
        self.assertGreater(
            len(helper._json_bytes(oversized)), helper.MAX_MANIFEST_INPUT_BYTES
        )

        for name, request in (
            ("nan", noncanonical),
            ("oversized", oversized),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    helper.compile_fast_lane(request, reasoning_effort="ultra")

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
                    helper.compile_fast_lane(request, reasoning_effort="ultra")

    def test_fast_lane_rejects_nontext_phase_in_api_and_cli(self) -> None:
        helper = load_efficiency()

        for name, invalid_phase in (("list", []), ("object", {})):
            request = self.fast_lane_contexts_empty_request(helper)
            request["scheduler_state"]["phase"] = invalid_phase
            with self.subTest(api=name):
                with self.assertRaises(ValueError):
                    helper.compile_fast_lane(request, reasoning_effort="ultra")

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

    def test_legacy_decompose_golden_bytes_are_unchanged(self) -> None:
        helper = load_efficiency()

        payload = canonical_bytes(helper.decompose(self.decomposition_manifest()))
        waves_payload = canonical_bytes(helper.plan_waves(self.decomposition_manifest()))

        self.assertEqual(payload, waves_payload)
        self.assertEqual(11730, len(payload))
        self.assertEqual(
            "f736b99d55bc562252d1fd6a98fb0f2d12813b0b50ba518f88d4f12c298a7775",
            hashlib.sha256(payload).hexdigest(),
        )

    def test_skill_documents_ultra_auto_policy(self) -> None:
        document = SKILL.read_text(encoding="utf-8")

        for expected in (
            "python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort ultra",
            "Ultra automatic activation",
            "--enable",
            "main Sol",
            'action="start"',
            'action="retain"',
            "terminal",
            "only after a terminal event",
            "no commentary polling",
            "no safe useful work",
            "prewarm: gpt-5.6-terra / medium",
            "routine implementation and ordinary verification: gpt-5.6-terra / high",
            "moderate-or-harder implementation and verification: gpt-5.6-terra / max",
            "review: gpt-5.6-terra / high",
            "bounded design probe: gpt-5.6-sol / ultra",
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
            "python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort ultra",
            "python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort max --enable",
            "target gates",
            "canonical repo anchor",
            "trusted shared integration worktree",
            "git worktree add",
            "Git common directory",
            "post-apply attestation",
            "parked endpoint bootstrap",
            "inert",
            "inert dispatch descriptors",
            "no model call",
            "agent spawn",
            "remote service",
            "Git mutation",
            "workflow call",
            "one regression",
            "one blocker review",
            "one global remediation",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, document)

    def test_bootstrap_defaults_to_a_dry_run_with_only_safe_git_argv(self) -> None:
        helper = load_efficiency()

        plan = helper.build_bootstrap_plan(**self.bootstrap_kwargs())

        expected_target = str(self.bootstrap_kwargs()["worktree"])
        self.assertEqual("dry_run", plan["mode"])
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

    def test_bootstrap_is_portable_to_any_compliant_codex_project_root(self) -> None:
        helper = load_efficiency()
        codex_root = Path(r"D:\bun\tmp\codex").resolve(strict=False)
        project_root = self.safe_root / "portable-project" / "nested-root"
        project = project_root.resolve(strict=False).relative_to(codex_root).as_posix()
        worktree = project_root / "worktrees" / "portable-atlas"
        temp_target = project_root / "tasks" / "portable-atlas"

        plan = helper.build_bootstrap_plan(
            task_id="ATLAS-12B",
            base_commit="a" * 40,
            branch="feature/code-atlas-v1-team-efficiency",
            write_scope=["skills/work-methodology/scripts/team_efficiency.py"],
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
        apply_target["worktree"] = self.temp / "apply-worktree"
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
        apply_target["worktree"] = self.temp / "failed-apply-worktree"
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
        escaped_target["worktree"] = self.safe_root.parent.parent / "outside"
        with self.assertRaises(ValueError):
            helper.build_bootstrap_plan(**escaped_target)

        existing_target = self.bootstrap_kwargs()
        existing_target["worktree"] = self.temp / "existing-worktree"
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
        index_service = ProjectIndexService(self.temp / "orchestrator-index.sqlite")
        self._index_services.append(index_service)
        snapshot = index_service.sync(workspace_root, include_paths=(indexed_path,))
        self.assertEqual(1, snapshot.file_count)
        snapshot_facts = index_service.snapshot_facts(
            workspace_root, snapshot.snapshot_id
        )
        (indexed_file,) = index_service.read_snapshot_files(
            workspace_root,
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
            workspace_root=workspace_root,
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
        self.assertEqual(
            {
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
            },
            set(first["arguments"]),
        )
        self.assertIsInstance(
            first["arguments"]["card"],
            str,
        )
        server_tree = ast.parse(
            (ROOT.parents[1] / "mcp-tools" / "server.py").read_text(encoding="utf-8")
        )
        signatures = {}
        required = {}
        for node in server_tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in {
                "workflow_register_task",
                "workflow_ready",
                "workflow_claim",
                "workflow_endpoint_bind",
            }:
                continue
            names = [argument.arg for argument in node.args.args]
            signatures[node.name] = set(names)
            required_count = len(names) - len(node.args.defaults)
            required[node.name] = set(names[:required_count])
        self.assertEqual({"workflow_id"}, signatures["workflow_ready"])
        self.assertEqual({"workflow_id"}, required["workflow_ready"])
        self.assertEqual(
            {
                "workflow_id",
                "task_id",
                "title",
                "owner_role",
                "card",
            },
            required["workflow_register_task"],
        )
        self.assertEqual(
            {"task_id", "owner", "expires_at"},
            required["workflow_claim"],
        )
        self.assertEqual(
            {"workflow_id", "task_id", "owner", "lease_epoch", "host_target"},
            required["workflow_endpoint_bind"],
        )
        unit_by_id = {unit["task_id"]: unit for unit in plan["units"]}
        registered: set[str] = set()
        for register in registration["register_steps"]:
            arguments = register["arguments"]
            task_id = arguments["task_id"]
            unit = unit_by_id[task_id]
            self.assertEqual("workflow_register_task", register["tool"])
            self.assertEqual(
                signatures["workflow_register_task"],
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
            self.assertEqual(signatures["workflow_ready"], set(ready["arguments"]))
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
                        signatures[operation_name],
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
        self.assertNotIn(manifest["packet"]["workspace"], rendered)
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
        self.assertIn("efficiency-automation.md", SKILL.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
