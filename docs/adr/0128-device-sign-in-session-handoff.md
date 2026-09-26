# ADR-0128: The owner signs in on their own device and hands the session to the isolated service

- Status: Accepted by the owner 2026-09-25
- Date: 2026-09-25
- Related: ADR-0058, ADR-0098, ADR-0106, ADR-0127, ADR-0129 (relies on
  decision 10 to end task grants on a new sign-in)
- Amends: ADR-0106 (its rejected alternative "importing a session the user
  created in their own browser"); ADR-0058 decision 16 (adds a device variant
  of the direct login ceremony); `docs/plan/browser-automation.md` (trust
  model, profile generation, authentication ceremony, hard gate 9). ADR-0106
  and ADR-0058 each gain an `Amended by` header line naming this ADR.
- Detailed design: `docs/plan/browser-automation.md` (device sign-in ceremony),
  `docs/plan/http-api-and-streaming.md` (browser profiles, authentication, and
  grants)

## Context

Website Access keeps each owner-owned browser profile in the isolated
browser-profile service on `api.veetbot.com`, a datacenter host. Today the owner
signs in through a remote authentication ceremony: a headed, automation-flagged
Chromium runs on the server, and the owner drives it through a screenshot
surface (ADR-0106). The runtime does not disguise itself.

On 2026-09-25 the owner ran a probe, typing by hand:

- Headed Playwright Chromium (`navigator.webdriver = true`) signing in to
  Duolingo through the server's datacenter address got a `401` from the login
  request twice.
- The same browser on the owner's work network got `200`.
- The session from that sign-in, reused in headless automated Chromium through
  the datacenter address, loaded `/learn` signed in (thirteen user-API `200`s)
  and opened `/lesson` (session `POST`s returned `200`, exercise elements were
  present).

Duolingo refuses an automation-flagged sign-in from a datacenter address, but it
serves a session created elsewhere to the server. ADR-0106 forbids disguising
automation, so the remote ceremony cannot be fixed on the server side for this
site. ADR-0106 also rejected session import, saying it "would break the rule
that no client supplies profile material". That rule came from the threat model
of the orchestration processes: the worker and API must never carry profile
bytes. It was not a finding that the owner's own client is less trustworthy
than the screenshot surface, which already relays every password keystroke from
the owner's device to the isolated service.

## Decision

1. **A device sign-in ceremony sits beside the remote one.** The existing begin
   route, `POST /v1/browser-profiles/{id}/authentication-ceremonies`, takes an
   optional `mode`: `remote` (the default, unchanged) or `device`. Scope,
   admission lock, one-open-ceremony rule, five-minute expiry, login-URL
   validation and the public status and cancel routes are shared. No public
   route is added, no durable field changes, and no migration is needed. The
   begin response carries `Cache-Control: private, no-store` in both modes. In
   device mode the client sends the root of the confirmed page's origin as the
   login URL: it is an allowed origin by construction, and the signed-in page's
   path and query, which can carry tokens, never reach the API.

2. **The owner signs in inside the Veetbot app.** A "Sign in on this device"
   sheet in the Apple client, on macOS and iOS/iPadOS, opens the website in a
   `WKWebView` with a non-persistent data store and the platform's default user
   agent. It adds no user script, message handler or automation setting, so
   it reads nothing the owner types.
   Top-level navigation stays on the profile's allowed origins, and popups,
   downloads and other schemes are refused. The owner signs in normally and
   taps "I'm signed in". Only then does the client begin the device ceremony,
   so its capability lives for seconds. For a new website login, the client
   creates the profile at that moment too. The launch URL and its capability
   live only in local variables of that step, never in published or persisted
   client state. A begin whose answer is lost, or that finds an open ceremony,
   is recovered once: the client cancels the newest open ceremony of the
   profile and begins again.

