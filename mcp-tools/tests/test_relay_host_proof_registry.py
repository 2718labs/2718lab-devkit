"""Host-private durable Relay proof registry coverage."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_relay_integration_proof import (
    _git,
    _git_delta,
    _git_oid,
    _integration_request,
)
from test_relay_lifecycle import _EVIDENCE_HASH, _record_evidence
from test_relay_runtime import (
    _WORKSPACE_ID as _RELAY_WORKSPACE_ID,
)
from test_relay_runtime import (
    _rehash,
    bind_worker,
    issue_worker,
    plan,
    service,
    task,
    worker_request,
)

import devkit_runtime.relay_proof_registry as proof_registry_module
from devkit_relay.canonical import canonical_hash
from devkit_relay.proofs import (
    IntegrationDeltaEntry,
    IntegrationExpectation,
    IntegrationProofError,
    IntegrationProofReceipt,
    IntegrationScopeEntry,
    ProofFinalizationEvidence,
    ProofFinalizationFence,
    RelayProofReservation,
)
from devkit_relay.service import RelayError, RelayService
from devkit_relay.store import RelayStore
from devkit_runtime.relay_proof_registry import (
    ControlledGitTargetResolver,
    GitProofTarget,
    RelayProofRegistry,
)

_WORKSPACE_ID = "sha256:" + "4" * 64


def _real_git_expectation(
    tmp_path: Path, *, object_format: str | None = None
) -> tuple[IntegrationExpectation, Path]:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    assert tmp_path.resolve().is_relative_to(task_root)
    repository = tmp_path / "private-repository"
    repository.mkdir()
    init_arguments = ("init", "-b", "main")
    if object_format is not None:
        init_arguments = ("init", f"--object-format={object_format}", "-b", "main")
    _git(repository, *init_arguments)
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.longpaths", "true")
    source = repository / "mcp-tools"
    source.mkdir()
    (source / "writer.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "base")
    base_commit = _git_oid(repository, "HEAD")
    _git(repository, "update-ref", "refs/heads/integration", base_commit)

    _git(repository, "checkout", "-b", "candidate")
    (source / "writer.py").write_text("value = 2\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "candidate")
    candidate_commit = _git_oid(repository, "HEAD")
    delta = _git_delta(repository, base_commit, candidate_commit)
    diff_hash = canonical_hash(
        {
            "schema": "2718lab-devkit/relay-integration-tree-delta-v1",
            "entries": [entry.to_dict() for entry in delta],
        }
    )
    return (
        IntegrationExpectation(
            workflow_id="relay-runtime-v3",
            run_id="run-a",
            plan_hash="sha256:" + "1" * 64,
            workspace_id=_WORKSPACE_ID,
            task_id="writer",
            task_version=1,
            originating_epoch=1,
            sol_scope="sol:integrate",
            candidate_id="candidate-a",
            candidate_base_commit=base_commit,
            candidate_head_commit=candidate_commit,
            candidate_diff_hash=diff_hash,
            candidate_evidence_hashes=("sha256:" + "2" * 64,),
            review_digest="sha256:" + "3" * 64,
            predecessor_integration_head=base_commit,
            predecessor_integration_version=0,
            write_scope=(IntegrationScopeEntry("mcp-tools", "tree"),),
        ),
        repository,
    )


def _target(repository: Path, name: str = "a") -> GitProofTarget:
    return GitProofTarget(
        repository=repository,
        repository_id=canonical_hash({"schema": "test-proof-target-v1", "name": name}),
        integration_ref="refs/heads/integration",
        attestor_id="host-git",
        attestor_version="1.0",
    )


def _registry(
    database_path: Path, target: GitProofTarget, *, workspace_id: str = _WORKSPACE_ID
) -> RelayProofRegistry:
    proof_registry_module.bootstrap_relay_proof_registry(database_path)
    return RelayProofRegistry(
        database_path,
        target_resolver=ControlledGitTargetResolver({workspace_id: target}),
    )


def _committed_evidence(
    fence: ProofFinalizationFence,
) -> ProofFinalizationEvidence:
    return ProofFinalizationEvidence(
        finalization_id=fence.finalization_id,
        state="committed",
        fence_hash=fence.fence_hash,
        result_hash="sha256:" + "c" * 64,
        journal_version=1,
    )


def _aborted_evidence(fence: ProofFinalizationFence) -> ProofFinalizationEvidence:
    return ProofFinalizationEvidence(
        finalization_id=fence.finalization_id,
        state="aborted",
        fence_hash=fence.fence_hash,
        result_hash=None,
        journal_version=1,
    )


class _TerminalFinalizationAuthority:
    """Deterministic terminal-journal stand-in for registry crash recovery."""

    def __init__(self, *, committed: bool) -> None:
        self._committed = committed

    def prepare_finalization(
        self, *, fence: ProofFinalizationFence
    ) -> ProofFinalizationEvidence:
        raise AssertionError(f"unexpected prepare for {fence.finalization_id}")

    def resolve_or_abort_finalization(
        self, *, fence: ProofFinalizationFence
    ) -> ProofFinalizationEvidence:
        if self._committed:
            return _committed_evidence(fence)
        return _aborted_evidence(fence)

    def find_committed_finalization(
        self,
        *,
        integration_proof_id: str,
        expectation_hash: str,
    ) -> ProofFinalizationEvidence | None:
        del integration_proof_id, expectation_hash
        return None


def _expectation_for(
    *,
    base_commit: str,
    candidate_commit: str,
    candidate_delta: tuple[IntegrationDeltaEntry, ...],
    candidate_id: str,
) -> IntegrationExpectation:
    return IntegrationExpectation(
        workflow_id="relay-runtime-v3",
        run_id=f"run-{candidate_id}",
        plan_hash="sha256:" + "1" * 64,
        workspace_id=_WORKSPACE_ID,
        task_id="writer",
        task_version=1,
        originating_epoch=1,
        sol_scope="sol:integrate",
        candidate_id=candidate_id,
        candidate_base_commit=base_commit,
        candidate_head_commit=candidate_commit,
        candidate_diff_hash=canonical_hash(
            {
                "schema": "2718lab-devkit/relay-integration-tree-delta-v1",
                "entries": [entry.to_dict() for entry in candidate_delta],
            }
        ),
        candidate_evidence_hashes=("sha256:" + "2" * 64,),
        review_digest="sha256:" + "3" * 64,
        predecessor_integration_head=base_commit,
        predecessor_integration_version=0,
        write_scope=(IntegrationScopeEntry("mcp-tools", "tree"),),
    )


def test_attests_registers_reserves_and_consumes_real_git_proof(tmp_path: Path) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)

    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)

    assert reservation.receipt.proof_id == proof_id
    assert (
        _git_oid(repository, target.integration_ref) == reservation.receipt.final_commit
    )
    with pytest.raises(IntegrationProofError) as busy:
        registry.reserve(proof_id, expectation)
    assert busy.value.code == "RELAY_INTEGRATION_PROOF_BUSY"
    assert (
        reservation.settle(evidence=_committed_evidence(reservation.fence))
        == "consumed"
    )


def test_attestor_rejects_shallow_repository_before_ref_cas(tmp_path: Path) -> None:
    expectation, source_repository = _real_git_expectation(tmp_path)
    shallow_repository = tmp_path / "shallow-private-repository"
    _git(
        tmp_path,
        "-c",
        "core.longpaths=true",
        "clone",
        "--no-local",
        "--depth",
        "2",
        source_repository.as_uri(),
        str(shallow_repository),
    )
    _git(
        shallow_repository,
        "-c",
        "core.longpaths=true",
        "config",
        "core.longpaths",
        "true",
    )
    _git(
        shallow_repository,
        "update-ref",
        "refs/heads/integration",
        expectation.candidate_base_commit,
    )
    target = _target(shallow_repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)

    with pytest.raises(IntegrationProofError) as shallow:
        registry.attest_and_register(expectation)
    assert shallow.value.code == "RELAY_INTEGRATION_ANCESTRY_INVALID"
    assert (
        _git_oid(shallow_repository, target.integration_ref)
        == expectation.candidate_base_commit
    )


def test_attests_real_sha256_repository_delta(tmp_path: Path) -> None:
    expectation, repository = _real_git_expectation(tmp_path, object_format="sha256")
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)

    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)

    assert len(expectation.candidate_base_commit) == 64
    assert reservation.receipt.object_format == "sha256"
    assert reservation.receipt.candidate_delta == _git_delta(
        repository,
        expectation.candidate_base_commit,
        expectation.candidate_head_commit,
    )
    assert (
        reservation.settle(evidence=_committed_evidence(reservation.fence))
        == "consumed"
    )


def test_attestor_rejects_real_tree_object_as_candidate_commit(tmp_path: Path) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    tree_object = (
        _git(
            repository,
            "rev-parse",
            "--verify",
            f"{expectation.candidate_head_commit}^{{tree}}",
        )
        .decode("ascii")
        .strip()
    )
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)

    with pytest.raises(IntegrationProofError) as noncommit:
        registry.attest_and_register(
            replace(expectation, candidate_head_commit=tree_object)
        )
    assert noncommit.value.code == "RELAY_INTEGRATION_OBJECT_INVALID"
    assert (
        _git_oid(repository, target.integration_ref)
        == expectation.candidate_base_commit
    )


def test_attestor_ignores_git_replace_when_validating_merge_ancestry(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "replace-private-repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.longpaths", "true")
    source = repository / "mcp-tools"
    source.mkdir()
    (source / "writer.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "base")
    base_commit = _git_oid(repository, "HEAD")
    _git(repository, "update-ref", "refs/heads/integration", base_commit)

    _git(repository, "checkout", "-b", "candidate")
    (source / "writer.py").write_text("value = 2\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "candidate")
    _git(repository, "checkout", "-b", "side", base_commit)
    (source / "side.py").write_text("side = True\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "side")
    _git(repository, "checkout", "candidate")
    _git(repository, "merge", "--no-ff", "side", "-m", "merge candidate")
    merge_commit = _git_oid(repository, "HEAD")
    expectation = _expectation_for(
        base_commit=base_commit,
        candidate_commit=merge_commit,
        candidate_delta=_git_delta(repository, base_commit, merge_commit),
        candidate_id="replace-merge",
    )

    merge_tree = _git(
        repository, "rev-parse", "--verify", f"{merge_commit}^{{tree}}"
    ).decode("ascii")
    replacement_commit = _git(
        repository,
        "commit-tree",
        merge_tree,
        "-p",
        base_commit,
        "-m",
        "forged single-parent replacement",
    ).decode("ascii")
    _git(repository, "replace", merge_commit, replacement_commit)

    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)
    with pytest.raises(IntegrationProofError) as replaced:
        registry.attest_and_register(expectation)
    assert replaced.value.code == "RELAY_INTEGRATION_ANCESTRY_INVALID"
    assert _git_oid(repository, target.integration_ref) == base_commit


def test_attestor_ignores_git_grafts_when_validating_merge_ancestry(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    base_commit = expectation.candidate_base_commit
    source = repository / "mcp-tools"
    _git(repository, "checkout", "-b", "side", base_commit)
    (source / "side.py").write_text("side = True\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "side")
    _git(repository, "checkout", "candidate")
    _git(repository, "merge", "--no-ff", "side", "-m", "merge candidate")
    merge_commit = _git_oid(repository, "HEAD")
    grafts = repository / ".git" / "info" / "grafts"
    grafts.write_text(f"{merge_commit} {base_commit}\n", encoding="ascii")
    forged = _expectation_for(
        base_commit=base_commit,
        candidate_commit=merge_commit,
        candidate_delta=_git_delta(repository, base_commit, merge_commit),
        candidate_id="graft-merge",
    )
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)

    with pytest.raises(IntegrationProofError) as grafted:
        registry.attest_and_register(forged)
    assert grafted.value.code == "RELAY_INTEGRATION_ANCESTRY_INVALID"
    assert _git_oid(repository, target.integration_ref) == base_commit


def test_attestor_neutralizes_replace_and_config_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    base_commit = expectation.candidate_base_commit
    source = repository / "mcp-tools"
    _git(repository, "checkout", "-b", "side", base_commit)
    (source / "side.py").write_text("side = True\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "side")
    _git(repository, "checkout", "candidate")
    _git(repository, "merge", "--no-ff", "side", "-m", "merge candidate")
    merge_commit = _git_oid(repository, "HEAD")
    merge_tree = _git(
        repository, "rev-parse", "--verify", f"{merge_commit}^{{tree}}"
    ).decode("ascii")
    replacement_commit = _git(
        repository,
        "commit-tree",
        merge_tree,
        "-p",
        base_commit,
        "-m",
        "environment replacement",
    ).decode("ascii")
    candidate_delta = _git_delta(repository, base_commit, merge_commit)
    # Use Git's portable replacement namespace.  A custom
    # ``GIT_REPLACE_REF_BASE`` is rejected by some Git for Windows builds
    # before the attestor is reached, which would test the runner rather than
    # the host's environment fencing.
    replacement_base = "refs/replace"
    _git(
        repository,
        "update-ref",
        f"{replacement_base}/{merge_commit}",
        replacement_commit,
    )
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", replacement_base)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.useReplaceRefs")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    # Keep the hostile variables in the parent environment and exercise only
    # the attestor's controlled Git path.  Git for Windows 2.55 exits 3 for a
    # standalone rev-list under these replacement/config overrides before the
    # product boundary is reached, so asserting that runner-specific command
    # would not test the host's environment fencing.
    forged = _expectation_for(
        base_commit=base_commit,
        candidate_commit=merge_commit,
        candidate_delta=candidate_delta,
        candidate_id="environment-merge",
    )
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)

    with pytest.raises(IntegrationProofError) as replaced:
        registry.attest_and_register(forged)
    assert replaced.value.code == "RELAY_INTEGRATION_ANCESTRY_INVALID"
    assert _git_oid(repository, target.integration_ref) == base_commit


def test_rejects_every_full_expectation_binding_swap_without_private_leakage(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)
    proof_id = registry.attest_and_register(expectation)
    private_path = str(repository)
    private_bearer = "private-bearer-not-for-relay-output"

    swaps = (
        replace(expectation, task_id="other-task"),
        replace(expectation, candidate_id="other-candidate"),
        replace(expectation, workspace_id="sha256:" + "5" * 64),
        replace(expectation, task_version=2),
        replace(expectation, originating_epoch=2),
        replace(expectation, candidate_diff_hash="sha256:" + "6" * 64),
        replace(
            expectation,
            write_scope=(IntegrationScopeEntry("other-path", "tree"),),
        ),
    )
    for swapped in swaps:
        with pytest.raises(IntegrationProofError) as raised:
            registry.reserve(proof_id, swapped)
        assert raised.value.code == "RELAY_INTEGRATION_BINDING_MISMATCH"
        assert private_path not in str(raised.value)
        assert private_bearer not in str(raised.value)

    assert private_path not in repr(target)
    assert private_path not in repr(registry)
    assert private_bearer not in repr(registry)


def test_concurrent_reservation_is_exclusive_and_terminal_abort_reopens_same_proof(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    registry = _registry(database_path, target)
    proof_id = registry.attest_and_register(expectation)
    first = _registry(database_path, target)
    second = _registry(database_path, target)
    barrier = threading.Barrier(3)
    outcomes: queue.Queue[tuple[str, object]] = queue.Queue()

    def reserve_concurrently(candidate: RelayProofRegistry) -> None:
        barrier.wait()
        try:
            outcomes.put(("reserved", candidate.reserve(proof_id, expectation)))
        except IntegrationProofError as error:
            outcomes.put(("error", error.code))

    threads = [
        threading.Thread(target=reserve_concurrently, args=(candidate,))
        for candidate in (first, second)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    observed = [outcomes.get_nowait() for _ in threads]
    reservations = [value for kind, value in observed if kind == "reserved"]
    errors = [value for kind, value in observed if kind == "error"]
    assert len(reservations) == 1
    assert errors == ["RELAY_INTEGRATION_PROOF_BUSY"]
    held = reservations[0]
    assert isinstance(held, RelayProofReservation)
    assert held.settle(evidence=_aborted_evidence(held.fence)) == "released"

    reopened = registry.reserve(proof_id, expectation)
    assert reopened.settle(evidence=_committed_evidence(reopened.fence)) == "consumed"


def test_stale_reservation_settlement_cannot_mutate_successor_epoch(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)
    proof_id = registry.attest_and_register(expectation)
    stale = registry.reserve(proof_id, expectation)
    stale_fence = stale.fence
    assert stale.settle(evidence=_aborted_evidence(stale_fence)) == "released"
    successor = registry.reserve(proof_id, expectation)

    with pytest.raises(IntegrationProofError) as stale_settle:
        successor.settle(evidence=_committed_evidence(stale_fence))
    assert stale_settle.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"
    with pytest.raises(IntegrationProofError) as successor_busy:
        registry.reserve(proof_id, expectation)
    assert successor_busy.value.code == "RELAY_INTEGRATION_PROOF_BUSY"
    assert successor.settle(evidence=_committed_evidence(successor.fence)) == "consumed"


def test_orphaned_reservation_recovers_from_terminal_abort_without_late_mutation(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    registry = _registry(database_path, target)
    proof_id = registry.attest_and_register(expectation)
    crashed = registry.reserve(proof_id, expectation)
    recovered_registry = _registry(database_path, target)
    recovered_registry.recover_finalizations(
        _TerminalFinalizationAuthority(committed=False)
    )
    recovered = recovered_registry.reserve(proof_id, expectation)

    with pytest.raises(IntegrationProofError) as busy:
        registry.reserve(proof_id, expectation)
    assert busy.value.code == "RELAY_INTEGRATION_PROOF_BUSY"
    with pytest.raises(IntegrationProofError) as stale:
        crashed.settle(evidence=_committed_evidence(crashed.fence))
    assert stale.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"
    assert recovered.settle(evidence=_committed_evidence(recovered.fence)) == "consumed"

    with pytest.raises(IntegrationProofError) as replay:
        registry.reserve(proof_id, expectation)
    assert replay.value.code == "RELAY_INTEGRATION_PROOF_REPLAY"


def test_rejects_corrupt_private_receipt_without_path_disclosure(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    registry = _registry(database_path, target)
    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)
    assert (
        reservation.settle(evidence=_aborted_evidence(reservation.fence)) == "released"
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE relay_host_integration_proofs SET receipt_json = '{}' WHERE proof_id = ?",
            (proof_id,),
        )
    with pytest.raises(IntegrationProofError) as corrupt:
        registry.reserve(proof_id, expectation)
    assert corrupt.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"
    assert str(repository) not in str(corrupt.value)


def _full_delta_expectation(
    tmp_path: Path,
) -> tuple[IntegrationExpectation, Path, tuple[IntegrationDeltaEntry, ...]]:
    repository = tmp_path / "full-delta-repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.longpaths", "true")
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
    delta = _git_delta(repository, base_commit, candidate_commit)
    expectation = IntegrationExpectation(
        workflow_id="relay-runtime-v3",
        run_id="run-full-delta",
        plan_hash="sha256:" + "1" * 64,
        workspace_id=_WORKSPACE_ID,
        task_id="writer",
        task_version=1,
        originating_epoch=1,
        sol_scope="sol:integrate",
        candidate_id="candidate-full-delta",
        candidate_base_commit=base_commit,
        candidate_head_commit=candidate_commit,
        candidate_diff_hash=canonical_hash(
            {
                "schema": "2718lab-devkit/relay-integration-tree-delta-v1",
                "entries": [entry.to_dict() for entry in delta],
            }
        ),
        candidate_evidence_hashes=("sha256:" + "2" * 64,),
        review_digest="sha256:" + "3" * 64,
        predecessor_integration_head=base_commit,
        predecessor_integration_version=0,
        write_scope=(IntegrationScopeEntry("mcp-tools", "tree"),),
    )
    return expectation, repository, delta


def test_attestor_covers_full_git_delta_before_cas_ref_update(tmp_path: Path) -> None:
    expectation, repository, delta = _full_delta_expectation(tmp_path)
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)
    invalid_scope = replace(
        expectation,
        write_scope=(IntegrationScopeEntry("mcp-tools/writer.py", "file"),),
    )

    with pytest.raises(IntegrationProofError) as scope_error:
        registry.attest_and_register(invalid_scope)
    assert scope_error.value.code == "RELAY_INTEGRATION_SCOPE_MISMATCH"
    assert (
        _git_oid(repository, target.integration_ref)
        == expectation.candidate_base_commit
    )

    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)
    by_path = {entry.path: entry for entry in delta}
    assert reservation.receipt.candidate_delta == delta
    assert by_path["mcp-tools/delete.txt"].new_oid is None
    assert by_path["mcp-tools/oldname.txt"].new_oid is None
    assert by_path["mcp-tools/newname.txt"].old_oid is None
    assert by_path["mcp-tools/script.sh"].new_mode == "100755"
    assert by_path["mcp-tools/link"].new_mode == "120000"
    assert by_path["mcp-tools/link"].new_type == "blob"
    assert (
        reservation.settle(evidence=_aborted_evidence(reservation.fence)) == "released"
    )


@pytest.mark.parametrize(
    ("changed_path", "scope"),
    [
        (
            "mcp-tools/writer.py.evil",
            IntegrationScopeEntry("mcp-tools/writer.py", "tree"),
        ),
        ("mcp-tools/Writer.py", IntegrationScopeEntry("mcp-tools/writer.py", "file")),
    ],
)
def test_real_git_rejects_segment_and_casefold_scope_aliases(
    tmp_path: Path, changed_path: str, scope: IntegrationScopeEntry
) -> None:
    repository = tmp_path / changed_path.replace("/", "-").replace(".", "_")
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.longpaths", "true")
    source = repository / "mcp-tools"
    source.mkdir()
    (source / "seed.py").write_text("seed = 1\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "base")
    base_commit = _git_oid(repository, "HEAD")
    _git(repository, "update-ref", "refs/heads/integration", base_commit)
    _git(repository, "checkout", "-b", "candidate")
    changed = repository.joinpath(*changed_path.split("/"))
    changed.write_text("blocked = True\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "scope alias")
    candidate_commit = _git_oid(repository, "HEAD")
    delta = _git_delta(repository, base_commit, candidate_commit)
    expectation = IntegrationExpectation(
        workflow_id="relay-runtime-v3",
        run_id="run-scope-alias",
        plan_hash="sha256:" + "1" * 64,
        workspace_id=_WORKSPACE_ID,
        task_id="writer",
        task_version=1,
        originating_epoch=1,
        sol_scope="sol:integrate",
        candidate_id="candidate-scope-alias",
        candidate_base_commit=base_commit,
        candidate_head_commit=candidate_commit,
        candidate_diff_hash=canonical_hash(
            {
                "schema": "2718lab-devkit/relay-integration-tree-delta-v1",
                "entries": [entry.to_dict() for entry in delta],
            }
        ),
        candidate_evidence_hashes=("sha256:" + "2" * 64,),
        review_digest="sha256:" + "3" * 64,
        predecessor_integration_head=base_commit,
        predecessor_integration_version=0,
        write_scope=(scope,),
    )
    target = _target(repository, changed_path)
    registry = _registry(tmp_path / f"{changed.name}.sqlite3", target)

    with pytest.raises(IntegrationProofError) as raised:
        registry.attest_and_register(expectation)
    assert raised.value.code == "RELAY_INTEGRATION_SCOPE_MISMATCH"
    assert _git_oid(repository, target.integration_ref) == base_commit


def test_registry_open_requires_explicit_bootstrap_without_creating_files(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path / "private-repository")
    database_path = tmp_path / "relay-proof-registry.sqlite3"

    with pytest.raises(IntegrationProofError) as unopened:
        RelayProofRegistry(
            database_path,
            target_resolver=ControlledGitTargetResolver({_WORKSPACE_ID: target}),
        )
    assert unopened.value.code == "RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE"
    assert database_path.exists() is False
    assert (tmp_path / ".relay-proof-registry.sqlite3.locks").exists() is False

    proof_registry_module.bootstrap_relay_proof_registry(database_path)
    opened = RelayProofRegistry(
        database_path,
        target_resolver=ControlledGitTargetResolver({_WORKSPACE_ID: target}),
    )
    opened.close()


def test_bootstrap_and_open_reject_lookalike_schema_without_full_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE relay_host_integration_proofs (
                proof_id TEXT,
                expectation_hash TEXT,
                target_key TEXT,
                receipt_json TEXT,
                state TEXT,
                reservation_token TEXT
            )
            """
        )
    target = _target(tmp_path / "private-repository")

    with pytest.raises(IntegrationProofError):
        proof_registry_module.bootstrap_relay_proof_registry(database_path)
    with pytest.raises(IntegrationProofError):
        RelayProofRegistry(
            database_path,
            target_resolver=ControlledGitTargetResolver({_WORKSPACE_ID: target}),
        )


