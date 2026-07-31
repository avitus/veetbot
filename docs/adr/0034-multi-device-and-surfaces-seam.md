# ADR-0034: Section 29 as an audited seam rather than a design

- Status: Accepted
- Date: 2026-07-31
- Related: Sections 8 (tools), 9 (policy and approvals), 11.1 (context
  assembly), 11.2 (trust labels), 16 (the HTTP API), 22 (security
  baseline), 27 (sessions, runs, turns), 29 (multi-device operation
  and the shared core), Milestone 10, ADR-0004 (the Postgres run
  queue), ADR-0005 (the tool execution pipeline), ADR-0010 (live
  event transport), ADR-0011 (multi-device and the shared core),
  ADR-0017 (layered approval and inbound-surface security), ADR-0020
  (the context engine), ADR-0021 (tool execution, effect
  watermarking, and MCP), ADR-0023 (the run loop), ADR-0024 (the
  composition root), ADR-0028 (the HTTP API surface)
- Detailed design: `docs/plan/multi-device-and-surfaces.md`

## Context

Section 29 was the last part of the engineering plan that no
specification expanded. The readiness review recorded it precisely:
eighty-seven lines, ADR-0011, four inbound consuming references, and
no expansion, with the section's core claim defensible on its own
because PostgreSQL is the source of truth and devices are API
clients. What it introduced beyond that claim was the `Device`
concept and four named ports, and none of the four had a contract.
The review's closing sentence named the gap as a placement problem:
the `Device` model itself still had no home.

The obvious response is the one every other gap got — write the
specification. Here that response is wrong, and the plan says so
twice. Section 29.8 defers the Device concept, presence,
device-scoped tool routing, and notifications to a milestone with
concrete use cases, *"exactly as memory and MCP are deferred"*. The
plan's sequencing table puts inbound surfaces and pairing at
Milestone 10 and beyond as a row of its own. A document that wrote
the four port contracts would be building the deferred thing, and
would do it without the concrete use cases the deferral is waiting
for.

The corpus also already made the smaller version of this decision and
made it deliberately. `tool-system.md` opens a "Device-scoped tools"
section, reserves `ExecutionTarget.kind = "device"` and the `device.`
domain, and states that device tools are *"a reserved seam, not a
design"*. It then lists four things it deliberately does not decide
and says they *"belong with the cross-device work"* — work that had
no document to belong to.

So the question was not whether to expand Section 29 but what a
document can usefully say about a subject the plan has correctly
deferred. There is one thing, and it is the thing a deferral silently
assumes: that the deferred work will be additive when it arrives. A
deferral is only safe if that assumption is true, and nobody had
checked it against the fourteen specifications written since ADR-0011
was accepted.

## Decision

1.  **A seventeenth document that declares no gates.**
    `multi-device-and-surfaces.md` joins
    `bootstrap-and-composition.md` and `development-toolchain.md` as
    a specification whose hard-gates section is absent rather than
    empty. It states no requirement that code could violate because
    it specifies no behaviour, so the gate census, the fourteen
    gate-declaring specifications, and the milestone map are all
    unchanged by it. The count that moves is the number of
    detailed-design documents, from sixteen to seventeen.
2.  **The document is an audit, not a design.** Its deliverable is
    the verification that the deferred work lands additively: the
    eight places the corpus already holds a device-shaped hole and
    needs no edit, the five places it does not, and what each of the
    five will cost whoever resolves it. No method signature appears
    in it.
3.  **Placement is decided; contracts are not.** A deferral does not
    require its subject to be homeless, and placement is answerable
    from a rule the corpus already states — a port lives in the
    module named for the capability it abstracts, not for the
    component that calls it. `DeviceRegistry` and `DevicePresence`
    are repository ports; `DeviceChannel` sits with the MCP ports it
    is explicitly modelled on; `NotificationService` needs a `ports/`
    module of its own; and the fourth item of Section 29.6 is an
    edit to two existing components rather than a port at all. The
    `Device` is a domain model with a table, because a device
    outlives every session.
4.  **Per-device scopes are an intersection stamped at submission,
    not a second evaluation path.** The scope set is captured at
    submission and stamped on the run, and `PrincipalResolver
    .for_run` reads the stamp and never a table, so narrowing a
    device is the intersection of two sets computed once. The policy
    engine is never told that a device exists. This is the finding
    that most changes what Milestone 10 costs.
5.  **Per-device scopes are a subset of the fifteen strings, never a
    `device.` prefix.** The scope vocabulary is closed with exactly
    one escape, for `mcp.<server id>`, and a hard gate asserts it. A
    `device.` scope prefix would mean rewriting that rule and that
    gate for no gain, and it would collide conceptually with the
    `device.` tool-name domain, which already exists and means
    something unrelated.
6.  **A Surface is the `Device` model with an empty capability set**,
    not a second model. It serves no device-scoped tool; it
    originates messages. Two models would mean two presence
    mechanisms for one property. What does not come along is the
    session-key resolver, which is the one genuinely new mechanism in
    Section 29, because `sessions` has no external key and no unique
    index beyond `id`.
