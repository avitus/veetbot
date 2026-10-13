# ADR-0190: Durable run recall preparation budget

- Status: Implementation in progress under ADR-0178; activation remains gated
- Date: 2026-10-12
- Related: ADR-0181, ADR-0182, ADR-0188, ADR-0189
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Bound the optional local semantic preparation phase across the whole run, rather
than restarting its allowance before each context assembly or tool batch. Separate
local query planning from provider preparation. Planning may read the owned session
and current working state, but performs no model call and receives no gold labels.
Deferred `tool.call` searches use the execution pipeline's same pinned-target
resolver, after checking the wrapper and target are enabled for the run's agent.
Direct searches also require the target to be enabled. Malformed,
unoffered or control-tool targets never enter preparation; valid searches retain
the same query as direct `memory.search`.

Before provider work, save a typed checkpoint reservation for at most four queries
and two preparation rounds, with one ten-second absolute deadline beginning at the
first planning attempt that reserves work. A batch must fit the remaining allowance
in full. The measured local tuple admits at most 256 tokens per query, hence at
most 1,024 input tokens across these reservations. `LocalEmbeddingConfig` permits
a schema maximum of 512 tokens per query; the selected model's smaller bound
still applies. Local inference has zero provider price. The existing
owner/day ledger and uncertain-reservation handling still apply. This does not
admit remote query egress or assisted model planning.

Failures and cancellation consume the reservation. Checkpoint events carry the
content-free counters and deadline so recovery after loss of materialized
checkpoints cannot reset them. Lease takeover and run resume retain the same
allowance. Failed checkpoint persistence prevents provider work. Ordinary sparse
recall remains available when optional work is withheld; its existing tools and
run limits are unchanged. These are preparation limits, not permission to exceed
the existing final recall item/token caps.

Runtime-bound query handles expire at the shared deadline. Governed source
recovery additionally requires the exact query to have a prepared vector; another
query in the same tool batch cannot borrow a handle to expand its sources. Existing
injection, owner, scope, source revision and erasure checks remain mandatory.

This closes a runtime work-budget gap. It does not by itself complete task-context
planning, missing-evidence escalation, whole-request cost/latency evaluation, the
shared rendered-evidence budget experiment or M33 gates 5 and 10. Numerical release
ceilings still require a production-shaped baseline and preregistration.
