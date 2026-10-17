# ADR-0196: Enforce the reserved recall allowance during execution

- Status: Accepted for implementation; release activation remains gated
- Date: 2026-10-12
- Related: ADR-0178, ADR-0183, ADR-0190, ADR-0195
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

The M33 runtime preparation composition also governs actual searches. A durable
reservation admits at most four search attempts in two rounds under the original
ten-second deadline. An automatic round reserves two slots for its base and
possible delta; a deliberate round reserves one per validated search. Unused,
failed and cancelled reservations remain consumed. Existing unconfigured and
frozen control callers retain their explicit legacy behavior.

Execution receives a scoped, nonrenewable permit only after checkpoint persistence.
The permit spans People discovery, ordinary and semantic candidate selection,
ranking, rendering, trace persistence and bounded source recovery. Its deadline
also uses elapsed worker time. A nested People-to-belief read consumes one search,
while parallel tool calls consume distinct slots. A finite source-read fallback
remains inside the original attempt and deadline. Leaving the phase closes its
permit, including in inherited asynchronous contexts. Resume cannot reconstruct
spent execution permits from old reservations; remaining work must reserve again.

When the allowance is exhausted, no further candidate query runs. Deliberate search
returns a typed budget refusal; automatic context explains that recall was bounded.
This is not evidence that no memory exists. Current correction notices and the
separately bounded validation of retained deliberate evidence remain available.
No stale automatic evidence cache is introduced. A long run can lose optional
automatic context after its allowance is spent; the reader must respect that
explicit limitation instead of guessing a fact or treating it as an empty search.

The runtime evaluation applies the same work limit to sparse and semantic arms.
Reports distinguish conservative reserved slots from observed executed searches.
This closes execution accounting, not corpus independence, quality, comparative
operational evidence or release admission.

The shared execution permit is a domain control primitive with injected wall and
monotonic time functions. Bootstrap supplies the worker timer from the determinism
adapter; runtime and People code have no dependency on a concrete retriever or
ambient clock. Recall-query preparation belongs to the memory composition layer
and reaches the runtime only through its preparation protocol.
