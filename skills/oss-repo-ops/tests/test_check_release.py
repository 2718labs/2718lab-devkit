from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release.py"


def write_common_release_files(repo: Path) -> None:
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "README.md").write_text("# Example\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.2.0] - 2026-07-26\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (repo / "LICENSE").write_text("AGPL-3.0\n", encoding="utf-8")
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\n",
        encoding="utf-8",
    )


def run_check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_codex_plugin_repository_does_not_require_astrbot_metadata() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / "2718lab-devkit"
        (repo / ".codex-plugin").mkdir(parents=True)
        write_common_release_files(repo)
        (repo / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "2718lab-devkit",
                    "version": "0.2.0",
                    "description": "Codex development toolkit.",
                    "author": {"name": "2718lab"},
                    "homepage": "https://github.com/2718labs/2718lab-devkit",
                    "repository": "https://github.com/2718labs/2718lab-devkit",
                    "license": "AGPL-3.0",
                }
            ),
            encoding="utf-8",
        )

        result = run_check(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[META]" not in result.stdout
    assert "0 error(s)" in result.stdout


def test_codex_plugin_repository_rejects_invalid_manifest_version() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / "2718lab-devkit"
        (repo / ".codex-plugin").mkdir(parents=True)
        write_common_release_files(repo)
        (repo / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "2718lab-devkit",
                    "version": "0.2",
                    "description": "Codex development toolkit.",
                    "author": {"name": "2718lab"},
                    "repository": "https://github.com/2718labs/2718lab-devkit",
                    "license": "AGPL-3.0",
                }
            ),
            encoding="utf-8",
        )

        result = run_check(repo)

    assert result.returncode == 1
    assert "[CODEX_VER]" in result.stdout
