# IDX-04 Index-First Agent Routing

Owner: sol-ultra-policy-tests
Depends on: IDX-03, IDX-04A

## Goal

Create mechanical RED assertions and validators for strict index-first routing
and Sol-ultra-only code writes.

## Context

- Read `../evidence/skill-routing-red.md` and the IDX-04A audit result.
- Preserve legacy `strict_index=false` compatibility.
- Luna/Terra handle mechanical index work, investigation, documentation, and
  read-only verification; they never write code.
- A Sol code writer is distinct from optional dangerous review.

## Write Scope

- `skills/bugkiller/scripts/validate_bugkiller.py`
- `skills/work-methodology/scripts/validate_work_package.py`
- `skills/work-methodology/tests/*.py`
- `mcp-tools/tests/test_bugkiller_assets.py`
- `mcp-tools/tests/test_bugkiller_metadata.py`

## Steps

1. Add mechanical RED assertions and validator rules for the routing contract.
2. Update the mechanical validators without editing policy Markdown.
3. Run the focused suite and retain the expected RED output for IDX-04B.

## Acceptance

Focused tests fail only because the old policy Markdown and agent assets still
encode Terra patch writing or omit index-first gates.

## Return

Changed files, RED/GREEN output, validation evidence, and blockers.
