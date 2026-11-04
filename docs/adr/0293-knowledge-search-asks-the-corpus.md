# ADR-0293: A knowledge search asks the corpus, not the catalogue

- Status: Accepted (continues the owner's direction of ADR-0290, 2026-11-03)
- Date: 2026-11-03
- Related: ADR-0285, ADR-0289, ADR-0290, ADR-0292
- Amends: `knowledge-documents.md`'s "per-document cap of two passages", which
  now reserves room rather than capping the result
- Detailed design: `docs/plan/knowledge-documents.md`

## Context

With ADR-0292's windows deployed, the maintenance sweep embedded the owner's
manual in 2,261 windows and pruned the whole-chunk space, and a fresh chat
about the valve-like sounds after parking still did not reach the answering
section. Its trace showed two reasons, neither in the ranking itself.

The model aims its searches at the document. Both of its queries named it,
"2026 BMW X3 M50 owner manual …" and "\"BMW Owner Manual.pdf\" …", and so did
both queries of the chat before it. A document's name matches its cover page
and the sections that carry the brand, so the cover ranked first in every
search and the answer 24th and 25th; the same second query with the file name
taken out ranks the answer second, and the earlier chat's two queries rank it
second and first. The owner's own words rank it sixth.

The corpus is one document, and `knowledge.search` caps a document at two
passages. The cap exists to stop one verbose document filling the budget when
others have something to say; with one document it stops the search at two
passages while the 3,000-token budget would hold five or six, so the answer
had to rank first or second to be returned at all.

## Decision

1. **A question drops the name of any visible document.** Before either arm
   runs, the service lists the titles the query may read
   (`KnowledgeStore.visible_titles`, both stores, under the search's own
   visibility, validity and sensitivity rules) and removes each title, or the
   title without its file extension, where it appears as written, a
   contiguous run in any case and script with or without a possessive, its
   punctuation counting ("C#" is not "C++"), together with the quotes around
   it (`knowledge_question_without_document_names`). Title words that appear
   apart stay, since a question may share a word with a title; a question
   that is nothing but a name stays whole. The trace keeps the question as
   asked.
2. **The per-document cap reserves room; it does not cap the result.** Both
   stores take every document's first `max_per_document` passages in rank
   order, then the rest of the ranking up to `max_passages`; the service's
   token budget bounds what is rendered, as before. A verbose document still
   cannot crowd another document's first passages out, and a corpus of one
   document fills its budget.
3. **The tool's description is unchanged.** Telling the model what to put in
   `text` would change the pinned `knowledge.search@1.0.0` spec, which every
   open chat compares at restore, and add tokens to rosters that sit under
   their cap; that lever waits for a tool version with a legacy spec.

## Consequences

- Measured on the manual against the deployed windows: of the four queries
  the model wrote across the two live chats, three now rank the answer where
  the budget returns it (second, first and second); the fourth guessed words
  the section does not use and stays out under any rule.
- A question about a document whose title is its subject, "Thermostat guide",
  keeps the subject word unless it names the document as a phrase; naming it
  removes the phrase, and the document's own sections still match the rest.
- Each search reads the visible titles in a short transaction before the
  question is embedded; the titles never leave the service.
- Gate 13 and the coverage gates are unchanged: their questions do not name
  their documents, and the first two passages of a document still come
  first.

## Verification

`tests/unit/test_knowledge_question.py` (the name rule), `tests/contract/
test_knowledge_passage_search_contract.py` (the reserving cap and visible
titles in the in-memory store), `tests/unit/test_knowledge_embedding.py` (the
embedded question drops the name and the trace keeps it), and
`tests/integration/test_memory_knowledge_persistence_m9.py` (both stores give
a lone document its third passage and list its title), with gate 13 and the
Milestone 9 knowledge gates as before.
