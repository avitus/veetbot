# ADR-0146: Hosted Chromium makes no vendor requests of its own

- Status: Proposed; implements the owner's 2026-10-01 direction
- Date: 2026-10-01
- Related: ADR-0058, ADR-0106, ADR-0138
- Amends: ADR-0145 (the vendor requests it recorded and left on are turned off)
- Detailed design: `docs/plan/browser-automation.md`

## Evidence

ADR-0145 recorded that full Chromium contacts its vendor's services on its
own and left that on. Measured on 2026-10-01 in the built service image under
the production container limits, with Playwright 1.62.0 and Chromium
151.0.7922.34, headed on the runtime's display, through the hosted runtime.
The proxy was a CONNECT relay that served a synthetic HTTPS website and
recorded every other request. Each run loaded a page with a sign-in form and
an address form, typed into three fields, submitted, loaded the page again and
stayed idle. Chromium's network log named the URL behind each connection.

The browser made these requests that no page asked for. Counts are for one
browser over 7.4 minutes with a relay that, like the production proxy, tunnels
any public host on port 443:

| Purpose | Request | Before | After |
| --- | --- | --- | --- |
| Network time | `http://clients2.google.com/time/1/current`, which the proxy refuses as plain HTTP | 1 | 0 |
| Google accounts in the cookie jar | `accounts.google.com`, `POST /ListAccounts` | 1 | 1 |
| AI Mode eligibility, and an idle connection to the search engine | `www.google.com`, `GET /async/folae` | 3 connections | 0 |
| Autofill field types | `content-autofill.googleapis.com`, `GET /v1/pages/` followed by an encoding of the form and field signatures on the page | 1 | 0 |
| Push-messaging check-in and registration | `android.clients.google.com`, `/checkin` then `/c2dm/register3` | 1 connection | 0 |
| Push-messaging channel | `mtalk.google.com:5228`, refused for its port, then `mtalk.google.com:443`, held open | 2 | 0 |
| Spelling dictionary, once a text field takes focus | `redirector.gvt1.com` and the `gvt1.com` host it redirects to, `/edgedl/chrome/dict/en-us-10-1.bdic` | 2 | 0 |

- The image built for x86_64, which production runs, and the image built for
  arm64 gave the same requests and counts, before and after. The x86_64 runs
  were emulated on Apple silicon.
- With a relay that refuses everything but the test website, which is how
  ADR-0145 measured, the browser retries: in two minutes it asked twenty times
  for the autofill host, seven or eight for `www.google.com`, six for
  `accounts.google.com` and three for the check-in. After the change it asked
  six times for `accounts.google.com` and for nothing else.
- Nothing bypasses the proxy. In the runs with the refusing relay, on a
  network with no route out, the container sent no packet outside its
  loopback interface, before or after the change; a packet capture in its
  network namespace saw only the kernel's ARP announcement.
- ADR-0145's list lacked the dictionary, which needs a focused text field, and
  the messaging channel, which follows a check-in that succeeds. A container
  with no network interface hides the account request, because Chromium holds
  its sign-in calls while it is offline.
- The account request that remains is a `POST` with a one-byte body. Its
  captured headers hold the user agent and `accept-language` and no cookie. It
  comes from the browser's own default profile, which is created empty at each
  launch; pages and the profile's session live in a separate, non-persistent
  context.
- Playwright 1.62 appends the runtime's launch arguments after its own, which
  include a `--disable-features` switch naming sixteen features. Chromium
  honours only the last such switch: given a second one naming two features,
  `chrome://version` listed those two as disabled and none of Playwright's, in
  both builds.
- Playwright installs Google Chrome for Testing on x86_64 and its own Chromium
  build on arm64. They read managed policy from
  `/etc/opt/chrome_for_testing/policies` and `/etc/chromium/policies`.
- Chromium's source for this version shows no switch for three of the
  requests. The account request is issued whenever a browser component asks
  which Google accounts are in the cookie jar, and nothing gates it but the
  browser being offline; the `BrowserSignin` policy set to disabled did not
  stop it. The push-messaging client starts for every profile: its
  registrations in the measured browser were for policy invalidation, which
  Chromium sets up unconditionally. The dictionary loads for each spelling
  language whether or not spell checking is enabled; only the language
  blocklist policy removes it, and Chromium ignores that policy when the
  `SpellcheckEnabled` policy is false.

