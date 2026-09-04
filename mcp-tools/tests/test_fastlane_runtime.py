from __future__ import annotations

import copy
import inspect
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-tools"))

import devkit_fastlane  # noqa: E402
import server  # noqa: E402
from devkit_fastlane.scripts import team_efficiency  # noqa: E402
from devkit_runtime.bootstrap import RuntimeBootstrap  # noqa: E402
from devkit_runtime.composition import RuntimeRoot  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from project_index.models import IndexError as ProjectIndexError  # noqa: E402
from project_index.models import IndexState  # noqa: E402

sys.path.insert(0, str(ROOT / "mcp-tools" / "devkit_fastlane" / "tests"))


def _sample_request() -> tuple[object, dict[str, object], object]:
    """Build one real contract request without importing its test class at collection."""

    from importlib import import_module

    tests = import_module("test_team_efficiency")
    helper = tests.TeamEfficiencyTests("runTest")
    helper.setUp()
    return helper, helper.fast_lane_request(team_efficiency), tests


def _install_local_plan_runtime(monkeypatch, helper, request) -> str:
    """Install one read-only registry/index/Git projection for adapter tests."""

    project_binding = request["project_binding"]
    work_package = request["work_package"]
    scheduler_state = request["scheduler_state"]
    assert isinstance(project_binding, dict)
    assert isinstance(work_package, dict)
    assert isinstance(scheduler_state, dict)
    integration_state = scheduler_state["integration_state"]
    assert isinstance(integration_state, dict)
    workspace_id = project_binding["workspace_id"]
    snapshot_id = work_package["input_snapshot_id"]
    git_head = integration_state["commit"]
    assert isinstance(workspace_id, str)
    assert isinstance(snapshot_id, str)
    assert isinstance(git_head, str)

    marker = team_efficiency._sha256_json
    root_identity_hash = marker({"root": workspace_id})
    workspace_binding_hash = marker(
        {
            "workspace_id": workspace_id,
            "root_identity_hash": root_identity_hash,
        }
    )
    head_hash = marker({"head": git_head})
    manifest_hash = marker({"manifest": snapshot_id})
    parser_set_hash = marker({"parsers": snapshot_id})
    material = {
        "workspace_id": workspace_id,
        "root_identity_hash": root_identity_hash,
        "workspace_binding_hash": workspace_binding_hash,
        "snapshot_id": snapshot_id,
        "snapshot_attestation_hash": marker(
            {
                "workspace_id": workspace_id,
                "root_identity_hash": root_identity_hash,
                "workspace_binding_hash": workspace_binding_hash,
                "snapshot_id": snapshot_id,
                "head_hash": head_hash,
                "manifest_hash": manifest_hash,
                "parser_set_hash": parser_set_hash,
            }
        ),
        "head_hash": head_hash,
        "manifest_hash": manifest_hash,
        "parser_set_hash": parser_set_hash,
        "workspace_root": str(helper.repo),
        "git_head": git_head,
        "include_paths": (),
        "include_paths_hash": marker({"include_paths": ()}),
    }

    class LocalIndex:
        def local_plan_material(
            self, supplied_workspace_id: str, *, snapshot_id: str
        ) -> dict[str, object]:
            assert supplied_workspace_id == workspace_id
            assert snapshot_id == material["snapshot_id"]
            return dict(material)

    unit_of_work = SimpleNamespace(
        project_checkpoint=SimpleNamespace(project_index=LocalIndex())
    )

    def open_uow(*, read_only: bool):
        assert read_only is True
        return nullcontext(unit_of_work)

    def host_session_forbidden() -> object:
        raise AssertionError("Fast Lane must not consult an inherited Host session")

    monkeypatch.setattr(
        server, "_runtime_root", lambda: SimpleNamespace(open_uow=open_uow)
    )
    monkeypatch.setattr(server, "_host_session", host_session_forbidden, raising=False)
    return snapshot_id


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _assert_no_nulls(value: object) -> None:
    if isinstance(value, dict):
        assert all(item is not None for item in value.values())
        for item in value.values():
            _assert_no_nulls(item)
    elif isinstance(value, list):
        assert all(item is not None for item in value)
        for item in value:
            _assert_no_nulls(item)


