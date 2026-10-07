# ADR-0158: Content-free browser operation diagnostics

- Status: Accepted under the owner's ADR-0156 implementation instruction
- Date: 2026-10-01
- Related: ADR-0058, ADR-0127, ADR-0130, ADR-0156
- Detailed design: `docs/plan/browser-automation.md`, `docs/plan/tool-system.md`

## Decision

Record bounded, typed phase summaries on the existing terminal browser tool
activity events. Trusted execution collects them; tool result content cannot
supply or overwrite them. Collection never changes authorization, dispatch,
retries, the effect watermark, or the observation seen by the model. Existing
policy and approval events remain authoritative for authorization. This is an
additive metadata contract, versioned independently as diagnostics version 1.

Each summary contains at most 64 phase records in completion order, a truncation
flag, and a total duration. A record contains only a closed phase, placement,
outcome and failure category, and integer elapsed milliseconds capped at one
hour. Nested phase durations overlap and must not be added as wall time.
Unknown exceptions map to an internal failure category; their messages, class
names, arguments and causes are never copied. No URL, title, selector, label,
input, cookie, header, body, profile reference or page revision is accepted.

The collector is scoped to one asynchronous operation and receives its clock
from the caller; domain code performs no ambient clock reads. Instrument session
binding, launch, navigation, readiness, observation, dispatch, projection and
cleanup. Hosted session responses may include the same versioned summary as a
sibling of their ordinary response. The authenticated client validates the
entire summary before admitting it; invalid diagnostic metadata is discarded
without changing an otherwise valid operation. Older services omit it and older
clients ignore it. Diagnostics are evidence about execution, never authority.

Reuse existing event access control, export exclusion and deletion/retention
rules. This first W2 implementation adds no separate high-volume event stream,
raw trace, HAR or capture retention store. The native run activity details
show the validated duration, fixed failure guidance and phase summary beside
existing action and permission information; unknown versions or fields that
would become free-text status labels are not presented. The program's proposed seven-day
operational retention and thirty-day metrics apply to a later separately owned
store, not retroactive deletion of the audit log. Terminal metadata cannot
explain a process killed before it records its terminal event; the existing
started event and effect watermark continue to govern that case.

## Verification

Reproduce missing metadata first, then verify nested success and failure,
concurrent collector isolation, strict rejection of extra fields and unsafe
strings, bounded volume, disabled collection, hosted success and failure, and
terminal event integration. Secret canaries must be absent from the new
metadata. Real Chromium tests prove phase ordering and dispatch uncertainty.
This supplies W2's operational foundation; live in-flight phase streaming,
aggregate operational reporting and full program exit evidence remain tracked.
