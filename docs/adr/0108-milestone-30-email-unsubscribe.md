# ADR-0108: Milestone 30 email unsubscribe assistance

- Status: Accepted — the owner requested the capability, decided its surfaces, consent model, server-side egress, and fallbacks, and directed implementation on 2026-09-19
- Date: 2026-09-19
- Related: ADR-0017, ADR-0040, ADR-0054, ADR-0071, ADR-0085, ADR-0092, ADR-0095, ADR-0096, ADR-0098, ADR-0099, ADR-0104
- Design: [Email unsubscribe assistance](../plan/email-unsubscribe.md)

## Context

On 2026-09-19 the owner said that unsubscribing from junk mail takes a lot of
their time and asked that Veetbot help. Nothing in the corpus designed it.
Milestone 18 gave the agent Gmail read, draft, label, trash, and
approval-gated send; Milestone 26 added Email mode, the priority inbox, and an
owner-requested Archive gesture, and explicitly excluded automatic mailbox
actions. Neither reads the headers that say how to leave a list, and neither
can act on them.

The mechanism already exists in the mail. RFC 2369 defines `List-Unsubscribe`,
and RFC 8058 defines the one-click form: an HTTPS POST of one constant body to
the header's address, with no cookie, no authorization, and no redirect. Gmail
has required it of large senders since February 2024. RFC 8058 also states the
two conditions this design turns into gates: the headers must be covered by a
valid DKIM signature, and the receiver must not send the request without the
user's consent.

One thing about the platform makes this an architectural decision rather than
a feature. Nothing in it dials an arbitrary public host. `web.fetch` hands its
URL to a hosted provider, the Gmail package is gated to two Google endpoints,
and the browser reaches only profile-allowlisted origins. An unsubscribe
request goes to whatever host a sender named. Deciding who sends it, from
where, and under which checks is decision 5.

The owner answered four shaping questions in chat on 2026-09-19: all three
surfaces; a tap as the consent; a server-side request through the egress
proxy; and Report spam, `mailto:` unsubscribe, and sender cleanup as the
fallbacks, with browser-driven web forms deferred.

## Decisions

1. **Milestone 30 is a parallel workstream with twenty gates in the `email`
   area.** It proceeds on the terms every parallel workstream has: its gates
   may turn green independently and the verified ceiling still advances only
   in numerical order. It builds on Milestone 18's servers and Milestone 26's
   Email mode and completes neither of them; their open items stay theirs.
   Its shared-file touches are named here rather than discovered in review:
   the read server's header projection and roster, the policy engine's
   approval floor, tool registration's target pinning, the egress proxy's
   constructors, the typed email task kinds, and the email router.
2. **Three surfaces, one service.** A Subscriptions review list in Email
   mode, an Unsubscribe action on any bulk thread, and two builtin Chat
   tools all read and act through one application service and one set of
   records, so a sender kept, unsubscribed, or reported in one place is so
   everywhere.
3. **One-click is the primary mechanism, and eligibility is authenticated.**
   A mechanism is offered only when Gmail's own, topmost
   `Authentication-Results` verdict reports a passing DKIM signature and that
   signature's `h=` tag covers the mechanism's headers. A message that fails
   offers nothing automated. This is RFC 8058's own condition, and its
   practical effect is that the mail whose links should never be touched is
   offered only Report spam.
4. **The destination is server-derived and never an argument.** The
   first-party read server extracts the header's address; the refresh task
   verifies and stores it; `email.unsubscribe` accepts only subscription ids
   and evidence digests and dials the stored value. A conversation chooses
   which sender, never where. Approving the arguments is approving the
   destination, because the digest binds them, and the address itself reaches
   no model context, approval view, response, notification, or log.
5. **The platform sends the request itself, through a dedicated
   public-HTTPS egress transport. The owner approved this posture change
   explicitly on 2026-09-19.** It is the platform's first direct dial to a
   host selected by mail content. The transport is the rule ADR-0098
   established for browser resources, under its own constructor: `CONNECT`
   only, port 443 only, a public hostname shape, one resolution with every
   resolved address checked against the non-configurable denylist, and a dial
   to the checked address. The request is a constant — fixed body, fixed
   headers, no cookie, no credential, nothing authored by the owner or a
   model — it follows no redirect, and its response is discarded unparsed.
   The operator's egress allowlist is untouched and cannot select the
   transport, which is constructed only for this one tool. The accepted
   residuals are stated rather than implied away: a sender learns the
   server's address and that the mailbox is live, and an authenticated sender
   can aim one constant, credential-free POST at a public endpoint of its
   choosing. Every RFC 8058 receiver accepts the same exposure.
6. **A tap is the consent, and the approval floor is unchanged.** Every
   action here is `EXTERNAL_WRITE` or `EXTERNAL_MESSAGE` and resolves to
   `REQUIRE_APPROVAL`. In Email mode, ADR-0095's mechanism supplies the
   approval: an immutable consent that expires after 120 seconds derives the
   only invocations it will authorize, and a consent consumer resolves the
   pending approval only on an exact match, rechecked at the pre-effect
   boundary. This ADR generalizes that consent's subject from one thread to
   one bounded batch of at most twenty-five senders and changes nothing else
   about it. In Chat, the owner approves one batch by value in the ordinary
   queue. `email.unsubscribe` joins the floor no profile may lower, and no
   standing authorization satisfies any action. Roadmap B8 is untouched.
