---
title: Multi-Device and Surfaces
status: design
canonical: true
---

# Multi-device and surfaces

## What this document is responsible for

Section 29 defers itself. Its last subsection states the position
plainly — *"Defer the Device concept, presence, device-scoped tool
routing, and notifications to a milestone with concrete use cases,
exactly as memory and MCP are deferred"* — and the plan's sequencing
table puts inbound surfaces and pairing at Milestone 10 and beyond.
Writing contracts for the four ports Section 29.6 names would be
building the thing the plan defers.

So this document does not design the `Device`. It audits the seam.
The rule that governs every choice below: **a deferred design has to
be additive when it lands, and the only way to know whether it is
additive is to check.** The check is the deliverable — where the
corpus already holds a device-shaped hole, where it holds something
that contradicts Section 29, and what each contradiction will cost
whoever resolves it.

Three things follow.

1.  A seam nobody has walked is a hope rather than a seam.
    [tool-system.md](tool-system.md) reserves
    `ExecutionTarget.kind = "device"` and the `device.` tool domain
    and argues that doing so *"costs nothing now and prevents the
    device path from arriving as a parallel pipeline later"*. That is
    a checkable claim about the rest of the corpus, and it is checked
    below rather than assumed.
2.  The places that do not fit are the content. Eight already hold
    the hole and need no edit at all. Five do not, and those five are
    what a Milestone 10 implementer would otherwise find one at a
    time, each in the middle of doing something else.
3.  Nothing here is 0.1 work. Section 29.8 asks two things of 0.1 —
    that every read and write be principal-scoped and served from the
    core, and that a second client be confirmed able to attach to a
    session and replay — and both are specified, the second by a hard
    gate at Milestone 5, `gate.api.replay_exact` in
    [http-api-and-streaming.md](http-api-and-streaming.md). An audit
    is the right shape for this subject because the obligation that
    is actually due is already discharged.

## The three subsections that need nothing

Sections 29.1 through 29.3 — one shared core, what must be
cloud-shared, what stays device-local — describe the architecture the
corpus already has rather than propose a change to it. PostgreSQL is
the source of truth, devices are clients of the HTTP API, and every
one of the thirteen components 29.2 lists as cloud-shared is
cloud-shared because it is a table, a port, or a server-side service
in some other document. [readiness.md](readiness.md) reached the same
conclusion when it recorded the gap: the section's core claim is
*"defensible without"* an expansion.

The audit below is therefore about 29.4 through 29.7, which is where
the `Device` is introduced and where the corpus can actually be
wrong.

## The eight places the seam is already cut

Each of these exists today, and a `Device` landing later uses it
without editing it.

1.  **`ToolSource.DEVICE` is a declared enum member.**
    [tool-system.md](tool-system.md) declares four sources and
    `DEVICE` is one of them, so a device tool entering the registry
    is a value the type system already admits.
2.  **Device output is forced untrusted at registration.** The
    registry overwrites `output_trust` to `EXTERNAL_UNTRUSTED` for
    `DEVICE` exactly as it does for `MCP` and `SANDBOX` — overwritten
    rather than validated, so a device cannot declare itself
    trustworthy. [builtin-tools.md](builtin-tools.md) widens the same
    rule to key on `target_kind` as well as `source`, which catches a
    builtin whose execution target is a device.
3.  **The composition root is where both are enforced.**
    Reserved-domain enforcement and forced trust labels *"both happen
    at registration, which means they happen here or they do not
    happen at all"*
    ([bootstrap-and-composition.md](bootstrap-and-composition.md)),
    and `DEVICE` is named in that enforcement beside `MCP` and
    `SANDBOX`.
4.  **The `device.` domain is reserved and a builtin cannot take
    it.** The domain partition table assigns `device` to
    device-scoped tools registered at attach, and registering a
    builtin in that domain is a startup error — which is what stops a
    device from shadowing `workspace.write_text`.
5.  **A device is a field on the policy input, not a second policy
    path.** `ExecutionTarget` carries `kind` with `device` among its
    four values and a nullable `device_id`, and
    [policy-and-approvals.md](policy-and-approvals.md) states why:
    *"Modelling the device as a field on the input rather than as a
    separate evaluation path is how that stays true."*
