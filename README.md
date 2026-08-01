[简体中文](README.zh-CN.md)

# 2718lab DevKit — Code Atlas v0.3.0 release-candidate contract

`2718lab-devkit` is a local development toolkit for Codex and Claude-hosted workflows. This document describes frozen **v0.3.0 release-candidate metadata and contract**, not an activated Code Atlas release. Sol still owns integration and final end-to-end acceptance; it does not claim this version has been published, installed, hot-reloaded, or passed final E2E verification.

## Overview

Code Atlas turns bounded, accepted engineering evidence into deterministic local graph knowledge: graph-shaped nodes, edges, recipes, and content-addressed blobs (CAS). It helps a worker prepare and render a scoped implementation packet without asking an LLM to rediscover routine repository structure.

It is deliberately not an autonomous coding system. A task card, declared write scope, review, and verification remain authoritative. Sol owns the single design pass when evidence is insufficient, plus dispatch, review, integration, and final acceptance; it does not need to pre-decompose every verified Atlas task.

## Installation status

v0.3.0 is a release candidate. No remote publication or successful installation is asserted here. The Code Atlas MCP activation remains gated on ATLAS-11 registration and ATLAS-13 end-to-end acceptance. Once Sol accepts a published release, the local marketplace identifier remains `2718lab-devkit@pidan-local-plugins`; verify the installed artifact in a new Codex task rather than assuming source changes hot-reload.

## Four frozen Code Atlas interface names — ATLAS-11 activation gate

The following names define the frozen v0.3 interface contract. They are **not currently registered by the FastMCP tool set**, cannot be called from it, and do not activate automatic ingestion. ATLAS-11 must register and contract-test them before this interface becomes callable; ATLAS-13 must then pass final E2E acceptance.

| Tool | Purpose | Safety boundary |
| --- | --- | --- |
| `code_atlas_graph_query` | Contract: read a bounded local graph neighbourhood after activation. | Would return graph records and hashes, never an unbounded source dump. |
| `code_atlas_prepare` | Contract: match a verified local recipe and prepare an implementation packet after activation. | Would return a status and evidence-bound packet; it would not edit a workspace. |
| `code_atlas_render` | Contract: render a deterministic patch candidate from a valid packet and bindings after activation. | Would return a candidate and inert test specifications; it would not apply a patch or run commands. |
| `workflow_accept_code_task` | Contract: accept a coordinator-authorized code-task result and queue local Atlas ingestion after activation. | Would remain authority- and evidence-bound; workers could not self-accept. |

The status contract below is frozen now, but the related MCP interface remains pending ATLAS-11 registration.

| Status | Exact action |
| --- | --- |
| `READY` | Use the local verified recipe within the assigned scope and verify it. |
| `NO_VERIFIED_RECIPE` | Perform one normal scoped implementation, preserve evidence, then allow later automatic local ingestion. Do not fall back to an external CodeGraph, LLM, or vector service. |
| `INDEX_STALE` | Refresh the approved local index before using a recipe. |
| `AMBIGUOUS_MATCH` | Stop and ask Sol to select or narrow the contract. |
| `UNSUPPORTED_LANGUAGE` | Record the gap and keep the task scoped; do not invent a recipe. |
| `RENDER_INVALID` | Reject the candidate, keep evidence, and correct the local contract. |
| `EVIDENCE_INCOMPLETE` | Collect bounded local evidence before claiming a verified recipe. |
| `RECIPE_QUARANTINED` | Do not use the recipe; preserve the quarantine reason. |
| `INGEST_PENDING` | Keep the accepted implementation evidence for later local ingestion. |
| `ATLAS_UNAVAILABLE` | Use the task card's scoped fallback and record the degraded state. |
| `MODEL_UNAVAILABLE` | Do not substitute a model; return the routing result to Sol. |

## Deterministic local graph and recipes

The graph contains typed knowledge nodes and explicit edges for local recipes, constraints, dependencies, test specifications, evidence descriptors, and accepted task episodes. Recipes and binary/template assets are referenced by canonical SHA-256 content addresses in the local CAS. Queries, matching, rendering, canonicalization, and conflict handling are deterministic and bounded by node, edge, depth, and byte budgets.

Code Atlas uses **no LLM, embeddings, vector database, network service, or external CodeGraph**. It never falls back to an external CodeGraph. It is a local planning and reuse aid, not a model router, shell executor, patch applier, or hidden dispatcher.

## Planned automatic accepted-task ingestion

After ATLAS-11 registration and ATLAS-13 E2E acceptance, the frozen `workflow_accept_code_task` contract would write a durable acceptance/outbox record and queue deterministic local projection. Projection would record a redacted `TaskEpisode` node. Recipe creation and CAS storage would occur only when strict reuse, privacy, and evidence gates pass; otherwise the result would remain episode-only. Repeated identical acceptance would be idempotent, while a conflicting payload would be rejected rather than silently replacing knowledge.

This planned flow preserves useful graph history even when a task is not safe or general enough to become a reusable recipe. If activated, automatic ingestion must never store raw source, raw command output, credentials, or an arbitrary execution transcript.

