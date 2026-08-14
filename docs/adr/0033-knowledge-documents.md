# ADR-0033: The knowledge document, its corpus, and passage retrieval

- Status: Accepted
- Date: 2026-07-28
- Related: Sections 6 (core domain objects), 9 (policy), 11.2 (trust
  labels), 11.3 (the context budget), 18.4 (artifact storage), 20
  (evaluation), 21 (Milestone 9), 22 (security baseline), 29
  (multi-device and the shared core), ADR-0014 (memory surface and
  external providers), ADR-0017 (layered approval), ADR-0018 (memory
  formation), ADR-0019 (memory retrieval), ADR-0020 (the context
  engine), ADR-0022 (the gate registry), ADR-0027 (the milestone
  map), ADR-0029 (sandbox isolation and artifacts), ADR-0030 (skills
  and the authoring loop), ADR-0032 (trajectory export, redaction,
  and consent)
- Detailed design: `docs/plan/knowledge-documents.md`

## Context

Milestone 9 is titled "long-term memory and knowledge retrieval" and
is the last milestone the readiness review scored anything other than
Ready. Its named gap was blunt: knowledge documents have no design.
The milestone's own separate-stores subsection lists knowledge
documents as a distinct store and instructs that the five stores not
be collapsed into vector records. Two specifications, fourteen gates,
and two ADRs cover the memory half. Nothing covered the other half.

The gap is not cosmetic. `TrustLevel.KNOWLEDGE` has existed in
Section 11.2 since the plan was written, with nothing in the corpus
able to produce a value carrying it. Section 29 lists "long-term
memory and knowledge - user facts and retrieved documents" among the
components that must be shared across devices. Section 21's Milestone
9 acceptance criteria are stated for memory alone. A reader building
Milestone 9 from the corpus would have shipped half its title and had
no way to know which half was missing.

Two constraints shaped the answer more than anything else. The first
is that `memory-retrieval-and-ranking.md` opens with "Scope:
retrieval and ranking **of beliefs and episodes**" and
`memory-formation-and-consolidation.md` with "Scope: **formation**
only" — so neither could host documents without contradicting the
sentence it uses to define itself. The second is that
`questions-for-review.md` records a decision *against* a fourteenth
specification, for the trajectory export, on the grounds that the
export borrowed the log, the projections, the schema, and the gate
area from a document that already owned all four.

## Decision

1.  **A fourteenth specification and a fourteenth gate area,
    `knowledge`.** The trajectory-export precedent applies a test —
    does the new document own what it needs, or borrow it — and
    knowledge passes it where the export failed. It owns the document
    model, ingestion, chunking, the index, the scope model, retrieval
    over chunks, rendering and citation, retention, its tool surface,
    and its gates; it borrows the artifact store, the event log, the
    context budget, and the trace. Owning ten and borrowing four is
    the inverse of the case that was rejected. ADR-0030's thirteenth
    area for skills is the governing precedent: an area names a
    subject that one specification owns.
2.  **It is a Milestone 9 deliverable, sequenced after memory**, not
    a milestone of its own. It is half the milestone's own title, and
    the separate-stores subsection already sits in that milestone's
    section. This answers the readiness review's open question 5,
    which asked whether knowledge retrieval should be split out.
3.  **A knowledge document is text a principal admitted so that later
    runs can retrieve passages verbatim and cite them.** It is not a
    belief: it carries provenance rather than confidence, it is never
    formed, it does not reinforce or decay, supersession is by version
    rather than by claim, and deletion is real and cascades.
4.  **Source bytes live in the artifact store** under a new
    `ArtifactOrigin.KNOWLEDGE_SOURCE`, with `expires_at = None` at
    ingest so the document owns the lifetime. Deletion writes
    `expires_at = now` and the existing sweeper collects it — the
    move ADR-0032 made for consent withdrawal, so the rarest
    governance operation runs on the most exercised code path.
5.  **The source is retained after extraction**, not discarded.
    Re-chunking must not require re-upload, a citation that cannot
    resolve to its source is not a citation, and extraction is the
    step most likely to be wrong.
6.  **Ingestion is one tool, `knowledge.ingest`.** Not a route: the
    Milestone 5 API baseline was closed at fourteen and states that an artifact
    is not uploaded through it in 0.1. ADR-0050's later session routes do not
    add an upload surface. Not a thirteenth CLI noun. Subject
    specifications declaring their own tools is the established
    pattern — `memory.search`, `skill.load`, and `skill_manage` are
    all outside the builtin roster, whose count is unchanged.
