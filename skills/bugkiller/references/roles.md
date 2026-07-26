# Roles and Routing

Bugkiller selects from the shared DevKit role catalog; it does not define a
private agent team. At dispatch, the coordinator inspects the `spawn` tool's
exposed model choices. If Luna is available, explicitly select Luna for
`2718lab-triage`. Explicitly select Terra for `2718lab-investigator`,
`2718lab-doc-writer`, `2718lab-verifier`, and low-risk fallback triage.
These roles never write executable code. Dispatch `2718lab-code-writer` with
`gpt-5.6-sol` and `ultra` for the exact approved executable write scope.
If Luna is unavailable, explicitly select Terra only for low-risk fallback
triage and record `DEGRADED_TRIAGE`; high-risk triage blocks. Shared agent
Markdown defines permissions; Bugkiller adds only defect-specific states and
risk routing.

| Role | Scope | Write access |
| --- | --- | --- |
| `2718lab-triage` | triage, repository map, history, evidence organization | read-only |
| `2718lab-investigator` | reproduction, localization, design evidence | read-only |
| `2718lab-doc-writer` | approved documentation-only update | documentation only; never code |
| `2718lab-verifier` | targeted and required verification evidence | read-only |
| `2718lab-code-writer` | approved executable write scope | code writer; dispatch `gpt-5.6-sol` with `ultra` |
| `2718lab-risk-reviewer` | dangerous, user-approved evidence review | read-only |

Route low-risk work through `2718lab-triage` where it is available. When the
required investigation or verification model is unavailable, block; read-only
roles never write code.

Risk triggers include authentication, authorization, credentials, cryptography, payments, privacy, deletion or migration, remote execution, supply chain, CI or release, public network exposure, conflicting evidence, and two failed patch rounds.

Ordinary work does not automatically request a reviewer.
`2718lab-risk-reviewer` starts with `budget: 0`; only a dangerous user approval
permits `budget: 1` and exactly one read-only review call. It is separate from
`2718lab-code-writer` and cannot patch, claim another task, inspect unrelated
cards, or expand the writer's scope.
