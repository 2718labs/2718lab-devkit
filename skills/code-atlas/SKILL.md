---
name: code-atlas
description: Use when a scoped implementation needs a deterministic local recipe lookup, host-routing decision, or evidence-backed Code Atlas handoff.
---

# Code Atlas

Use Code Atlas as a local, evidence-backed planning aid. It does not call a
model, a network service, a vector database, or an external CodeGraph.

1. Read the assigned card and its direct contracts.
2. Resolve the host role from reported capabilities with `code_atlas.routing`.
3. Prepare a local recipe and follow its returned status action.
4. Keep implementation, verification, and later ingestion as scoped evidence.

Detailed rules live in [host routing](references/host-routing.md),
[status contracts](references/status-contract.md), and the
[operational workflow](references/atlas-workflow.md).
