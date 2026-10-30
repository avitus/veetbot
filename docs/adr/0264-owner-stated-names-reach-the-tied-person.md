# ADR-0264: An owner-stated name reaches the person the owner is already tied to

- Status: Proposed
- Date: 2026-10-28
- Related: ADR-0121, ADR-0125, ADR-0259; gate P01; Section 5 of
  `docs/plan/people-and-relationships.md`
- Amends: ADR-0121 (identity from the owner's context), ADR-0125 decision 3
  (names only ever suggest), and the closing sentence of ADR-0259 decision 2
  ("no name is matched to a person")
- Detailed design: `docs/plan/people-and-relationships.md`

## Context

On 2026-10-28 the owner told Chat "Cheryl Vitus is my mom". Background formation
saved the fact and a mother edge, but on a new People record, "Cheryl Vitus".
The owner's mother already had a record, "Cheryl", created from the owner's own
words on 2026-09-22 with two parent edges to the owner. That record knew the
name "Cheryl Vitus" too, three times over, but every one of those aliases had
been learned from mail and carried an email context. Identity resolution reads
a name alias only within the context it was stated in (ADR-0121), so the
owner's name, stated in the owner context, matched nothing, and an unmatched
name tied to the owner creates a person. Nothing was reinforced, the mother now
had two records and three mother beliefs, and the duplicate sweep raised a merge
suggestion thirteen minutes later for the owner to resolve by hand.

The rule that stopped the match exists for a reason. Two people can share a
name (gate P01), a display name copied from a mail header is not identity, and
a name stated in one context must not pick a person known under it in another
(ADR-0121). Each of those stays true. What changed is that the owner, in their
own words, named someone they are already tied to.

## Decision

1. **The owner's words select the person the owner already has.** When a name
   is stated in the owner context (Chat) and ordinary resolution finds no
   match, the people holding that exact name as an alias in any context, or as
   their display name, are candidates. Exactly one candidate with a current,
   resolved direct relationship to the owner is a match. Two or more such
   candidates are ambiguous. A candidate the owner is not tied to is not
   selected: a name without an owner tie still identifies nobody on its own,
   and stays a merge suggestion (ADR-0125).
2. **Only the owner's statements qualify.** A name read from mail or another
   source keeps its context rule unchanged. An ended tie (`valid_to` in the
   past), a tie starting after the statement's time, or an unresolved edge does
   not qualify a candidate. The supporting belief must pass the governed owner
   relationship query's lifecycle, validity and sensitivity checks. A lone
   unqualified display-name candidate can qualify through that evidence; a
   truncated or colliding name set cannot.
3. **Nothing else moves.** Formation, the duplicate sweep, merge suggestions,
   the P01 gate and every other resolution step are unchanged. A restated tie
   now lands on the known person, where formation's own reinforcement rules
   apply.

## Consequences

- "Cheryl Vitus is my mom" attaches to Cheryl, with her existing parent edge, and
  forms no second person. The duplicate already created in production is
  resolved by the open merge suggestion, not by this change.
- A first name the owner states that two tied relatives share stays ambiguous,
  as before.
- Each owner-context name resolution that found nothing reads at most ten
  pages of one hundred relationship rows per candidate.

## Verification

- `tests/unit/test_people_resolution.py`: the tied person matches from an
  email-context alias; the same name from a source context does not; a name
  fragment does not; without a tie the name stays unresolved; two tied people
  are ambiguous; an ended tie does not qualify.
- `tests/unit/test_people_formation.py`: "Cheryl Vitus is my mom" through
  formation@11 links the existing Cheryl, writes her edge, and creates no
  second person.
