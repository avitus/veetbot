---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 11 — scheduled runs (implemented locally;
  final verification and hosted review remain)
- **Verified gate ceiling:** Milestone 9
- **Authorized workstreams:** Milestone 11 scheduling, alongside the unfinished
  Milestone 10 automatic-memory and self-authored-skills tranches and the
  provider-neutral web and authenticated-browser capability tranches.
- **Deferred:** New routing behavior, general-purpose subagents, arbitrary cron,
  workflow DAGs, and notification delivery.
- **Project status:** Milestones 0 through 9 are complete. Milestones 10 and 11
  are in progress. Milestone 11 cadence validation, pure recurrence, domain
  values, durable schedule persistence, fresh firing authority, atomic
  occurrence materialization, no-overlap behavior, admission, lifecycle and
  HTTP surfaces, terminal accounting, wakeup, metrics, reserved queue capacity,
  and the isolated production scheduler role are implemented locally. Hosted
  CI and the required final CodeRabbit review are not complete.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant rollout remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.
The construction gates and local repository checks pass; paired rollout
evidence, hosted CI, and the required GitHub CodeRabbit review remain open.

Milestone 11 is an independent, logically subsequent milestone because adding
scheduling to Milestone 10 would change that milestone's established thirty-four-gate
completion contract. Its [scheduled-runs design](scheduling.md) defines a
versioned schedule, immutable occurrences, deterministic civil time, bounded
misfires, fresh authorization at firing, and atomic creation of an ordinary
durable run. Scheduling remains default-off until all Milestone 11 gates pass.
The recurrence slice validates the closed cadence union and computes
next and latest-due instants directly from civil rules, including the declared
gap and fold semantics. The persistence slice adds shared adapter contracts,
four RLS-protected tables, unit-of-work composition, clean and stepwise
migrations, and explicit audited erasure of occurrence links. The gate contract
has been expanded to twenty-three entries so that discipline is explicit; this
does not advance the verified gate ceiling. The materialization slice locks a
due definition, revalidates current authority, creates the occurrence before
its deferred session and run links, seeds the ordinary checkpoint and event
sequence, and advances the definition in one PostgreSQL transaction. Its
concurrency and every-write rollback gates pass on PostgreSQL.
The overlap slice queries the latest materialized occurrence under the same
schedule lock and records `SKIPPED_OVERLAP` without changing the linked run or
failure counter. Admission has closed allow, transient-retry, and terminal-
reject outcomes. Its PostgreSQL controller serializes each tenant's decision
and enforces active-run, per-minute, active reservation, daily-cost, and
monthly-cost ceilings from versioned configuration.
The worker reads deterministic bounded batches, isolates one definition's
failure from its siblings, and sleeps until notification, the next durable due
time, or its fallback poll, with a bounded backoff for admission retries.
Lifecycle operations use exact scopes, optimistic revisions, immutable
definitions, request idempotency, stable pagination, and default-off HTTP
routes. Terminal run accounting is idempotent and resets or increments the
failure counter before applying the automatic-pause limit. A configured,
content-versioned principal directory supplies fresh firing authority. The
production scheduler uses a least-privilege PostgreSQL composition in the sole
bootstrap root and receives no model, tool, sandbox, object-store, provider, or
API bearer credential. Interactive and asynchronous workers claim disjoint
reserved priority classes. Fifteen positive scheduling and capacity limits
extend the executable configuration inventory to 121; two environment feature
flags independently default the API and worker off, and production activation
requires both together. All twenty-three schedule registry entries name real
checks. This local evidence does not advance the verified gate ceiling.

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
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

The five authorized workstreams are independently deliverable because they do
not share a delivery dependency. The
self-authored-skills contract is Section 30.6, the six Milestone 10
`gate.skill.*` entries, and the definition of done. The memory-formation
specification supplies eleven memory-maturation gates: five for ordinary
conversation and lifecycle, plus six for governed inspection and the
evaluation-gated provider path. The web-access tranche uses Section 32.3 and
the seven formal gates in
[web-access.md](web-access.md#hard-gates). Authorization does not extend to
`delegate.run` or new model-routing behavior.

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
is not the scheduler implementation. The Milestone 11 contract is its
twenty-three `gate.schedule.*` entries plus the acceptance criteria in the
engineering plan and [scheduling design](scheduling.md).

## Completion rule

The gate-bearing workstreams complete only when all thirty-four Milestone 10
gates and all 200 cumulative gates pass, the self-authored form of case 27 clears its
rollout threshold without increasing policy failures, all required CI lanes
pass on the final head, and the final CodeRabbit review has no finding or
unresolved conversation. The seven `gate.web.*` and ten `gate.browser.*`
entries make both network-capability tranches part of that formal Milestone 10
contract. Partial work does not advance the verified gate ceiling or mark
Milestone 10 complete.

Milestone 11 completes only when all twenty-three scheduling gates and all 223
cumulative gates pass, the PostgreSQL integration and resilience lanes pass,
the hosted CI lanes pass on the final head, and the final CodeRabbit review has
no finding or unresolved conversation. Even if its implementation finishes
first, the verified gate ceiling cannot advance through 11 until Milestone 10
also completes.
