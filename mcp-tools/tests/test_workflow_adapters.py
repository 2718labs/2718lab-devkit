"""Tests for deterministic, shell-free workflow command profiles."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.adapters import LanguageKind, detect_repository


class WorkflowAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.task_temp = self.root / "task-temp"
        self.task_temp.mkdir()

    def test_python_profile_uses_structured_pytest_command(self) -> None:
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "sample"\nversion = "0.1.0"\n', encoding="utf-8"
        )

        result = detect_repository(self.root, task_temp_root=self.task_temp)

        self.assertIsNone(result.blocked_reason)
        self.assertEqual(
            (LanguageKind.PYTHON,), tuple(item.kind for item in result.profiles)
        )
        command = result.profiles[0].commands[0]
        self.assertEqual("python", command.executable)
        self.assertEqual(("-m", "pytest"), command.args)
        self.assertFalse(command.requires_gate)
        self.assertFalse(command.network)

    def test_javascript_emits_only_declared_lifecycle_scripts_as_gated(self) -> None:
        package = {
            "scripts": {
                "test": "vitest run",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "postinstall": "curl https://example.invalid",
            }
        }
        (self.root / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (self.root / "pnpm-lock.yaml").write_text(
            "lockfileVersion: '9.0'\n", encoding="utf-8"
        )

        result = detect_repository(self.root, task_temp_root=self.task_temp)

        self.assertIsNone(result.blocked_reason)
        profile = result.profiles[0]
        self.assertEqual(LanguageKind.JAVASCRIPT, profile.kind)
        self.assertEqual(
            (("test",), ("lint",), ("typecheck",)),
            tuple(command.args for command in profile.commands),
        )
        self.assertTrue(
            all(command.executable == "pnpm" for command in profile.commands)
        )
        self.assertTrue(all(command.requires_gate for command in profile.commands))
        self.assertNotIn(
            "postinstall", {arg for command in profile.commands for arg in command.args}
        )

    def test_conflicting_javascript_lockfiles_are_blocked(self) -> None:
        (self.root / "package.json").write_text(
            '{"scripts":{"test":"vitest"}}', encoding="utf-8"
        )
        (self.root / "package-lock.json").write_text("{}", encoding="utf-8")
        (self.root / "yarn.lock").write_text("", encoding="utf-8")

        result = detect_repository(self.root, task_temp_root=self.task_temp)

        self.assertEqual("conflicting_javascript_lockfiles", result.blocked_reason)
        self.assertEqual((), result.profiles)

    def test_multiple_npm_lockfiles_are_also_blocked(self) -> None:
        (self.root / "package.json").write_text(
            '{"scripts":{"test":"node test.js"}}', encoding="utf-8"
        )
        (self.root / "package-lock.json").write_text("{}", encoding="utf-8")
        (self.root / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")

        result = detect_repository(self.root, task_temp_root=self.task_temp)

        self.assertEqual("conflicting_javascript_lockfiles", result.blocked_reason)
        self.assertEqual((), result.profiles)

    def test_mixed_rust_and_go_repository_preserves_isolated_command_specs(
        self,
    ) -> None:
        (self.root / "Cargo.toml").write_text(
            '[package]\nname="sample"\nversion="0.1.0"\n', encoding="utf-8"
        )
        (self.root / "go.mod").write_text(
            "module example.test/sample\n\ngo 1.23\n", encoding="utf-8"
        )

        result = detect_repository(self.root, task_temp_root=self.task_temp)

        self.assertIsNone(result.blocked_reason)
        self.assertEqual(
            (LanguageKind.RUST, LanguageKind.GO),
            tuple(item.kind for item in result.profiles),
        )
        rust_command = result.profiles[0].commands[0]
        self.assertEqual("cargo", rust_command.executable)
        self.assertEqual(("test",), rust_command.args)
        self.assertEqual(
            str((self.task_temp / "cargo-target").resolve()),
            dict(rust_command.env)["CARGO_TARGET_DIR"],
        )
        go_command = result.profiles[1].commands[0]
        self.assertEqual(("test", "./..."), go_command.args)

    def test_unknown_repository_returns_structured_block(self) -> None:
        result = detect_repository(self.root, task_temp_root=self.task_temp)

        self.assertEqual("unknown_build_system", result.blocked_reason)
        self.assertEqual((), result.profiles)


if __name__ == "__main__":
    unittest.main()
