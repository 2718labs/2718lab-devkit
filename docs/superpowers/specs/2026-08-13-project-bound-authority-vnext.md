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
| Plugin without a host grant (legacy-compat) | Existing legacy-compat behavior under its current public contract; public Fast Lane compile/bootstrap remains inert | Representing a legacy path as strict project ownership; project-bound writes, worktree creation, legacy migration, or project selection from caller input |
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
grant_id, receipt_id, host_instance_id, session_id, invocation_id
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
ownership. The plugin must match locally observable grant fields to its current
request; the host must refresh and reverify the registry decision before every
strict access boundary. An unknown issuer, expired/revoked epoch, mismatched
PID/session/invocation, root replacement, or workspace outside the selected
physical project fails closed.

No public MCP tool receives a grant, receipt, project root, project identifier,
or signing material.  Environment variables such as `CODEX_PROJECT_*` and
`PLUGIN_DATA` remain legacy configuration only and cannot select a strict
project.

## External host-bridge handoff

**Status: `BLOCKED_EXTERNAL_HOST_CONTRACT`.**  The inherited-handle bridge in
this repository authenticates framed traffic only after a launcher has supplied
a private handle.  It has no Desktop registry, root-handle attestor,
issuer-selection policy, or child-process admission verifier.  A typed packet
parser, a Python-private token, or a session MAC alone is therefore not a
project-admission implementation and must not construct a strict
`projects-v2` `RuntimeConfig`, strict `RuntimeRoot`, or
`RuntimeProjectAuthorityProvider`. It does not change the default
legacy-compat server configuration.

The Desktop host must complete the following sequence before the plugin can add
a strict path:

1. Open and retain a trusted project-root handle, reject reparse/junction
   traversal, and atomically lookup-or-issue a registry record keyed by the
   physical root binding.  The registry selects the host-owned data and scratch
   domains before the child starts.
2. Start the child with one inherited private bridge endpoint and bind the
   registry decision to the actual child PID, host instance, invocation,
   expiry, and registry epoch. After the child establishes the authenticated
   bridge session, it sends one `project_admission_request` that contains no
   project selection, root, receipt, or data-domain input. The host verifies
   that request against the child handle and launch metadata, then records the
   single-use `grant_id` before any project database bootstrap.
3. Deliver exactly one private `project_admission_grant` response using the
   request's exact bridge action id and expected sequence. Its canonical
   payload contains exactly the grant fields listed above, plus the serialized
   `ProjectAuthorityReceipt` and canonical `ProjectFence`. The Desktop
   protocol, not this plugin, defines how its retained root handle and
   host-owned data/scratch domains are transferred or referenced; the plugin
   must never infer that a raw path is trustworthy. No roots or domains are
   environment or MCP inputs.
4. The plugin's typed receiver validates exact schema/fields and bounds,
   action/sequence correlation, local PID, local expiry, `grant_id` single-use
   bookkeeping, receipt shape, host-bound physical-root revalidation, and exact
   derived fence. It derives strict data/scratch paths only from the admitted host
   domains. It cannot itself establish issuer identity, current registry epoch,
   or revocation. Before every strict bootstrap or RO/RW UoW boundary it must
   make a host-private `project_admission_refresh` exchange bound to the same
   grant/session; the host rechecks its registry, child handle, epoch, and
   revocation state, then returns an exact current decision or rejects it.

The future typed bridge receiver must reject a missing, duplicate, malformed,
expired, wrong-PID, wrong-session, wrong-invocation, substituted root/domain,
receipt/fence mismatch, or root-replacement grant before it constructs any
strict runtime object. The host refresh verifier must additionally reject a
stale/revoked epoch or unknown issuer. Neither may fall back to caller
environment paths or silently reopen legacy data. When a strict path is
implemented, a failed strict admission maps to a stable public
authority-unavailable envelope with no path, receipt, SQL, hash, or grant
detail; the current legacy server does not yet expose that new envelope.

The corresponding Desktop-host acceptance evidence is mandatory: concurrent
same-root lookup-or-issue collapses to one record; a copied, cross-volume, or
reparsed root cannot reuse a receipt; restart preserves the chosen record;
registry/ACL tampering and crash windows fail closed; and no MCP request can
choose a root, receipt, data domain, or project id.  This plugin repository can
test only the receiving boundary after that host capability exists; it cannot
manufacture the host-side evidence.

## Canonical project fence

Future stores use one canonical, path-free project fence:

```text
schema = team-efficiency/project-fence-v1
project_id = lowercase 64-hex
binding_digest = sha256: lowercase 64-hex
binding_version = 1
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
     strict vNext bootstrap/UoW paths.
   - Missing or invalid admission returns a stable public authority error
     before directory creation, SQLite open, custom UoW factory invocation, or
     other persistent side effect **for a strict vNext operation**.
   - The default server stays legacy-compat and unadmitted.  This card must
     not globally gate or silently relabel existing legacy MCP operations;
     they remain non-project-owned compatibility paths and may never be
     attached to, migrated into, or used as authority for a strict vNext
     project. Existing MCP signatures, including the four-field
     `atlas_accept`, do not change.

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
inert Fast Lane behavior, globally disable legacy compatibility operations, or
claim that Python underscores, closures, or module-private objects defend
against a hostile caller in the same process.  The plugin may validate a grant
once a trusted host supplies it; it cannot create the host trust relationship
locally.
