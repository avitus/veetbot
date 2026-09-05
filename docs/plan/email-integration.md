---
title: Email Integration
status: design
canonical: true
---

# First-class email integration

This specification expands the engineering plan's Milestone 18 and roadmap
item B11's email half, and records the mechanism selected by
[ADR-0071](../adr/0071-milestone-18-email-integration.md) and extended by
[ADR-0085](../adr/0085-operator-managed-multi-account-gmail.md): the owner's
operator-managed Gmail accounts reach the agent as isolated first-party MCP
server triplets, configured over stdio and carried entirely by the Milestone 8
adapter. The platform gains no builtin email tool, no provider port, and no
Gmail type inside `agent_core`.

## Scope

The milestone delivers four capabilities over one or more operator-managed
mailboxes belonging to the configured principal, through eight tools on three
servers per account:

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
notification transport (B4); a second mail provider; public self-service OAuth
and per-principal mailbox lifecycle; and any auto-approval or standing-grant
mechanism (B8).

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

Each server declares exactly one required platform scope,
`mcp.{server_id}.use`, granted to the configured principal. The default
account retains `gmail_read`, `gmail_write`, and `gmail_send`, so its tools
remain `mcp.gmail_read.search_threads` and so on. An additional account uses
the explicit ids `gmail_{account_id}_read`, `gmail_{account_id}_write`, and
`gmail_{account_id}_send`; the `work` read tool is therefore
`mcp.gmail_work_read.search_threads`.

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
`refresh_token`, the granted Google scope, and, for manifest-configured
accounts, the non-secret operator `account_id`. The server exchanges the
refresh token for access tokens against Google's fixed HTTPS token endpoint
inside its own process — a package constant, over authenticated TLS with
CA-chain validation and hostname verification, with redirects never
followed, the same transport rules the Gmail calls obey — checking expiry at
use rather than on a timer. Access tokens, the
refresh token, and Google's error bodies never cross the stdio pipe; the
platform never learns the credential is OAuth. A refresh token Google refuses
surfaces as the adapter's ordinary `tool.server_unauthorized`, terminal at
connect and laddered mid-session, exactly as for any MCP server.

Initial consent is a one-time operator ceremony. The legacy single-account
form remains:

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

For a manifest account the operator supplies its durable routing label:

```text
python -m gmail_mcp bootstrap --account-id work --output-directory /private/path/work
```

The label is written into all three documents. It does not verify the Google
address selected in the browser, so the operator selects the same account for
all three consents and confirms it with the real-mailbox smoke. At runtime the
server receives the label as non-secret configuration and rejects an absent or
mismatched label before any Gmail request.

## Configuration and composition

Email is disabled by default. The environment layer owns:

```text
AGENT_EMAIL_ENABLED            default off
GMAIL_READ_CREDENTIAL_FILE     path to the read server's credential JSON
GMAIL_WRITE_CREDENTIAL_FILE    path to the write server's credential JSON
GMAIL_SEND_CREDENTIAL_FILE     path to the send server's credential JSON
GMAIL_ACCOUNTS_FILE            path to the versioned multi-account manifest
```

When the flag is set, exactly one configuration form is accepted. The legacy
form requires all three credential-file variables and synthesizes the original
three rows. The manifest form rejects every legacy Gmail credential-file
variable and reads this bounded, non-secret schema:

```json
{
  "version": 1,
  "default_account": "personal",
  "accounts": [
    {
      "account_id": "personal",
      "read_credential_file": "/etc/veetbot/gmail/personal/gmail-read.json",
      "write_credential_file": "/etc/veetbot/gmail/personal/gmail-write.json",
      "send_credential_file": "/etc/veetbot/gmail/personal/gmail-send.json"
    },
    {
      "account_id": "work",
      "read_credential_file": "/etc/veetbot/gmail/work/gmail-read.json",
      "write_credential_file": "/etc/veetbot/gmail/work/gmail-write.json",
      "send_credential_file": "/etc/veetbot/gmail/work/gmail-send.json"
    }
  ]
}
```

