# ADR-0074: Milestone 21 WhatsApp business surface

- Status: Proposed
- Date: 2026-08-26
- Related: engineering plan Sections 21, 22, and 29; ADR-0061, ADR-0064,
  ADR-0071
- Detailed design: `docs/plan/whatsapp-surface.md`

## Context

On 2026-08-26 the owner asked for WhatsApp as an urgent channel. Reading
the owner's personal account has no official API; the owner chose the
phased route: the official Meta Business Cloud API first — the agent's
own number, ToS-clean — with the unofficial linked-device bridge deferred
behind explicit risk acceptance. The Cloud API delivers inbound messages
by webhook only, which makes this milestone the one that pays the
inbound-webhook price ADR-0071 named as B3 and B4 infrastructure.
inbound-surfaces.md anticipated exactly this extension: a webhook
transport as a second implementation of the same port, with its
secret-token check and proxy route. The roadmap rule requires the
owner's authorization and a specification with gates; this ADR is the
authorization and whatsapp-surface.md is the specification, with twelve
gates in a new `whatsapp` area.

## Proposed decisions

1. **Parallel workstream with a stated implementation dependency.**
   Milestone 21 is authorized on the established terms — independent
   gates, the ceiling advances only in numerical order. Unlike the prior
   parallel workstreams its implementation cannot begin immediately: the
   listener and adapter live inside Milestone 14 deliverables. The
   documents, gates, Nginx preparation, and the Meta ceremony proceed
   now; code begins when the surface ports exist. Nothing amends
   Milestone 14's acceptance criteria.
2. **Webhook ingress in the surface role, never the API process.** A
   loopback-bound listener behind one Nginx location on the API virtual
   host; handshake and constant-time signature verification before any
   parse; bounded bodies; content-free rejects. The firewall's inbound
   set stays 22, 80, 443, and the loopback-only structural posture
   holds.
3. **The adapter is additive on the Milestone 14 ports.** A `whatsapp`
   push provider, `dm:<wa_id>` keys, unchanged pairing and scope
   ceiling, the existing reply redaction and chunking.
4. **The receipt key generalizes.** `update_id BIGINT` was a Telegram
   `getUpdates` artifact; the receipt key becomes
   `external_update_id TEXT` so a webhook channel keys its provider's
   message identifier. The amendment is made in inbound-surfaces.md by
   this change-set — Milestone 14 is unimplemented, so no migration
   changes — and this ADR is its record. This is one of three shared
   files this milestone touches, named here rather than discovered in
   review: inbound-surfaces.md, the devices domain enum, and the Nginx
   configuration.
5. **The twenty-four-hour window is designed, not discovered.** Freeform
   inside the window; outside it, one approved content-free utility
   template or refusal with a closed reason code.
6. **Three broker-held secrets with rule families.** Access token, app
   secret, verify token: private-file loading, dedicated fields, scanner
   and redaction coverage, and egress confined to the fixed Meta Graph
   origin.
7. **Default off.** `AGENT_SURFACE_WHATSAPP_ENABLED` gates listener,
   adapter, and provider registration together. This follows the
   schedule and notification routers exactly.
8. **Twelve gates open the `whatsapp` area**, registered at Milestone 21
   against the design's hard-gates section; the gate-identifier grammar
   gains the area.

## Consequences

- The census grows from 365 to 377 registry entries; `whatsapp` is a new
  gate area with its own registry file.
- The corpus gains its first designed inbound third-party HTTPS surface,
  confined to one signed route in a least-privilege role.
- The linked-device bridge enters the roadmap as B13 with a
  risk-acceptance entry condition, so the ToS-violating route can never
  arrive silently.
- The Meta ceremony's review lead time starts now instead of after
  Milestone 14.

## Alternatives considered

- **The linked-device bridge first:** rejected for phase one; it is a
  ToS violation with account-ban risk, and the owner chose to take that
  risk decision separately (B13).
- **Folding the adapter into Milestone 14:** rejected; it would rewrite
  a finished specification and put webhook ingress on the critical path
  of the seam every channel depends on.
- **Webhook termination in the API process:** rejected; it would add an
  unauthenticated public route to the API and couple inbound delivery to
  API availability — the exact objection ADR-0064 recorded.
- **A polling workaround:** rejected as impossible; the Cloud API has no
  polling mode.
- **Twilio's WhatsApp gateway instead of Meta directly:** rejected; it
  adds a paid intermediary, still webhook-delivered, without removing
  any constraint.
- **A `gate.surface.*` home for the new gates:** rejected; keeping
  Milestone 14's area closed at its own twenty-one keeps both
  milestones' evidence legible.
- **One combined milestone for SMS and WhatsApp:** rejected; the two
  seams share no dependency, and the WhatsApp half cannot start before
  Milestone 14 exists, which would leave a combined milestone half-open
  indefinitely.
