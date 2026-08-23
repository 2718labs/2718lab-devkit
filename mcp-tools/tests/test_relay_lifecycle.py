"""Relay v3 worker handoff and Sol-only integration behavior."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_relay_runtime import (
    _BASE_COMMIT,
    ProofRegistry,
    bind_worker,
    issue_worker,
    plan,
    service,
    synthetic_integration_receipt,
    task,
    worker_request,
)

from devkit_relay.canonical import canonical_hash
from devkit_relay.service import RelayError, RelayService
from devkit_relay.store import RelaySchemaIncompatible, RelayStore

_EVIDENCE_HASH = "sha256:" + "d" * 64
_EXTRA_EVIDENCE_HASH = "sha256:" + "9" * 64


def _candidate(evidence_hashes: list[str]) -> dict[str, object]:
    return {
        "candidate_id": "candidate-a",
        "branch": "relay/candidate-a",
        "base_commit": _BASE_COMMIT,
        "head_commit": "e" * 40,
        "diff_hash": "sha256:" + "f" * 64,
        "evidence_hashes": evidence_hashes,
        "pr_reference": None,
    }


def _record_evidence(
    relay: RelayService, action: dict[str, object], task_version: int
) -> int:
    result = relay.handoff(
        worker_request(
            action,
            lifecycle_action="evidence",
            capability=issue_worker(relay, action, lifecycle_action="evidence"),
            expected_task_version=task_version,
            evidence={
                "kind": "pytest",
                "selector": f"tests/{action['task_id']}.py",
                "digest": _EVIDENCE_HASH,
            },
        )
    )
    task_data = result["task"]
    assert isinstance(task_data, dict)
    return int(task_data["task_version"])


def _recovery_request(
    relay: RelayService,
    action: dict[str, object],
    *,
    lifecycle_action: str,
    expected_task_version: int,
) -> dict[str, object]:
    lease = action["lease"]
    assert isinstance(lease, dict)
    return {
        "workflow_id": "relay-runtime-v3",
        "task_id": action["task_id"],
        "action": lifecycle_action,
        "epoch": lease["epoch"],
        "endpoint": "sol-main",
        "expected_task_version": expected_task_version,
        "capability": relay.issue_sol_capability(
            workflow_id="relay-runtime-v3",
            task_id=str(action["task_id"]),
            action=lifecycle_action,
            epoch=int(lease["epoch"]),
            endpoint="sol-main",
        ),
        "predecessor_action_id": action["action_id"],
        "predecessor_lease_id": lease["lease_id"],
    }


class _NoProofProviderStore:
    """Lifecycle double that makes proof-provider reachability observable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def approve_readonly(self, **_: object) -> dict[str, object]:
        self.calls.append("approve_readonly")
        return {"action": "approve_readonly"}

    def review_candidate(self, **_: object) -> dict[str, object]:
        self.calls.append("review")
        return {"action": "review"}

    def rebase_candidate(self, **_: object) -> dict[str, object]:
        self.calls.append("rebase")
        return {"action": "rebase"}

    def reject_candidate(self, **_: object) -> dict[str, object]:
        self.calls.append("reject")
        return {"action": "reject"}

    def integration_expectation(self, **_: object) -> object:
        self.calls.append("integration_expectation")
        raise AssertionError("missing proof provider must be rejected before store")


def _missing_proof_request(
    relay: RelayService,
    *,
    action: str,
    extra: dict[str, object],
) -> dict[str, object]:
    request = {
        "workflow_id": "relay-runtime-v3",
        "task_id": "writer-a",
        "action": action,
        "epoch": 1,
        "endpoint": "sol-main",
        "expected_task_version": 1,
        "capability": relay.issue_sol_capability(
            workflow_id="relay-runtime-v3",
            task_id="writer-a",
            action=action,
            epoch=1,
            endpoint="sol-main",
        ),
    }
    return {**request, **extra}