3. **Only that site's session is handed over, and only to the isolated
   service.** The client reads the data store's cookies, keeps those whose
   domain is an allowed host or a parent of one, and reads `localStorage` for
   the allowed origins it visited, in an isolated script world. It sends them
   once, with the confirmed page's URL, to
   `POST /authentication/{ceremony_id}/handoff` on the ceremony origin. This is
   the direct surface channel the remote ceremony's keystrokes already use. The
   request carries the ceremony capability in `X-Browser-Ceremony-Capability`
   and never carries the Veetbot API credential. The client then clears the
   data store and keeps nothing: no cookie, storage value or capability is
   persisted, logged or placed in durable client state. The API, the worker and
   the model never see the session.

4. **The capability is random, single-use and handoff-only.** It is 256 random
   bits, returned once in the launch URL's fragment and held only as a keyed
   digest in service memory. It opens no frame and sends no event, and a remote
   capability cannot hand off. The first well-formed handoff consumes it,
   whatever the outcome. Cancel, expiry, profile revocation or deletion, and a
   service restart also end it. Remote capabilities also become random: today
   they derive from a per-process counter under a persistent secret, so a
   restarted service can issue the same capability again.

5. **The service validates and filters the payload without trusting the
   client.** The body is at most 1 MiB, with at most 300 cookies and 64 origins
   of at most 2000 storage items each. Cookies use exactly the Playwright
   storage-state fields, and every string is bounded. On the handoff path only,
   Nginx raises the virtual host's 64 KiB body limit to 1 MiB and streams the
   body to the service unbuffered, through an in-memory buffer that holds the
   whole bound and with ten seconds allowed between reads. The body therefore
   never reaches Nginx's temporary files, and the service refuses a missing or
   wrong capability before reading it. The service keeps a host-only cookie
   only when its domain equals an allowed host. It keeps a domain cookie only
   when its domain is an allowed host or a parent of one and is not a public
   suffix under the Public Suffix List, with private entries counted as public
   suffixes. It drops a cookie whose domain is an IP address or ends in an
   all-digit label. It keeps storage only for exactly allowed origins and drops
   expired cookies. The list comes from the maintained `publicsuffixlist`
   package, a new dependency used only by the isolated service. A malformed
   body is `400` and does not consume the capability.

6. **The service verifies before it seals, and only the service decides.** It
   starts two headless run-attempt runtimes through the audited egress proxy:
   one holds the filtered state and one holds nothing. Both load the confirmed
   page, within thirty seconds in total. The result is `ready` only when the
   site itself tells the two apart. With the session, the page stays on an
   allowed origin at the confirmed path (or below it) and shows no sign-in
   challenge. Without it, the page shows a challenge, moves to another path or
   leaves the allowed origins. This also proves the site serves the session to
   the service's own address. On `ready` the service seals the verifying
   runtime's storage state, as a healthy lease close does, and that replaces
   the previous material. Every other outcome seals nothing, keeps the last
   profile, and ends the ceremony `cancelled`. The handoff's response says why:
   `session_empty`, `session_signed_out`, `session_unconfirmed`,
   `tool.browser.provider_unavailable` or `tool.browser.profile_unavailable`.
   The client then reads the ceremony status through the public API, which
   moves the profile to `READY`.

7. **Nothing from the payload is ever logged.** No cookie or storage name or
   value, no confirmed URL and no capability reaches a log, error, event,
   metric or diagnostic, on the service or on the client. Responses carry fixed
   codes only. The handoff route and its boundary answer every failure
   themselves, so no exception from the handoff path reaches the server's error
   log. The web server re-raises any other unhandled exception to be logged
   with its message, traceback and chained causes, so the service's log
   configuration replaces that detail, on every logger including the web
   server's, with a fixed message and the exception's class name.

