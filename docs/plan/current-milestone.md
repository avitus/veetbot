---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 10 — memory maturation and authorized
  capability tranches (in progress)
- **Verified gate ceiling:** Milestone 9
- **Authorized workstreams:** Automatic memory formation, the independently
  deliverable self-authored-skills tranche, and provider-neutral public-web
  access, plus provider-neutral authenticated browser automation.
- **Deferred:** Milestone 11 scheduling; Milestone 10 model-routing changes and
  general-purpose subagents.
- **Project status:** Milestones 0 through 9 are complete. Milestone 10 remains
  in progress; skill authoring stays behind default-off rollout controls, and
  web access is likewise disabled until an operator selects its providers.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant rollout remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.
The construction gates and local repository checks pass; paired rollout
evidence, hosted CI, and the required GitHub CodeRabbit review remain open.

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
- [Milestone 11 — scheduling](engineering-plan.md#milestone-11-scheduling)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

The four authorized workstreams are independently deliverable because they do
not share a delivery dependency. The
self-authored-skills contract is Section 30.6, the six Milestone 10
`gate.skill.*` entries, and the definition of done. The memory-formation
specification supplies eleven memory-maturation gates: five for ordinary
conversation and lifecycle, plus six for governed inspection and the
evaluation-gated provider path. The web-access tranche uses Section 32.3 and
the seven formal gates in
[web-access.md](web-access.md#hard-gates). Authorization does not extend to
`delegate.run`, Milestone 11 scheduling, or new model-routing behavior.

The browser tranche uses Section 33.3 and
[browser-automation.md](browser-automation.md#hard-gates). All ten formal gates
now resolve to executable checks. The completed implementation includes the
provider-neutral tools and policy seam, origin-confined Playwright adapters,
revision-bound approved actions, durable scoped profile/authentication/grant
metadata, the separately deployed AES-256-GCM profile service, exclusive
run-attempt leases, server-side storage sealing, hosted-provider composition,
the direct five-minute user login surface, public management routes, and exact
policy-revalidated standing grants with hard exclusions. Chromium, private
secret mounts, bounded container resources, controlled egress, loopback-only
ingress, HTTPS reverse proxying, release health waits, and migration/schema
contracts are part of the deployment gate. Scheduling remains Milestone 11 and
is not implied by this completed browser tranche.

## Completion rule

The gate-bearing workstreams complete only when all thirty-four Milestone 10
gates and all 200 cumulative gates pass, the self-authored form of case 27 clears its
rollout threshold without increasing policy failures, all required CI lanes
pass on the final head, and the final CodeRabbit review has no finding or
unresolved conversation. The seven `gate.web.*` and ten `gate.browser.*`
entries make both network-capability tranches part of that formal Milestone 10
contract. Partial work does not advance the verified gate ceiling or mark
Milestone 10 complete.
