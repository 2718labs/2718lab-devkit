"""Relay integration proof trust-boundary and atomicity coverage."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_relay_lifecycle import _EVIDENCE_HASH, _candidate, _record_evidence
from test_relay_runtime import (
    _BASE_COMMIT,
    ProofRegistry,
    _rehash,
    bind_worker,
    issue_worker,
    plan,
    service,
    synthetic_integration_receipt,
    task,
    worker_request,
)

from devkit_relay.canonical import canonical_hash
from devkit_relay.proofs import (
    IntegrationDeltaEntry,
    IntegrationExpectation,
    IntegrationProofError,
    IntegrationProofReceipt,
    IntegrationScopeEntry,
    RelayProofReservation,
    validate_integration_proof,
)
from devkit_relay.service import RelayError, RelayService
from devkit_relay.store import RelaySchemaIncompatible, RelayStorageFailure, RelayStore

try:
    from devkit_relay.proofs import (
        ProofFinalizationEvidence,
        ProofFinalizationFence,
    )
except ImportError:

    @dataclass(frozen=True, slots=True)
    class ProofFinalizationFence:
        finalization_id: str
        reservation_epoch: int
        integration_proof_id: str
        workspace_id: str
        expectation_key: str
        expectation_version: int
        expectation_hash: str
        target_ref: str
        base_oid: str
        final_oid: str

        @property
        def fence_hash(self) -> str:
            return canonical_hash(
                {
                    "finalization_id": self.finalization_id,
                    "reservation_epoch": self.reservation_epoch,
                    "integration_proof_id": self.integration_proof_id,
                    "workspace_id": self.workspace_id,
                    "expectation_key": self.expectation_key,
                    "expectation_version": self.expectation_version,
                    "expectation_hash": self.expectation_hash,
                    "target_ref": self.target_ref,
                    "base_oid": self.base_oid,
                    "final_oid": self.final_oid,
                }
            )

    @dataclass(frozen=True, slots=True)
    class ProofFinalizationEvidence:
        finalization_id: str
        state: str
        fence_hash: str
        result_hash: str | None
        journal_version: int


def _git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Relay Test",
            "GIT_AUTHOR_EMAIL": "relay@example.invalid",
            "GIT_COMMITTER_NAME": "Relay Test",
            "GIT_COMMITTER_EMAIL": "relay@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=environment,
        input=input_bytes,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _git_oid(repository: Path, revision: str) -> str:
    return _git(repository, "rev-parse", "--verify", revision).decode("ascii")


def _git_delta(
    repository: Path, predecessor: str, successor: str
) -> tuple[IntegrationDeltaEntry, ...]:
    raw = _git(
        repository,
        "diff-tree",
        "-r",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        "--no-commit-id",
        "-z",
        predecessor,
        successor,
    )
    if not raw:
        return ()
    fields = raw.split(b"\x00")
    if fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise AssertionError("unexpected git raw delta")
    entries: list[IntegrationDeltaEntry] = []
    for offset in range(0, len(fields), 2):
        header = fields[offset].decode("ascii").split()
        if len(header) != 5 or not header[0].startswith(":"):
            raise AssertionError("unexpected git raw header")
        old_mode = header[0][1:]
        new_mode, old_oid, new_oid = header[1:4]
        path = fields[offset + 1].decode("utf-8", errors="strict")

        def side(oid: str, mode: str) -> tuple[str | None, str | None, str | None]:
            if mode == "000000":
                return None, None, None
            object_type = _git(repository, "cat-file", "-t", oid).decode("ascii")
            return oid, mode, object_type

        old_values = side(old_oid, old_mode)
        new_values = side(new_oid, new_mode)
        entries.append(
            IntegrationDeltaEntry(
                path=path,
                old_oid=old_values[0],
                new_oid=new_values[0],
                old_mode=old_values[1],
                new_mode=new_values[1],
                old_type=old_values[2],
                new_type=new_values[2],
            )
        )
    return tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))


class _GitProofRegistry(ProofRegistry):
    def __init__(self, repository: Path) -> None:
        super().__init__()
        self._repository = repository

    def attest_and_register(
        self,
        expectation: IntegrationExpectation,
        *,
        integration_ref: str = "refs/heads/integration",
    ) -> str:
        predecessor = expectation.predecessor_integration_head
        candidate = expectation.candidate_head_commit
        for object_id in (predecessor, candidate):
            if _git(self._repository, "cat-file", "-t", object_id) != b"commit":
                raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        if _git_oid(self._repository, integration_ref) != predecessor:
            raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
        commits = tuple(
            item
            for item in _git(
                self._repository,
                "rev-list",
                "--reverse",
                candidate,
                f"^{predecessor}",
            )
            .decode("ascii")
            .splitlines()
            if item
        )
        if not commits or commits[-1] != candidate:
            raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
        parent = predecessor
        for commit in commits:
            ancestry = (
                _git(self._repository, "rev-list", "--parents", "-n", "1", commit)
                .decode("ascii")
                .split()
            )
            if ancestry != [commit, parent]:
                raise IntegrationProofError("RELAY_INTEGRATION_ANCESTRY_INVALID")
            parent = commit

        predecessor_tree = _git_oid(self._repository, f"{predecessor}^{{tree}}")
        candidate_tree = _git_oid(self._repository, f"{candidate}^{{tree}}")
        candidate_delta = _git_delta(self._repository, predecessor, candidate)
        if not candidate_delta:
            raise IntegrationProofError("RELAY_INTEGRATION_TREE_MISMATCH")
        final_commit = _git(
            self._repository,
            "commit-tree",
            candidate_tree,
            "-p",
            predecessor,
            "-m",
            "Relay proof integration",
        ).decode("ascii")
        try:
            _git(
                self._repository,
                "update-ref",
                integration_ref,
                final_commit,
                predecessor,
            )
        except subprocess.CalledProcessError:
            raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE") from None
        final_tree = _git_oid(self._repository, f"{final_commit}^{{tree}}")
        final_delta = _git_delta(self._repository, predecessor, final_commit)
        object_format = _git(
            self._repository, "rev-parse", "--show-object-format"
        ).decode("ascii")
        receipt = IntegrationProofReceipt.create(
            expectation=expectation,
            object_format=object_format,
            repository_id=canonical_hash(
                {
                    "schema": "test/opaque-git-repository-v1",
                    "base": predecessor,
                    "object_format": object_format,
                }
            ),
            integration_ref=integration_ref,
            predecessor_commit=predecessor,
            candidate_head_commit=candidate,
            candidate_commits=commits,
            final_commit=final_commit,
            predecessor_tree=predecessor_tree,
            candidate_tree=candidate_tree,
            final_tree=final_tree,
            final_parent_commit=predecessor,
            ref_before_commit=predecessor,
            ref_after_commit=final_commit,
            candidate_delta=candidate_delta,
            final_delta=final_delta,
            merge_free=True,
            linear_ancestry=True,
            attestor_id="test-host-git",
            attestor_version="1.0",
        )
        return self.register(receipt)

    def reserve(
        self, proof_id: str, expectation: IntegrationExpectation
    ) -> RelayProofReservation:
        receipt = self.receipts.get(proof_id)
        if receipt is not None:
            if (
                _git_oid(self._repository, receipt.integration_ref)
                != receipt.ref_after_commit
            ):
                raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
            for object_id, expected_type in (
                (receipt.predecessor_commit, "commit"),
                (receipt.candidate_head_commit, "commit"),
                (receipt.final_commit, "commit"),
                (receipt.predecessor_tree, "tree"),
                (receipt.candidate_tree, "tree"),
                (receipt.final_tree, "tree"),
            ):
                if (
                    _git(self._repository, "cat-file", "-t", object_id).decode("ascii")
                    != expected_type
                ):
                    raise IntegrationProofError("RELAY_INTEGRATION_OBJECT_INVALID")
        return super().reserve(proof_id, expectation)


def _expectation() -> IntegrationExpectation:
    return IntegrationExpectation(
        workflow_id="relay-runtime-v3",
        run_id="run-proof",
        plan_hash="sha256:" + "0" * 64,
        workspace_id="sha256:" + "d" * 64,
        task_id="writer",
        task_version=3,
        originating_epoch=1,
        sol_scope="sol:integrate",
        candidate_id="candidate-a",
        candidate_base_commit="a" * 40,
        candidate_head_commit="e" * 40,
        candidate_diff_hash="sha256:" + "f" * 64,
        candidate_evidence_hashes=("sha256:" + "d" * 64,),
        review_digest="sha256:" + "1" * 64,
        predecessor_integration_head="a" * 40,
        predecessor_integration_version=0,
        write_scope=(IntegrationScopeEntry("mcp-tools/writer.py", "file"),),
    )


def _receipt() -> IntegrationProofReceipt:
    delta = (
        IntegrationDeltaEntry(
            path="mcp-tools/writer.py",
            old_oid="1" * 40,
            new_oid="2" * 40,
            old_mode="100644",
            new_mode="100644",
            old_type="blob",
            new_type="blob",
        ),
    )
    return IntegrationProofReceipt.create(
        expectation=_expectation(),
        object_format="sha1",
        repository_id="sha256:" + "9" * 64,
        integration_ref="refs/heads/main",
        predecessor_commit="a" * 40,
        candidate_head_commit="e" * 40,
        candidate_commits=("e" * 40,),
        final_commit="2" * 40,
        predecessor_tree="3" * 40,
        candidate_tree="4" * 40,
        final_tree="4" * 40,
        final_parent_commit="a" * 40,
        ref_before_commit="a" * 40,
        ref_after_commit="2" * 40,
        candidate_delta=delta,
        final_delta=delta,
        merge_free=True,
        linear_ancestry=True,
        attestor_id="host-git",
        attestor_version="1.0",
    )


def test_typed_expectation_and_receipt_have_frozen_canonical_vectors() -> None:
    expectation = _expectation()
    receipt = _receipt()

    assert expectation.to_dict()["schema"] == (
        "2718lab-devkit/relay-integration-expectation-v1"
    )
    assert expectation.expectation_hash == (
        "sha256:fd232a8ab0c93975b5da2fbecf7d48a4bb3e575af35f361a4331fb97607d9f2a"
    )
    assert "proof_id" not in receipt.to_dict()
    assert receipt.to_dict()["expectation"] == expectation.to_dict()
    assert receipt.candidate_delta_hash == receipt.final_delta_hash
    assert receipt.proof_id == (
        "sha256:550a8f1b4893b6c35481e455e4489e30bcb81a4383057e90a5c5a769de648ea3"
    )


def _reviewed_candidate(
    tmp_path: Path,
    registry: ProofRegistry | None = None,
) -> tuple[RelayService, RelayStore, dict[str, object], dict[str, object]]:
    relay, store = service(tmp_path, registry)
    created = relay.start_create(
        plan(
            task(
                "writer",
                priority=100,
                write_scope=[{"path": "mcp-tools/writer.py", "kind": "file"}],
            ),
            task(
                "child",
                dependencies=["writer"],
                write_scope=[{"path": "mcp-tools/child.py", "kind": "file"}],
            ),
            capacity=1,
        ),
        idempotency_key="proof-create",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    task_version = _record_evidence(relay, action, task_version)
    handed_off = relay.handoff(
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
    candidate_task = handed_off["task"]
    assert isinstance(candidate_task, dict)
    lease = action["lease"]
    assert isinstance(lease, dict)
    relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer",
            "action": "review",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer",
                action="review",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "review_digest": "sha256:" + "1" * 64,
        }
    )
    return relay, store, action, candidate_task


def _integration_request(
    relay: RelayService,
    action: dict[str, object],
    candidate_task: dict[str, object],
    proof_id: str,
    *,
    task_id: str = "writer",
    candidate_id: str = "candidate-a",
) -> dict[str, object]:
    lease = action["lease"]
    assert isinstance(lease, dict)
    return {
        "workflow_id": "relay-runtime-v3",
        "task_id": task_id,
        "action": "integrate",
        "epoch": lease["epoch"],
        "endpoint": "sol-main",
        "expected_task_version": candidate_task["task_version"],
        "capability": relay.issue_sol_capability(
            workflow_id="relay-runtime-v3",
            task_id=task_id,
            action="integrate",
            epoch=int(lease["epoch"]),
            endpoint="sol-main",
        ),
        "candidate_id": candidate_id,
        "integration_proof_id": proof_id,
    }


def _registered_candidate(
    tmp_path: Path,
) -> tuple[
    RelayService,
    RelayStore,
    ProofRegistry,
    dict[str, object],
    dict[str, object],
    str,
]:
    registry = ProofRegistry()
    relay, store, action, candidate_task = _reviewed_candidate(tmp_path, registry)
    lease = action["lease"]
    assert isinstance(lease, dict)
    expectation = store.integration_expectation(
        "relay-runtime-v3",
        "writer",
        epoch=int(lease["epoch"]),
        expected_task_version=int(candidate_task["task_version"]),
        candidate_id="candidate-a",
        proof_id="sha256:" + "0" * 64,
    )
    proof_id = registry.register(synthetic_integration_receipt(expectation))
    return relay, store, registry, action, candidate_task, proof_id


def _review(
    relay: RelayService,
    action: dict[str, object],
    candidate_task: dict[str, object],
    *,
    task_id: str,
    candidate_id: str,
) -> None:
    lease = action["lease"]
    assert isinstance(lease, dict)
    relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": task_id,
            "action": "review",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id=task_id,
                action="review",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": candidate_id,
            "review_digest": "sha256:" + "1" * 64,
        }
    )


def _assert_no_bearer(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert "capability" not in str(key).casefold()
            assert "bearer" not in str(key).casefold()
            _assert_no_bearer(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_bearer(item)
    elif isinstance(value, str):
        assert "bearer " not in value.casefold()


def test_legacy_commit_only_integration_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    relay, store, action, candidate_task = _reviewed_candidate(tmp_path)
    lease = action["lease"]
    assert isinstance(lease, dict)
    before_fingerprint = store.database_fingerprint()
    before_status = relay.status("relay-runtime-v3")

    with pytest.raises(RelayError) as raised:
        relay.integrate(
            {
                "workflow_id": "relay-runtime-v3",
                "task_id": "writer",
                "action": "integrate",
                "epoch": lease["epoch"],
                "endpoint": "sol-main",
                "expected_task_version": candidate_task["task_version"],
                "capability": relay.issue_sol_capability(
                    workflow_id="relay-runtime-v3",
                    task_id="writer",
                    action="integrate",
                    epoch=int(lease["epoch"]),
                    endpoint="sol-main",
                ),
                "candidate_id": "candidate-a",
                "integration_head": _BASE_COMMIT,
                "integration_commit": "2" * 40,
            }
        )

    assert raised.value.code == "RELAY_CANDIDATE_INVALID"
    assert store.database_fingerprint() == before_fingerprint
    assert relay.status("relay-runtime-v3") == before_status

    with pytest.raises(RelayError) as capability_error:
        relay.integrate(
            {
                "workflow_id": "relay-runtime-v3",
                "task_id": "writer",
                "action": "integrate",
                "epoch": lease["epoch"],
                "endpoint": "sol-main",
                "expected_task_version": candidate_task["task_version"],
                "capability": "not-a-capability",
                "candidate_id": "candidate-a",
                "integration_head": _BASE_COMMIT,
                "integration_commit": "2" * 40,
            }
        )
    assert capability_error.value.code == "RELAY_CAPABILITY_INVALID"
    assert store.database_fingerprint() == before_fingerprint


def test_registered_proof_integrates_atomically_and_status_is_bounded(
    tmp_path: Path,
) -> None:
    relay, _store, registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )

    integrated = relay.integrate(
        _integration_request(relay, action, candidate_task, proof_id)
    )

    assert integrated["task"]["state"] == "integrated"
    assert integrated["candidate"]["integration_proof_id"] == proof_id
    assert integrated["candidate"]["integration_tree"] == "4" * 40
    assert registry.states[proof_id] == "consumed"
    status = relay.status("relay-runtime-v3")
    assert status["schema"] == "2718lab-devkit/relay-status-v2"
    assert status["run"]["integration_head"] == "2" * 40
    assert status["run"]["integration_version"] == 1
    assert status["integration_proofs"] == [
        {
            "proof_id": proof_id,
            "expectation_hash": registry.receipts[
                proof_id
            ].expectation.expectation_hash,
            "task_id": "writer",
            "candidate_id": "candidate-a",
            "integration_version": 1,
            "predecessor_commit": _BASE_COMMIT,
            "final_commit": "2" * 40,
            "final_tree": "4" * 40,
            "attestor_id": "host-git",
            "attestor_version": "1.0",
        }
    ]
    assert "repository_id" not in repr(status["integration_proofs"])
    assert "receipt" not in repr(status["integration_proofs"]).lower()
    assert [item["task_id"] for item in status["queues"]["ready"]] == ["child"]
    _assert_no_bearer(integrated)
    _assert_no_bearer(status)


def test_unregistered_and_busy_proofs_fail_without_relay_writes(tmp_path: Path) -> None:
    relay, store, action, candidate_task = _reviewed_candidate(tmp_path)
    unknown = "sha256:" + "7" * 64
    before = store.database_fingerprint()
    with pytest.raises(RelayError) as invalid:
        relay.integrate(
            _integration_request(relay, action, candidate_task, "not-a-proof-id")
        )
    assert invalid.value.code == "RELAY_CANDIDATE_INVALID"
    assert store.database_fingerprint() == before

    with pytest.raises(RelayError) as unregistered:
        relay.integrate(_integration_request(relay, action, candidate_task, unknown))
    assert unregistered.value.code == "RELAY_CANDIDATE_INVALID"
    assert store.database_fingerprint() == before

    stale_request = _integration_request(relay, action, candidate_task, unknown)
    stale_request["expected_task_version"] = int(candidate_task["task_version"]) + 1
    with pytest.raises(RelayError) as stale:
        relay.integrate(stale_request)
    assert stale.value.code == "RELAY_EXPECTATION_STALE"
    assert store.database_fingerprint() == before

    store.close()
    second = tmp_path / "busy"
    second.mkdir()
    relay, store, registry, action, candidate_task, proof_id = _registered_candidate(
        second
    )
    registry.reserve(proof_id, registry.receipts[proof_id].expectation)
    before = store.database_fingerprint()
    integrated = relay.integrate(
        _integration_request(relay, action, candidate_task, proof_id)
    )
    assert integrated["task"]["state"] == "integrated"
    assert registry.states[proof_id] == "consumed"
    assert store.database_fingerprint() != before


def test_attestor_exception_is_bounded_and_does_not_disclose_host_state(
    tmp_path: Path,
) -> None:
    relay, store, registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )

    def unavailable() -> None:
        raise OSError(r"G:\secret-repository\receipt.json")

    registry.on_reserve = unavailable
    before = store.database_fingerprint()
    with pytest.raises(RelayError) as raised:
        relay.integrate(_integration_request(relay, action, candidate_task, proof_id))

    assert raised.value.code == "RELAY_FINALIZATION_PENDING"
    assert str(raised.value) == "RELAY_FINALIZATION_PENDING"
    assert "secret" not in str(raised.value)
    assert store.database_fingerprint() == before


def test_sqlite_commit_then_registry_consume_failure_repairs_without_repromotion(
    tmp_path: Path,
) -> None:
    relay, _store, registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )
    request = _integration_request(relay, action, candidate_task, proof_id)
    registry.fail_settle_once = True

    integrated = relay.integrate(request)

    assert integrated["task"]["state"] == "integrated"
    committed = relay.status("relay-runtime-v3")
    committed_schedule = committed["schedule_version"]
    assert committed["run"]["integration_version"] == 1
    assert registry.states[proof_id] == "consumed"

    replay = relay.integrate(request)

    assert replay["task"]["state"] == "integrated"
    assert relay.status("relay-runtime-v3")["schedule_version"] == committed_schedule
    assert registry.states[proof_id] == "consumed"


def test_consumed_registry_with_missing_sqlite_row_recovers_same_proof(
    tmp_path: Path,
) -> None:
    relay, _store, registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )
    registry.states[proof_id] = "consumed"
    before = _store.database_fingerprint()

    with pytest.raises(RelayError) as raised:
        relay.integrate(_integration_request(relay, action, candidate_task, proof_id))

    assert raised.value.code == "RELAY_FINALIZATION_PENDING"
    assert _store.database_fingerprint() == before


def test_store_cas_failure_releases_registry_reservation_without_proof_write(
    tmp_path: Path,
) -> None:
    relay, store, registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )
    connection = store._require_connection()

    def mutate_binding() -> None:
        connection.execute(
            """
            UPDATE relay_v3_candidates SET review_digest = ?
            WHERE candidate_id = 'candidate-a'
            """,
            ("sha256:" + "8" * 64,),
        )

    registry.on_reserve = mutate_binding
    with pytest.raises(RelayError) as raised:
        relay.integrate(_integration_request(relay, action, candidate_task, proof_id))

    assert raised.value.code == "RELAY_EXPECTATION_STALE"
    assert registry.states[proof_id] == "registered"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM relay_v3_integration_proofs"
        ).fetchone()[0]
        == 0
    )
    candidate = relay.status("relay-runtime-v3")["candidates"][0]
    assert candidate["status"] == "reviewed"
    assert candidate["integration_proof_id"] is None


def test_integration_version_race_fails_head_stale_and_rolls_back_proof(
    tmp_path: Path,
) -> None:
    relay, store, registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )
    connection = store._require_connection()

    def advance_version() -> None:
        connection.execute(
            """
            UPDATE relay_v3_runs SET integration_version = integration_version + 1
            WHERE workflow_id = 'relay-runtime-v3'
            """
        )

    registry.on_reserve = advance_version
    with pytest.raises(RelayError) as raised:
        relay.integrate(_integration_request(relay, action, candidate_task, proof_id))

    assert raised.value.code == "RELAY_EXPECTATION_STALE"
    assert registry.states[proof_id] == "registered"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM relay_v3_integration_proofs"
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT status FROM relay_v3_candidates WHERE candidate_id = 'candidate-a'"
        ).fetchone()[0]
        == "reviewed"
    )


def test_persisted_proof_corruption_fails_closed_in_status(tmp_path: Path) -> None:
    relay, store, _registry, action, candidate_task, proof_id = _registered_candidate(
        tmp_path
    )
    relay.integrate(_integration_request(relay, action, candidate_task, proof_id))
    store._require_connection().execute(
        "UPDATE relay_v3_integration_proofs SET receipt_json = '{}' WHERE proof_id = ?",
        (proof_id,),
    )

    with pytest.raises(RelayError) as raised:
        relay.status("relay-runtime-v3")

    assert raised.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"


def test_existing_v3_schema_is_rejected_before_any_mutation(tmp_path: Path) -> None:
    database = tmp_path / "relay-v3.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE relay_v3_schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO relay_v3_schema_metadata (key, value)
        VALUES ('schema_version', '3');
        CREATE TABLE relay_v3_runs (run_id TEXT PRIMARY KEY);
        """
    )
    connection.close()
    before = database.read_bytes()

    with pytest.raises(RelaySchemaIncompatible) as raised:
        RelayStore(database)

    assert raised.value.code == "RELAY_SCHEMA_INCOMPATIBLE"
    assert database.read_bytes() == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def _receipt_with_delta(
    receipt: IntegrationProofReceipt,
    delta: tuple[IntegrationDeltaEntry, ...],
) -> IntegrationProofReceipt:
    return IntegrationProofReceipt.create(
        expectation=receipt.expectation,
        object_format=receipt.object_format,
        repository_id=receipt.repository_id,
        integration_ref=receipt.integration_ref,
        predecessor_commit=receipt.predecessor_commit,
        candidate_head_commit=receipt.candidate_head_commit,
        candidate_commits=receipt.candidate_commits,
        final_commit=receipt.final_commit,
        predecessor_tree=receipt.predecessor_tree,
        candidate_tree=receipt.candidate_tree,
        final_tree=receipt.final_tree,
        final_parent_commit=receipt.final_parent_commit,
        ref_before_commit=receipt.ref_before_commit,
        ref_after_commit=receipt.ref_after_commit,
        candidate_delta=delta,
        final_delta=delta,
        merge_free=receipt.merge_free,
        linear_ancestry=receipt.linear_ancestry,
        attestor_id=receipt.attestor_id,
        attestor_version=receipt.attestor_version,
    )


