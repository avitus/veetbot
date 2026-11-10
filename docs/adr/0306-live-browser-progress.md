# ADR-0306: Show browser phases while an operation is running

- Status: Accepted under the owner's browser work-package continuation on 2026-11-10
- Date: 2026-11-10
- Amends: ADR-0158
- Scope: W2 browser progress, hosted transport and native activity presentation
- Detailed design: `docs/plan/browser-automation.md`, `docs/plan/http-api-and-streaming.md`

## Decision

The existing phase collector reports the current phase as work begins and returns
from nested phases. A progress record has a version, closed phase and placement,
and bounded elapsed milliseconds. It contains no page text, address, identifiers,
input, exception text or secret. Each operation offers at most 128 updates into
a one-item queue. New progress replaces an older queued update. Terminal
phase diagnostics retain their existing contract.

The authenticated hosted client requests the versioned browser-progress NDJSON
media type. A supporting service sends bounded progress records followed by one
result envelope carrying the original HTTP status and response body. Validation
and authorization failures before execution retain their ordinary HTTP responses.
Older services return their existing JSON response. The client bounds streamed
bytes and records, validates progress, and requires a final result. A broken
stream is an operation failure under the existing effect and recovery rules.
No transport retry or repeated action is added. Disconnect cancels and joins
the service operation, including its existing cleanup.

The worker forwards validated updates through the existing best-effort live
broadcaster as `tool.call.progress`, bound to the current run and call. Publishing
has a finite timeout and one queued update; failure cannot fail or delay the
browser operation. The publisher is stopped before the terminal tool event.
Progress frames carry no SSE event id, are not persisted or replayed, and supply
no model context, permission, completion evidence or recovery decision.

The native client displays fixed phase labels on an already-running browser
activity. It ignores unknown versions, invalid fields, another run's updates,
and updates for calls that have finished. Terminal events clear live progress
and retain the authoritative result and terminal phase diagnostics. Reconnecting
clients recover those durable events through the existing stream contract.

## Verification

Regression coverage must observe progress before releasing a blocked operation,
exercise successful and refused hosted results, old-service compatibility,
malformed and oversized streams, bounded updates, cancellation and publisher
failure. Client tests must reject unsafe labels and late progress after a terminal
result. Existing browser diagnostics, action uncertainty and hosted provider
contracts remain required. Production delivery and broader W2 acceptance remain
separate from these construction checks.

Construction checks on 2026-11-10 passed 18 focused progress cases, 122 browser
provider/session contracts, 27 real-Chromium runtime cases, and 1,185 Apple core
plus four accessibility-evidence tests in the local VMs. The initial streaming
regression failed because the response waited for the operation to finish. A
second regression reproduced interrupted cleanup on an HTTP disconnect; it now
passes for both a waiting reader and a blocked send. Lint and typing pass.
The broader static partition and final hosted verification remain delivery checks.
