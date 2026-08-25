# Code Atlas Status Contract

Treat every Code Atlas status as a bounded next action, never as authority to
broaden a task or query an external service.

| Status | Action |
| --- | --- |
| `READY` | Use the local verified recipe within the assigned scope and verify it. |
| `NO_VERIFIED_RECIPE` | Perform one normal scoped implementation, preserve evidence, then allow later automatic local ingestion. Do not fall back to an external CodeGraph, LLM, or vector service. |
| `INDEX_STALE` | Refresh the approved local index before using a recipe. |
| `AMBIGUOUS_MATCH` | Stop and ask the coordinator to select or narrow the contract; an independently routed Sol lane may advise when required. |
| `UNSUPPORTED_LANGUAGE` | Record the gap and keep the task scoped; do not invent a recipe. |
| `RENDER_INVALID` | Reject the candidate, keep evidence, and correct the local contract. |
| `EVIDENCE_INCOMPLETE` | Collect bounded local evidence before claiming a verified recipe. |
| `RECIPE_QUARANTINED` | Do not use the recipe; preserve the quarantine reason. |
| `INGEST_PENDING` | Keep the accepted implementation evidence for later local ingestion. |
| `ATLAS_UNAVAILABLE` | Use the task card's scoped fallback and record the degraded state. |
| `MODEL_UNAVAILABLE` | Do not substitute a model; return the routing result to the coordinator. |

Only the coordinator accepts a completed task. A worker's local test pass or
recipe result is verification evidence, not integration or release approval.
