# ADR-0186: Persistent sign-in for the dreaming review page

- Status: Proposed — implementation authorized by the owner's persistent sign-in request
- Date: 2026-10-11
- Related: ADR-0028, ADR-0174
- Governing design: `docs/plan/http-api-and-streaming.md`, `docs/plan/memory-reconsolidation.md`

## Decision

The owner reported repeatedly entering the token to review dreaming. Extend the
existing bearer authentication with a purpose-bound review cookie. This amends
ADR-0174's prohibition on browser credential persistence for this cookie alone;
the general owner bearer and private memory content remain absent from storage.

`POST /v1/dreaming/session` requires the configured bearer and `memory.read`, then
issues a thirty-day `__Host-veetbot-dreaming` cookie: Secure, HttpOnly, SameSite=Strict,
Path=/, no Domain. The signed value contains only issuance time and a random nonce.
Its HMAC binds the purpose, configured principal, roles and scopes, using the owner
secret. Ordinary deployments preserve sign-in; expiry, token rotation, principal or
authority changes invalidate it. A cookie cannot renew itself. No new signing key,
database, dependency, or general identity provider is introduced. Session age uses
the composition clock; token-mode review startup refuses a missing clock.

Cookie authentication admits only dreaming status, reconsolidation list/detail,
individual decision, schedule pause/resume, merge undo and sign-out. It yields
only the current principal's existing memory.read/memory.write scopes. Other API
routes still require bearer authentication. Explicit invalid bearer credentials
never fall back to the cookie. Cookie requests require HTTPS and a custom same-origin
header; mutations additionally require an exact Origin match. Browser fetch-site
metadata, when present, must be same-origin. Existing authorization, source checks,
revision/idempotency controls, no-store responses and CSP remain unchanged.

`DELETE /v1/dreaming/session` clears this browser's cookie. Sign-out does not revoke
a separately stolen copy server-side; its maximum lifetime is thirty days, and
rotating the owner token revokes every copy. This is an explicit limit of the
stateless design. The page fetches private content anew after reload and clears
rendered content on sign-out, authentication failure and page hiding. Both Cookie
and Set-Cookie fields are redacted by structured logging.

## Blocked runs

The production diagnosis found legacy purged People heads with unknown reverse
attribution. Preserve the existing fail-closed rule; this change cannot invent
empty attribution or restore erased payloads. The status API reports a content-free
current blocker, independently of historical outcomes. New run receipts retain
finite preparation reasons so source, policy and budget failures are distinguishable.
Existing receipts remain unchanged. Recovery of missing attribution is separate work.

## Verification

Regression tests cover persistent connection, route/scope restriction, expiry,
rotation, changed owner/authority, tampering, insecure transport, cross-site writes,
sign-out and redaction. Chromium exercises reload without another token and confirms
no JavaScript-visible cookie or web-storage content. Both stores retain finite
run reasons; legacy attribution remains a refusal, with no provider calls.