8. **No application-layer sealing (HPKE) beyond TLS.** The handoff makes one
   TLS hop to Nginx on the service's own host and then crosses host loopback to
   the container. The client's App Transport Security requires TLS 1.2 or later
   with forward secrecy, and every TLS server block on that Nginx allows only
   TLS 1.2 and 1.3. The remote ceremony already relays passwords over the same
   path, and a session is no more valuable than the password that mints it.
   HPKE would help only against an attacker who reads plaintext at Nginx or on
   loopback. That attacker is root on the service host and can already read the
   container's memory, keyring and live browsers. HPKE would add per-ceremony
   key handling on both sides and a new cryptographic dependency or a
   hand-composed construction, which the specification avoids in favour of
   maintained primitives.
   Apple's CryptoKit HPKE also needs iOS 17 and macOS 14, above the client's
   iOS 15 and macOS 12 floor. What protects the handoff instead: the header
   capability, `no-store`, no body logging, no body on disk, system TLS trust,
   an ephemeral `URLSession` that refuses redirects, and the service's own
   filter and verification.

9. **Remote ceremonies stop losing their outcome.** While a remote ceremony is
   open, the client refreshes it when the app becomes active and when Website
   Access appears. It also reconciles the latest open ceremony of any profile
   that is not ready. The service sweeps every fifteen seconds, closing expired
   runtimes without waiting for a request. It also checks each open remote
   ceremony once in its last twenty seconds and seals it only when two checks
   2.5 seconds apart both find the existing ready evidence. Terminal outcomes
   are kept for twenty-four hours instead of five minutes. The manual ready
   rule is unchanged.

10. **Every sign-in attempt changes the profile generation, and the lease
    contract is otherwise unchanged.** Beginning a ceremony in either mode
    advances the profile's generation in the unit of work that records the
    ceremony, without changing its status, so a failed begin changes nothing.
    A `ready` outcome advances it again when it is recorded, through status or
    cancel, on a profile that is already `READY`. Standing and task grants pin
    the generation, so no grant survives a sign-in, including one that replaces
    a ready profile's session with another account's. The begin step holds
    even when no client ever reads the outcome, as with a ceremony the
    service's sweep seals. An open or verifying device ceremony excludes a
    lease for its profile, as a remote ceremony does. Lease reuse and renewal
    are ADR-0127's.

11. **The isolated service can switch device sign-in off.** The service's
    `BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED` setting, `true` when unset, refuses
    device begins with `tool.browser.provider_unavailable` when `false`, so the
    operator can stop the new material path with a service restart instead of a
    code revert through `main`. Remote sign-in is unaffected. No orchestration
    flag is added: device mode is reachable only through an authenticated
    `browser.profile.write` begin, and older clients never ask for it.

## Consequences

- Duolingo and similar sites become usable: the sign-in happens from the
  owner's network in a normal browser engine, and the unattended lease uses the
  resulting session from the server. The browser tranche's security boundary
  moves in one place only: a trusted client, holding a single-use capability
  minted by an authenticated `browser.profile.write` call, may supply one
  site's session to the isolated service. Orchestration still never carries
  profile material, and the model still never sees it (hard gate 9 is extended,
  not relaxed).
- **What a compromised client or a stolen credential can do.** The capability
  is write-only: the handoff response carries fixed codes and never material.
  After the service's filter it can write only cookies and storage for the
  profile's allowed origins. The worst case is a session for a different
  account on the same site (session fixation) or planted `localStorage` for
  those origins. Decision 10 makes every sign-in end every grant on the
  profile, so the agent cannot keep acting, unreviewed, in an account someone
  else reads. The same credential can already delete profiles, create standing
  grants and resolve approvals, so no new cross-principal path exists.
- A standing grant must be created again after any sign-in. A remote sign-in
  whose status polling passed through `AUTHENTICATION_REQUIRED` already ended
  one; decision 10 makes that true of every sign-in.
- A session made by WebKit is replayed by Chromium from another address. Sites
  that bind sessions to a user agent, a device key or an IP address will fail
  verification with `session_signed_out`. That is the correct outcome, and the
  remote ceremony stays available.
