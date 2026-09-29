# ADR-0064: Milestone 14 inbound surfaces and pairing

- Status: Proposed; amended 2026-09-28 (decision 13 accepted by the owner)
- Date: 2026-08-20
- Related: Sections 16, 21, 22, 27.5, and 29 of the engineering plan;
  ADR-0004, ADR-0011, ADR-0017, ADR-0034, ADR-0049, ADR-0058, ADR-0059,
  ADR-0061, ADR-0062
- Detailed design: `docs/plan/inbound-surfaces.md`

## Context

Section 29.4 names Surfaces — inbound messaging channels — as devices with an
empty capability set unified under one session-key resolver, and requires an
unknown sender to be default-denied until an explicit pairing step completes.
ADR-0017 decided the pairing shape; the seam audit (ADR-0034) found that the
session-key resolver is the one genuinely new mechanism in Section 29, that
pairing needs a home and an endpoint, that no client is attributed on a write,
and that the corpus had no rule for the trust label of a paired third party.
Milestone 12 (ADR-0062) landed the device registry and the notification outbox
the surface reuses.

The owner authorized Milestone 14 on 2026-08-20 (ADR-0061) as the third
roadmap milestone, with a Telegram bot as the first channel, so the agent can
be reached from a phone without the native client.

## Proposed decisions

1. **A Surface is a Device with an empty capability set.** One `devices` row
   per configured bot with `kind = surface`, `platform = telegram`, and
   `push_provider = telegram` — the kind and provider Milestone 12's closed
   enums declare for this purpose — so the row is representable by the
   Milestone 12 contract; no second model, as ADR-0034 decided.
2. **Long polling, not a webhook.** The API stays loopback-only behind the
   proxy; the surface role polls, resumes from the last committed update, and
   holds a per-surface advisory lock so two workers are safe. A webhook is a
   second implementation of the same port if ever needed.
3. **A dedicated least-privilege `surface` role holds the bot token.** The
   token comes from an owner-only private file into a secret field, never the
   credential map; the role has the database credential and nothing else; it
   runs Milestone 12's dispatcher for the Telegram provider only (dispatch is
   partitioned by provider) and drains the surface-reply outbox, so the token
   lives in one process and the push key in another.
4. **Pairing as ADR-0017 decided, given a home.** A one-time code minted by an
   authenticated principal with `surface.write`, at least forty bits, salted
   hash, ten-minute expiry, five attempts, one-hour per-sender lockout,
   returned once, bound to the surface and sender with `granted_scopes` no
   wider than the minter's; revocation effective before the next message.
5. **The session-key resolver.** One live mapping per `(surface,
   external_key)`; `dm:<chat_id>` built, group and thread shapes reserved;
   rotation on `/new`, idle time, a closed session, or a stale pinned agent
   version; a rotated key is never reused.
6. **A paired message is an ordinary submission through one shared function.**
   The HTTP submit body is extracted and shared with the surface ingress and
   the CLI; no second path. A busy session rejects rather than queues.
7. **One ingress transaction per update, receipt first.** The receipt keyed by
   `(surface, update_id)` is the idempotency boundary; the poll offset advances
   only from committed receipts; rejection of an unpaired, locked, or
   rate-limited sender happens before any content write.
8. **A paired sender's message is `USER` for the bound principal.** Pairing is
   authentication; the owner pairing their own account is the case this
   milestone serves. Pairing a third party to the owner's principal is a
   widening that needs explicit owner approval and is bounded by
   `granted_scopes`; a dedicated third-party label is roadmap item B3.
9. **Attribution on the write.** Origin on the seed message and the queued
   run, the receipt as the reverse map, the surface in session metadata; no
   session or run column, because a session is not owned by a channel.
