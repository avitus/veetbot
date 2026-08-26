---
title: The WhatsApp business surface
status: design
canonical: true
---

# The WhatsApp business surface

## Authorization and context

The owner authorized this milestone on 2026-08-26 (ADR-0074) as a parallel
workstream on the established terms. It gives the agent its own WhatsApp
number through the official Meta WhatsApp Business Cloud API, as an
additive channel on the Milestone 14 surface seam — the extension
[inbound-surfaces.md](inbound-surfaces.md) designed for, including its
own anticipation of "a webhook transport … a second implementation of the
same port, to be added with its secret-token check and proxy route".

Two facts shape everything here. First, the Cloud API delivers inbound
messages by webhook only; there is no polling mode, so this milestone
deliberately pays the inbound-webhook price ADR-0071 named as B3 and B4
infrastructure. Second, a business number may send freeform messages only
within twenty-four hours of the sender's last inbound message; outside
the window, only pre-approved template messages.

Reading the owner's personal WhatsApp account is explicitly not this
milestone: no official API exists for it, and the unofficial
linked-device route is a ToS violation the roadmap holds behind a
risk-acceptance ADR.

## Sequencing

This milestone's code lives inside Milestone 14 deliverables that do not
exist yet: the surface role process, the five surface ports, the pairing
and receipt tables. The documents, gates, Nginx preparation, and the
owner ceremony — Meta business account, phone number, WhatsApp Business
Account, access token, and utility-template approval, which has real lead
time — proceed now; implementation begins when Milestone 14's ports are
real. Nothing here amends Milestone 14's acceptance criteria.

## The webhook ingress

A loopback-bound HTTP listener runs inside the Milestone 14 surface role
(`agent worker --role surface`) — never in the API process — exposed as
one Nginx location on the existing `api.veetbot.com` virtual host
proxying to it. The firewall story stays inbound 22, 80, and 443; the
operational-hardening structural gate that keeps every bind loopback-only
holds; Nginx remains the sole public terminator, and the API process
gains no new unauthenticated route.

The listener implements Meta's subscription handshake — the GET challenge
echoed only on a constant-time verify-token match — and validates
`X-Hub-Signature-256` (HMAC over the raw body with the app secret,
constant-time) before any parse. A missing or wrong signature is a
content-free reject that stores nothing but a content-free receipt.
Bodies are bounded; anything over the limit is rejected unread.

## Credentials

Three secrets — the access token, the app secret, and the verify token —
load through the established private-file loader into dedicated secret
fields: never the credential map, never the environment, never a log.
Three new secret-rule families (`whatsapp_access_token`,
`whatsapp_app_secret`, `whatsapp_verify_token`) put them under the
scanner, export redaction, and event checks. Egress is confined to the
fixed Meta Graph API origin over HTTPS — no redirects, system trust
store, bounded response bodies — the rules the Telegram and Apple
transports already follow.

## The adapter on the Milestone 14 ports

- `PushProvider` gains a `whatsapp` member; the chat reference resolved
  at pairing is the routing token, exactly as the Telegram provider
  works. The cross-constraint that a surface provider implies
  `kind = surface` extends to it.
- The session key is `dm:<wa_id>`; the group and thread shapes stay
  reserved and rejected.
- Pairing is unchanged: `/pair <code>` over WhatsApp, the same
  default-deny before any content is stored, the same scope-ceiling
  intersection recomputed at every message, the same immediate
  revocation with session-key rotation.
- Replies drain through the surface-replies outbox with the same
  redaction families and the same chunking; WhatsApp's 4096-character
  limit matches the existing chunk size.
- Inbound idempotency keys on Meta's string message id through the
  generalized `external_update_id TEXT` receipt key (the
  inbound-surfaces amendment ADR-0074 records). Loss recovery leans on
  Meta's multi-day webhook retry plus receipt dedupe; the Telegram
  poll-offset resume does not apply to a webhook transport.

## The twenty-four-hour window

