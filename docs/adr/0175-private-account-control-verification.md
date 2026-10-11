# ADR-0175 — Private account-control verification

- Status: Accepted under the owner's browser implementation instruction
- Date: 2026-10-10
- Amends: ADR-0164 site verification definitions

## Decision

A site may identify its current account in a button or link whose accessible
name describes an account menu, while its rendered label identifies the account.
Account verification may therefore require an exact role/name control and its
exact normalized visible label, using the runtime's existing private facts.
The configured label remains operator-owned private configuration, not a model
argument, permission, page-wide text search or new DOM-reading capability.

Require one matching control in the bounded observation, matching-revision
facts, a non-editable field kind, and complete label evidence. Missing, stale,
truncated, duplicate or wrong-account evidence fails closed. Both the exact
protected location and positive ready evidence remain mandatory. Interrupted
observations cannot confirm readiness. Existing region definitions remain valid.
Both sign-in methods retain the signed-in/empty-session differential check;
saved sessions receive the same check before a lease is published.

Production may opt into a private catalog through a dedicated Compose overlay.
Only that file is mounted read-only into the isolated browser service, which
retains its existing ownership, mode, size and closed-schema validation. The
default deployment has no account catalog. Definitions must pass fixture and
live account qualification before activation; personal labels and profile IDs
do not belong in the repository.

## Verification

Exercise exact account-menu evidence with synthetic identities in unit,
service-boundary and real-Chromium tests. Reject wrong accounts, unrelated feed
mentions, editable controls, stale or incomplete facts, duplicate controls and
public matches. Verify optional deployment wiring without widening secret mounts.
