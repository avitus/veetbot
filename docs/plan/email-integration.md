---
title: Email Integration
status: design
canonical: true
---

# First-class email integration

This specification expands the engineering plan's Milestone 18 and roadmap
item B11's email half, and records the mechanism selected by
[ADR-0071](../adr/0071-milestone-18-email-integration.md): the owner's Gmail
mailbox reaches the agent as three first-party MCP servers, operator-configured
over stdio, carried entirely by the Milestone 8 adapter. The platform gains no
builtin email tool, no provider port, and no Gmail type inside `agent_core`.

## Scope

The milestone delivers four capabilities over one mailbox, the owner's Gmail
account, through eight tools on three servers:

| Capability | Served by |
| --- | --- |
| Read and triage | `gmail_read` — search, thread reading, label listing |
| Draft | `gmail_write` — draft creation |
| Organize | `gmail_write` — batch label changes, archive, trash, untrash |
| Send | `gmail_send` — one send tool, always approval-gated |

Proactive monitoring is a documented recipe over existing schedules and
notifications, not new runtime.

Out of scope: calendar; permanent deletion (`users.messages.delete` appears in
no roster); attachment download or upload; Gmail push and Pub/Sub; interval or
cron recurrence (roadmap B5); the email inbound Surface (B3) and the email
notification transport (B4); a second mail provider; multiple accounts; and
any auto-approval or standing-grant mechanism (B8).

## The three servers

The tool system classifies at the server level — every tool a server exposes
inherits one operator-declared side-effect, risk, and idempotency tuple — so
the roster splits by what it does to the world, one honest class per server:

| Server | Classification | Google OAuth scope |
| --- | --- | --- |
| `gmail_read` | `NETWORK_READ` / `LOW` / `READ_ONLY` | `gmail.readonly` |
| `gmail_write` | `EXTERNAL_WRITE` / `MEDIUM` / `NON_IDEMPOTENT` | `gmail.modify` |
| `gmail_send` | `EXTERNAL_MESSAGE` / `HIGH` / `NON_IDEMPOTENT` | `gmail.send` |

All three are one Python package, `src/gmail_mcp/`, run as
`python -m gmail_mcp --mode read|write|send`. The mode flag selects which
roster the process serves and which credential it expects; it is not a secret
and may live in the configured command line. The package sits beside
`agent_core`, never inside it: `gmail_mcp` imports nothing from `agent_core`
and `agent_core` imports nothing from `gmail_mcp`, and a structural gate walks
both directions. The server speaks Gmail's REST API over `httpx` and the MCP
protocol over the SDK the platform already carries; it adds no dependency.

Google's scope grammar is coarser than this split — `gmail.modify` can
technically send — so token separation is partial and deliberate: each
server's token is consented to the smallest Google scope that serves its
roster, and the platform-side classification remains the control that counts.

Each server declares exactly one required platform scope, `mcp.gmail_read.use`,
`mcp.gmail_write.use`, or `mcp.gmail_send.use`, granted to the configured
principal. Tools register under the adapter's ordinary names:
`mcp.gmail_read.search_threads` and so on.

## Tool roster

`gmail_read`:

- `search_threads(query, max_results, page_token?)` — Gmail query syntax,
  one through twenty-five results, opaque page token. Returns thread id,
  senders, subject, date, snippet, and label ids per thread.
- `get_thread(thread_id)` — every message in one thread: headers, plain-text
  body, label ids. HTML bodies are reduced to text; attachments are named
  with filename, type, and size but never fetched.
- `list_labels()` — system and user label ids and names.

`gmail_write`:

- `create_draft(to, cc?, bcc?, subject, body, thread_id?)` — a draft,
  optionally attached to an existing thread as a reply.
- `modify_labels(thread_ids[], add_label_ids?, remove_label_ids?)` — batch
  label changes over at most twenty-five threads; archive is removing
  `INBOX`, mark-read is removing `UNREAD`. At least one change list must be
  non-empty.
- `trash_thread(thread_id)` and `untrash_thread(thread_id)` — Gmail's
  reversible trash with its thirty-day grace period. Permanent deletion is
  not exposed by any server.

`gmail_send`:

- `send_message(to, cc?, bcc?, subject, body, thread_id?)` — sends one
  plain-text message, threading as a reply when `thread_id` is given. There
  is deliberately no send-a-draft tool: a send is proposed by value, so the
  complete outbound content sits in the proposed action's arguments where the
  approver reads it, rather than behind a draft id the approver cannot see.

## Credentials and the bootstrap ceremony

