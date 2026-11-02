# ADR-0282: The device sign-in window asks before a new website adds another address

- Status: Proposed
- Date: 2026-11-01
- Amends: ADR-0128 decision 2 (top-level navigation in the sign-in window);
  `docs/plan/browser-automation.md` (the device sign-in window)
- Related: ADR-0098, ADR-0106
- Scope: The Apple client's device sign-in window. No API, service or policy
  change; no milestone or gate moves.
- Detailed design: `docs/plan/browser-automation.md`

## Context

On 2026-11-01 the owner could not add their Chase credit card to Website Access.
Entering `chase.com` reported that `secure.chase.com` could not be opened, and
entering `secure.chase.com` reported that `www.chase.com` could not be opened.
The window was replayed against the live site in a real `WKWebView`, without
signing in, and showed two separate causes:

1. **One website, several addresses.** `chase.com` redirects to `www.chase.com`,
   which the window adopts as the new profile's one origin (D11). The Sign in
   link on that page opens `secure.chase.com`, a different exact origin, so the
   window refuses it. Nothing the owner can type gives a new profile two
   origins. Many websites keep their sign-in on a `login.`, `accounts.` or
   `secure.` host, so this is not particular to Chase.
2. **Chase refuses the web view's identity.** A `WKWebView`'s default user agent
   has no `Version/… Safari/…` tokens. Chase's sign-in page treats that as an
   unsupported browser and redirects to
   `www.chase.com/digital/resources/browser-upgrade`, which is the refusal seen
   when starting from `secure.chase.com`. With those tokens added, the same page
   stays on `secure.chase.com` and shows its sign-in form.

This ADR decides the first cause only. The second is the open question below.

## Decision

1. Before a new profile exists, a top-level move to another host of the same
   website is held back, and the window names that host and offers to allow it.
   Nothing loads until the owner chooses. A page cannot add an origin by itself.
2. "The same website" means the same registrable domain as an origin the window
   already allows, on the default HTTPS port. The domain is approximated as the
   ADR-0251 website offer does, because the public suffix list stays inside the
   isolated service. A wrong match only asks the owner, who sees the host; the
   service's public-suffix cookie filter still applies.
3. When the owner allows it, the window loads the page it held back. The new
   profile is created with every allowed origin, each still exact. Allowing an
   address ends first-load `www` adoption, which would replace the list.
4. If an earlier "I'm signed in" attempt in the same window already created the
   profile, and the owner then allows another address, that unused profile is
   removed and a new one is created with the whole list, because a profile's
   origins never change.
5. Any other host stays refused with today's message. So does every host when
   signing in again to an existing profile. The window stops asking at the
   server's limit of 64 origins.

## Consequences

- Hard gate 3 keeps its meaning: every navigation stays inside an exact origin
  list, and the owner, not a URL, puts each origin on it.
- The session handed over may now include host-only cookies and `localStorage`
  of the added addresses, which the service filters as it does any allowed
  origin. Domain cookies such as `.chase.com` were already in scope.
- On a shared-hosting suffix absent from the approximation, such as
  `github.io`, one tenant's page can ask for another tenant's host. The owner
  sees the host and must allow it, and the service still drops cookies set on
  the suffix itself.
- A saved login that needs a further address has to be added again; existing
  profiles do not grow.
- The remote browser is unchanged. Choosing it from the window starts over from
  the typed address with one origin.
- This does not let the owner sign in to Chase in this window; see below.

## Chase's browser check

The sign-in window keeps the platform's default user agent, as ADR-0128 decision
2, the design and a unit test require. On 2026-11-01 the owner decided to try the
remote ceremony first and to allow `Version/… Safari/…` in the window only if
Chase refused it. The owner then signed in to Chase through the remote ceremony
from `secure.chase.com` and Chase accepted, so the tokens were not added.

Chase therefore cannot be signed in through this window: a real sign-in there
ends on Chase's System Requirements page. The remote ceremony is the path for
Chase and needs no rule change. ADR-0283 repairs the two defects that stopped that
sign-in being saved and offers the remote ceremony to a saved login from this
window.

## Alternatives considered

- **Adopt every same-site host automatically.** Fewer taps, but a URL would
  authorize its own origin, which the design forbids.
- **Offer to allow any host.** One tap on an identity provider would hand that
  provider's session to the profile. Hosts of other sites stay refused.
- **An extra-origins field on the form.** The owner cannot know a website's
  hosts in advance, and ADR-0098 removed the form's extra-origin field.

## Validation

Apple unit tests cover the navigation policy (same-site, lookalike, country
suffix, IP address, port, scheme, existing profile, origin limit), the window's
hold, allow and retry behaviour with a stubbed navigation, and the view model's
profile creation and replacement. A throwaway test drove the real window against
the live Chase site with no credentials and showed the offer and the allow, then
the browser-upgrade redirect of the second cause.
