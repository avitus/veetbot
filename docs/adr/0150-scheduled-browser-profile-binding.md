# ADR-0150: Scheduled website reads pin an owned browser profile

- Status: Proposed
- Date: 2026-10-05
- Related: ADR-0058, ADR-0059, ADR-0072, ADR-0088, ADR-0130
- Detailed design: `docs/plan/scheduling.md`, `docs/plan/browser-automation.md`

## Context

The owner requested diagnosis and repair of the daily personalized X briefing.
A scheduled occurrence had no browser-profile binding, advertised no browser
tools, made no tool calls, and completed with an inability-to-access response.
The existing scheduling integration design requires a pinned profile, but the
schedule definition and occurrence materializer had no way to carry it.

## Decision

The authenticated schedule definition accepts an optional `browser_profile_id`.
The caller must hold `browser.profile.read`, explicitly include it in the
requested execution scopes, and select an owned `READY` profile. The immutable
revision stores only its UUID. The scheduler copies that pin into the ordinary
reserved session binding, without reading browser state or gaining database
privileges. Its existing fresh-authority check refuses a revoked scope.

The run worker checks ownership, scope and profile readiness before planning,
and requires the browser read tools in the plan before calling the model.
Unavailable access produces a failed run with a stable reason code, which the
existing schedule accounting and notification paths consume. The hosted
provider still revalidates the profile when acquiring and using its lease.
Every execution attempt, including resume, also verifies that composition
uses a session-bound hosted provider or a fixed hosted provider with the same
profile UUID. A mismatch fails before model work or pending tool dispatch; a
deployment-wide pin cannot substitute another account for the schedule pin.
Hosted adapters accept the same tenant and principal with a subset of the
configured owner's scopes and roles; full principal-object equality incorrectly
rejected restricted scheduled runs. Other identities and broader authority
remain refused. Budgets and deadlines remain those explicitly pinned by the
schedule.

Content-only chat edits preserve the binding. Chat creation continues to grant
no scopes and select no profile: neither a model argument, a URL in a prompt,
nor the fact that a profile exists authorizes a binding. An owner or operator
uses the authenticated full-definition update to bind an existing schedule.
Ordinary browser action approvals remain required; this repair creates no
standing or task grant and does not promise unattended mutation or scrolling.

## Compatibility and validation

The new field defaults to null in old JSON revisions; no schema migration is
needed. Null bindings retain the original create-request hash so an old
idempotency key still replays. Historical revisions and materialized sessions
are unchanged. No milestone status or gate count changes.

The regression suite covers HTTP binding and rejection, retries, guarded edits,
unchanged earlier revisions, content-patch preservation, materialization,
restricted scopes and budget, and startup failure before model calls. The
PostgreSQL regression passes a persisted binding through materialization and
the real session selector and tool pipeline into a fake isolated browser
service. Production acceptance still requires deployment, explicitly binding
the existing briefing, and observing an authenticated feed read. The isolated
scheduler's configured principal directory must include `browser.profile.read`
in its `AUTH_SCOPES` before that schedule requests the scope; restart the
schedule worker after changing its non-secret authority configuration. No
browser credential is copied into the scheduler environment.
