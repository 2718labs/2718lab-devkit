# IDX-01A Provenance And Real Incremental Parsing

Owner: sol-ultra-index-hardening
Depends on: IDX-01

## Goal

Harden the core so every fact is auditable and unchanged blobs are genuinely
reused without rerunning their parser.

## Context

- Read `../contracts/project-index-api.md` only.
- Current targeted and full tests are green, but review found that node/edge
  records omit explicit extractor/version/provenance/byte span, Markdown omits
  structured work-package blocks, queries are not persisted, and
  `reused_blob_count` does not yet prevent reparsing.
- Preserve the public APIs already consumed by checkpoint code.

## Write Scope

- `mcp-tools/project_index/__init__.py`
- `mcp-tools/project_index/models.py`
- `mcp-tools/project_index/extractors.py`
- `mcp-tools/project_index/store.py`
- `mcp-tools/project_index/service.py`
- `mcp-tools/tests/test_project_index_core.py`

## Steps

1. Add failing pytest function tests first for every item below and run them to
   record RED:
   - nodes and edges expose content hash, extractor id/version, exact line and
     UTF-8 byte spans, and only `observed`, `resolved`, or `declared`
     provenance;
   - identical sync and rename of an identical blob reuse a durable parser
     cache after service reopen; a one-file change parses only that blob while
     path-dependent nodes/edges are rebuilt correctly;
   - Markdown extracts frontmatter keys, headings, links, checkboxes, code
     fences, and explicit `Owner`, `Depends on`, and `Write Scope` facts;
   - unresolved/dynamic Python references become coverage gaps, not guessed
     edges;
   - successful query receipts persist and can be fetched by `trace_id` after
     reopen, including snapshot, bounds, returned ids, gaps, and miss escape;
   - snapshot records expose deterministic manifest/parser-set hashes and HEAD
     when mechanically available.
2. Implement a path-neutral parse cache keyed by blob hash and extractor
   version. Do not count reuse while reparsing the blob.
3. Re-resolve cross-file/path-dependent edges for every new immutable snapshot.
4. Keep models frozen and `_json_safe` compatible. Do not add summaries,
   embeddings, model-written graph APIs, timestamps to deterministic ids, or
   third-party index libraries.
5. Run core, checkpoint, then full tests.

## Acceptance

Tests prove parser invocation counts, reopen persistence, rename behavior,
explicit provenance/span fields, structured Markdown coverage, query-receipt
durability, and unchanged checkpoint compatibility. Full tests pass.

## Return

Changed files, each RED/GREEN command and result, schema version/migration
notes, and blockers. Do not commit, push, or create a PR.
