# ADR-0014: Memory surface and external memory providers

- Status: Accepted
- Date: 2026-07-20
- Related: Milestone 9 (memory), ADR-0012 (prompt-stability invariant), Section 11

## Context

Milestone 9 memory is DB-structured (provenance, sensitivity, conflict handling,
tenant scope) and not yet built or human-editable. Hermes exposes memory as
human-readable, editable files (`MEMORY.md`, `USER.md`, `SOUL.md`), injected as a
**frozen per-session snapshot**, **threat-scanned at load**, with an optional
external semantic provider (Honcho) for vector recall.

## Decision

1. Add a **human-readable, user-editable memory surface** over the structured
   store (view / edit / delete), keeping the database as source of truth with
   provenance, sensitivity, conflict handling, and tenant scope.
2. Inject memory as a **frozen snapshot once per session** (prompt-stability
   invariant, Section 10.1); mid-session writes persist but do not mutate the
   cached prefix.
3. **Scan memory for prompt injection at load**; replace poisoned entries with
   `[BLOCKED]` placeholders.
4. Make **external semantic memory a provider behind the memory port** (e.g.
   Honcho); allow the builtin store plus at most one external provider.
5. Optionally layer a **persona/identity surface** (a `SOUL.md`-equivalent) over
   `AgentSpec.instructions`.

## Consequences

- Better UX (users can inspect and correct what the agent believes), injection
  resistance, and pluggable semantic recall — while keeping governance.
- More surfaces to secure and to keep tenant-scoped.

## Alternatives considered

- **DB-only opaque memory**: rejected; no user control or correction.
- **Files as source of truth (Hermes-style)**: rejected; loses governance,
  provenance, and multi-tenant scoping.