@pytest.mark.parametrize(
    "path",
    [
        "../mcp-tools/writer.py",
        "mcp-tools\\writer.py",
        "mcp-tools/writer.py\x00suffix",
        "mcp-tools/cafe\u0301.py",
        "mcp-tools/writer.py.evil",
    ],
)
def test_proof_rejects_path_alias_and_segment_scope_bypass(path: str) -> None:
    receipt = _receipt()
    changed = replace(receipt.candidate_delta[0], path=path)
    invalid = _receipt_with_delta(receipt, (changed,))

    with pytest.raises(IntegrationProofError) as raised:
        validate_integration_proof(invalid.proof_id, invalid.expectation, invalid)

    assert raised.value.code == "RELAY_INTEGRATION_SCOPE_MISMATCH"


def test_proof_rejects_casefold_aliases_and_invalid_object_modes() -> None:
    expectation = replace(
        _expectation(), write_scope=(IntegrationScopeEntry("mcp-tools", "tree"),)
    )
    receipt = synthetic_integration_receipt(expectation)
    first = replace(receipt.candidate_delta[0], path="mcp-tools/A.py")
    second = replace(
        receipt.candidate_delta[0],
        path="mcp-tools/a.py",
        new_oid="7" * 40,
    )
    aliased = _receipt_with_delta(receipt, (first, second))
    with pytest.raises(IntegrationProofError) as alias_error:
        validate_integration_proof(aliased.proof_id, expectation, aliased)
    assert alias_error.value.code == "RELAY_INTEGRATION_SCOPE_MISMATCH"

    bad_mode_entry = replace(receipt.candidate_delta[0], new_type="commit")
    bad_mode = _receipt_with_delta(receipt, (bad_mode_entry,))
    with pytest.raises(IntegrationProofError) as object_error:
        validate_integration_proof(bad_mode.proof_id, expectation, bad_mode)
    assert object_error.value.code == "RELAY_INTEGRATION_OBJECT_INVALID"

    empty = _receipt_with_delta(receipt, ())
    with pytest.raises(IntegrationProofError) as tree_error:
        validate_integration_proof(empty.proof_id, expectation, empty)
    assert tree_error.value.code == "RELAY_INTEGRATION_TREE_MISMATCH"


