# ADR-0087: Corrections retract, a provider re-pass recovers evidence, and the formation@9 artifact is withdrawn under distillation-scorer@3

- Status: Proposed
- Date: 2026-09-04
- Related: Milestone 21 of the engineering plan; ADR-0077, ADR-0086
- Detailed design: `docs/plan/adaptive-memory-distillation.md`

## Context

An independent staff-level review of the `formation@9` branch at `6082006`
reproduced eight release-blocking defects, all still present on the deployed
`main`:

1. Ordinary corrections were discarded. The deterministic fallback dropped
   every legacy retraction, the provider schema had no polarity, and local
   validation rejected any candidate citing a correction cue, so "I don't
   drive my BMW anymore" formed nothing under `formation@9` while the
   watermark advanced and the old belief stayed live. The correction cue also
   missed "no longer take", "stopped attending", and "gave up".
2. A single user message over 32,768 characters raised an uncaught validation
   error building the lossless fallback episode: segmentation permits
   ninety-six kilobytes and never splits an event, but the narrative bound was
   a third of that.
3. The comparative scorer was an order-insensitive bag of words that ignored
   numbers of one hundred or more, so "prefers tea to coffee" matched "prefers
   coffee to tea" and "ran 100 miles" matched "ran 200 miles" in a long enough
   sentence. The published lift could therefore credit materially wrong
   memories.
4. Represented-clause verification checked one-third token overlap and
   nothing else, so "User can take meetings on Fridays" was accepted as
   representing "I cannot take meetings on Fridays", the one case the
   verification existed to catch.
5. The corpus had been edited after observing model output, with no frozen
   holdout, so the artifact was development data presented as independent
   activation evidence.
6. The populated-store gate checked only that seeds were written. A provider
   returning zero predictions and zero attributed redundancies for every case
   could still publish.
7. `formation@9` disabled provider retry, so an outage committed whatever the
   fallback recognized and permanently consumed the evidence, the same root
   cause as the earlier production loss.
8. The fallback resolved "that" to the last recognized subject across
   intervening sentences and formed "User wants to improve their swimming"
   from a question about a sourdough recipe.

The review also found the runtime combiner merging same-subject candidates
regardless of assertion, an unbounded anticipation prefix, a build reference
never compared to the repository's history, an unbounded cost in the artifact,
and plan text still saying twenty-four gates and three calls per consolidation.

## Decision

1. **Corrections update memory and never create it.** The fallback recognizes
   stated ends to activities and passes legacy retractions through; the
   provider's candidate schema gains `polarity`, and local validation accepts
   a correction clause only as a retraction and a retraction only with a
   correction clause. At commit a retraction supersedes the live belief under
   its conflict key; with nothing live to retract it is counted as
   `skipped_unmatched_retraction` and forms nothing.
2. **A retryable provider failure schedules a bounded re-pass.** `formation@9`
   still completes with its audited fallback and advances the watermark (gate
   6 is unchanged), then appends a `provider_retry` request naming the
   consumed source range with the `formation@8` attempt limit and backoff. The
   re-pass re-reads that range, commits what the provider adds, resolves what
   the fallback formed as the same source, keeps an already-stored episode
   when it re-derives its key, and audits exhaustion after the last attempt.
3. **`distillation-scorer@3`.** Polarity is compared as parity, with a
   negation inside a subordinate circumstance treated as a qualifier; absence
   conditions such as `without` must match; counts must match; large numbers
   must match when both statements carry one; and the object after every
   directional marker both statements share must match, with a preference's
   "to" read as "than". Represented-clause verification
   applies the same compatibility floor and then requires half the memory's
   content in the clause. The runtime combiner applies the floor before any
   subject- or overlap-based merge, so contradictions reach consolidation.
4. **Evidence must show anticipation working.** The artifact schema is version
   3 and requires `represented_case_count` of at least one; a corpus case may
   label `represented_text` only under a seed pool that asserts it, the corpus
   must contain such a case, and publication fails unless every labelled clause
   was verifiably represented. The corpus gains a seeded case that restates a
   seed across a segment boundary, where the blinded prefix carries a cue. The
   artifact refuses a cost above one thousand US dollars, and the bundle test
   requires every build reference to be an ancestor of the bundling tree.
5. **The `formation@9` artifact is withdrawn.** It was published under the
   superseded scorer and the weaker gates. `auto` selects `formation@10` for
   the production tuple until a re-evaluation on the deploying tree passes.
   Re-activation additionally requires a frozen holdout corpus authored
   without observing model output; the corpus digest binding is the hook, and
   the holdout itself is an open item rather than part of this change.
