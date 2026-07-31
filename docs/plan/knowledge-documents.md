---
title: Knowledge Documents
status: design
canonical: true
---

# Knowledge documents

This document specifies the other half of Milestone 9's title. The milestone is
"long-term memory and knowledge retrieval"; two specifications cover memory's write
path and read path, and until now nothing said what a knowledge document is, how one
enters the system, how it is chunked, indexed, or scoped, or how retrieval over it
differs from retrieval over beliefs. It sits under Milestone 9 of the
[engineering plan](engineering-plan.md) and is recorded as
[ADR-0033](../adr/0033-knowledge-documents.md).

Scope: **documents a principal admitted so that later runs can retrieve passages from
them verbatim and cite them.** Beliefs are specified in
[memory formation and consolidation](memory-formation-and-consolidation.md) and
[memory retrieval and ranking](memory-retrieval-and-ranking.md); this document borrows
their pipeline shape and states every place it diverges. The bytes live in the artifact
store of [sandbox isolation and artifacts](sandbox-isolation.md); the budget class is
allocated by the [context engine](context-engine.md).

## Why knowledge is not memory

The plan's Milestone 9 section already separates the stores. It does not say what the
second one holds. Three stores now exist and they answer three different questions:

| | Belief | Knowledge document | Artifact |
| --- | --- | --- | --- |
| Answers | what is true of the principal | what a source says | what a run produced |
| Origin | derived from episodes by consolidation | admitted whole by a principal | written by a tool or a model |
| Unit of retrieval | one statement | one passage | the whole object |
| Authority | the agent's own, hedged by confidence | the source's, attributed | none — it is output |
| How it changes | supersession by claim | a new version of the document | never; a new artifact |
| Lifetime | until retired or superseded | until deleted | thirty days by default |
| Trust label | `MEMORY` | `KNOWLEDGE` | per its origin |

Both memory labels already exist in the plan's `TrustLevel` enum (Section 11.2), which
is the strongest available evidence that the two stores were always meant to be
distinct: the label for knowledge has been reserved since before anything could produce
it.

Five consequences follow, and each one closes a question a reader would otherwise ask.

1. **Knowledge is never *formed*.** Formation writes beliefs from episodes; ingestion
   writes documents from bytes a principal handed over. Neither writes the other. A
   document does not become a belief because it was read, and a belief is not promoted
   into the corpus because it was reinforced. This has a gate, because the cheapest
   wrong implementation is a consolidation pass that mines documents for claims and
   asserts them in the agent's own voice.
2. **A document has provenance, not confidence.** The agent does not believe a
   passage. It quotes one, attributed. Confidence bands, corroboration counts, and the
   affirmed/inferred authority ladder are all properties of a claim the agent is
   making, and the agent is not making this one.
3. **There is no reinforcement, no decay, and no utility.** A document does not become
   truer for having been cited, and does not fade for having been ignored. The whole
   feedback machinery of belief ranking is deliberately absent, and its absence is the
   reason the ranking function below is four terms rather than seven.
4. **Supersession is by version, not by claim.** Two beliefs that disagree are a
   conflict formation must resolve or surface. Two documents that disagree are a
   library. Only a new version of the *same document* supersedes anything.
5. **Deletion is real and it cascades.** A belief is retired and kept for audit,
   because the record of having believed something is itself worth keeping. A document
   is deleted — chunks, index rows, and source bytes — because a principal who
   withdraws a document is usually withdrawing it for a reason that a tombstone does
   not satisfy.

Knowledge is also not a skill. A skill is procedural — instructions the agent follows,
loaded deliberately, trusted by author, specified in [skills.md](skills.md). Knowledge
is declarative — passages the agent quotes, retrieved by relevance, trusted by nobody.
The distinction is the one the tool system already draws between a tool's behaviour and
a tool's output, and it is why a skill body is never retrieved by a ranker and a
passage is never followed as an instruction.

## Where the bytes live

There is no third object store. A source document is put into the **artifact store**
under a new origin:

```python
class ArtifactOrigin(StrEnum):
    SANDBOX_EXPORT = "SANDBOX_EXPORT"
    TOOL_OUTPUT = "TOOL_OUTPUT"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    UPLOAD = "UPLOAD"
    TRAJECTORY_EXPORT = "TRAJECTORY_EXPORT"
    KNOWLEDGE_SOURCE = "KNOWLEDGE_SOURCE"   # new
```