7. **`email.unsubscribe` is `EXTERNAL_WRITE`, `MEDIUM`, and `IDEMPOTENT`.**
   The request is a constant and repeating it changes nothing, so recovery
   may re-execute. `NON_IDEMPOTENT` would send every timeout to a human
   review with nothing to review, because an unsubscribe cannot be read back.
   The `mailto` send and the label actions keep Milestone 18's
   `NON_IDEMPOTENT` class and its rule that a possibly dispatched call is
   `uncertain` and never repeated.
8. **Fallbacks are closed and fixed.** A `mailto` unsubscribe is exactly the
   header's single recipient, subject, and body within tight bounds, sent
   through the account's send server; any other header field disqualifies it.
   Report spam adds `SPAM` and removes `INBOX`, Not spam restores exactly the
   threads a report moved, and sender cleanup removes `INBOX`. Thread sets are
   server-selected inside the consented run and post-filtered by identity;
   callers supply neither threads nor labels. No new Gmail permission is
   requested: the existing read, modify, and send grants cover all of it.
9. **The census is a deterministic projection that rides refresh.** It reads
   a closed `bulk` block on the thread summaries the refresh task already
   pages, inside ADR-0096's ninety-day window, excluding Spam, Trash, and
   owner-sent mail. It makes no model call, reserves no automatic-email
   dollars, issues no query of its own, and advances only in foreground
   slices. Senders the owner marked Important or writes to are protected from
   bulk selection.
10. **Outcomes are reported honestly and nothing follows up on its own.**
    Only a 2xx response records an accepted request, and accepted is not
    described as honored. Mail dated after a ten-day grace period marks the
    sender as still sending and offers Report spam. No state change
    dispatches anything.
11. **Off by default.** `AGENT_EMAIL_UNSUBSCRIBE_ENABLED` requires both
    existing email flags. Unset, no route, tool, transport, census, or new
    read exists.

## Scope admission and consequences

Admitted: the census; the one-click request and its transport; the `mailto`,
Report spam, Not spam, and sender-cleanup actions; gesture and Chat consent;
four routes and two additive projection fields; two builtin tools; and the
native Subscriptions view and thread action on iPhone, iPad, and Mac.

Deferred, each for a stated reason in the design: unsubscribe links in a
message body; web pages that need clicks or a login; standing rules and
automatic unsubscribes; background or scheduled cleanup; Gmail filters and
blocked senders, which need a new OAuth scope; undoing an unsubscribe, which
the protocol does not support; a durable Keep from Chat; and a second mail
provider.

Consequences:

- The platform gains a second public-HTTPS transport and its first tool that
  dials a mail-selected host. The boundary that holds it is construction plus
  enforcement: a closed input schema, a registration pin, an authenticated
  stored destination, and the proxy's address check. A future tool that wants
  the same reach needs its own decision; this one grants the transport to one
  tool by name.
- The read server's roster grows by one application-only tool and its
  projection by one closed block. Its endpoint set, refused redirects, and
  import isolation are unchanged, and Milestone 18's gates keep asserting
  them.
- The approval floor in the policy engine gains one tool-name arm beside its
  email-server arm. The default matrix and the trust overlay are unchanged.
- ADR-0095's consent now has a second subject shape. Both share one consumer
  discipline: exact match, bounded expiry, and a pre-effect recheck.
- The census registry grows by twenty entries in the existing `email` area,
  so the gate-identifier grammar is unchanged.
- Spam reports start Gmail's thirty-day deletion clock, as `trash_thread`
  already does. The design answers it the way Milestone 18 answered trash:
  the action is reversible inside the product.

## Alternatives considered

- **Send the request from the owner's device.** It adds no server egress,
  but it exposes the device's address and location to bulk senders, cannot
  serve Chat, bypasses the tool pipeline and its approval, and leaves no
  governed audit trail. It would also need three native network
  implementations to keep one invariant.
- **Let `gmail_mcp` dial the sender.** Registered gates hold that package to
  two Google endpoints, and a process holding a Gmail credential is the last
  place an arbitrary dial belongs.
- **Use the hosted web-fetch providers.** They extract pages; none offers a
  constant-body POST, and routing an owner-identifying token through a third
  party trades a bounded exposure for an unbounded one.
- **Use browser automation.** It reaches only profile-allowlisted origins,
  it is a full browser pointed at a sender's page, and it is the
  highest-risk surface for the lowest-value case. Deferred, not rejected.
- **Only surface the link for the owner to open.** The lowest risk and the
  least help: it removes the search and keeps the visit, the page, and the
  confirmation, once per sender.
- **Parse unsubscribe links out of message bodies.** A body link is
  unauthenticated, is the form an attacker controls most freely, and is how a
  mailbox confirms itself to a spammer.
- **Widen the operator allowlist instead of adding a transport.** An open
  destination set cannot be listed, and expressing it there would weaken the
  sandbox's boundary for a tool that never runs in a sandbox.
- **Standing auto-unsubscribe rules.** RFC 8058 forbids the request without
  consent, roadmap B8 owns standing grants, and Milestone 26 excludes
  automatic mailbox actions.

## Acceptance status

The owner requested the capability and answered the four shaping decisions in
chat on 2026-09-19, including explicit approval of the server-side egress
posture in decision 5, and then directed implementation through the design's
build sequence the same day. Implementation proceeds under the repository's
red-green rule with the feature default-off. Pull request creation, merge,
production activation, and any live unsubscribe request, send, or mailbox write
retain their explicit authorization boundaries; the owner's real-mailbox smoke
is the milestone's own release evidence and no fake provider stands in for it.
