"""Relay v3 durable start, status, and refill behavior."""

from __future__ import annotations

import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.compiler import compile_plan
from devkit_relay.proofs import (
    IntegrationDeltaEntry,
    IntegrationExpectation,
    IntegrationProofError,
    IntegrationProofReceipt,
    IntegrationProofReservation,
    IntegrationProofResolver,
)
from devkit_relay.service import RelayError, RelayService
from devkit_relay.store import RelayStore

_BASE_COMMIT = "a" * 40
_INPUT_SNAPSHOT = "sha256:" + "b" * 64
_ATLAS_PACKET = "sha256:" + "c" * 64
_WORKSPACE_ID = "sha256:" + "d" * 64


class CompilerRegistryResolver:
    """Read-only compiler binding used by compiler-to-runtime closure tests."""

    def resolve(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        input_snapshot_id: str,
        atlas_packet_ids: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "workflow_id": workflow_id,
            "workspace_id": workspace_id,
            "input_snapshot_id": input_snapshot_id,
            "atlas_packet_ids": list(atlas_packet_ids),
            "current": True,
        }


def task(
    task_id: str,
    *,
    kind: str = "implementation",
    priority: int = 50,
    dependencies: list[str] | None = None,
    write_scope: list[dict[str, str]] | None = None,
    required_evidence: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build one canonical Relay v3 task for the runtime tests."""

    return {
        "task_id": task_id,
        "kind": kind,
        "title": f"{task_id} title",
        "objective": f"Complete the bounded {task_id} task.",
        "priority": priority,
        "dependencies": [] if dependencies is None else dependencies,
        "write_scope": [] if write_scope is None else write_scope,
        "route": {
            "route_class": "terra_max",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
        },
        "constraints": [
            {
                "code": "bounded",
                "detail": "Keep all work inside the declared contract.",
            }
        ],
        "acceptance_criteria": [
            {
                "criterion_id": f"{task_id}-accepted",
                "description": "The focused checks pass.",
            }
        ],
        "atlas_packet_ids": [_ATLAS_PACKET],
        "required_evidence": (
            [{"kind": "pytest", "selector": f"tests/{task_id}.py"}]
            if required_evidence is None
            else required_evidence
        ),
        "prewarm_for_task_id": None,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
    }


def _scopes_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    if left["path"] == right["path"]:
        return True
    if left["kind"] == "tree" and right["path"].startswith(left["path"] + "/"):
        return True
    return right["kind"] == "tree" and left["path"].startswith(right["path"] + "/")


def _compiler_conflicts(tasks: list[dict[str, object]]) -> list[dict[str, str]]:
    dependencies = {str(item["task_id"]): list(item["dependencies"]) for item in tasks}
    cache: dict[str, frozenset[str]] = {}

    def ancestors(task_id: str) -> frozenset[str]:
        if task_id not in cache:
            direct = dependencies[task_id]
            cache[task_id] = frozenset(
                {
                    *direct,
                    *(ancestor for item in direct for ancestor in ancestors(item)),
                }
            )
        return cache[task_id]

    writers = [item for item in tasks if item["kind"] == "implementation"]
    conflicts: list[dict[str, str]] = []
    for index, left in enumerate(writers):
        for right in writers[index + 1 :]:
            left_id = str(left["task_id"])
            right_id = str(right["task_id"])
            if left_id in ancestors(right_id) or right_id in ancestors(left_id):
                continue
            left_scopes = left["write_scope"]
            right_scopes = right["write_scope"]
            assert isinstance(left_scopes, list)
            assert isinstance(right_scopes, list)
            if not any(
                _scopes_overlap(first, second)
                for first in left_scopes
                for second in right_scopes
            ):
                continue
            if int(left["priority"]) != int(right["priority"]):
                blocker, blocked = (
                    (left, right)
                    if int(left["priority"]) > int(right["priority"])
                    else (right, left)
                )
            else:
                blocker, blocked = (
                    (left, right) if left_id < right_id else (right, left)
                )
            conflicts.append(
                {
                    "from_task_id": str(blocker["task_id"]),
                    "kind": "write_scope_conflict",
                    "to_task_id": str(blocked["task_id"]),
                }
            )
    return sorted(
        conflicts, key=lambda item: (item["from_task_id"], item["to_task_id"])
    )


def _rehash(value: dict[str, object]) -> dict[str, object]:
    value["plan_hash"] = canonical_hash(
        {key: item for key, item in value.items() if key != "plan_hash"}
    )
    return value


def plan(*raw_tasks: dict[str, object], capacity: int = 2) -> dict[str, object]:
    """Construct the immutable compiled-plan shape consumed by RelayService."""

    tasks = sorted(raw_tasks, key=lambda item: str(item["task_id"]))
    conflicts = _compiler_conflicts(tasks)
    candidates = [
        item for item in tasks if item["kind"] != "prewarm" and not item["dependencies"]
    ]
    candidate_ids = {str(item["task_id"]) for item in candidates}
    withheld = {
        item["to_task_id"]
        for item in conflicts
        if item["from_task_id"] in candidate_ids and item["to_task_id"] in candidate_ids
    }
    ready = sorted(
        (item for item in candidates if item["task_id"] not in withheld),
        key=lambda item: (-int(item["priority"]), str(item["task_id"])),
    )
    prewarms = sorted(
        (item for item in tasks if item["kind"] == "prewarm"),
        key=lambda item: (-int(item["priority"]), str(item["task_id"])),
    )
    body: dict[str, object] = {
        "schema": "2718lab-devkit/relay-plan-v1",
        "workflow_id": "relay-runtime-v3",
        "workspace_binding": {
            "workspace_id": _WORKSPACE_ID,
            "input_snapshot_id": _INPUT_SNAPSHOT,
            "atlas_packet_ids": [_ATLAS_PACKET],
        },
        "base_commit": _BASE_COMMIT,
        "capacity": capacity,
        "runtime_policy_id": "2718lab-devkit/relay-runtime-policy-v1",
        "tasks": tasks,
        "dependencies": sorted(
            (
                {
                    "from_task_id": str(item["task_id"]),
                    "kind": "depends_on",
                    "to_task_id": dependency,
                }
                for item in tasks
                for dependency in item["dependencies"]
            ),
            key=lambda item: (str(item["from_task_id"]), str(item["to_task_id"])),
        ),
        "conflicts": conflicts,
        "queues": {
            "prepared_prewarms": [item["task_id"] for item in prewarms],
            "ready": [item["task_id"] for item in ready],
            "running_slots": [],
            "review_integration": [],
            "terminal": [],
        },
    }
    return {**body, "plan_hash": canonical_hash(body)}


def _compiled_plan(raw_tasks: list[dict[str, object]]) -> dict[str, object]:
    return compile_plan(
        {
            "schema": "2718lab-devkit/relay-compile-request-v1",
            "workflow_id": "relay-runtime-v3",
            "workspace_id": _WORKSPACE_ID,
            "input_snapshot_id": _INPUT_SNAPSHOT,
            "base_commit": _BASE_COMMIT,
            "capacity": 3,
            "tasks": raw_tasks,
        },
        registry_resolver=CompilerRegistryResolver(),
    )


def _maximum_graph_plan(graph_kind: str) -> dict[str, object]:
    raw_tasks = []
    for index in range(64):
        task_id = f"graph-{index:02d}"
        dependencies = (
            [
                f"graph-{dependency:02d}"
                for dependency in range(max(0, index - 32), index)
            ]
            if graph_kind == "dependencies"
            else []
        )
        scope_path = (
            f"mcp-tools/graph-{index:02d}.py"
            if graph_kind == "dependencies"
            else "mcp-tools/shared.py"
        )
        raw_tasks.append(
            task(
                task_id,
                dependencies=dependencies,
                write_scope=[{"path": scope_path, "kind": "file"}],
            )
        )
    return _compiled_plan(raw_tasks)


class _RejectingProofResolver:
    def reserve(
        self, proof_id: str, expectation: IntegrationExpectation
    ) -> IntegrationProofReservation:
        del proof_id, expectation
        raise IntegrationProofError("RELAY_INTEGRATION_PROOF_UNREGISTERED")


class ProofRegistry:
    """Host-ledger double used by lifecycle and proof-boundary tests."""

    def __init__(self) -> None:
        self.receipts: dict[str, IntegrationProofReceipt] = {}
        self.states: dict[str, str] = {}
        self._held: dict[tuple[str, str, str], str] = {}
        self.fail_consume_once = False
        self.on_reserve: object = None

    def register(self, receipt: IntegrationProofReceipt) -> str:
        proof_id = receipt.proof_id
        if proof_id in self.receipts:
            raise AssertionError("duplicate test proof")
        self.receipts[proof_id] = receipt
        self.states[proof_id] = "registered"
        return proof_id

    def reserve(
        self, proof_id: str, expectation: IntegrationExpectation
    ) -> IntegrationProofReservation:
        receipt = self.receipts.get(proof_id)
        if receipt is None:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_UNREGISTERED")
        if receipt.expectation != expectation:
            raise IntegrationProofError("RELAY_INTEGRATION_BINDING_MISMATCH")
        state = self.states[proof_id]
        if state == "reserved":
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")
        recovery = state == "consumed"
        key = (
            expectation.workspace_id,
            receipt.repository_id,
            receipt.integration_ref,
        )
        owner = self._held.get(key)
        if owner is not None and owner != proof_id:
            raise IntegrationProofError("RELAY_INTEGRATION_PROOF_BUSY")
        if not recovery:
            self.states[proof_id] = "reserved"
            self._held[key] = proof_id
        callback = self.on_reserve
        if callable(callback):
            callback()
        return _ProofReservation(self, proof_id, key, recovery)


class _ProofReservation:
    def __init__(
        self,
        registry: ProofRegistry,
        proof_id: str,
        key: tuple[str, str, str],
        recovery: bool,
    ) -> None:
        self._registry = registry
        self._proof_id = proof_id
        self._key = key
        self._recovery = recovery

    @property
    def receipt(self) -> IntegrationProofReceipt:
        return self._registry.receipts[self._proof_id]

    def consume(self) -> None:
        if self._recovery:
            return
        if self._registry.fail_consume_once:
            self._registry.fail_consume_once = False
            raise IntegrationProofError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")
        self._registry.states[self._proof_id] = "consumed"
        self._registry._held.pop(self._key, None)

    def release(self) -> None:
        if self._recovery:
            return
        if self._registry.states.get(self._proof_id) == "reserved":
            self._registry.states[self._proof_id] = "registered"
        self._registry._held.pop(self._key, None)


def synthetic_integration_receipt(
    expectation: IntegrationExpectation,
    *,
    final_commit: str | None = None,
    delta_path: str | None = None,
) -> IntegrationProofReceipt:
    object_length = len(expectation.predecessor_integration_head)
    object_format = "sha1" if object_length == 40 else "sha256"
    scope = expectation.write_scope[0]
    path = delta_path or (
        scope.path if scope.kind == "file" else f"{scope.path}/integration-proof.txt"
    )
    delta = (
        IntegrationDeltaEntry(
            path=path,
            old_oid="5" * object_length,
            new_oid="6" * object_length,
            old_mode="100644",
            new_mode="100644",
            old_type="blob",
            new_type="blob",
        ),
    )
    return IntegrationProofReceipt.create(
        expectation=expectation,
        object_format=object_format,
        repository_id="sha256:" + "9" * 64,
        integration_ref="refs/heads/main",
        predecessor_commit=expectation.predecessor_integration_head,
        candidate_head_commit=expectation.candidate_head_commit,
        candidate_commits=(expectation.candidate_head_commit,),
        final_commit=final_commit or "2" * object_length,
        predecessor_tree="3" * object_length,
        candidate_tree="4" * object_length,
        final_tree="4" * object_length,
        final_parent_commit=expectation.predecessor_integration_head,
        ref_before_commit=expectation.predecessor_integration_head,
        ref_after_commit=final_commit or "2" * object_length,
        candidate_delta=delta,
        final_delta=delta,
        merge_free=True,
        linear_ancestry=True,
        attestor_id="host-git",
        attestor_version="1.0",
    )


def service(
    tmp_path: Path,
    integration_proof_resolver: IntegrationProofResolver | None = None,
) -> tuple[RelayService, RelayStore]:
    store = RelayStore(tmp_path / "relay.sqlite3")
    resolver = (
        _RejectingProofResolver()
        if integration_proof_resolver is None
        else integration_proof_resolver
    )
    return (
        RelayService(
            store,
            capability_secret=b"relay-v3-test-secret",
            integration_proof_resolver=resolver,
        ),
        store,
    )


def issue_worker(
    relay: RelayService,
    action: dict[str, object],
    *,
    lifecycle_action: str,
    endpoint: str = "worker-a",
) -> str:
    lease = action["lease"]
    assert isinstance(lease, dict)
    return relay.issue_worker_capability(
        workflow_id="relay-runtime-v3",
        task_id=str(action["task_id"]),
        action=lifecycle_action,
        epoch=int(lease["epoch"]),
        endpoint=endpoint,
    )


def worker_request(
    action: dict[str, object],
    *,
    lifecycle_action: str,
    capability: str,
    expected_task_version: int,
    endpoint: str = "worker-a",
    **extra: object,
) -> dict[str, object]:
    lease = action["lease"]
    assert isinstance(lease, dict)
    return {
        "workflow_id": "relay-runtime-v3",
        "task_id": action["task_id"],
        "action": lifecycle_action,
        "epoch": lease["epoch"],
        "endpoint": endpoint,
        "expected_task_version": expected_task_version,
        "capability": capability,
        **extra,
    }


def bind_worker(relay: RelayService, action: dict[str, object]) -> int:
    lease = action["lease"]
    assert isinstance(lease, dict)
    result = relay.handoff(
        worker_request(
            action,
            lifecycle_action="bind_endpoint",
            capability=issue_worker(relay, action, lifecycle_action="bind_endpoint"),
            expected_task_version=int(lease["task_version"]),
        )
    )
    task_data = result["task"]
    assert isinstance(task_data, dict)
    return int(task_data["task_version"])


def _capacity_plan(capacity: int) -> dict[str, object]:
    return plan(
        *(
            task(
                f"writer-{index}",
                priority=100 - index,
                write_scope=[{"path": f"mcp-tools/writer-{index}.py", "kind": "file"}],
            )
            for index in range(capacity)
        ),
        capacity=capacity,
    )


@pytest.mark.parametrize("capacity", [1, 2, 3])
def test_start_accepts_supported_host_child_capacity(
    tmp_path: Path, capacity: int
) -> None:
    relay, _store = service(tmp_path)

    created = relay.start(
        {
            "mode": "create",
            "plan": _capacity_plan(capacity),
            "idempotency_key": f"accepted-capacity-{capacity}",
        }
    )

    actions = created["host_actions"]
    assert isinstance(actions, list)
    assert [action["task_id"] for action in actions] == [
        f"writer-{index}" for index in range(capacity)
    ]


@pytest.mark.parametrize("capacity", [4, 8])
def test_start_rejects_self_hashed_over_capacity_plan_before_persistence(
    tmp_path: Path, capacity: int
) -> None:
    relay, store = service(tmp_path)
    submitted = _capacity_plan(capacity)
    before = store.database_fingerprint()

    with pytest.raises(RelayError) as caught:
        relay.start(
            {
                "mode": "create",
                "plan": submitted,
                "idempotency_key": f"rejected-capacity-{capacity}",
            }
        )

    assert caught.value.code == "RELAY_PLAN_INVALID"
    assert str(caught.value) == "RELAY_PLAN_INVALID"
    assert store.database_fingerprint() == before


@pytest.mark.parametrize("line_break", ["\r", "\n"])
@pytest.mark.parametrize(
    ("field", "member"),
    [
        ("title", None),
        ("objective", None),
        ("constraints", "detail"),
        ("acceptance_criteria", "description"),
        ("required_evidence", "selector"),
    ],
)
def test_start_rejects_self_hashed_line_break_text_before_persistence(
    tmp_path: Path, line_break: str, field: str, member: str | None
) -> None:
    relay, store = service(tmp_path)
    submitted = plan(
        task(
            "writer-text",
            write_scope=[{"path": "mcp-tools/writer-text.py", "kind": "file"}],
        ),
        capacity=1,
    )
    tasks = submitted["tasks"]
    assert isinstance(tasks, list)
    writer = tasks[0]
    assert isinstance(writer, dict)
    if member is None:
        writer[field] = f"safe{line_break}forged"
    else:
        entries = writer[field]
        assert isinstance(entries, list)
        entry = entries[0]
        assert isinstance(entry, dict)
        entry[member] = f"safe{line_break}forged"
    _rehash(submitted)
    before = store.database_fingerprint()

    with pytest.raises(RelayError) as caught:
        relay.start(
            {
                "mode": "create",
                "plan": submitted,
                "idempotency_key": f"line-break-{field}-{ord(line_break)}",
            }
        )

    assert caught.value.code == "RELAY_PLAN_INVALID"
    assert str(caught.value) == "RELAY_PLAN_INVALID"
    assert store.database_fingerprint() == before


@pytest.mark.parametrize(
    ("graph_kind", "edge_field", "edge_count"),
    [
        ("dependencies", "dependencies", 1_520),
        ("conflicts", "conflicts", 2_016),
    ],
)
def test_start_accepts_compiler_maximum_legal_graph(
    tmp_path: Path, graph_kind: str, edge_field: str, edge_count: int
) -> None:
    relay, _store = service(tmp_path)
    compiled = _maximum_graph_plan(graph_kind)
    edges = compiled[edge_field]
    assert isinstance(edges, list)
    assert len(edges) == edge_count

    created = relay.start(
        {
            "mode": "create",
            "plan": compiled,
            "idempotency_key": f"maximum-{graph_kind}",
        }
    )

    actions = created["host_actions"]
    assert isinstance(actions, list)
    assert [action["task_id"] for action in actions] == ["graph-00"]


@pytest.mark.parametrize(
    ("graph_kind", "edge_field", "edge_count"),
    [
        ("dependencies", "dependencies", 1_521),
        ("conflicts", "conflicts", 2_017),
    ],
)
def test_start_rejects_self_hashed_one_over_maximum_graph_before_persistence(
    tmp_path: Path, graph_kind: str, edge_field: str, edge_count: int
) -> None:
    relay, store = service(tmp_path)
    forged = deepcopy(_maximum_graph_plan(graph_kind))
    edges = forged[edge_field]
    assert isinstance(edges, list)
    edges.append(deepcopy(edges[-1]))
    assert len(edges) == edge_count
    _rehash(forged)
    before = store.database_fingerprint()

    with pytest.raises(RelayError) as caught:
        relay.start(
            {
                "mode": "create",
                "plan": forged,
                "idempotency_key": f"one-over-{graph_kind}",
            }
        )

    assert caught.value.code == "RELAY_PLAN_INVALID"
    assert store.database_fingerprint() == before


@pytest.mark.parametrize("invalid_capacity", [0, 4, 1.5, "invalid"])
def test_store_schema_rejects_noninteger_or_out_of_range_capacity(
    tmp_path: Path, invalid_capacity: object
) -> None:
    database = tmp_path / "relay.sqlite3"
    relay, store = service(tmp_path)
    relay.start(
        {
            "mode": "create",
            "plan": _capacity_plan(1),
            "idempotency_key": "capacity-schema",
        }
    )
    before = store.database_fingerprint()

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE relay_v3_runs SET capacity = ?", (invalid_capacity,)
            )

    assert store.database_fingerprint() == before


@pytest.mark.parametrize("invalid_capacity", [0, 4, 1.5, "invalid"])
def test_status_rejects_invalid_stored_capacity_as_storage_error(
    tmp_path: Path, invalid_capacity: object
) -> None:
    database = tmp_path / "relay.sqlite3"
    relay, store = service(tmp_path)
    relay.start(
        {
            "mode": "create",
            "plan": _capacity_plan(1),
            "idempotency_key": "capacity-tamper",
        }
    )
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE relay_v3_runs SET capacity = ?", (invalid_capacity,))

    tampered_relay, _tampered_store = service(tmp_path)
    with pytest.raises(RelayError) as caught:
        tampered_relay.status("relay-runtime-v3")

    assert caught.value.code == "RELAY_STORAGE_ERROR"
    assert str(caught.value) == "RELAY_STORAGE_ERROR"


def test_start_is_idempotent_and_status_is_read_only(tmp_path: Path) -> None:
    relay, store = service(tmp_path)
    compiled = plan(
        task(
            "writer-a",
            priority=100,
            write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
        ),
        task(
            "writer-b",
            priority=50,
            write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
        ),
    )

    created = relay.start_create(compiled, idempotency_key="create-runtime")

    assert relay.start_create(compiled, idempotency_key="create-runtime") == created
    actions = created["host_actions"]
    assert isinstance(actions, list)
    assert [action["task_id"] for action in actions] == ["writer-a", "writer-b"]
    for action in actions:
        assert action["kind"] == "codex.spawn_agent"
        assert "capability" not in action
        assert "prompt" not in action
        assert "D:\\" not in repr(action)
        assert "C:\\" not in repr(action)

    before = store.database_fingerprint()
    status = relay.status("relay-runtime-v3")
    after = store.database_fingerprint()

    assert before == after
    assert status["schedule_version"] == created["schedule_version"]
    assert [item["task_id"] for item in status["queues"]["running_slots"]] == [
        "writer-a",
        "writer-b",
    ]

    changed = dict(compiled)
    changed["capacity"] = 1
    _rehash(changed)
    with pytest.raises(RelayError, match="RELAY_IDEMPOTENCY_CONFLICT"):
        relay.start_create(changed, idempotency_key="create-runtime")


def test_terminal_event_releases_slot_and_persists_refill_in_same_transition(
    tmp_path: Path,
) -> None:
    relay, store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                priority=100,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-b",
                priority=50,
                write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
            ),
            capacity=1,
        ),
        idempotency_key="create-terminal",
    )
    first = created["host_actions"][0]
    assert isinstance(first, dict)
    version = bind_worker(relay, first)

    terminal = relay.handoff(
        worker_request(
            first,
            lifecycle_action="terminal",
            capability=issue_worker(relay, first, lifecycle_action="terminal"),
            expected_task_version=version,
            outcome="blocked",
        )
    )

    assert terminal["schedule_version"] == created["schedule_version"] + 1
    status = relay.status("relay-runtime-v3")
    assert [item["task_id"] for item in status["queues"]["terminal"]] == ["writer-a"]
    directive = status["refill_directives"][0]
    assert directive["task_id"] == "writer-b"
    assert directive["expected_schedule_version"] == terminal["schedule_version"]

    refilled = relay.start_refill(
        "relay-runtime-v3",
        str(directive["directive_id"]),
        expected_schedule_version=int(directive["expected_schedule_version"]),
        idempotency_key="refill-writer-b",
    )
    assert [action["task_id"] for action in refilled["host_actions"]] == ["writer-b"]
    assert store.database_fingerprint() != ""


def test_start_accepts_only_registry_opaque_workspace_ids(tmp_path: Path) -> None:
    relay, _store = service(tmp_path)
    compiled = plan(
        task(
            "writer-a",
            write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
        )
    )

    assert (
        relay.start_create(compiled, idempotency_key="opaque-workspace")["workflow_id"]
        == "relay-runtime-v3"
    )

    nonopaque = deepcopy(compiled)
    binding = nonopaque["workspace_binding"]
    assert isinstance(binding, dict)
    binding["workspace_id"] = "workspace-main"
    _rehash(nonopaque)
    with pytest.raises(RelayError, match="RELAY_PLAN_INVALID"):
        relay.start_create(nonopaque, idempotency_key="nonopaque-workspace")


def test_start_rejects_self_hashed_noncanonical_compiler_projection(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    canonical = plan(
        task(
            "writer-root",
            priority=100,
            write_scope=[{"path": "mcp-tools", "kind": "tree"}],
        ),
        task(
            "writer-overlap",
            priority=90,
            write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
        ),
        task(
            "writer-child",
            dependencies=["writer-root"],
            write_scope=[{"path": "mcp-tools/child.py", "kind": "file"}],
        ),
    )

    unknown_dependency = deepcopy(canonical)
    tasks = unknown_dependency["tasks"]
    assert isinstance(tasks, list)
    child = next(item for item in tasks if item["task_id"] == "writer-child")
    child["dependencies"] = ["missing-task"]
    unknown_dependency["dependencies"] = [
        {
            "from_task_id": "writer-child",
            "kind": "depends_on",
            "to_task_id": "missing-task",
        }
    ]
    _rehash(unknown_dependency)

    cycle = deepcopy(canonical)
    cycle_tasks = cycle["tasks"]
    assert isinstance(cycle_tasks, list)
    root = next(item for item in cycle_tasks if item["task_id"] == "writer-root")
    child = next(item for item in cycle_tasks if item["task_id"] == "writer-child")
    root["dependencies"] = ["writer-child"]
    child["dependencies"] = ["writer-root"]
    cycle["dependencies"] = [
        {
            "from_task_id": "writer-child",
            "kind": "depends_on",
            "to_task_id": "writer-root",
        },
        {
            "from_task_id": "writer-root",
            "kind": "depends_on",
            "to_task_id": "writer-child",
        },
    ]
    cycle["queues"]["ready"] = []
    _rehash(cycle)

    mismatched_edges = deepcopy(canonical)
    mismatched_edges["dependencies"] = []
    _rehash(mismatched_edges)

    missing_conflict = deepcopy(canonical)
    missing_conflict["conflicts"] = []
    _rehash(missing_conflict)

    wrong_packets = deepcopy(canonical)
    packet_binding = wrong_packets["workspace_binding"]
    assert isinstance(packet_binding, dict)
    packet_binding["atlas_packet_ids"] = []
    _rehash(wrong_packets)

    omitted_ready = deepcopy(canonical)
    omitted_ready["queues"]["ready"] = []
    _rehash(omitted_ready)

    prewarm = task("prewarm", kind="prewarm")
    prewarm["prewarm_for_task_id"] = "writer-root"
    prewarm_dependency = plan(
        prewarm,
        task(
            "writer-root",
            dependencies=["prewarm"],
            write_scope=[{"path": "mcp-tools/root.py", "kind": "file"}],
        ),
    )

    for index, malformed in enumerate(
        (
            unknown_dependency,
            cycle,
            mismatched_edges,
            missing_conflict,
            wrong_packets,
            omitted_ready,
            prewarm_dependency,
        )
    ):
        with pytest.raises(RelayError, match="RELAY_PLAN_INVALID"):
            relay.start_create(malformed, idempotency_key=f"projection-{index}")
