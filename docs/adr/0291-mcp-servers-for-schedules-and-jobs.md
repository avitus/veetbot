# ADR-0291: MCP servers for scheduled runs and saved jobs

- Status: Accepted by owner request on 2026-11-03
- Date: 2026-11-03
- Related: ADR-0123, ADR-0153, ADR-0213, ADR-0254, ADR-0255, ADR-0268, ADR-0271
- Amends: ADR-0153's "a scheduled run is not offered the tools"; ADR-0255's
  owner-added tools reaching every session; ADR-0268's four broker calls
- Detailed design: `docs/plan/scheduling.md`, `docs/plan/cockpit.md`,
  `docs/plan/tool-system.md`

## Context

The owner asked for MCP tools in scheduled runs and in Cockpit widgets. Three
paths existed, and none of them did this deliberately.

A scheduled run's principal holds exactly its revision's `requested_scopes`.
`schedule.create` asks for none of the `mcp.<server>.use` scopes, so an
operator server (Gmail, Scale VP) never reaches a scheduled run. The Scale VP
scope is also absent from the schedule role's snapshot, so a revision asking
for it fails as `schedule.scope_revoked`.

An owner-added server (ADR-0255) requires no scope, so nothing narrowed it: its
tools reach every scheduled run that discovers them. No one chose that, and
nothing in an approval shows it.

A widget has two data paths, both through feeds. An agent-run feed schedule is
a scheduled run, with the same gap. A saved job has four broker calls, and
ADR-0268 refused "any tool through the pipeline" because a job has no model to
judge an approval.

The owner chose two things on 2026-11-03: each schedule names its servers, and
widgets get MCP data through feed schedules and through saved jobs.

## Decision

1. **A schedule names its servers.** `ScheduleDefinition` and
   `ScheduleRevision` gain `mcp_servers`, a set of server ids, empty by default
   and absent from stored and hashed definitions while empty. `schedule.create`
   takes `use_mcp_servers`, and `schedule.update` may replace the set; the
   approval view lists them. The model still never names a scope: each server's
   `required_scopes` join `requested_scopes`, and an update removes the scopes
   of the servers it drops. `requested_scopes` may still hold a scope no named
   server explains, because call and Gmail schedules do (ADR-0213); the caller
   must hold it, and the model has no way to ask for it.

2. **Who may be named.** An enabled row that is owner-added, a Gmail server or
   Scale VP. Anything else is `schedule.mcp_server_unavailable`. Calling keeps
   its own flag (ADR-0213), so a Bland server cannot be named. A job schedule
   names none, because its grant does (decision 5).

3. **Owner-added tools follow the same choice.** A scheduled session records
   its set as `schedule_mcp_servers`, and the planner offers an owner-added
   server's tools there only if it is named. A delegated child of a scheduled
   run inherits the set; a call plan's report run records none, because it has
   no scopes to withhold a server that needs none. Chat is unchanged. An
   existing schedule therefore loses
   owner-added tools no one selected, which this ADR intends.

4. **The schedule role vouches for Scale VP.** The role mirrors
   `AGENT_SVP_ENABLED`, without `SVP_CREDENTIAL_FILE`, and adds
   `mcp.svp_read.use` to its snapshot, as it already does for Gmail. The release
   refuses a mismatch. The worker alone holds the credential.

5. **A job reads MCP through a fifth broker call, and only reads.**
   `JobAccess.mcp_servers` lists the servers a grant may read. A non-empty list
   needs approval and binds the grant to its code revision, like a host or a
   feed. The list is private, so the public `job.json` declares only the
   `mcp.read` call. The SDK's `job.mcp.read(tool, arguments)` returns
   `{content, structured}`. The broker refuses a server outside the grant, counts
   the call against `maximum_requests` and bounds the answer by
   `maximum_response_bytes`.

   ADR-0268's objection stands for anything that could need a decision, so the
   reader offers only what needs none. The tool's class must be a read
   (`none`, `workspace_read` or `network_read`), low risk and read-only, its
   scopes must be held, and the policy engine must answer `allow`. Anything else
   is `job.mcp_refused`; nothing waits for an approval. A Bland server is never
   grantable.

6. **A job schedule derives its scopes.** `pin_job` and the schedule service add
   each granted server's `required_scopes` to the `{jobs.run}` set they already
   derive, so a schedule that fires holds exactly what its grant reads. Switching
   a feed schedule to a job clears the set the schedule named.

7. **Widgets are unchanged.** Widget code stays a pure function of its declared
   feeds ("Feeds over fetching widgets"). MCP data reaches a widget only through
   a feed that a schedule or a job publishes.

8. **Writes still ask.** A named server classified `changes`, or `gmail_send`,
   stays `REQUIRE_APPROVAL` in a scheduled run. A feed refresh that needs an
   approval waits for it, and its next interval skips while it overlaps.

## Consequences

Rolling back below this decision is refused while a stored revision names a
server, because the older code cannot read one. It also refuses to start with
`AGENT_SVP_ENABLED=1` in the schedule environment, so the runbook removes that
line with every unit stopped and restores it if the rollback fails. A saved job
that declares `mcp.read` fails on such a release.

A schedule can read a mailbox or a data service with the owner's knowledge at
approval. A run that reads untrusted text holds only the servers it was given.

A grant holding both a server and a host can carry MCP data to that host, because
the code is the agent's. The approval says so explicitly, and it remains the
owner's decision, as it is for two hosts.

A job's MCP reads are recorded by the existing `job.broker.*` events: operation,
outcome and time, never arguments. They do not appear as `tool.call.*` events,
because a job takes no model step.

Native schedule screens do not edit the set in this change. Schedules are chosen
in Chat, and the route accepts the field.

## Alternatives considered

- **All connected servers in every scheduled run.** Simpler, but every run that
  reads untrusted text could reach every mailbox read. The owner declined it.
- **Give owner-added servers a scope.** One mechanism for both kinds, but their
  tools deliberately need no scope (ADR-0255), and the schedule role would have
  to read `mcp_servers` to vouch for them, widening a least-privilege role.
- **Let a job call any MCP tool and park it for approval.** A job holds a
  sandbox and a deadline while it waits, and approves nothing itself.
- **MCP directly from widget code.** It reverses "feeds over fetching", gives a
  browser context a path to the owner's servers, and has no budget.
- **Declare servers in `job.json`.** Symmetric with feeds, but that file is
  public, and a server name can reveal a private integration.
