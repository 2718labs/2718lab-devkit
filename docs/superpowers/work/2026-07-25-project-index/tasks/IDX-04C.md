# IDX-04C Read-Only Strict Cards

Owner: sol-ultra-validator-fix
Depends on: IDX-04

## Goal

Let layered work packages represent read-only Luna/Terra tasks without applying
write-only checkpoint and output-diff gates.

## Context

- `Write Scope: - none` currently fails shape validation.
- The strict runtime contract requires query receipt plus verification for
  read-only tasks, but no checkpoint or indexed output diff.

## Write Scope

- `skills/work-methodology/scripts/validate_work_package.py`
- `skills/work-methodology/tests/test_work_package.py`

## Steps

1. Add failing tests for legacy and strict read-only cards.
2. Accept explicit `none` while still rejecting a missing scope section.
3. Apply the read-only strict marker sequence separately from write tasks.

## Acceptance

Read-only Terra/Luna cards pass with query, verification, and completion gates;
write cards retain the full checkpoint/output sequence.

## Return

Changed files, RED/GREEN output, and blockers.
