"""Contract tests for bounded, privacy-safe execution receipts."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_atlas.canonical import canonical_hash, canonical_json
from code_atlas.receipts import (
    EVIDENCE_KEY_FILENAME,
    HostCaptureContext,
    RawExecutionReceipt,
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
_TEST_EVIDENCE_KEY = bytes(range(32))


def _host_capture_context(
    payload: dict[str, object], *, host: str | None = None
) -> HostCaptureContext:
    nested = payload.get("context")
    sources = (payload, nested) if isinstance(nested, dict) else (payload,)

    def first_string(*keys: str) -> str:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str):
                    return value
        return ""

    raw_host = payload.get("host")
    normalized = (
        "".join(character for character in raw_host.casefold() if character.isalnum())
        if isinstance(raw_host, str)
        else ""
    )
    context_host = host or {
        "claude": "claude",
        "claudecode": "claude",
        "codex": "codex",
        "openaicodex": "codex",
    }.get(normalized, "codex")
    return HostCaptureContext(
        host=context_host,
        session_id=first_string("session_id", "sessionId"),
        turn_id=first_string("turn_id", "turnId"),
        workspace=first_string(
            "workspace", "cwd", "working_directory", "workingDirectory"
        ),
        observed_at=first_string("observed_at", "observedAt", "timestamp")
        or "2026-07-29T01:02:03Z",
    )


def _normalize(
    payload: dict[str, object],
    *,
    host: str | None = None,
    evidence_key: bytes = _TEST_EVIDENCE_KEY,
) -> tuple[RawExecutionReceipt, ...]:
    return normalize_post_tool_use(
        payload,
        capture_context=_host_capture_context(payload, host=host),
        evidence_key=evidence_key,
    )


def _capture(
    repository: ReceiptRepository, payload: dict[str, object]
) -> tuple[RawExecutionReceipt, ...]:
    return repository.capture(payload, capture_context=_host_capture_context(payload))


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
    direct = _normalize(_codex_shell_payload())
    nested = _normalize(_nested_codex_payload())

    assert len(direct) == 1
    assert tuple(item.semantic_projection() for item in direct) == tuple(
        item.semantic_projection() for item in nested
    )
    assert direct[0].parent_tool_use_id == ""
    assert nested[0].parent_tool_use_id == "wrapper-1"


def test_payload_trust_flags_cannot_authenticate_a_capture_or_infer_host(
    tmp_path: Path,
) -> None:
    self_asserted = _codex_shell_payload()
    self_asserted["trusted"] = True
    missing_host = dict(self_asserted)
    missing_host.pop("host")

    assert normalize_post_tool_use(self_asserted) == ()
    assert normalize_post_tool_use(missing_host) == ()
    repository = ReceiptRepository(tmp_path)
    assert repository.capture(self_asserted) == ()
    assert not repository.evidence_key_path.exists()
    assert not repository.receipt_root.exists()


def test_authenticated_context_requires_explicit_recognized_matching_root_host() -> (
    None
):
    missing_host = _codex_shell_payload()
    missing_host.pop("host")
    unknown_host = _codex_shell_payload()
    unknown_host["host"] = "self-asserted-unknown-host"
    mismatched_host = _codex_shell_payload()

    assert _normalize(missing_host, host="codex") == ()
    assert _normalize(unknown_host, host="codex") == ()
    assert _normalize(mismatched_host, host="claude") == ()


def test_successful_and_failed_exits_are_explicit_facts() -> None:
    succeeded = _normalize(_codex_shell_payload(exit_code=0))[0]
    failed = _normalize(_codex_shell_payload(exit_code=17))[0]

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

    receipt = _normalize(payload)[0]

    assert receipt.host == "claude"
    assert receipt.canonical_tool == canonical_tool
    if canonical_tool == "patch":
        assert receipt.command_spec == ()


def test_nested_wrapper_normalizes_shell_and_patch_results() -> None:
    nested = _nested_codex_payload()
    nested["tool_response"] = {
        "results": [_codex_shell_payload(), _codex_patch_payload()]
    }

    nested_receipts = _normalize(nested)
    direct_receipts = (
        *_normalize(_codex_shell_payload()),
        *_normalize(_codex_patch_payload()),
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

    assert _normalize(nested) == ()


def test_repository_stores_hashes_not_raw_output_or_environment(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = _capture(repository, _codex_shell_payload())[0]
    stored = repository.read(receipt.receipt_id)
    body = canonical_json(stored)

    assert _SECRET not in body
    assert _OUTPUT not in body
    assert _WORKSPACE not in body
    assert "must-not-be-persisted" not in body
    assert stored.output_hash.startswith("sha256:")
    assert stored.command_spec_hash.startswith("sha256:")
    assert any("[REDACTED]" in item for item in stored.command_spec)


def test_raw_evidence_values_are_not_plain_guessable_hashes() -> None:
    payload = _codex_shell_payload()
    receipt = _normalize(payload)[0]

    assert receipt.session_id_hash != canonical_hash({"value": payload["session_id"]})
    assert receipt.turn_id_hash != canonical_hash({"value": payload["turn_id"]})
    assert receipt.workspace_hash != canonical_hash({"value": payload["workspace"]})
    assert receipt.input_hash != canonical_hash(payload["tool_input"])
    assert receipt.output_hash != canonical_hash(payload["tool_response"])


def test_equal_raw_evidence_uses_distinct_per_install_digests(tmp_path: Path) -> None:
    payload = _codex_shell_payload()
    first = _capture(ReceiptRepository(tmp_path / "first"), payload)[0]
    second = _capture(ReceiptRepository(tmp_path / "second"), payload)[0]

    assert first.input_hash != second.input_hash
    assert first.output_hash != second.output_hash
    assert first.workspace_hash != second.workspace_hash


def test_repository_rejects_receipt_signed_by_a_different_key(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    payload = _codex_shell_payload()
    repository.normalize(payload, capture_context=_host_capture_context(payload))
    forged = _normalize(payload, evidence_key=b"x" * 32)[0]

    with pytest.raises(ReceiptIntegrityError, match="receipt_content_invalid"):
        repository.store(forged)


def test_evidence_key_is_outside_receipt_reader_tree(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = _capture(repository, _codex_shell_payload())[0]
    receipt_tree = tmp_path / "code-atlas-receipts"

    assert repository.evidence_key_path == tmp_path / EVIDENCE_KEY_FILENAME
    assert repository.evidence_key_path.is_file()
    assert repository.evidence_key_path not in tuple(receipt_tree.rglob("*"))
    assert EVIDENCE_KEY_FILENAME not in canonical_json(
        repository.read(receipt.receipt_id)
    )
    assert repository.evidence_key_path.read_bytes().hex() not in canonical_json(
        receipt
    )


@pytest.mark.parametrize(
    ("mode", "attributes"),
    (
        (stat.S_IFLNK | 0o600, 0),
        (stat.S_IFREG | 0o600, 0x400),
    ),
)
def test_evidence_key_rejects_symlink_or_reparse_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    attributes: int,
) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = _capture(repository, _codex_shell_payload())[0]
    real_lstat = Path.lstat

    def unsafe_lstat(path: Path) -> object:
        if path == repository.evidence_key_path:
            return SimpleNamespace(
                st_mode=mode,
                st_file_attributes=attributes,
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", unsafe_lstat)
    with pytest.raises(ReceiptIntegrityError, match="evidence_key_unsafe"):
        repository.read(receipt.receipt_id)


def test_evidence_key_rejects_size_changes(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = _capture(repository, _codex_shell_payload())[0]
    repository.evidence_key_path.write_bytes(b"short")

    with pytest.raises(ReceiptIntegrityError, match="evidence_key_size_invalid"):
        repository.read(receipt.receipt_id)


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

    receipt = _normalize(payload)[0]

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

    receipt = _normalize(payload)[0]
    body = canonical_json(receipt)

    assert opaque_environment_value not in body
    assert _WORKSPACE not in body
    assert any("[REDACTED]" in part for part in receipt.command_spec)
    assert "[REDACTED_PATH]" in receipt.command_spec


@pytest.mark.parametrize(
    ("command", "forbidden_fragments"),
    (
        (
            r'cmd /c type "\\server\private\Alice\secret.txt"',
            ("server", "private", "Alice", "secret.txt"),
        ),
        (
            r'cmd /c type "%USERPROFILE%\Private\secret.txt"',
            ("USERPROFILE", "Private", "secret.txt"),
        ),
        (
            r'cmd /c type "C:\Users\Alice Example\Private\secret.txt"',
            ("Alice", "Example", "Private", "secret.txt"),
        ),
        (
            r'cmd /c type "~\Private\secret.txt"',
            ("Private", "secret.txt"),
        ),
        (
            r'powershell Get-Content "$HOME\Private\secret.txt"',
            ("HOME", "Private", "secret.txt"),
        ),
    ),
)
def test_windows_unc_and_home_paths_are_fully_redacted(
    command: str, forbidden_fragments: tuple[str, ...]
) -> None:
    payload = _codex_shell_payload()
    payload["tool_input"] = {"command": command}

    receipt = _normalize(payload)[0]
    body = canonical_json(receipt)

    assert "[REDACTED_PATH]" in receipt.command_spec
    assert not any(fragment in body for fragment in forbidden_fragments)


def test_windows_relative_paths_keep_backslashes_during_command_parsing() -> None:
    payload = _codex_shell_payload()
    payload["tool_input"] = {"command": r"python scripts\verify.py -q"}

    receipt = _normalize(payload)[0]

    assert r"scripts\verify.py" in receipt.command_spec


def test_mixed_windows_and_posix_private_paths_are_fully_redacted() -> None:
    payload = _codex_shell_payload()
    payload["tool_input"] = {
        "command": (
            r'cmd /c type "C:\Users\Windows Alice\Private\secret.txt" '
            "&& cat /home/posix-user/.ssh/id_rsa"
        )
    }

    receipt = _normalize(payload)[0]
    body = canonical_json(receipt)

    assert "[REDACTED_PATH]" in receipt.command_spec
    assert not any(
        fragment in body
        for fragment in (
            "Windows",
            "Alice",
            "Private",
            "secret.txt",
            "posix-user",
            ".ssh",
            "id_rsa",
        )
    )


def test_patch_tools_store_empty_command_spec_and_patch_input_hash() -> None:
    payload = _codex_patch_payload()
    receipt = _normalize(payload)[0]

    assert receipt.canonical_tool == "patch"
    assert receipt.command_spec == ()
    assert receipt.command_spec_hash == canonical_hash(())
    assert receipt.input_hash.startswith("sha256:")
    assert receipt.input_hash != canonical_hash(payload["tool_input"])
    assert (receipt.exit_code, receipt.success) == (0, True)


def test_oversize_command_is_marked_without_persisting_its_text() -> None:
    payload = _codex_shell_payload()
    payload["tool_input"] = {
        "command": "sk-fixture-command-token-123456789 " + "x" * MAX_COMMAND_SPEC_BYTES,
    }

    receipt = _normalize(payload)[0]

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

    assert _capture(repository, {"unexpected": object()}) == ()
    assert repository.capture(untrusted) == ()
    assert _capture(repository, missing_context) == ()
    assert tuple(tmp_path.rglob("*.json")) == ()


def test_invalid_timestamp_or_secret_shaped_tool_id_yields_no_receipt() -> None:
    malformed_time = _codex_shell_payload()
    malformed_time["observed_at"] = "2026-99-99T99:99:99Z"
    secret_shaped_tool_id = _codex_shell_payload()
    secret_shaped_tool_id["tool_use_id"] = _SECRET

    assert _normalize(malformed_time) == ()
    assert _normalize(secret_shaped_tool_id) == ()


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

    assert _normalize(nested) == ()


def test_repository_rejects_same_id_content_conflict(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = _capture(repository, _codex_shell_payload())[0]
    conflict = replace(receipt, success=False)

    with pytest.raises(ReceiptConflictError):
        repository.store(conflict)


def test_replay_deduplicates_despite_a_new_observation_time(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    first = _capture(repository, _codex_shell_payload())[0]
    replay = _codex_shell_payload()
    replay["observed_at"] = "2026-07-29T01:03:04+00:00"
    repeated = _capture(repository, replay)[0]

    assert repeated.receipt_id == first.receipt_id
    assert repeated.observed_at == first.observed_at
    assert len(tuple(tmp_path.rglob("*.json"))) == 1


def test_interrupted_atomic_publish_leaves_valid_retryable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = ReceiptRepository(tmp_path)
    payload = _codex_shell_payload()
    receipt = repository.normalize(
        payload, capture_context=_host_capture_context(payload)
    )[0]
    final_path = (
        repository.receipt_root / f"{receipt.receipt_id.removeprefix('sha256:')}.json"
    )
    events: list[str] = []
    real_fsync = os.fsync
    real_link = os.link

    def recording_fsync(file_descriptor: int) -> None:
        events.append("fsync")
        real_fsync(file_descriptor)

    def publish_then_interrupt(source: str | Path, target: str | Path) -> None:
        events.append("publish")
        real_link(source, target)
        raise OSError("simulated interruption after atomic publish")

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "link", publish_then_interrupt)
    with pytest.raises(OSError, match="simulated interruption"):
        repository.store(receipt)

    assert events.index("fsync") < events.index("publish")
    assert final_path.is_file()
    assert tuple(repository.receipt_root.iterdir()) == (final_path,)
    monkeypatch.undo()
    assert repository.store(receipt) == receipt
    assert repository.read(receipt.receipt_id) == receipt


def test_atomic_publish_only_cleans_up_its_owned_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = ReceiptRepository(tmp_path)
    payload = _codex_shell_payload()
    receipt = repository.normalize(
        payload, capture_context=_host_capture_context(payload)
    )[0]
    replacement = b"unowned replacement"
    stage_path: Path | None = None

    def replace_stage_then_interrupt(source: str | Path, target: str | Path) -> None:
        nonlocal stage_path
        del target
        stage_path = Path(source)
        stage_path.unlink()
        stage_path.write_bytes(replacement)
        raise OSError("simulated stage replacement")

    monkeypatch.setattr(os, "link", replace_stage_then_interrupt)
    with pytest.raises(OSError, match="simulated stage replacement"):
        repository.store(receipt)

    assert stage_path is not None
    assert stage_path.read_bytes() == replacement
    stage_path.unlink()


def test_repository_rejects_noncanonical_or_tampered_content(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = _capture(repository, _codex_shell_payload())[0]
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
        input=json.dumps(
            {
                "trusted": True,
                "session_id": "forged-session",
                "turn_id": "forged-turn",
                "workspace": _WORKSPACE,
                "tool_name": "shell_command",
                "tool_use_id": "forged-tool",
                "tool_input": {"command": "python -m pytest"},
                "tool_response": {"exit_code": 0},
            }
        ),
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

    payload = _codex_shell_payload()
    payload.pop("plugin_trusted")
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "execution_receipt.py")],
        input=json.dumps(payload),
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
    evidence_key = durable_root / EVIDENCE_KEY_FILENAME
    assert evidence_key.is_file()
    assert evidence_key not in tuple((durable_root / "code-atlas-receipts").rglob("*"))
    assert evidence_key.read_bytes().hex() not in stored[0].read_text(encoding="utf-8")
