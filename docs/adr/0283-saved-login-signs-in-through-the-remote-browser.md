# ADR-0283: A saved login can sign in again through the remote browser

- Status: Accepted by the owner on 2026-11-01
- Date: 2026-11-01
- Amends: ADR-0128 (the device window offers the remote browser only for a new
  website); `docs/plan/browser-automation.md` (remote ceremony evidence, Website
  Access)
- Related: ADR-0251, ADR-0280, ADR-0282
- Scope: The Apple client's sign-in window and the browser service's sign-in
  challenge check. No API, policy or scope change; no milestone or gate moves.
- Detailed design: `docs/plan/browser-automation.md`

## Context

On 2026-11-01 the owner signed in to Chase through the remote browser and Chase
accepted the sign-in. A new chat asking for the card's balance then opened the
device sign-in window. Production showed that Veetbot had never saved the session:
the login was still `authentication_required`, had never been used, and its only
ceremony had expired in that state. The window was the ADR-0251 offer's sign-in
for a login that was not ready, not a sign-out by Chase. Two defects combined.

1. **The remote ceremony could not see Chase's sign-in form.** Chase keeps its
   password field in a same-site frame, `iframe#logonbox`. The challenge check
   read only the top-level page, so text the owner typed was not evidence of a
   sign-in and the ceremony could never report `ready`. The verifying browser
   without a session would also have looked signed in, and a signed-out lease
   would not have paused for the owner.
2. **A saved login had no way out of the device window.** Sign in again, the
   ADR-0280 card and the ADR-0251 offer all open that window, and Chase redirects
   it to an unsupported-browser page (ADR-0282). The remote browser was offered
   only for a new website.

## Decision

1. A visible password or one-time-code field in a visible frame of the page is a
   sign-in challenge, wherever the check runs: the remote ceremony's evidence, the
   verifying browsers, and the lease's check before it acts. Only those fields
   count in a frame. CAPTCHA classes and challenge wording stay top-level, because
   inside an embedded widget they say nothing about the page. A frame that goes
   away or stalls past one second shows no field, so it cannot fail a typed key or
   a verification load.
2. The sign-in window offers **Use Veetbot's remote browser** for a saved login as
   well as a new website, on the same conditions. Choosing it ends the device
   attempt and begins a remote ceremony on that saved login, at the address the
   window started on, with the same recovery from an open ceremony.
3. Backing out keeps the saved login. Start over and a launch that cannot open end
   the ceremony only; removing a login is reserved for a new website's unused
   setup and the row's own trash button.
4. Only Website Access shows the launch link, so a remote sign-in started from a
   chat opens Website Access once the ceremony has begun. A finished sign-in
   connects the chat and carries an ADR-0251 offer as a device sign-in does.

## Consequences

- A remote ceremony advances the profile generation, as every sign-in already
  does, so it ends that login's grants.
- A signed-in page with a visible third-party frame holding a visible password
  field reads as signed out. That is unusual, and the failure is a request to
  sign in, not an unsafe action.
- Whether Chase serves the saved session to a later browser is not yet known. The
  verifying browsers decide before anything is sealed, and a rejected session stays
  `needs_user`. Chase's session lifetime decides how often the owner signs in again.

## Alternatives considered

- **A separate remote button on each login row.** It would not reach the chat's
  sign-in card or offer, and most websites do not need it on the row.
- **Identify the device window as Safari.** The owner allowed this only if Chase
  refused the remote browser. Chase accepted it, so the default user agent stays.
- **Scan frames for every kind of challenge.** More complete, but embedded widgets
  carry CAPTCHA classes and such wording on signed-in pages, which would pause
  working tasks.

## Validation

Real-Chromium cases cover a sign-in form and a one-time-code field in a frame, the
ceremony reaching `ready`, verification and the lease check seeing the form, and
invisible frames and fields not counting. Unit tests cover a detached and a stalled
frame. Apple tests cover the switch from each surface, the begin request, recovery
from an open ceremony, a failed begin, and that Start over, a failed launch and a
finished sign-in keep the login. A real Chase sign-in after deployment is the
remaining evidence, on the owner's testing page.
