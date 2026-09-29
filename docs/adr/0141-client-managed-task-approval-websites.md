# ADR-0141: Client-managed task approval websites

- Status: Accepted by owner request on 2026-09-28
- Date: 2026-09-28
- Related: ADR-0129
- Amends: ADR-0129 decision 2, configuration ownership and removal timing
- Detailed design: `docs/plan/browser-automation.md`

## Context

The owner requested that the task approval website list be maintained on the
server and accessible through the clients. Adding a website must not require
a server configuration change, restart or deployment.

## Decision

Store a revisioned list per tenant and principal in PostgreSQL. Clients read
and replace it through GET and PUT `/v1/browser-task-scopes`, protected by
`browser.grant.read` and `browser.grant.write`. The task-grants feature flag
also gates these routes. No model-callable tool edits this list.

Keep the existing maximum of sixteen unique exact public HTTPS origins with
one nonsensitive path segment. This changes management of the scopes, not
their bounds. Adding an entry makes task approval available; the owner must
still accept the approval card. The thirty-minute window, two-hundred-action
limit, typed-character budget and sensitive-action exclusions remain intact.

Use optimistic concurrency: PUT carries the revision read by the client.
A stale edit receives conflict. An exact immediate replay returns the saved
revision without duplicating audit events. A no-op does not advance revision.
The native Website Access settings show, add and remove server entries, reload
on foreground, and report failures without claiming unsaved changes succeeded.

Read persisted scopes when offering a task grant, resolving an approval and
consuming a use. Serialize grant creation and consumption with edits using a
policy row lock. Removal ends all unended grants outside the new list in the
same transaction, with the existing scope-removed audit. Re-adding a scope
cannot revive them. Actions authorized before removal may finish in flight.

Audit successful edits as `browser.task_scopes.updated` process events with
the principal, tenant, revision and bounded scope list, in the same transaction.
RLS and explicit owner filters isolate all rows. Empty lists are durable.

At startup, seed the configured owner once from `BROWSER_TASK_GRANT_SCOPES`
when the feature is enabled. Insert only if no policy exists; never overwrite
client changes or repopulate a cleared list. This preserves existing configured
sites during rollout. The environment variable becomes a migration/bootstrap
input, not ongoing runtime authority. Other principals start with an empty list.

## Validation

Cover API reads and writes, scope validation, flag and permission boundaries,
owner isolation, stale edits and uncertain-response replay. Shared persistence
contracts cover restart-safe initialization and revision checks. PostgreSQL
checks cover transaction rollback, concurrent edits, RLS and persistence across
connections. Runtime tests cover fresh offers, stale-offer rejection, removal
and re-addition without revival. Native tests cover request encoding, successful
edits, server failures, conflicts and refresh behavior.

## Alternatives

A deployment environment list requires an operator for routine owner choices.
A device-local list diverges between clients and cannot govern a server worker.
Neither meets the requested shared control plane.