Ingestion writes `expires_at = None`, so the thirty-day sweeper does not collect it and
the document owns the lifetime. Deletion writes `expires_at = now`, and the sweeper that
already exists collects the bytes on its next pass. This is exactly the move ADR-0032
made for consent withdrawal, for exactly the same reason: the rarest governance
operation runs on the most exercised code path, rather than on a second deletion routine
that is written once and never exercised again.

The source is **retained** after extraction rather than discarded, for three reasons:

- re-chunking under a new chunker version must not require the principal to re-upload
  something they gave the system a year ago;
- a citation that cannot resolve to the source it came from is not a citation, and
  "show me where this passage is from" is the first thing anyone asks;
- extraction is the step most likely to be wrong, and a text-only store makes an
  extraction bug permanent and undiagnosable.

Skill packages are not artifacts, by ADR-0030's decision 20, because they must outlive
every pin. Knowledge sources are artifacts, because expiry is precisely the mechanism
deletion wants — the two decisions differ because the lifetime requirements are
opposite, not because the reasoning changed.

## Ingestion

One tool, `knowledge.ingest`, taking an `ArtifactRef` and a visibility. Not a route:
the API's route table is fourteen routes, closed for 0.1, and that document already
states that an artifact is not uploaded through the API in 0.1. Not a thirteenth CLI
noun either. Subject specifications declare their own tools — `memory.search`,
`skill.load`, and `skill_manage` are all outside the builtin roster's eight — so this
costs `builtin-tools.md` nothing, and the roster's count is unchanged.

Ingestion is seven ordered steps in one transaction. Each refuses rather than warns,
except where noted.

### 1. Admit

The calling turn's origin trust must be `USER` or above. This is the direct analogue of
formation's ban on untrusted writes, and it is stricter than it looks: a belief written
from injected content is wrong until it is corrected, while a **document** admitted from
injected content is a stored, retrievable, indefinitely replayed injection vector that
every future session can draw from. The blast radius is larger, so the gate is the same
and the reasoning is not.

Web pages an agent fetched during a run are therefore not admissible by the agent. A
principal may admit one, which makes the admission a human act with a human accountable
for it, which is the entire point.

### 2. Extract

`text/plain` and `text/markdown` in 0.1. An `Extractor` port exists from the first
commit so that PDF, HTML, and DOCX arrive as adapters rather than as a rewrite, and an
unsupported media type fails **at ingest**, loudly, rather than producing an empty
document that retrieves nothing and explains nothing.

### 3. Normalize

A stated, closed list of transformations, never a paraphrase and never a model call:
Unicode NFC, CRLF to LF, tabs to spaces at the file's own tab width, trailing whitespace
stripped, and runs of more than two blank lines collapsed to two. Nothing else. The
verbatim guarantee below is only worth stating if the normalization it survives is
written down, and "we clean up the text a bit" is not a specification.

### 4. Scan

Two scans, and they behave differently on purpose.

The **secret scan blocks.** A credential inside a retrievable corpus is a durable leak
that no trust envelope mitigates, because the leak is the string itself. Ingestion
fails, names the chunk, and stores nothing.

The **injection scan does not block.** It records `contains_instruction_like_text` on
the chunk and surfaces it in the trace and in the rendered envelope. Two reasons. First,
the threat model differs from memory's: a poisoned belief is the agent asserting a
falsehood *in its own voice*, while a poisoned passage is the agent quoting a liar,
which is what `TrustLevel.KNOWLEDGE` and the rendered envelope already communicate to
the model. Second, blocking would make the corpus useless for the documents most worth
having in it — a style guide, an API reference, or anything at all about prompting is
instruction-shaped by nature.

### 5. Chunk

Specified below. Deterministic under a `chunker_version`.

### 6. Index

Postgres full-text search over the chunk text plus its heading path.

### 7. Commit

The document row, the chunk rows, the index rows, and a `knowledge.document.ingested`
event, in **one** transaction. A half-ingested document that retrieves three of its
eleven sections is worse than a failed ingest, because nothing about the result
announces that it is partial.

## Chunking

