"""Contract tests for bounded, privacy-safe execution receipts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from code_atlas.canonical import canonical_hash, canonical_json
from code_atlas.receipts import (
    ReceiptConflictError,
    ReceiptIntegrityError,
    ReceiptRepository,
    normalize_post_tool_use,
)
from code_atlas.security import MAX_COMMAND_SPEC_BYTES


ROOT = Path(__file__).resolve().parents[2]
_SECRET = "sk-fixture-secret-token-123456789"
_OUTPUT = "complete command output with sk-fixture-output-token-123456789"
_WORKSPACE = "D:/private/fixture-project"
_OPAQUE_CREDENTIAL = "fixtureCredentialValue9"


def _codex_shell_payload(*, exit_code: int = 0) -> dict[str, object]:
    return {
        "plugin_trusted": True,
        "host": "codex",
        "session_id": "session-fixture-1",
        "turn_id": "turn-fixture-1",
        "workspace": _WORKSPACE,
        "tool_name": "shell_command",
        "tool_use_id": "tool-shell-1",
        "tool_input": {
            "command": f"API_KEY={_SECRET} python -m pytest tests -q",
            "env": {"UNRELATED_ENV": "must-not-be-persisted"},
        },
        "tool_response": {
            "exit_code": exit_code,
            "stdout": _OUTPUT,
        },
        "observed_at": "2026-07-29T01:02:03+00:00",
    }


def _codex_patch_payload() -> dict[str, object]:
    return {
        "trusted": True,
        "host": "codex",
        "session_id": "session-fixture-1",
        "turn_id": "turn-fixture-1",
        "workspace": _WORKSPACE,
        "tool_name": "apply_patch",
        "tool_use_id": "tool-patch-1",
        "tool_input": {
            "patch": "*** Begin Patch\n*** Update File: package.py\n*** End Patch",
        },
        "tool_response": {"success": True, "output": _OUTPUT},
        "observed_at": "2026-07-29T01:02:03+00:00",
    }


def _nested_codex_payload() -> dict[str, object]:
    direct = _codex_shell_payload()
    return {
        "plugin_trusted": True,
        "host": "codex",
        "session_id": "session-fixture-1",
        "turn_id": "turn-fixture-1",
        "workspace": _WORKSPACE,
        "tool_name": "exec",
        "tool_use_id": "wrapper-1",
        "tool_response": {"results": [direct]},
        "observed_at": "2026-07-29T01:02:03+00:00",
    }


def test_direct_and_nested_payloads_normalize_to_equivalent_receipts() -> None:
    direct = normalize_post_tool_use(_codex_shell_payload())
    nested = normalize_post_tool_use(_nested_codex_payload())

    assert len(direct) == 1
    assert tuple(item.semantic_projection() for item in direct) == tuple(
        item.semantic_projection() for item in nested
    )
    assert direct[0].parent_tool_use_id == ""
    assert nested[0].parent_tool_use_id == "wrapper-1"


def test_successful_and_failed_exits_are_explicit_facts() -> None:
    succeeded = normalize_post_tool_use(_codex_shell_payload(exit_code=0))[0]
    failed = normalize_post_tool_use(_codex_shell_payload(exit_code=17))[0]

    assert (succeeded.exit_code, succeeded.success) == (0, True)
    assert (failed.exit_code, failed.success) == (17, False)


@pytest.mark.parametrize(
    ("tool_name", "canonical_tool", "tool_input"),
    (
        ("Bash", "shell", {"command": "python -m pytest -q"}),
        (
            "Edit",
            "patch",
            {"file_path": "package.py", "old_string": "old", "new_string": "new"},
        ),
        ("Write", "patch", {"file_path": "package.py", "content": "new"}),
    ),
)
def test_direct_claude_native_tools_normalize_to_canonical_classes(
    tool_name: str, canonical_tool: str, tool_input: dict[str, str]
) -> None:
    payload = {
        "host": "claude",
        "context": {
            "trusted": True,
            "session_id": "claude-session-1",
            "turn_id": "claude-turn-1",
            "workspace": _WORKSPACE,
        },
        "tool_name": tool_name,
        "tool_use_id": f"claude-{tool_name.casefold()}-1",
        "tool_input": tool_input,
        "tool_response": {"success": True, "stdout": "safe fixture output"},
        "observed_at": "2026-07-29T01:02:03Z",
    }

    receipt = normalize_post_tool_use(payload)[0]

    assert receipt.host == "claude"
    assert receipt.canonical_tool == canonical_tool
    if canonical_tool == "patch":
        assert receipt.command_spec == ()


def test_nested_wrapper_normalizes_shell_and_patch_results() -> None:
    nested = _nested_codex_payload()
    nested["tool_response"] = {
        "results": [_codex_shell_payload(), _codex_patch_payload()]
    }

    nested_receipts = normalize_post_tool_use(nested)
    direct_receipts = (
        *normalize_post_tool_use(_codex_shell_payload()),
        *normalize_post_tool_use(_codex_patch_payload()),
    )

    assert tuple(item.semantic_projection() for item in nested_receipts) == tuple(
        item.semantic_projection() for item in direct_receipts
    )
    assert nested_receipts[1].command_spec == ()


def test_nested_wrapper_rejects_cross_host_tool_claims() -> None:
    nested = _nested_codex_payload()
    nested["tool_response"] = {
        "results": [
            {
                **_codex_shell_payload(),
                "host": "claude",
                "tool_name": "Bash",
                "tool_use_id": "mismatched-host-tool",
            }
        ]
    }

    assert normalize_post_tool_use(nested) == ()


def test_repository_stores_hashes_not_raw_output_or_environment(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = repository.capture(_codex_shell_payload())[0]
    stored = repository.read(receipt.receipt_id)
    body = canonical_json(stored)

    assert _SECRET not in body
    assert _OUTPUT not in body
    assert _WORKSPACE not in body
    assert "must-not-be-persisted" not in body
    assert stored.output_hash.startswith("sha256:")
    assert stored.command_spec_hash.startswith("sha256:")
    assert any("[REDACTED]" in item for item in stored.command_spec)


@pytest.mark.parametrize(
    "command",
    (
        f"tool --token {_OPAQUE_CREDENTIAL}",
        f"curl -H 'X-API-Key: {_OPAQUE_CREDENTIAL}' https://example.invalid",
        (
            "curl --data "
            f'\'{{"api_key":"{_OPAQUE_CREDENTIAL}"}}\' https://example.invalid'
        ),
    ),
)
def test_labeled_command_credentials_are_never_persisted(command: str) -> None:
    payload = _codex_shell_payload()
    payload["tool_input"] = {"command": command}

    receipt = normalize_post_tool_use(payload)[0]

    assert receipt.command_spec == ("[REDACTED]",)
    assert _OPAQUE_CREDENTIAL not in canonical_json(receipt)


def test_command_spec_redacts_quoted_environment_values_and_workspace_paths() -> None:
    opaque_environment_value = "opaque configuration value"
    payload = _codex_shell_payload()
    payload["tool_input"] = {
        "command": (
            f'FEATURE_FLAG="{opaque_environment_value}" '
            f"cd {_WORKSPACE} && python -m pytest -q"
        )
    }

    receipt = normalize_post_tool_use(payload)[0]
    body = canonical_json(receipt)

    assert opaque_environment_value not in body
    assert _WORKSPACE not in body
    assert any("[REDACTED]" in part for part in receipt.command_spec)
    assert "[REDACTED_PATH]" in receipt.command_spec


def test_patch_tools_store_empty_command_spec_and_patch_input_hash() -> None:
    payload = _codex_patch_payload()
    receipt = normalize_post_tool_use(payload)[0]

    assert receipt.canonical_tool == "patch"
    assert receipt.command_spec == ()
    assert receipt.command_spec_hash == canonical_hash(())
    assert receipt.input_hash == canonical_hash(payload["tool_input"])
    assert (receipt.exit_code, receipt.success) == (0, True)


def test_oversize_command_is_marked_without_persisting_its_text() -> None:
    payload = _codex_shell_payload()
    payload["tool_input"] = {
        "command": "sk-fixture-command-token-123456789 " + "x" * MAX_COMMAND_SPEC_BYTES,
    }

    receipt = normalize_post_tool_use(payload)[0]

    assert receipt.command_spec == ("[TRUNCATED COMMAND]",)
    assert "sk-fixture-command-token-123456789" not in canonical_json(receipt)


def test_malformed_or_untrusted_payloads_fail_open_without_files(
    tmp_path: Path,
) -> None:
    repository = ReceiptRepository(tmp_path)
    untrusted = _codex_shell_payload()
    untrusted["plugin_trusted"] = "true"
    missing_context = _codex_shell_payload()
    missing_context.pop("workspace")

    assert repository.capture({"unexpected": object()}) == ()
    assert repository.capture(untrusted) == ()
    assert repository.capture(missing_context) == ()
    assert tuple(tmp_path.rglob("*.json")) == ()


def test_invalid_timestamp_or_secret_shaped_tool_id_yields_no_receipt() -> None:
    malformed_time = _codex_shell_payload()
    malformed_time["observed_at"] = "2026-99-99T99:99:99Z"
    secret_shaped_tool_id = _codex_shell_payload()
    secret_shaped_tool_id["tool_use_id"] = _SECRET

    assert normalize_post_tool_use(malformed_time) == ()
    assert normalize_post_tool_use(secret_shaped_tool_id) == ()


def test_overdepth_wrapper_payload_quietly_yields_no_receipts() -> None:
    nested: dict[str, object] = _codex_shell_payload()
    for index in range(5):
        nested = {
            "tool_name": "exec",
            "tool_use_id": f"wrapper-{index}",
            "tool_response": {"results": [nested]},
        }
    nested.update(
        {
            "plugin_trusted": True,
            "host": "codex",
            "session_id": "session-fixture-1",
            "turn_id": "turn-fixture-1",
            "workspace": _WORKSPACE,
            "observed_at": "2026-07-29T01:02:03Z",
        }
    )

    assert normalize_post_tool_use(nested) == ()


def test_repository_rejects_same_id_content_conflict(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = repository.capture(_codex_shell_payload())[0]
    conflict = replace(receipt, success=False)

    with pytest.raises(ReceiptConflictError):
        repository.store(conflict)


def test_replay_deduplicates_despite_a_new_observation_time(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    first = repository.capture(_codex_shell_payload())[0]
    replay = _codex_shell_payload()
    replay["observed_at"] = "2026-07-29T01:03:04+00:00"
    repeated = repository.capture(replay)[0]

    assert repeated.receipt_id == first.receipt_id
    assert repeated.observed_at == first.observed_at
    assert len(tuple(tmp_path.rglob("*.json"))) == 1


def test_repository_rejects_noncanonical_or_tampered_content(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = repository.capture(_codex_shell_payload())[0]
    path = (
        tmp_path / "code-atlas-receipts" / "sha256" / f"{receipt.receipt_id[7:]}.json"
    )
    decoded = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(decoded, indent=2), encoding="utf-8")

    with pytest.raises(ReceiptIntegrityError):
        repository.read(receipt.receipt_id)


def test_hook_is_silent_and_fail_open_for_malformed_or_untrusted_payloads(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["BUGKILLER_HOME"] = str(tmp_path / "durable")
    environment.pop("PLUGIN_DATA", None)
    environment.pop("CODEX_HOME", None)

    malformed = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "execution_receipt.py")],
        input="not-json",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    untrusted = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "execution_receipt.py")],
        input=json.dumps({"trusted": False}),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert malformed.returncode == 0
    assert untrusted.returncode == 0
    assert malformed.stdout == untrusted.stdout == ""
    assert tuple((tmp_path / "durable").rglob("*.json")) == ()


def test_hook_writes_a_bounded_receipt_without_stdout(tmp_path: Path) -> None:
    environment = os.environ.copy()
    durable_root = tmp_path / "durable"
    environment["BUGKILLER_HOME"] = str(durable_root)
    environment.pop("PLUGIN_DATA", None)
    environment.pop("CODEX_HOME", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "execution_receipt.py")],
        input=json.dumps(_codex_shell_payload()),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    stored = tuple(durable_root.rglob("*.json"))
    assert len(stored) == 1
    assert _SECRET not in stored[0].read_text(encoding="utf-8")