6.  **One authorization gate already names the device channel.** Hard
    gate 3 of [policy-and-approvals.md](policy-and-approvals.md)
    requires exactly one function to move an invocation from
    `PROPOSED` to `AUTHORIZED`, asserted by an import-boundary test,
    and states that *"the sandbox bridge and device channel both
    reach it"*. ADR-0005 found three surfaces that propose actions
    and the device channel is one of them.
7.  **Offline is a specified outcome rather than a hang.** A call to
    a tool whose device is not connected returns `unavailable` with
    `tool.device_offline`, which is a row of the availability table
    that already exists — added there, rather than as an exception to
    it, on the same argument that keeps a disconnected MCP server's
    tools advertised.
8.  **Concurrent approval resolution is already handled.**
    [policy-and-approvals.md](policy-and-approvals.md) cites Section
    29 by name for the requirement that any authorized device may
    resolve an approval idempotently, implements it as a guarded
    `UPDATE` where zero rows returned means someone else won, and
    carries *"Two devices resolve at once"* as a row of its
    failure-mode table.

## The five places it is not

### Attach is a third registration source, and not at session open

[tool-system.md](tool-system.md) states that *"The registry accepts
new entries from exactly two sources — the build, and MCP discovery
at session open — and `skill_manage` is not one of them."* The domain
partition table in the same document registers the `device` domain at
attach. That is a third source, and the timing is the part that
matters: MCP discovery is at session open by design, because the
context engine pins the tool set there. A device attaches and
detaches on its own schedule, many times inside one session or with
no session open at all.

The sentence costs one word to correct and it is worth naming now
because it is load-bearing somewhere else — it is the argument for
why `skill_manage` writes a catalog rather than a tool. An editor who
widens "two" to "three" without noticing what the two were defending
weakens that argument by accident. The consequence of the timing
mismatch is the subject of the first conflict below.

### Device lifecycle events have no session to be charged to

`events.session_id` is `NOT NULL` and the table's only uniqueness
constraint is `UNIQUE(session_id, sequence)`, so every event in the
log belongs to exactly one session. Device attach, detach, and scope
revocation are none of a session's business: a device attaches before
any session exists and stays attached across many.

There are three ways out and this document picks none of them — make
`session_id` nullable and find another sequence source, give device
lifecycle its own table and its own audit trail, or synthesize a
per-principal session to charge them to. The first touches the event
log's central invariant, the second is the smallest, and the third is
the worst, because it makes the word session mean two things. The
cost is a schema decision, and the reason to record it now is that
Section 29.7 requires revocation to be immediate and observable —
*"Revoking a device immediately removes its scopes and presence
server-side"* — which is an audit requirement, and the log is where
audit requirements go.

The harness reaches the same wall and did not notice it.
[evaluation-harness.md](evaluation-harness.md) declares four `eval.*`
events on the harness rather than on a run, under a span root that is
explicitly not `agent.run`, so those have no session either. Two
documents arriving here independently is an argument for the second
way out, and for taking it once rather than twice.

### A hand-off is a fourth suspension kind

[runtime-loop.md](runtime-loop.md) tabulates three suspension kinds —
`APPROVAL`, `USER`, and `CHILD_RUN` — across two run states, and
[tool-system.md](tool-system.md) declares the persisted column as
`suspended_kind TEXT NULL` with the comment `user_input | child_run`,
a closed two-value vocabulary. Section 29.5's hand-off is neither: a
run that needs a capability only an offline device has *"enters
WAITING_FOR_USER or returns a structured 'capability unavailable'
result rather than failing silently"*.

`WAITING_FOR_USER` is entered by `conversation.ask_user` and resumed
by the input endpoint carrying a `question_id`. A device hand-off has
no question and is resumed by a device attaching. Reusing the state
without a fourth kind would make `WAITING_FOR_USER` mean two things
with two different resume paths, which is the exact failure
[runtime-loop.md](runtime-loop.md) avoided when it declined to add a
`WAITING_FOR_CHILD` state: it kept one state and added a typed kind.
The same move works here and costs one enum value and one row of that
table.