def test_missing_proof_provider_only_blocks_integrate_before_store_access() -> None:
    store = _NoProofProviderStore()
    relay = RelayService(
        store,  # type: ignore[arg-type]
        capability_secret=b"relay-v3-test-secret",
        integration_proof_resolver=None,
    )

    with pytest.raises(RelayError) as caught:
        relay.integrate(
            _missing_proof_request(
                relay,
                action="integrate",
                extra={
                    "candidate_id": "candidate-a",
                    "integration_proof_id": "sha256:" + "f" * 64,
                },
            )
        )

    assert caught.value.code == "RELAY_FINALIZATION_PENDING"
    assert store.calls == []


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("approve_readonly", {}),
        (
            "review",
            {"candidate_id": "candidate-a", "review_digest": "sha256:" + "a" * 64},
        ),
        (
            "rebase",
            {
                "candidate_id": "candidate-a",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "diff_hash": "sha256:" + "c" * 64,
                "evidence_hashes": ["sha256:" + "d" * 64],
            },
        ),
        ("reject", {"candidate_id": "candidate-a"}),
    ],
)
def test_missing_proof_provider_preserves_nonintegrate_sol_actions(
    action: str, extra: dict[str, object]
) -> None:
    store = _NoProofProviderStore()
    relay = RelayService(
        store,  # type: ignore[arg-type]
        capability_secret=b"relay-v3-test-secret",
        integration_proof_resolver=None,
    )

    result = relay.integrate(_missing_proof_request(relay, action=action, extra=extra))

    assert result == {"action": action}
    assert store.calls == [action]


def test_candidate_handoff_releases_worker_slot_and_sol_integration_unblocks_dependency(
    tmp_path: Path,
) -> None:
    registry = ProofRegistry()
    relay, store = service(tmp_path, registry)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                priority=100,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-b",
                priority=90,
                write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
            ),
            task(
                "writer-overlap",
                priority=95,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-c",
                priority=80,
                write_scope=[{"path": "mcp-tools/c.py", "kind": "file"}],
            ),
            task(
                "writer-child",
                priority=70,
                dependencies=["writer-a"],
                write_scope=[{"path": "mcp-tools/child.py", "kind": "file"}],
            ),
        ),
        idempotency_key="candidate-create",
    )
    actions = {item["task_id"]: item for item in created["host_actions"]}
    writer = actions["writer-a"]
    assert isinstance(writer, dict)
    task_version = bind_worker(relay, writer)
    task_version = _record_evidence(relay, writer, task_version)

    handed_off = relay.handoff(
        worker_request(
            writer,
            lifecycle_action="candidate_handoff",
            capability=issue_worker(
                relay, writer, lifecycle_action="candidate_handoff"
            ),
            expected_task_version=task_version,
            candidate=_candidate([_EVIDENCE_HASH]),
        )
    )
    candidate_task = handed_off["task"]
    assert isinstance(candidate_task, dict)
    assert candidate_task["state"] == "review_integration"
    assert candidate_task["scope_owner"] == "sol"

    status = relay.status("relay-runtime-v3")
    assert [item["task_id"] for item in status["queues"]["review_integration"]] == [
        "writer-a"
    ]
    assert [item["task_id"] for item in status["queues"]["prepared_prewarms"]] == [
        "writer-child"
    ]
    directive = status["refill_directives"][0]
    assert directive["task_id"] == "writer-c"

    relay.start_refill(
        "relay-runtime-v3",
        str(directive["directive_id"]),
        expected_schedule_version=int(directive["expected_schedule_version"]),
        idempotency_key="candidate-refill",
    )

    lease = writer["lease"]
    assert isinstance(lease, dict)
    rebased = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer-a",
            "action": "rebase",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer-a",
                action="rebase",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "base_commit": _BASE_COMMIT,
            "head_commit": "3" * 40,
            "diff_hash": "sha256:" + "4" * 64,
            "evidence_hashes": [_EVIDENCE_HASH],
        }
    )
    assert rebased["candidate"]["status"] == "rebased"
    assert rebased["candidate"]["diff_hash"] == "sha256:" + "4" * 64

    review = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer-a",
            "action": "review",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer-a",
                action="review",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "review_digest": "sha256:" + "1" * 64,
        }
    )
    assert review["candidate"]["status"] == "reviewed"

    expectation = store.integration_expectation(
        "relay-runtime-v3",
        "writer-a",
        epoch=int(lease["epoch"]),
        expected_task_version=int(candidate_task["task_version"]),
        candidate_id="candidate-a",
        proof_id="sha256:" + "0" * 64,
    )
    proof_id = registry.register(synthetic_integration_receipt(expectation))

    integrated = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer-a",
            "action": "integrate",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer-a",
                action="integrate",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "integration_proof_id": proof_id,
        }
    )
    assert integrated["task"]["state"] == "integrated"
    status = relay.status("relay-runtime-v3")
    assert [item["task_id"] for item in status["queues"]["ready"]] == [
        "writer-overlap",
        "writer-child",
    ]