7.  **Admission requires `USER` origin trust.** A belief written from
    injected content is wrong until corrected; a document admitted
    from injected content is a stored, retrievable, indefinitely
    replayed injection vector. An agent may not admit what it
    fetched.
8.  **The secret scan blocks; the injection scan does not.** A
    credential in a retrievable corpus is a durable leak no envelope
    mitigates. Instruction-like text is recorded as
    `contains_instruction_like_text`, surfaced in the envelope and
    the trace, and retrieved — because a poisoned passage is the
    agent quoting a liar rather than asserting in its own voice, and
    because blocking would exclude style guides, API references, and
    anything at all about prompting.
9.  **No overlap; heading paths instead.** Sliding-window overlap
    duplicates roughly a sixth of the index to solve a lost-context
    problem that eight tokens of rendered heading path solve better,
    and duplication corrupts term statistics and defeats the
    per-document cap.
10. **Chunk boundaries are deterministic under a `chunker_version`.**
    The citation *is* the chunk id, so a drifting chunker silently
    invalidates every citation ever emitted. Re-chunking produces a
    new chunk-set version beside the old one, never an in-place edit.
11. **`visibility` replaces `principal_id` as the isolation
    predicate.** Documents carry `visibility ∈ {principal, project,
    tenant}`; `principal_id` records who ingested one, for provenance
    and deletion authority. This inverts memory's rule deliberately —
    a belief is about the principal, who is the same person in every
    project, while a document is about a subject and its audience is
    a decision someone made. It has its own gate because a reader
    carrying memory's rule over would build either a corpus nobody
    can share or one that leaks.
12. **Knowledge gets its own Region B budget class**, 3 passages and
    3,000 tokens, which yields first — ahead of in-turn recall. A
    passage is up to 1,000 tokens against a belief's twenty-five, so
    sharing in-turn recall's 2,000 would let one passage evict every
    belief in the turn and the failure would present as memory being
    broken.
13. **Passages drop whole and are never truncated.** A truncated
    passage is a misquotation with a citation attached.
14. **No supersession collapse, no per-subject cap, and no conflict
    surfacing.** Two beliefs that disagree are a defect; two sources
    that disagree are a library, and adjudicating that at read time
    would be the retriever deciding which author is right. A
    per-document cap of two passages does the useful half.
15. **Ranking is four terms** — match, authority, scope affinity, and
    recency, less a penalty. No confidence, no reinforcement, no
    utility: the agent is quoting, not believing. `recency` reads the
    document's own date, because a newer edition supersedes an older
    one and nothing else in the store knows that.
16. **One trace record, extended, not a second one.** `RecallTrace`
    gains a `passages` list and `RecallTraceView` a `TracedPassage`.
    A trace showing only beliefs when passages were also rendered
    would be exactly the overclaim ADR-0019 forbids. The two-ceiling
    filter and two-tier retention apply unchanged.
17. **One new scope, `knowledge.write`**, taking the closed
    vocabulary from fourteen strings to fifteen. `knowledge.search`
    carries none, mirroring `skill.write` shipping without a
    `skill.read`: an uncheckable scope is worse than a missing one.
18. **The management surface is deferred; the deletion semantics are
    not.** No route and no CLI command in 0.1, mirroring ADR-0030's
    decision on skills. `KnowledgeStore.delete()` and its cascade are
    specified now, so the surface that eventually calls it inherits a
    design rather than inventing one at the call site.
19. **Twelve gates, all at Milestone 9** — eight case, three
    property, one corpus, none structural.

## Consequences

- Milestone 9 becomes implementable in full, and the readiness table
  has no named gap left that blocks code. The store has a shape, the
  ingest path has a trust rule, chunking has parameters, retrieval
  has a budget, and the milestone has gates for the half of its title
  that had none.
- The gate registry gains a fourteenth area and twelve entries,
  going from one hundred and sixty to one hundred and seventy-two.
  Milestone 9's census row goes from fourteen to twenty-six. The
  declared count across specifications goes from one hundred and
  fifty-four to one hundred and sixty-six, and the declaration count
  from one hundred and sixty-three to one hundred and seventy-five.
  Kinds become ninety-five case, twenty-two property, nine corpus,
  and forty-six structural.
- Nine documents said "thirteen specs". Three of those sentences are
  live and are corrected — two in `milestone-map.md` and one in
  `engineering-plan.md`, plus that plan's "fourteen specs and this
  plan" — and six are changelog and questions-for-review entries,
  which are records of what was true when written and are not
  rewritten. The ripple ADR-0032 feared, measured, is four lines.
