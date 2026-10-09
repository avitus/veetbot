# ADR-0167: Automatic memory maintenance does not refresh chat recency

- Status: Proposed
- Date: 2026-10-08
- Related: ADR-0050, ADR-0069; engineering plan Section 16
- User authorization: diagnose and fix a quiet chat continually appearing in
  Recent, and push the verified repair to dev.

## Context

The history index treats every persisted session event as conversation activity.
The automatic memory sweep appends `memory.decayed` and `memory.retired` to a
belief's source session, so an old conversation repeatedly appears recent even
though no run or message occurred. This is an interaction between ADR-0050's
blanket event timestamp rule and ADR-0069's background lifecycle audit.

## Proposed decision

Refine the activity projection in both event adapters: `memory.decayed` and
`memory.retired` with actor `memory` and no run identifier allocate a sequence
and persist normally, but leave `sessions.updated_at` unchanged. All other
appends retain the existing monotonic timestamp update. No event, provenance,
confidence update, retirement, or client history entry is deleted or hidden.

A data migration corrects existing active sessions whose current timestamp
exactly matches their latest automatic decay or retirement and is later than
all remaining events. It restores the maximum of creation and remaining event
times. Closed sessions and timestamps from later independent writes are left
alone. The migration serializes against session writers. Downgrade retains the
corrected timestamps; subsequent writes use the deployed adapter's rule.

## Consequences

The Recent shortcuts and displayed age converge through ordinary server-index
reconciliation, including chats already affected. Memory eligibility still uses
conversation idleness; an automatic decay no longer postpones that idle boundary.
No memory policy, audit payload, event sequence, API shape, client code, or
milestone acceptance criterion changes. Shared adapter, actual sweep, and
PostgreSQL migration regressions cover the repair.
