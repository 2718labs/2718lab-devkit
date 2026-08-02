# Roles and Routing

Sol is the primary coordinator: architecture, task decomposition, dispatch,
review, integration, and final acceptance. A task's explicit card and
host-reported capabilities determine routing; no dispatcher may guess a host
capability or substitute a model/reasoning level.

| Role | Model / reasoning | Scope |
| --- | --- | --- |
| Sol coordinator (`bugkiller-sol-coordinator`) | `gpt-5.6-sol` / `high` | Design, dispatch, review, ordered integration, and acceptance. |
| Terra High (`bugkiller-terra-investigator`) | `gpt-5.6-terra` / `high` | Routine/bounded coding, tests, debugging, documentation, investigation, and validation. |
| Terra Max | `gpt-5.6-terra` / `max` | Moderately complex/harder implementation, integration, refactoring, security-sensitive work, and difficult regressions. |
| Sol High (`bugkiller-sol-escalation`) | `gpt-5.6-sol` / `high` | Explicit exceptional bounded execution or deep investigation. |
| Luna | unavailable | Do not spawn Luna and do not label a substitute as Luna. |
Workers accept only their exact scope and return a candidate commit plus
evidence to Sol. No execution worker may accept its own task or broaden write
scope. Parallel tasks require disjoint active scopes; same-path work queues
behind the active owner.

`spawn_agent` exposes available Codex host model choices, but availability is a
reported capability fact rather than authorization. Luna's unavailable state
is final for the current route.
