---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 12 — notifications and device identity — is
  complete. Milestone 13 — general-purpose subagents and delegation — is the
  next sequential authorized milestone, is specified by
  [subagents-and-delegation.md](subagents-and-delegation.md) with twenty-one
  gates, and is in progress: build steps 1 through 5 and the deterministic
  half of 6 are implemented with every registered gate resolving to a live,
  locally passing check. Milestone 14 — inbound surfaces and
  pairing — is specified by [inbound-surfaces.md](inbound-surfaces.md) with
  twenty-one gates and follows Milestone 13. Milestone 15 — operational
  hardening — is specified by
  [operational-hardening.md](operational-hardening.md) with sixteen gates and
  follows Milestone 14; its backup tranche has no dependency on the three
  before it. Milestone 17 — the memory read API and the native memory
  browser — is specified by
  [memory-read-api-and-browser.md](memory-read-api-and-browser.md) with ten
  gates and completed as a second parallel workstream on 2026-08-24 without
  advancing the verified sequential ceiling.
  Milestone 18 — first-class email integration — is specified by
  [email-integration.md](email-integration.md) with thirteen gates and is a
  third parallel workstream on the same terms, its two shared-file touches
  named in ADR-0071. Milestone 19 — conversational schedule creation — is
  specified by [scheduling.md](scheduling.md) with five gates and is a fourth
  parallel workstream, limited to one-time creation through the model.
  Milestone 20 — SMS through the owner's iPhone — is specified by
  [device-channel-and-sms.md](device-channel-and-sms.md) with twelve gates
  and is a fifth parallel workstream on the same terms, its trigger-catalog
  and registration-source widenings named in ADR-0073.
- **Verified gate ceiling:** Milestone 12 (247 gates).
- **Authorized workstreams:** Milestones 13 through 15 in order — general-purpose
  subagents and delegation, inbound surfaces and pairing, operational hardening
  (ADR-0061) — plus Milestone 16 memory evaluation and lifecycle as an
  independently advancing parallel workstream whose gate ceiling cannot move
  ahead of Milestone 15 (ADR-0069); it completed on 2026-08-23. Milestone 17,
  the memory read API and the native memory browser, was authorized on
  2026-08-23 as a second such workstream (ADR-0070) and completed on
  2026-08-24 on the same terms, moving the verified ceiling no further.
  Milestone 18, first-class email integration, was authorized on 2026-08-24
  as a third (ADR-0071), on the same terms again. Milestone 19,
  conversational schedule creation, was authorized on 2026-08-24 as a fourth
  (ADR-0072), again without advancing the verified ceiling. Milestone 20,
  SMS through the owner's iPhone, was authorized on 2026-08-26 as a fifth
  (ADR-0073), again without advancing the verified ceiling.
- **Deferred:** New model-routing behavior and everything listed in the
  engineering plan's roadmap subsection. Nothing on the roadmap is authorized
  until the owner says so and a specification with gates exists for it.
- **Project status:** Milestones 0 through 12 are complete: all 247 cumulative
  gates, the full local and PostgreSQL lanes, Apple package and simulator lanes,
  hosted CI passed on the candidate head, and the completed integration was
  delivered directly to `dev`; code review is reserved for the final merge into
  `main`. Milestone 13 is in progress: the
  delegation domain values and limit derivation, the ledger persistence and
  erasure, delegate.run and its one-transaction materializer, the child-run
  suspension, join, and cancel cascade, the delegation limits block with
  tenant admission and the default-off flag, and case 32 with the tools arm
  overlay are implemented, with all twenty-one gates passing locally; the
  capability scenario awaits the owner's redacted failed trajectory, and
  hosted CI and the CodeRabbit review loop remain outstanding. Milestones 14
  and 15 remain authorized and specified with twenty-one and sixteen
  registered gates; neither has started. Milestone 16, the parallel memory-evaluation
  workstream, has implemented all twenty of its gates, republished the
  provider evidence at `formation@8`, and re-recorded its baseline, and completed on 2026-08-23 when hosted CI and
  the CodeRabbit review loop finished clean on the `dev` to `main` pull
  request (merge `571f6d9`); the verified gate ceiling stays at Milestone 12. Its live benchmark arm ran three times — the first two failed
  only the absolute incomplete-runs condition, at two of 132 probe arms each,
  after which the harness gained a content-free failure class and one retry per
  probe arm, and the third published
  `evals/capability/memory-benchmark-evidence.192a0161d881837218c0ed125c55a121663f8eda.json`
  with four retried runs and a lift of forty-five — which is milestone evidence
  rather than one of the two completion conditions below. Milestone 17, the
  second parallel workstream, completed on 2026-08-24: all ten gates, the
  Python and PostgreSQL lanes, the native Apple package and simulator lanes,
  hosted CI, GitGuardian, and the final CodeRabbit review passed on the
  dev-to-main pull request (merge `3faa978`), with supplemental end-to-end
  browser coverage later passing the same hosted gates in merge `07c8bdf`.
  Milestone 18, the third parallel workstream, is
  authorized and specified with thirteen registered gates; it has not
  started. Milestone 19, the fourth parallel workstream, is in progress with
  five registered gates; its one-time model tool, clarification-to-approval
  regression, complete non-live suite, and PostgreSQL lane pass locally, with
  only hosted CI and the final CodeRabbit review outstanding. Each in-progress
  milestone's remaining work is itemized on the
  [milestones page](../status/milestones.md), which `make docs-check`
  reconciles against the project state.