def test_readonly_worker_needs_sol_approval_before_it_satisfies_dependencies(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task("verify", kind="verification", required_evidence=[]),
            task(
                "verify-child",
                kind="verification",
                dependencies=["verify"],
                required_evidence=[],
            ),
            capacity=1,
        ),
        idempotency_key="readonly-create",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    handed_off = relay.handoff(
        worker_request(
            action,
            lifecycle_action="terminal",
            capability=issue_worker(relay, action, lifecycle_action="terminal"),
            expected_task_version=task_version,
            outcome="completed",
        )
    )
    assert handed_off["task"]["state"] == "review_integration"
    assert relay.status("relay-runtime-v3")["queues"]["ready"] == []

    lease = action["lease"]
    assert isinstance(lease, dict)
    approved = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "verify",
            "action": "approve_readonly",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": handed_off["task"]["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="verify",
                action="approve_readonly",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
        }
    )
    assert approved["task"]["state"] == "completed"
    assert [
        item["task_id"] for item in relay.status("relay-runtime-v3")["queues"]["ready"]
    ] == ["verify-child"]


@pytest.mark.parametrize(
    "evidence_hashes",
    [[], [_EVIDENCE_HASH, "sha256:" + "9" * 64]],
)
def test_candidate_handoff_rejects_missing_or_unregistered_evidence_hashes(
    tmp_path: Path, evidence_hashes: list[str]
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            )
        ),
        idempotency_key="candidate-evidence-binding",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    if evidence_hashes:
        task_version = _record_evidence(relay, action, task_version)

    with pytest.raises(RelayError, match="RELAY_CANDIDATE_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="candidate_handoff",
                capability=issue_worker(
                    relay, action, lifecycle_action="candidate_handoff"
                ),
                expected_task_version=task_version,
                candidate=_candidate(evidence_hashes),
            )
        )


def test_candidate_handoff_requires_the_exact_registered_digest_set(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            )
        ),
        idempotency_key="candidate-exact-evidence",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = _record_evidence(relay, action, bind_worker(relay, action))
    relay.handoff(
        worker_request(
            action,
            lifecycle_action="evidence",
            capability=issue_worker(relay, action, lifecycle_action="evidence"),
            expected_task_version=task_version,
            evidence={
                "kind": "ruff",
                "selector": "mcp-tools/devkit_relay",
                "digest": _EXTRA_EVIDENCE_HASH,
            },
        )
    )

    with pytest.raises(RelayError, match="RELAY_CANDIDATE_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="candidate_handoff",
                capability=issue_worker(
                    relay, action, lifecycle_action="candidate_handoff"
                ),
                expected_task_version=task_version,
                candidate=_candidate([_EVIDENCE_HASH]),
            )
        )


