# ADR-0214: Formation deadline repair with retained observation provenance

- Status: Owner-authorized repair; continuation and release evidence pending
- Date: 2026-10-18
- Related: ADR-0194, ADR-0212, ADR-0213

## Cause and decision

The owner explicitly approved the formation repair, including runtime changes,
preservation of the 413 completed observations and an aggregate cap of USD 500 if
necessary. This supersedes ADR-0213's stop boundary for a separately verified repair.
It does not authorize another unchanged retry or change a failed attempt's outcome.

The retained provider journal records cancellation inside prediction-error
distillation. The enclosing extraction completes with its transient fallback and
schedules a provider re-pass. In the original call path, the local
`asyncio.timeout(120)` converts that cancellation into the caught `TimeoutError`;
external cancellation would escape instead. Thus the local stage deadline ended
the formation call. The originals do not establish why the provider needed longer,
whether it was still making progress, or what its unfinished response would contain.
The fixed evaluation clock's zero stage latency is not elapsed network time.

Formation@11 will use a finite 600-second overall stage deadline, separately from
the unchanged 120-second request transport timeout. OpenAI passes the latter to
its SDK as HTTP operation timeouts; it is not the total time allowed for a stream
that continues to receive events. This removes the shorter enclosing cutoff while
retaining the transport stall bound. The 600-second limit matches the ordinary
model-request budget. Explicit caller timeout overrides retain their exact limit.
Formation@9 and the other frozen controls retain their 120-second deadline.

The model, effort, prompts, schemas, output-token ceiling, request serialization,
three-stage sequence, parsing, source validation and fallback policy do not change.
An exhausted stage deadline remains a failure and records its local origin in safe
audit metadata. Caller cancellation continues to propagate. No provider or answer
retry is introduced by this repair. Reader latency, search, context and cost limits
remain unchanged; preparation time remains separate from answer latency.

## Preservation and continuation boundary

Preserve all 413 completed rows byte-for-byte with their original timestamps,
runtime identities, banks and ledgers. Keep every failed and pending bank and
unknown reservation. Never relabel these rows as measurements of the repaired
runtime. The remaining 1,187 positions retain the full 400-scenario, two-path,
sparse/hybrid formation comparison, selected by configuration before scoring.

A separately versioned continuation must bind the repair source, predecessor
protocol, exact completed inventory and the owner authorization. Compatibility is
limited to this transport/deadline repair: verify the original provider request
bytes, retained formation bank contents, parsing and reader behavior remain identical.
Record the runtime change explicitly in collection and publication provenance;
do not erase it from the deployment input comparison. A different model, prompt,
schema, formation decision or reader change cannot use this exception. Completed
answers are not rerun. Only the scenario with failed preparation and no completed
answer may form a new bank after the repaired source and operational admission pass.
New or unknown reader failures do not receive automatic retry authority.

The existing absolute deadline remains 2026-10-25T00:27:32.501898Z. All ten current
ledgers count: USD 128.20996350 reported plus USD 12.21728 uncertain, totaling
USD 140.42724350. Existing allocation is not renewed. The owner permits increasing
the aggregate cap from USD 300 to at most USD 500 if necessary; a fresh reconciled
allocation must document any use of this permission. This decision itself allocates
no money. All subsequent segments and reservations also count.

## Verification and release

Reproduce premature cancellation with a progressing provider and a fixed clock,
then prove the repaired deadline admits its completion without changing a single
request field. Verify explicit short overrides, final deadline exhaustion and
external cancellation. Preserve the frozen control and formation validation tests.
Continuation admission must refuse changed original rows, unrelated runtime edits,
unbound repair identity, failed-bank replay, reused workers and budget/deadline
renewal. Verify the final source before one separately admitted continuation.

No partial collection, timeout repair or synthetic test establishes answer quality.
All numerical gates and genuine independent human review of difficult failures,
interruptions, repairs and uncertain costs remain required. Eventual deployment
needs actual input-equivalence assessment and an authorized release route. No new
pull request, other development branch or unrelated production hold is authorized.

The progressing-stream regression reproduced the old deadline cancellation and
passes with the repaired finite deadline and an identical complete provider
request. Offline recovery on a disposable copy preserved all 413 results and
1,272 non-lock evidence files, reopened the durable store, verified all prior
worker admissions and retained the absolute deadline. Its synthetic successor
protocol did not admit paid work or modify the original state. The import-order
regression verifies that the package census receives the repaired class on its
first load. These checks establish recovery mechanics, not answer quality.
