---
title: Email Unsubscribe Assistance
status: design
canonical: true
---

# Email unsubscribe assistance

This document specifies Milestone 31. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md) and it extends rather than replaces
[email-integration.md](email-integration.md) and
[email-experience.md](email-experience.md): the account-isolated Gmail MCP
servers, the typed email tasks, the owner-gesture consent that Archive
introduced, the approval floor, and the egress proxy all stay exactly as they
are. [ADR-0112](../adr/0112-milestone-31-email-unsubscribe.md) records the
architectural decisions, the owner's four shaping choices of 2026-09-19, and
the one security-posture change this milestone makes.

The owner spends a lot of time unsubscribing from junk mail. Doing it by hand
means opening each message, finding a small link, following it to a web page,
and confirming — once per sender, across two accounts. Almost all of that is
mechanical, and the mail itself already says how to do it: a bulk message
carries a `List-Unsubscribe` header, and since February 2024 Gmail has
required every large sender to make that header work with one fixed,
credential-free HTTPS request
([RFC 8058](https://www.rfc-editor.org/rfc/rfc8058.html)). Milestone 31 builds
the assistant around that fact: a **census** of bulk senders read from headers
alone, a **one-click request** the platform sends only with the owner's
consent and only to a destination the server derived itself, and the
**fallbacks** for senders that offer something weaker or deserve something
harsher.

Milestone 31 is authorized as a parallel workstream. Its gates may become
green independently, but the verified gate ceiling advances only in numerical
order.

## Owner decisions

The owner decided these on 2026-09-19. They are requirements, not defaults an
implementation may trade away.

| Area | Decision |
| --- | --- |
| Surfaces | A Subscriptions review list in Email mode, an Unsubscribe action on any bulk thread, and Chat tools. |
| Consent | A clearly labelled tap, or one confirmed multi-select batch, is the consent for exactly those senders. In Chat the agent proposes a batch of at most twenty-five named senders and the owner approves it once. |
| Egress | The platform sends the request server-side through the egress proxy. This is the platform's first direct dial to a host that mail content selected, and the owner approved it explicitly. |
| Fallbacks | Report spam, `mailto:` unsubscribe, and archiving the sender's existing Inbox mail. |
| Deferred | Web-form unsubscribes through browser automation, and every standing or automatic unsubscribe rule. |

## Scope

Milestone 31 delivers the census, the three actions, the consent that
authorizes them, and the surfaces that operate them.

- **The census.** One record per bulk sender per account, derived from header
  metadata the refresh task already pages through, within the ninety-day
  window. No model call builds it and no automatic-email dollar is reserved
  for it.
- **Unsubscribe.** The RFC 8058 one-click request where the message
  authenticates it, and the header's `mailto:` message through the account's
  send server where that is all the sender offers.
- **Report spam and Not spam.** One fixed label delta through the account's
  write server, for senders that offer no safe mechanism, fail
  authentication, or keep mailing after they were asked to stop.
- **Sender cleanup.** Optionally archive the sender's existing Inbox mail as
  part of the same consented gesture.
- **Surfaces.** Four routes under `/v1/email/subscriptions`, an additive
  `subscription` block on the thread projection, two builtin Chat tools, and
  the native Subscriptions view and thread action on iPhone, iPad, and Mac.

Eight things are out of scope, and each is named because a reader who does
not find it here should find the reason here.

1. **Unsubscribe links in the message body.** The read server reduces HTML
   to text and discards every `href`, and that is the right default: a body
   link is the form an attacker controls most freely, it is not covered by
   the authentication this design relies on, and following one is how a
   mailbox confirms itself to a spammer.
2. **Web pages that need clicks or a login.** A `List-Unsubscribe` HTTPS
   address without the one-click marker is a page, not an endpoint. Driving
   it is browser automation against an origin no profile allowlists
   ([browser-automation.md](browser-automation.md)). The census reports such
   a sender honestly as having no automated mechanism.
3. **Standing rules and automatic unsubscribes.** RFC 8058 forbids the
   request without user consent, roadmap B8 still owns standing grants, and
   Milestone 26 excludes automatic mailbox actions. Nothing here unsubscribes,
   reports, or archives without an owner act for those exact senders.
4. **Background or scheduled cleanup.** The census advances only inside
   foreground refresh slices (email-experience.md:134-139). There is no
   monitor, digest, or notification about subscriptions.
5. **Gmail filters and blocked senders.** Both need the
   `gmail.settings.basic` OAuth scope and a new consent ceremony. No new
   Google permission is requested by this milestone.
6. **Undoing an unsubscribe.** The protocol has no inverse. Keep and Report
   spam are reversible; an accepted unsubscribe is not, and the interface
   says so before the tap.
7. **A durable Keep from Chat.** Keep is an interface and HTTP command. In
   conversation the owner simply leaves a sender out of the batch.
8. **A second mail provider.** The census reads Gmail's normalized contract
   and nothing else.

## Foundations this design reuses

- **The first-party Gmail servers.** `gmail_mcp` stays the only code that
  speaks to Google, still dials exactly its two fixed endpoints
  (email-integration.md:334-342), and still imports nothing from
  `agent_core`. It already fetches complete headers; it projects a closed
  allowlist of them, and this milestone widens that projection by one closed
  block. It never dials a sender.
- **Typed email tasks.** Unsubscribe work is a typed, model-free task on the
  ordinary durable queue, in the interactive class, preparing only the
  servers it calls (ADR-0104), exactly as Archive is
  (email-experience.md:710-726).
- **Owner-gesture consent.** ADR-0095 made a clearly labelled gesture the
  consent for exactly one action while keeping `REQUIRE_APPROVAL` in force: a
  consent consumer resolves the still-mandatory approval only on an exact
  match. This milestone reuses that mechanism and generalizes its subject
  from one thread to one bounded batch of senders.
- **The approval floor.** `EXTERNAL_WRITE` and `EXTERNAL_MESSAGE` require
  approval in the default matrix (policy-and-approvals.md:596-597), and no
  profile may downgrade a mailbox write or send (ADR-0071 decision 8). The
  new request tool joins that floor.
- **The egress proxy.** One policy function checks every outbound connection
  (sandbox-isolation.md:811-816): resolve once, refuse any non-public
  address, dial the address that was checked (sandbox-isolation.md:863-882).
  ADR-0098 already runs a dedicated public-HTTPS transport through it for the
  browser. This milestone adds a second transport under the same rule.
- **The ninety-day window and its exclusions.** ADR-0096's window, and the
  exclusion of Spam and Trash, apply unchanged
  (email-experience.md:231-244).

Nothing in the platform dials an arbitrary public host today. `web.fetch`
hands its URL to a hosted provider, which owns resolution and redirects
inside its own boundary (web-access.md:72-78). That is why the egress
decision below is an ADR decision and not an implementation detail.

## What a message may offer

A bulk message can describe up to three ways out. The design treats them by
how much can be verified, not by how convenient they are.

| Mechanism | Evidence in the message | What the platform does |
| --- | --- | --- |
| `one_click` | An HTTPS address in `List-Unsubscribe`, and `List-Unsubscribe-Post` equal to exactly `List-Unsubscribe=One-Click` | Sends the fixed RFC 8058 request. |
| `mailto` | A `mailto:` address in `List-Unsubscribe` and no usable one-click pair | Sends the header-specified message through the account's send server. |
| `none` | Anything else, including an HTTPS address without the marker, which the census records as *link-only* | Offers Report spam and Keep only. |

Every automated mechanism additionally requires **authentication**, which is
the RFC's own condition and the control that separates a newsletter from a
forgery:

1. Gmail's own verdict reports a passing DKIM signature. The verdict is the
   *first* `Authentication-Results` header in document order whose
   authentication-service identifier is `mx.google.com`. Receiving servers
   prepend their headers, so a sender-supplied forgery sits below Gmail's
   and is never read.
2. A `DKIM-Signature` header whose `d=` domain is one Gmail reported as
   passing lists the mechanism's headers in its `h=` tag:
   `list-unsubscribe` and `list-unsubscribe-post` for `one_click`,
   `list-unsubscribe` for `mailto`.

A message that fails either clause offers `none`, whatever its headers claim.
RFC 8058 says a receiver should not offer one-click for such a message; this
design does not offer it. The practical effect is the one wanted:
unauthenticated bulk mail is, overwhelmingly, the mail whose links should
never be touched, and the only thing the interface offers for it is Report
spam.

Eligibility is a pure function of one message's headers. It consults no
model, no memory, and no body text, and a property gate generates header sets
that violate each clause in turn.

## The read contract

Two additive changes to the read server, both in `gmail_mcp` and both covered
by the shared fake-provider contract suite.

**`search_threads` gains a `bulk` block per thread.** The metadata fan-out
adds `List-Unsubscribe`, `List-Unsubscribe-Post`, and `List-Id` to its header
allowlist, and each returned summary carries:

```json
{"bulk": {"message_id": "18c…", "from": "Example News <news@example.com>",
          "date": "Fri, 18 Sep 2026 09:00:00 +0000",
          "list_id": "news.example.com", "unsubscribe": "one_click"}}
```

All five values come from the newest *received* message in the thread, so an
owner's own reply never stands in for the sender; a thread with no received
message carries empty strings and `none`. `unsubscribe` is `one_click`,
`mailto`, `link`, or `none`; `list_id` is bounded to 255 characters and empty
when absent; `message_id` is the opaque provider id that names the evidence
message. No unsubscribe address and no URI appears here. This tool is
model-visible, and the block tells a conversation which results are bulk mail
without handing it a destination. The value is unauthenticated at this stage
and is never sufficient to act on.

**`get_unsubscribe(message_id)` is a new application-only tool.** It carries
the `veetbot/application-only` marker the Milestone 26 synchronization tools
carry, so it is never advertised to a model. It reads one message with
`format=metadata` and returns the closed, versioned block:

```json
{
  "schema_version": 1,
  "message_id": "…", "thread_id": "…", "history_id": "…",
  "from": "Example News <news@example.com>",
  "list_id": "news.example.com",
  "offered": "one_click",
  "mechanism": "one_click",
  "https_uri": "https://example.com/u/…",
  "mailto": null,
  "authenticated": true,
  "covered_headers": ["list-unsubscribe", "list-unsubscribe-post"]
}
```

`https_uri` is the first HTTPS address in the header, at most 2,048
characters, extracted syntactically. `mailto` is `null` or a closed object of
`to`, `subject`, and `body`: exactly one recipient, a subject of at most 256
characters, a body of at most 1,024, and nothing else. A `mailto:` address
carrying any other header field — `cc`, `bcc`, or an arbitrary one, all of
which RFC 6068 permits — or more than one recipient normalizes to no `mailto`
at all rather than to a trimmed one. `offered` is what the headers claim,
as the summary block reports it; `mechanism` and `authenticated` apply the
rules of the previous section, so a message whose signature covers only
`List-Unsubscribe` and that also carries a valid `mailto:` offers `one_click`
and yields `mailto`.

The package normalizes; it does not authorize. It cannot import
`agent_core`'s public-address rule and does not try to. `agent_core` applies
`is_public_https_url` when it stores the block and again when it dispatches,
and the proxy decides at connect time.

The read roster in [email-integration.md](email-integration.md) grows by this
one tool when the implementation lands, amended in the same change, and the
roster-confinement gate grows with it. The package's endpoint set, its
refused redirects, and its import isolation do not change.

## The census

### The subscription record

A subscription is one bulk sender as one account sees it. Its identity is the
message's `List-Id` when the message has one and the sender's address
otherwise, qualified by the account. The same newsletter arriving at both
accounts is two subscriptions, because it is two recipients and two
unsubscribe tokens. A sender's domain is deliberately not the identity: one
hosting domain carries thousands of unrelated lists.

Records live in `email_records` under the kind `subscription`, keyed by a
digest of the account and identity, with the store's ordinary revision,
tenant and principal predicates, and forced row-level security. A second
kind, `subscription_thread`, is the index from an account-qualified provider
thread to its subscription; it holds an opaque id and nothing else, is written
and pruned with the record's thread set, and is what places the action on a
thread without scanning the census.

| Field | Content |
| --- | --- |
| Identity | Account, identity kind (`list` or `sender`), identity value, display name, address. |
| Volume | Distinct threads observed in the window (counted to 200, then reported as more), first seen, last received. |
| Evidence | The newest received message carrying the header: its provider message and thread ids, received time, verified block, destination host, and **evidence digest**. |
| State | `active`, `kept`, `pending`, `unsubscribed`, `failed`, `still_sending`, or `reported_spam`. |
| Protection | Whether the sender is protected from bulk selection, and why. |
| Operation | The durable operation, run, action, outcome code, requested time, and — for a spam report — the provider thread ids it moved. |

The evidence digest is the SHA-256 of the verified block's canonical form. It
is how an approval, a consent, and a dispatch all name the same destination
without any of them carrying it.

### How records form

The refresh task already pages thread summaries for each account, up to 100
per slice (email-experience.md:254-259). The census reads the `bulk` block on
the summaries refresh was already reading. It issues no query of its own and
adds no page. A summary whose block says anything but `none`, or that carries
a `List-Id`, creates or updates its subscription; owner-sent mail, Spam, and
Trash never do.

The census is a deterministic projection. Observing a summary twice, or in
another order, or after a resynchronization, yields the same record: volume
counts distinct account-qualified provider thread ids, and a thread that
leaves the ninety-day window leaves the count. When a newer message from the
sender carries the header, evidence moves to it, because a newer token is the
one most likely to still work.

**Verification** fills the evidence block. It runs last in a refresh slice,
after synchronization, assessment, and drafting, and only with the tool and
step headroom the slice has left, so it can never starve the work Milestone
26 defined. Within that headroom the task calls `get_unsubscribe` for at most
twenty-five subscriptions per account whose evidence is new or unverified,
highest volume first, and stores the result; it stops quietly when the
headroom or the read server runs out. An unverified subscription shows as
*Checking* and cannot be selected for Unsubscribe; Keep and Report spam never
wait on verification. Message headers are immutable for a provider message id, so a
verified block does not go stale and dispatch needs no second read.

**Protection** keeps bulk selection away from mail the owner values. A
subscription is protected when the owner's explicit Important feedback
targets that sender, or when the owner has sent mail to that address inside
the window. Protected rows sort last, are skipped by Select all, and can
still be unsubscribed one at a time by an explicit tap. Protection is
computed from the feedback ledger and Sent metadata Milestone 26 already
holds; it reads no assessment and spends nothing.

### States

```text
active ──tap──▶ pending ──accepted──▶ unsubscribed ──mail after grace──▶ still_sending
   │               └──failed─────────▶ failed ──tap──▶ pending
   ├──keep──▶ kept ──unkeep──▶ active
   └──report spam──▶ pending ──▶ reported_spam ──not spam──▶ active
```

`kept` is durable: a kept sender is never suggested again, through new mail,
re-import, and resynchronization, until the owner reverses it. `unsubscribed`
and `reported_spam` are equally durable against re-suggestion.

`pending` never outlives its run. When a gesture's run is terminal and a
sender is still pending — the worker died, was cancelled, or ran out of
budget — the next read or command settles it under the owner lock, the way
Archive reconciles its own operation: a send or label write that may have
been dispatched becomes `uncertain` with `unsubscribe.outcome_unknown`, and
anything else becomes `failed` with `unsubscribe.not_attempted`. A one-click
request that left no outcome is safe to offer again, because repeating the
constant request changes nothing.

A row in `active` whose newest message leaves the window is deleted. A row
that records an owner decision or an outcome persists as a minimal decision
record, because forgetting it would re-suggest the sender and would blind the
follow-up below.

## The one-click request

### The tool

`email.unsubscribe` is a builtin tool, and the only code in the platform that
sends the request.

| Property | Value |
| --- | --- |
| Classification | `EXTERNAL_WRITE` / `MEDIUM` / `IDEMPOTENT` |
| Target | `unsubscribe_endpoint`, network enabled, not isolated |
| Output trust | `INTERNAL_TOOL` — closed outcome codes only, no remote content |
| Scopes | `email.read` and `email.write`, plus the read scope of each target's account |
| Input | `targets`: one to twenty-five unique `{subscription_id, evidence_digest}` pairs. Nothing else. |

The input schema is closed. There is no URL, host, header, body, or account
argument, and no optional one. Tool registration pins the
`unsubscribe_endpoint` target kind to this exact name and tuple, the way it
pins the web and browser provider targets, so no other tool can claim the
transport.

**The destination is server-derived.** For each target the tool loads the
principal's own subscription, requires verified `one_click` evidence whose
digest equals the argument, re-applies `is_public_https_url`, and dials the
stored address. A conversation can choose *which sender*; it cannot choose
*where*. Mail body text cannot influence the destination at all, because the
destination was never read from a body. A digest that does not match refuses
that target before any network activity, which is what makes an approval of
the arguments an approval of the destination.

**Classification.** `IDEMPOTENT` is the honest class and not a convenience:
the request is a constant, and sending it twice leaves the recipient exactly
as unsubscribed as sending it once. Recovery may therefore re-execute
(tool-system.md:662-671), which matters because an unsubscribe has no
read-back that could reconcile an unknown outcome. Per-target outcomes are
persisted as each completes, so a re-execution skips targets already
accepted rather than dialling them again.

### The request

```text
POST <path and query of the stored address> HTTP/1.1
Host: <its host>
Content-Type: application/x-www-form-urlencoded
Content-Length: 26
User-Agent: Veetbot-Unsubscribe/1

List-Unsubscribe=One-Click
```

Every byte is either a constant or the sender's own address handed back to
the sender. Nothing the owner or a model authored is in it, so it has nothing
to exfiltrate. The client holds no cookie jar and ignores `Set-Cookie`; it
sends no `Authorization`, `Cookie`, `Referer`, or `Origin`; TLS verification
against the system trust store is on and nothing may override it. A redirect
is never followed — RFC 8058 forbids the sender from issuing one, and
following one is how a consented request to one host becomes an unconsented
request to another. Each request has a ten-second deadline. At most 64 KiB of
the response is read and all of it is discarded: nothing from a response is
parsed, stored, or shown, and only its status class survives. Targets in one
invocation dispatch with a concurrency of five under a ninety-second tool
deadline.

### Outcomes

| Observation | Code | Subscription state |
| --- | --- | --- |
| 2xx | `unsubscribe.accepted` | `unsubscribed` |
| 3xx | `unsubscribe.redirect_refused` | `failed` |
| 4xx or 5xx | `unsubscribe.rejected` | `failed` |
| Proxy refusal | `unsubscribe.destination_refused` | `failed` |
| TLS failure | `unsubscribe.tls_failed` | `failed` |
| No response in time | `unsubscribe.unreachable` | `failed` |
| Digest mismatch | `unsubscribe.evidence_changed` | unchanged |
| Not eligible, kept, or already done | `unsubscribe.not_eligible` | unchanged |
| `mailto` evidence | `unsubscribe.requires_send` | unchanged |

`accepted` means the sender's server took the request. It is not a promise
the sender will honor it, and the interface does not say it is; the follow-up
below is what finds out. A `failed` row offers the actions that remain: try
again, Report spam, or Keep.

The other actions record closed codes of their own on the sender's durable
operation: `unsubscribe.sent`, `unsubscribe.send_failed`, and
`unsubscribe.send_uncertain` for a `mailto` message; `labels.completed`,
`labels.failed`, and `labels.uncertain` for a spam report or its reversal; and
`unsubscribe.not_attempted` for a sender a task never reached, which returns
it to the state it was in. No code carries provider or sender text.

## The egress transport

This is the milestone's security-posture change, recorded in ADR-0112
decision 5 with the owner's explicit approval.

The request leaves the worker through a process-local egress proxy running
the **public-HTTPS transport rule** — the rule ADR-0098 established for
browser resources, under a second constructor so the two transports stay
separately auditable:

1. `CONNECT` only. A plaintext request is refused.
2. Port 443 only.
3. A public hostname shape: no IP literal, no single-label name, no
   all-numeric final label, none of the `.internal`, `.local`, `.localhost`,
   `.home`, or `.lan` suffixes.
4. One resolution at the proxy. **Every** resolved address must be public
   under the non-configurable denylist (sandbox-isolation.md:884-898) —
   private, loopback, link-local and metadata, carrier-grade NAT, unique
   local, and IPv4-mapped forms are refused, and one bad address refuses the
   connection.
5. The proxy dials the address it checked, never the name again.
6. The decision is logged with tenant, host, port, resolved address, and
   outcome. The path and query — where the recipient token lives — are
   inside the TLS tunnel and never reach the proxy.

The operator's egress allowlist is untouched. Its serialized policy cannot
select this transport, sandboxes cannot reach it, and an approved
`SANDBOX_NETWORK` still grants the allowlist and nothing more. The transport
is constructed only for `email.unsubscribe`; no other tool or adapter is
handed its address.

What bounds the destination set is therefore not a list. It is the
conjunction of: an address Gmail authenticated as the sender's own, read by
the first-party server and never by a model; an owner act naming that sender;
a public address on port 443; and a request with no credential and no
authored content. What remains is accepted on the record: a sender learns the
server's address and learns that the mailbox is live, and an authenticated
sender can aim one constant, credential-free POST at a public endpoint of its
choosing. Every mailbox provider that implements RFC 8058 accepts the same
exposure, and RFC 8058's own security considerations describe it.

## Consent

Every action in this milestone is an `EXTERNAL_WRITE` or an
`EXTERNAL_MESSAGE`, every one requires approval, and nothing here changes
that. What differs between the two surfaces is who supplies the approval.

**In Email mode the gesture supplies it.** A tap on Unsubscribe, a confirmed
multi-select batch, Report spam, or Not spam creates one immutable
`EmailSubscriptionConsent`, persisted with the durable task: owner, action,
the targets with their subscription revisions and evidence digests, whether
to archive existing mail, the pinned server ids of each account, and an
expiry 120 seconds out. The consent *derives* the only invocations it will
authorize, as Archive's does:

| Action | Tool | Arguments the consent derives |
| --- | --- | --- |
| One-click targets | `email.unsubscribe` | The consented pairs, sorted |
| Each `mailto` target | `mcp.{send_server_id}.send_message` | Exactly the verified `to`, `subject`, and `body` |
| Archive existing mail | `mcp.{write_server_id}.modify_labels` | Remove `INBOX`, over a server-selected thread set |
| Report spam | the same | Add `SPAM`, remove `INBOX`, over a server-selected thread set |
| Not spam | the same | Remove `SPAM`, add `INBOX`, over the recorded thread ids |

The typed task invokes each tool through the ordinary pipeline. Policy
answers `REQUIRE_APPROVAL`; the approval is created and
`approval.requested` is recorded; the consent consumer resolves it
`APPROVE_ONCE` only when the pending invocation's tool and normalized
arguments equal what the consent derives and the owner, authority, revisions,
and expiry still hold. The pre-effect dispatch guard checks again immediately
before the effect watermark, so a revocation between resolution and dispatch
reaches nothing. No second notification asks for consent already given.
Refresh work, model work, and mail content can never mint a consent: it
exists only as the result of an authenticated owner command.

A **server-selected thread set** is how a label action stays fixed without
the owner enumerating threads. Inside the consented run, the task searches
the account through the governed read server with a query the server
composes from the subscription's identity, keeps only results whose own
sender or `List-Id` equals that identity, and takes at most 100 threads, 25
per call, always including the evidence thread for a spam report. An
identity that is not a plain address or list identifier disables the search
instead of being quoted into it, and the post-filter means a crafted identity
cannot widen the set even if it reaches the query. The consumer accepts a
`modify_labels` invocation only when its thread ids are a subset of that
recorded result and its label delta is exactly the consented one. Callers
supply neither threads nor labels.

**In Chat the approval queue supplies it.** The model calls
`email.unsubscribe` with a batch; policy requires approval; the owner
approves or denies once. The approval view names each sender — display name,
address, account, mechanism, and destination host — and never the path or
query. Argument digests keep the withheld values verifiable (ADR-0099), and
changed arguments void the approval as they always do. A `mailto` target
comes back `unsubscribe.requires_send`, and the model proposes the send
through the account's send tool, where the owner approves the exact message
by value like any other send. Spam reports and archives proposed in Chat use
the existing `modify_labels` tool and its existing approval.

No standing authorization satisfies any of these, in either surface.

## The fallbacks

**`mailto`.** The message is the sender's own specification: one recipient,
the header's subject and body or empty ones, no `cc`, no `bcc`, no thread.
The confirmation labels such a row with the recipient — *Sends an email to
unsub@lists.example.org* — because a tap that sends mail must say so and say
where. Dispatch is the account's ordinary send server under Milestone 18's
rules without exception: `NON_IDEMPOTENT`, and an outcome lost after
dispatch is `uncertain`, shown as *Send status unknown*, and never sent again
automatically (email-integration.md:381-394).

**Report spam** applies `SPAM` and removes `INBOX`. It is what the interface
offers when a sender has no safe mechanism, when it failed authentication,
and when it is `still_sending`. It is reversible: **Not spam** restores
exactly the threads the report moved, from the ids the operation recorded.
Gmail empties Spam after thirty days, as it empties Trash, which is the same
grace period Milestone 18 accepted for `trash_thread` and the reason the
reversal is a first-class command rather than advice to open Gmail.

**Sender cleanup** removes `INBOX` from the sender's existing Inbox threads.
It is an option on the unsubscribe confirmation, off by default, and applies
only to senders whose unsubscribe was accepted or sent. Archived mail stays
in All Mail.

Label actions keep Milestone 18's uncertainty rule: a possibly dispatched
`modify_labels` is never retried, and a fresh governed read reconciles what
Gmail actually shows.

## Follow-up

A sender gets time to comply. When refresh observes a newly received message
from an `unsubscribed` sender dated more than the grace period after the
request — ten days by default — the subscription becomes `still_sending` and
the row offers Report spam. Mail inside the grace period changes nothing,
which also covers the confirmation message many senders send.

Nothing in the follow-up dispatches anything. It is a state change the owner
sees the next time the list is open.

## The Chat tools

| Tool | Classification | Purpose |
| --- | --- | --- |
| `email.subscriptions` | `NONE` / `LOW` / `READ_ONLY`, in process, `EXTERNAL_UNTRUSTED` output, `email.read` | Cursor-paginated census rows, at most fifty per call, filterable by account and state. |
| `email.unsubscribe` | as above | The one-click request for a consented batch. |

`email.subscriptions` returns what a conversation needs to propose a batch
and nothing that would let it dial: subscription id, account, display name,
address, list identifier, volume, last received, mechanism, state,
protection, evidence digest, destination host, and — for a `mailto` sender —
the closed `to`, `subject`, and `body`. It never returns `https_uri`. Its
output is `EXTERNAL_UNTRUSTED` because names, addresses, and `mailto` text
are sender-authored, and the trust overlay treats anything proposed
downstream of it accordingly.

## The routes

All four are absent unless `AGENT_EMAIL_UNSUBSCRIBE_ENABLED` is set, carry
`Cache-Control: private, no-store`, and answer a foreign or unknown
subscription with an indistinguishable 404.

| Route | Scope | Operation |
| --- | --- | --- |
| `GET /v1/email/subscriptions` | `email.read` | Cursor-paginated rows; `account_id`, `state`, `limit` of one to one hundred. Never includes `https_uri`. |
| `POST /v1/email/subscriptions/unsubscribe` | `email.write` | One to twenty-five `{subscription_id, evidence_digest, expected_revision}` targets, `archive_existing`, and an idempotency key. Admits one typed task. |
| `POST /v1/email/subscriptions/{id}/spam` | `email.write` | `expected_revision`, a strict `spam` boolean (`false` is Not spam), and an idempotency key. |
| `POST /v1/email/subscriptions/{id}/keep` | `email.write` | `expected_revision` and a strict `kept` boolean. Changes no mailbox. |

The two commands that admit a task also require `email.read`, `run.write`,
`session.write`, `approval.resolve`, and each target account's exact MCP
scopes for what its mechanism calls — read always, send for a `mailto`
target, write for cleanup or a spam report — checked at admission and again
at dispatch. A request cannot mint account authority. Status is the existing
`GET /v1/email/operations/{operation_id}`; identical command retries replay
their durable result; a stale revision conflicts without changing state.

Two projections grow additively. The account projection advertises
`unsubscribe_supported`. The thread projection carries a nullable
`subscription` block — id, state, mechanism, destination, evidence digest,
and revision — on thread detail when the conversation belongs to one, which
is what places the action on a thread.

## The native experience

**Subscriptions** is a view inside Email mode on all three clients: a
list-to-detail flow on iPhone, and a list beside its detail on Mac and
regular-width iPad. Each row shows the account badge, the sender, how many
conversations arrived in the window, when the last one did, what the sender
offers, and the state. Rows sort by volume and then recency, with protected
rows last; the owner can filter by account and state.

Unsubscribe on one row, or Select followed by *Unsubscribe N*, opens one
confirmation. It lists every sender with its mechanism in plain words —
*One-click request to example.com*, *Sends an email to unsub@…* — states
that an unsubscribe cannot be undone, and offers *Also archive existing mail
from these senders*. Confirming is the consent. Select all skips protected
rows and stops at twenty-five.

A bulk thread's detail carries the same Unsubscribe action beside its sender,
opening the same confirmation with one row.

Rows update optimistically to *Unsubscribing…* and settle from the durable
operation, as Archive rows do. Success is quiet. A failure restores the row
with what remains possible: try again, Report spam, or Keep. `still_sending`
rows say so and offer Report spam; `reported_spam` rows offer Not spam. A
server or account that does not advertise `unsubscribe_supported` shows no
Subscriptions entry and no thread action, and the client never substitutes
another command for the missing one.

## Privacy, retention, and telemetry

An unsubscribe address carries a token that identifies the owner to the
sender, and a sender list is a map of the owner's interests. Both are mail
content under email-experience.md's privacy rules.

- `https_uri` reaches no model context, approval view, route response,
  notification, log, or metric. It exists in the governed tool event that
  read it, in the stored evidence block, and inside the TLS tunnel.
- Sender names, addresses, list identifiers, and `mailto` text appear in
  projections and approval views the owner reads. They never appear in
  operational logs, metrics, notifications, or checked-in fixtures. The
  proxy's audit line is its existing contract: host, port, address, and
  decision.
- Telemetry is content-free: subscriptions by state and mechanism, outcome
  codes, consent expiries and mismatches, time from gesture to outcome, and
  the `still_sending` rate.

Evidence blocks are erased when a subscription becomes `kept`,
`unsubscribed`, or `reported_spam`, and when the evidence message leaves the
window; a reversed Keep re-derives evidence at the next refresh. Decision
records keep only the identity, the state, the times, and — for a spam report
— the opaque thread ids Not spam needs. `get_unsubscribe` results are
source-qualified with account, thread, and message identity, so the Milestone
26 source-erasure mechanism removes them from tool events along with every
other copy of that source. Principal erasure removes every record this
milestone adds.

## Migration, configuration, and operations

No migration: subscription, consent, and operation state are new kinds in the
existing `email_records` table.

`AGENT_EMAIL_UNSUBSCRIBE_ENABLED` defaults to off and requires both
`AGENT_EMAIL_ENABLED` and `AGENT_EMAIL_MODE_ENABLED`; set without them it is
a configuration error at composition. Unset, the four routes are unmounted,
neither builtin tool is registered, the unsubscribe transport is never
constructed, refresh builds no census, and `get_unsubscribe` is never
called. `runtime/limits.yaml` gains `email.unsubscribe_grace_days`, default
10, bounded from 2 through 60. The request's shape, its deadlines, the batch
bound, and the consent expiry are constants, because a knob on any of them
would be a way to weaken a gate. That limit is the one versioned knob this
milestone adds to the executable inventory; the flag is an environment
variable like every other feature flag. A subscription task has a 300-second
deadline, because one gesture may send mail and page a sender's Inbox after
its one-click request; the consent's 120-second expiry still bounds every
approval it resolves.

Typed unsubscribe, spam, and cleanup tasks perform no model work and reserve
no automatic-email dollars, so an exhausted learning allowance never blocks
an explicit owner action. They remain bounded by ordinary tool, time,
concurrency, and authority limits.

## Safety

| Threat | What holds |
| --- | --- |
| A message body tells the agent to unsubscribe somewhere | The tool has no destination argument; the destination is a stored, authenticated header value. |
| A sender's header names an internal or metadata address | Public hostname shape, then the proxy's every-address check, then the pinned dial; port 443 only. |
| A sender's header aims the request at a third party | A constant body, no credential, no cookie, an owner act per batch, and an accountable DKIM domain. Accepted residual, stated above. |
| Data leaves in the request | Nothing owner- or model-authored is in it. |
| A forged `Authentication-Results` header | Only Gmail's own, topmost verdict is read, and coverage is checked against the signature Gmail passed. |
| Spam whose link confirms a live mailbox | Unauthenticated mail offers no automated mechanism; the row offers Report spam. |
| A `mailto:` address that mails a victim or carries a payload | One recipient, closed fields, tight bounds, the recipient named in the confirmation, the send floor and by-value consent. |
| A replayed, stale, or mismatched consent | Immutable, 120-second, exact-match, rechecked at the effect boundary. |
| A huge or slow response | Ten-second deadline, 64 KiB bounded read, body discarded unparsed. |
| A redirect to another host | Never followed; recorded as a failure. |
| DNS rebinding | One resolution, one dial, at the proxy. |
| A crafted identity widening a label action | Identity grammar, server-composed query, post-filter on every result, subset check in the consumer. |

## Hard gates

1. **The read contract is closed and Google-only.** The read server exposes
   the bounded `bulk` block on thread summaries and the versioned
   application-only `get_unsubscribe` block, passes the shared contract suite
   against the fake provider, never advertises `get_unsubscribe` to a model,
   and still dials only its two fixed endpoints. Registered as
   `gate.email.unsubscribe_read_contract`, case. **M31.**
2. **Eligibility is deterministic and authenticated.** Over generated header
   sets, `one_click` is offered only for an HTTPS address, the exact
   `List-Unsubscribe=One-Click` marker, and a Gmail-verified passing DKIM
   signature covering both headers; `mailto` only with covered
   `List-Unsubscribe` and the closed single-recipient fields; a forged lower
   verdict header, a missing clause, or any extra `mailto` field yields
   `none`. Registered as `gate.email.unsubscribe_eligibility`, property.
   **M31.**
3. **The destination is server-derived.** `email.unsubscribe` accepts only
   subscription ids and evidence digests under a closed schema; the dialled
   address equals the stored verified value; a digest mismatch, a foreign
   subscription, or an address failing the public-HTTPS rule refuses that
   target before any network activity; and tool registration rejects any
   other tool claiming the `unsubscribe_endpoint` target. Registered as
   `gate.email.unsubscribe_server_derived_destination`, structural. **M31.**
4. **The request is fixed.** Every request is a `POST` of exactly
   `List-Unsubscribe=One-Click` with the form content type and the fixed user
   agent, carries no cookie, authorization, referrer, or origin, never
   follows a redirect, verifies TLS without override, reads at most 64 KiB
   and keeps only the status class, and meets its ten-second deadline.
   Registered as `gate.email.unsubscribe_fixed_request`, case. **M31.**
5. **Egress is public HTTPS only.** Every dial crosses the process-local
   proxy under the public-HTTPS rule: private, loopback, link-local,
   metadata, carrier-grade NAT, unique-local, and IPv4-mapped destinations,
   IP literals, non-443 ports, and plaintext are refused before dialing; a
   name resolving to any non-public address is refused whole; the checked
   address is the one dialled; and the operator allowlist, the sandbox
   proxy, and the browser transport are unchanged and cannot select this
   transport. Registered as `gate.email.unsubscribe_public_https_egress`,
   case. **M31.**
6. **The approval floor holds.** `email.unsubscribe` resolves to
   `REQUIRE_APPROVAL` under the default ruleset and under every profile, the
   `mailto` send and both label actions keep their Milestone 18 floors, and
   no standing authorization satisfies any of them. Registered as
   `gate.email.unsubscribe_approval_floor`, case. **M31.**
7. **Gesture consent is exact.** A consent is immutable, expires after 120
   seconds, and resolves a pending approval only when tool, normalized
   arguments, owner, authority, revisions, and expiry all match, after
   `approval.requested` is recorded and again at the pre-effect boundary;
   any mismatch, expiry, or revocation produces no network request and no
   mailbox write; and refresh, model work, and mail content cannot create a
   consent. Registered as `gate.email.unsubscribe_gesture_consent`, case.
   **M31.**
8. **Chat approval is by value.** One invocation carries one to twenty-five
   unique targets; its approval view names each sender, address, account,
   mechanism, and destination host and never the path or query; withheld
   values stay verifiable by digest; and changed arguments or evidence void
   the approval. Registered as `gate.email.unsubscribe_batch_approval`, case.
   **M31.**
9. **Recovery is idempotent.** A lost response, a timeout, or a crash after
   the effect watermark re-executes the same fixed request, never dials an
   already accepted target again, persists each target's outcome once, and
   replays an identical command's durable result. Registered as
   `gate.email.unsubscribe_idempotent_recovery`, case. **M31.**
10. **The `mailto` path is closed.** The message is exactly the verified
    single recipient, subject, and body within their bounds, dispatched by
    value through the originating account's send server; an outcome lost
    after dispatch is `uncertain` and is never sent again automatically.
    Registered as `gate.email.unsubscribe_mailto_closed`, case. **M31.**
11. **Label actions are fixed deltas over server-selected threads.** Report
    spam adds `SPAM` and removes `INBOX`, Not spam restores exactly the
    recorded threads, and cleanup removes `INBOX`; thread sets come only from
    the run's own governed search, post-filtered by identity, at most 25 per
    call and 100 per gesture; callers supply neither threads nor labels; and
    a crafted identity cannot widen the set. Registered as
    `gate.email.unsubscribe_label_actions`, case. **M31.**
12. **The census is a deterministic projection.** Over generated duplicate,
    reordered, and resynchronized observations the same records result;
    identity is the account-qualified list identifier or sender address;
    volume counts distinct threads inside the ninety-day window; Spam, Trash,
    and owner-sent mail never form a record; and the census makes no model
    call, reserves no automatic-email dollars, and issues no query of its
    own. Registered as `gate.email.unsubscribe_census_projection`, property.
    **M31.**
13. **Owner decisions are durable.** Keep, an accepted unsubscribe, and a
    spam report survive new mail, re-import, and resynchronization without
    re-suggestion; reversing Keep restores the row; and protected senders
    sort last, are skipped by Select all, and remain individually
    actionable. Registered as `gate.email.unsubscribe_durable_decisions`,
    case. **M31.**
14. **Outcomes and follow-up are honest.** Only a 2xx response records
    `unsubscribed`; every other observation records `failed` with a closed
    content-free code; mail dated after the grace period marks
    `still_sending` and mail inside it changes nothing; and the follow-up
    dispatches nothing. Registered as `gate.email.unsubscribe_outcome_honesty`,
    case. **M31.**
15. **Routes are flagged, scoped, and bounded.** The four routes are absent
    without the flag; each requires its exact email scope and current
    account authority for what its mechanism calls; every command has
    validation, authorization, conflict, failure, and retry coverage; and
    responses are private, never include the address, and hide foreign
    subscriptions as 404. Registered as
    `gate.email.unsubscribe_routes_scope_and_flag`, structural. **M31.**
16. **The Chat tools are confined.** `email.subscriptions` is read-only with
    `EXTERNAL_UNTRUSTED` output and never returns the address;
    `email.unsubscribe` registers with exactly its declared tuple; and with
    the flag unset neither tool is registered or advertised. Registered as
    `gate.email.unsubscribe_chat_tools`, case. **M31.**
17. **Privacy holds under adversarial mail.** The address and its token
    reach no model context, approval view, response, notification, log, or
    metric; sender identity reaches no log, metric, or notification; body
    instructions and forged headers can neither trigger nor redirect a
    request; and source exclusion and principal erasure remove evidence,
    tool-event copies, and every record this milestone adds. Registered as
    `gate.email.unsubscribe_privacy`, case. **M31.**
18. **Persistence agrees on both adapters.** Subscription, consent, and
    operation records pass one contract suite on the in-memory and
    PostgreSQL stores with tenant and principal predicates and forced
    row-level security, and identical identities in different accounts never
    collide. Registered as `gate.email.unsubscribe_persistence_parity`, case.
    **M31.**
19. **The native experience is complete and honest.** iPhone, compact and
    regular iPad, and Mac present the Subscriptions view, the thread action,
    and the confirmation naming each mechanism; rows settle from the durable
    operation and restore with an actionable error; selection stops at
    twenty-five and skips protected rows; and a server or account without
    support shows nothing in its place. Registered as
    `gate.email.unsubscribe_native_experience`, case. **M31.**
20. **Integrated release evidence.** Every gate above, the local,
    PostgreSQL, and native lanes, and an owner-authorized real-mailbox smoke
    on both accounts — one accepted one-click request, one `mailto`
    unsubscribe, one spam report reversed by Not spam, and one cleanup —
    pass, with final-head hosted review and merged-revision production
    evidence. Registered as `gate.email.unsubscribe_release_evidence`, case.
    **M31.**

These twenty registry-backed gates are the milestone's blocking delivery
contract. They do not advance the verified gate ceiling, which still moves
only in milestone order. Pending check references remain pending until
matching implementation and verification exist, and no fake provider stands
in for the real-mailbox smoke.

## Tracked metrics

- **Mechanism mix** — subscriptions by `one_click`, `mailto`, and `none`,
  with the link-only share of `none` counted separately. That share is the
  measured case for or against the deferred web-form work.
- **Outcome mix** — outcome codes per request. A large
  `redirect_refused` share would be evidence to revisit that row with the
  owner, not licence to follow redirects.
- **Still-sending rate** — senders that kept mailing after an accepted
  request.
- **Gesture to outcome** — time from consent to a settled operation.
- **Consent refusals** — expiries and mismatches; any non-zero mismatch
  count is a defect to investigate.

## Build sequence

1. This document, ADR-0112, and the twenty registry entries, checks pending.
2. The read contract: the `bulk` block and `get_unsubscribe` against the
   fake provider, with the eligibility property. Gates 1 and 2.
3. The domain values, the store kinds on both adapters, and the census
   projection with verification inside refresh. Gates 12, 13, and 18.
4. The public-HTTPS unsubscribe transport, `email.unsubscribe`, its
   registration pin and approval floor, each landing with the specification
   it amends. Gates 3, 4, 5, 6, 8, 9, and 16.
5. Consent, the typed tasks, the `mailto` path, the label actions, and the
   follow-up. Gates 7, 10, 11, and 14.
6. The routes, the flag, the projections, `email.subscriptions`, and the
   privacy suite. Gates 15 and 17.
7. The native Subscriptions view and thread action on the existing Swift
   lanes. Gate 19.
8. Integrated evidence and the owner's real-mailbox smoke. Gate 20.

## Decisions

1. **The destination is never an argument.** A tool that accepted an address
   would make every injected instruction a potential dial. Binding the
   approval to an evidence digest gives by-value approval without putting
   the address anywhere a model or an approval view can see it.
2. **Verify ahead, dispatch from the store.** Headers are immutable per
   message, so verifying inside refresh costs nothing in freshness and lets
   one builtin tool serve both a typed task and a conversation without a
   tool calling a tool.
3. **`IDEMPOTENT`, because it is.** The alternative, `NON_IDEMPOTENT`, would
   route every timeout to a human review that has nothing to review: an
   unsubscribe cannot be read back.
4. **A redirect is a failure.** The specification forbids it and the
   platform follows none anywhere. If real senders redirect after accepting,
   the outcome metric will show it and the owner can decide with evidence.
5. **A second transport, not a wider allowlist.** An open destination set
   cannot be listed, and widening the operator allowlist to express it would
   weaken the sandbox's boundary to serve a tool that never runs in one.
6. **The census rides refresh.** A dedicated discovery query would find more
   senders sooner and would also be a second pager with its own cursor,
   budget, and failure modes. Coverage is a metric first.
7. **Identity is the list, then the address.** Domain grouping merges
   unrelated lists on shared hosts, and per-message grouping is no census at
   all.
8. **Not spam is a command.** A one-tap action that starts a thirty-day
   deletion clock needs its inverse in the same place.

## Open questions

1. Whether the census needs a discovery query of its own — for example over
   Gmail's promotional category — if refresh-borne coverage proves thin on
   the owner's real mailboxes.
2. Whether `redirect_refused` is common enough among otherwise compliant
   senders to deserve a distinct *request delivered, outcome unknown* state.
3. Whether Report spam should also be offered on a bulk thread that belongs
   to no subscription, which today it is not.