def _request_for_persisted_snapshot(
    helper,
    *,
    workspace_id: str,
    snapshot_id: str,
    git_head: str,
) -> dict[str, object]:
    authority = dict(helper.project_binding(team_efficiency))
    authority["workspace_id"] = workspace_id
    authority["input_snapshot_id"] = snapshot_id
    original_project_binding = helper.project_binding
    helper.project_binding = lambda _module, **_kwargs: dict(authority)
    try:
        request = helper.fast_lane_request(team_efficiency)
    finally:
        helper.project_binding = original_project_binding

    source_plan = team_efficiency.decompose(request["work_package"])
    units = {unit["task_id"]: unit for unit in source_plan["units"]}
    scheduler_state = request["scheduler_state"]
    assert isinstance(scheduler_state, dict)
    integration_state = scheduler_state["integration_state"]
    assert isinstance(integration_state, dict)
    integration_state["commit"] = git_head
    integration_state["tree"] = _git(helper.repo, "rev-parse", "HEAD^{tree}")
    execution_contexts = request["execution_contexts"]
    assert isinstance(execution_contexts, list)
    for context in execution_contexts:
        assert isinstance(context, dict)
        task_id = context["task_id"]
        unit = units[task_id]
        bootstrap = context["bootstrap_plan"]
        assert isinstance(bootstrap, dict)
        context["bootstrap_plan"] = team_efficiency.build_bootstrap_plan(
            task_id=task_id,
            base_commit=git_head,
            branch=bootstrap["branch"],
            write_scope=unit["write_scope"],
            repo=helper.repo,
            project=bootstrap["project"],
            worktree=bootstrap["worktree"],
            temp_target=bootstrap["temp_target"],
        )
        context["workspace_input_snapshot_id"] = snapshot_id
    return request


def _install_persistent_local_plan_runtime(
    monkeypatch,
    helper,
    *,
    include_paths: list[str] | None = None,
    partial: bool = False,
) -> tuple[RuntimeRoot, dict[str, object], str]:
    manifest = helper.decomposition_manifest()
    source_plan = team_efficiency.decompose(manifest)
    for unit in source_plan["units"]:
        for scope in unit["write_scope"]:
            target = helper.repo / scope
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (
                "def indexed_scope() -> bool:\n    return True\n"
                if target.suffix == ".py"
                else "# Indexed scope\n"
            )
            target.write_text(body, encoding="utf-8")
    if partial:
        (helper.repo / "unsupported.js").write_text(
            "export const unsupported = true;\n", encoding="utf-8"
        )
    _git(helper.repo, "init", "--quiet")
    _git(helper.repo, "config", "user.name", "Fast Lane Test")
    _git(helper.repo, "config", "user.email", "fastlane@example.invalid")
    _git(helper.repo, "add", ".")
    _git(helper.repo, "commit", "--quiet", "-m", "fixture")
    git_head = _git(helper.repo, "rev-parse", "HEAD")

    scratch = helper.temp / "runtime-scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(helper.temp / "runtime-data"),
            "CODEX_TASK_TEMP": str(scratch),
        }
    )
    RuntimeBootstrap.run(config)
    root = RuntimeRoot(config)
    monkeypatch.setattr(server, "_RUNTIME_ROOT", root)
    registered = server.project_index_register(str(helper.repo))
    assert registered["ok"] is True
    workspace_id = registered["data"]["workspace_id"]
    assert isinstance(workspace_id, str)
    synced = server.project_index_sync(workspace_id, include_paths=include_paths)
    assert synced["ok"] is True
    snapshot_id = synced["data"]["snapshot_id"]
    assert isinstance(snapshot_id, str)
    request = _request_for_persisted_snapshot(
        helper,
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        git_head=git_head,
    )
    return root, request, snapshot_id


def test_work_methodology_is_not_a_discoverable_skill() -> None:
    """Fast Lane contract lives in the MCP runtime, not prompt-discovered skills."""

    assert not (ROOT / "skills" / "work-methodology" / "SKILL.md").exists()
    runtime = ROOT / "mcp-tools" / "devkit_fastlane"
    assert (runtime / "__init__.py").is_file()
    assert (runtime / "scripts" / "team_efficiency.py").is_file()


def test_fastlane_docs_describe_the_mcp_plan_v2_boundary() -> None:
    contract = (ROOT / "mcp-tools/devkit_fastlane/FASTLANE_CONTRACT.md").read_text(
        encoding="utf-8"
    )
    readmes = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (ROOT / "README.zh-CN.md").read_text(encoding="utf-8"),
    ]

    for document in (contract, *readmes):
        assert "team-efficiency/fast-lane-plan-v2" in document
        assert "plan_only" in document
    assert "include_paths" in contract
    assert "INDEX_PARTIAL" in contract
    assert "local_plan_material" not in contract


def test_public_compiler_api_cannot_receive_local_authority_material() -> None:
    assert (
        "local_plan_material"
        not in inspect.signature(team_efficiency.compile_fast_lane).parameters
    )
    assert not hasattr(devkit_fastlane, "fast_lane_local_selector")

    helper, request, _ = _sample_request()
    try:
        with pytest.raises(TypeError):
            devkit_fastlane.compile_fast_lane(
                request,
                reasoning_effort="max",
                enable=True,
                local_plan_material={},
            )
    finally:
        helper.tearDown()


