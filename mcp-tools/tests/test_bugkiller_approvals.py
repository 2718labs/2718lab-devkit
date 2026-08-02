from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bugkiller.approvals import (
    ApprovalError,
    ApprovalJournal,
    ApprovalManifest,
    ApprovalState,
    EffectState,
)


def manifest(*, action: str = "commit") -> ApprovalManifest:
    common = dict(
        action=action,
        repo_realpath="D:/worktrees/task-4",
        origin_fingerprint="sha256:origin",
        base_head="abc123",
        status_hash="sha256:status",
        diff_hash="sha256:diff",
        test_hash="sha256:tests",
        risk_hash="sha256:risk",
    )
    if action == "commit":
        return ApprovalManifest(
            **common, commit_message="fix: preserve approval boundary"
        )
    if action == "push":
        return ApprovalManifest(**common, remote="origin", ref="refs/heads/task-4")
    return ApprovalManifest(
        **common,
        pr_payload={"base": "main", "head": "task-4", "title": "Fix approval boundary"},
    )


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("commit", {}),
        ("push", {}),
        ("pr", {}),
    ],
)
def test_manifest_requires_action_specific_fields(
    action: str, kwargs: dict[str, object]
) -> None:
    values = manifest(action=action).__dict__ | kwargs
    if action == "commit":
        values["commit_message"] = None
    elif action == "push":
        values["remote"] = None
    else:
        values["pr_payload"] = None

    with pytest.raises(ValueError, match="requires"):
        ApprovalManifest(**values)


def test_expired_grant_is_rejected(tmp_path) -> None:
    journal = ApprovalJournal(
        tmp_path / "approvals.sqlite3", now=lambda: datetime(2026, 7, 24, tzinfo=UTC)
    )
    prepared = journal.prepare(manifest(), expires_at=datetime(2026, 7, 23, tzinfo=UTC))
    journal.grant(prepared.id)

    with pytest.raises(ApprovalError, match="expired"):
        journal.claim(prepared.id, manifest())


def test_denied_approval_cannot_be_claimed(tmp_path) -> None:
    journal = ApprovalJournal(tmp_path / "approvals.sqlite3")
    prepared = journal.prepare(
        manifest(), expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    denied = journal.deny(prepared.id)

    assert denied.state is ApprovalState.DENIED
    with pytest.raises(ApprovalError, match="denied"):
        journal.claim(prepared.id, manifest())


def test_grant_is_single_use(tmp_path) -> None:
    journal = ApprovalJournal(tmp_path / "approvals.sqlite3")
    prepared = journal.prepare(
        manifest(), expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.grant(prepared.id)
    claimed = journal.claim(prepared.id, manifest())

    assert claimed.state is EffectState.CLAIMED
    with pytest.raises(ApprovalError, match="already claimed"):
        journal.claim(prepared.id, manifest())


@pytest.mark.parametrize(
    "changed",
    [
        "diff_hash",
        "base_head",
        "origin_fingerprint",
        "test_hash",
        "status_hash",
        "commit_message",
    ],
)
def test_changed_manifest_rejects_old_grant(tmp_path, changed: str) -> None:
    journal = ApprovalJournal(tmp_path / "approvals.sqlite3")
    original = manifest()
    prepared = journal.prepare(
        original, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.grant(prepared.id)

    with pytest.raises(ApprovalError, match="manifest changed"):
        journal.claim(prepared.id, replace(original, **{changed: "sha256:changed"}))


def test_action_grants_are_strictly_separate(tmp_path) -> None:
    journal = ApprovalJournal(tmp_path / "approvals.sqlite3")
    prepared = journal.prepare(
        manifest(action="commit"), expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.grant(prepared.id)

    with pytest.raises(ApprovalError, match="manifest changed"):
        journal.claim(prepared.id, manifest(action="push"))


@pytest.mark.parametrize(
    ("approved", "current"),
    [
        (
            manifest(action="push"),
            replace(manifest(action="push"), ref="refs/heads/another-branch"),
        ),
        (
            manifest(action="pr"),
            replace(
                manifest(action="pr"),
                pr_payload={"base": "main", "head": "task-4", "title": "Different"},
            ),
        ),
    ],
)
def test_changed_action_specific_target_revokes_grant(
    tmp_path, approved: ApprovalManifest, current: ApprovalManifest
) -> None:
    journal = ApprovalJournal(tmp_path / "approvals.sqlite3")
    prepared = journal.prepare(
        approved, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.grant(prepared.id)

    with pytest.raises(ApprovalError, match="manifest changed"):
        journal.claim(prepared.id, current)


class FakeHost:
    def __init__(self, fact: object | None = None) -> None:
        self.fact = fact
        self.executed: list[str] = []

    def query_effect(
        self, *, action: str, manifest: dict[str, object]
    ) -> object | None:
        return self.fact

    def execute_effect(self, *, action: str, manifest: dict[str, object]) -> object:
        self.executed.append(action)
        return {"ok": True, "action": action}


def test_recovery_queries_external_fact_before_retrying(tmp_path) -> None:
    journal = ApprovalJournal(tmp_path / "approvals.sqlite3")
    approval = journal.prepare(
        manifest(), expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.grant(approval.id)
    effect = journal.claim(approval.id, manifest())
    host = FakeHost(fact={"already": "committed"})

    recovered = journal.recover(effect.id, host)

    assert recovered.state is EffectState.SUCCEEDED
    assert host.executed == []
