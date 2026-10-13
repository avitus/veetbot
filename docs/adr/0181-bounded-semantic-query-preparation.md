# ADR-0181: Bounded semantic query preparation

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-11
- Related: ADR-0178, ADR-0179, ADR-0180
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Add explicit slow-path preparation of query vectors behind the shared local
`SemanticQueryLookup` port. A lookup performs no provider request, schedules no
background task and creates no spend reservation. A miss preserves sparse recall.
Production supplies no preparer or lookup until configuration admission,
scheduling and the complete M33 release evidence are implemented. This mechanism
provides neither an activation bypass nor a task-aware query planner.

The caller supplies current policy and retrieval-policy versions, surface identity,
a task digest and an explicit sensitivity floor. Cache identity also covers the
complete recall query (including tenant, principal, scope and read ceiling), the
embedding tuple, pricing, egress/residency configuration and mechanism version
`semantic-query@1`. Changing context or withdrawing admission clears the observed
cache; in-flight results from the old context cannot populate it. A cache contains
at most 128 vectors with a five-minute lifetime and least-recently-used eviction.
It retains hashes and vectors, never question text or source evidence. Retrieval
still resolves current permitted originals on every call, including after source
correction or erasure. Cache identity is not evidence that a source is visible.

One explicit preparation accepts at most four queries and makes at most one
provider request. Duplicate exact queries and current cache hits are skipped.
Queries for another owner, core recall, historical recall, People-scoped recall,
legacy candidate selection or empty text are ineligible. The caller's sensitivity
floor cannot be lowered by the query's read ceiling. Complete question text and
explicit subjects pass the existing local classification, credential/injection
and residency guards before serialization. Only public/internal text is eligible;
no inferred labels or expected answers enter the provider request. Inputs over
4,096 UTF-8 bytes are withheld without truncation. The canonical query text is a
JSON array of question text followed by explicit subjects.

The complete preparation has a ten-second deadline, with a fifteen-second durable
query reservation and up to three seconds of conservative failure cleanup. The
provider call runs outside all repository transactions. One-second checks observe
admission, context and lease validity; cancellation stops the request. There are
no automatic retries or unbounded queues. These are versioned mechanism safety
bounds, not measured operational-budget or latency claims.

Query and indexing requests share the existing owner/day embedding ledger and
request/day ceilings. Under the same owner lock, at most one unsettled query
reservation is admitted, independently of the indexing job and its cursors.
The reservation commits before egress; unknown usage retains its complete charge.
An expired reservation becomes unknown before replacement, including after a
process restart. Settlement rounds up to the existing database quantum and keeps
the admission UTC day even across midnight. Configuration changes cannot reset
spending. Only validated attributable usage lowers the charge, and vectors enter
the cache only after settlement commits and the context is checked again.

Query receipts add `kind` and `deadline` fields to the existing forced-RLS spend
table. Index receipts preserve their previous serialized shape for application
rollback. A partial unique owner index bounds pending query admission.
No source text, query text or vectors enter this table. Downgrade takes owner and
spend table locks, fails closed if RLS hides rows, and refuses pending query work.
Settled charges remain after downgrade. Indexing and M32 dreaming behavior remain
unchanged; no independent query budget can double the embedding allowance.

## Verification and limits

Regression tests first fail on missing durable query admission and then on missing
query population. Shared memory/PostgreSQL contracts exercise owner serialization,
restart/expiry, settlement, rollback, both directions of the shared budget, and
admission-before-provider execution. Query tests cover exact scope/context identity,
cache expiry/eviction, egress denials, uncertain responses, cancellation and
withdrawal. Automatic and deliberate recall use prepared vectors for a synthetic
non-ownership constraint and reject corrected source evidence on a later recall.
These mock-provider tests establish mechanism behavior, not embedding quality.

Production composition, current task/policy binding, scheduling, historical/People
parity, task-aware query planning and recovery, independently authored release
cases, answer-use measurements and complete activation evidence remain open.
All twelve M33 release gates remain pending. This does not yet fix recall on the
running deployment.