## Atlas-derived work packages and maximal safe waves

For a verified, sufficiently structured Code Atlas `ImplementationPacket`/`TaskEpisode` graph, the scheduler deterministically derives execution units, bounded write scopes, direct contract hashes, dependencies, a registration plan, and the maximal safe parallel waves. Ready units whose write scopes and direct contracts do not conflict may run together; same-path or contract-conflicting work queues behind its active owner. This local derivation does not use an LLM, a vector system, or an external CodeGraph.

Only when the evidence is unknown or insufficient is the task marked `needs_design` and handed to Sol for one design pass; it is not guessed into parallel execution. After the pending activation gates, an authorized accepted pattern that passes the strict reuse, privacy, and evidence gates may be ingested into the local graph and reused by the same deterministic derivation.

The local GitHub-style path is:

`task card + base revision → isolated worktree/branch → scoped commit + evidence → Sol review → ordered integration/rebase → CI gate → release gate`

Workers do not merge peers, extend their own write scope, or declare an unreviewed branch accepted. Durable peer handoff uses `workflow_artifact_register → workflow_message_send → workflow_inbox → workflow_artifact_resolve → workflow_message_ack`; chat may wake a worker but is not the source of truth.

## Todo, status, and crash resume

The deterministic team helper provides bounded `bootstrap`, `status`, `resume-packet`, `contract-check`, and `cache-key` operations. Todo/status views distinguish `pending_init`, `running`, `blocked`, and `done`, so queued initialization is not presented as active parallel work. Resume packets contain only bounded, redacted identifiers, lease/endpoint state, evidence hashes, candidate/base commits, and the next action.

After a host interruption, rebind the current endpoint and lease, inspect the durable inbox and artifact references, validate the resume packet, and continue from the next authorized action. Never reconstruct authority from chat history or raw logs.

## Privacy, data roots, and safe degradation

Runtime data stays local. Code Atlas and workflow evidence use the configured plugin data root; task scratch files, worktrees, caches, and test evidence are kept under `D:\bun\tmp\codex\<project-or-thread>`, never a C-drive temporary root. Records are canonical, size-bounded, redacted, and content-addressed.

If an index is stale, evidence is incomplete, a contract mismatches, a host is unavailable, or a recipe is quarantined, the system fails closed or records a scoped degraded state. It does not execute arbitrary commands, reveal secrets, silently substitute a model, or contact an external service.

## Current Codex roles

The checked-in host profile is the routing authority. When a declared host capability
cannot satisfy an exact route, the routing result is `UNAVAILABLE` with
`capability_unavailable`; an explicit route/profile conflict is `REJECTED`. The runtime
does not silently substitute a model or reasoning level.

| Role | Current responsibility |
| --- | --- |
| Sol main | `gpt-5.6-sol` at `high` for coordination: design, decomposition, dispatch, review, integration, and final acceptance. |
| Terra High | `gpt-5.6-terra` at `high` for routine bounded coding, testing, debugging, documentation, and auxiliary validation. |
| Terra Max | `gpt-5.6-terra` at `max` for moderate-or-harder implementation, integration, refactoring, security-sensitive execution, and difficult regressions. |
| Sol High | `gpt-5.6-sol` at `high`, optional only for exceptional bounded execution or deep investigation; Sol main still owns acceptance. |
| Luna | `gpt-5.6-luna` at the requested `low`, `medium`, `high`, or `xhigh` effort only when the current Codex host capability report attests that exact pair; otherwise the route is unavailable and no substitute is labelled Luna. An omitted effort uses the profile's `medium` default only when attested. This shared Codex profile does not migrate Bugkiller, which remains a separate policy surface. |

## Claude roles

| Family | Current responsibility |
| --- | --- |
| Opus | Coordinator at the profile's `coordinator` reasoning level. |
| Sonnet | Regular code execution at `standard`. |
| Haiku | Light tasks at `light`. |
| Fable | `high`-reasoning, expensive explicit escalation only; it is never an automatic default and requires an explicit non-empty escalation reason. |

## Recovery and verification

Recovery is evidence-led: preserve the task card, scoped branch, candidate commit, acceptance/outbox state, artifact hashes, and bounded resume packet. Re-run the relevant focused checks after resume and before integration. An exact verification-cache key may avoid repeating an unchanged non-core lane, but core verification never accepts partial or unknown fingerprints.

Before release, Sol verifies contract compatibility, task/lease authority, privacy/redaction boundaries, scoped diffs, targeted tests, static checks, ordered integration, and the final E2E cycle. This documentation does not claim those final checks have passed.

## Limitations

- Code Atlas is local and deterministic; it has no LLM, vector, network, or external CodeGraph fallback.
- A rendered patch is a candidate, not an applied change or approval to run it.
- A recipe is reusable only after strict evidence and privacy gates; many accepted tasks correctly remain episode-only.
- Task decomposition maximizes only non-conflicting work. Ambiguous or overlapping scopes require design or queueing.
- This release-candidate guide is not a publication, installation, hot-reload, or final-E2E claim.

## License

[AGPL-3.0](LICENSE).
