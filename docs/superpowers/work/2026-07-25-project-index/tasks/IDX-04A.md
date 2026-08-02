# IDX-04A Index-First Routing Audit

Owner: terra-routing-audit
Depends on: none

## Goal

Produce a compact, evidence-backed list of stale role and workflow text that
must change for the index-first team model.

## Context

- Use a fresh self-index database below
  `D:/bun/tmp/codex/bugkiller-plugin/routing-audit`.
- Query only `skills/bugkiller`, `skills/work-methodology`, `agents`, and
  `README.md`.
- Required policy: Luna/Terra for mechanical/read-only work; only a host-
  selected Sol ultra subagent may write code; ordinary tasks have no automatic
  reviewer; simple work uses a state machine and complex work a DAG; peer
  delivery is direct and artifact-backed; strict work is index-first.

## Write Scope

- none

## Steps

1. Sync and query the bounded paths through `ProjectIndexService`.
2. Return exact file/line/symbol locations and replacement intent only.
3. Do not edit files, run a reviewer, or inspect unrelated skills.

## Acceptance

The coordinator can dispatch one bounded documentation patch without rereading
the full plugin.

## Return

Snapshot id, query trace ids, stale locations, and any coverage gaps.
