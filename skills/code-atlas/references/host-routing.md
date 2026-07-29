# Host Routing Contract

`mcp-tools/code_atlas/routing.py` is a pure policy resolver. It consumes a
request plus a host-reported capability record, returns a canonical
`RoutingResult`, and never spawns, invokes, substitutes, or contacts anything.
Its `attempts` field contains actual dispatch attempts; the resolver itself
always returns an empty tuple.

## Capability input

Pass either one host report or a mapping keyed by host. A direct report has
`host` and a `models` map whose model entries list supported `reasoning`
levels. Missing hosts, roles, models, or reasoning levels fail closed as a
stable rejected or unavailable result. Policy assets are not capability claims.

## Current policy

| Host | Request | Effective model / reasoning | Rule |
| --- | --- | --- | --- |
| Codex | coordinator / Sol | `gpt-5.6-sol` / `high` | Sol coordinates architecture, dispatch, review, integration, and acceptance. |
| Codex | normal code / Terra High | `gpt-5.6-terra` / `high` | Routine, bounded implementation, tests, debugging, docs, and validation. |
| Codex | complex code / Terra Max | `gpt-5.6-terra` / `max` | Moderate-or-harder implementation, integration, refactoring, security work, and regressions. |
| Codex | exceptional bounded code / Sol High | `gpt-5.6-sol` / `high` | Explicit exceptional execution or deep investigation only; Sol still owns final acceptance. |
| Codex | Luna | unavailable | Never attempt a Luna spawn and never label a substitute as Luna. |
| Claude | coordinator | Opus / coordinator | Coordinator profile. |
| Claude | code | Sonnet / standard | Code-worker profile. |
| Claude | light | Haiku / light | Light-worker profile. |
| Claude | Fable | Fable / high | Powerful, expensive escalation only with a non-empty explicit escalation reason. |

The resolver rejects a caller-supplied model or reasoning value that differs
from policy. It never silently downgrades or upgrades a route.

## Result shape

`RoutingResult.to_dict()` uses stable fields: `status`, all requested and
effective host/role/model/reasoning fields, `attempts`, and `reason`.
`resolved` means only that a reported capability exactly matches policy; it is
not a spawn receipt or acceptance decision.
