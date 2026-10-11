# ADR-0177 — Bounded browser startup recovery

- Status: Accepted under the owner's continuation instruction
- Date: 2026-10-10
- Scope: The browser autonomy extension of ADR-0164

## Decision

Trusted hosted-provider composition supplies an application-owned lease
acquisition policy. Within one tool invocation, acquire may make at most three
attempts, and only a retryable `tool.browser.provider_unavailable` permits
another attempt. The complete acquisition request remains identical, including
the run attempt and requested lease expiry. A lost response can therefore
reattach to the same service-side lease without launching a duplicate browser.

Use exponential backoff with jitter derived from the durable invocation ID and
attempt index: the first delay is between 0.25 and 0.5
seconds, the second between 0.5 and 1 second. Both delays and all attempts share
the original tool deadline, also bounded by the run deadline. Check cancellation
and remaining time before each attempt and after sleeping; never extend either
deadline or change the run's budgets. An attempt that consumes the deadline
cannot retry. These are transport attempts inside one existing invocation,
not extra tool calls or a separate durable workflow. Crash recovery retains the
ordinary invocation ledger, worker retry limit and acquisition identity.

Only acquisition is wrapped. Navigation, observation, action, upload, renew and
close are not replayed by this policy. Authentication, owner sign-in, profile
contention, revocation, invalid output and non-retryable provider failures stop
immediately. Admission and protected-account checks still run in the isolated
service before it publishes a lease. Failed attempts remain visible as bounded,
content-free acquisition diagnostics. Exhaustion preserves the original stable
failure and the workflow's explicit unverified outcome.

This adds no tool version, model argument, dependency, permission, benchmark or
milestone gate. Independently constructed providers may retain single-attempt
acquisition; production composition installs the recovery policy for fixed and
session-selected hosted profiles.

## Verification

First reproduce the scheduled briefing stopping on a transient acquisition
failure. Verify recovery on the second and third attempts, exhaustion at three,
identical request identity, backoff bounds, authentication and permanent refusals,
cancellation and deadline exhaustion. Drop an acquisition response after the
real session service creates a lease and require one runtime and one navigation.
Drop navigation and action responses and require no replay. Exercise both hosted
composition paths and preserve the existing uncertain-action tests.
