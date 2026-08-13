# Project-Bound Authority vNext

**Status:** design and admission contract only.  This document does not enable
project-bound writes, migrate a legacy database, or make an in-process Python
object a host trust boundary.

## Decision

The current plugin process has no trustworthy way to decide which Desktop
project it serves.  Its `ProjectAuthority` and
`RuntimeProjectAuthorityProvider` can validate a receipt against a directory,
but they do not persist, select, sign, revoke, or inject that receipt from a
Desktop host registry.  A directory name, an environment variable, a work
package, a workspace identifier, or a Python-private capability is therefore
not project authority.

Until a Desktop host supplies a private, process-bound admission grant, public
Fast Lane compilation and bootstrap remain inert.  Existing project-scoped
paths remain compatibility isolation only; they must not be represented as
durable project ownership.

| State | Permitted now | Explicitly forbidden |
| --- | --- | --- |
| Plugin without a host grant | Read-only legacy diagnostics and fail-closed public requests | Project-bound writes, worktree creation, legacy migration, or project selection from caller input |
| Host-attested greenfield project | Future vNext bootstrap after all schema cards below are accepted | Opening or adopting a nonempty legacy database as the project |
| Legacy O v13, C v3, Atlas v1 data | Read-only preservation/quarantine | In-place fence stamping, automatic migration, or inferred ownership |

The Desktop-host registry, signing/attestation material, child-process binding,
and launch-time private grant injection are **BLOCKED_EXTERNAL_HOST_CONTRACT**
in this repository.  The local Desktop installation is a protected packaged
application and its source is not available here.

## Required host admission grant

The host, not MCP input or the plugin, must select the project and issue a
single-use, bounded grant for the launched child.  Conceptually, the grant has
these signed or host-private fields:

```text
schema = project-authority-grant/v1
receipt_id, host_instance_id, session_id, invocation_id
child_pid, issued_at, expires_at, registry_epoch
project_id, project-root physical binding, binding_digest, binding_version
host-owned data-domain and scratch-domain identifiers
package_payload_hash, workspace_id, input_snapshot_id
issuer_key_id, signature
```

The host registry is the sole durable authority for
`receipt_id -> physical root binding + data domain + revocation epoch`.  Before
issuing, it holds and checks the actual child-process handle, private bridge
session, invocation, root handle/identity, reparse state, and data-domain
ownership.  The plugin must match all grant fields to its current request and
must revalidate before every strict write boundary.  An unknown issuer,
expired/revoked epoch, mismatched PID/session/invocation, root replacement, or
workspace outside the selected physical project fails closed.

No public MCP tool receives a grant, receipt, project root, project identifier,
or signing material.  Environment variables such as `CODEX_PROJECT_*` and
`PLUGIN_DATA` remain legacy configuration only and cannot select a strict
project.

## Canonical project fence

Future stores use one canonical, path-free project fence:

```text
schema = project-fence/v1
project_id
binding_digest
binding_version
```

It is derived only after the host-admitted authority is physically revalidated.
The plugin's current canonical `ProjectFence` is deliberately only a
self-consistency value; it is not a host-attested grant and cannot activate
runtime writes by itself.

Each strict database has one immutable singleton fence row.  Every open,
prepared connection, transaction, replay, recovery, and attached operation
must compare that row exactly to the expected host-admitted fence.  A
database-directory separation alone is insufficient because copied or replaced
database and CAS roots otherwise retain indistinguishable records.

## Greenfield schema sequence

These cards are ordered; none may silently substitute a weaker boundary.

1. **PI-ADMISSION-CORE**
   - Introduce only a host-injected admission interface and require it before
     strict bootstrap/RW UoW paths.
   - Missing or invalid admission returns a stable public authority error
     before directory creation, SQLite open, custom UoW factory invocation, or
     other persistent side effect.
   - The default server stays unadmitted and therefore fails closed.  Existing
     MCP signatures, including the four-field `atlas_accept`, do not change.

2. **PI-O-SCHEMA-V14-PROJECT-FENCE**
   - Add an immutable `project_fence` singleton to a new, empty orchestrator
     database and require an exact expected fence at every store boundary.
   - Add the fence and host-mapped workspace identity to canonical acceptance,
     evidence, outbox, receipt-attestation, and finalization-v2 hash domains.
   - A nonempty v13 database refuses v14 bootstrap/open with zero mutation.

3. **PI-C-SCHEMA-V4-PROJECT-FENCE**
   - Add the same singleton plus a CAS-root marker.
   - Bind `ContinuityKey`, its key hash, manifest/view, receipt, attempt, and
     pointer to the fence under a v2 identity domain.
   - A nonempty C3 database or a database/CAS marker mismatch refuses opening
     with zero mutation.

4. **PI-ATLAS-SCHEMA-V2-PROJECT-FENCE**
   - Add the singleton and Atlas-CAS marker, then bind accepted projection
     request/evidence, ingestion, packet receipt, and replay identity to the
     same fence and host-mapped workspace.
   - A nonempty Atlas v1 database remains read-only/quarantined.

5. **CP-E finalization v2**
   - Only after O v14, C v4, and Atlas v2 are accepted.
   - The O main database and attached C database prove their singleton fences
     are exactly equal before `BEGIN IMMEDIATE`, again at publication, and at
     recovery.  The finalization certificate includes the project fence and
     workspace identity.

## Identity and migration rules

All cross-store identities must add the canonical project fence and
host-mapped workspace identity before hashing: O acceptance/evidence/outbox and
finalization payloads; C key/view/receipt/pointer payloads; Atlas projection,
ingestion, and packet receipt payloads.  Content-addressed file or node bodies
may retain content hashes, but a project-bound store must never use equality of
those bodies as authority to open, recover, or finalize another project.

There is no automatic legacy migration.  A host-approved migration is a
copy-only, auditable operation:

1. The host opens a selected target authority and a target empty vNext root.
2. It fingerprints the legacy O/C/Atlas databases and CAS roots while they are
   read-only and rejects WAL/SHM, mixed workspace, missing evidence, or split
   finalization state.
3. It recomputes all project-bound identities into the target, verifies the
   full graph, preserves the source as read-only evidence, and atomically
   switches the host's registered project target.

No path, environment value, existing workspace id, package JSON, or singleton
legacy row can stand in for this host decision.

## Acceptance matrix

The following RED cases must precede each relevant implementation:

- No admission, caller-minted provider, environment/root substitution, forged
  receipt, stale grant, or root replacement reaches a strict UoW, SQLite open,
  custom factory, worktree operation, or write.
- An A admission cannot open, claim, replay, publish, or finalize B data.
- Same business inputs under A and B yield different project-bound O/C/Atlas
  identities; a mismatched singleton or CAS marker fails with no mutation.
- An O+C attached transaction with different fences fails before `BEGIN`, and
  same-fence publication remains atomic under its established local-volume and
  DELETE-journal checks.
- A nonempty legacy database receives no fence row, schema rewrite, WAL
  conversion, or CAS marker when strict opening is attempted.
- Public MCP surface remains unchanged and authority failures disclose no path,
  receipt, SQL, hash, or grant detail.

## Non-goals and residual boundary

This document does not create a Desktop host registry, weaken the current
inert Fast Lane behavior, or claim that Python underscores, closures, or module
private objects defend against a hostile caller in the same process.  The
plugin may validate a grant once a trusted host supplies it; it cannot create
the host trust relationship locally.
