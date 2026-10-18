# ADR-0191: Separately admitted relevance reranking

- Status: Experimental implementation under ADR-0178; activation remains gated
- Date: 2026-10-12
- Related: ADR-0178, ADR-0185, ADR-0188
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

The full-runtime development pilot found a stored coaching routine among permitted
candidates but outside the twenty returned items. People context used its existing
sparse path, where confidence, review status and reinforcement outweighed stronger
relevance. Another source anchor ranked seventh even though its semantic similarity
exceeded every distractor, so the three-source recovery bound did not reach it.
These are ranking failures, independent of possession or a particular memory type.

## Decision

Retain reciprocal-rank fusion as the baseline and add an independent, default-off
reranking ablation. After visibility filtering and validated equivalence collapse,
compute the strongest of distinct query-term coverage, structured-match strength
and nonnegative semantic cosine similarity. Give that signal 0.8 of the final score
and the existing pre-fusion metadata score 0.2. The existing duplicate penalty,
item/token caps, lifecycle exclusions, confidence labels and uncertainty rendering
remain mandatory. A relevance score is not factual certainty.

The dense-only comparison supplies no sparse signals. Sparse and People reads may
use their permitted lexical/structured signals; this does not activate semantic
People retrieval or widen an identity scope. Verified equivalent originals may
transfer their strongest permitted relevance signal, but never create extra votes.
Historical questions retain their existing revision selection and visibility rules.
Frozen controls using recent candidate selection keep their original ranking.

The profile names `reranking: baseline` or `relevance`. A separate admission callback
is mandatory, even for a relevance profile. Production composition supplies the same
version-bound release admission used by the semantic services; missing or withdrawn
evidence restores the baseline. Explicit isolated evaluation may supply its own
admission for fabricated inputs. Traces identify `relevance-rerank@1` only when it
actually ran. Reranking adds no provider call, query, source expansion or model cost.

## Evidence and limits

A regression with one provisional, inferred, review-flagged fact and eighty newer
high-confidence neighbors failed for semantic, linked and lexical signals. The
experimental reranker places the stronger fact first without changing its authority
or lifecycle labels. Withdrawal restores the previous order. Full comparative
quality, cross-family regressions and operational evidence remain required before
activation. Raw cosine and lexical coverage are not calibrated probabilities;
this transparent formula is an experiment, not a claim of optimal ranking.
