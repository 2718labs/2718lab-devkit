"""Focused Project Index Host material contract tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from devkit_runtime.bootstrap import RuntimeBootstrap  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from devkit_runtime.project_checkpoint import open_project_checkpoint_rw  # noqa: E402
from devkit_runtime.tool_result import _query_data  # noqa: E402


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_query_attestation_hashes_exact_public_projection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text(
        "def alpha():\n    return 1\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Index Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "config",
            "user.email",
            "index@example.invalid",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(workspace), "add", "sample.py"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "--quiet", "-m", "fixture"],
        check=True,
    )

    data_root = tmp_path / "runtime-data"
    scratch_root = tmp_path / "runtime-scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(data_root), "CODEX_TASK_TEMP": str(scratch_root)}
    )
    RuntimeBootstrap.run(config)
    checkpoint = open_project_checkpoint_rw(
        config.project_index_database,
        config.checkpoint_cas_root,
        scratch_root=config.scratch_root,
    )
    service = checkpoint.project_index
    try:
        workspace_id = service.project_index_register(workspace)
        register_material = service.host_attestation_material(workspace_id)
        assert service.project_index_register(workspace) == workspace_id
        assert service.host_attestation_material(workspace_id) == register_material
        snapshot = service.sync(workspace_id)
        result = service.query(
            workspace_id,
            snapshot.snapshot_id,
            "alpha",
            source_lines=2,
            byte_budget=4096,
        )
        receipt = service.get_query_receipt(result.trace_id)
        material = service.host_attestation_material(
            workspace_id,
            snapshot_id=snapshot.snapshot_id,
            trace_id=result.trace_id,
        )

        projection = _query_data(result)
        projection.pop("state")
        projection["workspace_id"] = workspace_id
        assert material["index_context_hash"] == _hash(projection)
        assert material["query_receipt_hash"] == _hash(
            {
                "schema": "2718lab-devkit/project-index-query-receipt-binding-v1",
                "receipt": asdict(receipt),
                "index_context_hash": material["index_context_hash"],
            }
        )
    finally:
        checkpoint.close()