Version 1 requires one through eight unique accounts, a default id present in
the list, account ids matching `^[a-z][a-z0-9_]{0,31}$`, exactly three absolute
owner-only regular credential files per account, and no unknown fields. The
composition root synthesizes three operator-tier stdio server rows per account
— command, mode and optional account-id arguments, `env` auth scheme,
per-server credential reference, classification, and single required scope —
and hands them to the MCP adapter with every other configured server. The
default account keeps the three legacy server ids. Every other account uses an
account-qualified id, so account selection is fixed in the advertised tool
name rather than accepted as a model-authored argument.

Per-server request timeouts and `maximum_output_bytes` use the adapter's
defaults unless the deployment overrides them. When the flag is unset no row
is composed: no `mcp.gmail_*` tool exists in the registry, none is advertised,
and no `mcp.gmail_*.use` scope is granted. A missing or unreadable manifest or
credential file while the flag is set is a configuration error at composition,
not a connect failure later.

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
widens no destination policy: it adds no host to the egress allowlist, grants
the worker no outbound reach it did not already have, and leaves the
destination that ultimately serves a read governed on its own path — the
operator-configured command line for stdio, the platform's own no-redirect
HTTP transport and the egress allowlist for HTTP. A model-authored argument
can select neither. The arm lands with the change that implements it, amending
[policy-and-approvals.md](policy-and-approvals.md) in the same commit, and
every non-read MCP call remains exactly as gated as before.

### The stdio network boundary

Nothing here runs with networking disabled, and the specification says which
process is which rather than leaving it to be inferred. The Milestone 8
adapter spawns a stdio server as an ordinary child process of the worker, and
[tool-system.md](tool-system.md) states the consequence
(tool-system.md:1007-1009): a stdio child inherits the worker's network
position, which in this platform is a privileged one, which is why stdio
servers are operator-configured only. The restriction that applies to the
worker is that the worker itself dials nothing on a `gmail_*` call; the child
does, and neither egress enforcement point reaches the child. The sandbox
proxy governs a network namespace no MCP child is placed in, and the worker's
outbound guard checks URLs the platform itself dials
(sandbox-isolation.md:807-816); `gmail_mcp` opens its own sockets, which that
guard never sees. An `env`-scheme command line is not a network allowlist —
it fixes who chose the binary, not where the binary may connect — and this
document claims no more for it than that.

What confines `gmail_mcp` to Google is therefore the package, and the
milestone is built first-party for exactly that reason. The two endpoints are
package constants rather than arguments, no redirect is followed, TLS
verification cannot be overridden, and the credential the child holds is
consented to one Google scope and useless anywhere else. Hard gate 12 makes
the endpoint set and the refused redirect blocking assertions over the code
this repository ships, and hard gate 1 keeps the package a separately
auditable unit. This is confinement by construction rather than by
enforcement, which is the honest description and also the second reading of
ADR-0071's rejection of a third-party Gmail server: a stdio child's egress is
not platform-enforceable, so a server this repository does not build is a
server no gate can hold to a destination.

One precondition follows, named here rather than assumed away. A deployment
that wants egress enforcement underneath a worker-spawned child must impose
it at the host, where the operational baseline deliberately leaves outbound
open (operational-hardening.md:321-325). Milestone 18 neither changes that
posture nor depends on changing it, and a platform-level allowlist for stdio
children would be new mechanism in the sandbox and tool-system specifications
that no milestone has designed.

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

The server dials exactly two fixed HTTPS endpoints — the Gmail API host and
Google's token endpoint — both constants in the package, never arguments,
over authenticated TLS: certificate-chain validation against the system CA
bundle and hostname verification stay enabled, and nothing in the package
may pass a verification override. It
follows no redirect: a 3xx answer is a permanent rejection, never a second
request, so a credentialed call cannot be walked to another host. Contract
tests assert the fixed endpoints, the refused redirect, and the absence of
any verification override.

