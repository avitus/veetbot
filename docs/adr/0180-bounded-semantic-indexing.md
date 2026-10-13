# ADR-0180: Bounded semantic indexing and provider admission

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-11
- Related: ADR-0173, ADR-0178, ADR-0179
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Add a provider-neutral embedding port and a bounded background indexing service.
Its explicit configuration binds the embedding space, pricing revision and rate,
egress policy revision, residency permission and request/day ceilings. There is
no implicit model, free-price assumption, credential lookup or provider fallback.
Configuration changes reset inventory for that tuple, not accumulated spending.
Production composition and recall remain disabled pending M33 activation evidence.

The service reads current originals under the memory-owner and People guards.
It uses the existing local content classification and residency policy, with the
memory sensitivity as a floor, plus the existing credential/injection guard.
Only individually permitted public/internal subject and statement values are
serialized as embedding inputs. No raw conversation, evidence excerpt, inferred
label or source metadata is exported. A denied or oversized original gets no
truncated surrogate. Separate inputs retain their identity and scope; batching
does not combine their assertions or grant cross-scope recall authority.

A content-free owner job keeps fixed creation/change bounds, independent cursors,
a configuration digest, generation, lease, attempts and failed-page count. Each
slice reserves sixteen creation positions and sixteen change positions; duplicates
are embedded once, and unchanged current vectors are skipped. Inserts beyond the
captured bound wait for the next generation. The completed change watermark
survives generation rollover; old journal entries are not replayed from zero.
Denied or deleted sources advance inventory, but inventory completion is never
described as complete indexing.
Completed generations are revisited when bounds change, configuration changes,
or a new UTC day starts. No unbounded queue or all-pairs scan is introduced.

Each slice has at most one request, a sixty-second work deadline, a ninety-second
lease and a thirty-second provider timeout. Failure cleanup has a separate
five-second limit and cannot authorize a new send. While a request runs, a bounded
one-second check observes admission withdrawal and the durable lease. Calls take
place outside all repository transactions. Shutdown cancellation stops the call.
Three uncertain attempts at the same page produce a failed-page receipt and
advance the scan so a failing page cannot starve older records indefinitely.
A later generation can reconsider the page. Failed attempts are never hidden as
successful embeddings.

Before sending, commit a reservation for the complete request under the owner
lock, checking request and UTC-day headroom. The initial configurable ceilings
cannot exceed USD 0.25/request or USD 2/day; these are implementation safety caps,
not M33's still-required preregistered operational-budget evidence. Money rounds
up to the database quantum of USD 0.00000001; explicitly free calls reserve one
quantum until verified settlement. A request crossing midnight keeps its admission
day. Retries receive new reservations. Duplicate admission cannot authorize a
second send. Missing usage, cancellation, timeout or lost leases retain the full
charge; only verified, attributable usage can lower it. Settlement remains allowed
when new admissions are disabled. Crashed reservations become unknown on recovery.

Vectors, usage settlement and inventory advancement commit together after a
fresh lease check. Each vector still compare-and-swaps its original source version;
a correction or erasure during the provider call prevents its publication.
Accounting retains neither input text nor vectors, and survives source erasure.
New job/spend tables use forced tenant RLS. Their downgrade locks the owner and
accounting tables, fails closed when RLS would hide rows, and refuses retained work
or spending rather than silently resetting a budget. Dreaming's leases, budgets,
activation and frozen benchmark artifacts are unchanged.

## Transport and validation

The first adapter uses the [OpenAI embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create).
It makes one request to the fixed HTTPS endpoint, without redirects, ambient
proxy routing or internal retries. It verifies model identity, complete distinct
response indexes, numeric finite nonzero vectors, dimensions and consistent
positive prompt/total usage within the reservation. Provider error prose is never
retained. Limits are thirty-two input strings, 4,096 UTF-8 bytes per string,
65,536 conservatively estimated input tokens, and 2 MiB of response bytes.
The neutral port exposes no provider SDK values.

Shared contracts cover durable cursors, exclusive leases, restart recovery,
rollback, day accounting, configuration changes and finite retries. Worker
regressions cover admission-before-send, policy denial before serialization,
source changes, current-vector reuse, cancellation, withdrawal and fair old-record
coverage. Transport tests use fabricated inputs and a mock HTTP transport; they
make no paid calls and supply no production activation or embedding-quality claim.

## Remaining work

Production scheduling and configuration admission, query-vector population,
historical/People parity, task-aware search and recovery, held-out answer-use
measurements and complete matching activation evidence remain required. This
mechanism does not complete M33 or enable semantic recall on a running deployment.
