# ADR-0071: Milestone 18 first-class email integration

- Status: Proposed
- Date: 2026-08-24
- Related: Sections 2.6, 9, 21, and 22 of the engineering plan; ADR-0005,
  ADR-0017, ADR-0021, ADR-0044, ADR-0054, ADR-0061, ADR-0069, ADR-0070
- Detailed design: `docs/plan/email-integration.md`

## Context

Section 2.6 excluded email integration from the initial version, and the
roadmap carries the exclusion's expiry date: item B11 lists first-class email
integration with the entry condition "Owner intent; email and calendar first
as MCP servers." On 2026-08-24 the owner expressed that intent for the email
half — the agent should read and triage the owner's Gmail, draft and send
replies on approval, organize the mailbox, and check it on a schedule. The
roadmap rule requires two things before an item enters the plan: the owner's
authorization and a specification with gates to declare. This ADR is the
authorization, and [email-integration.md](../plan/email-integration.md) is the
specification, with thirteen gates to declare.

The entry condition also answers the mechanism question, and it answers it
well. The MCP adapter Milestone 8 built treats every server as an untrusted
external system that happens to speak a convenient protocol, forces its output
to `EXTERNAL_UNTRUSTED`, and lets the operator — never the server — declare
what its tools do to the world. The security baseline in Section 22 lists
email itself as untrusted input. A mailbox is therefore exactly the kind of
capability the MCP seam was built to carry: hostile content arriving through
a channel the platform already refuses to believe, with the classification
and the approval gate held on our side of the pipe.

One thing the corpus assumed and never exercised surfaces here. The
deterministic policy's `host_on_allowlist` condition recognizes only the two
fixed provider targets web access and browser automation constructed, so a
`NETWORK_READ` tool executing against an `mcp` target is denied by the default
ruleset today — including the platform's own `read_resource`. A read-only
email server is unusable until the condition learns the shape the tool system
already promises: an operator-declared read-only MCP classification. Decision
5 owns that change explicitly rather than letting it ride in as a fix.

## Decisions

1. **Milestone 18 is authorized as a parallel workstream.** It may proceed
   alongside Milestones 13 through 15 and Milestone 17 exactly as Milestones
   16 and 17 proceed alongside them, and its gates may become green
   independently. The verified gate ceiling still advances only in numerical
   order, so nothing here moves the ceiling past 15. Unlike the two memory
   workstreams this milestone does touch three shared files — the policy
   engine's condition function, the configuration surface, and the MCP
   adapter's failure mapping, where decision 10 generalizes an outcome the
   adapter already produces on one path — and the overlap is named here rather
   than discovered in review: all three changes are additive arms on existing
   rules that no authorized milestone's diff also touches.
2. **Email arrives as first-party MCP servers, not as builtin tools.** B11's
   entry condition names the mechanism and the design honors it: the platform
   gains no `email.*` builtin, no provider port, and no Gmail type in
   `agent_core`. The servers live in one new package, `src/gmail_mcp/`, beside
   `agent_core` rather than inside it, speak Gmail's REST API directly, and
   reach the platform only through the Milestone 8 adapter as operator-configured
   stdio servers. First-party because the gates hold the server to credential
   and output rules no third-party server ever promised; a structural gate
   asserts the two packages import nothing from each other.
3. **Three servers, one honest classification each.** The tool system
   classifies at the server level, so the roster splits by effect:
   `gmail_read` at `NETWORK_READ`/`LOW`/`READ_ONLY`, `gmail_write` at
   `EXTERNAL_WRITE`/`MEDIUM`/`NON_IDEMPOTENT`, and `gmail_send` at
   `EXTERNAL_MESSAGE`/`HIGH`/`NON_IDEMPOTENT`. Two servers would force a
   categorical lie — a send filed as a write, or a label filed as a message —
   and the classes are categories, not ranks. Under the default matrix every
   write and send already requires approval, so the split adds no policy rule;
   what it buys is honesty, and a pending-approval queue that orders sends
   above label changes by the risk the operator declared.
4. **No server exposes permanent deletion.** Gmail's trash is a reversible
   move with a thirty-day grace period and an untrash operation, so
   `trash_thread` is honestly an `EXTERNAL_WRITE`. `users.messages.delete`
   appears in no roster, and a gate asserts the confinement. The owner decided
   this on 2026-08-24: an agent that can empty a mailbox irreversibly is a
   capability nobody asked for, and the trash folder is the approval queue's
   second chance.
