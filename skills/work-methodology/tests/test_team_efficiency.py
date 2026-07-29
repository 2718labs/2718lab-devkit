from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "team_efficiency.py"
SKILL = ROOT / "SKILL.md"


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
            "project": "2718-devkit/atlas-12b",
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
            "source_kind": "code_atlas_packet",
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
        self.assertEqual("code_atlas_packet", plan["source_kind"])
        self.assertEqual(
            [
                ["ATLAS-12B-A", "ATLAS-12B-B"],
                ["ATLAS-12B-C"],
            ],
            [[unit["task_id"] for unit in wave] for wave in plan["waves"]],
        )
        self.assertEqual("Terra High", plan["waves"][0][0]["recommended_route"])
        self.assertEqual("Terra Max", plan["waves"][0][1]["recommended_route"])

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


if __name__ == "__main__":
    unittest.main()
