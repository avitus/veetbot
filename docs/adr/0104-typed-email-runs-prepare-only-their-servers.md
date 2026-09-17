# ADR-0104: Typed Email runs prepare only the MCP servers their task calls

- Status: Proposed
- Date: 2026-09-16
- Related: ADR-0030, ADR-0092, ADR-0095, ADR-0103 (refined by this decision)
- Detailed design: `docs/plan/tool-system.md`, `docs/plan/email-experience.md`

## Context

ADR-0103 stopped Email admission from starting MCP servers. It left the worker
starting "its own transports" when it executes the run, and the worker still
starts every enabled server. Before the task runner is invoked, the executor
builds the session's context plan. Building a plan opens the skill catalog,
which runs MCP discovery for every enabled server. It also attaches device tools
and recalls a memory snapshot.

In production on 2026-09-16, archive run `4369fab1` logged `run.started` at
18:29:43.2 and its six `mcp.server.connected` events at 18:30:00.9–18:30:01.6.
The worker spent 17.7 seconds starting three pairs of Gmail stdio processes
under the two-slot stdio startup limit, about six seconds per pair. The run
used only `gmail_work_read` and `gmail_work_write`. Closing the six transports
afterwards added another 4–7 seconds before the worker claimed its next run.

`docs/plan/tool-system.md` requires discovery of every configured server at
session open, because the plan must pin MCP tools before they can be
advertised. Typed Email work advertises nothing. Its model requests carry no
tools, and `render_email_context` renders neither tools nor skills. It does
render the plan's persona row and memory snapshot. Each task kind calls a
fixed set of servers:

| Task | Servers called | Model calls |
| --- | --- | --- |
| refresh | the read server of each admitted account | assessments, automatic drafts |
| archive | the consent account's read and write servers | none |
| send | the draft account's read and send servers | none |
| draft | none | the draft itself |

## Decision

Typed Email runs prepare only the MCP servers their task kind calls.

1. **Operational sessions have no tool surface in their plan either.** A plan
   for an `email_operational` session pins no tools and an empty skill catalog.
   Building it starts no MCP server and attaches no device tools. The plan
   still carries the persona row and the memory snapshot, which refresh
   assessments and automatic drafts render.
2. **The executor tells the planner when a run is typed Email work.** It checks
   before planning. For a typed run, the planner reuses an existing plan without
   reopening the session's skill catalog or device tools.
3. **The task runner prepares its own servers.** Before touching the mailbox, a
   typed run prepares the servers in the table above, concurrently within the
   existing preparation slots. A draft run prepares none. Connection,
   discovery, mapping, registration, catalog hashing and connection events are
   unchanged for each prepared server.
4. **The MCP runtime records preparation per server.** A later full
   preparation for the same session prepares only the servers not yet
   attempted, in configured order. This happens, for example, when Chat opens
   a thread's catalog in the same worker. A server that failed to connect counts
   as attempted, as it already does under full preparation.
5. **A new thread-bound session still pins its full surface.** When a typed run
   creates the first plan for a thread-bound session, the planner discovers
   every server. Later Chat runs reuse that plan's pinned tool set.

Consent, freshness, fencing, approval, policy and idempotency checks are
unchanged, as are uncertain-effect classification, the exact
`email_tool_pins` comparison and the run's restored tool-pin check. The context
builder version is unchanged too, so existing plans are not rotated. A run
suspended before this change resumes against the pins it recorded.

## Consequences

- An archive starts two stdio servers instead of six, which is one pair under
  the startup limit. It closes two transports afterwards instead of six.
- A refresh starts one read server per admitted account.
- A send in a thread session that already has a plan starts its read and send
  servers. Regenerating a draft in such a session starts no server.
- The first typed run in a new thread-bound session, usually the first
  requested draft, still discovers every server.
- Operational sessions created before this change keep their recorded plans,
  including their pinned builtin tools. The plans are never shown to a model.
- Archive and send runs still recall a memory snapshot they do not render. The
  plan belongs to the session, and the session does not record its task kind.
  That cost is unchanged and was not measured here.

## Alternatives considered

- **Prepare each server lazily on its first call.** An archive would start its
  write server only after its reads finished, which serializes the one pair
  the startup limit allows.
- **Cache discovery across sessions.** ADR-0103 rejected this because it
  changes the rule that each session discovers its own servers.
- **Plan thread-bound sessions without a surface, then rotate on the first Chat
  run.** That would also spare the first draft. It needs a new persisted plan
  field and a rotation rule that interacts with restored tool pins, so it is
  deferred.
- **Skip the memory snapshot for typed runs.** Refresh assessments and drafts
  render it.