def test_fastlane_tool_rejects_host_private_inputs() -> None:
    """Host attestations stay private; public MCP receives only an inert request."""

    helper, request, _ = _sample_request()
    try:
        request["host_status"] = {"workflow_id": "foreign"}
        result = server.fastlane_compile(
            request=request, reasoning_effort="ultra", enable=True
        )
    finally:
        helper.tearDown()
    assert result["ok"] is False
    assert result["error"]["code"] == "FASTLANE_REQUEST_INVALID"


def test_fastlane_tool_never_spawns_or_executes(monkeypatch) -> None:
    """The MCP compiler emits descriptors; the host owns execution and refill."""

    helper, request, _ = _sample_request()
    _install_local_plan_runtime(monkeypatch, helper, request)
    try:
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=False
        )
    finally:
        helper.tearDown()
    assert result["ok"] is True
    data = result["data"]
    assert data["schema"] == "team-efficiency/fast-lane-plan-v2"
    assert data["status"] == "inactive"
    assert data["assignments"] == []
    assert data["plan_only"] is True
    assert data["dispatch_state"] == "not_dispatched"
    assert data["execution_authorized"] is False
    assert "host_actions" not in data
    assert data["workflow_policy"]["compiler_side_effects"] is False


def test_fastlane_tool_hashes_the_exact_normalized_inactive_plan(monkeypatch) -> None:
    helper, request, _ = _sample_request()
    _install_local_plan_runtime(monkeypatch, helper, request)
    try:
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=False
        )
    finally:
        helper.tearDown()

    assert result["ok"] is True
    data = result["data"]
    _assert_no_nulls(data)
    assert data["plan_hash"] == team_efficiency._sha256_json(
        {key: value for key, value in data.items() if key != "plan_hash"}
    )


def test_fastlane_tool_hashes_the_exact_normalized_read_only_assignment(
    monkeypatch,
) -> None:
    helper, _, _ = _sample_request()
    request = helper.fast_lane_schedule_request(team_efficiency)
    _install_local_plan_runtime(monkeypatch, helper, request)
    try:
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        helper.tearDown()

    assert result["ok"] is True, result
    data = result["data"]
    read_only = next(
        assignment
        for assignment in data["assignments"]
        if assignment["role"] != "execution"
    )
    _assert_no_nulls(read_only)
    assert read_only["plan_item_id"] == team_efficiency._sha256_json(
        {key: value for key, value in read_only.items() if key != "plan_item_id"}
    )
    assert data["plan_hash"] == team_efficiency._sha256_json(
        {key: value for key, value in data.items() if key != "plan_hash"}
    )


def test_fastlane_plan_v2_rejects_unknown_keys_at_each_contract_layer(
    monkeypatch,
) -> None:
    helper, request, _ = _sample_request()
    _install_local_plan_runtime(monkeypatch, helper, request)
    try:
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        helper.tearDown()

    assert result["ok"] is True
    plan = result["data"]

    def add_plan_key(value: dict[str, Any]) -> None:
        value["unexpected"] = True

    def add_assignment_key(value: dict[str, Any]) -> None:
        value["assignments"][0]["unexpected"] = True

    def add_worktree_key(value: dict[str, Any]) -> None:
        value["assignments"][0]["worktree"]["unexpected"] = True

    def add_policy_key(value: dict[str, Any]) -> None:
        value["workflow_policy"]["unexpected"] = True

    for mutate in (
        add_plan_key,
        add_assignment_key,
        add_worktree_key,
        add_policy_key,
    ):
        tampered = copy.deepcopy(plan)
        mutate(tampered)
        with pytest.raises(ValueError, match="unsupported fields"):
            team_efficiency._validated_fast_lane_local_plan(tampered)


def test_fastlane_tool_plans_writers_from_current_local_index_without_host(
    monkeypatch,
) -> None:
    """A current local index is sufficient for a non-dispatched writer plan."""

    helper, request, _ = _sample_request()
    snapshot_id = _install_local_plan_runtime(monkeypatch, helper, request)
    try:
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        helper.tearDown()

    assert result["ok"] is True
    data = result["data"]
    assert data["schema"] == "team-efficiency/fast-lane-plan-v2"
    assert data["status"] == "planned"
    assert data["plan_only"] is True
    assert data["dispatch_state"] == "not_dispatched"
    assert data["execution_authorized"] is False
    writers = [
        assignment
        for assignment in data["assignments"]
        if assignment["role"] == "execution"
    ]
    assert writers
    assert all(assignment["execution_state"] == "plan_only" for assignment in writers)
    assert all(
        assignment["dispatch_state"] == "not_dispatched" for assignment in writers
    )
    assert all(assignment["execution_authorized"] is False for assignment in writers)
    assert all(assignment["lease_state"] == "unclaimed" for assignment in writers)
    assert all(assignment["worktree"]["state"] == "planned" for assignment in writers)
    assert all(
        assignment["index_evidence"]["snapshot_id"] == snapshot_id
        for assignment in writers
    )
    forbidden = {
        "action",
        "host_dispatch",
        "assignment_token",
        "dispatch_receipt",
        "terminal_receipt",
    }
    assert all(forbidden.isdisjoint(assignment) for assignment in writers)
    assert "host_status_unavailable" not in str(data)


