"""Tests for the fastlane TODO projector/state machine."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / ".codex-plugin" / "fastlane_todo_projection.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "fastlane_todo_projection", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fastlane_todo_projection"] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _projector(tmp_path: Path, clock: FakeClock | None = None):
    return mod.FastlaneTodoProjector(
        tmp_path / "fastlane-data", clock=clock or FakeClock(0.0)
    )


def _source(
    *,
    workflow_id: str = "workflow-1",
    workflow_state: str = "running",
    workflow_version: int = 1,
    tasks: list[tuple[str, str, int, str]] | None = None,
    fastlane: dict | None = None,
) -> str:
    payload = {
        "schema": mod.SOURCE_SCHEMA if fastlane is None else mod.SOURCE_SCHEMA_V2,
        "workflow": {
            "id": workflow_id,
            "state": workflow_state,
            "version": workflow_version,
        },
        "tasks": [
            {
                "id": task_id,
                "title": title,
                "state": state,
                "version": version,
            }
            for task_id, state, version, title in tasks or []
        ],
    }
    if fastlane is not None:
        payload["fastlane"] = fastlane
    return json.dumps(
        payload,
        ensure_ascii=False,
    )


def _fastlane(
    *,
    recovery: list[dict] | None = None,
    routes: list[dict] | None = None,
) -> dict:
    return {
        "schema": mod.FASTLANE_METADATA_SCHEMA,
        "metrics": {
            "queue_delay_ms": 0,
            "useful_slot_occupancy_permille": 0,
            "prewarm_yield_count": 0,
            "recovery_count": 0,
            "rerun_avoidance_count": 0,
            "cache_pressure_permille": 0,
        },
        "routes": routes or [],
        "recovery": recovery or [],
    }


def _route_metadata(
    *, fingerprint: str = "a", reason: str = "score_luna_medium"
) -> dict:
    return {
        "task_id": "task-1",
        "task_fingerprint": "sha256:" + (fingerprint * 64),
        "route_reason_codes": [reason],
        "floor_reason_codes": ["floor_role"],
    }


def _accept_delta(
    projector,
    payload: str,
    clock: FakeClock,
    *,
    first: float,
    second: float,
) -> dict:
    clock.value = first
    first_event = projector.observe(payload, now=clock())
    assert first_event["kind"] == "deferred"
    clock.value = second
    second_event = projector.observe(payload, now=clock())
    assert second_event["kind"] == "delta"
    return second_event["delta"]


def _state_file(tmp_root: Path, workflow_id: str = "workflow-1") -> Path:
    return tmp_root / "fastlane-data" / "fastlane-todo-v1" / workflow_id / "state.json"


def _pending_delta_file(tmp_root: Path, workflow_id: str = "workflow-1") -> Path:
    return (
        tmp_root
        / "fastlane-data"
        / "fastlane-todo-v1"
        / workflow_id
        / "pending-delta.json"
    )


def test_reject_unknown_source_fields(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    payload = json.loads(_source(tasks=[("task-1", "new", 1, "a")]))
    payload["unexpected"] = True
    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(json.dumps(payload, ensure_ascii=False))
    assert exc.value.code == "INVALID_SOURCE"


def test_reject_unknown_task_fields(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    payload = json.loads(_source(tasks=[("task-1", "new", 1, "a")]))
    payload["tasks"][0]["unexpected"] = "x"
    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(json.dumps(payload, ensure_ascii=False))
    assert exc.value.code == "INVALID_SOURCE"


def test_reject_too_many_tasks(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    payload = _source(
        tasks=[(f"task-{index}", "new", 1, "task") for index in range(65)]
    )
    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(payload)
    assert exc.value.code == "INVALID_SOURCE"


def test_version_collision_task_removal_and_terminal_mutation_rejected(
    tmp_path: Path,
) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)

    delta = _accept_delta(
        projector,
        _source(workflow_state="new", tasks=[("task-1", "new", 1, "initial")]),
        clock,
        first=0.0,
        second=0.3,
    )
    projector.ack("workflow-1", delta["delta_id"])

    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(
            _source(
                workflow_state="running",
                workflow_version=1,
                tasks=[("task-1", "ready", 1, "initial")],
            )
        )
    assert exc.value.code == "INVALID_TRANSITION"

    done_delta = _accept_delta(
        projector,
        _source(
            workflow_state="running",
            workflow_version=2,
            tasks=[("task-1", "done", 2, "initial")],
        ),
        clock,
        first=0.3,
        second=0.6,
    )
    projector.ack("workflow-1", done_delta["delta_id"])

    with pytest.raises(mod.FastlaneTodoError):
        projector.observe(
            _source(
                workflow_state="running",
                workflow_version=3,
                tasks=[("task-1", "running", 3, "initial")],
            )
        )

    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(
            _source(
                workflow_state="running",
                workflow_version=3,
                tasks=[],
            )
        )
    assert exc.value.code == "INVALID_TRANSITION"


def test_task_transition_reachability_skips_allowed_states(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    delta = _accept_delta(
        projector,
        _source(workflow_state="new", tasks=[("task-1", "new", 1, "a")]),
        clock,
        first=0.0,
        second=0.3,
    )
    projector.ack("workflow-1", delta["delta_id"])

    done_delta = _accept_delta(
        projector,
        _source(
            workflow_state="running",
            workflow_version=2,
            tasks=[("task-1", "done", 2, "a")],
        ),
        clock,
        first=0.4,
        second=0.7,
    )
    assert done_delta is not None


def test_metadata_only_changes_do_not_emit_delta(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    delta = _accept_delta(
        projector,
        _source(
            workflow_state="running",
            tasks=[("task-1", "new", 1, "Alpha\nline")],
        ),
        clock,
        first=0.0,
        second=0.3,
    )
    projector.ack("workflow-1", delta["delta_id"])

    event = projector.observe(
        _source(
            workflow_version=2,
            workflow_state="running",
            tasks=[
                ("task-1", "new", 2, "Beta"),
            ],
        ),
        now=0.6,
    )
    assert event["kind"] == "noop"


def test_v1_source_fingerprint_remains_compatible_when_recovery_is_absent() -> None:
    snapshot = mod._parse_source(
        _source(
            workflow_id="workflow-1",
            workflow_state="running",
            tasks=[("task-1", "running", 1, "one")],
        )
    )
    legacy_fingerprint = mod._sha256_hex(
        mod._canonical_json(
            {
                "workflow_id": "workflow-1",
                "workflow_state": "running",
                "tasks": [{"id": "task-1", "state": "running"}],
            }
        )
    )

    assert snapshot.fingerprint == legacy_fingerprint


def test_v2_route_metadata_is_bounded_redacted_and_never_causes_token_churn(
    tmp_path: Path,
) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    first = _source(
        tasks=[("task-1", "running", 1, "one")],
        fastlane=_fastlane(routes=[_route_metadata(fingerprint="a")]),
    )
    _accept_delta(projector, first, clock, first=0.0, second=0.3)
    pending = projector.recover("workflow-1")
    assert pending["kind"] == "delta"
    projector.ack("workflow-1", pending["delta"]["delta_id"])

    changed_metadata = _source(
        workflow_version=2,
        tasks=[("task-1", "running", 2, "one")],
        fastlane=_fastlane(
            routes=[_route_metadata(fingerprint="b", reason="score_luna_high")]
        ),
    )

    event = projector.observe(changed_metadata, now=0.6)

    assert event["kind"] == "noop"


def test_v2_recovery_transitions_survive_replay_and_ack_idempotently(
    tmp_path: Path,
) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    recovery_states = [
        "transport_degraded",
        "recovery_probe",
        "resumed",
        "fenced_replacement",
    ]
    previous = "none"
    for index, recovery_state in enumerate(recovery_states, start=1):
        source = _source(
            workflow_version=index,
            tasks=[("task-1", "running", index, "one")],
            fastlane=_fastlane(
                recovery=[{"task_id": "task-1", "state": recovery_state}],
                routes=[_route_metadata()],
            ),
        )
        deferred = projector.observe(source, now=clock.value)
        assert deferred["kind"] == "deferred"
        clock.value += 0.3
        event = projector.observe(source, now=clock.value)
        assert event["kind"] == "delta"
        transitions = event["delta"]["transitions"]
        assert {
            "kind": "recovery",
            "id": "task-1",
            "from_state": previous,
            "to_state": recovery_state,
        } in transitions

        restarted = _projector(tmp_path, clock)
        replay = restarted.recover("workflow-1")
        assert replay == event
        assert restarted.ack("workflow-1", event["delta"]["delta_id"])["kind"] == "noop"
        assert restarted.recover("workflow-1")["kind"] == "noop"
        projector = restarted
        previous = recovery_state


def test_v2_fastlane_metadata_rejects_raw_or_unknown_route_content(
    tmp_path: Path,
) -> None:
    projector = _projector(tmp_path)
    unsafe_route = _route_metadata()
    unsafe_route["route_reason_codes"] = [r"C:\secrets\token"]
    source = _source(
        tasks=[("task-1", "running", 1, "one")],
        fastlane=_fastlane(routes=[unsafe_route]),
    )

    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(source)

    assert exc.value.code == "INVALID_SOURCE"


def test_plan_has_exact_buckets_and_one_in_progress(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    tasks = [
        ("a", "blocked", 1, "blocked-task"),
        ("b", "failed", 1, "failed-task"),
        ("c", "running", 1, "running"),
        ("d", "verifying", 1, "verify"),
        ("e", "ready", 1, "ready"),
        ("f", "new", 1, "new-1"),
        ("g", "new", 1, "new-2"),
        ("h", "new", 1, "new-3"),
        ("i", "new", 1, "new-4"),
        ("j", "new", 1, "new-5"),
        ("k", "new", 1, "new-6"),
        ("l", "new", 1, "new-7"),
        ("m", "new", 1, "new-8"),
        ("n", "new", 1, "new-9"),
        ("o", "done", 1, "done"),
        ("p", "cancelled", 1, "closed"),
    ]
    delta = _accept_delta(
        projector,
        _source(workflow_state="running", tasks=tasks),
        clock,
        first=0.0,
        second=0.3,
    )

    steps = delta["plan"]
    assert len(steps) == 6
    assert [step["status"] for step in steps] == [
        "pending",
        "in_progress",
        "pending",
        "pending",
        "completed",
        "completed",
    ]
    assert sum(step["status"] == "in_progress" for step in steps) == 1
    assert any(step["step"].startswith("attention:") for step in steps)
    assert any(step["step"].startswith("active:") for step in steps)
    assert any(step["step"].startswith("ready:") for step in steps)
    assert any(step["step"].startswith("queued:") for step in steps)
    assert any(step["step"].startswith("done:") for step in steps)
    assert any(step["step"].startswith("closed:") for step in steps)
    assert "+1 omitted [sha256:" in steps[3]["step"]


def test_debounce_trailing_250ms_and_1s_cap(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)

    event1 = projector.observe(_source(tasks=[("task-1", "new", 1, "t")]), now=0.0)
    assert event1["kind"] == "deferred"
    event2 = projector.observe(_source(tasks=[("task-1", "running", 2, "t")]), now=0.2)
    assert event2["kind"] == "deferred"
    event3 = projector.observe(
        _source(tasks=[("task-1", "verifying", 3, "t")]), now=0.8
    )
    assert event3["kind"] == "deferred"
    event4 = projector.observe(_source(tasks=[("task-1", "done", 4, "t")]), now=1.1)
    assert event4["kind"] == "delta"


def test_recover_replays_pending_and_emits_observed_vs_acked(tmp_path: Path) -> None:
    projector = _projector(tmp_path)

    first = projector.observe(
        _source(tasks=[("task-1", "new", 1, "x")], workflow_state="running"), now=0.0
    )
    assert first["kind"] == "deferred"
    pending = projector.observe(
        _source(tasks=[("task-1", "new", 1, "x")], workflow_state="running"), now=0.3
    )
    assert pending["kind"] == "delta"
    pending_id = pending["delta"]["delta_id"]

    recovered_pending = projector.recover("workflow-1")
    assert recovered_pending["kind"] == "delta"
    assert recovered_pending["delta"]["delta_id"] == pending_id

    projector.ack("workflow-1", pending_id)

    projector.observe(
        _source(
            workflow_version=2,
            workflow_state="running",
            tasks=[("task-1", "running", 2, "x")],
        ),
        now=0.6,
    )
    assert projector.recover("workflow-1")["kind"] == "delta"


def test_unacked_pending_preserves_newest_observed_and_recover_follows_up(
    tmp_path: Path,
) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)

    first = _accept_delta(
        projector,
        _source(workflow_state="running", tasks=[("task-1", "new", 1, "x")]),
        clock,
        first=0.0,
        second=0.3,
    )
    pending_id = first["delta_id"]
    assert pending_id.startswith("sha256:")

    state_after_a = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    observed_fingerprint_a = state_after_a["observed"]["fingerprint"]
    pending_delta_to_fingerprint = state_after_a["pending_delta"]["to_fingerprint"]

    newer = projector.observe(
        _source(
            workflow_state="running",
            workflow_version=2,
            tasks=[("task-1", "running", 2, "y")],
        ),
        now=0.6,
    )
    assert newer["kind"] == "deferred"

    state_after_b = json.loads(_state_file(tmp_path).read_text(encoding="utf-8"))
    assert state_after_b["observed"]["fingerprint"] == observed_fingerprint_a
    assert "staged_observed" in state_after_b
    assert state_after_b["staged_observed"]["fingerprint"] != observed_fingerprint_a
    assert state_after_b["observed"]["workflow_version"] == 1
    assert state_after_b["staged_observed"]["workflow_version"] == 2
    assert state_after_b["pending_delta"]["delta_id"] == pending_id
    assert (
        state_after_b["pending_delta"]["to_fingerprint"] == pending_delta_to_fingerprint
    )
    assert state_after_b["pending_delta"]["from_fingerprint"] == ""

    pending_file = json.loads(_pending_delta_file(tmp_path).read_text(encoding="utf-8"))
    assert pending_file["delta_id"] == pending_id

    restarted = _projector(tmp_path, clock)
    recovered_a = restarted.recover("workflow-1")
    assert recovered_a["kind"] == "delta"
    assert recovered_a["delta"]["delta_id"] == pending_id

    assert restarted.ack("workflow-1", pending_id)["kind"] == "noop"

    follow = restarted.recover("workflow-1")
    assert follow["kind"] == "delta"
    follow_id = follow["delta"]["delta_id"]
    assert follow["delta"]["from_fingerprint"] == pending_delta_to_fingerprint
    assert (
        follow["delta"]["to_fingerprint"]
        == state_after_b["staged_observed"]["fingerprint"]
    )

    with pytest.raises(mod.FastlaneTodoError) as exc:
        restarted.ack("workflow-1", pending_id)
    assert exc.value.code == "ACK_UNKNOWN"

    assert restarted.ack("workflow-1", follow_id)["kind"] == "noop"
    pending_file_after_follow = _pending_delta_file(tmp_path)
    assert not pending_file_after_follow.exists()

    state_path_ids = {
        state_payload["pending_delta"]["delta_id"]
        for state_payload in [state_after_a, state_after_b]
    }
    assert len(state_path_ids) == 1


def test_ack_stale_and_unknown_ids(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    delta = _accept_delta(
        projector,
        _source(tasks=[("task-1", "new", 1, "x")]),
        FakeClock(0.0),
        first=0.0,
        second=0.3,
    )
    bad_delta = "sha256:" + ("0" * 64)
    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.ack("workflow-1", bad_delta)
    assert exc.value.code == "ACK_UNKNOWN"

    projector.ack("workflow-1", delta["delta_id"])
    assert projector.ack("workflow-1", delta["delta_id"])["kind"] == "noop"

    _accept_delta(
        projector,
        _source(tasks=[("task-1", "running", 2, "x")], workflow_state="running"),
        FakeClock(0.3),
        first=0.6,
        second=0.9,
    )
    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.ack("workflow-1", "sha256:" + ("1" * 64))
    assert exc.value.code == "ACK_UNKNOWN"


def test_concurrent_observe_is_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    source = _source(tasks=[("task-1", "new", 1, "x")], workflow_state="running")
    inside = []
    overlap = threading.Event()
    lock_depth = {"value": 0}

    original_lock = mod._acquire_lock

    @contextmanager
    def tracing_lock(path: Path):
        with original_lock(path):
            lock_depth["value"] += 1
            inside.append(threading.current_thread().name)
            if lock_depth["value"] > 1:
                overlap.set()
            try:
                yield
            finally:
                lock_depth["value"] -= 1
                inside.remove(threading.current_thread().name)

    monkeypatch.setattr(mod, "_acquire_lock", tracing_lock)
    try:

        def worker() -> None:
            for index in range(4):
                projector.observe(source, now=clock.value + index * 0.01)

        threads = [
            threading.Thread(target=worker, name=f"worker-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        monkeypatch.setattr(mod, "_acquire_lock", original_lock)

    assert not overlap.is_set()


def test_corrupt_state_is_fastlane_state_corrupt(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    payload = _source(tasks=[("task-1", "new", 1, "x")])
    projector.observe(payload, now=0.0)
    state_path = _state_file(tmp_path)
    state_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(mod.FastlaneTodoError) as exc:
        projector.observe(payload, now=0.3)
    assert exc.value.code == "FASTLANE_STATE_CORRUPT"


def test_recovery_is_idempotent_after_atomic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(0.0)
    projector = _projector(tmp_path, clock)
    first = _accept_delta(
        projector,
        _source(tasks=[("task-1", "new", 1, "first")]),
        clock,
        first=0.0,
        second=0.3,
    )
    projector.ack("workflow-1", first["delta_id"])

    real_write = mod._write_json_atomically
    calls = {"count": 0}

    def flaky_write(path: Path, payload: object) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash")
        real_write(path, payload)

    monkeypatch.setattr(mod, "_write_json_atomically", flaky_write)
    first_update = _source(
        workflow_version=2,
        workflow_state="running",
        tasks=[("task-1", "running", 2, "first")],
    )
    projector.observe(first_update, now=0.6)
    with pytest.raises(RuntimeError):
        projector.observe(first_update, now=0.9)

    recovered = projector.recover("workflow-1")
    assert recovered["kind"] == "delta"


def test_no_direct_host_or_network_calls_in_source() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_modules = {
        "subprocess",
        "socket",
        "requests",
        "http",
        "httpx",
        "urllib",
        "mcp",
    }
    forbidden_calls = {"update_plan"}
    imports: set[str] = set()
    calls: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                imports.add(name)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module.split(".")[0])
        if isinstance(node, ast.Name):
            calls.add(node.id)
        if isinstance(node, ast.Attribute):
            calls.add(node.attr)

    for item in forbidden_modules:
        assert item not in imports
    for item in forbidden_calls:
        assert item not in calls


def test_invalid_cli_source_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "cli-data"
    fake_stdin = io.StringIO(_source(tasks=[("task-1", "new", 1, "x")]))
    original_stdin = sys.stdin
    fake_stdout = io.StringIO()
    original_stdout = sys.stdout
    try:
        sys.stdin = fake_stdin
        sys.stdout = fake_stdout
        code = mod.main(["--data-root", str(data_root), "observe"])
    finally:
        captured = fake_stdout.getvalue()
        sys.stdin = original_stdin
        sys.stdout = original_stdout

    event = json.loads(captured or "{}")
    assert code == 0
    assert event["schema"] == mod.EVENT_SCHEMA
    assert event["kind"] in {"deferred", "delta"}
