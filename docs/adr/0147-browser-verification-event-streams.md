# ADR-0147: Established event streams do not hold sign-in verification open

- Status: Accepted by the owner, 2026-10-02
- Date: 2026-10-02
- Related: ADR-0128, ADR-0145
- Amends: the device verification request-settling mechanism in
  `docs/plan/browser-automation.md`

## Evidence

After the headed-browser release, the owner's x.com device handoff still
returned a provider failure at the thirty-second deadline. A controlled retry
used the native client's cookie mapping and a non-persistent WebKit view, then
loaded the confirmed page with and without its session in the production
hosted runtime. The signed-out control left the confirmed path. The signed-in
load reached its document, but one fetch remained open through the deadline:
a successful HTTP 200 response with `Content-Type: text/event-stream`.
Only timing, request types and fixed verification outcomes were inspected;
no session material or page content is retained in this evidence.

The request tracker waits for
[Playwright’s `requestfinished`](https://playwright.dev/python/docs/api/class-request),
which for an event stream means the live connection closes. That connection is designed to stay open.
The existing mechanism already excludes native EventSource and WebSocket
connections, but an equivalent stream opened with fetch or XHR blocks it.

## Decision

A tracked fetch or XHR response with HTTP 200 and media type exactly
`text/event-stream` is established when its response headers arrive. Remove
that request from the finite-request wait at that point and begin the same
500 ms quiet interval. Match the media type case-insensitively and ignore its
optional parameters, matching the
[HTML event-stream connection rules](https://html.spec.whatwg.org/multipage/server-sent-events.html#processing-model).
A stream without response headers, a non-200 response,
a different media type, or a document, script or stylesheet still waits for
completion. No hostname, URL path or website-specific selector is involved.

All ordinary application requests, including delayed JSON session checks,
still finish before positive evidence is collected. Both browsers still load
the confirmed page, and readiness still requires the signed-in page to stay
there without a challenge while the signed-out control behaves differently.
The thirty-second deadline, secret boundary, origin confinement, challenge
checks, profile sealing and revocation checks remain in force.

This is an explicit exception to the design's rule that every fetch and XHR
must finish. As with an existing WebSocket or EventSource connection, a site
may deliver a later authentication change over the established stream. The
verification is a check at the time of capture, not a guarantee that a site
will never revoke a session afterward. It must still refuse a challenge
already visible at capture.

## Verification evidence

- The real-Chromium regression in
  `tests/unit/test_device_handoff_real_chromium.py` reproduced the failure:
  expected HTTP 200 but received HTTP 409 at the verification deadline.
  With the repair it verifies and seals while the HTTP 200 fetch stream
  remains open.
- `tests/unit/test_browser_playwright.py` checks fetch and XHR, media-type
  case and parameters, status and resource-type exclusions, and listener
  cleanup on success and cancellation. All four new positive cases timed
  out before the repair and pass afterward.
- The real-Chromium delayed-session tests now run alongside an open event
  stream. The ready, challenge, redirect, timeout and retry outcomes pass;
  the stream never excuses the separate unfinished JSON check.
- The owner repeated the controlled handoff using the repaired runtime in
  an isolated diagnostic process on the production host. The signed-in page
  stayed at the confirmed path without a challenge, the signed-out control
  left it, and storage capture completed in 17.6 seconds while the stream
  remained open. This verified the repair without changing the running
  service or saving the diagnostic session as a profile.

## Alternatives

Increasing the deadline cannot finish a healthy live stream. Ignoring all
fetches, all cross-origin traffic, or a hardcoded x.com endpoint would admit
unfinished session checks or make the generic provider depend on one site.