def test_rebase_requires_the_exact_registered_digest_set(tmp_path: Path) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            )
        ),
        idempotency_key="rebase-exact-evidence",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = _record_evidence(relay, action, bind_worker(relay, action))
    relay.handoff(
        worker_request(
            action,
            lifecycle_action="evidence",
            capability=issue_worker(relay, action, lifecycle_action="evidence"),
            expected_task_version=task_version,
            evidence={
                "kind": "ruff",
                "selector": "mcp-tools/devkit_relay",
                "digest": _EXTRA_EVIDENCE_HASH,
            },
        )
    )
    handed_off = relay.handoff(
        worker_request(
            action,
            lifecycle_action="candidate_handoff",
            capability=issue_worker(
                relay, action, lifecycle_action="candidate_handoff"
            ),
            expected_task_version=task_version,
            candidate=_candidate([_EVIDENCE_HASH, _EXTRA_EVIDENCE_HASH]),
        )
    )
    candidate_task = handed_off["task"]
    assert isinstance(candidate_task, dict)
    lease = action["lease"]
    assert isinstance(lease, dict)

    with pytest.raises(RelayError, match="RELAY_CANDIDATE_INVALID"):
        relay.integrate(
            {
                "workflow_id": "relay-runtime-v3",
                "task_id": "writer-a",
                "action": "rebase",
                "epoch": lease["epoch"],
                "endpoint": "sol-main",
                "expected_task_version": candidate_task["task_version"],
                "capability": relay.issue_sol_capability(
                    workflow_id="relay-runtime-v3",
                    task_id="writer-a",
                    action="rebase",
                    epoch=int(lease["epoch"]),
                    endpoint="sol-main",
                ),
                "candidate_id": "candidate-a",
                "base_commit": _BASE_COMMIT,
                "head_commit": "3" * 40,
                "diff_hash": "sha256:" + "4" * 64,
                "evidence_hashes": [_EVIDENCE_HASH],
            }
        )


def test_stale_recovery_requires_an_exact_unbound_predecessor(tmp_path: Path) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            capacity=1,
        ),
        idempotency_key="stale-recovery",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    lease = action["lease"]
    assert isinstance(lease, dict)
    task_version = int(lease["task_version"])

    forged = _recovery_request(
        relay,
        action,
        lifecycle_action="stale_recovery",
        expected_task_version=task_version,
    )
    forged["predecessor_action_id"] = "action-forged"
    with pytest.raises(RelayError, match="RELAY_LEASE_CONFLICT"):
        relay.recover(forged)

    recovered = relay.recover(
        _recovery_request(
            relay,
            action,
            lifecycle_action="stale_recovery",
            expected_task_version=task_version,
        )
    )
    replacement = recovered["host_actions"][0]
    assert isinstance(replacement, dict)
    replacement_lease = replacement["lease"]
    assert isinstance(replacement_lease, dict)
    assert replacement_lease["epoch"] == int(lease["epoch"]) + 1
    recovery = replacement["recovery"]
    assert isinstance(recovery, dict)
    assert set(recovery) == {
        "kind",
        "predecessor_action_id",
        "predecessor_lease_id",
        "predecessor_epoch",
        "predecessor_context_hash",
    }
    assert recovery["kind"] == "stale_recovery"
    assert recovery["predecessor_action_id"] == action["action_id"]
    assert recovery["predecessor_lease_id"] == lease["lease_id"]
    assert recovery["predecessor_epoch"] == lease["epoch"]
    assert recovery["predecessor_context_hash"] == canonical_hash(
        {
            "route": action["route"],
            "task_contract": action["task_contract"],
            "worktree_bootstrap": action["worktree_bootstrap"],
        }
    )
    status = relay.status("relay-runtime-v3")
    assert status["outstanding_action_ids"] == [replacement["action_id"]]
    assert [
        item["epoch"] for item in status["leases"] if item["state"] == "released"
    ] == [lease["epoch"]]


def test_interruption_recovery_requires_a_bound_predecessor(tmp_path: Path) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            capacity=1,
        ),
        idempotency_key="interruption-recovery",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)

    with pytest.raises(RelayError, match="RELAY_STATE_STALE"):
        relay.recover(
            _recovery_request(
                relay,
                action,
                lifecycle_action="stale_recovery",
                expected_task_version=task_version,
            )
        )

    recovered = relay.recover(
        _recovery_request(
            relay,
            action,
            lifecycle_action="interruption_recovery",
            expected_task_version=task_version,
        )
    )
    replacement = recovered["host_actions"][0]
    assert isinstance(replacement, dict)
    assert replacement["recovery"]["kind"] == "interruption_recovery"
    assert replacement["recovery"]["predecessor_action_id"] == action["action_id"]


