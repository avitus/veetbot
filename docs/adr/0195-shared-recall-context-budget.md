# ADR-0195: One rendered recall allowance per model request

- Status: Implementation in progress under ADR-0178
- Date: 2026-10-12
- Related: ADR-0183, ADR-0190, ADR-0192
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

Automatic base and delta recall share the in-turn allowance, but each deliberate
`memory.search` result can add another twenty items and two thousand tokens to the
same model request. Comparing strategies with those different effective context
caps would violate the M33 evaluation contract.

## Decision

Share one twenty-item, two-thousand-token rendered allowance across retained
`memory.search` results and automatic base/delta recall in each model request.
The frozen prefix snapshot keeps its separate existing class. Corrections remain
outside this allowance and must still be read when no optional recall fits.

The builtin search returns content-free counts bound to the exact rendered bytes.
Only its successful result may persist that receipt through the tool pipeline.
Context assembly keeps complete search result blocks in reverse conversation order,
deduplicates identical blocks, and withholds whole blocks that cannot fit. At most
four retained search blocks can enter dependency validation. It keeps
the call/result pair, trust and canonical event content; a budget omission marker
replaces only the model presentation. Older results without a
trace-bound receipt are withheld in model context; pure budget inspection charges
legacy blocks conservatively until validation. Changed or excerpted bytes cannot
borrow an old receipt, nor can one payload borrow another payload's trace permission.

Every assembly revalidates retained results through current authoritative storage
under one shared second. The trace must match its owner, session, current run,
rendered digest and item count. Original records pass the same hard predicates;
statements, metadata, merge membership, derived support and the actually rendered
source quotes must remain unchanged. New quotes never enter this validation path.
Unknown projections are withheld; the builtin `memory.search` produces belief
packages, while automatic People context continues to be freshly assembled.
Errors withhold unvalidated blocks and cancellation propagates.

Semantic-derived blocks additionally require current runtime authority and release
admission. This check does not revive a query vector after checkpoint changes or
reserve further search work. The search deadline limits additional work; consuming
already produced evidence still checks current run/lease/transport authority and
every source, even after that work allowance is spent. The trace check adds no
embedding/provider call or ranking pass. Its latency belongs to assembly cost.

Automatic query preparation and context assembly derive the same remaining allowance
from the checkpoint with the same current-evidence checks. Automatic retrieval uses that remainder; its existing base and
delta accounting and current-source validation remain. This is deterministic context
selection, not a cached answer, a source visibility grant or a global relevance rank.
The evaluator must continue to distinguish per-query ranking from final visibility.

This bounds the memory evidence simultaneously shown to each reader. Repeated model
input/output usage remains charged to the existing run cost/token budgets; it is
not made free by a matching receipt. The four-query, two-round preparation reservation
and shared deadline remain separately enforced. Complete search-work accounting,
assisted planning comparisons and passing release receipts are still required.

## Verification required

Regressions must cover automatic plus deliberate evidence, parallel and repeated
search blocks, deferred calls, legacy results, byte/receipt mismatch, canonical
event preservation, checkpoint replay, current-source/authority changes, partial
source packages and corrections after allowance exhaustion.
Production-shaped comparisons must use the same cap for control and candidate.