@pytest.mark.parametrize(
    ("receipt", "code"),
    [
        (replace(_receipt(), merge_free=False), "RELAY_INTEGRATION_ANCESTRY_INVALID"),
        (
            replace(_receipt(), final_parent_commit="b" * 40),
            "RELAY_INTEGRATION_ANCESTRY_INVALID",
        ),
        (
            replace(_receipt(), final_tree="7" * 40),
            "RELAY_INTEGRATION_TREE_MISMATCH",
        ),
        (
            replace(_receipt(), ref_before_commit="b" * 40),
            "RELAY_INTEGRATION_HEAD_STALE",
        ),
    ],
)
def test_proof_rejects_ancestry_tree_and_head_forgery(
    receipt: IntegrationProofReceipt, code: str
) -> None:
    with pytest.raises(IntegrationProofError) as raised:
        validate_integration_proof(receipt.proof_id, receipt.expectation, receipt)

    assert raised.value.code == code


def test_concurrent_candidate_keeps_allocation_base_then_rebases_to_monotonic_head(
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
            capacity=2,
        ),
        idempotency_key="concurrent-proof-create",
    )
    actions = {str(item["task_id"]): item for item in created["host_actions"]}
    versions: dict[str, int] = {}
    for task_id, action in actions.items():
        assert isinstance(action, dict)
        version = bind_worker(relay, action)
        versions[task_id] = _record_evidence(relay, action, version)

    action_a = actions["writer-a"]
    handed_a = relay.handoff(
        worker_request(
            action_a,
            lifecycle_action="candidate_handoff",
            capability=issue_worker(
                relay, action_a, lifecycle_action="candidate_handoff"
            ),
            expected_task_version=versions["writer-a"],
            candidate=_candidate([_EVIDENCE_HASH]),
        )
    )
    task_a = handed_a["task"]
    assert isinstance(task_a, dict)
    _review(
        relay,
        action_a,
        task_a,
        task_id="writer-a",
        candidate_id="candidate-a",
    )
    lease_a = action_a["lease"]
    assert isinstance(lease_a, dict)
    expectation_a = store.integration_expectation(
        "relay-runtime-v3",
        "writer-a",
        epoch=int(lease_a["epoch"]),
        expected_task_version=int(task_a["task_version"]),
        candidate_id="candidate-a",
        proof_id="sha256:" + "0" * 64,
    )
    proof_a = registry.register(synthetic_integration_receipt(expectation_a))
    relay.integrate(
        _integration_request(
            relay,
            action_a,
            task_a,
            proof_a,
            task_id="writer-a",
            candidate_id="candidate-a",
        )
    )

    action_b = actions["writer-b"]
    handed_b = relay.handoff(
        worker_request(
            action_b,
            lifecycle_action="candidate_handoff",
            capability=issue_worker(
                relay, action_b, lifecycle_action="candidate_handoff"
            ),
            expected_task_version=versions["writer-b"],
            candidate={
                "candidate_id": "candidate-b",
                "branch": "relay/candidate-b",
                "base_commit": _BASE_COMMIT,
                "head_commit": "7" * 40,
                "diff_hash": "sha256:" + "6" * 64,
                "evidence_hashes": [_EVIDENCE_HASH],
                "pr_reference": None,
            },
        )
    )
    task_b = handed_b["task"]
    assert isinstance(task_b, dict)
    _review(
        relay,
        action_b,
        task_b,
        task_id="writer-b",
        candidate_id="candidate-b",
    )
    lease_b = action_b["lease"]
    assert isinstance(lease_b, dict)
    before_replay = store.database_fingerprint()
    with pytest.raises(RelayError) as replay_error:
        relay.integrate(
            _integration_request(
                relay,
                action_b,
                task_b,
                proof_a,
                task_id="writer-b",
                candidate_id="candidate-b",
            )
        )
    assert replay_error.value.code == "RELAY_CANDIDATE_INVALID"
    assert store.database_fingerprint() == before_replay

    stale_expectation = store.integration_expectation(
        "relay-runtime-v3",
        "writer-b",
        epoch=int(lease_b["epoch"]),
        expected_task_version=int(task_b["task_version"]),
        candidate_id="candidate-b",
        proof_id="sha256:" + "0" * 64,
    )
    stale_proof = registry.register(
        synthetic_integration_receipt(stale_expectation, final_commit="8" * 40)
    )
    with pytest.raises(RelayError) as raised:
        relay.integrate(
            _integration_request(
                relay,
                action_b,
                task_b,
                stale_proof,
                task_id="writer-b",
                candidate_id="candidate-b",
            )
        )
    assert raised.value.code == "RELAY_CANDIDATE_INVALID"
    assert registry.states[stale_proof] == "registered"

    relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer-b",
            "action": "rebase",
            "epoch": lease_b["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": task_b["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer-b",
                action="rebase",
                epoch=int(lease_b["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-b",
            "base_commit": "2" * 40,
            "head_commit": "8" * 40,
            "diff_hash": "sha256:" + "5" * 64,
            "evidence_hashes": [_EVIDENCE_HASH],
        }
    )
    _review(
        relay,
        action_b,
        task_b,
        task_id="writer-b",
        candidate_id="candidate-b",
    )
    expectation_b = store.integration_expectation(
        "relay-runtime-v3",
        "writer-b",
        epoch=int(lease_b["epoch"]),
        expected_task_version=int(task_b["task_version"]),
        candidate_id="candidate-b",
        proof_id="sha256:" + "0" * 64,
    )
    proof_b = registry.register(
        synthetic_integration_receipt(expectation_b, final_commit="9" * 40)
    )
    relay.integrate(
        _integration_request(
            relay,
            action_b,
            task_b,
            proof_b,
            task_id="writer-b",
            candidate_id="candidate-b",
        )
    )

    status = relay.status("relay-runtime-v3")
    assert status["run"]["integration_head"] == "9" * 40
    assert status["run"]["integration_version"] == 2
    assert [item["integration_version"] for item in status["integration_proofs"]] == [
        1,
        2,
    ]


def test_real_git_proof_covers_full_delta_and_integrates_atomically(
    tmp_path: Path,
) -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    repository = (tmp_path / "real-git-repository").resolve()
    assert repository.is_relative_to(task_root)
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.autocrlf", "false")
    source = repository / "mcp-tools"
    source.mkdir()
    (source / "writer.py").write_text("value = 1\n", encoding="utf-8")
    (source / "delete.txt").write_text("remove me\n", encoding="utf-8")
    (source / "oldname.txt").write_text("rename me\n", encoding="utf-8")
    (source / "script.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "base")
    base_commit = _git_oid(repository, "HEAD")
    _git(repository, "update-ref", "refs/heads/integration", base_commit)

    _git(repository, "checkout", "-b", "candidate")
    (source / "writer.py").write_text("value = 2\n", encoding="utf-8")
    (source / "delete.txt").unlink()
    (source / "oldname.txt").rename(source / "newname.txt")
    _git(repository, "add", "--all")
    _git(repository, "update-index", "--chmod=+x", "mcp-tools/script.sh")
    link_blob = _git(
        repository, "hash-object", "-w", "--stdin", input_bytes=b"writer.py"
    ).decode("ascii")
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{link_blob},mcp-tools/link",
    )
    _git(repository, "commit", "-m", "candidate")
    candidate_commit = _git_oid(repository, "HEAD")
    candidate_delta = _git_delta(repository, base_commit, candidate_commit)
    delta_by_path = {entry.path: entry for entry in candidate_delta}
    assert delta_by_path["mcp-tools/delete.txt"].new_oid is None
    assert delta_by_path["mcp-tools/oldname.txt"].new_oid is None
    assert delta_by_path["mcp-tools/newname.txt"].old_oid is None
    assert delta_by_path["mcp-tools/script.sh"].new_mode == "100755"
    assert delta_by_path["mcp-tools/link"].new_mode == "120000"
    assert delta_by_path["mcp-tools/link"].new_type == "blob"
    diff_hash = canonical_hash(
        {
            "schema": "2718lab-devkit/relay-integration-tree-delta-v1",
            "entries": [entry.to_dict() for entry in candidate_delta],
        }
    )

    registry = _GitProofRegistry(repository)
    relay, store = service(tmp_path, registry)
    compiled = plan(
        task(
            "writer",
            priority=100,
            write_scope=[{"path": "mcp-tools", "kind": "tree"}],
        ),
        task(
            "child",
            dependencies=["writer"],
            write_scope=[{"path": "child", "kind": "tree"}],
        ),
        capacity=1,
    )
    compiled["base_commit"] = base_commit
    _rehash(compiled)
    created = relay.start_create(compiled, idempotency_key="real-git-create")
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    task_version = _record_evidence(relay, action, task_version)
    handed_off = relay.handoff(
        worker_request(
            action,
            lifecycle_action="candidate_handoff",
            capability=issue_worker(
                relay, action, lifecycle_action="candidate_handoff"
            ),
            expected_task_version=task_version,
            candidate={
                "candidate_id": "candidate-a",
                "branch": "candidate",
                "base_commit": base_commit,
                "head_commit": candidate_commit,
                "diff_hash": diff_hash,
                "evidence_hashes": [_EVIDENCE_HASH],
                "pr_reference": None,
            },
        )
    )
    candidate_task = handed_off["task"]
    assert isinstance(candidate_task, dict)
    lease = action["lease"]
    assert isinstance(lease, dict)
    relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer",
            "action": "review",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer",
                action="review",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "review_digest": "sha256:" + "1" * 64,
        }
    )
    expectation = store.integration_expectation(
        "relay-runtime-v3",
        "writer",
        epoch=int(lease["epoch"]),
        expected_task_version=int(candidate_task["task_version"]),
        candidate_id="candidate-a",
        proof_id="sha256:" + "0" * 64,
    )
    proof_id = registry.attest_and_register(expectation)

    integrated = relay.integrate(
        _integration_request(relay, action, candidate_task, proof_id)
    )

    receipt = registry.receipts[proof_id]
    assert integrated["task"]["state"] == "integrated"
    assert _git_oid(repository, receipt.integration_ref) == receipt.final_commit
    assert receipt.final_parent_commit == base_commit
    assert receipt.final_tree == receipt.candidate_tree
    assert receipt.final_delta == receipt.candidate_delta
    status = relay.status("relay-runtime-v3")
    assert status["run"]["integration_head"] == receipt.final_commit
    assert status["integration_proofs"][0]["proof_id"] == proof_id

    wrong_parent = replace(receipt, final_parent_commit=candidate_commit)
    with pytest.raises(IntegrationProofError) as parent_error:
        validate_integration_proof(
            wrong_parent.proof_id, wrong_parent.expectation, wrong_parent
        )
    assert parent_error.value.code == "RELAY_INTEGRATION_ANCESTRY_INVALID"
    wrong_tree = replace(receipt, final_tree=receipt.predecessor_tree)
    with pytest.raises(IntegrationProofError) as tree_error:
        validate_integration_proof(
            wrong_tree.proof_id, wrong_tree.expectation, wrong_tree
        )
    assert tree_error.value.code == "RELAY_INTEGRATION_TREE_MISMATCH"

    _git(
        repository,
        "update-ref",
        receipt.integration_ref,
        base_commit,
        receipt.final_commit,
    )
    with pytest.raises(IntegrationProofError) as ref_error:
        registry.reserve(proof_id, expectation)
    assert ref_error.value.code == "RELAY_INTEGRATION_HEAD_STALE"

    tree_as_candidate = replace(
        expectation, candidate_head_commit=receipt.candidate_tree
    )
    with pytest.raises(IntegrationProofError) as object_error:
        registry.attest_and_register(tree_as_candidate)
    assert object_error.value.code == "RELAY_INTEGRATION_OBJECT_INVALID"

    _git(repository, "checkout", "-b", "side", base_commit)
    (source / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "side")
    _git(repository, "checkout", "candidate")
    _git(repository, "merge", "--no-ff", "side", "-m", "merge candidate")
    merge_commit = _git_oid(repository, "HEAD")
    merge_expectation = replace(expectation, candidate_head_commit=merge_commit)
    with pytest.raises(IntegrationProofError) as merge_error:
        registry.attest_and_register(merge_expectation)
    assert merge_error.value.code == "RELAY_INTEGRATION_ANCESTRY_INVALID"

    _git(
        repository,
        "update-ref",
        receipt.integration_ref,
        receipt.final_commit,
        base_commit,
    )