The other branch of 29.5's sentence needs nothing. A structured
"capability unavailable" result is `tool.device_offline`, which is
item 7 above.

### No client is attributed on a write

Every application-service signature takes a `Principal` and nothing
about where the request came from. `RunService.deliver_input` takes
`principal`, `run_id`, `content`, and an optional `question_id`;
submission and cancellation are the same shape. Neither `sessions`
nor `runs` carries an origin, source, channel, or device column.

For 29.1 that is correct and deliberate — a write from any device
goes to the shared core, and the core does not need to know which
one. For 29.7 it is not, because *"Treat all device-provided content
as untrusted input for prompt-injection purposes"* is a claim about
content, and a trust label is applied where content enters. Tool
*output* is covered: that is `ToolSource.DEVICE` and forced trust,
items 1 and 2. Tool *input* — a message typed on a paired Surface by
somebody who is not the principal — is not, because it arrives
through the same endpoint as a message the principal typed
themselves.

This is where Devices and Surfaces stop being one problem, and it is
picked up below.

### `NotificationService` is a port name with nothing behind it

The name appears exactly twice in the corpus, in Section 29.6 and in
ADR-0011, and in both places it is a list item. No document states
what it delivers, over what transport, or with what durability.

The corpus does have a notification mechanism and it is not this one.
ADR-0010 makes PostgreSQL `LISTEN`/`NOTIFY` the transport for worker
wakeup and live stream delivery, and
[event-log-and-persistence.md](event-log-and-persistence.md) is
explicit that it is *"a latency optimization and never a delivery
guarantee"*, with every consumer polling from a watermark. That is
the right property for one server process waking another against the
same database and the wrong one for telling a phone that an approval
is waiting. Pushing to a client that holds no open connection is a
different problem with different durability requirements, and nothing
in the corpus solves it.

Naming this costs nothing and prevents one specific mistake:
reading ADR-0010 as though it covered Section 29.5's *"presence /
notification service"*. It does not.

## Per-device scopes are an intersection, computed once

This is the finding that justifies the audit. Section 29.4 requires
that *"a principal may grant a device a subset of scopes (a shared
desktop gets fewer than a personal laptop), enforced centrally"*, and
it is the requirement that sounds like it needs a new evaluation
path.

It does not, because of a decision
[policy-and-approvals.md](policy-and-approvals.md) already made for
an unrelated reason. The scope set is captured at submission and
stamped on the run in `runs.principal_scopes`, and
`PrincipalResolver.for_run` reads that stamp and never a table. Hard
gate 13 states the property directly: **the scope set is the run's.**

So per-device narrowing is the intersection of the principal's scopes
with the device's granted scopes, computed once at submission and
stamped like any other scope set. The policy engine is never told
that a device exists. Revocation then behaves correctly without being
designed: a run already in flight finishes under the scopes it was
submitted with, and the next submission from that device gets the
narrowed set, which is exactly the semantics revocation already has
for a principal.

One constraint travels with this and is easy to miss. The scope
vocabulary is a closed list of fifteen strings with exactly one
escape — *"an entry is legal when it is one of the fifteen, or when
its first segment is `mcp` and its second is the server id"* — and
hard gate 11 asserts it. A `device.` scope prefix would require that
rule to change and that gate to be rewritten. The `device.`
reservation that exists today is a **tool-name domain, not a scope
prefix**, and the two namespaces are unrelated. Per-device scopes are
therefore a subset of the fifteen and never a new namespace, which is
both the cheaper version and the only one compatible with the gate.

## Surfaces are Devices with an empty capability set

Section 29.4's last two bullets introduce Surfaces — Telegram, Slack,
email — as *"a device-like client with a presence and a capability
set, unified under one session-key resolver (DM per user, group per
participant, thread shared)"*. Read against the `Device` model, a
Surface is that model with `capabilities` empty and `kind` naming the
channel. It serves no device-scoped tool; it originates messages.
That is one model, not two, and treating it as two would produce two
presence mechanisms for one property.

