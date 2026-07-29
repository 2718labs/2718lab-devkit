from __future__ import annotations

import contextlib
import copy
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
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
    TestSpec as AtlasTestSpec,
    canonical_hash,
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


class TeamEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        task_temp = Path(os.environ["CODEX_TASK_TEMP"])
        task_temp.mkdir(parents=True, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(dir=task_temp)
        self.temp = Path(self._temporary_directory.name)
        self.safe_root = task_temp
        codex_temp_root = Path(r"D:\bun\tmp\codex").resolve(strict=False)
        self.project = self.safe_root.resolve(strict=False).relative_to(
            codex_temp_root
        ).as_posix()
        self.repo = (
            Path(r"D:\bun\tmp\codex\2718-devkit\worktrees") / "atlas12b-team-efficiency"
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

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
                    "handoff_contracts": ["contracts/helper-api"],
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
                    "handoff_contracts": ["contracts/helper-api"],
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
                    "handoff_contracts": ["contracts/helper-api"],
                },
            ],
        }

    def code_atlas_manifest(self) -> dict[str, object]:
        digest = lambda marker: f"sha256:{marker * 64}"
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

    def task_episode_manifest(
        self,
        *,
        docs_path: str = "docs/core.md",
        supersedes: bool = False,
    ) -> dict[str, object]:
        digest = lambda marker: f"sha256:{marker * 64}"
        build_slot = SlotSpec("build_path", "relative_python_path")
        docs_slot = SlotSpec("docs_path", "relative_python_path")
        verification = AtlasTestSpec(("python", "-m", "pytest"), 0)
        dependency = DependencySpec("pytest", "python", ">=8")

        def episode_payload(marker: str) -> dict[str, str]:
            return {
                "workflow_id_hash": digest(marker),
                "task_id_hash": digest("1"),
                "acceptance_id_hash": digest("2"),
                "workspace_hash": digest("3"),
                "checkpoint_id_hash": digest("4"),
                "input_snapshot_id_hash": digest("5"),
                "output_snapshot_id_hash": digest("6"),
                "task_kind": "code",
            }

        episode_build = AtlasNode.create(
            NodeKind.TASK_EPISODE,
            episode_payload("7"),
            provenance="observed",
            source_hashes=(digest("7"),),
        )
        episode_docs = AtlasNode.create(
            NodeKind.TASK_EPISODE,
            episode_payload("8"),
            provenance="observed",
            source_hashes=(digest("8"),),
        )
        source_build = AtlasNode.create(
            NodeKind.SOURCE_EVIDENCE,
            {
                "path": "src/core.py",
                "before_hash": digest("9"),
                "after_hash": digest("a"),
                "before_bytes": 1,
                "after_bytes": 2,
            },
            provenance="observed",
            source_hashes=(digest("9"), digest("a")),
        )
        source_docs = AtlasNode.create(
            NodeKind.SOURCE_EVIDENCE,
            {
                "path": docs_path,
                "before_hash": digest("b"),
                "after_hash": digest("c"),
                "before_bytes": 1,
                "after_bytes": 2,
            },
            provenance="observed",
            source_hashes=(digest("b"), digest("c")),
        )
        test_node = AtlasNode.create(
            NodeKind.TEST_SPEC,
            verification.to_dict(),
            provenance="observed",
            source_hashes=(digest("d"),),
        )
        dependency_node = AtlasNode.create(
            NodeKind.DEPENDENCY,
            dependency.to_dict(),
            provenance="observed",
            source_hashes=(digest("e"),),
        )
        build_slot_node = AtlasNode.create(
            NodeKind.ADAPTATION_SLOT,
            build_slot.to_dict(),
            provenance="observed",
            source_hashes=(digest("0"),),
        )
        docs_slot_node = AtlasNode.create(
            NodeKind.ADAPTATION_SLOT,
            docs_slot.to_dict(),
            provenance="observed",
            source_hashes=(digest("1"),),
        )

        def recipe_node(
            key: str,
            manifest_hash: str,
            slot: SlotSpec,
        ) -> AtlasNode:
            manifest = RecipeManifest(
                recipe_id="",
                recipe_key=key,
                version=1,
                intent_id=key,
                language_name="python",
                language_extractor_version="1",
                repository_signature=digest("2"),
                layer="local",
                manifest_hash=manifest_hash,
                slots=(slot,),
                constraints=(),
                dependencies=(dependency,),
                tests=(verification,),
                operations=(),
                provenance_kind="observed",
                provenance_source="accepted_task",
            )
            return AtlasNode.create(
                NodeKind.RECIPE,
                manifest.to_dict(),
                provenance="observed",
                source_hashes=(manifest_hash,),
            )

        recipe_build = recipe_node("build-core", digest("3"), build_slot)
        recipe_docs = recipe_node("document-core", digest("4"), docs_slot)
        nodes = (
            episode_build,
            episode_docs,
            source_build,
            source_docs,
            test_node,
            dependency_node,
            build_slot_node,
            docs_slot_node,
            recipe_build,
            recipe_docs,
        )
        edges = [
            AtlasEdge.create(
                EdgeRelation.CHANGES,
                episode_build,
                source_build,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.CHANGES,
                episode_docs,
                source_docs,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.VERIFIED_BY,
                episode_build,
                test_node,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.VERIFIED_BY,
                episode_docs,
                test_node,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.DERIVED_FROM,
                recipe_build,
                episode_build,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.DERIVED_FROM,
                recipe_docs,
                episode_docs,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.HAS_SLOT,
                recipe_build,
                build_slot_node,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.HAS_SLOT,
                recipe_docs,
                docs_slot_node,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.REQUIRES,
                recipe_build,
                dependency_node,
                provenance="observed",
            ),
            AtlasEdge.create(
                EdgeRelation.REQUIRES,
                recipe_docs,
                dependency_node,
                provenance="observed",
            ),
        ]
        if supersedes:
            edges.append(
                AtlasEdge.create(
                    EdgeRelation.SUPERSEDES,
                    recipe_docs,
                    recipe_build,
                    provenance="observed",
                )
            )
        graph = GraphQueryResult(
            tuple(sorted(nodes, key=lambda node: node.node_id)),
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
            "graph": graph.to_dict(),
        }

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
        reordered["path_bindings"] = dict(
            reversed(reordered["path_bindings"].items())
        )

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
                "kind": "code_atlas_handoff",
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
            self.assertTrue(unit["handoff_contracts"])
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
        invented_contracts["packet"]["handoff_contract_hashes"] = [
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
            {"docs/core.md", "src/core.py"},
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
            {by_scope["src/core.py"]["task_id"], by_scope["docs/core.md"]["task_id"]},
            set(verification["depends_on"]),
        )
        self.assertEqual("Terra Max", verification["recommended_route"])
        core_contracts = by_scope["src/core.py"]["direct_contract_hashes"]
        docs_contracts = by_scope["docs/core.md"]["direct_contract_hashes"]
        graph_fingerprint = canonical_hash(
            {
                "nodes": manifest["graph"]["nodes"],
                "edges": manifest["graph"]["edges"],
                "truncated": False,
            }
        )
        self.assertEqual(1, len(core_contracts))
        self.assertEqual(1, len(docs_contracts))
        self.assertEqual(
            [
                canonical_hash(
                    {
                        "kind": "code_atlas_task_episode_handoff",
                        "graph_fingerprint": graph_fingerprint,
                        "task_episode_node_id": by_scope["src/core.py"][
                            "task_node_ids"
                        ][0],
                    }
                )
            ],
            core_contracts,
        )
        self.assertEqual([], by_scope["src/core.py"]["depends_on"])
        self.assertEqual([], by_scope["docs/core.md"]["depends_on"])
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
            self.assertTrue(unit["handoff_contracts"])

        conflict = self.task_episode_manifest(docs_path="src")
        conflict_plan = helper.decompose(conflict)
        conflict_by_scope = {
            unit["write_scope"][0]: unit
            for unit in conflict_plan["units"]
            if unit["write_scope"]
        }
        build_id = conflict_by_scope["src/core.py"]["task_id"]
        docs_id = conflict_by_scope["src"]["task_id"]
        self.assertIn(build_id, conflict_plan["conflict_graph"][docs_id])
        self.assertFalse(
            any(
                {build_id, docs_id}
                <= {unit["task_id"] for unit in wave}
                for wave in conflict_plan["waves"]
            )
        )
        self.assertEqual("verification", conflict_plan["waves"][-1][0]["unit_kind"])

        dependent = helper.decompose(self.task_episode_manifest(supersedes=True))
        dependent_by_scope = {
            unit["write_scope"][0]: unit
            for unit in dependent["units"]
            if unit["write_scope"]
        }
        self.assertIn(
            dependent_by_scope["src/core.py"]["task_id"],
            dependent_by_scope["docs/core.md"]["depends_on"],
        )

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
        with self.assertRaises(ValueError):
            helper.decompose(unknown_relation)

        unsafe_scope = self.task_episode_manifest(docs_path="../outside.py")
        with self.assertRaises(ValueError):
            helper.decompose(unsafe_scope)

        invented_scope = self.task_episode_manifest()
        invented_scope["path_bindings"] = {"docs_path": "docs/core.md"}
        with self.assertRaises(ValueError):
            helper.decompose(invented_scope)

        command_output = self.task_episode_manifest()
        test_node = next(
            node
            for node in command_output["graph"]["nodes"]
            if node["kind"] == "TestSpec"
        )
        test_node["payload"]["command_output"] = "1 passed"
        with self.assertRaises(ValueError):
            helper.decompose(command_output)

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
                self.assertEqual(helper.decompose(manifest), json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
