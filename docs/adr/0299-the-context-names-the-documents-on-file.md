# ADR-0299: The context names the documents on file

- Status: Accepted (continues the owner's direction of ADR-0290, 2026-11-04)
- Date: 2026-11-04
- Related: ADR-0239, ADR-0290, ADR-0292, ADR-0293
- Amends: `knowledge-documents.md`'s fifth decision, "the tool is the path",
  which now has a catalog row telling the model what the tool reaches; adds
  row 13b and a budget class to `context-engine.md`
- Detailed design: `docs/plan/knowledge-documents.md`,
  `docs/plan/context-engine.md`

## Context

ADR-0292 and ADR-0293 made `knowledge.search` return the answering section
of the owner's manual for the valve-like sounds after parking: with the
document's name dropped from the question, the section ranks second. The
first fresh chat after that deployment, asked in the owner's own words,
still never reached it, because the model never called `knowledge.search`.
It searched the public web four times and answered from a forum thread.

The trace shows why. Nothing in the context says a manual exists. The
knowledge corpus is one document, the tool's description says only "search
visible knowledge passages", and the only cue that there is anything to
search is a memory: three provisional beliefs say the owner provided the
car's manual and wants it kept for future questions. In-turn recall returned
twenty beliefs for the question, three of them restatements of the question
itself formed by the earlier chats about it, and the three manual beliefs
sat below the twentieth, behind "User has a read" and "User has a Sutter".
The two chats of the day before, asked in a shorter wording, had recalled
two of them, and in both the model searched the manual at once. The tool
call therefore depended on a provisional belief surviving a twenty-item
cut, and the question's own echo pushed it out.

The owner records design already settled the same problem for numeric
series: "the owner decided that the model should always know which series
exist" (ADR-0239), and the records catalog is one platform-rendered body
row, titles and dates and never a value. Documents are the same case. A
corpus the model cannot see is a corpus it searches only by luck, and the
cue that it exists should not be a belief competing with every other belief
for a slot.

## Decision

1. **The context carries a knowledge catalog.** Row 13b of the assembly
   order, after the records catalog and before the runtime metadata, lists
   each current document the run may read: its title, its document date
   when it has one, the day it was added, and how many passages it holds.
   Never a passage. The builder reads it inside assembly through an injected
   `KnowledgeCatalogSource` (`context/knowledge_catalog.py`,
   `knowledge/catalog.py`), as it reads the records catalog, with the run's
   principal, the session's project scope, the constant `restricted`
   ceiling and the class's document limit. Both stores implement
   `KnowledgeStore.catalog` under the search's own visibility, tenancy,
   validity, erasure-fence and sensitivity rule, so the catalog names
   exactly what a search could return. A superseded document appears once,
   as its current version.
2. **The tool is still the path, and the catalog follows it.** The row is
   absent, with no bytes, when the session's roster lacks
   `knowledge.search`, when no source is injected, when the class is absent
   (zero caps), or when the store holds no document the run may read. A
   session without documents is byte-identical to one built before this
   decision. A failed read logs `context_knowledge_catalog_failed` and
   assembles without the row. A `knowledge.search` that names no `scope`
   reads under the session's project scope, as `memory.search` does, so one
   search reaches every document the catalog listed; a passed `scope` still
   wins, and the tool's spec is unchanged.
3. **Its own budget class, dropping whole.** `classes.knowledge_catalog`
   in `context/plan.yaml`, twelve documents and 400 tokens, absolute caps;
   `ContextBudget` gains `knowledge_catalog_tokens`. The twelve earliest by
   title render and a last line counts the rest; a catalog over its token
   cap drops whole. Under window pressure it yields whole and second, after
   the records catalog and before knowledge passages and in-turn recall
   trim, because a catalog that names some documents misstates the corpus
   and one `knowledge.search` reaches what it named.
4. **One framing sentence, and a builder rotation.** Row 2's platform
   framing gains one fixed sentence: a knowledge catalog, when present,
   lists the documents on file by title with their dates and passage
   counts, holds titles and never passages, and a question that falls
   within a listed document is searched with `knowledge.search` before it
   is answered from general knowledge. The sentence is in every session's
   prefix whether or not a catalog renders, so the builder identity
   advances to `context-builder@16` and every earlier plan re-plans once
   through the ordinary logged epoch rotation.
5. **The item is `INTERNAL_TOOL` data with `source="knowledge.catalog"`**,
   rendered inside the ordinary envelope exactly as a tool result would be.
   Titles are the owner's own and pass the shared credential rule at
   ingestion, since search results and the catalog both render them; the
   catalog applies the rule again when it renders, so a title stored before
   the rule renders `untitled`. The row lowers
   neither the request's origin trust nor the executor's turn label. A title
   renders on one line whatever it holds, its line breaks and whitespace runs
   collapsed to one space, so no title can forge a second entry.

## Consequences

- A fresh chat about the car knows a manual is on file before it decides
  how to answer, whatever memory recall returns for the question. The proof
  is the same chat as before, asked again after deployment.
- Memory keeps its role as what the owner said about the manual; the
  catalog is what the corpus holds. A belief that the owner "provided the
  manual" may still be recalled, and nothing depends on it.
- Every open session re-plans once at the next turn, as it did for
  ADR-0239's sentence; the fifty-turn prefix script still holds one prefix
  under its existing padding.
- The catalog costs each turn with documents one short row inside a 400-token
  class; the owner's corpus of one document costs one line. A corpus past
  twelve documents renders twelve and a count, and `knowledge.search` still
  reaches the rest; a `knowledge.list` tool for the model is not built here.
- The versioned-knob count moves from 224 to 226.

## Evidence

- `tests/unit/test_knowledge_catalog.py`: the rendering, byte for byte, a
  title with line breaks on one line, and a stored credential-like title
  rendered `untitled`.
- `tests/gates/test_knowledge_m9.py`: a search without a scope reaches the
  session's project documents, and a passed scope wins; a credential-bearing
  title fails the ingest.
- `tests/unit/test_knowledge_catalog_context.py`: row 13b's bytes, trust and
  position; absence without a source, a roster entry or documents; the
  whole-row yield ahead of recall and behind the records catalog; the reader
  over a store.
- `tests/contract/test_knowledge_passage_search_contract.py`: the store
  catalog under the visibility rule, the cap, supersession and the erasure
  fence.
- `tests/integration/test_memory_knowledge_persistence_m9.py`: both stores
  agree on the catalog of an ingested document.
- `tests/unit/test_context_budget_allocator.py`, `tests/unit/test_config.py`,
  `tests/unit/test_context_rendering.py`: the class, the knobs, the framing
  sentence and the builder version.
