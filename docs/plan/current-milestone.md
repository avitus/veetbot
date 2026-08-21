---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 11 — scheduled runs (implemented locally;
  hosted CI and the final review remain). Milestone 12 — notifications and
  device identity — is authorized and specified by
  [notifications-and-devices.md](notifications-and-devices.md); its twenty
  gates are registered and implementation may begin. Milestone 13 —
  subagents and delegation — is specified by
  [subagents-and-delegation.md](subagents-and-delegation.md) with twenty-one
  gates and follows Milestone 12.
- **Verified gate ceiling:** Milestone 9 (166 gates).
- **Authorized workstreams:** Milestone 10's four tranches (automatic memory,
  self-authored skills, public-web access, authenticated browser automation),
  Milestone 11 scheduling, and Milestones 12 through 15 in order —
  notifications and device identity, general-purpose subagents and delegation,
  inbound surfaces and pairing, operational hardening (ADR-0061).
- **Deferred:** New model-routing behavior and everything listed in the
  engineering plan's roadmap subsection. Nothing on the roadmap is authorized
  until the owner says so and a specification with gates exists for it.
- **Project status:** Milestones 0 through 9 are complete. Milestones 10 and
  11 are implemented locally; every registered gate passes (227 cumulative),
  and hosted CI plus the final CodeRabbit review remain for both. Milestones
  12 and 13 are authorized and specified with twenty and twenty-one registered
  gates. Milestones 14 and 15 are authorized and unspecified: each starts with
  its detailed-design document and ADR, which declare the milestone's gates.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant activation remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes, and that activation is
roadmap item B1 rather than a Milestone 10 completion condition (ADR-0061).
The provider-assisted memory extractor's version-bound evidence passed on the
intended production model and ADR-0057 is accepted. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.

Milestone 11 is an independent, logically subsequent milestone because adding
scheduling to Milestone 10 would have changed that milestone's established
completion contract. Its [scheduled-runs design](scheduling.md) defines a
versioned schedule, immutable occurrences, deterministic civil time, bounded
misfires, fresh authorization at firing, and atomic creation of an ordinary
durable run; all twenty-three schedule registry entries name real checks, the
production scheduler is a least-privilege role, and scheduling remains
default-off until the milestone closes. This local evidence does not advance
the verified gate ceiling.

Milestones 12 through 15 follow the pattern Milestone 11 set: a detailed-design
document and an ADR land first, register the milestone's gates, and only then
does implementation begin. Milestone 12's
[notifications-and-devices.md](notifications-and-devices.md) and ADR-0062 have
landed with twenty `gate.device.*` and `gate.notify.*` entries, and
Milestone 13's [subagents-and-delegation.md](subagents-and-delegation.md) and
ADR-0063 with twenty-one `gate.delegate.*` entries; the census reports a zero
row for each of 14 and 15 until its specification declares gates.

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
activation gated on the capability-scenario evidence. Milestones 14 and 15 each
own one gate area declared by their design document: surfaces and operations.
Their acceptance criteria are in the engineering plan today; their gates exist
only once the specification that declares them lands.

## Completion rule

Milestone 10 completes when all thirty-eight Milestone 10 gates and all 204
cumulative gates pass, all required CI lanes pass on the final head, and the
final CodeRabbit review has no finding or unresolved conversation. Enabling
authoring for a tenant is separately governed by the rollout evidence rule and
is not a completion condition (ADR-0061). Partial work does not advance the
verified gate ceiling.

Milestone 11 completes only when all twenty-three scheduling gates and all 227
cumulative gates pass, the PostgreSQL integration and resilience lanes pass,
the hosted CI lanes pass on the final head, and the final CodeRabbit review has
no finding or unresolved conversation. Even if its implementation finishes
first, the verified gate ceiling cannot advance through 11 until Milestone 10
also completes.

Milestones 12 through 15 complete, in order, when every gate their specification
declares and the cumulative registry pass, the PostgreSQL lanes pass where the
milestone touches persistence, hosted CI passes on the final head, and the final
CodeRabbit review is clean. The verified ceiling advances through each only
after every earlier milestone has completed.