Structure first, size second. Headings are the primary boundary, paragraphs the
secondary one, and a hard split happens only when a single paragraph exceeds the
ceiling.

| Parameter | Value | Why |
| --- | --- | --- |
| Target | 600 tokens | a section a reader would quote whole |
| Ceiling | 1,000 tokens | the budget class holds three of these |
| Floor | 100 tokens | below this, merge up into the parent section |
| Overlap | none | see below |

Every chunk carries its **heading path** (`Guide > Deployment > Rollback`) and its
ordinal within the document. The heading path is rendered with the passage, and this is
what replaces overlap. Sliding-window overlap is the conventional answer to the problem
that a chunk read alone has lost its context; it solves that problem by duplicating
15 percent of the corpus into the index, which corrupts term statistics, returns the
same sentence twice under two ids, and makes the per-document cap below meaningless.
Rendering the heading path restores the context that was actually missing, at a cost of
about eight tokens instead of a sixth of the index.

Boundaries are **deterministic under a `chunker_version`**: the same bytes and the same
version produce the same chunk ids, always. This is not a tidiness preference. The
citation *is* the chunk id, so a chunker that drifts between runs silently invalidates
every citation ever emitted. Re-chunking under a new version produces a new chunk-set
version alongside the old one; it is never an in-place edit, and old citations keep
resolving until the old set is deleted.

## Indexing

Postgres FTS first, matching Milestone 9's explicit instruction and the memory read
path's decision for the same reason. `pgvector` is built behind the **same RRF fusion
interface** memory already uses, so a semantic arm is a configuration flip, and it is
enabled only when the harness shows lift over lexical retrieval on a corpus gate rather
than because it is expected to.

Paraphrase-heavy queries are the case where semantic retrieval genuinely wins, and they
are more common against prose documents than against beliefs. The expectation is
therefore that this arm turns on earlier here than it does for memory. It still has to
earn it.

## Scope and visibility

This is the real divergence from memory, and it is the one a reader is most likely to
get wrong by carrying over the memory spec's rule.

Every document carries `visibility ∈ {principal, project, tenant}`. Reads are governed
by three SQL predicates: `tenant_id`, `visibility` resolved against the current scope,
and the surface's sensitivity ceiling.

`principal_id` on a document records **who ingested it**, for provenance and for
deletion authority. It is **not** the isolation predicate. The memory retrieval spec
says, correctly for beliefs, that `principal_id` is a hard filter; that sentence does
not carry over, and a reader who assumes it does will build either a corpus nobody can
share or one that leaks. The difference has its own gate for that reason.

The asymmetry is deliberate and it runs opposite to memory's:

- **A belief is about the principal**, who is the same person in every project, so
  beliefs carry across projects by default and project scope is a ranking feature.
- **A document is about a subject**, and who may read it is a decision the ingesting
  principal made explicitly. It never carries by default and never crosses a tenant at
  all.

`scope_affinity` still exists as a ranking feature — a document ingested into this
project ranks above an equally matching tenant-wide one — but it ranks; it does not
admit.

## Retrieval

The pipeline skeleton is memory's, and this document does not restate it. What follows
is every place it differs, because the differences are the design.

1. **There is no snapshot.** Knowledge is Region B only, always, and it is never in the
   cached prefix. A snapshot would have to be selected before the task is known, and a
   thousand-token passage chosen against an unknown task is a thousand tokens of noise
   frozen into the prefix for the whole session. Consequently there is no watermark, no
   recall delta, and no correction lines here — three mechanisms the memory spec needs
   that this one does not.
2. **There is no structured arm.** A belief has a subject and a type to look up
   directly. A passage has neither. Lexical is always on, semantic arrives on evidence,
   and graph expansion is not applicable. The **query former is shared** with memory —
   the same objective, the same active entities, the same aliasing — because the task
   is the same task. The arms are not shared.
3. **Knowledge has its own budget class**, and this is the most consequential decision
   in the document. A passage is up to 1,000 tokens against a belief's twenty-five. If
   knowledge drew from in-turn recall's 2,000-token allowance, a single retrieved
   passage would evict every belief in the turn, and the failure would look like memory
   being broken rather than like a budget being shared by two things of wildly
   different granularity. The context engine therefore gains one Region B row:
   **3 passages / 3,000 tokens**, which **yields first** under pressure — ahead of
   in-turn recall — because a turn that has lost its user model is more damaged than
   one that has lost a quotation.
