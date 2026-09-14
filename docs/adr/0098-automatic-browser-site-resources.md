# ADR-0098: Load website resources without CDN configuration

- Status: Proposed; implements the owner's 2026-09-14 direction to remove manual CDN configuration
- Date: 2026-09-14
- Related: ADR-0058; engineering plan Section 33
- Design: [Browser automation](../plan/browser-automation.md)

## Context

An origin-only Duolingo profile opens an empty document because its required
JavaScript lives on a separate CDN. Requiring users to identify every script,
stylesheet, image, font, API, and embedded verification origin does not meet the
requested single-URL login experience. The owner explicitly requested automatic
loading instead of the Advanced settings workaround.

## Decision

Separate navigation authority from resource transport. Profile origins continue
to bound top-level navigation, observations, and agent actions. Public HTTPS
resources and embedded frames required by that page load automatically, during
both interactive authentication and later profile reuse. Resource destinations
are never added to a profile or standing grant, and cannot become top-level
navigation authority by having supplied an asset.

The browser uses a dedicated, process-local HTTPS CONNECT proxy. It validates
DNS hostnames, restricts connections to port 443, rejects plaintext HTTP and IP
literals, checks every resolved address for private/loopback/link-local/metadata
destinations, and dials the already checked address. Ordinary worker/sandbox
allowlists retain their existing behavior; their serialized policy cannot select
the browser transport. Browser cookies, CORS, TLS checks, service-worker denial,
popup closure, and the user-only credential channel remain in force.

Chromium's request-stage document interception checks every main-frame request,
including redirect hops, before network dispatch. A CDN URL requested as a
top-level document is still refused unless explicitly allowed for navigation.
Embedded public HTTPS frames may load verification UI; they confer no model
interaction authority beyond the existing root-page element contract.

The native form requires only the website URL and has no CDN-origin field.
Existing origin-only profiles receive this behavior without migration or
reconfiguration. Authentication remains user-controlled.

## Consequences and validation

This intentionally relaxes exact-origin restrictions for page resources while
retaining them for top-level documents. A website can make public HTTPS resource
requests under normal browser security rules, including its cross-origin APIs.
It cannot reach private networks or turn those destinations into navigation or
standing-grant permissions. No arbitrary-JavaScript or cookie API is introduced.

Regression coverage must demonstrate a site loading a distinct CDN, refusal of
top-level navigation and redirects even to a resource host, private-address and
plaintext refusal before dialing, unchanged sandbox policy, and the single-URL
native flow. An isolated anonymous Duolingo probe must reach the login form with
only its website origin configured. Full login still requires the user's
credentials and any website verification.

## Alternatives

- Hardcoded CDN lists do not generalize and become stale.
- Adding resource origins to profile origins silently expands navigation scope.
- Unrestricted proxy egress removes private-network protection.
- Asking users to debug dependency domains repeats the defect.
