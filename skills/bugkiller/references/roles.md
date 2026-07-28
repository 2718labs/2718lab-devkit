# Roles and Routing

At dispatch, the coordinator inspects the `spawn` tool's exposed model choices. If Luna is available, explicitly select Luna for read-only triage. Explicitly select Terra for investigator, documentation writer, verifier, and low-risk fallback triage. Luna/Terra never write code. Dispatch `bugkiller-sol-code-writer` with `gpt-5.6-sol` and `ultra` for the exact approved executable write scope. If Luna is unavailable, explicitly select Terra only for low-risk fallback triage and record `DEGRADED_TRIAGE`; high-risk triage blocks. Plugin agent Markdown defines roles and boundaries; code-writer dispatch parameters do not authorize metadata changes.

| Role | Scope | Write access |
| --- | --- | --- |
| Luna triage | triage, repository map, history, evidence organization | read-only |
| Terra investigator | reproduction, localization, design evidence | read-only |
| Terra documentation writer (`bugkiller-terra-doc-writer`) | approved documentation-only update | documentation only; never code |
| Terra verifier | targeted and required verification evidence | read-only |
| Sol code writer (`bugkiller-sol-code-writer`) | approved executable write scope | code writer; dispatch `gpt-5.6-sol` with `ultra` |
| Sol escalation (`bugkiller-sol-escalation`) | dangerous, user-approved evidence review | read-only |

Route low-risk work through Luna triage where it is available. When Terra is unavailable, block; Luna never writes code.

Risk triggers include authentication, authorization, credentials, cryptography, payments, privacy, deletion or migration, remote execution, supply chain, CI or release, public network exposure, conflicting evidence, and two failed patch rounds.

Ordinary work does not automatically request a reviewer. `bugkiller-sol-escalation` starts with `budget: 0`; only a dangerous user approval permits `budget: 1` and exactly one read-only Sol escalation call. It is separate from the Sol code writer and cannot patch, claim another task, inspect unrelated cards, or expand the writer's scope.