class _FencedReservation:
    """A host-private reservation double with only the v3 settlement surface."""

    def __init__(
        self,
        resolver: _FencedProofResolver,
        proof_id: str,
        receipt: IntegrationProofReceipt,
        fence: ProofFinalizationFence,
    ) -> None:
        self._resolver = resolver
        self._proof_id = proof_id
        self._receipt = receipt
        self._fence = fence

    @property
    def receipt(self) -> IntegrationProofReceipt:
        return self._receipt

    @property
    def fence(self) -> ProofFinalizationFence:
        return self._fence

    def settle(self, *, evidence: ProofFinalizationEvidence) -> str:
        if self._resolver.fail_settlement:
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")
        if (
            evidence.finalization_id != self._fence.finalization_id
            or evidence.fence_hash != self._fence.fence_hash
            or evidence.state not in {"committed", "aborted"}
        ):
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
        if evidence.state == "committed":
            if evidence.result_hash is None:
                raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
            if self._resolver.references[self._proof_id] == "base":
                self._resolver.references[self._proof_id] = "final"
            elif self._resolver.references[self._proof_id] != "final":
                raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
            already_consumed = self._resolver.states[self._proof_id] == "consumed"
            self._resolver.states[self._proof_id] = "consumed"
            self._resolver.settlements.append("committed")
            return "already_consumed" if already_consumed else "consumed"
        if evidence.result_hash is not None:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_CORRUPT")
        if self._resolver.references[self._proof_id] == "final":
            self._resolver.references[self._proof_id] = "base"
        elif self._resolver.references[self._proof_id] != "base":
            raise IntegrationProofError("RELAY_INTEGRATION_HEAD_STALE")
        already_released = self._resolver.states[self._proof_id] == "registered"
        self._resolver.states[self._proof_id] = "registered"
        self._resolver.settlements.append("aborted")
        return "already_released" if already_released else "released"