- Sessions kept in IndexedDB, `sessionStorage` or device-bound credentials do
  not transfer. Passkeys and identity providers on other origins do not work in
  the sheet: a non-browser app's `WKWebView` gets WebAuthn only for its
  associated domains, and navigation is origin-confined. Password and
  one-time-code sign-ins work.
- Verification costs two short-lived headless browsers (about ten to thirty
  seconds) inside the existing one-gibibyte container limit. It runs outside
  the service-wide lock.
- A `session_unconfirmed` result asks the owner to confirm on a page only
  signed-in members see, for example Duolingo's `/learn`. That is a small
  friction traded for not sealing profiles that hold no session. The residual
  false-ready case (a page that differs signed out for unrelated reasons) has
  the consequence the remote ceremony already accepts: a run meets a
  signed-out page and the owner signs in again.
- One new runtime dependency (`publicsuffixlist`) is used only by the isolated
  service. Its bundled list can age; WebKit and Chromium enforce their own
  lists when cookies are set, so staleness can only admit a suffix newer than
  the snapshot.
- The ceremony host's Nginx gains one location, and every TLS server block in
  `nginx/veetbot.conf` gains a TLS 1.2 floor. Both reach production only
  through the `deploy-nginx` job after a merge to `main`.
- No public route and no versioned configuration knob is added. The public
  route count stays at thirty-one and the knob census at its `origin/dev` value
  (185 after ADR-0131). The service's switch is a service environment
  variable, validated at release like the ceremony origin.
- Delivery: the work lands on a development branch first. Production deploys
  only from `main`, and the `main` PR waits until the whole lesson path (ADRs
  0127 to 0130) is built and verified.

## Alternatives

- **Keep only the remote ceremony.** It is honest and site-independent, but
  Duolingo refuses it from the datacenter address (the probe). The owner's goal
  is unattainable with it alone.
- **Disguise the automation** (override the user agent, hide
  `navigator.webdriver`, add stealth patches). Likely to pass, but it evades a
  site's abuse control. ADR-0106 rejected it and this ADR keeps that rejection.
- **Non-datacenter egress for the ceremony** (a residential or office proxy
  service, or tunnelling the ceremony's traffic through the owner's device).
  It keeps the automation-flagged browser, so it only moves the refusal
  signal, and it may still be refused. A commercial proxy adds a third party
  that sees the owner's password traffic, plus a recurring cost. Tunnelling
  through the device is far more machinery than a web view, for the same
  trust.
- **Import from the owner's everyday browser** (Safari or Chrome cookie
  export). It exposes unrelated sessions and needs platform permissions.
  ADR-0058 already rejected using the user's ordinary profile. The sheet's
  store is empty, dedicated and discarded.
- **A device-local browser provider** that runs lessons on the owner's device
  through the reserved `device.*` seam. It fits ADR-0058's placement model,
  but unattended runs then need the device awake and online, and the provider
  is unbuilt.
- **Seal the payload with HPKE** to a per-ceremony service key. Not adopted,
  for the reasons in decision 8.
- **Seal the raw payload rather than the verifying runtime's state.** Rejected
  because a site may rotate its session on the first visit. The verified
  runtime's own state is what a lease close would seal, and it keeps the
  profile format Playwright-native.
- **Change the generation only when the ready outcome is recorded.** Rejected:
  a ceremony that the service's sweep seals reaches orchestration only when a
  client reads its status, so the very client being defended against could
  skip the change. Decision 10 changes it at begin as well.

## Validation

Automated verification, all red first:

- Isolated-service boundary tests for the handoff route: happy path, every
  validation bound, scope filtering (including a public-suffix domain, a
  private-suffix domain, an IP-literal domain, a sibling host and a foreign
  origin), authorization (missing, wrong, consumed, remote-mode and expired
  capabilities, checked before the body is buffered), single use under
  concurrency, retry after a lost response, each failure outcome writing
  nothing and keeping the prior material, verification timeout, revocation
  during verification, lease exclusion, and the kill switch.
