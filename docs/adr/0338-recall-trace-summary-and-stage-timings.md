# ADR-0338: The recall health report reads a per-trace summary; traces time every stage and keep the candidate ranking

- Status: Proposed — owner approved the measurement repair on 2026-11-20
- Date: 2026-11-20
- Related: ADR-0247, ADR-0249, ADR-0282, ADR-0290
- Detailed design: `docs/plan/memory-evaluation-and-lifecycle.md`, `docs/plan/memory-retrieval-and-ranking.md`

## Context

On 2026-11-20 `agent memory health` failed on production for every window with
a statement timeout. Its three trace aggregates each read whole trace
documents. A document is about 17 KB and stored out of line, and automated
sessions (server email checks, schedules, Cockpit jobs) had come to write about
6,700 snapshot traces a week, so `recall_traces` held 24,862 rows and 413 MB.
The report's ten-second statement bound could no longer be met, and the cost
grows with every automated session.

The same review found two gaps in what a trace records. Only the vector search
was timed: the `lexical` and `structured` latencies were the constant 0, so the
report's latency figures said nothing while the time owner chats spent before
each recall completed had grown from about 0.2 s to about 2.4 s per model step.
And a trace kept only the beliefs it returned and the identifiers it dropped for
budget, so a missed belief could not be placed: never a candidate, ranked too
low, or cut by a budget.

## Decision

1. **A summary beside each trace.** `recall_traces` gains a nullable `summary`
   JSONB column written with the trace: total recall time, the arms that ran,
   and returned and dropped counts by review class (flagged, provisional,
   source-learned, owner-authority). The health report aggregates this column
   in one statement and never reads a trace document. An index on owner and
   creation time serves the window.
2. **Classes as of the recall.** The retriever counts classes from the records
   it ranked, so the report describes what reached the model when it did. It
   previously joined the store at report time, which let a later promotion or
   review rewrite history.
3. **No backfill.** A trace recorded before the column has no summary. The
   report counts those traces apart (`summarized_traces`) rather than reading
   their documents, so it is complete for any window that starts after the
   deploy. Backfilling would rewrite a 413 MB table inside a release.
4. **Every stage timed.** `arm_latencies_ms` keeps its arm keys and gains the
   recall's stages: `lock` (opening the unit of work and taking the owner
   lock), `lexical` (the candidate query, which serves the lexical and the
   structured arm), `semantic` (the vector search), `expand` (identity scoping
   and merge candidates), `rank` (scoring, fusion and ordering), `pack`
   (budgeted admission and rendering) and `total`. `structured` stays 0
   because that arm has no read of its own. The report's latency is `total`.
5. **The candidate ranking is kept.** A question-led recall records
   `ranked_candidates`: every scored candidate, best first, before duplicate
   and per-subject collapse, to a bound above what recall can fetch. A
   snapshot recall has no question and records none.
6. **Retention and erasure are unchanged in kind.** The ranking and the
   summary's timings and arms are operator tier and are removed by the
   existing expiry sweep. The summary's counts name no belief and stay, as
   `dropped_for_budget_count` already does. The three erasure paths now scrub
   one shared list of identifier fields, `TRACE_BELIEF_ID_FIELDS`, and a test
   fails if a trace gains an identifier list that is not in it.

7. **An owner question set is the standing measure for a recall change.** Real
   questions the owner asked, each with the stored facts it should reach, are
   kept in the existing probe format beside the incident probes, outside Git,
   split once into a development file and a held-out file that is never tuned
   against. A change to candidate selection, ranking, packing or the semantic
   index is run against both, before and after, before it reaches `dev`. The
   first version was drafted on 2026-11-20 from the owner's chats since
   September and awaits the owner's review.

Ranking, rendering and the retrieval policy label are unchanged: no recalled
block differs.

## Consequences

- The report's trace section stays inside its statement bound whatever the
  size of the trace documents, and its latency percentiles become real.
- A missed belief can be attributed from production traces for thirty days:
  absent from the ranking means the arms never found it, present but not
  returned means ranking or packing lost it, and `dropped_for_budget` tells
  those two apart.
- A question-led trace grows by its ranking, typically 7 KB and at most about
  20 KB. Snapshot traces, which are most of the table, do not grow.
- For up to ninety days after the deploy a window can include traces without a
  summary; the report says how many.
- Rollback across this revision is a schema boundary, as for any migration.

## Not decided here

Whether automated sessions should write a full snapshot trace at all, and
where the 2.4 s per step goes. The stage timings exist to answer the second.