6. **Bounds.** The episode narrative bound holds the largest single message
   the API accepts plus a citation prefix per event of a full segment, and the
   anticipation prefix keeps the most recent text under twice the segment byte
   limit. The fallback resolves a pronoun only to the clause it follows.
7. **Plan text is reconciled.** Milestone 21 carries thirty-one gates, and a
   consolidation makes three calls per planned segment, as ADR-0077 already
   decided.

## Second review

A second review of the first fix commit (`3d1c123`) found six residual
defects, each fixed with a regression test in the same change set:

1. Fallback retractions keyed on the object alone missed assertions keyed on
   gerund and object. The fallback now keys retractions the way the matching
   assertion does, and consolidation retracts every live affirmative belief
   about the user whose statement contains what the retraction negates,
   under whatever key it was filed.
2. A provider retraction could carry an affirmative statement and supersede
   the old belief with a record saying the opposite of the correction. A
   retraction must now state the negated claim.
3. The scorer equated argument swaps and lost a negation behind a fronted
   subordinate clause. Shared content terms must now keep their order, and
   negation is scoped to the clause, with the same rules in clause
   verification.
4. Two claims under one subject and kind were merged on the key alone. The
   combiner now merges only one claim in two wordings and files a second
   claim under a key extended with its distinguishing words.
5. The anticipation prefix bound exempted an oversized newest event; that
   event is now sent as its tail.
6. An operator-supplied artifact was matched on the policy tuple alone.
   Startup now computes the digest of the corpus the running tree ships and
   activates no artifact whose digest differs. The build reference remains
   unverifiable at runtime and is checked by ancestry in the bundle test.

## Third review

A third review, after PR #98 merged, found four residual defects, each fixed
with a regression test:

1. A fronted subordinate clause without a comma still hid the main clause's
   negation. The scope now ends where the main clause's subject begins, so
   "Although busy I do not take ..." is negated and "When I am not lifting I
   run" is not.
2. Two claims built from the same words in a different relationship kept one
   key and the store dropped the second. The combiner now keys such a claim by
   its names in order, or by an ordinal when nothing else separates it.
3. Retraction targeting matched raw term subsets, missing "is running
   outdoors" and catching "tracks runs outdoors in a journal". Targets now
   share the retraction's main verb and contain its lemmatized content.
4. An operator-supplied artifact was bound to the corpus but not to a build.
   Startup now activates an operator file only when its build reference is
   the running release's commit; outside production the gap is logged. The
   frozen holdout corpus and the re-evaluation remain open items.

The first live re-evaluation on the fixed tree (2026-09-08) surfaced four
more defects, each fixed with a regression test. A defaulted `polarity` made
the candidate schema non-strict and the provider rejected every distillation
call, so every stage schema is now checked for optional properties. The
scorer's term-order rule rejected "modify the routine to improve it" against
"improve the routine", so `distillation-scorer@4` holds proper names to their
exact order and tolerates one displaced shared term, still rejecting a
two-argument swap. The provider stored an "unspecified activity" and an hour
of dishwashing, so a statement with no object or a completed one-off event is
now rejected locally. And the combiner had turned "loves learning about
exoplanets" into a duplicate keyed "exoplanets loves learning", so a claim
whose kind is its subject (interest, preference, relationship, role,
constraint) merges under its key unless the statements contradict.

## Consequences

- Merging this deactivates `formation@9` in production until it is
  re-evaluated (about one US dollar, run on the tree that deploys) and its
  artifact rebundled. `formation@10` remains active for the tuple, so no
  consolidation falls to deterministic formation.
- Attributed redundancy remains unreachable on a single-segment consolidation,
  because the blinded prefix is empty by construction; the new corpus case
  exercises the mechanism as designed, across a segment boundary. Giving
  anticipation a cue in the common case is a design change recorded as an
  open item, not decided here.
- Two candidates with the same subject and claim kind still merge on wording
  alone, because that pair is the conflict key the store resolves on; only
  contradictions are now kept apart.
- Unmatched fallback retractions can only retract the exact conflict key they
  render; the provider path carries corrections whose subject wording differs.

## Alternatives considered

- **Hold the watermark on a provider failure, as `formation@8` does.**
  Rejected: it discards the fallback's immediate memories and contradicts the
  audited-fallback gate.
- **Keep `distillation-scorer@2` and the bundled artifact.** Rejected: the
  artifact's numbers were computed by a scorer shown to credit reversed
  comparisons and mismatched numbers.
- **Compare full term order.** Rejected: paraphrases reorder freely; only the
  object after a shared directional marker carries meaning.
- **Add a runtime comparison of the build reference to the running release.**
  Rejected: the artifact is necessarily bundled in a later commit than the one
  it evaluated; ancestry is checked where history exists, in the bundle test.