- `sandbox-isolation.md` gains one enum member and its "five sources"
  becomes six. `tool-system.md` gains one row in the domain partition
  table. `policy-and-approvals.md`'s scope vocabulary goes to fifteen
  strings, which is three sentences in one section.
  `context-engine.md` gains one budget row and one step in the yield
  order. `memory-retrieval-and-ranking.md`'s scope line gains a
  pointer to this document. None of these is a redesign.
- `readiness.md`'s open question 5 is answered: a Milestone 9
  deliverable, sequenced after memory, not its own milestone.
- `gate.knowledge.no_belief_from_document` — that no belief is ever
  formed from a document — is the one that constrains future work
  hardest, and it is
  deliberate. It forecloses the cheapest wrong implementation, in
  which a consolidation pass mines the corpus for claims and asserts
  them in the agent's own voice with no source attached.

## Alternatives considered

- **Folding knowledge into `memory-retrieval-and-ranking.md`**:
  rejected. Its scope line says "of beliefs and episodes", and a
  document whose scope sentence is false about itself stops being
  usable as a contract. The same objection kills the formation spec,
  whose scope line says "formation only".
- **A `memory-and-knowledge.md` merger of both memory specs plus
  this one**: rejected. It would be the largest document in the
  corpus, it would merge a write path, a read path, and a third store
  with different trust semantics, and it would discard two ADRs'
  worth of settled structure to avoid adding one file.
- **Treating knowledge documents as artifacts with a search index**:
  rejected. Artifacts expire in thirty days, are addressed whole, and
  carry no visibility model. Every property that makes a corpus
  useful would have had to be bolted onto the artifact record, which
  is the "do not treat them all as vector records" instruction in a
  different costume.
- **Blocking ingestion on instruction-like text**: rejected. It
  excludes the documents most worth having — style guides, API
  references, runbooks — and it treats a quoted instruction as
  equivalent to an asserted one, which the trust envelope exists
  precisely to distinguish.
- **Allowing an agent to admit a page it fetched**: rejected. It
  turns any successful prompt injection into a permanent corpus
  entry, which is the one failure in this design with no bound on its
  blast radius.
- **Sliding-window chunk overlap**: rejected. It is the conventional
  answer, and it duplicates a sixth of the index, corrupts term
  statistics, returns the same sentence under two ids, and makes the
  per-document cap meaningless — to solve a context problem that
  rendering the heading path solves for eight tokens.
- **Sharing in-turn recall's budget**: rejected. Two stores whose
  units differ by forty times cannot share an allowance without one
  routinely erasing the other, and the resulting bug reports would be
  about memory rather than about the budget.
- **Truncating a passage to fit the budget**: rejected. The whole
  value of a passage is that it is quotable, and a truncated quote
  with a citation on it is worse than an absent one.
- **Surfacing conflicts between documents**: rejected. Retrieval
  would be deciding which source is right, with no evidence beyond
  its own ranking, and presenting the result as the corpus's answer.
- **Letting citations raise a document's authority**: rejected, for
  the reason ADR-0019 refuses to let usage raise confidence. Evidence
  must come from the world, not from the retriever approving of its
  own output.
- **A `knowledge.read` scope**: rejected, exactly as `skill.read`
  was. Nothing in 0.1 would check it, and a scope nobody checks gets
  granted by default and audited as though it meant something.
- **A `POST /v1/knowledge` route**: rejected for 0.1. The route table
  was closed at fourteen for the Milestone 5 baseline and the API explicitly
  does not accept artifact uploads in this version. ADR-0050's later session
  routes do not change that, so a knowledge route would need an upload
  path built first, for a milestone that does not need it.
- **Deleting documents with a bespoke deletion routine rather than
  the artifact sweeper**: rejected. A second deletion path is written
  once, exercised almost never, and trusted completely — which is the
  combination that produces data that was reported as deleted and was
  not.
- **Keeping only extracted text and discarding the source**:
  rejected. It makes every extraction bug permanent, makes
  re-chunking impossible without re-upload, and makes "show me where
  this came from" unanswerable.
- **Splitting the twelve gates across `memory`, `context`, and
  `tool`**: rejected, as ADR-0030 rejected the same split for skills.
  `gate.knowledge.visibility_isolation` and
  `gate.knowledge.no_belief_from_document` are two halves of one
  governance story and would have landed in different areas.