Two things do not come along, and they are why the plan's sequencing
table lists inbound surfaces and pairing as a row separate from the
multi-device core.

1.  **The session-key resolver has nothing to resolve against.**
    `sessions` has one unique index, on `id`, and no external key. A
    resolver that maps a group conversation to a session needs a
    unique `(surface, external_key)` and a rule for what happens when
    a long-lived thread outlives an `agent_version`, which is frozen
    at session creation. This is the one genuinely new mechanism in
    Section 29. Everything else in it is a placement or an
    intersection.
2.  **Pairing is a security boundary, and it is the one part already
    designed.** ADR-0017 default-denies an unknown sender and
    requires an explicit one-time code with expiry, rate limit, and
    lockout before any run is created on their behalf, writing the
    sender into a per-Surface allowlist. That needs a home and an
    endpoint rather than a decision.

The trust question left open by the attribution finding resolves
here rather than on the device side. A message from a paired sender
is not `USER` trust in the sense the trust model means it, because
the sender is not the principal — they are somebody the principal
allowlisted. Section 29.4 says device output is *"EXTERNAL_UNTRUSTED
(or USER where appropriate)"* and leaves appropriate undefined. The
corpus has the labels; what it lacks is the rule for choosing between
them for inbound content, and that rule belongs with pairing, because
pairing is where the corpus learns who the sender is.

## Where the Device would live

[readiness.md](readiness.md) states the gap as a placement problem:
*"The `Device` model itself still has no home."* Placement is
answerable now without deciding a single contract, and
[bootstrap-and-composition.md](bootstrap-and-composition.md) supplies
the rule — **a port lives in the module named for the capability it
abstracts, not for the component that calls it.**

Under that rule the four items of Section 29.6 do not go in one
module, and one of them is not a port.

1.  `DeviceRegistry` and `DevicePresence` abstract stored state and a
    query over it. That is `ports/repositories.py`, beside the seven
    repositories already there. Presence is a column on a row, not a
    capability of its own, and splitting it into a second module
    would be naming the caller rather than the capability.
2.  `DeviceChannel` abstracts invoking a tool on a remote executor
    and streaming its result back. Section 29.4 says as much —
    *"the same pattern as an MCP transport"* — and the MCP ports live
    in `ports/tools.py`, so this one does too.
3.  `NotificationService` abstracts delivering a message outward to a
    principal, and nothing in `ports/` abstracts that today. It needs
    a new module rather than a new Protocol in an existing one, and
    that is the structural measure of the finding above: the port
    with no mechanism behind it is also the port with no neighbours.
4.  The fourth item is not a port. *"Extend the tool registry and
    context builder to filter and route device-scoped tools by
    presence"* is an edit to two components the corpus already owns,
    and the first conflict below is what that edit runs into.

The `Device` type itself is a domain model rather than a port and
belongs with the other domain models. Its storage is a new table and
not a column on an existing one, because a device outlives every
session and belongs to a principal rather than to a conversation.

## Conflicts this document resolves

Three, all resolved by naming which document a later milestone edits
rather than by editing anything now.

1.  **Presence-based exposure versus the pinned prefix.** Section
    29.4 requires the registry and context builder to expose a
    device-scoped tool *"only when a qualifying device is
    connected"*. [context-engine.md](context-engine.md) pins the tool
    set at session open, and [tool-system.md](tool-system.md)
    generalizes that into *"Advertisement is pinned; availability is
    resolved at call time"*, with `tool.device_offline` as the
    call-time outcome. The pin wins, and it already does: the
    availability table carries the device row, so a device that
    disconnects mid-session is handled. The unresolved half is a
    device that *attaches* mid-session, whose tools miss the pin and
    are invisible for the life of that session. The corpus answers
    that by precedent rather than by new design — an MCP server
    sending `tools/list_changed` has the notification *"recorded and
    not applied"*, with new tools available at the next session open,
    and a device is the same case with the same cache-invalidation
    risk behind it. What
    [tool-system.md](tool-system.md) reserved as *"whether a device
    tool may be advertised in a session opened while the device was
    absent"* therefore resolves to: not in that session. The cost is
    real and belongs in the open questions below.