5. **The deterministic `host_on_allowlist` condition gains an MCP arm.** The
   condition holds for an `mcp` target when the executing tool's specification
   declares `NETWORK_READ` and `READ_ONLY`. The justification is the same one
   ADR-0054 recorded for the web-provider arm: the classification cannot be
   model-authored or server-claimed — the operator declares it at
   configuration time, stdio command lines are operator-configured only, and a
   tenant-supplied HTTP endpoint was validated against the egress allowlist
   when the configuration row was written. The arm widens no destination
   policy: it adds no host to the egress allowlist, and the destination that
   ultimately serves a read is governed on its own path — the
   operator-configured command line for a stdio child, or the
   egress-allowlisted endpoint reached over the platform's own no-redirect
   HTTP transport — so no model-authored argument can select where a read
   goes. What the arm does not do is confine a stdio child's egress, because
   nothing on the platform side can: a stdio server is a child of the worker
   and inherits its network position, and the specification states that and
   its consequences under *The stdio network boundary* rather than implying a
   restriction that does not exist. The change is one additive arm in
   one function, amended in [policy-and-approvals.md](../plan/policy-and-approvals.md)
   in the same change, and it repairs the existing denial of the adapter's own
   read-only `read_resource` tool rather than relaxing anything: every
   non-read MCP call is exactly as gated as before.
6. **Credentials enter through the existing broker, one `env`-scheme reference
   per server.** Gmail's OAuth is the authorization-code grant with a refresh
   token, which is not the `oauth2_client` client-credentials exchange and
   must not be forced into it. The seam that fits without touching the tool
   system is the one stdio servers already use: the broker resolves each
   server's `credential_ref` to one opaque value placed as one declared
   variable in a constructed child environment. The value is a JSON document
   holding the client id, client secret, and refresh token; the server runs
   the refresh exchange against Google inside its own process, checking expiry
   at use, and the platform never learns the credential is OAuth. The server
   dials exactly two fixed HTTPS endpoints — the Gmail API host and the token
   endpoint, both package constants — over authenticated TLS with CA-chain
   validation and hostname verification, and follows no redirect, so
   credential material cannot be walked to a third host or served to an
   impersonator; nothing in the package may weaken the TLS default, and
   contract tests assert the endpoints, the refusal, and the absence of any
   verification override. Initial
   consent is a one-time operator ceremony, `python -m gmail_mcp bootstrap`,
   which runs the installed-app loopback flow and writes owner-readable
   credential files that enter the broker through file-backed settings
   references, the shape the browser-automation profile credential already
   uses. The owner's Google OAuth client runs in production publishing status,
   an owner decision of 2026-08-24 accepting a one-time unverified-app consent
   warning in exchange for refresh tokens that do not expire on the seven-day
   testing-status clock.
7. **Email is off by default.** `AGENT_EMAIL_ENABLED` gates composition: unset,
   no server row is composed, no `mcp.gmail_*` tool is registered or
   advertised, and no scope is granted. This follows the schedule,
   notification, and memory-read-API flags exactly, and a gate asserts the
   absence.
8. **No policy profile may auto-approve email writes or sends.** The
   trust-overlay rule already forbids a plain allow for a send proposed after
   reading untrusted mail; this decision closes the other door, a profile that
   downgrades `EXTERNAL_MESSAGE` or `EXTERNAL_WRITE` for the `gmail_*`
   servers. Standing approval grants remain roadmap item B8, and arriving
   there requires the policy ADR that item names, not a profile edit.
9. **Proactive monitoring rides Milestones 11 and 12 unchanged.** A daily or
   weekly schedule whose instruction is a triage brief materializes an
   ordinary run; the run reads freely, and anything it proposes to write or
   send parks in the approval queue whose notification trigger already
   reaches the owner's phone, content-free. The design documents this as a
   recipe rather than building runtime. Interval and cron recurrence stay
   B5, Gmail push notification stays out, and the email inbound Surface and
   email notification transport stay B3 and B4 — different roadmap items
   this ADR does not touch. Calendar, B11's other half, enters only with its
   own specification.
