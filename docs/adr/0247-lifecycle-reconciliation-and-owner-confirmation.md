# ADR-0247: Lifecycle reconciliation and owner confirmation

- Status: Proposed (the owner approved the repair plan that contains this table
  on 2026-10-25; acceptance of the table itself, plan decision 2, is pending)
- Date: 2026-10-25
- Related: ADR-0117, ADR-0169, ADR-0174, ADR-0224, ADR-0232, ADR-0245
- Would amend: `memory-evaluation-and-lifecycle.md` gates 12 and 13
- Detailed design: `docs/plan/memory-evaluation-and-lifecycle.md`,
  `docs/plan/adaptive-memory-distillation.md`,
  `docs/plan/memory-formation-and-consolidation.md`,
  `docs/plan/memory-read-api-and-browser.md`

## Context

The 2026-10-23 retrieval investigation found the decay sweep retiring owner
content: "Andy confirmed the CEO list" and Matthew Scullion's role (both
`AFFIRMED`, `DIRECT`) and "User wants Galileo, Datagen and Unbabel removed from
the tracked companies" (`INFERRED`, `DIRECT`) were retired at confidence 0.15
after idling past the fact horizon. Only explicit high-confidence beliefs were
exempt, and nothing had ever been cited (ADR-0245's predecessor commit
450c1965 added the citing instruction), so "unused" meant "never delivered".

Two documents disagree about what a citation does. Gate 13 of
`memory-evaluation-and-lifecycle.md` says citing a belief *resists decay*.
The later M21 design (`adaptive-memory-distillation.md`) says a faithful
citation updates `last_used_at` and utility only, durable direct decay is
measured from `last_evidence_at`, and "a memory cannot keep itself alive by
being recalled". The earlier plan revision's idle clock
`max(last_evidence_at, last_used_at)` would have violated the second.

The governed restore path also did not exist. Recording a `CHANGED` rejection
first stores the statement hash, and formation refuses any statement whose
hash matches an outstanding rejection, so "confirm" implemented as a
correction with the same words would block its own replacement. ADR-0232
forbids restoring a retired belief by editing its status.

Usage counters under `usage-accounting@2` (`delivered_count`, `cited_count`)
exist since 450c1965; legacy `utility` is depressed by two months of charges
for beliefs the model never saw and cannot be reset by waiting.

## Decision

### Lifecycle classes

| Class | Rule |
| --- | --- |
| User authority (explicit saves, corrections, confirmations) | Outside automatic decay in any status. |
| `AFFIRMED` + `DIRECT` (owner-confirmed conclusions) | Outside automatic decay in any status; corrected only by review or supersession. |
| `INFERRED` + `DIRECT` + `DURABLE` (automatically captured owner statements) | Decay is still measured from `last_evidence_at` (M21), but a step is taken only with evidence of non-use: `delivered_count - cited_count >= decay.min_unused_deliveries` (default 3). A belief never delivered is unseen, not unused, and is left alone. |
| Hypotheses, tentative and ongoing observations | Unchanged: they expire from `last_evidence_at`; a citation never extends them. |

Gate 12 ("unused provisional beliefs decay and retire") is re-stated with this
definition of unused. Gate 13 is re-stated as "citing a belief counts as use,
so the non-use rule does not fire"; a citation still never touches evidence,
confidence or expiry, which keeps M21's rule intact.

Both store adapters apply the exemptions inside `list_idle` when the sweep
passes `min_unused_deliveries`, so a bounded sweep page is not filled with rows
the sweep would skip. `decay_eligible_lifecycle()` in the memory domain is the
single predicate; the sweep applies it again in Python.

Every lifecycle decision reads the `usage-accounting@2` counters. Legacy
`utility` keeps feeding ranking as `max(0, utility)` and is frozen for
decisions; the counters start at deploy, so the `INFERRED` + `DIRECT`
protection is weak until they accumulate. The beliefs already retired are
restored through `confirm`.

### `confirm`

`MemoryReviewOutcome.CONFIRM` on `POST /v1/memories/{id}/review`, the CLI
(`agent memory review --outcome confirm`) and the Apple detail view ("Still
true"). In one unit of work the service appends `memory.owner_confirmation`
(actor: the principal; payload: belief id and the statement), supersedes the
predecessor (`superseded`, `valid_to`, fresh store position, `superseded_by`)
**without a rejection record**, and forms the replacement through formation
citing the confirmation event: same statement, subject, scope, type,
portability, sensitivity and polarity; user authority; explicit; `ACTIVE`;
no automatic expiry. The replacement is a user-authority belief and therefore
never decays.

Refused, as `conflict`: a belief a correction already superseded; a statement
whose hash matches a tombstone or an `untrue`/`changed` rejection; a belief
whose source events were erased; a derived summary. Allowed: live beliefs and
beliefs retired by decay or expiry. `expected_updated_at` and the
`Idempotency-Key` replay keep their ADR-0224 semantics; the replay returns the
replacement's view.

### Knob

`formation.decay.min_unused_deliveries` (default 3, minimum 1) joins the
memory profile registry (44 → 45 leaves).

## Consequences

- The CEO-list and Scullion class (`AFFIRMED`) is protected by exemption; the
  "remove Galileo" class is protected only once deliveries accumulate, and the
  three already retired are restored with `confirm` once this ships.
- Decay now needs delivery evidence, so test fixtures for the sweep record
  deliveries explicitly; the sweep's ordering and idempotency rules are
  unchanged.
- `confirm` is one tap from a quote to owner authority; the client shows the
  statement before sending, and the refusals above bound it.
