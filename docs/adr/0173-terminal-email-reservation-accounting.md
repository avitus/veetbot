# ADR-0173: Terminal email reservations become conservative budget charges

- Status: Accepted — owner explicitly approved conservative accounting on 2026-10-10
- Date: 2026-10-10
- Amends: ADR-0096

## Problem

An email run can end without complete provider usage evidence, including after
a transport timeout or cancellation. Its reservation currently counts against
every subsequent daily allowance and never ages out of the rolling window.
Deleting its run also removes the evidence needed for ordinary settlement.
Accumulating such holds can permanently prevent new email work.

The existing email design requires ambiguous attempts to retain their
reservations. Releasing them as zero would undercount possible spending.

## Decision

1. Keep the full reservation for every live run, including queued, resumed and
   suspended runs. Do not release it because of age or a transient failure.
2. Once a run is terminal, retain uncertain actual usage as unknown, but close
   its live hold with a separately labelled conservative budget charge equal
   to the greater of its full reservation and recorded run cost. A missing run
   receives the full reservation charge; its absence is never proof of zero
   cost. Preserve the original reservation and accounting provenance.
3. Count this charge in the task's original UTC-day and rolling-thirty-day
   buckets, exactly as known usage is counted. It therefore consumes the full
   admitted allowance once without occupying every future daily allowance.
   Existing daily, rolling-month and per-run limits do not increase.
4. Reconsider existing unresolved tasks automatically under the existing owner
   lock before admitting another task. Recovery is idempotent and requires no
   database edits or extra model requests. A later complete usage record may
   replace the conservative charge with proven actual cost.
5. Keep actual settlement distinct from conservative accounting. Missing,
   malformed or failed model outcomes must never appear as a proven zero-cost
   settlement. Charge data survives deletion of the associated run. The task
   retains `budget_charge` and `budget_charge_reason`; later actual settlement
   takes precedence in admission without erasing this provenance. Budget-error
   details identify the estimated subset of each period's counted spending.
6. Normalize raw provider transport timeouts into the existing typed model
   failure path so attempts retain failure and usage evidence. Do not silently
   retry a stream after output or create an unreserved email retry.

## Validation

Regressions cover old and new terminal failures, cancellation, missing runs,
recorded cost above the reservation, same-day and rolling-month exhaustion,
window expiry, repeated reconciliation, later proven usage, and active-run
holds. Adapter regressions cover raw transport failures before and after output
and verify typed failure events and retry ownership. Existing accounting and
admission concurrency tests remain required.

## Consequences

This is conservative internal budget accounting, not a claim about provider
billing or a change to provider charges. It can temporarily overstate spending.
It replaces indefinite cross-day holds only after work has ended; ongoing work
continues to reserve its allowance. Deployment remains a separate action.
