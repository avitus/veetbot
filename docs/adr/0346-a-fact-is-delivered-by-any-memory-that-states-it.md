# ADR-0346: A question's fact is delivered by any live memory that states it

- Status: Proposed (found while diagnosing the owner question set on
  2026-11-23; it changes how the owner's yardstick counts, so it waits for the
  owner's approval)
- Date: 2026-11-23
- Amends: ADR-0339 (decision 7, how an expected text is matched to memory)
- Related: ADR-0338, ADR-0340, ADR-0341, ADR-0343
- Detailed design: `docs/plan/memory-evaluation-and-lifecycle.md`
- Scope: the probe driver and its report version. No retrieval, ranking,
  packing, schema, milestone or gate change.

## Context

The owner question set (ADR-0338, ADR-0339) names each expected fact by a
pinned memory or by text that a stored memory contains. For a text, the driver
chose one memory to follow: the newest live memory whose statement contains
it. Every stage and the pass or miss were then read from that one memory.

That was sound while a text matched one memory. It stopped being sound once
mail and websites began forming memories (ADR-0233, ADR-0340). A short text
such as a product model or a place now sits in several live memories: the
owner's own statement and later mentions in mail. The newest is usually a
mention. The driver followed the mention, and reported the fact as never found
or ranked far down while the owner's statement was delivered near the top.

Measured on a clone of production taken on 2026-11-23, 114 questions, both
paths, no model calls, semantic recall on every path:

| | Questions passed | Development | Held-out | Required facts delivered |
| --- | --- | --- | --- | --- |
| Newest memory only (`@1`) | 81 of 114 | 42 of 58 | 39 of 56 | 93 of 125 |
| Any live memory (`@2`) | 99 of 114 | 52 of 58 | 47 of 56 | 111 of 125 |

Recall is identical in both rows; only the scoring differs. 45 of the 125
required facts are named by a text that more than one live memory contains.
Eighteen questions were reported missed although a memory stating the fact was
delivered. No question that passed under the old rule misses under the new one.

The same driver already counts forbidden text the other way: a question fails
when any delivered memory contains a forbidden text. The two checks disagreed
about what a text names.

The earlier reading also let the measure move for reasons unrelated to
recall: which memory is newest changes whenever mail forms or re-rates a
memory that mentions the same text. Between the runs of 2026-11-22 and
2026-11-23 seven facts moved between "never found" and "ranked far down" with
no retrieval change.

## Decision

1. **A text names a fact.** Every live memory whose statement contains an
   expected text states that fact. The fact reaches the furthest stage any of
   them reached, and the report carries that memory's identifier and rank.
2. **Ties go to the newest.** When several reach the same stage and rank, the
   newest is reported, as before.
3. **A fact with no live memory is still explained.** When no live memory
   contains the text, the newest retired or superseded one stands for it, so
   the report says `stored_but_retired` or `stored_but_superseded` as before.
4. **A pinned memory is followed alone.** A question that means one particular
   memory pins it; nothing else can stand for it.
5. **The report names its rule.** Reports are `owner-recall-probes@2`.
   `owner-recall-probes@1` reports still load. Two runs are compared only
   under the same rule.

## Consequences

- The question set measures whether the fact reached the model, which is what
  the owner asks of it. The count to compare later runs with becomes 99 of 114
  on the 2026-11-23 store; 80 and 81 were `@1` counts.
- A loose text is satisfied by any memory that contains it, including a
  mention. That was already true whenever the mention happened to be the
  newest match. A question that needs the owner's own statement should pin it
  or quote more of it; that is the owner's edit to make on the question page.
- A recorded result on production's question page was scored under `@1` until
  a run made with this change is recorded. Recording an `@2` report needs the
  release that carries this change.
- The store still holds the same fact several times across chat, mail and
  websites. This decision stops that from hiding a delivered fact in the
  measure; it does not merge the copies.

## What the remaining misses are

With the scoring corrected, 15 questions miss (6 development, 9 held-out),
covering 14 required facts, one missing name and one forbidden text:

- 5 facts sit just below the delivered list, at ranks 20 to 43. The automatic
  list is cut at 20 memories in 88 of 114 questions while using a median 880
  of its 2,000 tokens.
- 4 facts share almost no words or meaning with their question (best
  similarity under 0.2, at most one common word). No ranking order reaches
  them; they need the question's subject resolved first.
- 5 facts are person-linked or restricted memories that the recall's People
  scope or sensitivity ceiling leaves out. The one examined on the development
  half is linked to a different People record than the one the question
  resolves to.
- 1 question needs a name the owner overview does not show, and 1 delivers a
  memory it should not.

Four alternative scoring formulas were replayed offline over the captured
candidates (rarity-weighted word match, combining word and meaning evidence,
and both together). None helped both halves: the best gained two development
questions and lost two held-out ones. No ranking change is proposed here.

## Validation

- Red test first: `test_a_fact_is_delivered_when_any_live_memory_holding_its_text_is`
  failed on the stage assertion (the driver reported the newer, ineligible
  memory) before the change.
- `tests/unit/test_owner_recall_probes.py` and
  `tests/integration/test_owner_recall_probes_postgres.py` pass in the Linux
  container, the latter against a disposable PostgreSQL.
- Before and after runs on the same untouched clone template, reset between
  runs; reports hold identifiers, stages and ranks only and are kept outside
  Git.

## Not decided here

Lengthening the delivered list, resolving a question's subject before search,
duplicate People records, and merging the same fact across chat, mail and
websites.
