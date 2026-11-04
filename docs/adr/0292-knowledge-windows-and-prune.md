# ADR-0292: The OpenAI knowledge route windows long chunks, and a superseded space is pruned once the active one is full

- Status: Accepted (continues the owner's direction of ADR-0290, 2026-11-03)
- Date: 2026-11-03
- Related: ADR-0289, ADR-0290
- Amends: ADR-0290's decision 3, "an unbounded provider gets one window
  identical to `knowledge-text@1`"
- Detailed design: `docs/plan/knowledge-documents.md`; runbook in
  `docs/deployment.md`, "Standing owner activation of semantic retrieval"

## Context

The first live chat after ADR-0290's activation proved the wiring and missed
the passage. Its knowledge traces read `knowledge@1+semantic@1:…:dense`, so
the question was embedded with OpenAI's model and the dense channel ordered
the candidates, yet the manual's answering section stayed out of the top two:
it is a 2,800-character "General driving notes" section whose one relevant
sentence is averaged with the tailgate and the particulate filter. Ranked
against the manual's 401 chunks by best window, for the owner's question, the
model's two `knowledge.search` queries and a plain phrasing, whole chunks put
it at 78, 136, 106 and 11; 128-token windows put it at 6, 7, 1 and 2 (the
table is in ADR-0290, "The first live measurement"). Windows cut both ways:
gate 13's members are short, single-topic sections, and cutting them
sharpened the thermostat guide's "confirmed with a short click" sentence into
a window that overtook the answer, so the gate failed at 96 and 128 tokens
while 160 passed it and left the manual at 41, 46, 26 and 6.

A space change also has a lifecycle. A release switches the `current` link
and then restarts every unit, so for some seconds the previous API still
searches the previous space while the new maintenance process sweeps; and a
rollback repoints `current` at a release that reads the previous space.
Pruning the previous space as the new one fills would leave both reading an
emptied space.

## Decision

1. **Windows only where a chunk is long enough to need them.** On the OpenAI
   route a chunk whose text the chunker estimates at
   `KNOWLEDGE_OPENAI_WHOLE_TOKENS` (256) or more is cut into windows under
   `KNOWLEDGE_OPENAI_WINDOW_TOKENS` (128) by the same estimate, and a shorter
   chunk stays one window. The bound measures the chunk's text alone, as the
   chunker sizes a chunk; the budget counts the heading path, since it is
   embedded. The space is `knowledge-openai@2`. The local route keeps its
   tokenizer and capacity, which a chunk may not exceed. Gate 13 is
   re-recorded under the rule: every member stays whole and passes as before
   (2, 1, 2); all but two manual chunks are cut, and the answering section
   ranks 6, 7, 1 and 2.
2. **A superseded space is pruned once the active one is full.** The
   maintenance sweep embeds the chunks of the owner's current documents that
   lack a vector in the active space, as ADR-0290 decided; in the round that
   finds none left it removes the owner's vectors of every other space
   (`KnowledgeStore.remove_embeddings_outside`, both stores, scoped to the
   tenant and ingesting principal) and logs `knowledge_vectors_pruned` once.
   Until that round the previous space stays whole, so the previous API
   during the restart overlap, and a rollback, keep their vectors; a rolled
   back release's own sweep refills its space and prunes the newer one the
   same way. Vectors are derived rows: no document or chunk changes. A
   superseded document version stays queryable by `as_of` through the lexical
   arm, which under the dense channel covers the chunks the active space has
   not seen; the sweep does not embed it.

## Consequences

- The manual is about 2,300 windows and 244,000 tokens once per space change,
  at `text-embedding-3-small` prices.
- The model's first query names the document, which pulls the front-matter
  chunk ahead of the answer under every budget; the model's refined query
  ranks the answer first. That is the query, not the ranking.
- Between a deployment and the sweep's last round the new space is partly
  filled, and a chunk without a vector is reached lexically; the runbook
  watches `knowledge_vectors_backfilled` rounds and the single prune.

## Verification

`tests/unit/test_knowledge_windows.py` (the whole bound, measured on the
text), `tests/unit/test_knowledge_provider_composition.py` (the OpenAI route's
bounds and space), `tests/contract/test_knowledge_backfill_contract.py` (the
prune waits for the full space and leaves other owners' vectors alone),
`tests/integration/test_knowledge_semantic_postgres.py` (the Postgres prune),
and gate 13 in both spaces.
