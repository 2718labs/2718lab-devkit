---
name: code-atlas
description: Use for an explicitly scoped 2718lab DevKit implementation that needs a deterministic local recipe lookup, host-routing decision, or evidence-backed Code Atlas handoff; never load it for an unrelated project.
---

# Code Atlas

范围门：只查询当前 DevKit 项目的本地证据；不跨项目复用任务、索引、缓存或路由结果。

Use Code Atlas as a local, evidence-backed planning aid. It does not call a
model, a network service, a vector database, or an external CodeGraph.

1. Read the assigned card and its direct contracts.
2. Resolve the host role from reported capabilities with `code_atlas.routing`.
3. Prepare a local recipe and follow its returned status action.
4. Keep implementation, verification, and later ingestion as scoped evidence.

Detailed rules live in [host routing](references/host-routing.md),
[status contracts](references/status-contract.md), and the
[operational workflow](references/atlas-workflow.md).
