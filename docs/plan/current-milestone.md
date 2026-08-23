---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 12 — notifications and device identity — is
  complete. Milestone 13 — general-purpose subagents and delegation — is the
  next authorized milestone, is specified by
  [subagents-and-delegation.md](subagents-and-delegation.md) with twenty-one
  gates, and has not started. Milestone 14 — inbound surfaces and
  pairing — is specified by [inbound-surfaces.md](inbound-surfaces.md) with
  twenty-one gates and follows Milestone 13. Milestone 15 — operational
  hardening — is specified by
  [operational-hardening.md](operational-hardening.md) with sixteen gates and
  follows Milestone 14; its backup tranche has no dependency on the three
  before it.
- **Verified gate ceiling:** Milestone 12 (247 gates).
- **Authorized workstreams:** Milestones 13 through 15 in order — general-purpose
  subagents and delegation, inbound surfaces and pairing, operational hardening
  (ADR-0061) — plus Milestone 16 memory evaluation and lifecycle as an
  independently advancing parallel workstream whose gate ceiling cannot move
  ahead of Milestone 15 (ADR-0069).
- **Deferred:** New model-routing behavior and everything listed in the
  engineering plan's roadmap subsection. Nothing on the roadmap is authorized
  until the owner says so and a specification with gates exists for it.
- **Project status:** Milestones 0 through 12 are complete: all 247 cumulative
  gates, the full local and PostgreSQL lanes, Apple package and simulator lanes,
  hosted CI passed on the candidate head, and the completed integration was
  delivered directly to `dev`; code review is reserved for the final merge into
  `main`. Milestones 13 through 15 remain
  authorized and specified with twenty-one, twenty-one, and sixteen registered
  gates; none has started.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant activation remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes, and that activation is
roadmap item B1 rather than a Milestone 10 completion condition (ADR-0061).
The provider-assisted memory extractor's historical `formation@4` evidence
passed on the intended production model and ADR-0057 is accepted. ADR-0068's
semantic deterministic repair and retry lifecycle advance the active policies to
deterministic `formation@5` and provider-assisted `formation@6`; `auto` now falls
back safely until the current tuple is reevaluated. Tenant activation remains
separate from Milestone completion under ADR-0061. The machine-readable [project
state](../status/project-state.yaml) records progress and evidence.

Milestone 11 was an independent, logically subsequent milestone because adding
scheduling to Milestone 10 would have changed that milestone's established
completion contract. Its [scheduled-runs design](scheduling.md) defines a
versioned schedule, immutable occurrences, deterministic civil time, bounded
misfires, fresh authorization at firing, and atomic creation of an ordinary
durable run; all twenty-three schedule registry entries name real checks, the
production scheduler is a least-privilege role, and scheduling remains
default-off until explicitly activated. Hosted CI and the final CodeRabbit
review passed on head `90e9142`, advancing the verified ceiling through 11.

Milestones 12 through 15 follow the pattern Milestone 11 set: a detailed-design
document and an ADR land first, register the milestone's gates, and only then
does implementation begin. Milestone 12 implementation is complete; its
[notifications-and-devices.md](notifications-and-devices.md) and ADR-0062 have
landed with twenty `gate.device.*` and `gate.notify.*` entries, and
Milestone 13's [subagents-and-delegation.md](subagents-and-delegation.md) and
ADR-0063 with twenty-one `gate.delegate.*` entries, and Milestone 14's
[inbound-surfaces.md](inbound-surfaces.md) and ADR-0064 with twenty-one
`gate.surface.*` entries, and Milestone 15's
[operational-hardening.md](operational-hardening.md) and ADR-0065 with sixteen
`gate.ops.*` entries; no authorized milestone reports a zero row. Milestone
16's [memory-evaluation-and-lifecycle.md](memory-evaluation-and-lifecycle.md)
and ADR-0069 landed the same way, with nineteen further `gate.memory.*`
entries in the existing area, and it never showed a zero row because its
authorization and its specification arrived together.

Milestone 12 — notifications and device identity — completed all eight build
steps. The delivered slice includes the principal-scoped device registry,
transactional content-free outbox, provider-partitioned claim and retry worker,
APNs HTTP/2 adapter, least-privilege `notify` role, seven exact-scope routes,
offline inbox, and native Apple registration and deep-link restoration. Review
hardening made registration request identity deterministic, preserves partial
per-target delivery outcomes, and executes queued navigation plus initial and
changed focus behavior. All twenty gates and all 247 cumulative gates pass;
PostgreSQL, Apple package, iPhone and iPad UI, unsigned Release builds, and hosted
CI pass; the completed integration is delivered directly to `dev`. Production
APNs activation remains default-off until the owner supplies the external Apple
capability, provisioning profiles, and provider key.

Authoritative acceptance criteria for every milestone are defined only by the
canonical [engineering plan](engineering-plan.md); this page is a pointer, not a
substitute.

## Authorized work

