# ADR-0265: The first family page reaches every tier of the family

- Status: Proposed
- Date: 2026-10-28
- Related: ADR-0238, ADR-0259; Section 8 of `docs/plan/people-and-relationships.md`
- Amends: ADR-0238 ("pages contain at most three focal people") for the first
  page of an overview; ADR-0259 decision 4 keeps its order
- Detailed design: `docs/plan/people-and-relationships.md`

## Context

On 2026-10-28 the owner asked Chat what it knew about their family. The answer
named the owner's wife, both daughters, brother, cousin and in-laws, and then
said it did not know how Cheryl Vitus was related, although People held her as
the owner's mother with two supporting beliefs.

ADR-0238 made family overviews read direct owner relationships in pages of
three focal people, and automatic recall renders only the first page. ADR-0259
ordered those pages closest first, because a daughter had fallen to page two
behind two parent records. With a spouse and two children, the first page is
now always the household, and parents and siblings are always on a page the
model is told about but, in practice, never asks for. The mother's own beliefs
are provisional and flagged, so ordinary relevance recall ranked them below the
affirmed beliefs about the brother and did not deliver them either.

## Decision

1. **A first page reaches every tier present.** The first page of an overview
   (no `people_after`) runs closest first, as ADR-0259 orders, and continues
   until it has reached the first member of every tier present: partner,
   children, parents, siblings, other relatives. It holds at least three
   people and at most eight. Continuation pages still hold three.
2. **Everything else about a page is unchanged.** Each focal person still
   contributes at most one hundred relationship rows, the shared twenty-item,
   two-thousand-token budget still binds, the page still names its
   continuation and says when a group holds more than one overview reads,
   and existing belief visibility predicates still admit the evidence. The
   continuation normally advances only through people whose eligible relationship
   contexts were fully delivered. If the first remaining person exceeds the page
   budget, the response explicitly identifies that person's context as incomplete
   and advances past them, so later people remain reachable. It directs a narrower
   `people.context` lookup for omitted detail rather than claiming full delivery.
   Continuation and incomplete-person metadata are budgeted before selection.
3. **The live label moves.** Overview pages render `+owner-overview@3`; the
   base retrieval label and the frozen evaluation controls do not move.

## Consequences

- For the owner's family (spouse, two children, two parent records, two
  sibling records), the automatic page names seven people instead of three, and
  the mother is among them.
- A family with many people in one tier still pages: eight is the ceiling, and
  the page names where to continue.
- The page introduction no longer promises "at most three people".

## Verification

- `tests/contract/owner_overview_cases.py`: one person per tier all appear on
  the first page in closeness order with nothing left to continue; nine people
  across five tiers show eight and name the eighth as the continuation; the
  deliberate continuation page still holds three.
- Both persistence adapters run the cases: `tests/unit/test_owner_overview.py`
  and `tests/integration/test_owner_overview_postgres.py`.
