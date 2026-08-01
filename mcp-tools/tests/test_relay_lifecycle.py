"""Relay v3 worker handoff and Sol-only integration behavior."""

from __future__ import annotations

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
