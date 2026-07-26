from __future__ import annotations

import importlib.util
import inspect
import os
import tempfile
import unittest
from pathlib import Path

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

    def write_strict_sol_package(self) -> None:
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
            "Owner: sol-code-writer\n"
            "Depends on: none\n\n"
            "## Goal\nImplement state values.\n\n"
            "## Context\n"
            "Dispatch the code writer with `gpt-5.6-sol` and `ultra`.\n\n"
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
        self.write_strict_sol_package()
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

    def test_strict_sol_ultra_package_with_all_index_gates_passes(self) -> None:
        self.write_strict_sol_package()

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
        self.write_strict_sol_package()
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

    def test_strict_package_rejects_luna_or_terra_for_code_scope(self) -> None:
        self.write_strict_sol_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "Owner: sol-code-writer", "Owner: terra-doc-writer"
            ),
            encoding="utf-8",
        )

        errors = self.validate_strict()

        self.assertTrue(
            any("Luna/Terra" in error and "code" in error for error in errors)
        )

    def test_strict_package_allows_terra_documentation_only_scope(self) -> None:
        self.write_strict_sol_package()
        card = self.root / "tasks" / "BK-01.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            .replace("Owner: sol-code-writer", "Owner: terra-doc-writer")
            .replace("`src/state.py`", "`docs/state.md`")
            .replace(
                "Dispatch the code writer with `gpt-5.6-sol` and `ultra`.",
                "Dispatch Terra for documentation only; Terra never writes code.",
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


if __name__ == "__main__":
    unittest.main()
