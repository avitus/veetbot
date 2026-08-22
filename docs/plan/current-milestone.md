---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 12 — notifications and device identity — is
  specified by [notifications-and-devices.md](notifications-and-devices.md);
  its twenty gates are registered and implementation is in progress. Milestone 13 —
  subagents and delegation — is specified by
  [subagents-and-delegation.md](subagents-and-delegation.md) with twenty-one
  gates and follows Milestone 12. Milestone 14 — inbound surfaces and
  pairing — is specified by [inbound-surfaces.md](inbound-surfaces.md) with
  twenty-one gates and follows Milestone 13. Milestone 15 — operational
  hardening — is specified by
  [operational-hardening.md](operational-hardening.md) with sixteen gates and
  follows Milestone 14; its backup tranche has no dependency on the three
  before it.
- **Verified gate ceiling:** Milestone 11 (227 gates).
- **Authorized workstreams:** Milestones 12 through 15 in order — notifications
  and device identity, general-purpose subagents and delegation, inbound
  surfaces and pairing, operational hardening (ADR-0061).
- **Deferred:** New model-routing behavior and everything listed in the
  engineering plan's roadmap subsection. Nothing on the roadmap is authorized
  until the owner says so and a specification with gates exists for it.
- **Project status:** Milestones 0 through 11 are complete: all 227 cumulative
  gates and the hosted lanes passed on final head `90e9142`, CodeRabbit passed,
  and every review conversation was resolved. Milestone 12 is in progress;
  Milestones 13 through 15 remain authorized and specified with twenty-one,
  twenty-one, and sixteen registered gates.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant activation remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes, and that activation is
roadmap item B1 rather than a Milestone 10 completion condition (ADR-0061).
The provider-assisted memory extractor's version-bound evidence passed on the
intended production model and ADR-0057 is accepted. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.

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
does implementation begin. Milestone 12 implementation is now active; its
[notifications-and-devices.md](notifications-and-devices.md) and ADR-0062 have
landed with twenty `gate.device.*` and `gate.notify.*` entries, and
Milestone 13's [subagents-and-delegation.md](subagents-and-delegation.md) and
ADR-0063 with twenty-one `gate.delegate.*` entries, and Milestone 14's
[inbound-surfaces.md](inbound-surfaces.md) and ADR-0064 with twenty-one
`gate.surface.*` entries, and Milestone 15's
[operational-hardening.md](operational-hardening.md) and ADR-0065 with sixteen
`gate.ops.*` entries; no authorized milestone reports a zero row.

Milestone 12 — notifications and device identity — build steps 1 through 6 are
implemented locally. The domain, persistence, and transactional producer foundation
now feeds a provider-partitioned dispatcher with a PostgreSQL claim lease, closed
retry schedule, staleness and expiry suppression, per-attempt delivery ledger,
accepted-send crash replay, immediate token invalidation, and fake and APNs push
transports under one shared contract. The APNs adapter verifies a mode-`0600` P-256
key, signs and refreshes ES256 provider tokens, selects sandbox or production per
device, sends the closed content-free payload over HTTP/2, and maps every provider
response into the closed outcome vocabulary. Six dispatcher and transport gates are
newly executable. The dedicated notify role adds a least-privilege repository set,
transactional PostgreSQL wakeups with a bounded polling fallback, default-off
settings, conditional trigger composition, a credential-minimized environment,
and an independently confined systemd unit. The feature-flagged public surface
adds all seven exact-scope routes, secret-free device views, stable device and
inbox pagination, targeted test pushes, content-free lifecycle audit, immediate
revocation, and offline recovery of every kind and delivery outcome. All twenty
gates are executable; the Apple client and final delivery verification in build
steps 7 and 8 remain.

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
activation gated on the capability-scenario evidence; Milestone 14's is its
twenty-one `gate.surface.*` entries plus the plan's acceptance criteria and
the [inbound-surfaces design](inbound-surfaces.md); Milestone 15's is its
sixteen `gate.ops.*` entries plus the plan's acceptance criteria and the
[operational-hardening design](operational-hardening.md).

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