class _FencedProofResolver:
    """Structural v3 resolver fake; Git movement is represented by exact labels."""

    def __init__(self) -> None:
        self.receipts: dict[str, IntegrationProofReceipt] = {}
        self.states: dict[str, str] = {}
        self.references: dict[str, str] = {}
        self.reservations: dict[str, _FencedReservation] = {}
        self.settlements: list[str] = []
        self.fail_settlement = False
        self.on_reserved: Callable[[], None] | None = None

    def register(self, receipt: IntegrationProofReceipt) -> str:
        proof_id = receipt.proof_id
        if proof_id in self.receipts:
            raise AssertionError("duplicate test proof")
        self.receipts[proof_id] = receipt
        self.states[proof_id] = "registered"
        self.references[proof_id] = "base"
        return proof_id

    def reserve(
        self, proof_id: str, expectation: IntegrationExpectation
    ) -> _FencedReservation:
        receipt = self.receipts.get(proof_id)
        if receipt is None:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_UNREGISTERED")
        if receipt.expectation != expectation:
            raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
        if self.states[proof_id] != "registered":
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")
        fence = ProofFinalizationFence(
            finalization_id=f"finalization-{proof_id[7:31]}",
            reservation_epoch=1,
            integration_proof_id=proof_id,
            workspace_id=expectation.workspace_id,
            expectation_key=expectation.candidate_id,
            expectation_version=expectation.task_version,
            expectation_hash=expectation.expectation_hash,
            target_ref=receipt.integration_ref,
            base_oid=receipt.ref_before_commit,
            final_oid=receipt.ref_after_commit,
        )
        reservation = _FencedReservation(self, proof_id, receipt, fence)
        self.reservations[proof_id] = reservation
        self.states[proof_id] = "reserved"
        self.references[proof_id] = "final"
        if self.on_reserved is not None:
            self.on_reserved()
        return reservation

    def recover_finalizations(self, authority: object) -> None:
        resolve = getattr(authority, "resolve_or_abort_finalization")
        for proof_id, reservation in self.reservations.items():
            if self.states[proof_id] != "reserved":
                continue
            evidence = resolve(fence=reservation.fence)
            reservation.settle(evidence=evidence)