def test_bootstrap_pins_registry_schema_constraints_indexes_and_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    proof_registry_module.bootstrap_relay_proof_registry(database_path)
    digest = "sha256:" + "a" * 64

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        index_rows = {
            row[1]: (bool(row[2]), bool(row[4]))
            for row in connection.execute(
                "PRAGMA index_list(relay_host_integration_proofs)"
            ).fetchall()
        }
        assert index_rows["relay_host_proof_target_state"] == (False, False)
        assert index_rows["relay_host_proof_active_target"] == (True, True)
        assert index_rows["relay_host_proof_pending"] == (False, False)
        assert tuple(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(relay_host_proof_events)"
            ).fetchall()
        ) == (
            "event_id",
            "proof_id",
            "reservation_epoch",
            "event_kind",
            "fence_hash",
            "result_hash",
        )
        event_index_rows = {
            row[1]: (bool(row[2]), bool(row[4]))
            for row in connection.execute(
                "PRAGMA index_list(relay_host_proof_events)"
            ).fetchall()
        }
        assert event_index_rows["relay_host_proof_event_epoch"] == (False, False)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO relay_host_integration_proofs
                (proof_id, expectation_hash, target_key, receipt_json, state)
                VALUES (?, ?, ?, '{}', 'reserved')
                """,
                (digest, digest, digest),
            )


def test_persisted_receipt_must_be_byte_for_byte_canonical_json(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)

    for variant in ("leading-space", "reordered-keys", "duplicate-key"):
        _git(
            repository,
            "update-ref",
            target.integration_ref,
            expectation.candidate_base_commit,
        )
        database_path = tmp_path / f"{variant}.sqlite3"
        registry = _registry(database_path, target)
        proof_id = registry.attest_and_register(expectation)
        with sqlite3.connect(database_path) as connection:
            receipt_json = connection.execute(
                "SELECT receipt_json FROM relay_host_integration_proofs WHERE proof_id = ?",
                (proof_id,),
            ).fetchone()[0]
            assert isinstance(receipt_json, str)
            if variant == "leading-space":
                mutated = f" {receipt_json}"
            elif variant == "reordered-keys":
                decoded = json.loads(receipt_json)
                assert isinstance(decoded, dict)
                mutated = json.dumps(
                    {key: decoded[key] for key in reversed(decoded)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                mutated = '{"proof_id":"sha256:' + "0" * 64 + '",' + receipt_json[1:]
            connection.execute(
                "UPDATE relay_host_integration_proofs SET receipt_json = ? WHERE proof_id = ?",
                (mutated, proof_id),
            )
        with pytest.raises(IntegrationProofError) as corrupt:
            registry.reserve(proof_id, expectation)
        assert corrupt.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"


def _sqlite_artifact_state(database_path: Path) -> tuple[tuple[bool, bytes, int], ...]:
    states: list[tuple[bool, bytes, int]] = []
    for path in (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    ):
        if path.exists():
            states.append((True, path.read_bytes(), path.stat().st_mtime_ns))
        else:
            states.append((False, b"", 0))
    return tuple(states)


def test_existing_registry_open_has_zero_database_wal_and_shm_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    proof_registry_module.bootstrap_relay_proof_registry(database_path)
    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA user_version = 3")
        connection.execute("PRAGMA user_version = 2")
        connection.execute("COMMIT")
        before = _sqlite_artifact_state(database_path)
        assert all(exists for exists, _, _ in before)
        target = _target(tmp_path / "private-repository")
        opened = RelayProofRegistry(
            database_path,
            target_resolver=ControlledGitTargetResolver({_WORKSPACE_ID: target}),
        )
        opened.close()
        assert _sqlite_artifact_state(database_path) == before
    finally:
        connection.close()


def test_registry_uses_no_durable_file_lock_artifacts(tmp_path: Path) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    registry = _registry(database_path, target)

    assert not (tmp_path / ".relay-proof-registry.sqlite3.locks").exists()
    registry.attest_and_register(expectation)
    assert not (tmp_path / ".relay-proof-registry.sqlite3.locks").exists()
    assert not tuple(tmp_path.glob("*.lock"))


def test_repo_ref_lock_is_shared_across_opaque_workspace_aliases(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    alias_workspace_id = "sha256:" + "7" * 64
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    proof_registry_module.bootstrap_relay_proof_registry(database_path)
    registry = RelayProofRegistry(
        database_path,
        target_resolver=ControlledGitTargetResolver(
            {_WORKSPACE_ID: target, alias_workspace_id: target}
        ),
    )
    registry.attest_and_register(expectation)

    with pytest.raises(IntegrationProofError) as aliased:
        registry.attest_and_register(
            replace(expectation, workspace_id=alias_workspace_id)
        )
    assert aliased.value.code == "RELAY_INTEGRATION_PROOF_BUSY"


def test_controlled_target_resolver_rejects_ambiguous_repo_identity_mappings(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "private-repository"
    other_repository = tmp_path / "other-private-repository"
    repository.mkdir()
    other_repository.mkdir()
    workspace_b = "sha256:" + "8" * 64
    target = _target(repository, "configured-a")
    different_id_same_repo_ref = _target(repository, "configured-b")
    same_id_other_repo = GitProofTarget(
        repository=other_repository,
        repository_id=target.repository_id,
        integration_ref=target.integration_ref,
        attestor_id=target.attestor_id,
        attestor_version=target.attestor_version,
    )
    same_id_other_ref = GitProofTarget(
        repository=repository,
        repository_id=target.repository_id,
        integration_ref="refs/heads/other-integration",
        attestor_id=target.attestor_id,
        attestor_version=target.attestor_version,
    )

    for conflicting in (
        different_id_same_repo_ref,
        same_id_other_repo,
        same_id_other_ref,
    ):
        with pytest.raises(ValueError) as rejected:
            ControlledGitTargetResolver(
                {_WORKSPACE_ID: target, workspace_b: conflicting}
            )
        assert str(repository) not in str(rejected.value)
        assert str(other_repository) not in str(rejected.value)


def _reviewed_real_relay_candidate(
    tmp_path: Path,
) -> tuple[
    RelayService,
    RelayStore,
    RelayProofRegistry,
    dict[str, object],
    dict[str, object],
    str,
]:
    candidate, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    registry_path = tmp_path / "relay-proof-registry.sqlite3"
    proof_registry_module.bootstrap_relay_proof_registry(registry_path)
    registry = RelayProofRegistry(
        registry_path,
        target_resolver=ControlledGitTargetResolver({_RELAY_WORKSPACE_ID: target}),
    )
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
    compiled["base_commit"] = candidate.candidate_base_commit
    _rehash(compiled)
    created = relay.start_create(compiled, idempotency_key="host-registry-create")
    host_actions = created["host_actions"]
    assert isinstance(host_actions, list)
    action = host_actions[0]
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
                "base_commit": candidate.candidate_base_commit,
                "head_commit": candidate.candidate_head_commit,
                "diff_hash": candidate.candidate_diff_hash,
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
    return relay, store, registry, action, candidate_task, proof_id


def test_reservation_spans_relay_commit_and_recovers_settlement_failure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay, store, registry, action, candidate_task, proof_id = (
        _reviewed_real_relay_candidate(tmp_path)
    )
    request = _integration_request(relay, action, candidate_task, proof_id)

    class FailingSettlementReservation:
        def __init__(self, delegate: RelayProofReservation) -> None:
            self._delegate = delegate

        @property
        def receipt(self) -> IntegrationProofReceipt:
            return self._delegate.receipt

        @property
        def fence(self) -> ProofFinalizationFence:
            return self._delegate.fence

        def settle(self, *, evidence: ProofFinalizationEvidence) -> str:
            del evidence
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")

    original_reserve = registry.reserve

    def fail_settlement(
        registered_proof_id: str, expectation: IntegrationExpectation
    ) -> FailingSettlementReservation:
        return FailingSettlementReservation(
            original_reserve(registered_proof_id, expectation)
        )

    monkeypatch.setattr(registry, "reserve", fail_settlement)
    with pytest.raises(RelayError) as first:
        relay.integrate(request)
    assert first.value.code == "RELAY_FINALIZATION_PENDING"
    first_status = store.status("relay-runtime-v3")
    first_run = first_status["run"]
    assert isinstance(first_run, dict)
    assert first_run["integration_version"] == 1

    monkeypatch.setattr(registry, "reserve", original_reserve)
    retried = relay.integrate(request)
    retried_task = retried["task"]
    assert isinstance(retried_task, dict)
    assert retried_task["state"] == "integrated"
    retry_status = store.status("relay-runtime-v3")
    retry_run = retry_status["run"]
    assert isinstance(retry_run, dict)
    assert retry_run["integration_version"] == 1
    replayed = relay.integrate(request)
    replayed_task = replayed["task"]
    assert isinstance(replayed_task, dict)
    assert replayed_task["state"] == "integrated"


def test_r4_committed_recovery_rolls_forward_and_consumes_once(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    registry = _registry(database_path, target)
    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)
    fence = reservation.fence

    assert _git_oid(repository, target.integration_ref) == fence.final_oid
    _git(
        repository,
        "update-ref",
        target.integration_ref,
        fence.base_oid,
        fence.final_oid,
    )

    restarted = _registry(database_path, target)
    authority = _TerminalFinalizationAuthority(committed=True)
    restarted.recover_finalizations(authority)
    restarted.recover_finalizations(authority)

    assert _git_oid(repository, target.integration_ref) == fence.final_oid
    with pytest.raises(IntegrationProofError) as replay:
        restarted.reserve(proof_id, expectation)
    assert replay.value.code == "RELAY_INTEGRATION_PROOF_REPLAY"


@pytest.mark.parametrize(
    ("committed", "ref_before_recovery", "expected_state", "expected_ref"),
    (
        (True, "base", "consumed", "final"),
        (True, "final", "consumed", "final"),
        (False, "base", "registered", "base"),
        (False, "final", "registered", "base"),
    ),
)
def test_r5_recovery_reaches_only_terminal_git_and_proof_pairs(
    tmp_path: Path,
    committed: bool,
    ref_before_recovery: str,
    expected_state: str,
    expected_ref: str,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    database_path = tmp_path / "relay-proof-registry.sqlite3"
    registry = _registry(database_path, target)
    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)
    fence = reservation.fence

    if ref_before_recovery == "base":
        _git(
            repository,
            "update-ref",
            target.integration_ref,
            fence.base_oid,
            fence.final_oid,
        )

    restarted = _registry(database_path, target)
    restarted.recover_finalizations(_TerminalFinalizationAuthority(committed=committed))

    with sqlite3.connect(database_path) as connection:
        state = connection.execute(
            "SELECT state FROM relay_host_integration_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
    assert state == (expected_state,)
    expected_oid = fence.final_oid if expected_ref == "final" else fence.base_oid
    assert _git_oid(repository, target.integration_ref) == expected_oid

    if expected_state == "registered":
        successor = restarted.reserve(proof_id, expectation)
        assert successor.fence.reservation_epoch == fence.reservation_epoch + 1
        assert (
            successor.settle(evidence=_aborted_evidence(successor.fence)) == "released"
        )


def test_r6_stale_epoch_cannot_settle_or_replace_successor_reservation(
    tmp_path: Path,
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)
    proof_id = registry.attest_and_register(expectation)

    stale = registry.reserve(proof_id, expectation)
    stale_fence = stale.fence
    assert stale.settle(evidence=_aborted_evidence(stale_fence)) == "released"

    successor = registry.reserve(proof_id, expectation)
    assert successor.fence.reservation_epoch == stale_fence.reservation_epoch + 1
    with pytest.raises(IntegrationProofError) as stale_settle:
        successor.settle(evidence=_committed_evidence(stale_fence))
    assert stale_settle.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"
    assert _git_oid(repository, target.integration_ref) == successor.fence.final_oid
    with pytest.raises(IntegrationProofError) as busy:
        registry.reserve(proof_id, expectation)
    assert busy.value.code == "RELAY_INTEGRATION_PROOF_BUSY"
    assert successor.settle(evidence=_aborted_evidence(successor.fence)) == "released"


@pytest.mark.parametrize("fault", ("wrong-fence", "third-oid", "symbolic", "missing"))
def test_r7_rejects_corrupt_evidence_and_ref_without_private_leakage(
    tmp_path: Path, fault: str
) -> None:
    expectation, repository = _real_git_expectation(tmp_path)
    target = _target(repository)
    registry = _registry(tmp_path / "relay-proof-registry.sqlite3", target)
    proof_id = registry.attest_and_register(expectation)
    reservation = registry.reserve(proof_id, expectation)
    fence = reservation.fence
    evidence = _committed_evidence(fence)

    if fault == "wrong-fence":
        evidence = replace(evidence, fence_hash="sha256:" + "f" * 64)
    elif fault == "third-oid":
        _git(
            repository,
            "update-ref",
            target.integration_ref,
            expectation.candidate_head_commit,
            fence.final_oid,
        )
    elif fault == "symbolic":
        _git(repository, "symbolic-ref", target.integration_ref, "refs/heads/main")
    else:
        _git(repository, "update-ref", "-d", target.integration_ref, fence.final_oid)

    with pytest.raises(IntegrationProofError) as rejected:
        reservation.settle(evidence=evidence)
    assert rejected.value.code == "RELAY_INTEGRATION_PROOF_CORRUPT"
    rendered = str(rejected.value)
    assert str(repository) not in rendered
    assert fence.base_oid not in rendered
    assert fence.final_oid not in rendered
