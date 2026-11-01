# ADR-0281: Recall preparation keeps its own clock and the automatic round refreshes

- Status: Accepted (owner direction on 2026-11-01)
- Date: 2026-11-01
- Related: ADR-0183, ADR-0190, ADR-0196, ADR-0223, ADR-0269, ADR-0274
- Detailed design: `docs/plan/general-memory-recall.md`, `docs/plan/context-engine.md`

## Context

On 2026-11-01 the owner asked about a sound in a parked car and Veetbot neither
knew the car nor found the manual. The production events for that chat and the
three other interactive chats of the morning show the same shape: the context
build before the first model request took just over ten seconds, no in-turn
recall trace was written, and the model's own `memory.search` was refused with
`memory_recall_budget_exhausted`. No warning reached the journal.

The owner recall trial activated at 10:06 PST that morning. Under its
execution-bound preparation, ADR-0190 starts one ten-second deadline at the
first reservation and ADR-0196 runs the searches under that same deadline. The
provider preparation that computes query vectors ran inside the window and,
when it was slow, consumed it entirely: the permit's clock expired before the
first search began, the automatic recall was refused, the deliberate search
was refused by the same spent reservation, and the only trace of any of this
was a limitation marker in the model's context. The one in-turn trace the
trial did produce that morning landed exactly ten seconds after its snapshot.

A second, independent loss followed from ADR-0196's accounting. Every build
step re-reads memory (ADR-0183), and each automatic read reserved two slots and
one round. After two steps the run's two rounds were spent, so a deliberate
search issued at step two or later was refused, and the automatic read at step
three and beyond was refused as well. ADR-0196 accepted that a long run could
lose automatic context; in practice a three-step chat lost it.

## Decision

1. **Preparation and search keep separate clocks.** Provider preparation may
   take at most five seconds. The searches it serves get their own ten seconds
   from the moment preparation returns or times out. The durable reservation
   bounds both together at fifteen seconds, so resume and checkpoint-loss
   semantics are unchanged: a reservation still cannot be renewed. A
   preparation timeout is logged as `recall_preparation_timed_out`; sparse
   recall proceeds on the search clock.
2. **The automatic round is reserved once per run and refreshed per step.**
   The first automatic read reserves its base and delta slots and no round.
   Later build steps refresh the automatic read under a fresh search clock
   without new slots, rounds or checkpoint writes. Deliberate searches reserve
   one slot each from the two the automatic round leaves, in at most two
   deliberate rounds. The four-slot ceiling on reserved searches stays; the
   bound on automatic executions becomes the run's own step cap. Reservations
   written before this decision keep their stricter accounting.
3. **A refused or timed-out automatic recall is recorded.** The context
   builder keeps rendering the limitation marker for the model and now also
   logs `context_memory_recall_limited` and marks the request; the run loop
   appends `memory.recall.limited` with the step number and the reason
   (`exhausted` or `timed_out`). A turn without recall is a measurable event.

## Consequences

- The valve-sounds failure mode cannot recur from slow preparation alone: a
  slow or unavailable vector provider costs at most five seconds and then the
  sparse arms run.
- Automatic recall now executes on every build step of a run, as it already
  did in production under sparse fallback, where no permit is composed.
- Recall health can be read from events: a run whose interactive turns show
  `memory.recall.limited` instead of `memory.recalled` is degraded, and the
  reason says why.
- The evaluation runtime shares the loop's accounting, so its observed
  executed searches may exceed the reserved slots by the automatic refreshes.

## Verification

- `tests/unit/test_runtime_recall_preparation.py`: an eleven-second
  preparation no longer refuses the turn's searches; a twenty-second one
  leaves `memory.recall.limited`; the reservation counters follow the new
  accounting across five build steps.
- `tests/unit/test_context_memory_budget.py`: the limitation reaches the
  request metadata.