def test_fastlane_tool_uses_persisted_runtime_registry_index_and_git_head(
    monkeypatch,
) -> None:
    helper, _, _ = _sample_request()
    root = None
    try:
        root, request, snapshot_id = _install_persistent_local_plan_runtime(
            monkeypatch, helper
        )
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        if root is not None:
            root.shutdown()
        helper.tearDown()

    assert result["ok"] is True
    data = result["data"]
    assert data["schema"] == "team-efficiency/fast-lane-plan-v2"
    assert data["status"] == "planned"
    assert data["index_evidence"]["snapshot_id"] == snapshot_id
    assert data["index_evidence"]["include_paths_hash"].startswith("sha256:")
    assert all(item["execution_state"] == "plan_only" for item in data["assignments"])


def test_fastlane_tool_rejects_stale_persisted_snapshot(monkeypatch) -> None:
    helper, _, _ = _sample_request()
    root = None
    try:
        root, request, _ = _install_persistent_local_plan_runtime(monkeypatch, helper)
        changed = helper.repo / "skills/work-methodology/scripts/team_efficiency.py"
        changed.write_text(
            "def changed_after_sync():\n    return True\n", encoding="utf-8"
        )
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        if root is not None:
            root.shutdown()
        helper.tearDown()

    assert result["ok"] is False
    assert result["error"]["code"] == "INDEX_STALE"


def test_fastlane_tool_rejects_partial_persisted_snapshot(monkeypatch) -> None:
    helper, _, _ = _sample_request()
    root = None
    try:
        root, request, _ = _install_persistent_local_plan_runtime(
            monkeypatch, helper, partial=True
        )
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        if root is not None:
            root.shutdown()
        helper.tearDown()

    assert result["ok"] is False
    assert result["error"]["code"] == "INDEX_PARTIAL"


@pytest.mark.parametrize(
    "state",
    [
        IndexState.INDEX_PARTIAL,
        IndexState.INDEX_STALE,
        IndexState.INDEX_UNAVAILABLE,
        IndexState.INDEX_CORRUPT,
        IndexState.HISTORICAL_UNVERIFIED,
    ],
)
def test_project_index_local_plan_material_requires_exact_ready_state(
    monkeypatch, state: IndexState
) -> None:
    helper, _, _ = _sample_request()
    root = None
    try:
        root, request, snapshot_id = _install_persistent_local_plan_runtime(
            monkeypatch, helper
        )
        project_binding = request["project_binding"]
        assert isinstance(project_binding, dict)
        workspace_id = project_binding["workspace_id"]
        assert isinstance(workspace_id, str)
        with root.open_uow(read_only=True) as uow:
            index = uow.project_checkpoint.project_index
            ready = index._require_snapshot(workspace_id, snapshot_id)
            non_ready = replace(ready, state=state)
            monkeypatch.setattr(
                index,
                "_require_snapshot",
                lambda supplied_workspace_id, supplied_snapshot_id: non_ready,
            )
            with pytest.raises(ProjectIndexError) as captured:
                index.local_plan_material(workspace_id, snapshot_id=snapshot_id)
    finally:
        if root is not None:
            root.shutdown()
        helper.tearDown()

    assert captured.value.code == state.name


def test_fastlane_tool_rejects_snapshot_that_omits_a_writer_scope(monkeypatch) -> None:
    helper, _, _ = _sample_request()
    source_plan = team_efficiency.decompose(helper.decomposition_manifest())
    first_scope = source_plan["units"][0]["write_scope"][0]
    root = None
    try:
        root, request, _ = _install_persistent_local_plan_runtime(
            monkeypatch, helper, include_paths=[first_scope]
        )
        result = server.fastlane_compile(
            request=request, reasoning_effort="max", enable=True
        )
    finally:
        if root is not None:
            root.shutdown()
        helper.tearDown()

    assert result["ok"] is False
    assert result["error"]["code"] == "INDEX_PARTIAL"
