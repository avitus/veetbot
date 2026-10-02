# ADR-0145: Hosted browsers run headed for agent runs and sign-in verification

- Status: Accepted (authorized by the repository owner, 2026-10-01)
- Date: 2026-10-01
- Related: ADR-0058, ADR-0098, ADR-0127, ADR-0129
- Amends: ADR-0106 (run-attempt leases no longer stay headless), ADR-0128
  (the two verification browsers are headed), ADR-0138 (its headed popup
  policy and document transport apply to every hosted browser)
- Detailed design: `docs/plan/browser-automation.md`

## Evidence

On 2026-10-01 the owner signed in to x.com on his device twice. Both handoffs
ended `409 tool.browser.provider_unavailable` thirty seconds after the
ceremony began. Measurements that day, none of which used a credential:

- x.com answers a request whose user agent contains `HeadlessChrome` with
  `403` and an empty body, on every page tried, from the production host and
  from a residential address alike. An ordinary Chrome user agent gets `307`.
- Chromium adds that token whenever it runs with its headless switch.
  Playwright's headless shell and full Chromium in new headless mode both
  report it, and both were refused.
- The same Playwright-driven full Chromium run headed was served the real
  page, `307` then `200` with the sign-in form. It had no user-agent override
  and reported `navigator.webdriver` as true. The hosted runtime's own headed
  mode, with ADR-0138's document transport and the browser egress proxy, was
  served the same way, by x.com and by duolingo.com.
- The production verification composition, run locally against x.com with no
  session, finishes both loads in about one second: each gets the blank `403`
  page on the confirmed path, which is `session_unconfirmed`. The
  thirty-second production outcome was not reproduced without a signed-in
  session and is not explained.

ADR-0106 kept run-attempt leases headless on the premise that a website
attaches its abuse signal to login and signup only. That premise is wrong for
a website that refuses the headless token on every page, and ADR-0128's
verification inherited it: its signed-out control and its signed-in load both
ran in the browser the website refuses.

## Decision

1. Every browser the hosted profile service starts is full, headed Chromium:
   the remote ceremony as before, each run-attempt lease, and both device
   verification loads. On Linux each owns a private Xvfb display as ADR-0106
   describes. A display that cannot start fails the start as
   `tool.browser.provider_unavailable`; nothing falls back to headless.
2. The browser is still not disguised. The runtime sets no user agent, removes
   no default automation argument, and masks no automation indicator. A
   website that refuses a truthful headed browser is not worked around.
3. Every hosted browser denies popups the way ADR-0138 gave the headed
   ceremony: the CSP sandbox policy on document responses, the document
   transport through the browser context's request client, and the closure
   guard. Headless shell's `--block-new-web-contents` switch remains only in
   the ephemeral adapter, which stays headless.
4. The production container allows 2 GiB and 1,024 processes, from 1 GiB and
   256.
5. The runtime's launch flag is named `headed`, since a lease is headed
   without being interactive.

## Consequences

- Memory and processes, measured in the built service image under the former
  limits with x.com's signed-out page loaded: one headed browser used about
  610 MiB (peak 670 MiB) and 181 processes and threads; the headless shell on
  the blank refusal page used about 200 MiB and 78. Two headed browsers, which
  one verification starts, failed to launch under the 256-process limit and
  used about 1.1 GiB and 370 processes and threads once it was raised. A
  signed-in page will use more. The service has no cap on how many profiles
  hold a lease at once, so the container limit is the bound.
- The production host had 4 GiB with about 1.3 GiB available on 2026-10-01. It
  needs more memory before this release is deployed, or a verification or two
  concurrent runs can exhaust the host.
- ADR-0138's transport limit now covers runs and verification as well: a
  website that requires the browser's own TLS fingerprint for documents may
  refuse them. The sandbox still prevents legacy `document.domain` relaxation.
- Full Chromium contacts its vendor's services on its own, which the headless
  shell does not. Measured in the service image: `accounts.google.com`,
  `android.clients.google.com`, `content-autofill.googleapis.com` and
  `www.google.com` over HTTPS, and `clients2.google.com` over plain HTTP,
  which the egress proxy refuses. The remote ceremony already made these
  requests; now every run does. They carry no website cookie or page text; the
  autofill query is derived from the structure of forms the browser sees.
  Turning them off is not part of this decision.
- On a development Mac a hosted browser opens a visible window, as the remote
  ceremony already did.
- This decision lets the runtime load a website that refuses headless
  browsers. It does not decide which websites the owner uses. X's developer
  guidelines name browser automation as grounds for suspension, which the
  owner was told on 2026-10-01.
- Not yet observed: whether x.com serves a signed-in session to the headed
  browser from the production address, and whether its signed-in page settles
  within the verification deadline.

## Alternatives rejected

- **Overriding the user agent or hiding automation.** Evasion of an abuse
  control, rejected by ADR-0106 and again here.
- **Chromium's new headless mode.** It reports `HeadlessChrome` and was
  refused.
- **Treating the website as unsupported.** The owner rejected it on
  2026-10-01: the headed browser the service already runs is served.
- **Headed only for websites that refuse headless.** The profile contract
  names origins and nothing about a website's behavior, and a control load in
  a different browser build is not a control.
- **Verification loads one after the other, to fit 1 GiB.** It doubles the
  wall time against a thirty-second deadline that successful production
  verifications already spend about twenty-eight seconds of.

## Verification

- `tests/unit/test_device_handoff_real_chromium.py`: a synthetic website that
  answers a `HeadlessChrome` user agent with an empty `403` confirms a
  handed-off session and serves a lease. Before the change the handoff was
  `422 session_unconfirmed` and the lease observed an empty page.
- `tests/contract/test_hosted_profile_session_service_contract.py`: a lease
  and both verification loads start headed.
- `tests/unit/test_toolchain.py`: the production container limits.
- The built service image under the new limits: two concurrent headed
  browsers load x.com's signed-out page through the hosted runtime and the
  browser egress proxy.
