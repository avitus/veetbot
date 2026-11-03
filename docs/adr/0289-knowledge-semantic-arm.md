# ADR-0289: The knowledge semantic arm is built, measured, and off by default

- Status: Proposed (owner direction on 2026-11-02; activation needs the production space measured)
- Date: 2026-11-02
- Related: ADR-0033, ADR-0178, ADR-0285
- Detailed design: `docs/plan/knowledge-documents.md`

## Context

On 2026-11-02, after ADR-0285 made knowledge search rank by coverage, the
owner asked a fresh chat about valve-like sounds from the parked car. The run
called `knowledge.search` twice; both calls returned the only document, the
car's 424-page owner's manual (403 chunks of about 560 tokens), but the wrong
passages: Remote Software Upgrade, the A-to-Z index, Media at a glance. The one
relevant passage, under Driving tips, Things to remember when driving (page
313: audible noise such as the radiator fan several minutes after the engine
is shut off is normal), never surfaced.

An offline reproduction with the project's own extractor, chunker and
`lexical_query_forms` showed why. The question says sounds, valves and parked;
the passage says noise, radiator fan and shut off. No spelling of any content
word is shared, so lexical coverage cannot bridge the paraphrase. Dropping
function words from the query and weighting terms by rarity were both tried
and neither lifted the passage; only a question in the passage's own words
did.

The knowledge design already names the answer and its condition. The Indexing
section builds `pgvector` behind the same reciprocal-rank-fusion interface
memory retrieval uses, enabled only when the harness shows lift over lexical
retrieval on a corpus gate; build step 6 deferred it on that condition.

## Decision

1. **Chunks are embedded at ingestion.** When the arm is on, ingestion embeds
   each chunk's heading path over its text (`knowledge-text@1`, cut to the
   embedding request's per-item bound) before the unit of work opens, with the
   provider the memory semantic index admitted, and writes one row per chunk
   and embedding space to `knowledge_chunk_vectors`, a `pgvector` column that
   cascades with its chunk. The in-memory store keeps the same rows. An
   embedding failure fails the ingest, as every other step does: a document
   half of whose passages the arm can see is worse than one tried again, and
   the chat-upload path already backs off and retries (ADR-0284). Egress
   follows the memory index's rules: a remote provider receives only public
   and internal text (`PROVIDER_EGRESS_SENSITIVITIES`), so a restricted
   document stays lexical; a local provider embeds every sensitivity; and the
   index's admission check is read before every provider call, so a
   withdrawn admission stops knowledge egress with the rest.
2. **Search embeds the question, then fuses.** The question is embedded before
   the unit of work opens; a question the provider cannot embed, or whose
   response fails validation, is answered by the lexical arm alone, logged
   once with the error class, and the trace's retrieval policy version names
   the space and channel that answered. A semantic hit below
   `KNOWLEDGE_SEMANTIC_MIN_SIMILARITY` (0.3) is no candidate, as a chunk
   holding none of the query's terms is none for the lexical arm; on the
   recorded fixture answering passages score 0.348 and above and passages of
   the other documents 0.333 and below, and a question none of the documents
   answers returns nothing. Fusion is the domain's shared
   `reciprocal_rank_fusion`, which belief recall's `_rrf_fuse` now calls, so
   the two paths cannot drift.
3. **Two channels, memory's by name.** `hybrid` fuses the lexical and semantic
   arms by reciprocal rank and blends the result with the better arm score at
   memory's fusion weight. `dense` orders candidates by the semantic arm alone,
   as memory's dense channel skips its sparse query, except that the lexical
   arm still reaches the chunks the space has not seen, so a document
   ingested before the arm was turned on stays reachable rather than
   disappearing until it is ingested again. The ADR-0285 lexical ranking is
   the lexical arm in `hybrid` and the whole of search when the arm is off;
   it is not changed.
4. **Off by default, and the gate decides the channel.** The arm sits behind
   `AGENT_KNOWLEDGE_SEMANTIC_ENABLED`, default off, and shares the semantic
   index's admitted provider (`AGENT_MEMORY_SEMANTIC_INDEX_*`), so it never
   composes an embedding egress of its own; without a composed index it logs
   `knowledge_semantic_unavailable_using_lexical` once and search stays
   lexical. `AGENT_KNOWLEDGE_SEMANTIC_CHANNEL` defaults to `dense`, the one
   configuration the measurement below showed lift under.