- [Milestone 0 — Repository and engineering foundation](engineering-plan.md#milestone-0-repository-and-engineering-foundation)
- [Milestone 1 — In-memory vertical slice](engineering-plan.md#milestone-1-in-memory-vertical-slice)
- [Milestone 2 — PostgreSQL persistence and durable worker](engineering-plan.md#milestone-2-postgresql-persistence-and-durable-worker)
- [Milestone 3 — model adapters and normalized streaming](engineering-plan.md#milestone-3-model-adapters-openai-anthropic-openai-compatible-and-normalized-streaming)
- [Milestone 4 — policy, approvals, and complete tool lifecycle](engineering-plan.md#milestone-4-policy-approvals-and-complete-tool-lifecycle)
- [Milestone 5 — HTTP API and SSE](engineering-plan.md#milestone-5-http-api-and-sse)
- [Milestone 6 — isolated execution and artifacts](engineering-plan.md#milestone-6-isolated-execution-and-artifacts)
- [Milestone 7 — context budgeting and structured working state](engineering-plan.md#milestone-7-context-budgeting-and-structured-working-state)
- [Milestone 8 — skills and MCP integration](engineering-plan.md#milestone-8-skills-and-mcp-integration)
- [Milestone 9 — long-term memory and knowledge retrieval](engineering-plan.md#milestone-9-long-term-memory-and-knowledge-retrieval)
- [Milestone 10 — memory maturation](engineering-plan.md#memory-maturation)
- [Milestone 10A — self-authored skills](engineering-plan.md#self-authored-skills-authorized-tranche)
- [Milestone 10B — web access](engineering-plan.md#32-web-access)
- [Milestone 10C — authenticated browser automation](engineering-plan.md#33-authenticated-browser-automation)
- [Milestone 11 — scheduled runs](engineering-plan.md#milestone-11-scheduled-runs)
- [Milestone 12 — notifications and device identity](engineering-plan.md#milestone-12-notifications-and-device-identity)
- [Milestone 13 — general-purpose subagents and delegation](engineering-plan.md#milestone-13-general-purpose-subagents-and-delegation)
- [Milestone 14 — inbound surfaces and pairing](engineering-plan.md#milestone-14-inbound-surfaces-and-pairing)
- [Milestone 15 — operational hardening](engineering-plan.md#milestone-15-operational-hardening)
- [Milestone 16 — memory evaluation and lifecycle](engineering-plan.md#milestone-16-memory-evaluation-and-lifecycle)
- [Roadmap beyond Milestone 15](engineering-plan.md#roadmap-beyond-milestone-15)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

The Milestone 10 workstreams are independently deliverable because they do
not share a delivery dependency. The self-authored-skills contract is Section
30.6, the six `gate.skill.*` entries, and the definition of done. The
memory-formation specification supplies fifteen memory-maturation gates for
ordinary conversation and lifecycle, governed inspection, and the
evaluation-gated provider path. The web-access tranche uses Section 32.3 and
the seven formal gates in [web-access.md](web-access.md#hard-gates); the
browser tranche uses Section 33.3 and the ten gates in
[browser-automation.md](browser-automation.md#hard-gates). The Milestone 11
contract is its twenty-three `gate.schedule.*` entries plus the acceptance
criteria in the engineering plan and the [scheduling design](scheduling.md).

Milestone 12's contract is its twenty `gate.device.*` and `gate.notify.*`
entries plus the acceptance criteria in the engineering plan and the
[notifications-and-devices design](notifications-and-devices.md); Milestone
13's is its twenty-one `gate.delegate.*` entries plus the plan's acceptance
criteria and the [delegation design](subagents-and-delegation.md), with tenant
activation gated on the capability-scenario evidence; Milestone 14's is its
twenty-one `gate.surface.*` entries plus the plan's acceptance criteria and
the [inbound-surfaces design](inbound-surfaces.md); Milestone 15's is its
sixteen `gate.ops.*` entries plus the plan's acceptance criteria and the
[operational-hardening design](operational-hardening.md); and Milestone 16's
is its nineteen `gate.memory.*` entries plus the plan's acceptance criteria
and the
[memory-evaluation-and-lifecycle design](memory-evaluation-and-lifecycle.md),
whose benchmark baseline is re-recorded deliberately by every change that
moves it.

## Completion rule

Milestone 10 completed after all thirty-eight Milestone 10 gates and all 204
cumulative gates passed, all required CI lanes passed on the final head, and the
final CodeRabbit review had no finding or unresolved conversation. Enabling
authoring for a tenant is separately governed by the rollout evidence rule and
is not a completion condition (ADR-0061). Partial work does not advance the
verified gate ceiling.

Milestone 11 completed after all twenty-three scheduling gates and all 227
cumulative gates passed, the PostgreSQL integration and resilience lanes passed,
the hosted CI lanes passed on the final head, and the final CodeRabbit review
had no finding or unresolved conversation.

Milestones 12 through 15 complete, in order, when every gate their specification
declares and the cumulative registry pass, the PostgreSQL lanes pass where the
milestone touches persistence, hosted CI passes on the final head, and the final
CodeRabbit review is clean. The verified ceiling advances through each only
after every earlier milestone has completed.

Milestone 16 completes on the same terms, with its own additional condition:
the checked-in benchmark baseline equals a fresh deterministic run exactly and
the provider-assisted extraction evidence has been republished at its new
policy version. Being a parallel workstream changes nothing about the ceiling,
which still advances only after every earlier milestone has completed.