def _registered_fenced_candidate(
    tmp_path: Path,
) -> tuple[
    RelayService,
    RelayStore,
    _FencedProofResolver,
    dict[str, object],
    dict[str, object],
    str,
]:
    resolver = _FencedProofResolver()
    relay, store, action, candidate_task = _reviewed_candidate(tmp_path, resolver)  # type: ignore[arg-type]
    lease = action["lease"]
    assert isinstance(lease, dict)
    expectation = store.integration_expectation(
        "relay-runtime-v3",
        "writer",
        epoch=int(lease["epoch"]),
        expected_task_version=int(candidate_task["task_version"]),
        candidate_id="candidate-a",
        proof_id="sha256:" + "0" * 64,
    )
    proof_id = resolver.register(synthetic_integration_receipt(expectation))
    return relay, store, resolver, action, candidate_task, proof_id


def test_finalization_stale_expectation_seals_abort_and_rolls_back_fenced_reservation(
    tmp_path: Path,
) -> None:
    relay, store, resolver, action, candidate_task, proof_id = _registered_fenced_candidate(
        tmp_path
    )
    connection = store._require_connection()

    def make_expectation_stale() -> None:
        connection.execute(
            """
            UPDATE relay_v3_candidates SET review_digest = ?
            WHERE candidate_id = 'candidate-a'
            """,
            ("sha256:" + "8" * 64,),
        )

    resolver.on_reserved = make_expectation_stale
    with pytest.raises(RelayError) as raised:
        relay.integrate(_integration_request(relay, action, candidate_task, proof_id))

    assert raised.value.code == "RELAY_EXPECTATION_STALE"
    assert resolver.states[proof_id] == "registered"
    assert resolver.references[proof_id] == "base"
    assert resolver.settlements == ["aborted"]