5. **The activation condition.** The arm turns on in production only after
   `gate.knowledge.paraphrase_lift` is re-recorded with the embedding space the
   semantic index will run (`scripts/record_knowledge_embeddings.py`, then the
   gate) and still passes, and the owner has accepted where chunk text goes:
   nowhere with a local provider, to the provider with a remote one. The
   recorder as written records one space, `text-embedding-3-small` at 1536
   dimensions through the OpenAI adapter; recording a local or other
   provider's space means extending the script first, and a passing replay
   of the OpenAI fixture is evidence for that space only.

## Evidence

The gate's corpus (`evals/corpora/knowledge_paraphrase/`) holds three
documents modelled on the manual: an owner's manual, a thermostat guide and a
printer guide, each with one answering passage that shares no content word
with its question and three or four other sections that share several. The
vectors are `text-embedding-3-small` at 1536 dimensions, recorded once on
2026-11-02 and replayed by text digest, so `make check` measures a real model
without a provider call. Ranks are the answering passage's position in what
`knowledge.search` returns with the tool's own parameters (ten passages, two
per document). `scripts/record_knowledge_embeddings.py --report` reproduces
the table.

| Member | Lexical alone | Hybrid fusion | Dense | Semantic arm alone |
| --- | --- | --- | --- | --- |
| Owner's manual (valve-like sounds after parking) | miss | miss | 2 | 2 of 5 |
| Printer guide (grabbing several pages) | miss | miss | 1 | 1 of 4 |
| Thermostat guide (heating clicks on and off) | miss | miss | 2 | 2 of 4 |

Lexical search misses every member: the answering passage holds none of the
question's content words, the document's other sections hold several, and the
two-per-document cap cuts it, which is the production miss. Hybrid fusion also
misses every member, and the reason is structural rather than a weight to
tune. Reciprocal rank fusion rewards a candidate both arms rank. The lexical
arm counts function words as coverage (ADR-0285 keeps `plainto_tsquery` under
the `simple` configuration, where "the" and "from" are terms), so a document's
other sections rank first lexically, and they also rank second or third
semantically because they share the question's words; a passage the semantic
arm ranks first but the lexical arm does not rank at all cannot outscore them
at any `k`. The dense channel places every member within the top two.

## Consequences

- With the flag off nothing changes: no vector is written, no provider is
  called, and search is ADR-0285's lexical ranking.
- With the flag on and the dense channel, a paraphrased question reaches its
  passage, and an exact-term question (a part number, an error code) relies on
  the embedding model rather than on term matching. The hybrid channel keeps
  both arms for the day the lexical arm stops counting function words, when
  its lift should be re-measured.
- Documents ingested before the flag was turned on have no vectors; under
  either channel the lexical arm still reaches them, and they gain vectors
  only when ingested again, which makes a new version of the document.
- Knowledge embedding spend is not metered by the memory semantic ledger; a
  403-chunk manual is about 225,000 tokens once. The local provider admits one
  request at a time, so a chunk batch running while recall embeds a question
  fails that ingest attempt and the upload path retries it.

## Verification

- `tests/gates/test_knowledge_semantic.py`: `test_paraphrase_lift` (gate 13)
  passes under the dense channel; `test_lexical_alone_misses_the_paraphrase`
  and `test_hybrid_fusion_does_not_reach_the_top_two` record the baselines
  the lift is measured against; `test_an_unrelated_question_returns_no_passage`
  is the floor's negative case.
- `tests/contract/test_knowledge_semantic_contract.py`: the lexical ranking is
  unchanged without a semantic query, both channels name their arms, a chunk
  without a vector in the space stays reachable through the lexical arm under
  either channel, a hit below the floor is no candidate, visibility and the
  per-document cap hold for semantic hits, deletion removes vectors, and a
  prepared ingest refuses stray or repeated vectors.
- `tests/integration/test_knowledge_semantic_postgres.py`: vectors are stored
  under the migration, both channels answer from `pgvector`, another principal
  reads nothing, and deleting the document removes the rows.
- `tests/unit/test_retrieval_fusion.py` and `tests/unit/test_memory_retrieval.py`:
  the shared fusion reproduces belief recall's numbers.
- `tests/unit/test_knowledge_embedding.py`: batching, the text bound, the
  provider call before any unit of work, the failed ingest, a restricted
  document withheld from a remote provider and embedded by a local one, a
  withdrawn admission stopping both paths, the lexical fallback on a
  provider failure or an invalid response, the policy version at search,
  and the settings.
