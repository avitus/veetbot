# ADR-0149: Deployment provisions the owner permission set

- Status: Accepted by the owner, 2026-10-02
- Date: 2026-10-02
- Related: ADR-0048, ADR-0119

## Context

Model settings shipped with exact `settings.read` and `settings.write` checks,
but an existing production environment lacked both. The owner requested that
permissions accompany deployments instead of requiring a manual activation
step after each release.

## Decision

The checked-in production environment template declares the reviewed owner
scope set. Deployment unions it with the host's explicit `AUTH_SCOPES`, sorts
and deduplicates the result, and writes only that assignment into the staged
release's `.owner-scopes.env`. It exports the same value for deployment checks.
The API, interactive worker, asynchronous worker and maintenance worker load
that file after the host environment. Promotion activates it with the release;
rollback selects the target release's file. The optional systemd directive
allows rollback to releases that predate the file, using the host grants.
The host environment and its credentials are never rewritten by this process.

Every closed platform scope must be classified by a regression test: owner
scopes belong in the template; `surface.read` and `surface.write` belong to
restricted surface identities, and `demo.write` is test-only. A new scope
therefore requires an explicit deployment decision before checks pass. This
also includes permissions for optional owner features; flags, credentials,
provider admission, deterministic policy and approval checks still govern use.
No wildcard, implicit hierarchy, or runtime authorization bypass is introduced.

Restricted schedule, notification, surface, calling, ingress and execution
units never load the owner file. Operator-supplied host grants remain explicit
and are preserved. An operator who needs a narrower owner set must change the
reviewed template; removing a managed scope only from the host file no longer
revokes it. This mechanism targets this repository's single-owner deployment.

Deployment requires an authenticated HTTP 200 from model settings after the
release's readiness and session-index checks. The probe reads only; it never
saves model choices or creates a settings revision.

## Verification

`tests/unit/test_deployment_scopes.py` covers complete scope classification,
owner-only systemd loading order, preservation, idempotence, and rejection of
malformed input. `deploy/app/release.test.sh` starts with a stale host grant,
requires the generated settings scopes, checks the host file is untouched,
and rejects a release whose Settings route returns HTTP 403.
