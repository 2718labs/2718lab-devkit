"""Contract tests for the deterministic project index core."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from project_index import IndexError, IndexState, ProjectIndexService  # noqa: E402
import project_index.service as service_module  # noqa: E402


def _service(tmp_path: Path) -> ProjectIndexService:
    return ProjectIndexService(tmp_path / "index.sqlite3")


def test_git_head_uses_devnull_for_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_kwargs: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed_kwargs.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="a" * 40 + "\n",
            stderr="",
        )

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)

    assert service_module._git_head(tmp_path) == "a" * 40
    assert observed_kwargs.get("stdin") is subprocess.DEVNULL


def test_sync_is_deterministic_and_reuses_unchanged_blobs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "alpha.py").write_text(
        "def alpha():\n    return 1\n", encoding="utf-8"
    )
    (workspace / "notes.md").write_text("# Notes\n", encoding="utf-8")
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    assert workspace_id.startswith("sha256:")
    assert len(workspace_id) == len("sha256:") + 64
    assert all(character in "0123456789abcdef" for character in workspace_id[7:])

    first = service.sync(workspace_id)
    identical = service.sync(workspace_id)
    (workspace / "alpha.py").write_text(
        "def alpha():\n    return 2\n", encoding="utf-8"
    )
    changed = service.sync(workspace_id)

    assert first.snapshot_id == identical.snapshot_id
    assert first.snapshot_id.startswith("sha256:")
    assert first == identical
    assert changed.snapshot_id != first.snapshot_id
    assert changed.reused_blob_count == 1
    assert changed.file_count == 2

    service.close()
    reopened = ProjectIndexService(tmp_path / "index.sqlite3")
    assert reopened.project_index_register(workspace) == workspace_id
    assert reopened.status(workspace_id).snapshot_id == changed.snapshot_id
    reopened.close()


def test_extractors_emit_only_parser_backed_facts_and_explicit_gaps(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text(
        "import json\n\nclass Widget:\n    pass\n\ndef test_widget():\n    return Widget()\n",
        encoding="utf-8",
    )
    (workspace / "guide.md").write_text(
        "# Guide\n\n## Install\n\nSee [details](details.md).\n\n"
        "```markdown\n# Not a document heading\n```\n",
        encoding="utf-8",
    )
    (workspace / "settings.json").write_text(
        '{"database": {"host": "localhost"}, "enabled": true}\n', encoding="utf-8"
    )
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n[tool.demo]\nenabled = true\n', encoding="utf-8"
    )
    (workspace / "pipeline.yaml").write_text(
        "name: build\nsteps:\n  - run: pytest\n", encoding="utf-8"
    )
    (workspace / "app.js").write_text("export const value = 1;\n", encoding="utf-8")
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)

    snapshot = service.sync(workspace_id)
    result = service.query(workspace_id, snapshot.snapshot_id, "", max_nodes=100)

    facts = {(node.kind, node.path, node.qualified_name) for node in result.nodes}
    assert ("class", "sample.py", "sample.Widget") in facts
    assert ("test", "sample.py", "sample.test_widget") in facts
    assert any(node.kind == "import" and node.name == "json" for node in result.nodes)
    assert any(
        node.kind == "heading" and node.name == "Install" for node in result.nodes
    )
    assert not any(
        node.kind == "heading" and node.name == "Not a document heading"
        for node in result.nodes
    )
    assert any(
        node.kind == "config_key" and node.qualified_name == "database.host"
        for node in result.nodes
    )
    assert any(
        node.kind == "config_key" and node.qualified_name == "tool.demo.enabled"
        for node in result.nodes
    )
    assert any(
        node.kind == "config_key"
        and node.path == "pipeline.yaml"
        and node.name == "steps"
        for node in result.nodes
    )
    assert any(
        gap.path == "pipeline.yaml" and gap.code == "YAML_PARTIAL"
        for gap in result.gaps
    )
    assert any(
        gap.path == "app.js" and gap.code == "UNSUPPORTED_PARSER" for gap in result.gaps
    )
    assert snapshot.state is IndexState.INDEX_PARTIAL
    service.close()


def test_lexical_graph_and_impact_queries_are_deterministic(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "logic.py").write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def uses_helper():\n"
        "    return helper()\n\n"
        "def unrelated():\n"
        "    return 0\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)

    lexical = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "helper",
        mode="lexical",
        node_kinds=("function",),
    )
    graph = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "uses_helper",
        mode="graph",
        relations=("calls",),
        max_depth=1,
    )
    impact = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "def helper",
        mode="impact",
        relations=("calls",),
        max_depth=1,
    )
    repeated = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "helper",
        mode="lexical",
        node_kinds=("function",),
    )
    body_match = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "return 0",
        mode="lexical",
        node_kinds=("function",),
    )

    assert lexical.trace_id == repeated.trace_id
    assert [node.node_id for node in lexical.nodes] == [
        node.node_id for node in repeated.nodes
    ]
    assert any(node.name == "helper" for node in lexical.nodes)
    assert any(node.name == "helper" for node in graph.nodes)
    assert any(edge.relation == "calls" for edge in graph.edges)
    assert any(node.name == "uses_helper" for node in impact.nodes)
    assert tuple(node.name for node in body_match.nodes) == ("unrelated",)
    service.close()


def test_query_bounds_windows_and_rejects_stale_source_hashes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "many.py"
    source.write_text(
        "def one():\n    return 1\n\n"
        "def two():\n    return 2\n\n"
        "def three():\n    return 3\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)

    bounded = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "def",
        node_kinds=("function",),
        max_nodes=1,
        source_lines=2,
        byte_budget=1024,
    )

    assert len(bounded.nodes) == 1
    assert bounded.truncated is True
    assert len(bounded.source_windows) == 1
    window = bounded.source_windows[0]
    assert window.end_line - window.start_line + 1 <= 2
    assert len(window.text.encode("utf-8")) <= 1024

    byte_limited = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "one",
        node_kinds=("function",),
        max_nodes=10,
        source_lines=0,
        byte_budget=64,
    )
    assert byte_limited.nodes == ()
    assert byte_limited.truncated is True

    source.write_text("def changed():\n    return 9\n", encoding="utf-8")
    with pytest.raises(IndexError) as captured:
        service.query(workspace_id, snapshot.snapshot_id, "one")
    assert captured.value.code == "INDEX_STALE"
    service.close()


def test_status_assert_current_and_diff_are_path_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "skip.py").write_text("SKIP = 1\n", encoding="utf-8")
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)

    first = service.sync(workspace_id, include_paths=("keep.py",))
    assert (
        service.status(
            workspace_id, first.snapshot_id, required_paths=("keep.py",)
        ).state
        is IndexState.INDEX_READY
    )
    assert (
        service.assert_current(
            workspace_id, first.snapshot_id, required_paths=("keep.py",)
        )
        == first
    )

    (workspace / "keep.py").write_text("VALUE = 2\n", encoding="utf-8")
    stale = service.status(workspace_id, first.snapshot_id, required_paths=("keep.py",))
    assert stale.state is IndexState.INDEX_STALE
    with pytest.raises(IndexError) as captured:
        service.assert_current(
            workspace_id, first.snapshot_id, required_paths=("keep.py",)
        )
    assert captured.value.code == "INDEX_STALE"

    second = service.sync(workspace_id, include_paths=("keep.py",))
    difference = service.diff(workspace_id, first.snapshot_id, second.snapshot_id)
    assert difference.added_paths == ()
    assert difference.removed_paths == ()
    assert difference.changed_paths == ("keep.py",)
    assert all(
        node.path != "skip.py"
        for node in service.query(workspace_id, second.snapshot_id, "").nodes
    )

    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = True\n", encoding="utf-8")
    with pytest.raises(IndexError) as captured:
        service.sync(workspace_id, include_paths=(outside,))
    assert captured.value.code == "SCOPE_ESCAPE"
    service.close()


def test_query_rejects_a_source_parent_replaced_by_external_link(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source_directory = workspace / "source"
    source_directory.mkdir(parents=True)
    indexed_file = source_directory / "module.py"
    content = "def indexed():\n    return True\n"
    indexed_file.write_text(content, encoding="utf-8")
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "module.py").write_text(content, encoding="utf-8")
    indexed_file.unlink()
    source_directory.rmdir()
    try:
        source_directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        service.close()
        pytest.skip("directory symlinks are unavailable on this Windows host")

    with pytest.raises(IndexError) as captured:
        service.query(workspace_id, snapshot.snapshot_id, "indexed")
    assert captured.value.code == "INDEX_STALE"
    service.close()


def test_required_paths_accept_directory_scopes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scope = workspace / "scope"
    scope.mkdir(parents=True)
    scoped_file = scope / "module.py"
    scoped_file.write_text("VALUE = 1\n", encoding="utf-8")
    outside = workspace / "outside.py"
    outside.write_text("OUTSIDE = 1\n", encoding="utf-8")
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)

    assert (
        service.assert_current(
            workspace_id, snapshot.snapshot_id, required_paths=("scope",)
        )
        == snapshot
    )
    outside.write_text("OUTSIDE = 2\n", encoding="utf-8")
    assert (
        service.status(
            workspace_id, snapshot.snapshot_id, required_paths=("scope",)
        ).state
        is IndexState.INDEX_READY
    )

    scoped_file.write_text("VALUE = 2\n", encoding="utf-8")
    status = service.status(
        workspace_id, snapshot.snapshot_id, required_paths=("scope",)
    )
    assert status.state is IndexState.INDEX_STALE
    assert status.changed_paths == ("scope/module.py",)
    service.close()


def test_nodes_and_edges_expose_auditable_utf8_spans(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    source.write_text(
        "# cafe: \u5496\u5561\n\n"
        "def helper():\n"
        "    return 1\n\n"
        "def caller():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    result = service.query(
        workspace_id, snapshot.snapshot_id, "", max_nodes=100, source_lines=0
    )
    source_bytes = source.read_bytes()
    allowed_provenance = {"observed", "resolved", "declared"}

    assert result.nodes
    assert result.edges
    for fact in (*result.nodes, *result.edges):
        assert fact.content_hash.startswith("sha256:")
        assert fact.extractor_id
        assert fact.extractor_version
        assert fact.provenance in allowed_provenance
        assert 0 <= fact.start_byte < fact.end_byte <= len(source_bytes)
        assert fact.start_line == source_bytes[: fact.start_byte].count(b"\n") + 1
        assert fact.end_line == source_bytes[: fact.end_byte - 1].count(b"\n") + 1

    helper = next(node for node in result.nodes if node.name == "helper")
    assert (
        source_bytes[helper.start_byte : helper.end_byte]
        .decode("utf-8")
        .startswith("def helper")
    )
    call_edge = next(edge for edge in result.edges if edge.relation == "calls")
    assert call_edge.provenance == "resolved"
    assert source_bytes[call_edge.start_byte : call_edge.end_byte] == b"helper()"
    service.close()


def test_parser_cache_is_durable_path_neutral_and_reresolves_each_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_index import extractors

    parse_source = getattr(extractors, "parse_source", None)
    assert callable(parse_source), (
        "a per-blob parser entry point is required for cache verification"
    )
    parsed_paths: list[str] = []

    def counting_parse(source):
        parsed_paths.append(source.path)
        return parse_source(source)

    monkeypatch.setattr(extractors, "parse_source", counting_parse)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "alpha.py"
    caller = workspace / "caller.py"
    original.write_text("def helper():\n    return 1\n", encoding="utf-8")
    caller.write_text(
        "from alpha import helper\n\ndef caller():\n    return helper()\n",
        encoding="utf-8",
    )

    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    first = service.sync(workspace_id)
    assert parsed_paths == ["alpha.py", "caller.py"]
    service.close()

    parsed_paths.clear()
    reopened = _service(tmp_path)
    assert reopened.project_index_register(workspace) == workspace_id
    identical = reopened.sync(workspace_id)
    assert identical == first
    assert parsed_paths == []

    renamed = workspace / "renamed.py"
    original.rename(renamed)
    renamed_snapshot = reopened.sync(workspace_id)
    assert parsed_paths == []
    assert renamed_snapshot.reused_blob_count == 2
    renamed_result = reopened.query(
        workspace_id, renamed_snapshot.snapshot_id, "", max_nodes=100, source_lines=0
    )
    assert not any(edge.relation == "calls" for edge in renamed_result.edges)
    assert any(
        gap.path == "caller.py" and gap.code == "PYTHON_UNRESOLVED_REFERENCE"
        for gap in renamed_result.gaps
    )
    assert any(
        node.path == "renamed.py" and node.name == "helper"
        for node in renamed_result.nodes
    )
    assert not any(node.path == "alpha.py" for node in renamed_result.nodes)

    caller.write_text(
        "from renamed import helper\n\ndef caller():\n    return helper()\n",
        encoding="utf-8",
    )
    parsed_paths.clear()
    changed = reopened.sync(workspace_id)
    assert parsed_paths == ["caller.py"]
    assert changed.reused_blob_count == 1
    changed_result = reopened.query(
        workspace_id,
        changed.snapshot_id,
        "caller",
        mode="graph",
        max_nodes=100,
        source_lines=0,
    )
    helper = next(
        node
        for node in changed_result.nodes
        if node.path == "renamed.py" and node.name == "helper"
    )
    assert any(
        edge.relation == "calls" and edge.target_id == helper.node_id
        for edge in changed_result.edges
    )
    reopened.close()


def test_markdown_extracts_structured_work_package_facts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "task.md").write_text(
        "---\n"
        "title: Cache hardening\n"
        "priority: high\n"
        "---\n\n"
        "# IDX-01A\n\n"
        "Owner: index-writer\n"
        "Depends on: IDX-01, RT-01\n\n"
        "## Write Scope\n\n"
        "- `src/index.py`\n"
        "- `tests/test_index.py`\n\n"
        "- [ ] preserve receipts\n"
        "- [x] define spans\n\n"
        "See [the contract](../contracts/project-index-api.md).\n\n"
        "```python\n"
        "# not a heading\n"
        "print('indexed')\n"
        "```\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    result = service.query(
        workspace_id, snapshot.snapshot_id, "", max_nodes=100, source_lines=0
    )

    facts = {(node.kind, node.name) for node in result.nodes}
    assert {("frontmatter_key", "title"), ("frontmatter_key", "priority")} <= facts
    assert ("heading", "IDX-01A") in facts
    assert ("link", "the contract") in facts
    assert ("checkbox", "preserve receipts") in facts
    assert ("checkbox", "define spans") in facts
    assert any(
        node.kind == "code_fence" and dict(node.attributes)["language"] == "python"
        for node in result.nodes
    )
    assert ("work_package_owner", "index-writer") in facts
    assert {
        ("work_package_dependency", "IDX-01"),
        ("work_package_dependency", "RT-01"),
    } <= facts
    assert {
        ("work_package_write_scope", "src/index.py"),
        ("work_package_write_scope", "tests/test_index.py"),
    } <= facts
    assert not any(
        node.kind == "heading" and node.name == "not a heading" for node in result.nodes
    )
    service.close()


def test_unresolved_and_dynamic_python_references_are_gaps_not_guessed_edges(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "caller.py").write_text(
        "def caller(name):\n    missing()\n    return getattr(object(), name)()\n",
        encoding="utf-8",
    )
    (workspace / "unrelated.py").write_text(
        "def missing():\n    return 'not imported'\n", encoding="utf-8"
    )
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    result = service.query(
        workspace_id, snapshot.snapshot_id, "", max_nodes=100, source_lines=0
    )
    caller = next(
        node
        for node in result.nodes
        if node.path == "caller.py" and node.name == "caller"
    )
    unrelated = next(
        node
        for node in result.nodes
        if node.path == "unrelated.py" and node.name == "missing"
    )

    assert not any(
        edge.relation == "calls"
        and edge.source_id == caller.node_id
        and edge.target_id == unrelated.node_id
        for edge in result.edges
    )
    caller_gap_codes = {gap.code for gap in result.gaps if gap.path == "caller.py"}
    assert "PYTHON_UNRESOLVED_REFERENCE" in caller_gap_codes
    assert "PYTHON_DYNAMIC_REFERENCE" in caller_gap_codes
    service.close()


def test_query_receipts_persist_and_can_be_loaded_after_reopen(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    (workspace / "unknown.js").write_text("export const value = 1;\n", encoding="utf-8")
    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    result = service.query(
        workspace_id,
        snapshot.snapshot_id,
        "alpha absent",
        mode="graph",
        max_nodes=2,
        max_depth=2,
        source_lines=1,
        byte_budget=4096,
        allow_miss_escape=True,
    )
    receipt = service.get_query_receipt(result.trace_id)

    assert receipt.trace_id == result.trace_id
    assert receipt.snapshot_id == snapshot.snapshot_id
    assert receipt.max_nodes == 2
    assert receipt.max_depth == 2
    assert receipt.source_lines == 1
    assert receipt.byte_budget == 4096
    assert receipt.allow_miss_escape is True
    assert receipt.miss_escape_used is True
    assert receipt.returned_node_ids == tuple(node.node_id for node in result.nodes)
    assert receipt.returned_edge_ids == tuple(edge.edge_id for edge in result.edges)
    assert receipt.gaps == result.gaps
    service.close()

    reopened = _service(tmp_path)
    assert reopened.get_query_receipt(result.trace_id) == receipt
    with pytest.raises(IndexError) as captured:
        reopened.get_query_receipt("sha256:" + "0" * 64)
    assert captured.value.code == "NOT_FOUND"
    reopened.close()


def test_snapshot_exposes_deterministic_manifest_parser_set_and_git_head(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Index Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "index@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(workspace), "add", "sample.py"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "--quiet", "-m", "fixture"], check=True
    )
    expected_head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    service = _service(tmp_path)
    workspace_id = service.project_index_register(workspace)
    first = service.sync(workspace_id)
    repeated = service.sync(workspace_id)
    assert first == repeated
    assert first.manifest_hash.startswith("sha256:")
    assert first.parser_set_hash.startswith("sha256:")
    assert first.head == expected_head
    service.close()


def test_snapshot_facts_and_files_are_hash_verified(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "module.py"
    source.write_bytes(b"VALUE = 1\n")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    facts = service.snapshot_facts(workspace_id, snapshot.snapshot_id)
    files = service.read_snapshot_files(
        workspace_id,
        snapshot.snapshot_id,
        ("module.py",),
        byte_budget=1024,
    )
    assert facts.snapshot == snapshot
    assert facts.file_hashes == (("module.py", files[0].content_hash),)
    assert files[0].body == b"VALUE = 1\n"
    source.write_bytes(b"VALUE = 2\n")
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("module.py",), byte_budget=1024
        )
    assert captured.value.code == "INDEX_STALE"
    service.close()


def test_snapshot_file_body_is_not_stored_in_the_index_database(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    marker = b"ATLAS05_SOURCE_MARKER_7fddb342\n"
    (workspace / "marker.txt").write_bytes(marker)
    database = tmp_path / "index.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    assert (
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("marker.txt",), byte_budget=1024
        )[0].body
        == marker
    )
    service.close()
    assert marker not in database.read_bytes()


@pytest.mark.parametrize("byte_budget", (0, -1, True, 1.5))
def test_snapshot_file_reader_rejects_invalid_budgets(
    tmp_path: Path, byte_budget: float
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"VALUE = 1\n")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    with pytest.raises(IndexError):
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("module.py",), byte_budget=byte_budget
        )
    service.close()


def test_snapshot_file_reader_rejects_stale_foreign_and_unsafe_inputs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    foreign = tmp_path / "foreign"
    workspace.mkdir()
    foreign.mkdir()
    (workspace / "a.py").write_bytes(b"A\n")
    (workspace / "b.py").write_bytes(b"B\n")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    foreign_id = service.project_index_register(foreign)
    snapshot = service.sync(workspace_id)
    for paths in (
        (),
        ("b.py", "a.py"),
        ("a.py", "a.py"),
        (".",),
        ("..",),
        ("build/x.py",),
        ("a.py:stream",),
    ):
        with pytest.raises(IndexError):
            service.read_snapshot_files(
                workspace_id, snapshot.snapshot_id, paths, byte_budget=1024
            )
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            foreign_id, snapshot.snapshot_id, ("a.py",), byte_budget=1024
        )
    assert captured.value.code == "NOT_FOUND"
    (workspace / "added.py").write_bytes(b"added\n")
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("a.py",), byte_budget=1024
        )
    assert captured.value.code == "INDEX_STALE"
    (workspace / "added.py").unlink()
    (workspace / "a.py").unlink()
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("a.py",), byte_budget=1024
        )
    assert captured.value.code == "INDEX_STALE"
    service.close()


def test_snapshot_file_reader_captures_each_target_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"VALUE = 1\n")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    observed: list[str] = []
    original = service_module._stream_workspace_file

    def counted(
        root: Path, path: Path, retain: bool, remaining_budget: int
    ) -> tuple[str, bytes | None, int]:
        observed.append(path.name)
        return original(root, path, retain, remaining_budget)

    monkeypatch.setattr(service_module, "_stream_workspace_file", counted)
    files = service.read_snapshot_files(
        workspace_id, snapshot.snapshot_id, ("module.py",), byte_budget=1024
    )
    assert files[0].body == b"VALUE = 1\n"
    assert observed.count("module.py") == 1
    service.close()


def test_snapshot_file_reader_orders_and_bounds_without_partial_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "a.py").write_bytes(b"aa")
    (workspace / "b.py").write_bytes(b"bb")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    assert tuple(
        item.path
        for item in service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("a.py", "b.py"), byte_budget=4
        )
    ) == ("a.py", "b.py")
    for budget in (1, 3):
        with pytest.raises(IndexError):
            service.read_snapshot_files(
                workspace_id,
                snapshot.snapshot_id,
                ("a.py", "b.py"),
                byte_budget=budget,
            )
    service.close()


@pytest.mark.parametrize(
    "path", ("/etc/passwd", "C:\\temp\\x.py", "\\\\server\\share\\x.py")
)
def test_snapshot_file_reader_rejects_absolute_drive_and_unc_paths(
    tmp_path: Path, path: str
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "a.py").write_bytes(b"x")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    with pytest.raises(IndexError):
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, (path,), byte_budget=8
        )
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, "sha256:" + "0" * 64, ("a.py",), byte_budget=8
        )
    assert captured.value.code == "NOT_FOUND"
    service.close()


def test_snapshot_file_reader_rejects_directory_and_preserves_inputs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "a.py"
    source.write_bytes(b"marker")
    (workspace / "folder").mkdir()
    database = tmp_path / "index.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    workspace_before = tuple(
        sorted(
            (path.relative_to(workspace).as_posix(), path.read_bytes())
            for path in workspace.rglob("*")
            if path.is_file()
        )
    )
    database_before = database.read_bytes()
    with pytest.raises(IndexError):
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("folder",), byte_budget=64
        )
    assert database.read_bytes() == database_before
    assert (
        tuple(
            sorted(
                (path.relative_to(workspace).as_posix(), path.read_bytes())
                for path in workspace.rglob("*")
                if path.is_file()
            )
        )
        == workspace_before
    )
    service.close()


def test_snapshot_file_reader_rejects_symlink_probe(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_bytes(b"marker")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    link = workspace / "link.py"
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"link capability unavailable: {exc}")
    with pytest.raises(IndexError):
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("link.py",), byte_budget=64
        )
    service.close()


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def test_snapshot_file_reader_rejects_same_size_atomic_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_bytes(b"safe")
    replacement = workspace / "replacement.py"
    replacement.write_bytes(b"evil")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id, include_paths=("module.py",))
    original_open = service_module.os.open
    replaced = False

    def replace_before_open(
        path: str | os.PathLike[str], flags: int, *args: int
    ) -> int:
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            os.replace(replacement, target)
        return original_open(path, flags, *args)

    monkeypatch.setattr(service_module.os, "open", replace_before_open)
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("module.py",), byte_budget=64
        )
    assert captured.value.code == "INDEX_STALE"
    assert target.read_bytes() == b"evil"
    service.close()


def test_snapshot_file_reader_rejects_directory_junction_parent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    parent = workspace / "src"
    parent.mkdir()
    (parent / "module.py").write_bytes(b"safe")
    external = tmp_path / "external"
    external.mkdir()
    (external / "module.py").write_bytes(b"outside")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    (parent / "module.py").unlink()
    parent.rmdir()
    try:
        _make_directory_link(parent, external)
    except (OSError, subprocess.CalledProcessError) as exc:
        service.close()
        pytest.skip(f"junction capability unavailable: {exc}")
    try:
        with pytest.raises(IndexError) as captured:
            service.read_snapshot_files(
                workspace_id, snapshot.snapshot_id, ("src/module.py",), byte_budget=64
            )
        assert captured.value.code == "INDEX_STALE"
        assert external.joinpath("module.py").read_bytes() == b"outside"
    finally:
        service.close()
        if os.name == "nt":
            parent.rmdir()
        else:
            parent.unlink()


class _TrackedReader:
    """Expose a real file object while recording bounded read and close behavior."""

    def __init__(
        self,
        stream: object,
        read_sizes: list[int],
        *,
        before_read: Callable[[], None] | None = None,
    ) -> None:
        self._stream = stream
        self._read_sizes = read_sizes
        self._before_read = before_read
        self.closed = False

    def __enter__(self) -> _TrackedReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True
        self._stream.close()  # type: ignore[attr-defined]

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[attr-defined]

    def read(self, size: int = -1) -> bytes:
        self._read_sizes.append(size)
        if self._before_read is not None:
            self._before_read()
        return self._stream.read(size)  # type: ignore[attr-defined]


def test_snapshot_file_reader_prevalidates_aggregate_before_requested_body_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "a.py").write_bytes(b"aaa")
    (workspace / "b.py").write_bytes(b"bbb")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    original_open = service_module.os.open
    original_fdopen = service_module.os.fdopen
    descriptors: dict[int, Path] = {}
    opened_paths: list[Path] = []
    requested_reads: list[tuple[Path, int]] = []
    readers: list[_TrackedReader] = []

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        candidate = Path(path)
        descriptors[descriptor] = candidate
        opened_paths.append(candidate)
        return descriptor

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _TrackedReader:
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        path = descriptors.get(descriptor)

        class RequestedReadTracker(_TrackedReader):
            def read(self, size: int = -1) -> bytes:
                if path is not None and path.name in {"a.py", "b.py"}:
                    requested_reads.append((path, size))
                return super().read(size)

        reader = RequestedReadTracker(stream, [])
        readers.append(reader)
        return reader

    monkeypatch.setattr(service_module.os, "open", tracked_open)
    monkeypatch.setattr(service_module.os, "fdopen", tracked_fdopen)
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("a.py", "b.py"), byte_budget=5
        )
    assert captured.value.code == "INVALID_QUERY"
    assert [path.name for path in opened_paths if path.name in {"a.py", "b.py"}] == [
        "a.py",
        "b.py",
    ]
    assert requested_reads == []
    assert readers and all(reader.closed for reader in readers)
    service.close()


def test_snapshot_file_reader_rejects_duplicate_requests_before_any_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"VALUE = 1\n")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)

    def fail_preflight(*args: object, **kwargs: object) -> None:
        pytest.fail("duplicate paths must fail before file-size preflight")

    def fail_scan(*args: object, **kwargs: object) -> None:
        pytest.fail("duplicate paths must fail before workspace scan")

    monkeypatch.setattr(
        service_module, "_prevalidate_requested_file_sizes", fail_preflight
    )
    monkeypatch.setattr(service, "_scan_snapshot_files", fail_scan)
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id,
            snapshot.snapshot_id,
            ("module.py", "module.py"),
            byte_budget=64,
        )
    assert captured.value.code == "SCOPE_ESCAPE"
    service.close()


def test_snapshot_file_reader_caps_retained_reads_when_file_grows_after_fstat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "module.py"
    source.write_bytes(b"x")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    original_fdopen = service_module.os.fdopen
    read_sizes: list[int] = []
    grew = False

    def grow_after_fstat() -> None:
        nonlocal grew
        if not grew:
            grew = True
            with source.open("ab") as stream:
                stream.write(b"y" * (1024 * 1024))

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _TrackedReader:
        return _TrackedReader(
            original_fdopen(descriptor, mode, *args, **kwargs),
            read_sizes,
            before_read=grow_after_fstat,
        )

    monkeypatch.setattr(service_module.os, "fdopen", tracked_fdopen)
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("module.py",), byte_budget=2
        )
    assert captured.value.code == "INDEX_STALE"
    assert read_sizes
    assert all(0 < size <= 2 for size in read_sizes)
    service.close()


def test_snapshot_file_reader_streams_large_unrequested_file_without_body_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "requested.py").write_bytes(b"requested\n")
    unrequested = workspace / "large.bin"
    unrequested.write_bytes(b"x" * (8 * 1024 * 1024))
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    original = service_module._stream_workspace_file
    original_open = service_module.os.open
    original_fdopen = service_module.os.fdopen
    descriptors: dict[int, Path] = {}
    large_read_sizes: list[int] = []
    observed: list[tuple[str, bool, bytes | None, int]] = []

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        descriptors[descriptor] = Path(path)
        return descriptor

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _TrackedReader:
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        path = descriptors.get(descriptor)

        class LargeReadTracker(_TrackedReader):
            def read(self, size: int = -1) -> bytes:
                if path == unrequested:
                    large_read_sizes.append(size)
                return super().read(size)

        return LargeReadTracker(stream, [])

    def recorded(
        root: Path, path: Path, retain: bool, remaining_budget: int
    ) -> tuple[str, bytes | None, int]:
        result = original(root, path, retain, remaining_budget)
        observed.append((path.name, retain, result[1], result[2]))
        return result

    monkeypatch.setattr(service_module.os, "open", tracked_open)
    monkeypatch.setattr(service_module.os, "fdopen", tracked_fdopen)
    monkeypatch.setattr(service_module, "_stream_workspace_file", recorded)
    files = service.read_snapshot_files(
        workspace_id, snapshot.snapshot_id, ("requested.py",), byte_budget=1024
    )
    assert files[0].body == b"requested\n"
    assert ("large.bin", False, None, 8 * 1024 * 1024) in observed
    assert large_read_sizes
    assert all(0 < size <= service_module._READ_CHUNK_SIZE for size in large_read_sizes)
    assert large_read_sizes[-1] == 1

    unrequested.write_bytes(b"y" * (8 * 1024 * 1024))
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("requested.py",), byte_budget=1024
        )
    assert captured.value.code == "INDEX_STALE"
    service.close()


def test_stream_workspace_file_uses_positive_bounded_reads_and_closes_successfully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source_size = 8 * 1024 * 1024 + 1
    source.write_bytes(b"x" * source_size)
    original_fdopen = service_module.os.fdopen
    read_sizes: list[int] = []
    readers: list[_TrackedReader] = []

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _TrackedReader:
        reader = _TrackedReader(
            original_fdopen(descriptor, mode, *args, **kwargs), read_sizes
        )
        readers.append(reader)
        return reader

    monkeypatch.setattr(service_module.os, "fdopen", tracked_fdopen)
    digest, body, size = service_module._stream_workspace_file(
        tmp_path, source, False, 1
    )
    assert digest.startswith("sha256:")
    assert body is None
    assert size == source_size
    assert read_sizes
    assert all(0 < size <= service_module._READ_CHUNK_SIZE for size in read_sizes)
    assert read_sizes[-1] == 1
    assert all(reader.closed for reader in readers)


def test_stream_workspace_file_closes_descriptor_after_read_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"x")
    original_fdopen = service_module.os.fdopen
    original_fstat = service_module.os.fstat
    fstat_descriptors: list[int] = []
    readers: list[_TrackedReader] = []

    class FailingReader(_TrackedReader):
        def read(self, size: int = -1) -> bytes:
            self._read_sizes.append(size)
            raise OSError("injected read failure")

    def failing_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _TrackedReader:
        reader = FailingReader(original_fdopen(descriptor, mode, *args, **kwargs), [])
        readers.append(reader)
        return reader

    def tracked_fstat(descriptor: int) -> os.stat_result:
        fstat_descriptors.append(descriptor)
        return original_fstat(descriptor)

    monkeypatch.setattr(service_module.os, "fdopen", failing_fdopen)
    monkeypatch.setattr(service_module.os, "fstat", tracked_fstat)
    with pytest.raises(IndexError) as captured:
        service_module._stream_workspace_file(tmp_path, source, False, 1)
    assert captured.value.code == "INDEX_STALE"
    assert fstat_descriptors
    assert readers and all(reader.closed for reader in readers)


def test_snapshot_file_reader_stops_unrequested_growth_after_size_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    unrequested = workspace / "a-unrequested.bin"
    unrequested.write_bytes(b"x")
    requested = workspace / "z-requested.py"
    requested.write_bytes(b"requested\n")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    original_open = service_module.os.open
    original_fdopen = service_module.os.fdopen
    descriptors: dict[int, Path] = {}
    read_sizes: list[int] = []
    read_bytes = 0
    grew = False

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        descriptors[descriptor] = Path(path)
        return descriptor

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _TrackedReader:
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        path = descriptors.get(descriptor)

        class GrowingReadTracker(_TrackedReader):
            def read(self, size: int = -1) -> bytes:
                nonlocal grew, read_bytes
                if path == unrequested:
                    read_sizes.append(size)
                    if not grew:
                        grew = True
                        with unrequested.open("ab") as growth:
                            growth.write(b"y" * (8 * 1024 * 1024))
                body = self._stream.read(size)  # type: ignore[attr-defined]
                if path == unrequested:
                    read_bytes += len(body)
                return body

        return GrowingReadTracker(stream, [])

    monkeypatch.setattr(service_module.os, "open", tracked_open)
    monkeypatch.setattr(service_module.os, "fdopen", tracked_fdopen)
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("z-requested.py",), byte_budget=1024
        )
    assert captured.value.code == "INDEX_STALE"
    assert read_sizes
    assert all(0 < size <= service_module._READ_CHUNK_SIZE for size in read_sizes)
    assert read_bytes <= 2
    service.close()


def test_stream_workspace_file_closes_raw_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"x")
    original_open = service_module.os.open
    original_close = service_module.os.close
    opened_descriptors: list[int] = []
    closed_descriptors: list[int] = []

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        opened_descriptors.append(descriptor)
        return descriptor

    def failing_fdopen(*args: object, **kwargs: object) -> _TrackedReader:
        raise OSError("injected fdopen failure")

    def tracked_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(service_module.os, "open", tracked_open)
    monkeypatch.setattr(service_module.os, "fdopen", failing_fdopen)
    monkeypatch.setattr(service_module.os, "close", tracked_close)
    with pytest.raises(IndexError) as captured:
        service_module._stream_workspace_file(tmp_path, source, False, 1)
    assert captured.value.code == "INDEX_STALE"
    assert opened_descriptors == closed_descriptors


def test_snapshot_file_reader_ignores_its_custom_database_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "module.py").write_bytes(b"VALUE = 1\n")
    database = workspace / "custom-reader-cache.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(workspace)
    snapshot = service.sync(workspace_id)
    assert tuple(
        path
        for path, _ in service.snapshot_facts(
            workspace_id, snapshot.snapshot_id
        ).file_hashes
    ) == ("module.py",)
    assert (
        service.read_snapshot_files(
            workspace_id, snapshot.snapshot_id, ("module.py",), byte_budget=64
        )[0].body
        == b"VALUE = 1\n"
    )
    service.close()


def test_snapshot_file_size_preflight_rejects_same_size_replacement_before_body_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_bytes(b"safe")
    replacement = workspace / "replacement.py"
    replacement.write_bytes(b"evil")
    original_open = service_module.os.open
    replaced = False

    def replace_before_open(
        path: str | os.PathLike[str], flags: int, *args: int
    ) -> int:
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            os.replace(replacement, target)
        return original_open(path, flags, *args)

    monkeypatch.setattr(service_module.os, "open", replace_before_open)
    with pytest.raises(IndexError) as captured:
        service_module._prevalidate_requested_file_sizes(workspace, ("module.py",), 64)
    assert captured.value.code == "INDEX_STALE"


def test_snapshot_file_size_preflight_rejects_symlink_replacement_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_bytes(b"safe")
    external = tmp_path / "external.py"
    external.write_bytes(b"evil")
    original_open = service_module.os.open
    replaced = False

    def replace_with_link(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            target.unlink()
            target.symlink_to(external)
        return original_open(path, flags, *args)

    monkeypatch.setattr(service_module.os, "open", replace_with_link)
    try:
        with pytest.raises(IndexError) as captured:
            service_module._prevalidate_requested_file_sizes(
                workspace, ("module.py",), 64
            )
    except OSError as exc:
        pytest.skip(f"link capability unavailable: {exc}")
    assert captured.value.code == "INDEX_STALE"
