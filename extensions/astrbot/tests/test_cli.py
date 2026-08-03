from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

import devkit_astrbot
from devkit_astrbot.cli import main


def test_cli_scaffold_and_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scaffold", "astrbot_plugin_echo", str(tmp_path)]) == 0
    plugin = tmp_path / "astrbot_plugin_echo"
    assert main(["validate", str(plugin)]) == 0

    output = capsys.readouterr().out
    assert "created" in output
    assert "0 errors" in output


def test_import_does_not_load_astrbot_runtime() -> None:
    import devkit_astrbot  # noqa: F401

    assert "astrbot" not in sys.modules


def test_package_uses_pep440_release_candidate_version() -> None:
    assert getattr(devkit_astrbot, "__version__", None) == "1.0.0rc4"
    assert distribution_version("2718lab-devkit-astrbot") == "1.0.0rc4"


def test_cli_reports_semver_release_candidate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "devkit-astrbot 1.0.0-rc4\n"


def test_isolated_import_has_no_astrbot_primary_or_environment_side_effects(
    tmp_path: Path,
) -> None:
    data_directory = tmp_path / "astrbot-data"
    data_directory.mkdir()
    script = textwrap.dedent(
        """
        import socket
        import sys
        from pathlib import Path

        data_directory = Path(sys.argv[1])
        before = tuple(data_directory.rglob("*"))

        def deny_network(*args, **kwargs):
            raise AssertionError("network access during import")

        socket.create_connection = deny_network
        socket.getaddrinfo = deny_network
        socket.socket.connect = deny_network

        import devkit_astrbot

        blocked_roots = {
            "astrbot",
            "bugkiller",
            "code_atlas",
            "devkit_relay",
            "orchestrator",
            "project_index",
        }
        loaded_roots = {name.partition(".")[0] for name in sys.modules}
        assert blocked_roots.isdisjoint(loaded_roots)
        assert tuple(data_directory.rglob("*")) == before
        """
    )
    environment = {
        **os.environ,
        "ASTRBOT_DATA_DIR": str(data_directory),
        "PLUGIN_DATA": str(data_directory),
    }

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(data_directory)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
