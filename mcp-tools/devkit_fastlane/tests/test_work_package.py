from __future__ import annotations

import importlib.util
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_work_package.py"
STRICT_READ_ONLY_MARKERS = (
    "project_index_query",
    "trace_id",
    'workflow_artifact_register(kind="verification", snapshot_id=...)',
    "workflow_complete",
)


def load_validator():
    if not SCRIPT.exists():
        raise AssertionError(f"validator is missing: {SCRIPT}")
    spec = importlib.util.spec_from_file_location("validate_work_package", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_integration_record() -> dict[str, object]:
    task_id = "ATLAS-12A"
    source_branch = "feature/code-atlas-v1-routing"
    source_worktree = "D:/bun/tmp/codex/2718-devkit/worktrees/atlas12a-routing"
    candidate_commit = "a" * 40
    evidence_hash = f"sha256:{'b' * 64}"
    return {
        "task_id": task_id,
        "source_branch": source_branch,
        "source_worktree": source_worktree,
        "candidate_commit": candidate_commit,
        "base_revision": "c" * 40,
        "evidence_hash": evidence_hash,
        "integration_order": 1,
        "review_receipt": {
            "receipt_hash": f"sha256:{'d' * 64}",
            "reviewer_role": "sol",
            "reviewer_task_id": "ATLAS-COORDINATOR",
            "decision": "accepted",
            "task_id": task_id,
            "candidate_commit": candidate_commit,
            "source_branch": source_branch,
            "source_worktree": source_worktree,
            "evidence_hash": evidence_hash,
        },
        "active_write_scopes": [],
    }


def valid_resume_packet() -> dict[str, object]:
    return {
        "workflow_id": "atlas-workflow",
        "task_id": "ATLAS-12A",
        "lease_epoch": 4,
        "current_endpoint": "/root/atlas12a_resume",
        "base_commit": "a" * 40,
        "candidate_commit": "b" * 40,
        "branch_or_worktree": "feature/code-atlas-v1-routing",
        "write_scope_hash": f"sha256:{'c' * 64}",
        "latest_red": {
            "command": "python -m pytest focused",
            "result": "1 failed",
        },
        "latest_green": {
            "command": "python -m pytest focused",
            "result": "32 passed",
        },
        "contract_hashes": [f"sha256:{'d' * 64}"],
        "evidence_hashes": [f"sha256:{'e' * 64}"],
        "next_action": "run core verification lane",
        "redacted": True,
        "resume_steps": [
            "workflow_endpoint_bind",
            "workflow_inbox",
            "workflow_artifact_resolve",
            "workflow_message_ack",
            "resume_next_action",
        ],
    }


class WorkPackageValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(os.environ["CODEX_TASK_TEMP"])
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_valid_package(self) -> None:
        (self.root / "tasks").mkdir()
        (self.root / "contracts").mkdir()
        (self.root / "product-brief.md").write_text(
            "# Feature\n\n"
            "## Goal\nDirection only.\n\n"
            "## Scope\nOne release.\n\n"
            "## Direction\nUse native Codex surfaces.\n\n"
            "## Risk Gate\nAsk only for dangerous work.\n\n"
            "## Done\nVerified and installed.\n",
            encoding="utf-8",
        )
        (self.root / "index.md").write_text(
            "# Work Index\n\n"
            "## Shared Contracts\n- `contracts/core.md`\n\n"
            "## Tasks\n- `tasks/BK-01.md`: pending\n\n"
            "## Dispatch\nLoad the index, one task card, and only its linked contracts.\n",
            encoding="utf-8",
        )
        (self.root / "contracts" / "core.md").write_text(
            "# Core Contract\n\n## API\nState values are stable.\n",
            encoding="utf-8",
        )
        (self.root / "tasks" / "BK-01.md").write_text(
            "# BK-01 Core\n\n"
            "Owner: terra\n"
            "Depends on: none\n\n"
            "## Goal\nImplement state values.\n\n"
            "## Context\nRead `contracts/core.md`.\n\n"
            "## Write Scope\n- `src/state.py`\n\n"
            "## Steps\n1. Write the failing test.\n2. Implement the minimum.\n\n"
            "## Acceptance\nRun `python -m unittest tests/test_state.py`.\n\n"
            "## Return\nChanged files, command, output, unresolved blockers.\n",
            encoding="utf-8",
        )

    def write_strict_terra_package(self) -> None:
        self.write_valid_package()
        (self.root / "index.md").write_text(
            "# Work Index\n\n"
            "## Shared Contracts\n- `contracts/core.md`\n\n"
            "## Tasks\n- `tasks/BK-01.md`: pending\n\n"
            "## Dispatch\n"
            "Run `project_index_sync`, then `workflow_register_task` with "
            "`strict_index=true`.\n",
            encoding="utf-8",
        )
        (self.root / "tasks" / "BK-01.md").write_text(
            "# BK-01 Core\n\n"
            "Owner: terra-high\n"
            "Depends on: none\n\n"
            "## Goal\nImplement state values.\n\n"
            "## Context\n"
            "Dispatch Terra High with `gpt-5.6-terra` and `high`.\n\n"
            "## Write Scope\n- `src/state.py`\n\n"
            "## Steps\n"
            "1. Call `project_index_query` and persist its `trace_id` receipt.\n"
            "2. Before writing, call `worktree_checkpoint_create`.\n"
            "3. Write the failing test and minimum implementation.\n\n"
            "## Acceptance\n"
            '1. Call `project_index_sync(bind_as="output")`.\n'
            "2. Call `project_index_query` on the output and persist its `trace_id` receipt.\n"
            '3. Call `workflow_artifact_register(kind="verification", snapshot_id=...)`.\n'
            "4. Only then call `workflow_complete`.\n\n"
            "## Return\nChanged files, command, output, unresolved blockers.\n",
            encoding="utf-8",
        )

    def write_strict_read_only_package(self) -> None:
        self.write_strict_terra_package()
        (self.root / "tasks" / "BK-01.md").write_text(
            "# BK-01 Core\n\n"
            "Owner: terra-investigator\n"
            "Depends on: none\n\n"
            "## Goal\nInspect state values.\n\n"
            "## Context\nRead-only investigation.\n\n"
            "## Write Scope\n- none\n\n"
            "## Steps\n"
            "1. Call `project_index_query` and persist its `trace_id` receipt.\n\n"
            "## Acceptance\n"
            '1. Call `workflow_artifact_register(kind="verification", snapshot_id=...)`.\n'
            "2. Only then call `workflow_complete`.\n\n"
            "## Return\nCommand, output, and unresolved blockers.\n",
            encoding="utf-8",
        )

    def validate_strict(self) -> list[str]:
        validator = load_validator()
        self.assertIn(
            "strict_index",
            inspect.signature(validator.validate_work_package).parameters,
            "validator must expose opt-in strict-index checks",
        )
        return validator.validate_work_package(self.root, strict_index=True)

    def test_valid_layered_package_passes(self) -> None:
        self.write_valid_package()
        validator = load_validator()

        errors = validator.validate_work_package(self.root)

        self.assertEqual([], errors)

    def test_shape_validator_is_explicitly_diagnostic_only(self) -> None:
        validator = load_validator()

        self.assertIn("diagnostic-only", validator.__doc__ or "")

    def test_legacy_package_explicitly_preserves_strict_index_false(self) -> None:
        self.write_valid_package()
        validator = load_validator()
        self.assertIn(
            "strict_index",
            inspect.signature(validator.validate_work_package).parameters,
            "validator must expose opt-in strict-index checks",
        )

        errors = validator.validate_work_package(self.root, strict_index=False)

        self.assertEqual([], errors)

    def test_legacy_read_only_package_accepts_explicit_none_write_scope(self) -> None:
        self.write_valid_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            .replace("Owner: terra", "Owner: luna-triage")
            .replace("- `src/state.py`", "- none"),
            encoding="utf-8",
        )
        validator = load_validator()

        errors = validator.validate_work_package(self.root)

        self.assertEqual([], errors)

    def test_strict_terra_high_package_with_all_index_gates_passes(self) -> None:
        self.write_strict_terra_package()

        errors = self.validate_strict()

        self.assertEqual([], errors)

    def test_strict_read_only_package_requires_only_read_only_gates(self) -> None:
        self.write_strict_read_only_package()

        errors = self.validate_strict()

        self.assertEqual([], errors)

    def test_strict_read_only_package_requires_its_gate_sequence(self) -> None:
        self.write_strict_read_only_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            .replace("project_index_query", "index lookup")
            .replace("trace_id", "query receipt")
            .replace(
                'workflow_artifact_register(kind="verification", snapshot_id=...)',
                "verification evidence",
            )
            .replace("workflow_complete", "completion"),
            encoding="utf-8",
        )

        errors = self.validate_strict()

        for marker in STRICT_READ_ONLY_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(any(marker in error for error in errors), errors)

    def test_strict_package_requires_index_first_and_completion_markers(self) -> None:
        self.write_strict_terra_package()
        index = self.root / "index.md"
        index.write_text(
            index.read_text(encoding="utf-8")
            .replace("project_index_sync", "initial index")
            .replace("workflow_register_task", "task registration")
            .replace("strict_index=true", "strict mode"),
            encoding="utf-8",
        )
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            .replace("project_index_query", "index lookup")
            .replace("trace_id", "query receipt")
            .replace("worktree_checkpoint_create", "checkpoint")
            .replace('project_index_sync(bind_as="output")', "output snapshot")
            .replace(
                'workflow_artifact_register(kind="verification", snapshot_id=...)',
                "verification evidence",
            )
            .replace("workflow_complete", "completion"),
            encoding="utf-8",
        )

        errors = self.validate_strict()

        for marker in (
            "project_index_sync",
            "workflow_register_task",
            "strict_index=true",
            "project_index_query",
            "trace_id",
            "worktree_checkpoint_create",
            'project_index_sync(bind_as="output")',
            'workflow_artifact_register(kind="verification", snapshot_id=...)',
            "workflow_complete",
        ):
            with self.subTest(marker=marker):
                self.assertTrue(any(marker in error for error in errors), errors)

    def test_strict_package_rejects_luna_without_an_exact_attested_pair(self) -> None:
        self.write_strict_terra_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "Owner: terra-high", "Owner: luna-unavailable"
            ),
            encoding="utf-8",
        )

        errors = self.validate_strict()

        self.assertTrue(
            any(
                "Luna code dispatch requires attested gpt-5.6-luna with low, medium, high, or xhigh"
                for error in errors
            )
        )

    def test_strict_package_allows_luna_with_an_exact_attested_pair(self) -> None:
        self.write_strict_terra_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            .replace("Owner: terra-high", "Owner: luna-high")
            .replace(
                "Dispatch Terra High with `gpt-5.6-terra` and `high`.",
                "Dispatch Luna only when the host capability report attests the exact "
                "`gpt-5.6-luna` and `medium` pair.",
            ),
            encoding="utf-8",
        )

        errors = self.validate_strict()

        self.assertEqual([], errors)

    def test_strict_package_allows_terra_documentation_only_scope(self) -> None:
        self.write_strict_terra_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            .replace("Owner: terra-high", "Owner: terra-doc-writer")
            .replace("`src/state.py`", "`docs/state.md`")
            .replace(
                "Dispatch Terra High with `gpt-5.6-terra` and `high`.",
                "Dispatch Terra High for documentation only.",
            ),
            encoding="utf-8",
        )

        errors = self.validate_strict()

        self.assertEqual([], errors)

    def test_monolithic_package_without_task_cards_fails(self) -> None:
        (self.root / "product-brief.md").write_text("# Brief\n", encoding="utf-8")
        (self.root / "index.md").write_text(
            "# One giant plan\n" * 200, encoding="utf-8"
        )
        validator = load_validator()

        errors = validator.validate_work_package(self.root)

        self.assertTrue(any("tasks" in error.lower() for error in errors))

    def test_product_brief_rejects_code_and_excess_length(self) -> None:
        self.write_valid_package()
        (self.root / "product-brief.md").write_text(
            "# Brief\n\n```python\nprint('implementation detail')\n```\n"
            + "direction\n" * 130,
            encoding="utf-8",
        )
        validator = load_validator()

        errors = validator.validate_work_package(self.root)

        self.assertTrue(
            any("product-brief.md" in error and "120" in error for error in errors)
        )
        self.assertTrue(
            any(
                "product-brief.md" in error and "code fence" in error
                for error in errors
            )
        )

    def test_task_card_requires_one_owner_and_write_scope(self) -> None:
        self.write_valid_package()
        (self.root / "tasks" / "BK-01.md").write_text(
            "# BK-01\n\nOwner: terra, sol\n\n## Goal\nDo work.\n",
            encoding="utf-8",
        )
        validator = load_validator()

        errors = validator.validate_work_package(self.root)

        self.assertTrue(any("one owner" in error.lower() for error in errors))
        self.assertTrue(any("write scope" in error.lower() for error in errors))

    def test_parallel_integration_requires_bound_non_worker_sol_receipt(self) -> None:
        validator = load_validator()
        valid_record = valid_integration_record()

        self.assertEqual(
            [], validator.validate_parallel_integration_record(valid_record)
        )

        missing_receipt = {**valid_record}
        missing_receipt.pop("review_receipt")
        review_receipt = cast(dict[str, object], valid_record["review_receipt"])
        worker_receipt = {
            **valid_record,
            "review_receipt": {
                **review_receipt,
                "reviewer_task_id": valid_record["task_id"],
            },
        }
        wrong_source = {
            **valid_record,
            "review_receipt": {
                **review_receipt,
                "source_branch": "feature/unreviewed-branch",
            },
        }
        invalid_receipt_hash = {
            **valid_record,
            "review_receipt": {
                **review_receipt,
                "receipt_hash": "not-a-receipt-hash",
            },
        }

        missing_errors = validator.validate_parallel_integration_record(missing_receipt)
        worker_errors = validator.validate_parallel_integration_record(worker_receipt)
        source_errors = validator.validate_parallel_integration_record(wrong_source)
        hash_errors = validator.validate_parallel_integration_record(
            invalid_receipt_hash
        )

        self.assertTrue(any("review_receipt" in error for error in missing_errors))
        self.assertTrue(any("non-worker Sol" in error for error in worker_errors))
        self.assertTrue(any("source_branch" in error for error in source_errors))
        self.assertTrue(any("receipt_hash" in error for error in hash_errors))

    def test_unknown_scope_state_fails_closed_and_cannot_hide_overlap(self) -> None:
        validator = load_validator()
        record = {
            **valid_integration_record(),
            "active_write_scopes": [
                {"task": "ATLAS-12A", "state": "running", "paths": ["src"]},
                {
                    "task": "ATLAS-12B",
                    "state": "pretend_finished",
                    "paths": ["src/routing.py"],
                },
            ],
        }

        errors = validator.validate_parallel_integration_record(record)

        self.assertTrue(any("unknown state" in error for error in errors))
        self.assertTrue(
            any("overlapping active write scopes" in error for error in errors)
        )

        terminal = {
            **record,
            "active_write_scopes": [
                {"task": "ATLAS-12A", "state": "running", "paths": ["src"]},
                {
                    "task": "ATLAS-12B",
                    "state": "done",
                    "paths": ["src/routing.py"],
                },
            ],
        }
        terminal_errors = validator.validate_parallel_integration_record(terminal)
        self.assertFalse(
            any("overlapping active write scopes" in error for error in terminal_errors)
        )

        non_text_state = {
            **record,
            "active_write_scopes": [
                {"task": "ATLAS-12A", "state": "running", "paths": ["src"]},
                {"task": "ATLAS-12B", "state": [], "paths": ["src/routing.py"]},
            ],
        }
        non_text_errors = validator.validate_parallel_integration_record(non_text_state)
        self.assertTrue(any("unknown state" in error for error in non_text_errors))
        self.assertTrue(
            any("overlapping active write scopes" in error for error in non_text_errors)
        )

    def test_verification_lanes_block_core_failures_and_bound_deferrals(self) -> None:
        validator = load_validator()
        valid_record = {
            "acceptance_requested": True,
            "lanes": {
                "core": {"status": "passed", "evidence_hash": "core-evidence"},
                "extended": {
                    "status": "deferred",
                    "evidence_hash": "extended-evidence",
                    "owner": "release-owner",
                    "release_gate": "release-2026-07",
                    "timebox": "2026-07-30T12:00:00Z",
                },
                "platform": {
                    "status": "skipped",
                    "reason": "Windows hook lane is not scheduled locally",
                },
            },
        }

        self.assertEqual([], validator.validate_verification_lanes(valid_record))

        core_failed = {
            **valid_record,
            "lanes": {
                **valid_record["lanes"],
                "core": {"status": "failed", "evidence_hash": "core-evidence"},
            },
        }
        missing_deferred_evidence = {
            **valid_record,
            "lanes": {
                **valid_record["lanes"],
                "extended": {"status": "deferred"},
            },
        }
        unknown_lane = {
            **valid_record,
            "lanes": {**valid_record["lanes"], "legacy": {"status": "passed"}},
        }
        claimed_skipped_platform = {
            **valid_record,
            "platform_support_claimed": True,
        }

        core_errors = validator.validate_verification_lanes(core_failed)
        deferred_errors = validator.validate_verification_lanes(
            missing_deferred_evidence
        )
        lane_errors = validator.validate_verification_lanes(unknown_lane)
        platform_errors = validator.validate_verification_lanes(
            claimed_skipped_platform
        )

        self.assertTrue(
            any(
                "core" in error and "blocks acceptance" in error
                for error in core_errors
            )
        )
        self.assertTrue(
            any("extended deferred lane missing" in error for error in deferred_errors)
        )
        self.assertTrue(any("unknown lane" in error for error in lane_errors))
        self.assertTrue(
            any("platform support claim" in error for error in platform_errors)
        )

    def test_resume_packet_and_interface_handoff_are_bounded_and_ordered(self) -> None:
        validator = load_validator()
        packet = valid_resume_packet()
        handoff = {
            "artifact_kind": "contract",
            "artifact_hash": "contract-sha",
            "metadata": {"kind": "contract"},
            "interface_frozen": True,
            "steps": [
                "workflow_artifact_register",
                "workflow_message_send",
                "workflow_inbox",
                "workflow_artifact_resolve",
                "workflow_message_ack",
            ],
        }

        self.assertEqual([], validator.validate_crash_resume_packet(packet))
        self.assertEqual([], validator.validate_mcp_handoff_record(handoff))

        unsafe_packet = {**packet, "stdout": "unbounded raw output"}
        resume_steps = cast(list[str], packet["resume_steps"])
        unordered_packet = {
            **packet,
            "resume_steps": list(reversed(resume_steps)),
        }
        handoff_steps = cast(list[str], handoff["steps"])
        unordered_handoff = {**handoff, "steps": list(reversed(handoff_steps))}

        self.assertTrue(
            any(
                "forbidden" in error
                for error in validator.validate_crash_resume_packet(unsafe_packet)
            )
        )
        self.assertTrue(
            any(
                "out of order" in error
                for error in validator.validate_crash_resume_packet(unordered_packet)
            )
        )
        self.assertTrue(
            any(
                "out of order" in error
                for error in validator.validate_mcp_handoff_record(unordered_handoff)
            )
        )

    def test_resume_packet_rejects_unbounded_or_unstructured_payloads(self) -> None:
        validator = load_validator()
        packet = valid_resume_packet()
        invalid_packets = {
            "workflow text": {**packet, "workflow_id": "x" * 10_000},
            "task identity": {**packet, "task_id": "ATLAS 12A\nstdout: secret"},
            "lease epoch": {**packet, "lease_epoch": 2**80},
            "endpoint": {**packet, "current_endpoint": "agent\nstderr: secret"},
            "commit": {**packet, "candidate_commit": "candidate-sha"},
            "branch/worktree": {**packet, "branch_or_worktree": "x" * 10_000},
            "scope hash": {**packet, "write_scope_hash": "scope-sha"},
            "next action": {**packet, "next_action": "x" * 10_000},
            "artifact hash": {
                **packet,
                "contract_hashes": ["token=raw-secret-value"],
            },
            "raw log summary": {
                **packet,
                "latest_green": {
                    "command": "python -m pytest focused",
                    "result": "Authorization: Bearer raw-secret-value",
                },
            },
        }

        for label, invalid in invalid_packets.items():
            with self.subTest(label=label):
                self.assertTrue(
                    validator.validate_crash_resume_packet(invalid),
                    f"unsafe packet accepted: {label}",
                )


if __name__ == "__main__":
    unittest.main()
