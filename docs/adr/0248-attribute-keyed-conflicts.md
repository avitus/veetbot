# ADR-0248: Attribute-keyed conflicts

- Status: Accepted (owner approved the repair plan containing this half on
  2026-10-25; the owner-authorized production reconciliation ran on
  2026-10-26 and cleared 9 of 10 pairs)
- Date: 2026-10-25
- Related: ADR-0117, ADR-0232, ADR-0245, ADR-0247
- Detailed design: `docs/plan/memory-formation-and-consolidation.md`,
  `docs/plan/memory-retrieval-and-ranking.md`

## Context

In the 2026-10-25 garden-calendar chat (session 71972605, run f2cefe08) the
owner's affirmed home address was returned with seven `conflicts_with`
partners. None was rendered and none was a real conflict: five were unrelated
email facts whose subject is also "Andy Vitus", two restated the same address.
The model read a one-sided `conflicts=[...]` marker, wrote that its sources
"do not establish the garden's location", and asked which city.

Formation selected related beliefs by `(subject, belief_type)` alone and the
resolver returned `conflict` whenever the incoming authority ranked lower, so
every provisional email fact about the owner became a conflict partner of the
affirmed address, and the conflict branch flagged both. The retrieval
specification already groups by `(subject, belief_type, predicate key)`;
formation had no predicate key.

## Decision

1. **Predicate key.** `agent_core.memory.predicates` derives a closed key for
   a statement: `home_address`, `home_city`, `phone`, `email`, `employer`,
   `job_title:<org>`, `birthday` and `relationship:<role>` (relationship
   beliefs), each with its value; any other statement, preferences included,
   falls back to `claim:<normalized statement>`, never to the bare subject.
   Preferences are not keyed: a topic has no reliable surface form ("dark
   mode everywhere" against "light mode now that the office is bright"), and
   the M16 update scenarios need the later preference to supersede the
   earlier one about the same subject. The key is stored on new beliefs (`memories.predicate_key`,
   nullable, no backfill) and derived at read time for legacy rows. It is
   derived content, excluded from the content signature. Rule version
   `predicate-key@1`.
2. **Formation.** A recognised claim relates only to beliefs with the same
   `(subject, belief_type, predicate key)`; two unrecognised claims keep the
   subject-and-type comparison formation always had, so a free-text update
   still supersedes the statement it replaces (the M16 currency gates pin
   this); a recognised claim and an unrecognised one are independent. The
   resolver returns `duplicate` for the same attribute with the same value,
   whatever the wording (corroboration, never a link), and keeps the
   authority-then-time rules within one attribute.
3. **Rendering.** A `conflicts=[m:...]` marker is rendered only beside a
   partner that is rendered too; the trace and the browser keep every link.
   The spec's "return both" is honoured, or the dispute is not shown as a
   one-sided doubt. Pulling a partner that was not a candidate into the
   budget is left for the review-reason work (ADR-B of the plan).
4. **Repair.** `agent memory reconcile-conflicts [--confirm]` re-evaluates
   every live link under rule 2, unlinks pairs that are not two values of one
   attribute on both sides, appends `memory.conflict_cleared` per pair with
   both ids and the rule version, and clears `flagged_for_review` where no
   doubt reason remains (no link, no outstanding rejection, not source
   learned, not an automatically captured sensitive statement). Changed
   beliefs take a fresh store position and a `memory.reviewed` event. A dry
   run reports the same counts and writes nothing. No SQL status edit. The
   owner-authorized production run on 2026-10-26 examined 10 pairs and
   cleared 9, including all seven links on the home address; no review flag
   needed clearing.
   Reconciliation takes the existing principal owner guard before reading any
   belief or rejection and keeps it through the unit of work, including dry
   runs. Taking the guard only when writing permits a stale full-record copy
   to undo an intervening owner's `not_here` portability restriction or
   `untrue` retirement. PostgreSQL interleaving regressions cover both outcomes;
   this serialization repair changes neither the predicate rule nor the
   requirement for explicit confirmation before a production repair.

## Consequences

- A heuristic key is still a heuristic: too coarse recreates false conflicts,
  too fine misses real ones. The set is closed and reviewed here. Two
  free-text claims about one subject still compare under the legacy rule, so
  a false conflict between two unrecognised statements remains possible; the
  plan's "fall back to claim identity" was tried and withdrawn because it
  stopped free-text updates from superseding (M16 `stale_live_beliefs` and
  `forbidden_rendered` regressed). Widening the key set is the lever.
- The M16 conflict gate's fixtures ("concise answers" against "detailed
  answers") are two unkeyed preferences about one subject and keep conflicting
  under the legacy rule.
- PR-3's `conflict` review reason can now earn its penalty: the links it
  reads are attribute conflicts once the reconciliation has run.
