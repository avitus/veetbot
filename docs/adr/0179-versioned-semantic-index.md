# ADR-0179: Version-bound semantic memory index

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-11
- Related: ADR-0019, ADR-0031, ADR-0178
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

The synthetic comparison's 97% dense top-20 recall versus 68.5% sparse recall
justifies implementing pgvector under ADR-0178. It does not establish production
quality. Add a provider-neutral local index port and matching in-memory and
PostgreSQL adapters. Bind every vector to owner, immutable source creation
sequence, content revision, text digest and explicit provider/model/revision/
dimension/text-version tuple. Use the existing source clock and opaque change
journal independently of M32 job leases or activation.

Commit a vector only after re-reading the original under the source-owner lock.
Correction, restriction, attribution change and erasure delete dependent vectors
in the same transaction; the read also rejoins the current original and revision.
An in-flight computation cannot restore an invalidated version. Physical source
deletion cascades to derived rows. Indexing never changes memory authority,
evidence age, confidence, utility or source text. Usage-only changes preserve the
embedding and reads return the current usage metadata.

Start with exact cosine search and shared current-source hard predicates before
LIMIT. No approximate vector index is created. pgvector documents that approximate
indexes can apply filters after their initial scan, starving eligible results;
scaling decisions require the separate production-shaped experiment. Historical
and People-scoped dense retrieval are withheld pending their dedicated parity
work; those queries retain existing sparse recall.

Expose bounded insertion and change-journal pages. A fixed upper bound and
immutable creation order make scans resumable and fair to older originals;
deleted rows still advance the cursor. This supplies the repository seam, not
a scheduled backfill worker or authority to export source text.

An experimental local query-vector lookup feeds both automatic and deliberate
recall through the same retriever. Query hashes bind the complete owner, scope,
surface ceiling, task and query settings. Cached values contain vectors, never
retrieved evidence. Every read resolves originals anew. Semantic candidates keep
the existing lifecycle scoring, deduplication, trust scan and final context caps;
RRF retains their cosine order. Verified equivalence transfers relevance to the
permitted canonical before scoring, including when only a duplicate has been
indexed; collapse prevents a second vote and explicit exclusions still apply.
Trace policy binds the embedding tuple. The
lookup is synchronous and local; production composition supplies no lookup,
and the existing semantic flag remains false. No model call is added to the
fast path or held inside a database transaction.

## Dependencies and operations

Add the `pgvector` Python adapter (0.4 series, locked with its NumPy dependency)
and PostgreSQL extension 0.8.7. The local/production database image builds the
checksum-pinned extension on the existing PostgreSQL 16 Alpine base, retaining
the existing locale environment and volume path. Build tools remain in a separate
image stage. Hosted integration uses the published pgvector 0.8.7 PostgreSQL 16
image. The additive migration enables the extension and creates an owner-bound,
forced-RLS vector table. Downgrade drops only rebuildable derived rows and leaves
the shared extension and authoritative originals intact.

## Limits and evidence

Shared contracts cover source revisions, policy restrictions, erasure, rollback,
owner isolation, filtering before limits, bounded inventory and stale work.
A real PostgreSQL concurrency case covers correction versus an in-flight write.
Automatic and deliberate synthetic regressions exercise a non-ownership implicit
constraint, trust scanning and context caps. These deterministic fixtures verify
mechanics, not embedding quality or answer use. M33's release gates remain pending.

Provider egress admission, durable spend reservations and backfill scheduling,
query-vector population, historical/People semantic parity, held-out answer-use
and operational-budget evidence remain required before activation. No production
provider selection, real-memory export or unattended dreaming is enabled.

Sources: [pgvector exact search and filtering](https://github.com/pgvector/pgvector#filtering),
[Python SQLAlchemy adapter](https://github.com/pgvector/pgvector-python#sqlalchemy).

## Verification observations (2026-10-11)

The initial shared lifecycle contract produced eight behavioral assertion failures
before implementation; the implicit non-ownership regression then failed through
both automatic recall and `memory.search`. The focused in-memory suite passed
nineteen cases after implementation. PostgreSQL passed the same lifecycle cases,
concurrent correction and forced-RLS/physical-erasure checks, plus the existing
migration suite: twenty-five cases in 77.03 seconds on the newly built Alpine
vector image. The preceding hosted-image run passed twenty-four cases in 76.98
seconds before the explicit RLS case was added. The extension build stage took
13.6 seconds. These are mechanism checks, not operational-budget or quality gates.
All five frozen M16/M32 manifest hashes remain unchanged.

A subsequent focused regression exposed noncanonical citation during partial
indexing of an equivalence group (one behavioral failure, one exclusion case
already passing). Moving equivalence relevance ahead of scoring repairs that
case without changing stored authority, evidence or explicit exclusions.

## Current People identity filtering (2026-10-12)

Current sparse and dense search apply the same identity restrictions as ordinary
People recall before candidate limits. An original with visible identity assignments
must include a focal person, and every assignment must be resolved and visible at
the requested ceiling. Unlinked ordinary originals retain their existing behavior.
The in-memory memory store filters its eligible originals before either ranking
path. PostgreSQL
uses the People store's current visibility query, including its dependency closure,
in shared correlated predicates before sparse or vector ordering and LIMIT. Missing People storage
withholds a scoped search; no name match can substitute for an identity link.

This index contract alone does not enable People-scoped runtime query preparation.
[ADR-0197](0197-people-semantic-query-narrowing.md) supplies the separately validated
vector binding for current query narrowing. The subsequent historical extension
in [ADR-0200](0200-historical-semantic-revisions.md) supplies distinct revision
slots, current-source fences and shared dated visibility predicates, with dedicated
in-memory and PostgreSQL contracts. Historical indexing remains default-off and
local-only. The release gates remain unchanged.
