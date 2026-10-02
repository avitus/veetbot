# ADR-0138: Deny browser popups before creation

- Status: Proposed in the task-approval repair
- Date: 2026-09-28
- Related: ADR-0058, ADR-0106, ADR-0128, ADR-0129
- Amends: no requirement; implements the existing prohibition on new windows
- Amended by: ADR-0145 (2026-10-01), which makes every hosted browser headed,
  so run-attempt leases and device sign-in verification use the headed policy
  and transport below
- Detailed design: `docs/plan/browser-automation.md`

## Context

Closing a popup with Playwright marks it as closing before Chromium destroys
it. Playwright then skips context interception, allowing a form in an initial
blank popup to submit during closure. Closing the Chromium target directly
preserves interception, but concurrent-popup regression tests still observed
a request reaching the server. An asynchronous close is insufficient to deny
new windows before they can act.

## Decision

Headless run-attempt browsers use Chromium headless shell's
`--block-new-web-contents` switch. Web-created windows are denied before
creation, including a blank window that could later submit a form.

Full headed Chromium has no equivalent switch. In the remote authentication
ceremony, the runtime adds a CSP sandbox policy to document responses. It
permits the existing sign-in capabilities, including scripts, forms,
same-origin storage and navigation, but omits popup permissions. The site's
original CSP remains alongside the additional policy; neither can relax the
other.

The headed runtime uses Playwright's context request transport through the
existing audited proxy to fetch each document and fulfill the browser request
with its unchanged body and augmented headers. It preserves Chromium's request
headers, including an empty cookie selection, so the HTTP client's cookie jar
cannot add cookies Chromium excluded. Automatic retries and redirect following
are disabled. Every redirect returns to the browser's existing navigation
guards, and a failed mutating request is never automatically replayed.

Unexpected pages retain the Chromium target closure guard. It waits for the
page's close event before detaching. If attachment, target discovery,
destruction or close confirmation fails or is cancelled while the popup is
still alive, it closes the entire browser session.

## Consequences and validation

The headed transport uses Playwright's HTTP client rather than Chromium's TLS
stack for documents. A site that requires a browser TLS fingerprint may reject
the remote ceremony. The sandbox also prevents legacy `document.domain`
relaxation. Device sign-in uses the owner's native web view and is unaffected;
the imported session is verified and used in headless browsers.

Regression tests cover protocol errors and cancellation at each closure stage,
GET and POST submissions from blank popups, and concurrent popup attempts in
both browser modes with and without a task grant. A real headed sign-in test
preserves two HttpOnly cookies, local storage, the site's image restriction,
one form POST and its redirect; an off-origin redirect never reaches the proxy.
Unit tests verify response-header preservation, Chromium's cookie selection
and failure without retry. These synthetic tests establish transport behavior,
not acceptance by any particular live website.

No approval authority, task-grant duration, action budget, origin allowlist,
path boundary or sensitive-action rule changes.

## Alternatives

- Closing through Playwright or the target protocol alone fails the concurrent
  regression. A fixed delay cannot guarantee prevention.
- A page script that replaces `window.open` is controlled by the page and
  cannot enforce the boundary.
- Removing the popup prohibition would weaken an existing requirement.
- Disabling the remote ceremony would remove an existing sign-in option.
