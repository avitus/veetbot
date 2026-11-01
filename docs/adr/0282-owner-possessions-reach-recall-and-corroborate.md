# ADR-0282: Owner possessions reach recall from "do I" questions and corroborate across chats

- Status: Accepted (owner direction on 2026-11-01)
- Date: 2026-11-01
- Related: ADR-0232, ADR-0247, ADR-0248, ADR-0259, ADR-0281
- Detailed design: `docs/plan/memory-retrieval-and-ranking.md`,
  `docs/plan/memory-formation-and-consolidation.md`

## Context

After ADR-0281 restored recall, the owner asked a fresh chat "What car do I
drive?" and Veetbot answered that it did not reliably know. The recall traces
show the belief "User has a 2026 BMW X3 M50." as a candidate in every read of
that chat, dropped for budget every time. Replaying the scoring gives 0.338
against 0.357 for the twentieth returned belief; it lost to "User has a X.",
"User has about a cup of alcohol infused with lemon peels." and "User has a
book."

Three things held it down at once. The question shares no word with the
statement, so the lexical arm gave nothing. The possession anchor of ADR-0232
requires a literal "my", and "do I drive" only set a type hint worth a 0.25
match instead of the anchor's 0.5. And the belief had been provisional since
September: the owner had named the car in three chats, under three subject
spellings, and ADR-0232's restatement match was deliberately limited to one
session, so the three copies never corroborated and none was promoted; the
frozen snapshot excludes provisional beliefs, so no new chat ever started with
the car. As an active belief the same fact scores 0.416, under the valve-chat
wording "my car" it scores 0.438, and either would have been returned.

## Decision

1. **A profile question about a possession anchors like "my".** When a
   clause is a personal profile question (`what/which … do I …`, `do I …`)
   whose verb is one the canonical possession statement uses (`have`, `own`,
   `drive`, `use`, `wear`), the query sets `include_owned_resources`. Advice
   questions ("Which car should I buy?") and residence questions ("Where do I
   live?") keep their own anchors; relationship and preference clauses are
   unchanged.
2. **A possession restated in another chat is the same possession.** The
   narrow restatement key of ADR-0232 (complete object, all numbers, `has` and
   `owns` agree, `new` before a number nonessential) now matches across
   sessions for direct affirmative `user_model_attr` claims not scoped to a
   person. The match reinforces and promotes through the existing duplicate
   path; it still never merges people, never uses subject similarity, and
   never treats a changed number or a negation as a restatement.
3. **A one-time repair folds the existing duplicates.** `agent memory
   merge-possessions` groups live direct possessions by the restatement key.
   The newest member of a group becomes the canonical belief: active, carrying
   the group's corroboration count, sources and scopes, and the group's best
   confidence raised by 0.1 per folded copy; the others are superseded by it,
   each with a `memory.superseded` event. A possession whose object is a
   single character ("User has a X.") is a formation placeholder and is
   retired. The dry run is the default; `--confirm` writes through the
   governed path with fresh store positions and no SQL status edit.

## Consequences

- "What car do I drive?" and "I parked my car" both reach the car fact; the
  former by the new anchor, the latter by the existing one.
- Repeating a possession in a new chat strengthens it instead of cloning it.
- The repair changes three provisional car beliefs in production into one
  active one; the two email-sourced hypotheses about the same vehicle are not
  possessions and are untouched.

## Verification

- `tests/unit/test_memory_recall_regressions.py`: four "do I" possession
  questions anchor; advice and residence questions do not.
- `tests/unit/test_owner_memory_formation_incident.py`: the same car stated
  in another session is one active belief with corroboration two; the repair
  folds two legacy copies, promotes the newest, retires the placeholder,
  leaves a lone possession alone, and is idempotent.
- `tests/unit/test_memory_review_cli.py`: the command reports by default and
  writes on `--confirm`.
