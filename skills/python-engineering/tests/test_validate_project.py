from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_project.py"


def run_validator(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )


def write_common_files(repo: Path) -> None:
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")


def test_explicit_uv_no_package_application_does_not_need_build_system() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        write_common_files(repo)
        (repo / "pyproject.toml").write_text(
            """
[project]
name = "example-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.uv]
package = false
""".strip()
            + "\n",
            encoding="utf-8",
        )

        result = run_validator(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "缺少 [build-system]" not in result.stdout


def test_package_project_still_requires_build_system() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        write_common_files(repo)
        (repo / "src" / "example_package").mkdir(parents=True)
        (repo / "pyproject.toml").write_text(
            """
[project]
name = "example-package"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
""".strip()
            + "\n",
            encoding="utf-8",
        )

        result = run_validator(repo)

    assert result.returncode == 1
    assert "缺少 [build-system]" in result.stdout
