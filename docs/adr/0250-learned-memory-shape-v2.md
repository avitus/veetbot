# ADR-0250: Learned-memory shape v2 and the reshape of v1 rows

- Status: Proposed (plan decision 4: the production reshape run needs the
  owner's authorization; the shape itself follows the owner's 2026-10-23
  decision that website-learned memories surface as attributed tentative facts)
- Date: 2026-10-25
- Related: ADR-0233, ADR-0245, ADR-0248, ADR-0249
- Detailed design: `docs/plan/memory-formation-and-consolidation.md`,
  `docs/plan/memory-retrieval-and-ranking.md`,
  `docs/plan/memory-read-api-and-browser.md`

## Context

`owner-source-learning@1` stored a learned claim as
`Source suggests: <quote> [Source: <url>]`, with the quote as its subject and
`local` portability. The 32 ScaleVP beliefs of 2026-10-23 were therefore
unrecallable twice over: the citation inside the statement defeated the
lexical arm (`Source suggests` and the URL matched every query equally, the
claim words were diluted), the quote-as-subject defeated the structured arm
and the subject escape, and `local` portability discounted them in every
other project. The owner wants them to surface automatically as attributed
tentative facts, below owner-stated facts.

## Decision

1. **Shape.** `owner-source-learning@2`: the statement is the grounded claim;
   the subject is the model's grounded subject of at most 64 characters, or
   the authorized host when omitted; the host is `attribution_host`, a new
   nullable column (migration b7d2e4f6a1c3), exposed on `MemoryView` (33
   fields) and carried on recalled items; portability is `contextual`.
   Confidence 0.35, inferred, hypothesis, tentative, sensitive, flagged,
   thirty-day expiry and the 0.55 reinforcement ceiling are unchanged. A
   learned quote is verbatim evidence the learn path has already scanned, so
   the automatic-capture word floor does not apply to it.
2. **Rendering.** A recalled learned belief prints `(learned from <host>)`
   beside its identifier, the way a carried belief prints its origin scope.
3. **Identity across versions.** Claim identity is the normalized claim text,
   as before. Receipts are read under both versions, so a host still counts
   once per claim and a @1 receipt makes the same claim from the same host
   `unchanged`. A receipt follows `superseded_by` to the belief that now
   stands for it. A deletion or correction of the @1 rendering blocks the @2
   identity. An expired claim may be learned again from a fresh read, under a
   receipt chained to the one it follows; a retired one still refuses.
4. **Reshape.** `agent memory reshape --policy owner-source-learning@1
   [--confirm]` supersedes every live @1 row with a @2 belief formed through
   formation from the same source events, confidence and expiry, links the
   same sources, marks the @1 row `superseded` with `superseded_by`, and
   leaves receipts to follow. A dry run counts and writes nothing. The
   production run (55 rows on 2026-10-23) needs the owner's authorization;
   the fallback is expiry on 2026-11-20/21 and re-learning.

## Consequences

- Learned beliefs become lexically and structurally reachable and, with
  ADR-0249's attention penalty, rank as attributed tentative lines below
  owner facts. Nothing about their authority changes.
- The source-link idempotency check lets a claim learned again from the same
  site reuse its link rather than fail on a revision conflict.
- `contextual` learned beliefs follow the owner across projects at the
  contextual discount; `local` remains for the deterministic LOCAL excerpt
  adapter.
