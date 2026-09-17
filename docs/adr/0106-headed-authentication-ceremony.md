# ADR-0106: The authentication ceremony runs a headed browser and never disguises it

- Status: Proposed; implements the owner's 2026-09-17 direction after a local acceptance test
- Date: 2026-09-17
- Related: ADR-0058; ADR-0098
- Design: [Browser automation](../plan/browser-automation.md)

## Context

The owner's Duolingo login failed in the isolated ceremony browser with the
website's "Wrong username or password" message although the credentials were
correct. Investigation on 2026-09-17 found:

- The website shows that message for every failed login request except one
  single-sign-on case, so it carries no information about the credentials.
- The login request carries a reCAPTCHA Enterprise abuse-classification token
  computed in the browser.
- The ceremony browser was the headless shell. It reports `HeadlessChrome` in
  its user agent and client hints, exposes no `window.chrome`, and lists no
  plugins. The runtime accepted an `interactive` flag and discarded it.
- A credential-free probe through the real hosted runtime and browser egress
  proxy reached the login form, produced the abuse token in 0.2 seconds, and
  saw no refused or failed request. The credential path from the ceremony
  surface to the page is byte-faithful. The defect was therefore not in
  resource loading, the proxy, or text entry, which earlier fixes addressed.
- The owner then logged in from a headed Playwright Chromium on a residential
  address: the login request returned 200 and the session cookie was set. That
  browser still reported `navigator.webdriver = true` and its real user agent.

A truthful headed browser is accepted; the headless one is not.

## Decision

An interactive authentication ceremony launches full, headed Chromium. Run-attempt
leases stay headless: the website attaches its abuse signal to login and signup
only, and an unattended lease has no user to look at a window.

On Linux the runtime starts one private Xvfb server per ceremony, lets the
server choose its display number, passes only that `DISPLAY` to the browser's
otherwise scrubbed environment, and stops the server when the runtime closes.
Other platforms use their native display. A display that cannot start fails the
launch as `tool.browser.provider_unavailable`. The runtime never falls back to
headless, because that fallback reproduces this defect as a misleading
credential error.

The runtime does not disguise the browser. It sets no user agent, removes no
default automation argument, and masks no automation indicator such as
`navigator.webdriver`. A website that refuses a truthful headed browser is
unsupported. This keeps the existing rule that CAPTCHA and verification belong
to the user: the design answers a website's abuse control with a real browser
operated by a real person, not with evasion.

## Consequences and validation

The browser-profile image names `xvfb` explicitly instead of relying on the
package list behind `playwright install --with-deps`. One display per ceremony
costs a small process for at most five minutes and keeps concurrent ceremonies
from sharing an X server, which has no isolation between its clients. The
server refuses TCP clients; its socket lives in the container's private `/tmp`
and network namespace, where every process already belongs to the service.
Input still arrives through the browser protocol, never through X events.

Headed Chromium uses more memory than the headless shell. The existing
one-gibibyte container limit and the one-ceremony-per-profile rule bound it.

Regression coverage demonstrates the headed launch with its private display,
display teardown on close, the headless run-attempt lease, refusal to fall back
when the display fails, the absence of identity overrides, the display server's
start, timeout, and shutdown behavior, and the image's display package. The
headed launch is also exercised in the built image under the production
container restrictions.

The owner's acceptance test ran from a residential address. Whether the website
also accepts the production host's datacenter address is only observable after
deployment; if it does not, the website is unsupported under this decision.

## Alternatives

- Overriding the user agent and hiding automation indicators would likely pass
  the website's check. It is evasion of an abuse control, contradicts the
  user-owned verification rule, and was rejected.
- Chromium's newer headless mode still reports `HeadlessChrome`.
- Importing a session the user created in their own browser would break the
  rule that no client supplies profile material, and was not needed.
- One shared display for the service would let concurrent ceremonies share an
  X server and would keep a process alive when no ceremony runs.