@pytest.mark.parametrize(
    ("phase", "expected_state", "expected_reference", "expected_settlement"),
    [
        ("before_commit", "registered", "base", "aborted"),
        ("after_commit", "consumed", "final", "committed"),
    ],
)
def test_finalization_ambiguous_store_failure_settles_only_authoritative_terminal(
    tmp_path: Path,
    phase: str,
    expected_state: str,
    expected_reference: str,
    expected_settlement: str,
) -> None:
    relay, store, resolver, action, candidate_task, proof_id = _registered_fenced_candidate(
        tmp_path
    )
    original = store.integrate_candidate

    def fail_before(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RelayStorageFailure()

    def fail_after(*args: object, **kwargs: object) -> dict[str, object]:
        original(*args, **kwargs)
        raise RelayStorageFailure()

    store.integrate_candidate = fail_before if phase == "before_commit" else fail_after  # type: ignore[method-assign]
    with pytest.raises(RelayError):
        relay.integrate(_integration_request(relay, action, candidate_task, proof_id))

    assert resolver.states[proof_id] == expected_state
    assert resolver.references[proof_id] == expected_reference
    assert resolver.settlements == [expected_settlement]


def test_finalization_failed_settlement_recovers_and_consumes_exactly_once(
    tmp_path: Path,
) -> None:
    relay, _store, resolver, action, candidate_task, proof_id = _registered_fenced_candidate(
        tmp_path
    )
    request = _integration_request(relay, action, candidate_task, proof_id)
    resolver.fail_settlement = True

    with pytest.raises(RelayError):
        relay.integrate(request)

    assert resolver.states[proof_id] == "reserved"
    assert resolver.references[proof_id] == "final"
    assert resolver.settlements == []

    resolver.fail_settlement = False
    recovered = relay.integrate(request)

    assert recovered["task"]["state"] == "integrated"
    assert resolver.states[proof_id] == "consumed"
    assert resolver.references[proof_id] == "final"
    assert resolver.settlements == ["committed"]
