# 2718lab Project Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans. Steps use checkbox syntax for tracking.

**Goal:** Add a deterministic unified project graph, strict index-first workflow gates, and task-owned checkpoint restore to `2718lab-tools`.

**Architecture:** Keep workflow control in `orchestrator.sqlite3`; store immutable graph snapshots in `project-index.sqlite3`; store checkpoint file bodies in a local CAS. Expose six general MCP tools and extend strict workflow completion without breaking existing non-strict tasks.

**Tech Stack:** Python standard library, SQLite/FTS5, official MCP Python SDK v1, pytest/unittest compatibility.

---

### Task 1: Core Project Index

**Files:**
- Create: `mcp-tools/project_index/models.py`
- Create: `mcp-tools/project_index/extractors.py`
- Create: `mcp-tools/project_index/store.py`
- Create: `mcp-tools/project_index/service.py`
- Test: `mcp-tools/tests/test_project_index_core.py`

- [ ] Write tests for deterministic snapshots, Python/Markdown/config nodes,
  exact provenance, lexical queries, graph expansion, impact lookup, stale hash
  rejection, parser gaps, and bounded source windows.
- [ ] Run the new test module and confirm it fails because the package is absent.
- [ ] Implement the minimal typed graph, adapters, immutable store, and service.
- [ ] Run the new tests and the existing orchestrator suite.

### Task 2: Task-Owned Checkpoints

**Files:**
- Create: `mcp-tools/project_index/checkpoints.py`
- Test: `mcp-tools/tests/test_project_index_checkpoints.py`

- [ ] Write fixture-repository tests for capture, add/change/delete restore,
  unowned workspace rejection, scope escape, symlink/reparse rejection, drift,
  rescue checkpoint, and idempotent status.
- [ ] Run the module and confirm the expected missing implementation failures.
- [ ] Implement scope manifests and content-addressed checkpoint blobs without
  `git reset`, `git checkout`, or writes to the original workspace.
- [ ] Run checkpoint and core-index tests.

### Task 3: Strict Workflow Gate

**Files:**
- Modify: `mcp-tools/orchestrator/store.py`
- Modify: `mcp-tools/orchestrator/service.py`
- Modify: `mcp-tools/server.py`
- Test: `mcp-tools/tests/test_project_index_workflow.py`
- Modify: `mcp-tools/tests/test_mcp_contract.py`

- [ ] Write failing tests for task/snapshot binding, query receipts, strict claim,
  strict completion, non-strict compatibility, and the six MCP tool schemas.
- [ ] Add a safe schema migration and strict-gate service operations.
- [ ] Wire `project_index_sync/status/query` and
  `worktree_checkpoint_create/status/restore` with FastMCP v1 decorators.
- [ ] Verify stale/error envelopes never expose tracebacks or source bodies.

### Task 4: Index-First Agent Workflow

**Files:**
- Modify: `skills/bugkiller/SKILL.md`
- Modify: `skills/bugkiller/references/workflow.md`
- Modify: `skills/work-methodology/SKILL.md`
- Create: `skills/work-methodology/references/project-index-runtime.md`
- Modify: `agents/bugkiller-*.md`
- Modify: `README.md`
- Test: `mcp-tools/tests/test_bugkiller_assets.py`
- Test: `skills/work-methodology/tests/test_runtime_orchestration.py`

- [ ] Add failing policy tests for index bootstrap, strict query/checkpoint flow,
  explicit partial/stale states, and no model-authored graph facts.
- [ ] Update concise skills and put detailed rules in one focused reference.
- [ ] Keep ordinary work free of automatic reviewer or Sol routing.

### Task 5: Distribution And Verification

**Files:**
- Modify: `.codex-plugin/plugin.json` through the cachebuster helper.

- [ ] Run the complete MCP and methodology tests with D-drive task temp.
- [ ] Run Bugkiller, MCP, and Codex plugin validators.
- [ ] Sync the exact files to long-term and marketplace sources.
- [ ] Reinstall `2718lab-devkit@<marketplace-name>` and repeat verification from
  the installed cache.
- [ ] Do not commit, push, create a PR, or publish a separate repository.