- Log safety: sentinel cookie names and values, storage names and values, the
  capability and the confirmed path are absent from every log record on every
  handoff path, including records emitted by uvicorn while the real
  application runs under an in-process `uvicorn.Server` with the service's log
  configuration and an exception whose message and cause carry the sentinels.
  A test with `httpx.ASGITransport(raise_app_exceptions=True)` shows that no
  exception leaves the application on any handoff path.
- Nginx: a block-scoped configuration test shows that the handoff location
  allows `1m`, holds a `1m` in-memory buffer, streams unbuffered, allows ten
  seconds between reads and sixty for the answer, that the virtual host keeps
  `64k`, and that every TLS server block allows only TLS 1.2 and 1.3. The
  deployment job's existing `nginx -t` rejects a malformed configuration and
  restores the previous one.
- Orchestration contract tests: `mode` on the public begin route, the default
  remote behaviour unchanged, invalid modes rejected, `no-store` on the begin
  response, authorization for both modes (`403` without
  `browser.profile.write`, `404` for another principal's profile), retry (a
  new begin is `409` after a rejected handoff until the ceremony is cancelled,
  and a lost begin followed by list, cancel and begin succeeds), the launch
  capability absent from status and list, and the hosted client still exposing
  no material surface.
- Generation: in memory and in PostgreSQL, beginning a ceremony in either mode
  on a `READY` profile advances the generation once without changing status, a
  failed begin leaves it unchanged, and recording a `ready` outcome through
  status or cancel advances it once more. A standing grant created before the
  begin no longer authorizes.
- Hard gate 9 (`tests/gates/test_browser_m10.py::test_authentication_boundary`)
  aggregates the device-ceremony contract beside the remote one.
- Remote outcome-loss tests: the sweep seals on two ready samples and not on
  one, expired runtimes close without traffic, and terminal outcomes survive
  for twenty-four hours.
- A real-browser local integration test: the isolated service with its real
  Playwright runtime (Chromium) against a local HTTPS test site that serves a
  login form and sets a session cookie. A session obtained from that site's
  login verifies and seals, the sealed profile loads the members-only page
  signed in through a lease, and a handoff without the session or confirmed
  on the public page is refused with its code. It runs wherever Chromium is
  installed and must be reported as run, not skipped.
- Apple unit tests: the web-view configuration (non-persistent store, no
  scripts, handlers or user agent), the navigation policy, the cookie and
  storage scope filter and mapping (lowercased domains, float expiry), the
  handoff client (capability header present, API credential and cookies
  absent, redirects refused, outcome mapping), the begin body's login URL on
  an adopted `www` origin, lost-begin and open-ceremony recovery, the launch
  URL never reaching published state, and the view-model flows for success,
  each failure, retry, abandonment and connection change. The existing UI
  lanes still pass.

Delivery and owner acceptance: production deploys only from `main`. The owner
chose to build on a development branch now and to open a `main` PR later, once
the whole lesson path is built and verified, so nothing in this ADR is accepted
in production before that PR. Agents verify with the automated tests above,
including the real-browser local test. After the `main` PR merges, its
`deploy-app` and `deploy-nginx` jobs succeed on the merge commit and the macOS
TestFlight build ships, the owner accepts in production:

- **V1:** on the Mac, sign in to Duolingo in the sheet from the owner's own
  network, confirm on `/learn`, see the profile `READY`, bind a chat, and open
  `/learn` and a lesson through the lease.
- **V2:** the same on an iPhone, once the iOS TestFlight build ships.
- **V3:** confirming on the signed-out home page produces
  `session_unconfirmed` and no `READY`.
- **V4:** the browser-profile service's container log from the acceptance run
  contains no cookie name from the payload.
