# ADR-0103: Email admission never discovers MCP servers under the owner lock

- Status: Accepted — owner chose this design and accepted it on 2026-09-16 after production measurements
- Date: 2026-09-16
- Related: ADR-0030, ADR-0071, ADR-0092, ADR-0095
- Detailed design: `docs/plan/email-experience.md`, `docs/plan/skills.md`

## Context

Email refresh and archive admission each created a new operational session.
Creating a session opened its skill catalog, and opening a catalog started every
enabled MCP server to discover its prompts. In production that meant six Gmail
stdio servers, admitted two at a time, each exchanging its OAuth refresh token
before answering. The work took 10–16 seconds and ran while the transaction held
the owner's email advisory lock.

On 2026-09-16 between 18:27 and 18:32 UTC, every thread read waited for exactly
those windows. Archive `4369fab1` was admitted at 18:28:20.2 and its session was
created at 18:28:33.7; no thread read completed between 18:28:20 and 18:28:34.
Archive `1f92a777` showed the same pattern from 18:29:00.6 to 18:29:17.0. After
commit, releasing the six transports one at a time added about eight seconds, so
each archive request took 23–25 seconds. Status polls, list reconciliation and
the archive worker each held the lock for about a millisecond.

Every catalog pinned this way was empty. The Gmail servers advertise no prompts,
the production agent enables no skills, and typed email tasks never render a
skill catalog: `render_email_context` builds its prefix without tools or skills.
The worker starts its own MCP transports when it executes the run.

## Decision

A session created for typed operational email work has no model-visible surface.
It records an empty skill catalog and starts no MCP server. This applies to
refresh, archive and the source-exclusion audit session.
These sessions carry `email_operational` metadata. The worker still prepares its
MCP transports and tool pins when it executes the run. Skill pinning for every
other session is unchanged.

A thread-bound session can later open in Chat, so it keeps the full catalog
required by `skills.md`. Email opens that catalog before taking the owner lock.
Under the lock it re-reads the thread. If the prepared catalog is not consumed,
for example because a concurrent request already bound a usable session, Email
discards it and closes its transports after the lock is released. A rolled-back
admission discards it the same way. A session found unusable only after the lock
was taken falls back to opening in place. That path is correct but slow, and it
requires a concurrent session closure.

Admission keeps every consent, freshness, fencing, idempotency and authorization
check. Only where catalog discovery runs has changed.

## Consequences

- Archive and refresh admission no longer start Gmail processes or exchange OAuth
  tokens in the API process. The owner lock is held only for durable admission.
- Thread detail, list reconciliation and status reads no longer wait behind MCP
  discovery during admission.
- Sessions created with `email_operational` metadata record `skill_pins: []`
  even if the agent enables skills. Those skills would never have been shown to a
  model in these sessions.
- A thread-bound request may occasionally prepare a catalog it then discards.
  That costs discovery time but holds no lock and leaves no durable state.

## Alternatives considered

- **Discover outside the lock for every session.** Reads would stop blocking,
  but archive and refresh requests would still take 15–23 seconds. The API would
  also still start six Gmail processes for each admission.
- **Reuse one operational session.** A session admits one active run at a time,
  and concurrent archives are normal.
- **Cache MCP discovery across sessions.** That would change the tool-system
  rule that each session discovers its own servers, to optimize a catalog that
  is never rendered.