Gmail's OAuth is the authorization-code grant with a refresh token. That is
not the `oauth2_client` client-credentials exchange and is not forced into it;
the seam that fits without touching the tool system is the one stdio servers
already use. Each server's configuration row carries `auth_scheme: env`,
`auth_name: GMAIL_MCP_CREDENTIAL`, and a per-server `credential_ref`. The
broker resolves the reference at connect time and the adapter places the value
as that one variable in a constructed child environment — never `argv`, never
an inherited environment, never an event or a log.

The resolved value is a JSON document: `client_id`, `client_secret`,
`refresh_token`, and the granted Google scope. The server exchanges the
refresh token for access tokens against Google's token endpoint inside its own
process, checking expiry at use rather than on a timer. Access tokens, the
refresh token, and Google's error bodies never cross the stdio pipe; the
platform never learns the credential is OAuth. A refresh token Google refuses
surfaces as the adapter's ordinary `tool.server_unauthorized`, terminal at
connect and laddered mid-session, exactly as for any MCP server.

Initial consent is a one-time operator ceremony:

```text
python -m gmail_mcp bootstrap
```

runs the installed-app loopback consent flow once per server — three consents,
each requesting exactly that server's Google scope — and writes three
owner-only (0600) credential files. The files enter the broker through
file-backed settings references, the shape the browser-automation profile
credential already uses. The ceremony prints file paths and granted scopes,
never token material. The owner's Google OAuth client runs in production
publishing status (ADR-0071 decision 6), so refresh tokens do not expire on
the testing-status seven-day clock; if Google revokes one anyway, recovery is
re-running the ceremony.

## Configuration and composition

Email is disabled by default. The environment layer owns:

```text
AGENT_EMAIL_ENABLED            default off
GMAIL_READ_CREDENTIAL_FILE     path to the read server's credential JSON
GMAIL_WRITE_CREDENTIAL_FILE    path to the write server's credential JSON
GMAIL_SEND_CREDENTIAL_FILE     path to the send server's credential JSON
```

When the flag is set, the composition root synthesizes three operator-tier
stdio server rows — command, mode flag, `env` auth scheme, per-server
credential reference, classification, and single required scope as declared
above — and hands them to the MCP adapter with every other configured server.
Per-server request timeouts and `maximum_output_bytes` use the adapter's
defaults unless the deployment overrides them. When the flag is unset no row
is composed: no `mcp.gmail_*` tool exists in the registry, none is advertised,
and no `mcp.gmail_*.use` scope is granted. A missing or unreadable credential
file while the flag is set is a configuration error at composition, not a
connect failure later.

## Policy, approvals, and trust

The default deterministic matrix already decides this milestone's actions:
`NETWORK_READ` allows under `host_on_allowlist`, and `EXTERNAL_WRITE` and
`EXTERNAL_MESSAGE` require approval. No new rule is added, and no profile may
downgrade the write or send decision for these servers (ADR-0071 decision 8);
standing grants remain roadmap B8.

One condition must learn one shape it does not yet hold. `host_on_allowlist`
recognizes only the web-provider and browser-provider fixed targets, so every
`NETWORK_READ` tool executing against an `mcp` target — including the
adapter's own `read_resource` — is denied by the default ruleset today. The
condition gains an MCP arm: it holds for an `mcp` target when the executing
tool's specification declares `NETWORK_READ` and `READ_ONLY`. The
justification is the web-provider arm's own: that classification is
operator-declared at configuration time and can never be model-authored or
server-claimed, stdio command lines are operator-configured only, and
tenant-supplied HTTP endpoints were egress-validated when written. The arm
lands with the change that implements it, amending
[policy-and-approvals.md](policy-and-approvals.md) in the same commit, and
every non-read MCP call remains exactly as gated as before.

Every tool result from these servers is `EXTERNAL_UNTRUSTED` — the adapter
forces it — which is the platform's standing answer to mail as an injection
vector: fetched mail cannot become policy, configuration, a credential, or a
trusted authoring source, and the trust overlay forbids a plain allow for a
send proposed downstream of it. The hardline rule against moving
credential-shaped values into messaging actions applies to `send_message`
arguments unchanged. Approval notifications about mail remain content-free:
no subject, sender, or body fragment rides a push.

## Proactive monitoring

The recipe, not new runtime: a daily or weekly schedule whose instruction is a
triage brief — for example, *"Search the inbox for messages newer than one
day; summarize what matters; propose labels for the rest; draft replies where
obviously needed"* — materializes an ordinary run. The run reads freely under
the read server; every label change, draft, or send it proposes parks in the
approval queue; the existing approval trigger notifies the owner's phone
content-free, and the schedule-outcome trigger reports the run itself. The
cadence floor is daily until roadmap B5; Gmail push stays out with B3 and B4.

## Bounds and failures