4. **Passages drop whole. They are never truncated.** A truncated passage is a
   misquotation with a citation attached to it, which is worse than no passage at all.
   When the budget cannot fit the third passage, two are rendered.
5. **The tool is the path.** `knowledge.search` is how a run reaches the corpus in the
   first build step. Automatic pre-turn retrieval — the analogue of in-turn recall —
   is a later step, gated by evals, because retrieving into every turn against a corpus
   that may be irrelevant to the whole session is the expensive default.
6. **Passages are quoted, never paraphrased**, and they are always cited. The rendered
   block carries the document title, the heading path, the chunk id, and the document's
   date.
7. **No supersession collapse, no per-subject cap, and no conflict surfacing.** All
   three exist in belief retrieval because two beliefs that disagree are a defect. Two
   sources that disagree are a library, and resolving that at read time would be the
   retriever deciding which author is right. They are replaced by a **per-document cap
   of two passages**, which does the useful half of diversification — stopping one
   verbose document from filling the budget — without adjudicating anything.
8. **Feedback is one-directional.** Citations are recorded for ranking evaluation and
   for the trace. They do not raise a document's authority, for the reason belief usage
   does not raise confidence: evidence must come from the world, not from the retriever
   liking its own output.

The rendered block is trust-labelled like every other untrusted region:

```text
<knowledge as_of="2026-07-28T00:00:00Z" policy="knowledge-v1">
  <passage doc="Deployment Guide" path="Deployment > Rollback"
           chunk="kc_7f3a91" doc_date="2026-05-02"
           instruction_like="false">
    Roll back by reverting the release tag and re-running the deploy
    job. Do not edit the running configuration directly.
  </passage>
</knowledge>
```

`instruction_like="true"` is what the injection scan produces instead of blocking. The
model sees the flag, the operator sees it in the trace, and the passage is still
available — which is the correct outcome for a document that legitimately contains
instructions and the safest available outcome for one that does not.

### Ranking

Four terms, against belief ranking's seven:

```text
score(c, q) =
      w_match     · match(c, q)          # fused arm score, 0..1
    + w_authority · authority(d)         # how the document was obtained
    + w_scope     · scope_affinity(d, q) # project match, not a filter
    + w_recency   · recency(d)           # the document's own date
    - w_penalty   · penalty(c)           # boilerplate, tiny chunk, dupe
```

`authority(d)` is a three-step ladder — `principal_authored` above
`principal_supplied` above `fetched` — and it is a property of how the document was
obtained, never of what it says. `recency(d)` uses the **document's** date rather than
the ingestion date, because a newer edition supersedes an older one and nothing else in
the store knows that. Weights are hand-set, versioned, and eval-tuned, exactly as
memory's are.

### The trace

The existing `RecallTrace` is extended with a `passages` list rather than duplicated,
and `RecallTraceView` gains a `TracedPassage`. One record, two consumers, as the
retrieval spec established. A trace that showed only beliefs when passages were also
rendered would be exactly the overclaim that spec forbids — a user asking what the
agent knew would be shown two thirds of the answer with no indication that a third was
missing.

The two-ceiling filter — the stricter of the recall surface's sensitivity ceiling and
the viewing surface's — and the two-tier retention schedule apply unchanged. A passage
in a trace is subject to them for the same reason a belief is.

## Versions, supersession, and deletion

A new version of a document is a new row with the same `document_id` and an incremented
`version`. The previous version stays queryable by `as_of` and is excluded from live
retrieval, which is the bi-temporal treatment beliefs already get. Citations into the
old version keep resolving, and this is the property that makes the whole citation
story credible rather than aspirational.

Deletion removes the document row, its chunk rows, its index rows, and — by setting
`expires_at = now` on the source artifact — its bytes. Traces that cited a deleted
passage keep the chunk id and lose the text, and the view renders "this passage has
been deleted" rather than a dangling id or, worse, silence.

The **management surface is deferred**, mirroring the same decision skills recorded and
with the same discomfort. There is no route and no CLI command in 0.1: the route table
is closed at fourteen and the CLI at twelve, and nothing in Milestone 9 needs either.
What is *not* deferred is the semantics — `KnowledgeStore.delete()` is specified now,
with its cascade, so that whatever surface eventually calls it is calling something that
was designed rather than something invented at the call site. This is the same
shape-now, mechanism-later split the API document uses for its limits.

