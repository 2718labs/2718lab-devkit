"""Account-usage quota coordinator removal inventory."""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_runtime.host_bridge import InheritedHandleHostBridge


def test_account_usage_quota_artifacts_are_physically_removed() -> None:
    root = Path(__file__).resolve().parents[1]
    removed = (
        root / "devkit_fastlane" / "scripts" / "codex_account_quota.py",
        root / "devkit_fastlane" / "scripts" / "fastlane_quota_balance.py",
        root
        / "devkit_fastlane"
        / "assets"
        / "fastlane-quota-balance-policy-v1.json",
        root
        / "devkit_fastlane"
        / "assets"
        / "fastlane-quota-balance-policy-v2.json",
        root / "devkit_fastlane" / "tests" / "test_codex_account_quota.py",
        root / "devkit_fastlane" / "tests" / "test_fastlane_quota_balance.py",
    )

    assert not [str(path.relative_to(root)) for path in removed if path.exists()]


def test_host_runtime_has_no_account_quota_transport_or_import() -> None:
    module = importlib.import_module("devkit_runtime.host_session")
    source = inspect.getsource(module)

    assert "codex_account_quota" not in source
    assert "fastlane_quota_balance" not in source
    assert "HostQuota" not in source
    assert not hasattr(module.HostSession, "read_quota")
    assert not hasattr(InheritedHandleHostBridge, "request_quota_snapshot")
    assert not hasattr(InheritedHandleHostBridge, "receive_quota_snapshot")


def test_owned_runtime_sources_have_no_private_account_quota_reference() -> None:
    root = Path(__file__).resolve().parents[1]
    owned = (
        root / "devkit_fastlane" / "__init__.py",
        root / "devkit_runtime" / "fastlane_host_adapter.py",
        root / "devkit_runtime" / "fastlane_host_intent.py",
        root / "devkit_runtime" / "host_bridge.py",
        root / "devkit_runtime" / "host_session.py",
        root / "devkit_runtime" / "relay_runtime.py",
        root / "server.py",
    )

    residual = {
        str(path.relative_to(root)): line.strip()
        for path in owned
        for line in path.read_text(encoding="utf-8").splitlines()
        if "quota" in line.casefold()
    }
    assert residual == {}