Gmail API responses are decoded under a hard byte bound; thread bodies are
truncated to the server's declared output budget before crossing the pipe, so
one bounded result always returns. The server maps upstream failures to a
closed set of stable, content-free codes: credential rejection, rate limit,
temporary provider unavailability, permanent provider rejection, invalid
provider output, and — the sixth, and the one the two write servers need —
an outcome the server cannot determine. Google's raw error text, diagnostic
headers, and `WWW-Authenticate` values never cross the pipe — normalized
mailbox content is the tool result, and it is the only upstream content that
does. Connect-time auth failure is
terminal for the session and mid-session 401s run the adapter's bounded
ladder, both exactly as [tool-system.md](tool-system.md) already specifies for
every MCP server.

### Retryability splits on the classification, not on the status code

A blanket "rate limits and 5xx are retryable" would be correct for a read
server and wrong for the other two. `gmail_write` and `gmail_send` are
`NON_IDEMPOTENT`, and a Gmail request can commit before the client reads the
answer, so a retry after a lost response is a second label change or a second
message rather than a second attempt at the first.

For `gmail_read`, whose every tool is `READ_ONLY`, the ordinary rule holds:
rate limits and 5xx are retryable, auth failures, other 4xx, and
schema-invalid arguments are not, and the tool pipeline retains ownership of
any retry inside the run deadline.

For `gmail_write` and `gmail_send` no failure is retryable once the mutating
request has been dispatched, and the corpus already owns the machinery that
says so. The executor watermarks every call whose side effect is not `NONE`
before the tool implementation runs — the conservative rule
[ADR-0040](../adr/0040-milestone-4-policy-and-tool-seams.md) records and
`mark_effect_sent` implements (tool-system.md:652-656) — so `effect_sent_at`
is set on every write and send before its request leaves the worker, and the
recovery table's answer for a `NON_IDEMPOTENT` call whose watermark is set is
`UNCERTAIN` (tool-system.md:667). A rate limit, a 5xx, or a lost response
observed after dispatch is therefore reported by the server as the
undetermined-outcome code, resolves to the platform's `uncertain` outcome with
`tool.outcome_unknown` and `retryable: false` (tool-system.md:806-810), and is
blocked from being proposed again in the run by the unified breaker's
threshold-of-one row (tool-system.md:849). This is the rule
[tool-system.md](tool-system.md) already applies to a mid-session 401 arriving
after the watermark (tool-system.md:1783-1785) and the one
[browser-automation.md](browser-automation.md) reached for the same reason
(browser-automation.md:532-536), generalized from those two cases to every
failure a dispatched non-idempotent MCP call can return. It lands as an
amendment to [tool-system.md](tool-system.md) in the change that implements
it, keyed on the declared idempotency class rather than on any `gmail_*` name,
because a rule that named this milestone's servers would be a rule the next
non-idempotent server does not get.

Failures the server can prove happened before dispatch are unaffected: a
refresh exchange Google refused, arguments the server rejected against its own
schema, and a connection that never carried the mutating request are ordinary
failures with their ordinary retryability, because nothing was attempted. The
line the server implements is the dispatch boundary, and where it cannot tell
which side of that line a failure fell on, it reports the undetermined
outcome. Turning a safe failure into an `uncertain` costs a review; the
reverse costs a duplicate send.

The milestone claims no provider idempotency. Nothing in the transport this
document specifies carries an idempotency key, and asserting what Gmail
deduplicates is not a claim this specification is in a position to make, so
reconciliation is what closes an `uncertain` write and it is deliberately not
automatic. A `gmail_write` outcome is reconciled by reading the affected
threads back through `gmail_read`, whose search and thread tools show the
labels and drafts that actually exist. A `gmail_send` outcome cannot be
reconciled inside the milestone at all: the send server's roster is one tool
and its Google scope carries no read, so an `uncertain` send is surfaced for
the human review the recovery table already routes it to, and the owner reads
the mailbox. The platform does not guess, and it does not send again.

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
- A manifest with a default and a second account composes six isolated server
  processes, retains the default account's three legacy ids, gives the second
  account three explicit ids, and grants exactly one matching platform scope
  per server.
- Every manifest-configured child receives only the credential matching its
  account and mode; tagged credentials refuse a different account id, and
  mixed legacy/manifest, partial, duplicate, unknown, relative, insecure, or
  over-eight-account configuration fails before discovery.