2.  **Two registration sources versus three.**
    [tool-system.md](tool-system.md) says the registry accepts
    entries from exactly two sources; its own domain table registers
    `device` at attach. The count is what is wrong, not the table —
    attach is a genuine third source and the sentence was written
    before it mattered. Whoever lands device registration corrects
    the count and preserves what the sentence was defending, which is
    that `skill_manage` is not a source.
3.  **Queue or reject, for a second device on a busy session.**
    Section 29.5 has a second device submitting to a busy session
    *"follow the configured policy (queue or reject)"*.
    [runtime-loop.md](runtime-loop.md) already resolved that against
    ADR-0004: the partial unique index makes queueing impossible at
    the database level, and 0.1 rejects. That resolution stands and
    Section 29.5's parenthesis is the stale half. A device is not a
    special case of the single-active-run rule; it is the ordinary
    case with a second client.

## Decisions

1.  **This document declares no gates.** It states no requirement
    that code could violate, because it specifies no behaviour. It
    joins [bootstrap-and-composition.md](bootstrap-and-composition.md)
    and [development-toolchain.md](development-toolchain.md) as a
    specification whose hard-gates section is absent rather than
    empty, and the gate census is unchanged by it.
2.  **Placement is decided; contracts are not.** The four items of
    Section 29.6 get modules, the `Device` gets a table rather than a
    column, and no method signature appears anywhere in this
    document. Section 29.8 defers the design, and a deferral does not
    require its subject to be homeless.
3.  **A Surface is the `Device` model rather than a second model.**
    `capabilities` empty, `kind` naming the channel. The two bullets
    Section 29.4 spends on Surfaces describe the same registry and
    the same presence the device bullets describe.
4.  **Per-device scopes are a subset of the fifteen.** Not a
    `device.` scope prefix, which would mean rewriting the grammar
    rule and hard gate 11 for no gain, and which would collide
    conceptually with the `device.` tool-name domain that already
    exists and means something else entirely.
5.  **Section 29's 0.1 obligation is already met.** Both halves of
    29.8 — principal-scoped reads and writes served from the core,
    and a second client confirmed able to attach and replay — are
    specified, and the second is gated at Milestone 5. Nothing in
    this document is scheduled before the milestone Section 29.8
    defers to.

## Open questions for review

1.  **Is "a device that attaches mid-session is invisible until the
    next one" acceptable?** It is what the pinned prefix implies and
    what the MCP precedent already chose, and it is the safe
    direction to be wrong in — the failure it permits is a capability
    a user has to open a new session to reach, and the failure it
    prevents is a third party invalidating a tenant's prompt cache by
    connecting and disconnecting. It is also more visible to a user
    than the MCP case ever is, because plugging in a laptop is a
    deliberate act with an expectation attached. If the answer is no,
    the exception costs a prefix rebuild mid-session and needs its
    own rate limit, and it should be argued for on those terms rather
    than adopted quietly.
2.  **Where do device lifecycle events go?** `events.session_id` is
    `NOT NULL` and a device has no session. A separate table is the
    smallest change and the only one that leaves the event log's
    central invariant alone; the argument against it is that Section
    29.7's revocation requirement is an audit requirement, and
    splitting audit across two logs is how audit gaps happen.
3.  **Should the `Device` table exist before device-scoped routing
    does?** Registering a device and tracking presence is useful on
    its own — it is what an approval notification needs, and Section
    29.5's approval routing is the one cross-device flow with no
    deferral marked against it. Landing the registry ahead of the
    channel would make the area incremental rather than one
    milestone-sized step.
4.  **Is `NotificationService` one port or two?** Delivering to a
    client that holds an open connection and delivering to a device
    that does not are different problems with different durability
    requirements, and one Protocol spanning both would hide that.
    This is the largest genuinely open design question Section 29
    leaves, and it is the reason the port has no neighbours.
5.  **What trust label does a paired sender's message carry?**
    Section 29.4 offers *"EXTERNAL_UNTRUSTED (or USER where
    appropriate)"* and does not define appropriate. A sender
    allowlisted by the principal but who is not the principal is a
    third position, and the two labels have no name for it.
