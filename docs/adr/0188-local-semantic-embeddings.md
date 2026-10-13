# ADR-0188: Local semantic embeddings

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-12
- Related: ADR-0180, ADR-0181, ADR-0182, ADR-0185, ADR-0187
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Admit an optional local embedding adapter alongside the existing OpenAI adapter.
Remote source and query egress restrictions remain unchanged. A local encoder
performs no network operation, uses no provider credential, loads only verified
operator-installed model files, and makes no automatic download or model fallback.
Its vectors remain derived data under the existing source version, owner, scope,
sensitivity, erasure and current-original selection predicates.

This addresses a coverage limitation of the remote path: restricted memories cannot
be sent to an embedding provider. A query shortened from restricted context retains
that restriction. Local inference avoids that additional disclosure altogether;
it does not lower the privacy labels of sources or retrieved evidence. Production
remote query preparation continues to require complete trusted provenance.

The first measured local tuple is the Apache-2.0 ONNX conversion of
`sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions, upstream revision
`d13954661f83248295ba75c1ed411eef3b7b936e`. The model and tokenizer are pinned by
SHA-256. ONNX Runtime and Tokenizers are the new inference dependencies; FastEmbed
was used for the pilot only. The adapter uses CPU execution, bounded threads,
attention-mask mean pooling and normalized vectors. Inputs exceeding the model's
256-token capacity are withheld rather than silently truncated. No pickle, custom
model code, GPU provider, telemetry or runtime model download is enabled.

Local indexing and explicit query preparation retain their durable shared admission,
source compare-and-swap, bounded batches, deadlines and cache invalidation. One
in-process inference may run at a time; cancellation discards its result and keeps
its slot occupied until the native computation finishes. There is no unbounded
queue. A local tuple explicitly names local residency and the local execution
revision, and has zero provider price. The existing ledger's minimum reservation
quantum remains a conservative admission receipt, including after uncertain cleanup;
it is not a claim that an external provider charged for local CPU work.

Query authority still binds the actual run, principal, checkpoint, surface and
reviewed reader configuration. The runtime records the reader actually selected,
including reasoning effort and summary mode, so a default catalog entry cannot
certify a different run selection. Authenticated owner chats and live paired DMs
use the private recall view; the actual transport, pairing and scopes also bind
the query context. Revocation or a reader change withholds and invalidates it. Local processing does not require an outbound
content classification because it makes no outbound request. Evidence admission
must independently verify the complete local tuple and its measured contribution.

Query encoding uses the natural task text. Subjects already present in that text
are not repeated; additional explicit subjects are appended as labeled text. The
complete structured query still determines cache identity and visibility. The
`semantic-query@2` cache namespace separates this representation from the earlier
JSON-array encoding. This applies to every subject and belief category; no fact
value, ownership pattern or evaluation label participates in the transformation.
The existing `memory-text@1` source encoding remains unchanged.

The optional runtime phase saves the actual reader and complete current checkpoint
before preparation. It runs before context assembly and pending tool batches,
including resumed batches. Automatic preparation uses the same current-message,
working-state and session-scope query as context assembly; deliberate preparation
validates and uses the same query constructor as `memory.search`. Its thirteen-second
outer limit includes the existing ten-second preparation and three-second cleanup.
Failure preserves sparse recall; cancellation propagates. Ordinary recall still
performs no embedding work. The composition owns and closes the local adapter.

## Evidence and limits

A separate synthetic diagnostic over the existing 400 information needs and 1,000
newer distractors measured local top-20 candidate recall of 95.25%; every family
reached at least 90%. This is candidate selection evidence, not the required
independent formation-to-answer release experiment. The pilot was measured on the
developer machine and makes no production latency or capacity claim. The existing
OpenAI comparison remains preserved. No M33 gate moves and no production feature
is activated by this decision.

The paired encoding comparison at
`evals/observations/general-memory-recall/20261012-local-encoding/README.md`
uses identical project-ticket distractors and actual governed rendering on all
eight runs. JSON query encoding yields 93.75% automatic and 92.25% deliberate
rendered recall. Natural text yields 95.50% for dense-only and 96.25% for hybrid on
both paths, with every hybrid family at or above 90%. Sparse recall remains 68.75%.
All runs share one recorded implementation identity and retain their complete
observations, including failures. The dense ablation uses the shared visibility,
lifecycle, trust and output-budget path, omitting sparse candidates and ranking
signals only when a prepared semantic query is available. The complete profile
and channel bind release identity. These development measurements do not replace
the independent corpus, production-shaped budget registration or answer-use gates.

The live formation pilot exposed a separate ranking failure: inferred provisional
facts can enter the candidate list and still lose every output slot to weak,
high-confidence neighbors. A bounded `semantic_fusion_weight` profile parameter
makes the existing fusion coefficient independently measurable. It defaults to
0.2, preserving the control; a 0.8 experimental arm emphasizes relevance rank.
The `semantic@2` fusion orders its lexical and structured arms by distinct term
coverage and anchor strength before applying final lifecycle/authority weighting.
Ordering those arms by the already blended score would count confidence twice
and can bury the best match even at a larger fusion coefficient. Verified
identical-claim merges transfer the strongest permitted member's arm relevance.
The trace names the fusion coefficient and semantic revision. The original
lifecycle, authority, review flag and confidence labels remain unchanged, and the sparse-only/frozen paths retain their existing coefficient.
Summary support uses the same semantic ranks and dense-only scoring rules as
originals, so it cannot reintroduce a sparse arm into a dense ablation. No weight
change is activated without comparative evidence for the full profile.

The complete release corpus, answer-use comparison, task planning/recovery,
historical/People semantic parity and final release evidence remain required.
Focused PostgreSQL integration checks exercise both paths under real worker leases;
their fabricated admission fixtures test composition, not release quality. The ownership
compatibility branch is removed only after its general-path regressions pass.

## Sources

- [Model card and license](https://huggingface.co/Qdrant/all-MiniLM-L6-v2-onnx).
- [Original model](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2).
- [ONNX Runtime inference API](https://onnxruntime.ai/docs/api/python/api_summary.html).