- The bootstrap ceremony writes 0600 credential files that round-trip through
  the settings loader, requests exactly the per-server Google scopes, and
  prints no secret.
- A send proposed after reading mail cannot be plain-allowed, and a daily
  triage schedule produces reads without approvals, writes with them, and
  content-free notifications.
- Against the fake Gmail API, a write and a send whose request the provider
  commits before answering with a 5xx, and again before the response is lost
  to a timeout, each resolve `uncertain` rather than a retryable failure, are
  refused if proposed again in the run, and produce no second Gmail request;
  the same failures against `gmail_read` stay retryable.

## Build sequence

1. This document, ADR-0071, ADR-0085, the milestone-map area and census rows,
   and the seventeen registry entries, checks pending. **M18.**
2. The fake Gmail API fixture and the shared three-mode contract suite, red;
   then `src/gmail_mcp/` read mode green, with the two-way import-isolation
   check. **M18.**
3. Write and send modes green; roster, classification, and failure taxonomy
   observed, including the committed-then-5xx and committed-then-timeout
   cases against the fake provider. **M18.**
4. The `host_on_allowlist` MCP arm with the policy-and-approvals amendment,
   and the dispatched-non-idempotent `uncertain` rule with the tool-system
   amendment, each landing with the document it amends. **M18.**
5. Composition: the flag, the credential-file settings, three synthesized
   server rows, scope grants. **M18.**
6. The bootstrap consent command. **M18.**
7. Operator-managed multi-account composition, account-bound bootstrap, and
   isolation gates. **M18.**
8. The monitoring recipe exercised end to end; gate evidence recorded; the
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
8. **Token confinement.** Access tokens, refresh tokens, and raw upstream
   error text or diagnostic headers never appear in tool results or durable
   events; failures cross the pipe only as the closed stable codes, and
   normalized mailbox content is the only upstream content that crosses at
   all. **M18.**
9. **Default off.** With the flag unset, no `mcp.gmail_*` server row is
   composed and no such tool is registered or advertised. **M18.**
10. **Scope confinement.** Each server requires exactly its own
    `mcp.{server_id}.use` scope, and a configuration declaring a platform
    scope for a `gmail_*` server is rejected. **M18.**
11. **Bootstrap consent.** The ceremony writes owner-only credential files
    that round-trip through the settings loader, requests exactly the
    per-server Google scopes, and never prints token material. **M18.**
12. **Failure taxonomy.** Connect-time auth failure is terminal, the
    mid-session ladder is bounded, rate limits and server errors are stable
    and retryable for the read server, but every post-dispatch failure for a
    write or send — including rate limits, server errors, timeouts, and lost
    responses — resolves `uncertain`, forbids any retry or second request,
    and cannot be proposed again in the run; the two package endpoints are
    the only hosts dialled, a redirect is refused rather than followed, and
    oversized upstream bodies truncate within the declared output budget.
    **M18.**
13. **Monitoring recipe.** A daily triage schedule materializes a run whose
    reads pass without approval, whose first write parks an approval whose
    notification is content-free, and whose outcome is reported. **M18.**

14. **Named-account composition.** A two-account manifest synthesizes three
    servers per account; the default retains the legacy ids, the second uses
    account-qualified ids, and all six advertise the expected rosters and
    exact server-id scopes. **M18.**
15. **Account credential isolation.** Every generated account/mode server
    resolves one distinct credential reference and passes only that credential
    plus the non-secret account id to its child; no process can receive another
    account's credential. **M18.**
16. **Account-bound bootstrap.** Bootstrap with `--account-id` writes three
    owner-only documents tagged with that id, a matching server accepts them,
    and a missing or different id is rejected before any Gmail request. Legacy
    untagged documents remain valid only through legacy single-account
    configuration. **M18.**
17. **Multi-account configuration bounds.** The accounts manifest is
    versioned, closed, bounded at eight unique ids, requires its declared
    default and three absolute private credential paths per account, and is
    mutually exclusive with the legacy triplet; every violation fails at
    startup. **M18.**

These seventeen registry-backed gates are the milestone's blocking delivery
contract. They do not advance the verified gate ceiling, which still moves
only in milestone order.