def test_cleanup_becomes_eligible_only_after_later_integration_and_never_deletes_locally(
    tmp_path: Path,
) -> None:
    """Retention uses accepted integration versions, never wall-clock or refill calls."""

    registry = ProofRegistry()
    relay, store = service(tmp_path, registry)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                priority=100,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-b",
                priority=90,
                dependencies=["writer-a"],
                write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
            ),
            capacity=1,
        ),
        idempotency_key="cleanup-lifecycle",
    )

    def integrate(
        action: dict[str, object],
        *,
        candidate_id: str,
        head_commit: str,
        final_commit: str,
        cleanup_policy: dict[str, object] | None = None,
    ) -> None:
        task_version = bind_worker(relay, action)
        task_version = _record_evidence(relay, action, task_version)
        candidate = {
            "candidate_id": candidate_id,
            "branch": f"relay/{candidate_id}",
            "base_commit": _BASE_COMMIT if candidate_id == "candidate-a" else "1" * 40,
            "head_commit": head_commit,
            "diff_hash": "sha256:" + "f" * 64,
            "evidence_hashes": [_EVIDENCE_HASH],
            "pr_reference": None,
        }
        if cleanup_policy is not None:
            candidate["cleanup_policy"] = cleanup_policy
        handed_off = relay.handoff(
            worker_request(
                action,
                lifecycle_action="candidate_handoff",
                capability=issue_worker(relay, action, lifecycle_action="candidate_handoff"),
                expected_task_version=task_version,
                candidate=candidate,
            )
        )
        handed_task = handed_off["task"]
        assert isinstance(handed_task, dict)
        lease = action["lease"]
        assert isinstance(lease, dict)
        relay.integrate(
            {
                "workflow_id": "relay-runtime-v3",
                "task_id": action["task_id"],
                "action": "review",
                "epoch": lease["epoch"],
                "endpoint": "sol-main",
                "expected_task_version": handed_task["task_version"],
                "capability": relay.issue_sol_capability(
                    workflow_id="relay-runtime-v3",
                    task_id=str(action["task_id"]),
                    action="review",
                    epoch=int(lease["epoch"]),
                    endpoint="sol-main",
                ),
                "candidate_id": candidate_id,
                "review_digest": "sha256:" + "1" * 64,
            }
        )
        expectation = store.integration_expectation(
            "relay-runtime-v3",
            str(action["task_id"]),
            epoch=int(lease["epoch"]),
            expected_task_version=int(handed_task["task_version"]),
            candidate_id=candidate_id,
            proof_id="sha256:" + "0" * 64,
        )
        proof_id = registry.register(
            synthetic_integration_receipt(expectation, final_commit=final_commit)
        )
        relay.integrate(
            {
                "workflow_id": "relay-runtime-v3",
                "task_id": action["task_id"],
                "action": "integrate",
                "epoch": lease["epoch"],
                "endpoint": "sol-main",
                "expected_task_version": handed_task["task_version"],
                "capability": relay.issue_sol_capability(
                    workflow_id="relay-runtime-v3",
                    task_id=str(action["task_id"]),
                    action="integrate",
                    epoch=int(lease["epoch"]),
                    endpoint="sol-main",
                ),
                "candidate_id": candidate_id,
                "integration_proof_id": proof_id,
            }
        )

    branch = "relay/candidate-a"
    head = "e" * 40
    cleanup_policy = {
        "retention_rounds": 1,
        "delete_merged_branch": True,
        "remove_disposable_worktree": True,
        "branch_identity": canonical_hash({"branch": branch, "head_commit": head}),
        "worktree_identity": "sha256:" + "2" * 64,
    }
    actions = created["host_actions"]
    assert isinstance(actions, list)
    integrate(
        actions[0],
        candidate_id="candidate-a",
        head_commit=head,
        final_commit="1" * 40,
        cleanup_policy=cleanup_policy,
    )

    with pytest.raises(Exception, match="CLEANUP_NOT_ELIGIBLE"):
        store.prepare_cleanup_operation(
            "relay-runtime-v3",
            "candidate-a",
            host_recheck={},
        )

    directive = relay.status("relay-runtime-v3")["refill_directives"][0]
    assert isinstance(directive, dict)
    refill = relay.start_refill(
        "relay-runtime-v3",
        str(directive["directive_id"]),
        expected_schedule_version=int(directive["expected_schedule_version"]),
        idempotency_key="cleanup-lifecycle-refill",
    )
    second_action = refill["host_actions"][0]
    assert isinstance(second_action, dict)
    integrate(
        second_action,
        candidate_id="candidate-b",
        head_commit="f" * 40,
        final_commit="2" * 40,
    )

    ledger = store.cleanup_ledger("relay-runtime-v3", "candidate-a")
    recheck = {
        "schema": "2718lab-devkit/host-cleanup-recheck-v1",
        "candidate_id": "candidate-a",
        "integration_proof_id": ledger["integration_proof_id"],
        "integration_version": 2,
        "integration_head": "2" * 40,
        "contains_candidate_integration_commit": True,
        "branch_identity": cleanup_policy["branch_identity"],
        "worktree_identity": cleanup_policy["worktree_identity"],
        "branch_is_protected": False,
        "branch_is_current": False,
        "active_lease": False,
        "pending_review": False,
        "approved_g_task_root": True,
        "worktree_disposable": True,
        "rollback_receipt_hash": None,
        "attestation_hash": "sha256:" + "3" * 64,
    }
    protected = dict(recheck)
    protected["branch_is_protected"] = True
    with pytest.raises(Exception, match="CLEANUP_HOST_RECHECK_FAILED"):
        store.prepare_cleanup_operation(
            "relay-runtime-v3", "candidate-a", host_recheck=protected
        )
    operation = store.prepare_cleanup_operation(
        "relay-runtime-v3", "candidate-a", host_recheck=recheck
    )
    assert operation["schema"] == "2718lab-devkit/cleanup-operation-v1"
    assert operation["delete_merged_branch"] is True
    assert operation["remove_disposable_worktree"] is True
    assert not hasattr(store, "delete_branch")
    rollback = store.record_rollback_receipt(
        "relay-runtime-v3",
        "candidate-a",
        receipt={
            "schema": "2718lab-devkit/rollback-receipt-v1",
            "candidate_id": "candidate-a",
            "integration_proof_id": ledger["integration_proof_id"],
            "pre_rollback_integration_version": 2,
            "receipt_hash": "sha256:" + "6" * 64,
        },
    )
    assert rollback["state"] == "CLEANUP_ROLLBACK_OBSERVED"
    with pytest.raises(Exception, match="CLEANUP_ROLLBACK_OBSERVED"):
        store.prepare_cleanup_operation(
            "relay-runtime-v3", "candidate-a", host_recheck=recheck
        )
    with pytest.raises(Exception, match="CLEANUP_NOT_ELIGIBLE"):
        store.cleanup_ledger("relay-runtime-v3", "candidate-b")


