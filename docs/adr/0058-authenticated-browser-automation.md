# ADR-0058: Provider-neutral authenticated browser automation

- Status: Proposed
- Date: 2026-08-19
- Related: Sections 8, 9, 18, 22, 29, 32, and 33; ADR-0005, ADR-0017,
  ADR-0021, ADR-0044, ADR-0054
- Detailed design: `docs/plan/browser-automation.md`

## Context

Veetbot can search and extract public pages but cannot operate a rendered site,
hold a principal-owned authenticated session, or take action through a browser.
Scheduled and interactive workflows need that capability without exposing
passwords, cookies, arbitrary JavaScript, or broad external-write authority to
the model. The existing tool pipeline, approval lifecycle, credential broker,
external-untrusted label, effect-sent boundary, and reserved device seam should
remain the enforcement path rather than being bypassed by a separate agent.

## Proposed decisions

1. Add a provider-neutral `BrowserProvider` whose implementation is bound by
   trusted composition to one principal, opaque profile, and origin policy.
2. Separate read-only `browser.navigate` and `browser.observe` from mutating
   `browser.act`. Classify the first pair as bounded `NETWORK_READ`; classify
   every generic browser action conservatively as `EXTERNAL_WRITE`, `HIGH`, and
   `NON_IDEMPOTENT`.
3. Return bounded accessibility observations with opaque element references and
   page revisions. Never expose raw DOM, scripts, cookies, storage, headers,
   credentials, or provider diagnostics.
4. Make authentication a user-controlled surface. The model cannot enter or
   retrieve passwords, passkeys, MFA codes, cookies, or tokens. CAPTCHA and
   reauthentication suspend with `needs_user`.
5. Store persistent profile material encrypted outside PostgreSQL behind a
   tenant/principal-scoped opaque reference. Revocation and deletion invalidate
   leases and grants.
6. Require ordinary approval for browser mutation by default. Unattended work
   may use only a narrow, expiring, revocable standing grant created through an
   explicit approval surface and revalidated before every action.
7. Treat all browser output as `EXTERNAL_UNTRUSTED`; enforce allowed origins on
   initial navigation, redirects, frames, popups, and actions. Private-network
   and non-HTTPS destinations fail closed.
8. Reuse the effect-sent boundary. A possibly delivered non-idempotent action
   becomes uncertain and is not replayed without a verified postcondition.
9. Support both device-local and isolated hosted providers behind the same
   port. Hosted profiles are the intended reliable scheduled-work topology;
   device-local profiles require presence.
10. Deliver in default-off slices. After the contracts and fake-provider seam,
    use Playwright 1.x with headless Chromium for the ephemeral adapter. Create
    a fresh non-persistent context per provider lifetime, use a temporary
    scrubbed process environment, disable downloads and service workers, close
    popups, and route all requests through both exact-origin interception and
    the audited worker egress proxy. Browser binaries remain an explicit
    deployment install step.
11. Bind every interactive reference to the exact observation revision and a
    stable Playwright element handle. `browser.act` is always a serial,
    approval-gated, non-idempotent external write. It refuses password and
    one-time-code controls, marks the effect before dispatch, and converts an
    ambiguous browser failure after dispatch to `tool.browser.outcome_unknown`.
12. Reserve tenant/principal-scoped profile metadata before asking the isolated
    control plane to provision material, then bind its opaque reference with an
    expected generation. This prevents duplicate-id compensation from deleting
    an existing profile's material and makes failed provisioning recoverable
    without placing secret state in PostgreSQL.
13. Deploy the hosted profile control plane as a separate service identity and
    process. Only that identity may read the encryption key and profile-material
    volume. Its lifecycle API has provision, revoke, and delete operations but
    no material-export surface; scoped idempotency, authenticated encryption,
    atomic writes, fail-closed startup, and crash-recoverable key rotation are
    part of the adapter contract rather than deployment guidance.
14. Encrypt the first filesystem-backed profile envelope with AES-256-GCM from
    the maintained `cryptography` package. Bind the complete versioned scope as
    canonical additional authenticated data, use a fresh 96-bit nonce per
    write, and resolve exact 256-bit keys by opaque version from a service-local
    keyring. This direct dependency is preferred to a platform-authored cipher
    or unauthenticated encryption format.
15. Add a distinct hosted session/data-plane port with exclusive, expiring,
    run-attempt-scoped leases. Lease operations revalidate encrypted revocation
    state server-side; orchestration receives observations but never profile
    bytes, and revocation synchronously closes every live runtime.
16. Implement login as a five-minute, single-use direct browser ceremony whose
    launch capability is returned once and never persisted. The isolated
    runtime, not the caller, reports readiness or `needs_user`; the public API
    never proxies credentials, keystrokes, cookies, or browser protocol frames.
17. Represent standing browser authority as tenant-scoped durable metadata
    created through an authenticated platform surface. Trusted composition pins
    its id. The tool pipeline consults it only after deterministic policy asks
    for approval, revalidates it before each dispatch, and categorically refuses
    hard-excluded or unknown consequences.
18. Publish the isolated service only on host loopback and terminate its public
    HTTPS ceremony/control origin at the deployment reverse proxy. Give the
    container a database-isolated outbound network so its service-local audited
    exact-origin proxy can reach permitted sites; the browser itself remains
    bound to that proxy and to Playwright request interception.

## Consequences

- Browser automation stays inside the existing schema, policy, approval,
  persistence, and audit pipeline.
- A generic action is more approval-heavy than action-specific browser tools,
  but a misleading page cannot downgrade its own consequence.
- Persistent authentication depends on the separate profile, encryption, login,
  deletion, and grant surfaces; their authority remains narrower than ordinary
  tool approval.
- Future Milestone 11 scheduled operation cannot be considered complete merely because a browser
  can click; it also needs pinned authority, uncertainty recovery, and visible
  `needs_user` outcomes.
- Playwright is now a runtime dependency. The hosted production image installs
  Chromium during its reproducible build; local use of the ephemeral adapter
  still requires an explicit browser installation. The ephemeral adapter does
  not persist login state and is not sufficient for authenticated unattended
  rollout.
- PostgreSQL owns only queryable, principal-scoped profile metadata. A local
  encryption helper in the API or worker cannot qualify as the hosted profile
  implementation because it would give orchestration access to the key and
  material store.

## Alternatives considered

- **Arbitrary Playwright or JavaScript tool:** rejected because it combines
  network access, credential reachability, code execution, and external writes
  in an unreviewable argument.
- **Duolingo- or site-specific tool:** rejected because the requirement is a
  general browser capability and a site-specific schema would not transfer.
- **Expose the user's normal browser profile:** rejected because it contains
  unrelated sessions and weakens principal, site, and purpose confinement.
- **Treat clicks as read-only until a form submits:** rejected because browser
  events can cause writes without a form or reliable preflight signal.
- **Store cookies in PostgreSQL or the credential broker:** rejected because
  browser profiles contain large, provider-specific, rapidly changing secret
  state with different lifecycle and deletion requirements.
- **Require approval for every scheduled action forever:** safe but fails the
  unattended requirement. Narrow standing grants preserve an explicit human
  authorization object without blanket auto-approval.