10. **A dispatched non-idempotent MCP call that fails resolves `UNCERTAIN`,
    not a retryable failure.** `gmail_write` and `gmail_send` are
    `NON_IDEMPOTENT`, and Gmail can commit a request before the client reads
    the answer, so a rate limit, a 5xx, or a lost response after dispatch is
    ambiguous rather than failed and retrying it duplicates a label change or
    a message. The corpus already holds the machinery and already reaches this
    conclusion twice: ADR-0021's watermark makes the ambiguity a fact rather
    than an inference, the adapter produces exactly this outcome for a
    mid-session 401 arriving after the watermark, and browser automation
    settled it the same way for a mutation that may have reached the site. The
    decision is to generalize that rule rather than special-case a mailbox —
    the adapter's mapping is keyed on the declared idempotency class, so every
    non-idempotent MCP server gets it — and to claim no provider idempotency,
    because nothing in the specified transport carries a key. Reconciliation
    is therefore explicit and manual: a write is reconciled by reading the
    threads back through `gmail_read`, and a send cannot be reconciled inside
    the milestone at all, because the send server's roster is one tool and its
    Google scope carries no read, so it goes to human review. The change can
    only turn a `failed` into an `uncertain`, which is the safe direction, and
    it costs the false positive ADR-0040 already accepted.

## Consequences

- The platform gains its first first-party MCP servers, and `src/` gains a
  second top-level package. The architecture walk stays rooted at
  `agent_core`; a new structural gate owns the two-way import boundary.
- Thirteen new gates arrive in a new `email` area, the twenty-third, and the
  census grows from 335 to 348 registry entries.
- The policy engine's condition vocabulary is unchanged; one condition's
  holding set grows by one operator-vouched target shape, and the platform's
  own `read_resource` tool becomes usable under the default ruleset for
  read-only-classified servers.
- Every non-idempotent MCP server, not only the two this milestone adds,
  inherits decision 10's outcome: a failure after dispatch becomes
  `UNCERTAIN` and a human review rather than a `failed` the model may propose
  again. That is a behavior change outside the milestone's own surface, it is
  the direction ADR-0021 and ADR-0040 already chose, and its cost is reviews
  for calls that never left the process.
- A stdio child's egress is not restricted by anything the platform enforces.
  The milestone accepts that and answers it by building the server itself,
  gating its endpoint constants and its refused redirect, and naming the
  host-level enforcement it does not require as a deployment precondition
  rather than a platform feature. A third-party stdio server would have the
  same reach with none of the gates, which is decision 2 read from the
  security end.
- Approval fatigue is accepted for round one: every label change, draft, and
  send requires a human decision. The batch `modify_labels` tool is the
  mitigation; the relief valve is B8 and stays closed.
- Google's scope grammar is coarser than the server split — the modify scope
  can technically send — so token-level separation is partial. Per-server
  consent narrows each token to the smallest Google scope set that serves its
  roster, and the platform-side classification remains the control that
  matters.
- The owner carries an operational duty the platform cannot absorb: creating
  the Google Cloud OAuth client, keeping it in production status, and
  re-running the bootstrap ceremony if Google revokes the refresh token.
- The monitoring cadence floor is daily until B5; an owner who wants
  minute-level vigilance is asking for the item the roadmap already prices
  separately.

## Alternatives considered

- **Builtin `email.*` tools on a provider port, the web-access shape:**
  rejected. B11's entry condition names MCP as the mechanism, and the builtin
  shape would put a Gmail client and its OAuth machinery inside `agent_core`
  behind a bespoke port that MCP already generalizes. Web access earned its
  port by being provider-plural on day one; email is one provider with one
  mailbox.
- **A third-party Gmail MCP server:** rejected. The gates this milestone
  registers assert credential confinement, output confinement, and roster
  confinement inside the server process, and a server this repository does not
  build is a server those gates cannot hold. The adapter would treat it as
  untrusted either way; untrusted and unaccountable together is one step too
  far for a mailbox.
- **One server, per-tool classification:** rejected. It would add a per-tool
  classification surface to the MCP configuration schema — a tool-system
  change benefiting exactly one integration — where the three-server split
  reaches the same honesty with configuration that exists today.
- **Two servers, read and act:** rejected. A send filed as `EXTERNAL_WRITE`
  or a label filed as `EXTERNAL_MESSAGE` misstates the class either way, and
  the queue ordering the risk levels buy is lost.
- **A fourth server for permanent deletion at `EXTERNAL_DELETE`:** rejected by
  the owner in favor of decision 4. Trash covers the need reversibly.
- **Forcing Gmail auth through `oauth2_client`:** rejected. The scheme is the
  client-credentials exchange; presenting a refresh-token grant through it
  would either fail or teach the broker resolver to distinguish token shapes,
  which is the inference the scheme column exists to forbid.
- **Gmail push via Pub/Sub for real-time monitoring:** deferred. It requires
  an inbound webhook surface, which is the infrastructure B3 and B4 price;
  polling on the existing scheduler needs nothing.
- **Calendar in the same milestone:** deferred. B11 couples them in one
  roadmap row, but each enters on its own specification, and nothing in the
  email design depends on calendar types.