## Decision

1. A headed launch passes one `--disable-features` switch that repeats
   Playwright's list and adds `AimEnabled`, `AutofillServerCommunication`,
   `NetworkTimeServiceQuerying` and `PreconnectToSearch`. A test compares the
   repeated list with the installed Playwright driver.
2. A headed launch passes `--gcm-checkin-url=about:blank`. The push-messaging
   client registers and connects only after a check-in, and it cannot fetch
   that address. A hosted page cannot use Web Push in any case: its context is
   non-persistent and service workers are blocked.
3. The service image carries a managed policy,
   `SpellcheckLanguageBlocklist` naming `en-US`, in both policy directories.
4. The account request stays. It has no switch, and the egress proxy does not
   refuse a host that websites load for sign-in.
5. Nothing else about the browser changes. The runtime still sets no user
   agent, removes no default argument and masks no automation indicator
   (ADR-0106, ADR-0145), and none of these settings changes what a website can
   observe. The ephemeral adapter's headless launch is unchanged; the headless
   shell makes none of these requests.

## Consequences

- A hosted browser asks the egress proxy for one thing on its own: a
  connection to `accounts.google.com` as it starts. The autofill request, the
  only one derived from a page, is gone, and so is the long-lived messaging
  connection.
- The feature names are Chromium's internals and the repeated list is
  Playwright's. Either can change when the Playwright version is raised. The
  launch test fails if Playwright's list gains a feature the runtime's lacks.
  The real-browser test fails if a request returns, but only on a machine with
  Chromium installed, which CI is not. Hosted CI repeats this measurement in
  the built image on both architectures whenever the image, its limits, the
  Playwright version or these switches change (amended by ADR-0152).
- The launch switches apply wherever the runtime starts a headed browser. The
  policy applies only in the image. A development Mac does not need it,
  because Chromium uses the system spell checker there; a headed browser on
  Linux outside the image still downloads the dictionary.
- The policy names one language because the image runs Chromium in `en-US`. A
  browser started in another locale would load another dictionary.
- The push-messaging client keeps retrying its check-in on a timer inside the
  browser process. No retry reaches the proxy.
- If Playwright changes which Chromium build it installs for an architecture,
  the policy directory may change with it and the dictionary download would
  return. ADR-0152's image check fails when it does.

## Alternatives rejected

- **A second `--disable-features` naming only the new features.** It replaces
  Playwright's list, which re-enables, among others, HTTPS upgrades and
  third-party storage partitioning.
- **Removing Playwright's switch with `ignore_default_args`.** It needs the
  same copy of Playwright's list and removes a default argument, which
  ADR-0106 rules out.
- **Managed policy for every request it can express.** Policy stopped the
  time, search and autofill requests in a trial. It applies only in the image,
  so the local real-browser tests could not observe it, its directory depends
  on the build, and it has no setting for push messaging or the account
  request. It is used for the dictionary alone.
- **Refusing the vendor's hosts at the egress proxy.** The browser would keep
  asking, and `www.google.com` and `accounts.google.com` serve reCAPTCHA and
  Google sign-in to websites.
- **Pointing Chromium's account service elsewhere to stop the account
  request.** It changes how the browser treats Google's sign-in pages, which
  websites embed.
- **Disabling the components that ask for the account list.** There are
  several, and some are visible to websites, such as passkey support.

## Verification

- `tests/unit/test_browser_playwright.py`: a headed launch carries the four
  features and the check-in address in a single switch that keeps every
  feature Playwright disables; a headless launch adds neither.
- `tests/unit/test_browser_runtime_real_chromium.py`: a headed browser on a
  page with a sign-in form asks the relay for nothing but the page. Before the
  change it asked for the time, `www.google.com`, the autofill host and the
  check-in.
- `tests/real_browser_support.py`: the allowance for the browser's own
  requests is the account request, and on Linux the dictionary; it was every
  host under `google.com` and `googleapis.com`.
- `tests/unit/test_toolchain.py`: the image carries the policy in both
  directories.
- The built image under the production limits, on both architectures: the
  table above.