10. **Notifications through the Milestone 12 outbox; replies through a
    separate surface-reply outbox.** A reply is not a notification and never
    enters Milestone 12's closed trigger catalog; both are redacted with the
    secret-rule families and chunked to Telegram's limit with per-chunk
    progress; approvals by `/approve` and `/deny` through the existing service;
    questions by a plain reply through the existing input rule; inline
    keyboards deferred. Because notification payloads are content-free, the
    surface role reads question text and approval summaries through the
    existing application services as the paired principal under the pairing's
    scopes, and sends a generic notice when it lacks them.
11. **Two scopes, six routes, two flags, default-off.** `surface.read`,
    `surface.write`; pairing routes and CLI; `AGENT_SURFACE_API_ENABLED` and
    `AGENT_SURFACE_WORKER_ENABLED` changing together.
12. **One gate area, `surface`, twenty-one gates.**

## Consequences

- The owner can message the agent from a phone and receive replies,
  approvals, and questions there; an unknown sender reaches nothing.
- Six tables, two scopes, six routes, one role, one unit, one environment
  file, a `surfaces:` limits block, a `telegram_bot_token` secret-rule family,
  and one transport adapter are added; the public run submission body becomes
  a shared application function. The Milestone 12 outbox and its trigger
  catalog are unchanged.
- The owner must create the bot and place its token in a private file on the
  host.

## Alternatives considered

- **Webhook delivery:** rejected for this deployment; public ingress into the
  API process or a third virtual host, coupled to API availability.
- **Routing inbound messages through the API with a stored bearer token:**
  rejected; it turns a worker into a credential holder and bypasses the
  pairing boundary.
- **A new trust level for paired senders:** deferred; the only sender in this
  milestone is the principal, and `granted_scopes` is the right lever for
  anyone else.
- **Origin columns on sessions and runs:** rejected; a session may be continued
  from another client, so attribution belongs on the write.
- **Inline-keyboard approvals:** deferred; a second resolution entry point for
  no added authority.

## Amendment — 2026-09-28

On PostgreSQL every paired message was refused (`REJECTED_ADMISSION`,
`surface.cost_reservation_missing`): admission reserved the default agent's
cost limit, and the shipped agent has none. The in-memory admission controller
allows everything, so no gate saw it. The owner chose a surface budget over a
cost limit on the default agent, which would have capped every chat, reversed
ADR-0130's "every other chat has no cost limit", and changed the
content-addressed agent version.

13. **A surface run has its own budget.** (Owner, 2026-09-28.) The
    `surfaces:` limits block gains `max_cost_per_run` (USD 10) and
    `synthesis_reserve_cost` (USD 2). Admission reserves the budget and the
    run a paired message starts carries it as its cost limit and synthesis
    reserve, unless its agent's own limit is lower. The reservation therefore
    bounds what the run can spend. With the USD 25 daily ceiling, two surface
    runs can be in flight at once. Chats started from any other client keep
    the ordinary limits. The ingress requires the budget and the self-approval
    setting as arguments, so neither can be left unwired again.

Engineering decisions made while resolving the review findings pull request
104 left open:

14. **An unreadable update is receipted, not retried.** A Telegram update with
    an identifier but no readable sender, or one that is not a message,
    receives an `IGNORED_UNREADABLE` receipt (migration `c978eb0bac0f`), so
    the poll offset passes it rather than stalling on it.
15. **Replies have a terminal attempt limit.** A reply still undelivered after
    `reply_max_attempts` (eight) claims settles as failed.
16. **A repeated pairing key is a non-retryable conflict.** Replaying the
    response would present the code twice, which decision 4 forbids. The 409
    names the issued code's identifier and expiry.
17. **Credentials stay with their role.** The surface loader refuses a
    provider credential variable, and every other role refuses a surface
    secret-file variable, instead of ignoring it. The call roles still ignore
    stray provider variables until their production environment files are
    confirmed clean.

Consequences: one limits key pair, one reply limit, one receipt disposition
and migration, and one reason code (`surface.unavailable`, a pairing attempt
on a revoked surface). `surface.cost_reservation_missing` no longer exists.
