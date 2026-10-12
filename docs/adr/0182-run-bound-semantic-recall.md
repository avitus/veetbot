# ADR-0182: Run-bound semantic recall

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-11
- Related: ADR-0178, ADR-0180, ADR-0181
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Add `RuntimeSemanticQueries` beside the experimental synchronous lookup port.
The shared retriever exposes explicit slow-path preparation and resolves a local
lookup before opening its recall transaction. Automatic recall and `memory.search`
use that same binding. Neither ordinary recall nor a cache miss prepares a vector,
queues work or sends a provider request. A failure preserves sparse recall.
Preparation and retrieval apply one effective-query normalization, including the
current merge policy; otherwise a valid prepared vector misses the cache when
owner-reviewed merging is enabled. Foreign-owner input never becomes authorized
through normalization.

`RunSemanticQueries` reads the owner-authorized session, run and latest checkpoint.
It requires an active session, a running uncancelled run, an unexpired worker lease,
remaining run deadline, available principal scopes and matching identities. Missing
or inconsistent state withholds the optional channel. The composition must supply
an explicit trusted authority resolver: effective policy version, full retrieval
profile identity, surface identity and task-content sensitivity floor. No model
argument or read-clearance ceiling supplies that authority; an absent resolver
result withholds preparation and lookup.

The task digest covers the session and its metadata, principal roles/scopes, run
identity/parent/kind/agent/step/lease epoch and owner, limits, seed, provider pin,
and the complete saved checkpoint. That includes conversation, working state,
tool/model pins and compaction lineage. Heartbeat timestamps and mutable usage
counters do not change task identity. The cache retains hashes and vectors, never
checkpoint or source text. This binds task context; it does not yet plan searches
from unresolved information needs.

Each recall gets its own bound lookup handle. There is no shared mutable current
chat or task. Thirty-two context handles share one 128-vector LRU, with five-minute
lifetimes, shortened to the current lease or run deadline. A bound handle also
checks this expiry locally before returning a vector. Expiry, observed state or authority changes, withdrawal and eviction
revoke affected handles and their vectors. Restoring an old context cannot revive
its revoked handle. Other chats retain their valid vectors. Admission failure
closes the entire optional cache.

Binding has a one-second limit. Explicit preparation has one ten-second deadline
including state reads; ADR-0181's conservative cancellation cleanup can take up to
three more seconds. Runtime state is rechecked before reservation, before sending,
while waiting, before settlement and before cache publication. These reads and
provider calls stay outside recall and reservation transactions. Cancellation or
changed authority cannot publish a late result; uncertain usage retains its
committed reservation in the shared owner/day budget. These mechanism bounds are
not measured release latency claims.

Snapshots, historical queries, People-scoped queries and existing-transaction
People recall retain their established paths. Production still supplies neither
runtime nor experimental semantic lookup. No deployment configuration or provider
activation is introduced by this mechanism.

## Verification and remaining work

Behavioral regressions first failed on absent shared preparation. The automatic
and deliberate cases use a synthetic dietary constraint with no ownership anchor
and enable owner-reviewed merging. Further checks cover current checkpoint and
policy identity, missing authority, cancelled/expired/unleased runs, foreign
identities, parallel chat handles, cache limits, late results and retained spend.
PostgreSQL checks exercise committed run/checkpoint state, provider admission
outside transactions, both retrieval paths and current-source correction.

Production configuration/evidence admission, the trusted authority resolver,
explicit slow-path scheduling and context-builder recall-cache invalidation still
need composition. Source indexing needs maintenance scheduling. Historical/People
parity, task-aware planning, source recovery and independently authored held-out
formation/answer-use evidence remain open. All twelve M33 gates stay pending;
there is no production activation or claim that the original incident is fixed
on the running deployment.