Milestone 10A adds governed foreground skill authoring and an optional,
non-joining background-review child run. Authoring stays disabled by default;
tenant activation remains blocked until the evaluation threshold in the
[skills design](skills.md#rollout-evidence) passes, and that activation is
roadmap item B1 rather than a Milestone 10 completion condition (ADR-0061).
The provider-assisted memory extractor's evidence is accepted under ADR-0057.
ADR-0068's semantic deterministic repair and retry lifecycle advanced the active
policies to deterministic `formation@5` and provider-assisted `formation@6`, and
Milestone 16's admission of working-state established facts advances them again
to `formation@7` and `formation@8` (ADR-0069). Milestone 16 republished the
provider evidence at `formation@8` on the intended production model and deleted
the superseded `formation@4` artifact, so `auto` again activates provider
assistance for the balanced OpenAI `gpt-5.6-sol` and default-profile tuple and
falls back safely for every other one. Tenant activation remains
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
and ADR-0069 landed the same way, with twenty further `gate.memory.*`
entries in the existing area, and it never showed a zero row because its
authorization and its specification arrived together. Milestone 17's
[memory-read-api-and-browser.md](memory-read-api-and-browser.md) and ADR-0070
landed on the same day as that milestone's authorization, adding ten more
`gate.memory.*` entries to the same area. Milestone 18's
[email-integration.md](email-integration.md) and ADR-0071 landed the same
way on 2026-08-24, adding thirteen `gate.email.*` entries in a new area of
their own. Milestone 19 reuses [scheduling.md](scheduling.md) and the existing
`schedule` area under ADR-0072, adding five gates for the deliberately narrow
one-time creation bridge.

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
APNs activation was completed by the owner on 2026-08-23: a signed TestFlight
build registered a physical iPhone as a production target and the authenticated
test-notification route delivered the Test notification alert. That external
activation followed repository completion and leaves the default-off contract
unchanged for fresh deployments.

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
- [Milestone 17 — memory read API and browser](engineering-plan.md#milestone-17-memory-read-api-and-browser)
- [Milestone 18 — first-class email integration](engineering-plan.md#milestone-18-first-class-email-integration)
- [Milestone 19 — conversational schedule creation](engineering-plan.md#milestone-19-conversational-schedule-creation)
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
is its twenty `gate.memory.*` entries plus the plan's acceptance criteria
and the
[memory-evaluation-and-lifecycle design](memory-evaluation-and-lifecycle.md),
whose benchmark baseline is re-recorded deliberately by every change that
moves it. Milestone 17's contract is its ten `gate.memory.*` entries —
`read_api_ceiling_required`, `read_api_ceiling_filter`,
`read_api_principal_isolation`, `read_api_pagination`, `read_api_filters`,
`read_api_read_only`, `read_api_flag_absent`, `browse_contract_parity`,
`read_api_view_projection`, and `read_api_error_vocabulary` — plus the plan's
acceptance criteria and the
[memory-read-api-and-browser design](memory-read-api-and-browser.md).
Milestone 18's contract is its thirteen `gate.email.*` entries plus the
plan's acceptance criteria and the
[email-integration design](email-integration.md). Milestone 19's contract is
its five new `gate.schedule.*` entries plus the plan's acceptance criteria and
the [scheduling design](scheduling.md#model-callable-one-time-creation).

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

Milestone 16 completed on those terms on 2026-08-23: every declared gate and
the cumulative registry passed, the PostgreSQL lanes passed, and hosted CI and
the final CodeRabbit review finished clean on the `dev` to `main` pull request.

Milestone 17 completes on the same terms, with its own additional condition:
the native Apple package and simulator lanes pass, because the browser is half
of what the milestone delivers and no Python gate observes Swift. Being a
parallel workstream changes nothing about the ceiling, which still advances
only after every earlier milestone has completed.

Milestone 17 completed on those terms on 2026-08-24: every declared gate and
the cumulative registry passed, the PostgreSQL and native Apple lanes passed,
and hosted CI, GitGuardian, and the final CodeRabbit review finished clean on
dev-to-main pull request 58. Pull request 64 subsequently added end-to-end
PostgreSQL pagination and iPhone/iPad navigation coverage with the same hosted
lanes green and no unresolved review conversation.

Milestone 18 completes on the same terms, with its own additional condition:
the owner's real-mailbox smoke — bootstrap consent, a scheduled triage run,
an approval delivered to the phone, and one approved send — is recorded as
evidence, because the mailbox is an external system no gate's fake can vouch
for. Being a parallel workstream changes nothing about the ceiling, which
still advances only after every earlier milestone has completed.

Milestone 19 completes when its five gates and the cumulative registry pass,
all relevant local and hosted lanes pass on the final head, and the final
CodeRabbit review is clean. Because it is a parallel workstream, completion
does not advance the verified gate ceiling past the sequential milestones.
