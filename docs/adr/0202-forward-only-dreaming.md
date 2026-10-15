# ADR-0202: Forward-only dreaming after legacy attribution loss

- Status: Accepted — explicitly authorized by the owner on 2026-10-13
- Date: 2026-10-13
- Related: ADR-0171, ADR-0174, ADR-0186
- Governing design: `docs/plan/memory-reconsolidation.md`

## Decision

Metadata-only recovery could not recover the missing reverse attribution for
469 erased legacy People heads. The owner explicitly authorized forward-only
dreaming while keeping uncertain legacy evidence excluded. No erased payload is
restored and unknown attribution remains unknown.

An explicit operator command establishes one immutable, owner-bound cutover:
`agent memory reconsolidations forward-only --owner TENANT/PRINCIPAL --confirm`.
It requires the configured owner's memory.write scope. The command saves the
current event high-water mark, memory creation and change counters, activation
time and a digest of the erased heads with unknown attribution. It refuses live
unknown heads and an active dreaming lease. Repeating the command returns the
original boundary; it cannot move the cutover or reset daily budgets/scheduling.
PostgreSQL fences event insertion before taking owner/People locks, so allocated
but uncommitted originals cannot fall through the boundary. A transaction-local
five-second timeout bounds each lock wait, including waits after the event fence.
The whole cutover rolls back on failure and can be retried after contention clears.
A migration downgrade refuses to remove an active boundary.

Only memories created after the boundary whose every original event also follows
the boundary may enter dreaming. Backdated timestamps, updated old memories,
copied old source events and mixed old/new support cannot bypass this rule.
Selection excludes old evidence before neighbor ranking and limits; scanning
starts at the saved counters. Queued old groups become stale through existing
source validation. Original recall remains governed by its existing rules.

The owner-wide legacy refusal is lifted only for these forward sources and only
while the unknown-head digest still matches. New or changed uncertainty blocks
admission again. Known matching purged heads, source suppression, sensitivity,
attribution, identity, correction, erasure and provenance checks remain mandatory.
Proposal preparation, verification, commit, approval and current/historical reads
resolve the same boundary afresh. Activation waits out an existing provider lease.

The review page displays the cutover date and explains that legacy evidence is
excluded. This is supervised daily proposal generation; every application still
requires individual owner approval. Automatic application, numeric quality floors,
frozen evaluation controls and historical observations do not change.

## Verification and delivery

Fabricated regressions cover legacy exclusion, fresh provider review, mixed and
copied sources, backdated records, bounded scan progress, neighbor starvation,
new uncertainty, erasure, owner isolation, scope, retry, rollback and concurrent
event insertion. Both storage adapters exercise the same source-selection case; all 272 PostgreSQL
reconsolidation tests pass. The original selector fails the new-source inventory
regression before the boundary/cursor fix.
Deployment must apply the additive migration, run the command once for the
configured production owner, and verify the status boundary and next daily run.
A no-change run is valid when no suitable new evidence exists; proposals are not
manufactured from legacy evidence merely to produce activity.

## Independent delivery (2026-10-15)

The owner authorized a separate dreaming release, its pull request, review and
merge so supervised dreaming does not wait for Milestone 33 recall evaluations.
Migration `ea3210a1b00d` descends directly from production's `ea3310a1b003`.
The later recall migrations must be rebased after it before their own release,
following the existing linear migration contract. Their old evaluated checkouts,
databases and observations retain their original ancestry and identities; new
recall candidates use freshly migrated databases and fresh release evidence.
This release includes no optional recall implementation or evaluation-rule change.