7.  **Advertisement stays pinned; a device that attaches mid-session
    is not advertised until the next one.** This resolves the
    question `tool-system.md` reserved by name. It is decided on the
    precedent already in that document rather than on new reasoning:
    an MCP server whose catalog changes mid-session has the change
    *"recorded and not applied"*, because a third party who can
    rewrite the prefix can invalidate a tenant's prompt cache at
    will, and a device connecting and disconnecting is the same
    exposure. The cost — a capability the user has to open a new
    session to reach — is recorded as an open question rather than
    buried.
8.  **Section 29.5's "queue or reject" is reject.** ADR-0004's
    partial unique index makes queueing impossible at the database
    level and `runtime-loop.md` already settled it. A second device
    on a busy session is not a special case of the
    single-active-run-per-session rule; it is the ordinary case with
    a second client.
9.  **Nothing in the document is scheduled before Milestone 10.**
    Section 29.8's own 0.1 obligation is already discharged: reads
    and writes are principal-scoped and served from the core, and a
    second client attaching and replaying is `gate.api.replay_exact`
    at Milestone 5.

## Consequences

- Sections 29 through 31 all carry an outward cross-reference
  paragraph now, and the readiness review's last named section-level
  gap closes. No part of the engineering plan is unexpanded.
- Milestone 10 acquires a five-item list of what it will actually
  cost, each item a schema or vocabulary decision rather than a
  surprise: a third registration source, device lifecycle events
  against a `NOT NULL` session column, a fourth suspension kind, no
  client attribution on a write, and a notification port with no
  mechanism behind it.
- `tool-system.md` gains a pointer to where its four reserved
  questions are audited, and the ordinal in its `tool.device_offline`
  sentence is corrected — the availability table grew rows for MCP
  authentication after that sentence was written.
- Section 29 gains its outward paragraph, which records two conflicts
  resolved in the specifications' favour rather than in the plan's.
  This is the same shape as ADR-0030's correction to Section 30.2 and
  weakens no requirement: both resolutions were already made
  elsewhere, and the paragraph reports them.
- The corpus now has three gate-less specifications, which is enough
  to make the pattern legible: a document earns gates by specifying
  behaviour, and a document that places, audits, or tools the corpus
  does not.
- `NotificationService` is now recorded as the largest open design
  question in the area rather than as a solved item in a port list.
  The corpus's `LISTEN`/`NOTIFY` transport is explicitly not it.

## Alternatives considered

- **Writing the four port contracts anyway**: rejected. Section 29.8
  defers exactly those four, and the deferral is reasoned — the
  contracts depend on a transport and an attach handshake that
  depend on use cases nobody has. A contract written without them
  would be guessed, and a guessed Protocol in `ports/` is worse than
  no Protocol, because the composition root would be built against
  it.
- **Folding the audit into `tool-system.md`**: rejected. That
  document already carries the device seam it owns — the target kind,
  the reserved domain, the offline outcome — and explicitly hands the
  rest elsewhere. Per-device scopes, the session-key resolver,
  notifications, and event placement are not tool-system subjects,
  and adding them would make the largest specification in the corpus
  the owner of a subject it declined.
- **Adding the surface half to `http-api-and-streaming.md`**:
  rejected. That document states that it *"adds nothing to the API
  surface that Section 16 did not already put there"*, and pairing
  endpoints and a session-key resolver would be new surface. The
  constraint is load-bearing and worth more than the convenience.
- **Declaring gates for the additive properties**: rejected, and it
  was tempting. A structural gate asserting that `DEVICE` appears in
  the forced-trust table would be checkable today. But it would
  duplicate hard gate 2 of `tool-system.md`, which already covers the
  whole `source` enum, and a gate that restates another gate makes
  the registry a worse map of the system. The audit's findings are
  properties of other documents' gates, not new ones.
- **Reopening ADR-0011**: rejected. Nothing in ADR-0011 is wrong.
  Its six decisions stand, its four ports stand, and its scope
  paragraph is the deferral this ADR is built on. This is a companion
  rather than a supersession.
- **Waiting for Milestone 10 to do the audit**: rejected, and this is
  the whole argument for the document existing. An audit run at
  Milestone 10 finds the same five problems, but it finds them while
  somebody is trying to ship, one at a time, each blocking work in
  progress. Run now, it costs a reading pass and produces a list.
  The additive assumption is also load-bearing *before* Milestone 10:
  it is the reason nine other documents were allowed to leave device
  handling as a reserved word.
- **A `device.` scope namespace**: rejected. See decision 5. The
  closed vocabulary and its gate are worth more than the symmetry
  with the tool-name domain, and the symmetry is misleading anyway,
  since the two namespaces would mean different things.
- **Treating Surfaces as a separate subject with a separate
  document**: rejected. They share the registry, the presence model,
  and the scope intersection with devices, and separating them would
  duplicate all three. What is genuinely surface-only — the
  session-key resolver and pairing — is two sections, not a
  document.