def test_service_starts_disjoint_v2_a1_a2_a3_stages_without_granting_design_a_write_scope(
    tmp_path: Path,
) -> None:
    compiled = plan(
        task(
            "writer",
            priority=90,
            write_scope=[{"path": "mcp-tools/writer.py", "kind": "file"}],
        ),
        task("design", kind="design", priority=80, required_evidence=[]),
        task("prewarm", kind="prewarm", priority=70, required_evidence=[]),
        capacity=3,
    )
    tasks = compiled["tasks"]
    assert isinstance(tasks, list)
    for item in tasks:
        if item["task_id"] == "writer":
            item.update(
                {
                    "stage": "a1_writer",
                    "design_for_task_id": None,
                    "split_policy": None,
                    "split_parent_task_id": None,
                    "split_depth": 0,
                    "split_verdict": None,
                }
            )
        elif item["task_id"] == "design":
            item.update(
                {
                    "stage": "a2_design",
                    "design_for_task_id": "writer",
                    "split_policy": None,
                    "split_parent_task_id": None,
                    "split_depth": 0,
                    "split_verdict": None,
                }
            )
        else:
            item["route"] = {
                "route_class": "luna_medium",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
            }
            item.update(
                {
                    "stage": "a3_prewarm",
                    "design_for_task_id": None,
                    "split_policy": None,
                    "split_parent_task_id": None,
                    "split_depth": 0,
                    "split_verdict": None,
                    "prewarm_for_task_id": "writer",
                }
            )
    binding = compiled["workspace_binding"]
    assert isinstance(binding, dict)
    workspace_id = binding["workspace_id"]
    input_snapshot_id = binding["input_snapshot_id"]
    attestation: dict[str, object] = {
        "schema": "2718lab-devkit/new-project-bootstrap-attestation-v1",
        "workflow_id": compiled["workflow_id"],
        "workspace_id": workspace_id,
        "repository_id": "sha256:" + "1" * 64,
        "project_id": "sha256:" + "2" * 64,
        "bootstrap_root_identity": "sha256:" + "3" * 64,
        "initial_manifest_hash": "sha256:" + "4" * 64,
        "initial_entry_count": 0,
        "state": "new_empty",
        "capability_epoch": 1,
        "capability_hash": "sha256:" + "5" * 64,
        "attested_input_snapshot_id": "sha256:" + "6" * 64,
        "issued_at": 1_700_000_000,
        "expires_at": 1_700_000_060,
    }
    attestation["attestation_hash"] = canonical_hash(attestation)
    bootstrap_binding: dict[str, object] = {
        "schema": "2718lab-devkit/project-binding-v1",
        "mode": "new_empty_bootstrap",
        "workflow_id": compiled["workflow_id"],
        "workspace_id": workspace_id,
        "repository_id": attestation["repository_id"],
        "project_id": attestation["project_id"],
        "bootstrap_root_identity": attestation["bootstrap_root_identity"],
        "attestation": attestation,
    }
    bootstrap_binding["binding_hash"] = canonical_hash(bootstrap_binding)
    receipt: dict[str, object] = {
        "schema": "2718lab-devkit/project-index-bootstrap-receipt-v1",
        "attestation_hash": attestation["attestation_hash"],
        "workspace_id": workspace_id,
        "attested_input_snapshot_id": attestation["attested_input_snapshot_id"],
        "initial_manifest_hash": attestation["initial_manifest_hash"],
        "index_snapshot_id": input_snapshot_id,
        "index_identity": canonical_hash(
            {
                "workspace_id": workspace_id,
                "attested_input_snapshot_id": attestation[
                    "attested_input_snapshot_id"
                ],
                "initial_manifest_hash": attestation["initial_manifest_hash"],
                "index_snapshot_id": input_snapshot_id,
            }
        ),
        "issued_at": 1_700_000_001,
        "expires_at": 1_700_000_060,
    }
    receipt["receipt_hash"] = canonical_hash(receipt)
    compiled["schema"] = "2718lab-devkit/relay-plan-v2"
    compiled["project_binding"] = {
        "schema": "2718lab-devkit/project-binding-v1",
        "mode": "indexed",
        "bootstrap_binding": bootstrap_binding,
        "bootstrap_receipt": receipt,
    }
    compiled["queues"] = {
        "writer_ready": ["writer"],
        "design_ready": ["design"],
        "prewarm_ready": ["prewarm"],
        "bootstrap_index": [],
        "review_integration": [],
        "terminal": [],
        "unsplittable": [],
    }
    compiled["plan_hash"] = canonical_hash(
        {key: value for key, value in compiled.items() if key != "plan_hash"}
    )
    relay, _store = service(tmp_path)

    started = relay.start_create(compiled, idempotency_key="v2-stages")

    assert [item["task_id"] for item in started["host_actions"]] == [
        "writer",
        "design",
        "prewarm",
    ]


def test_cleanup_ledger_migrates_only_known_v5_metadata_and_rejects_unknown_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "relay-migration.sqlite3"
    original = RelayStore(database)
    original.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE relay_v3_cleanup_ledger")
        connection.execute(
            "UPDATE relay_v3_schema_metadata SET value = '5' WHERE key = 'schema_version'"
        )

    migrated = RelayStore(database)
    connection = migrated._require_connection()
    assert connection.execute(
        "SELECT value FROM relay_v3_schema_metadata WHERE key = 'schema_version'"
    ).fetchone()[0] == "6"
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'relay_v3_cleanup_ledger'"
    ).fetchone() is not None
    migrated.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE relay_v3_schema_metadata SET value = '4' WHERE key = 'schema_version'"
        )
    with pytest.raises(RelaySchemaIncompatible):
        RelayStore(database)