Outbound sends on a channel whose last inbound message is older than
twenty-four hours use the one pre-approved utility template — a
content-free nudge naming nothing but that an update exists — and
freeform sends are refused with a closed reason code. Within the window,
ordinary surface replies flow. Template registration is part of the
owner ceremony.

## Persistence

The adapter reuses the Milestone 14 tables — pairings, sessions,
receipts, replies — under the generalized receipt key. One addition
carries the window: the surface session's `last_inbound_at`, updated in
the same transaction as the receipt, is the window clock. All rows carry
the tenant RLS policy.

## Configuration and flags

`AGENT_SURFACE_WHATSAPP_ENABLED`, default off, gating the listener, the
adapter, and the provider registration together at release validation.
The secrets arrive as `AGENT_SURFACE_WHATSAPP_TOKEN_FILE`,
`AGENT_SURFACE_WHATSAPP_APP_SECRET_FILE`, and
`AGENT_SURFACE_WHATSAPP_VERIFY_TOKEN_FILE`. The scope vocabulary does
not grow; the surface scopes cover it.

## Exclusions

No personal-account access, read or send (the linked-device bridge is a
roadmap item behind a risk-acceptance ADR); no media intake or outbound
files beyond the Milestone 14 posture; no group or thread session keys
(roadmap B3); no inline-keyboard approvals (B3); no second business
number or multi-tenant pairing UI.

## Build sequence

1. The owner ceremony: business account, number, WABA, token, and the
   utility template through Meta review. **M21.**
2. The listener with handshake and signature verification against a fake
   Meta signing real payloads. **M21.**
3. The adapter over the Milestone 14 ports behind the flag, with the
   provider member and the generalized receipt key. **M21.**
4. The window clock and template-only enforcement. **M21.**
5. The Nginx location, the systemd environment, and the end-to-end
   verification against the live number. **M21.**

## Hard gates

1. **Signature required.** A webhook body whose `X-Hub-Signature-256`
   is missing or wrong is rejected content-free before parsing; nothing
   content-bearing is stored. **M21.**
2. **Handshake confined.** The subscription challenge is echoed only on
   a constant-time verify-token match. **M21.**
3. **Receipt idempotency.** A replayed Meta message id processes once
   through the `external_update_id` receipt key. **M21.**
4. **Unpaired denied.** An unpaired WhatsApp sender yields the rejected
   disposition and a content-free receipt, and creates nothing else. **M21.**
5. **Token confinement.** The three WhatsApp secrets appear in no event,
   result, log, or export; their rule families cover scanner and
   redaction. **M21.**
6. **Transport confined.** Adapter egress reaches only the fixed Meta
   Graph origin over HTTPS with no redirects and bounded bodies. **M21.**
7. **Template outside the window.** A send on a channel idle past
   twenty-four hours goes only through the approved template path;
   freeform is refused with a closed reason code. **M21.**
8. **Replies chunked and redacted.** Outbound replies pass the surface
   redaction families and the 4096-character chunking with per-chunk
   receipts. **M21.**
9. **Scope ceiling.** Every message resolves the pairing's granted
   scopes intersected with the principal's current scopes, fresh. **M21.**
10. **Loopback bind.** The listener binds loopback only; the structural
    check walks the bind the way the operational-hardening gate
    does. **M21.**
11. **Persistence isolated.** WhatsApp rows ride the surface tables'
    tenant RLS; a second tenant sees nothing. **M21.**
12. **Default off.** With the flag unset, no listener starts, no
    adapter or provider registers, and no route is proxied. **M21.**

## Open questions

1. Whether the utility template Meta approves can stay fully
   content-free; the ceremony records the approved wording.
2. Whether the listener should also serve future webhook channels (B3's
   Slack events, B4's inbound email) — it is designed as
   channel-agnostic plumbing, but each channel enters only with its own
   authorization.
3. Whether `last_inbound_at` belongs on the session or on the pairing —
   decided at implementation with Milestone 14's real schema in view.
