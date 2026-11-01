# ADR-0284: Knowledge search ranks by coverage with folded terms, and PDF outlines become heading paths

- Status: Accepted (owner direction on 2026-11-01)
- Date: 2026-11-01
- Related: ADR-0120, ADR-0259, ADR-0283
- Detailed design: `docs/plan/knowledge-documents.md`

## Context

While the owner's car manual was searchable in October, its searches returned
front matter and a "reset vehicle data" passage for a question about the
blind-spot indicator, and nothing for a question about the seat lever. The
PostgreSQL store ORed the query's terms and ranked by `ts_rank_cd`, which
rewards a passage that repeats one common word over a passage that holds
several of the question's words; it matched exact spellings only under the
`simple` configuration, so "valves" did not find "valve"; and a PDF's chunks
all carried an empty heading path, because the extractor emitted page text
without the bookmarks the document carries, so the heading part of the index
was blank for the one kind of document the owner sends most.

The in-memory store ranked by a different formula again, so the contract
suite could not see the production ranking.

## Decision

1. **Coverage ranks first.** Both stores count the distinct query terms a
   passage holds and rank by that count before anything else; within equal
   coverage the PostgreSQL store keeps `ts_rank_cd` as the tiebreaker. The
   passage score is the same formula in both stores: three quarters coverage,
   with the document's authority and its affinity with the current scope
   making up the rest.
2. **Terms match in their folded spellings.** Each query term matches any of
   the spellings that fold to its stem, the folding relevance recall already
   uses (ADR-0259); the query is expanded, never the stored text, so the
   existing `simple` index serves it. A term with digits or other letters
   keeps its one spelling, and a term with punctuation inside it matches the
   way `plainto_tsquery` reads it: every word of it present.
3. **A PDF's outline becomes Markdown headings.** The extractor writes each
   bookmark as a heading on the page it opens, nested by outline depth, so the
   chunker gives a manual's sections real heading paths. A PDF without an
   outline, or with one that cannot be read, keeps plain page text.

Automatic pre-turn knowledge retrieval stays where the knowledge design left
it, a later step behind an evaluation gate; this decision changes only what
`knowledge.search` returns.

## Consequences

- A question about a parked car's valve noise ranks a passage that mentions
  parking and valves above one that repeats "parking".
- The contract suite now exercises the production ranking rules through the
  in-memory store.
- Documents ingested before this decision keep their chunks; a PDF gains
  heading paths when it is ingested again.

## Verification

- `tests/contract/test_knowledge_passage_search_contract.py`: "valves closing
  after I parked" finds a passage about a valve; a passage holding two of the
  question's terms outranks one repeating a single term.
- `tests/contract/test_extractor_contract.py`: a PDF with bookmarks extracts
  as nested Markdown headings and chunks with the matching heading path.
