# IDX-01 Deterministic Project Index Core

Owner: sol-ultra-core
Depends on: none

## Goal

Implement the immutable deterministic project graph and bounded query service.

## Context

- Read `../contracts/project-index-api.md` only.
- Follow existing Python style under `mcp-tools/orchestrator/`.
- Facts must come from parsers and hashes, never summaries or embeddings.

## Write Scope

- `mcp-tools/project_index/__init__.py`
- `mcp-tools/project_index/models.py`
- `mcp-tools/project_index/extractors.py`
- `mcp-tools/project_index/store.py`
- `mcp-tools/project_index/service.py`
- `mcp-tools/tests/test_project_index_core.py`

## Steps

1. Write pytest function tests for deterministic snapshots and ids, incremental
   blob reuse, Python symbols/imports/tests, Markdown explicit structure,
   JSON/TOML/config keys, conservative YAML gaps, unsupported parser gaps,
   lexical/graph/impact queries, bounded source windows, truncation, and stale
   hash rejection.
2. Run only the new test file and record the expected RED caused by the absent
   package or behavior.
3. Implement the minimum standard-library index, immutable SQLite schema, and
   service API in the shared contract.
4. Run the new test file, then all existing `mcp-tools/tests`.
5. Do not modify checkpoint, orchestrator, server, skill, agent, or docs files.

## Acceptance

With `TEMP`, `TMP`, `TMPDIR`, and `CODEX_TASK_TEMP` set below the task root:

`python -m pytest -q mcp-tools/tests/test_project_index_core.py`

The module passes; identical inputs produce identical snapshots; stale reads
return stable errors; no node or edge accepts model-authored content.

## Return

Changed files, RED output, GREEN output, public API deviations (ideally none),
and blockers. Do not commit, push, or create a PR.
