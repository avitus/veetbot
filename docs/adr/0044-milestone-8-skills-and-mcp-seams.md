# ADR-0044: Milestone 8 skill and MCP seams

- Status: Proposed
- Date: 2026-08-04
- Related: Milestone 8, ADR-0003, ADR-0021, ADR-0024, ADR-0029,
  ADR-0030
- Detailed design: `docs/plan/skills.md`, `docs/plan/tool-system.md`, and
  `docs/plan/sandbox-isolation.md`

## Context

Milestone 8 is the first implementation of immutable skill packages, the pinned
session catalog, selective body loading, and MCP servers behind the ordinary tool
pipeline. The detailed designs fix the security and replay properties but leave
several reversible implementation choices: archive encoding, cache ownership,
how ephemeral MCP prompts appear beside durable skills, how the official MCP SDK
receives an exact subprocess environment, and where trusted-worker HTTP traffic
meets the existing egress enforcement core.

These choices preserve the engineering plan's requirements. They are recorded as
Proposed because the repository owner asked to review decisions made while the
milestone was implemented unattended.

## Proposed decisions

1. **Skill archives are deterministic, content-addressed `tar.zst` values.**
   Validation rejects links, traversal, duplicate members, oversized files, and
   non-canonical manifests before encoding. Members are sorted and tar metadata
   is normalized; the stored SHA-256 therefore identifies the validated package,
   and reopening verifies that identity before extraction.
2. **Positive skill revisions and hashes are reconstructed from the session
   event.** `session.created` records the exact skill pins and dropped entries.
   A new process reopens those recorded revisions instead of consulting the
   current catalog, so a later revision cannot rewrite an existing session.
   Checkpoints materialize the loaded body and its provenance but are not the
   authority for catalog identity.
3. **Catalogs and loaded bodies have both normative token caps and bounded
   process caches.** Catalog order is deterministic, required entries outrank
   optional entries, body loading remains limited to two entries and 6,000 raw
   text tokens, and session cache/lock structures cannot grow without bound.
4. **MCP prompts are ephemeral revision-zero catalog entries.** They are never
   persisted as skill packages and cannot satisfy a durable required-skill pin.
   Their normalized content hash is recorded with the session catalog so a
   reconstructed connection admits only the same prompt identity.
5. **The official MCP SDK is confined to one adapter package.** Domain, ports,
   runtime, evaluation fixtures, and tests exchange repository-owned immutable
   models. This keeps SDK transport and authentication types out of durable
   events, checkpoints, and the centralized tool pipeline.
6. **Exact stdio child environments are enforced at the SDK spawn boundary.**
   The adapter builds the child environment from the configured allowlist plus
   the resolved credential. Because the SDK otherwise adds its own inherited
   defaults, the adapter serializes the small spawn section while temporarily
   suppressing that SDK list, then restores it immediately. A process-wide lock
   covers that section across threads and event loops; SDK imports are confined
   so every platform spawn observes it. Tests execute a real SDK subprocess and
   permit only the one environment entry macOS itself injects.
7. **Trusted-worker HTTP MCP uses a managed, audited loopback proxy.** The
   composition root starts it only when an enabled persisted HTTP server needs
   it, validates every current destination against the deployment allowlist, and
   closes it with the composition. The proxy reuses the sandbox egress decision
   core, resolves once, dials the checked address, logs tenant ownership, rejects
   ambiguous framing, and gives the SDK no direct HTTP client path.
8. **Authentication recovery is connection-scoped and bounded once.** Credentials
   are resolved by reference rather than persisted. An unauthorized result may
   refresh once; unchanged, failed, or repeatedly rejected credentials make the
   server unavailable. A retry after a possible non-idempotent effect returns an
   outcome-unknown failure instead of replaying the operation.
9. **Skill and MCP evaluation deltas use two deterministic arms.** Conditional
   scripted turns wait for a context marker, allowing the harness to compare the
   same prompt with and without a skill or MCP capability while remaining fully
   offline. The no-socket gate also forbids internet sockets and subprocesses for
   scripted MCP evaluation.
10. **Pipeline parity is observed with a bounded repository-owned trace.** Both
    builtin and MCP tools are dispatched through the same fourteen-stage
    pipeline. A capped trace records the stage sequence for gate evidence without
    turning diagnostic history into unbounded application state.
11. **Dynamic tool identity is tenant-, source-, and server-qualified.** The
    registry stores dynamic implementations under tenant, name, and version and
    refuses any name reserved by a static tool. Checkpoints retain each pinned
    `ToolSpec`, so recovery resolves the exact source and server rather than a
    same-named latest entry.
12. **Archive publication records ownership for rollback compensation.** The
    filesystem store publishes with atomic create-if-absent semantics and reports
    whether this transaction created the object. PostgreSQL units of work delete
    only newly created archives before releasing their identity lock when the
    transaction rolls back. Concurrent identity creation uses a no-conflict
    insert followed by a locked reselect.

## Consequences

- Skill catalogs and loaded bodies remain reproducible after process restart,
  while newer revisions affect only new sessions.
- MCP discovery adds tools dynamically, but every call still receives schema
  validation, policy, approval, deduplication, effect-watermark, timeout,
  artifact, and event handling from the ordinary pipeline.
- HTTP MCP availability fails closed when a persisted destination is no longer
  allowed by current deployment policy.
- SDK updates that change global stdio-environment behavior may require the
  confined adapter to change; the real-subprocess gate detects that drift.
- The process-local proxy is an initial deployment seam. A later external proxy
  may replace it if it preserves the same audited decision and lifecycle
  contract.

## Alternatives considered

- **Store unpacked mutable skill directories:** rejected because their content
  can change underneath a pinned revision and filesystem traversal becomes part
  of every load.
- **Resolve the latest skill revision after restart:** rejected because it breaks
  session reproducibility.
- **Persist MCP prompts as ordinary skills:** rejected because remote prompt
  lifecycle and trust do not satisfy operator or registry package provenance.
- **Let the MCP SDK inherit its default subprocess environment:** rejected
  because it can leak undeclared host variables to an MCP child.
- **Allow the HTTP adapter to connect directly after configuration validation:**
  rejected because DNS and destination enforcement must occur at the actual
  connection boundary.
- **Retry every unauthorized write once:** rejected because authorization failure
  can occur after the remote side has applied an effect.
