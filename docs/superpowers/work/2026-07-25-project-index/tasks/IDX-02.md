# IDX-02 Task-Owned Checkpoints

Owner: sol-ultra-checkpoint
Depends on: none

## Goal

Implement scoped content-addressed checkpoints and drift-safe restore for
plugin-registered linked Git worktrees.

## Context

- Read `../contracts/project-index-api.md` only.
- Treat the core API in that contract as fixed; coordinate directly with the
  core owner if a concrete type detail is needed.
- Restore safety takes priority over convenience.

## Write Scope

- `mcp-tools/project_index/checkpoints.py`
- `mcp-tools/tests/test_project_index_checkpoints.py`

## Steps

1. Write pytest function tests that create a real temporary Git repository and
   linked worktree, then cover capture, add/change/delete restore, reopen,
   idempotent status, unowned/original workspace rejection, scope escape,
   symlink/reparse rejection, current-tree drift with zero writes, automatic
   rescue checkpoint, and exact byte restoration.
2. Run only the new test file and record the expected RED.
3. Implement CAS blobs and metadata using only standard-library APIs and
   structured `git` argv calls needed for ownership checks. Never use shell
   strings, `git reset`, or `git checkout`.
4. Run checkpoint tests and the core test module when it becomes available.
5. Do not modify core, orchestrator, server, skill, agent, or docs files.

## Acceptance

With all temp variables on the task D-drive root:

`python -m pytest -q mcp-tools/tests/test_project_index_checkpoints.py`

All paths remain within the registered scope; drift writes zero workspace
bytes; a successful restore creates a rescue checkpoint first and restores the
target manifest byte-for-byte.

## Return

Changed files, RED output, GREEN output, safety decisions, and blockers. Do not
commit, push, or create a PR.
