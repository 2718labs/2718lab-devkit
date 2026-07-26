# Bugkiller Policy Contract

## Bug States

`NEW -> TRIAGED -> REPRODUCING -> LOCALIZING -> DESIGNING -> PATCHING -> VERIFYING -> DONE`

Conditional states: `HUMAN_GATE`, `REVIEWING`, `BLOCKED`, `FAILED`, `CANCELLED`, `ABANDONED`, `ROLLED_BACK`.

Low-risk verification reaches `DONE` directly. `REVIEWING` is reachable only after a dangerous user gate authorizes it.

## Model Routing

- Luna: triage, repository map, history and evidence organization; read-only.
- Terra: reproduction, localization, design, the only patch writer, and verification.
- Sol: read-only dangerous escalation after user approval; default budget zero, one call per approved escalation.
- Luna unavailable: low-risk Terra fallback marked `DEGRADED_TRIAGE`; high-risk blocks.
- Terra unavailable: block; Luna never writes code.

## Risk Triggers

Authentication, authorization, credentials, cryptography, payments, privacy, data deletion/migration, remote execution, supply chain, CI/release, public network exposure, evidence conflict, or two failed patch rounds.

## Language Profiles

- Python: project manifests/locks; existing `python -m pytest`; no silent dependency install.
- JS/TS: one unambiguous lockfile; declared test/lint/typecheck scripts; lifecycle scripts are tainted.
- Rust: Cargo manifests/lock; isolated `CARGO_TARGET_DIR`.
- Go: go.mod/sum/work; target test then `go test ./...`.

Commands are structured argv/cwd/env/network/timeout specs, never shell strings extracted from repository text.