## Ports and data model

```python
class KnowledgeDocument(BaseModel):
    document_id: UUID
    tenant_id: str
    ingested_by_principal_id: str      # provenance, NOT the filter
    visibility: str                    # principal | project | tenant
    project_scope: str | None          # required when visibility=project
    title: str
    source_ref: ArtifactRef            # KNOWLEDGE_SOURCE, expires_at None
    media_type: str
    doc_date: date | None              # the document's date, for recency
    authority: str                     # principal_authored | ... | fetched
    version: int
    chunker_version: str
    superseded_by: UUID | None
    ingested_at: datetime
    sensitivity: str

class KnowledgeChunk(BaseModel):
    chunk_id: str                      # the citation; stable per version
    document_id: UUID
    version: int
    ordinal: int
    heading_path: list[str]
    text: str                          # verbatim after normalization
    tokens: int
    contains_instruction_like_text: bool
    content_sha256: str                # binds the citation to the bytes

class KnowledgeQuery(BaseModel):
    tenant_id: str                     # isolation boundary, hard filter
    principal_id: str                  # for visibility resolution only
    current_scope: str | None          # ranking, and project visibility
    text: str
    as_of: datetime | None = None
    budget_tokens: int
    max_passages: int
    max_per_document: int = 2
    min_score: float

class RetrievedPassage(BaseModel):
    chunk_id: str
    document_id: UUID
    title: str
    heading_path: list[str]
    text: str                          # whole; never truncated
    doc_date: date | None
    authority: str
    score: float
    arms: list[str]
    instruction_like: bool
```

Ports, all replaceable strategies:

```python
class Extractor(Protocol):
    def media_types(self) -> set[str]: ...
    async def extract(
        self, source: AsyncIterator[bytes], media_type: str
    ) -> str: ...

class Chunker(Protocol):
    version: str                       # chunk ids are stable per version
    def chunk(
        self, text: str, title: str
    ) -> list[KnowledgeChunk]: ...

class KnowledgeStore(Protocol):
    async def ingest(
        self,
        source: ArtifactRef,
        title: str,
        visibility: str,
        principal: Principal,
    ) -> KnowledgeDocument: ...
    async def search(
        self, query: KnowledgeQuery
    ) -> list[RetrievedPassage]: ...
    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None: ...
    async def delete(
        self, document_id: UUID, principal: Principal
    ) -> None: ...
```

## The agent-facing surface

One tool, returning `TrustLevel.KNOWLEDGE` data, plus the ingestion tool:

- **`knowledge.search`** — text query, optional `as_of`, returns ranked passages with
  their chunk ids and heading paths. `required_scopes` is empty, mirroring `skill.write`
  shipping without a `skill.read`: nothing in 0.1 could check a read scope, and an
  uncheckable scope is worse than a missing one.
- **`knowledge.ingest`** — takes an `ArtifactRef`, a title, and a visibility. Requires
  the new `knowledge.write` scope and `USER` origin trust.

`knowledge.write` is **one** new string in the closed scope vocabulary, taking it from
fourteen to fifteen. Both tools live in a new `knowledge` tool domain, registered at
build time alongside the other builtin domains.

## Failure modes and defenses

| Failure | Defense |
| --- | --- |
| Injected content admitted as a permanent corpus entry | ingestion requires `USER` origin trust; an agent cannot admit what it fetched |
| A credential lands in a retrievable corpus | the secret scan blocks the ingest and stores nothing |
| A poisoned passage is followed as an instruction | `TrustLevel.KNOWLEDGE`, the rendered envelope, and `instruction_like` on the chunk |
| Citations stop resolving after a chunker change | chunk ids are deterministic per `chunker_version`; re-chunking makes a new set beside the old |
| A passage is quoted with words the source does not contain | verbatim after a closed normalization list; `content_sha256` on every chunk |
| One document fills the retrieval budget | per-document cap of two passages, plus the relevance floor |
| Knowledge starves the belief budget | its own budget class, which yields before in-turn recall |
| A truncated passage becomes a misquotation | passages drop whole; never truncated to fit |
| Cross-tenant or cross-project leak | `tenant_id` and `visibility` are SQL predicates, never rank features |
| Documents silently mined into beliefs | formation reads episodes only; asserted by a gate |
| A deleted document survives in the object store | deletion sets `expires_at = now` and the existing sweeper collects it |
| The trace shows beliefs but not the passages also rendered | one trace record carries both; asserted by a gate |
| Extraction bugs become permanent and undiagnosable | the source artifact is retained, so re-extraction is always possible |

