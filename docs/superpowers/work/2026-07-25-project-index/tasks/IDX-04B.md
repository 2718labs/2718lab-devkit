# IDX-04B Index-First Policy Text

Owner: terra-routing-docs
Depends on: IDX-04

## Goal

Patch the bounded Bugkiller and methodology text so the IDX-04 mechanical
contract turns green.

## Context

- Read `../evidence/skill-routing-red.md` and the IDX-04 RED output only.
- Preserve legacy `strict_index=false` compatibility.
- Code writers and optional dangerous reviewers are separate roles.

## Write Scope

- `agents/bugkiller-*.md`
- `skills/bugkiller/SKILL.md`
- `skills/bugkiller/references/*.md`
- `skills/work-methodology/SKILL.md`
- `skills/work-methodology/references/*.md`
- `README.md`

## Steps

1. Replace Terra patch-writer text with mechanical/docs-only routing.
2. Add the explicit `gpt-5.6-sol` + `ultra` code-writer contract.
3. Add strict index query/checkpoint/output/verification gates without
   weakening linear/DAG, peer messaging, or danger approval rules.

## Acceptance

IDX-04 tests and both validators pass; no code file is modified by this card.

## Return

Changed Markdown files, focused GREEN output, and blockers.
