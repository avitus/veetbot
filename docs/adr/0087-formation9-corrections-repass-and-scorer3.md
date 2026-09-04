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
   negation inside a subordinate circumstance treated as a qualifier; counts
   must match; large numbers must match when both statements carry one; and
   the object after every directional marker both statements share must match,
   with a preference's "to" read as "than". Represented-clause verification
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