Gmail API responses are decoded under a hard byte bound; thread bodies are
truncated to the server's declared output budget before crossing the pipe, so
one bounded result always returns. The server maps upstream failures to a
closed set of stable, content-free codes: credential rejection, rate limit,
temporary provider unavailability, permanent provider rejection, and invalid
provider output. Rate limits and 5xx are retryable; auth failures, other 4xx,
and schema-invalid arguments are not; the tool pipeline retains ownership of
any retry inside the run deadline. Google's response text, headers, and
`WWW-Authenticate` values never cross the pipe. Connect-time auth failure is
terminal for the session and mid-session 401s run the adapter's bounded
ladder, both exactly as [tool-system.md](tool-system.md) already specifies for
every MCP server.

## Acceptance criteria

- All eight tools pass one shared contract suite in all three modes against a
  fake Gmail API, and no mode's roster exposes permanent deletion.
- Registered specifications carry exactly the three declared classification
  tuples, `EXTERNAL_UNTRUSTED` output, and the per-server scope, and a
  read-only-classified server's reads execute under the default ruleset while
  writes and sends require approval.
- The credential reaches each server only as the one declared environment
  variable in a constructed environment, and no token material, upstream
  error text, or credential value ever appears in `argv`, tool results,
  events, or logs.
- With `AGENT_EMAIL_ENABLED` unset, no `mcp.gmail_*` tool, row, or scope
  grant exists.
- The bootstrap ceremony writes 0600 credential files that round-trip through
  the settings loader, requests exactly the per-server Google scopes, and
  prints no secret.
- A send proposed after reading mail cannot be plain-allowed, and a daily
  triage schedule produces reads without approvals, writes with them, and
  content-free notifications.

## Build sequence

1. This document, ADR-0071, the milestone-map area and census rows, and the
   thirteen registry entries, checks pending. **M18.**
2. The fake Gmail API fixture and the shared three-mode contract suite, red;
   then `src/gmail_mcp/` read mode green, with the two-way import-isolation
   check. **M18.**
3. Write and send modes green; roster, classification, and failure taxonomy
   observed. **M18.**
4. The `host_on_allowlist` MCP arm and the policy-and-approvals amendment, in
   one change. **M18.**
5. Composition: the flag, the credential-file settings, three synthesized
   server rows, scope grants. **M18.**
6. The bootstrap consent command. **M18.**
7. The monitoring recipe exercised end to end; gate evidence recorded; the
   owner's real-mailbox smoke. **M18.**

## Hard gates

1. **Package isolation.** `gmail_mcp` imports nothing from `agent_core` and
   `agent_core` imports nothing from `gmail_mcp`, walked in both directions
   by the architecture check. **M18.**
2. **Contract parity.** All three server modes pass the complete shared
   contract suite against the fake Gmail API and normalize to the same
   domain shapes. **M18.**
3. **Roster confinement.** Each mode advertises exactly its declared roster,
   and no mode exposes permanent deletion. **M18.**
4. **Classification.** The registered `mcp.gmail_*` specifications carry
   exactly the three declared side-effect, risk, and idempotency tuples,
   `EXTERNAL_UNTRUSTED` output, and `ToolSource.MCP`. **M18.**
5. **Read allows, write approves.** Under the default ruleset a read-server
   tool call is allowed and a write-server and send-server call each require
   approval, observing the condition's MCP arm. **M18.**
6. **Untrusted origin.** A send proposed after untrusted mail content entered
   the run cannot resolve to a plain allow. **M18.**
7. **Credential confinement.** Over generated server configurations, the
   resolved credential reaches the child only as the one declared variable in
   a constructed environment — never `argv`, never inherited, never an event,
   result, or log. **M18.**
8. **Token confinement.** Access tokens, refresh tokens, and upstream Google
   text never appear in tool results or durable events; failures cross the
   pipe only as the closed stable codes. **M18.**
9. **Default off.** With the flag unset, no `mcp.gmail_*` server row is
   composed and no such tool is registered or advertised. **M18.**
10. **Scope confinement.** Each server requires exactly its own
    `mcp.{server_id}.use` scope, and a configuration declaring a platform
    scope for a `gmail_*` server is rejected. **M18.**
11. **Bootstrap consent.** The ceremony writes owner-only credential files
    that round-trip through the settings loader, requests exactly the
    per-server Google scopes, and never prints token material. **M18.**
12. **Failure taxonomy.** Connect-time auth failure is terminal, the
    mid-session ladder is bounded, rate limits and server errors are
    retryable and stable, and oversized upstream bodies truncate within the
    declared output budget. **M18.**
13. **Monitoring recipe.** A daily triage schedule materializes a run whose
    reads pass without approval, whose first write parks an approval whose
    notification is content-free, and whose outcome is reported. **M18.**

These thirteen registry-backed gates are the milestone's blocking delivery
contract. They do not advance the verified gate ceiling, which still moves
only in milestone order.
