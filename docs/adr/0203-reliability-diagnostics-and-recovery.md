# ADR-0203: Safe failure diagnostics and bounded recovery

- Status: Accepted
- Date: 2026-10-14
- Related: Sections 13 and 19, Milestones 15 and 26, ADRs 0023, 0025, 0065 and 0104
- Authorization: The owner requested implementation of the reliability assessment on a dedicated `reliability` branch.

## Decision

1. Unexpected API, run and tool failures retain an opaque diagnostic reference,
   a stable code-location fingerprint, bounded exception-class chains and
   first-party stack locations. They never retain exception text, source lines,
   local variables, prompts or private upstream bodies. Run failure details keep
   the reference and fingerprint so an operator can correlate the durable record
   with the service journal. Public API errors retain their existing envelope.
   Run views expose only a validated optional diagnostic UUID, also displayed
   by the native client. Generic service exception logging uses the same safe
   metadata rather than formatting exception messages or traceback source lines.
2. Request, run and tool log scopes restore their previous context on every exit,
   including cancellation and nested in-process dispatch. Service logs identify
   the release. Diagnostics do not add an external telemetry vendor or exporter.
3. A missing or expired email body triggers bounded read-only retrieval before
   drafting. Only the affected account's read server is prepared, on demand;
   fresh cached drafts still start no MCP server. This amends ADR-0104's absolute
   no-server rule for drafting, preserving its lazy preparation intent. Complete
   source evidence, retention, account identity, budgets, approvals and immutable
   draft revision rules remain mandatory. Retrieval failure preserves existing edits
   and records the additive `dependency_unavailable` failure reason, with actionable
   fixed guidance. Older clients already tolerate unknown failure reasons.
4. Repeated worker infrastructure failures use capped exponential backoff that
   resets after a successful iteration. Cancellation is never swallowed. Optional
   post-run work has independent error and timeout boundaries, so one hook cannot
   starve later hooks or invalidate a committed answer. External writes and
   uncertain effects are never retried by these changes.
5. An operator reliability report reads tenant/principal-scoped aggregate outcomes,
   failure groups, recovered tool failures and current queue health in a bounded
   read-only database transaction. Cancelled and waiting runs are not successful
   completions; no traffic is not evidence of availability. A separately enabled
   operational check emits deduplicated alerts and recoveries through the existing
   notification outbox. Its failure-rate and queue thresholds are explicit.
   Database failure exits unsuccessfully and cannot be reported as a healthy result.
6. Failure-injection regressions assert successful recovery and preservation of
   committed state, in addition to the existing privacy and authority assertions.
   No milestone completion, release gate or production activation is inferred
   from implementing this repair.

## Consequences

The branch can be reviewed and verified without production mutations. Deployment,
alert activation and an independent outage channel remain operator actions; the
existing outbox cannot alert while its database is unavailable. The remainder of
Milestone 15, including backups and restore rehearsal, remains governed by its
existing design. No new dependency, model routing or automatic replay of failed
external actions is introduced.