## Hard gates

Twelve gates in a new fourteenth area, `knowledge`, all at Milestone 9.

1. **Ingestion trust** — an ingest attempted from a turn below `USER` origin trust is
   refused, and nothing is written. **M9.**
2. **No secrets ingested** — a source containing a credential pattern fails the
   ingest, and no document, chunk, index row, or artifact survives the attempt. **M9.**
3. **Chunk determinism** — the same bytes under the same `chunker_version` produce
   identical chunk ids, across processes and across runs. A property gate. **M9.**
4. **Visibility isolation** — zero cross-tenant results, and zero results whose
   `visibility` does not admit the querying principal. A hard gate, not a metric to
   improve. **M9.**
5. **Passage verbatim** — every rendered passage matches its chunk's stored text
   exactly, and the chunk matches its `content_sha256`. A property gate. **M9.**
6. **Citation resolves** — every chunk id in a rendered block resolves to a live chunk
   whose text contains what was quoted. A property gate. **M9.**
7. **Budget yield** — under pressure the knowledge class yields before in-turn recall,
   and passages are dropped whole rather than truncated. **M9.**
8. **Version supersession** — after a new version is ingested, live retrieval returns
   the new passages and never the old, while an `as_of` query still returns the old
   ones. **M9.**
9. **Deletion cascades** — after a delete, no chunk, index row, or retrievable passage
   remains, the source artifact is swept, and a trace that cited it renders as deleted
   rather than dangling. **M9.**
10. **Trace completeness** — for sampled turns, a trace that rendered passages lists
   them, and the listed passages reproduce the rendered block the recorded hash covers.
   **M9.**
11. **No belief from a document** — a consolidation pass over a session in which
   passages were rendered writes zero beliefs whose provenance is a document. **M9.**
12. **Corpus retrieval** — a labelled corpus of questions and their answering passages,
   with a floor on passage recall@3 and a ceiling on noise ratio. A corpus gate. **M9.**

That is eight case gates, three property gates, and one corpus gate.

## Tracked metrics

- **Passage recall@k** — of the questions a corpus answers, how many retrieve an
  answering passage within budget.
- **Noise ratio** — retrieved-and-irrelevant over retrieved, reported alongside recall
  and never without it.
- **Citation rate and citation accuracy** — how often an answer drawn from a passage
  cites it, and how often the cited passage actually contains the claim. The second is
  the one that matters; the first is trivially gamed.
- **Ingestion outcomes** — refusals by cause: trust, media type, secret scan. A rising
  media-type refusal rate is the signal to build the next extractor.
- **Instruction-like rate** — the share of the corpus flagged by the injection scan.
  Tracked because a sudden rise is either a poisoning attempt or a scanner regression,
  and both are worth a look.
- **Budget pressure** — how often the knowledge class yields, which is the signal that
  three passages is the wrong number.

## Build sequence (incremental, each gated by evals)

1. **Ingest and store.** The tool, `text/plain` and `text/markdown` extraction, the
   normalizer, both scans, the chunker, the document and chunk tables, the artifact
   origin, and the ingest event. No retrieval yet. Chunk determinism and the two
   ingestion gates are provable at this step.
2. **Lexical retrieval.** The FTS index, the four-term ranker, the relevance floor, the
   per-document cap, `knowledge.search`, and the rendered envelope with citations.
3. **Budgeted assembly and the trace.** The context-engine class, the yield order, and
   the `passages` extension to the trace and its view. Passage verbatim, citation
   resolution, budget yield, and trace completeness are provable here.
4. **Versions and deletion.** New-version ingest, `as_of` retrieval, the cascade, and
   the deleted-passage rendering in the trace view.
5. **Automatic pre-turn retrieval.** Knowledge retrieved without an explicit tool call,
   behind a relevance floor, enabled only if the corpus gate improves with it.
