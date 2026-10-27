# ADR-0249: Review reasons and two ranking controls

- Status: Accepted (the owner accepted the two controls and the ordering
  invariant with its one-third cap on 2026-10-26, and chose the same day to
  bump only the live selector to `retrieval@7`, see point 4)
- Date: 2026-10-25
- Related: ADR-0117, ADR-0224, ADR-0233, ADR-0245, ADR-0247, ADR-0248
- Detailed design: `docs/plan/memory-retrieval-and-ranking.md`,
  `docs/plan/memory-read-api-and-browser.md`

## Context

Website-learned memories (ADR-0233) were formed but never recalled: the one
literal `flagged_for_review` penalty of 0.2 put every one of them at the
score floor, and the same penalty served four unrelated reasons for a flag,
so the review queue could not say why a belief was in it. The 2026-10-23
production store held 326 flagged beliefs: conflict links, corrections,
source-learned hypotheses and automatically captured sensitive statements,
indistinguishable to the browser and to ranking.

The owner decided (2026-10-23) that website-learned memories surface
automatically as attributed tentative facts, below owner-stated facts, never
at owner authority.

## Decision

1. **Review reason, derived.** `review_reason(record)` returns `conflict`
   (a `conflicts_with` link, flagged or not), `source_learned`
   (`owner-source-learning@*`), `sensitive_automatic` (flagged, inferred,
   sensitive or restricted) or `correction` (any other flagged belief), or
   nothing. No column: the browser, the counts and the ranking read one
   projection. `MemoryView` exposes it (32 fields); the list filters by
   `review_reason`; the counts route reports the live queue by reason.
2. **Two controls.** `retrieval.flagged_penalty` (default 0.2) applies to
   doubt, `conflict` and `correction`; `retrieval.attention_penalty`
   (default 0.05) applies to attention, `source_learned` and
   `sensitive_automatic`. The registry grows from 45 to 47 memory leaves.
   The `conflict` reason earns its penalty because ADR-0248 made links
   attribute conflicts and the reconciliation clears the false ones.
3. **Ordering invariant, tested** under the shipped profile with match
   driven through the semantic rank: (i) at equal match an attention-flagged
   provisional hypothesis trails an active user belief by at least 0.10;
   (ii) a user belief with match at least 0.5 outranks any attention-flagged
   hypothesis whatever its match; (iii) attention-flagged hypotheses fill at
   most `ceil(max_items / 3)` of a recall's rendered slots.
4. **Policy version: the live selector moves, the base label stays.** The
   rule is `retrieval@4 -> retrieval@6` and `retrieval@5 -> retrieval@7`
   with the M16 deterministic baseline re-recorded in the same change. The
   re-record was run: every metric is identical, only the policy string and
   build reference move. The M32 frozen control pins the baseline's digest in
   `memory-reconsolidation.manifest.json`, its recording pins that manifest
   and `retrieval@4` in all 72 traces, and the owner-reviewed review contract
   pins the manifest, so the base bump would re-pin a reviewed contract for no
   measured change. The owner decided on 2026-10-26 to bump only the live
   relevance selector, `retrieval@5 -> retrieval@7`, which every production
   recall trace carries, and to keep `retrieval@4`. This is a recorded
   deviation from the both-labels rule: `retrieval@4` keeps naming base
   ranking whose penalties changed for inputs the M16 corpus does not hold
   (learned and attention-flagged memories). A base bump becomes due when a
   change moves an M16 metric.

## Consequences

- A learned hypothesis at the shipped weights scores about 0.17 plus 0.4
  times its match; an active owner statement about 0.46 plus 0.4 times its
  match. The margins in decision 3 hold with room, and `attention_penalty`
  is the dial if production noise rises.
- Frozen M16 and M33 controls and the ADR-0220 held-out are untouched; the
  baseline moves only through the version bump.
- The learned-memory shape (subject, claim identity, host attribution,
  `owner-source-learning@2`) and the reshape of existing rows are the second
  half of the plan's ADR-B and follow in their own change.