6. **The semantic arm.** `pgvector` behind the existing RRF fusion, enabled on measured
   lift.
7. **Later extractors.** PDF, HTML, and DOCX as `Extractor` adapters, each re-running
   ingestion over already-stored sources rather than asking for re-upload.

## Decisions

- **Knowledge is a fourteenth specification and a fourteenth gate area.** It owns the
  document model, ingestion, chunking, the index, the scope model, retrieval over
  chunks, rendering and citation, retention, its tool surface, and its gates. It borrows
  four things — the artifact store, the event log, the context budget, and the trace —
  and borrowing four while owning ten is the test the trajectory export failed when it
  was folded into the persistence spec instead.
- **It is a Milestone 9 deliverable, sequenced after memory**, not a milestone of its
  own. It is half the milestone's own title, and the plan's separate-stores subsection
  already sits in the Milestone 9 section. This answers the readiness review's open
  question 5.
- **Neither memory specification could host it.** The retrieval spec's scope line reads
  "retrieval and ranking **of beliefs and episodes**", and the formation spec's reads
  "formation only". Putting documents in either would contradict a sentence that
  document uses to define itself.
- **Source bytes live in the artifact store under a new origin**, with `expires_at =
  None` at ingest and `expires_at = now` at delete, so the existing sweeper is the
  deletion mechanism.
- **Ingestion is a tool, not a route and not a CLI command.** The API is closed at
  fourteen routes and explicitly does not accept artifact uploads in 0.1; the CLI is
  closed at twelve commands. Subject specs declaring their own tools is the established
  pattern.
- **The secret scan blocks and the injection scan does not.** A credential is a durable
  leak the envelope cannot mitigate. Instruction-like text is what the envelope and the
  trust label exist for, and blocking it would exclude the documents most worth having.
- **No overlap; heading paths instead.** Overlap duplicates a sixth of the index to
  solve a context problem that eight tokens of heading path solve better.
- **Chunk ids are deterministic per `chunker_version`**, because the citation is the
  chunk id and a drifting chunker invalidates every citation ever emitted.
- **`visibility` replaces `principal_id` as the isolation predicate.** Documents never
  cross tenants and cross projects only when visibility says so — the inverse of
  memory's carry-by-default, because a belief is about the principal and a document is
  about a subject.
- **Knowledge gets its own budget class and yields first.** Sharing in-turn recall's
  2,000 tokens would let one 1,000-token passage evict every belief in the turn.
- **Passages drop whole.** A truncated passage is a misquotation with a citation
  attached.
- **No supersession collapse, no conflict surfacing, no per-subject cap.** Two sources
  that disagree are a library. A per-document cap of two does the useful half.
- **Ranking is four terms.** No confidence, no reinforcement, no utility — the agent is
  quoting, not believing, and `recency` reads the document's date rather than the
  ingestion date.
- **One new scope, `knowledge.write`**, taking the closed vocabulary to fifteen.
  `knowledge.search` carries none, mirroring `skill.write` without a `skill.read`.
- **The management surface is deferred; deletion semantics are not.** The method and its
  cascade are specified now so the surface that eventually calls it inherits a design.

## Open questions

1. **Where does the management surface land?** Listing, re-titling, re-scoping, and
   deleting documents all need a caller, and 0.1 has none. The likely answer is the
   same as skills': a CLI command at Milestone 10, and no route until a client needs
   one. Recorded with the same discomfort — an operator who has to read PostgreSQL to
   find out what is in the corpus will not audit it.
2. **Should automatic pre-turn retrieval be on by default once it exists?** Build step
   5 leaves it eval-gated. The cost of retrieving into every turn against an irrelevant
   corpus is real, and the relevance floor may or may not be enough to make the default
   safe.
3. **Does a document ever earn a belief?** The gate says formation never mines
   documents. A narrower future case — the principal states a preference *in* an
   admitted document — is arguably a belief with unusually good provenance. Not built,
   and it would need its own trust argument.
4. **Should `authority` distinguish an organization's own documents from a vendor's?**
   The three-step ladder is about how the system obtained the document, not about who
   wrote it. A tenant with both internal runbooks and vendor manuals may want the
   former to outrank the latter on a tie, which is a fourth step nobody has asked for
   yet.
