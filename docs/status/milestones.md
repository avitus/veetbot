---
title: Milestones
---

# Milestones

This page is the human-readable summary of every milestone and the work still
open. It is a projection of the authoritative, machine-readable
[`project-state.yaml`](project-state.yaml): `make docs-check` fails when a
milestone appears here under the wrong group, under a stale title, or with a
checklist that disagrees with that file's `open_items`. Update the state file
first; this page follows it.

The **verified gate ceiling is Milestone 12** (247 cumulative gates). The
parallel workstreams below advance their own gates independently but never
move that ceiling; it advances only as Milestones 13 through 15 complete in
order. Which gate belongs to which milestone is the
[milestone map](../plan/milestone-map.md); the authoritative acceptance
criteria live in the [engineering plan](../plan/engineering-plan.md).

## Complete

- **Milestone 0 — Repository and engineering foundation**
- **Milestone 1 — In-memory vertical slice**
- **Milestone 2 — PostgreSQL persistence and durable worker**
- **Milestone 3 — Model adapters (OpenAI, Anthropic, OpenAI-compatible) and normalized streaming**
- **Milestone 4 — Policy, approvals, and complete tool lifecycle**
- **Milestone 5 — HTTP API and SSE**
- **Milestone 6 — Isolated execution and artifacts**
- **Milestone 7 — Context budgeting and structured working state**
- **Milestone 8 — Skills and MCP integration**
- **Milestone 9 — Long-term memory and knowledge retrieval**
- **Milestone 10 — Automatic memory, skills, public-web, and authenticated-browser tranches**
- **Milestone 11 — Scheduled runs**
- **Milestone 12 — Notifications and device identity** — completed with
  production APNs delivery owner-verified on a physical iPhone.
- **Milestone 16 — Memory evaluation and lifecycle** — the first parallel
  workstream to complete; hosted review finished clean on 2026-08-23.
- **Milestone 17 — Memory read API and browser** — the second completed
  parallel workstream; hosted review finished clean on 2026-08-24, followed by
  supplemental end-to-end PostgreSQL and native navigation coverage.

Evidence for completed milestones lives in
[`verification-history.yaml`](verification-history.yaml).

## In progress

### Milestone 13 — General-purpose subagents and delegation

The next sequential milestone. The delegation domain, the ledger persistence,
`delegate.run` with its one-transaction materializer, child-run suspension,
join, and the cancel cascade are implemented, and all twenty-one registered
gates pass locally. Remaining:

- [ ] Hosted CI on the milestone's final head
- [ ] CodeRabbit review loop on the dev to main pull request (build step 7)

### Milestone 19 — Conversational schedule creation

A parallel workstream, deliberately narrow: one-time schedule creation through
the model-callable `schedule.create` tool. Its five gates, complete non-live
suite, PostgreSQL lane, and clarification-to-approval journey pass locally.
Remaining:

- [ ] Hosted CI and the CodeRabbit review loop on the dev to main pull request

### Milestone 18 — First-class email integration

A parallel workstream providing three least-privilege Gmail MCP servers for
read and triage, drafts and label mutation, and approval-gated sending. Its
thirteen gates and full local repository check pass. Remaining:

- [ ] Owner real-mailbox smoke covering bootstrap consent, scheduled triage, phone approval, and one approved send
- [ ] Hosted CI and the CodeRabbit review loop on the dev to main pull request

### Milestone 20 — Calendar recurrence and conversational schedules

A fifth parallel workstream extending the existing scheduler with monthly and
yearly civil-calendar rules and widening `schedule.create` to daily, weekly,
monthly, and yearly recurrence. Its six gates, complete non-live suite, and
fresh PostgreSQL integration lane pass locally. Remaining:

- [ ] Hosted CI and the CodeRabbit review loop on the dev to main pull request

### Milestone 21 — Adaptive memory distillation

A sixth parallel workstream making memory formation materially less timid.
Its twenty-four gates cover integrated episodes, the fixed three-call
prediction-error pipeline, direct and hypothesis recall, evidence-based
forgetting, persistence, and comparative activation evidence. The local
implementation, static and contract suites, strict documentation build, Apple
package tests, and fresh PostgreSQL 16 integration lane pass. Remaining:

- [ ] Run the three-arm live corpus and publish passing evidence for the production tuple
- [ ] Complete per-stage cost telemetry required by the comparative gate
- [ ] Run hosted CI and the CodeRabbit review loop on the final head

### Milestone 22 — Persona surface and curated belief promotion

A seventh parallel workstream: an owner-edited persona document rendered as a
trusted Region A prefix row, with curated promotion — a formed belief reaches
instruction text only through the owner's explicit affirmation of a governed
nomination. All fourteen `gate.persona.*` checks, the full local repository
check, the Apple package lane, and a fresh PostgreSQL lane pass locally.
Remaining:

- [ ] Run hosted CI and the CodeRabbit review loop on the dev-to-main pull request

## Authorized

Specified, gated, and authorized, with implementation not yet begun.

- **Milestone 14 — Inbound surfaces and pairing** — twenty-one gates; follows
  Milestone 13 in the sequential order.
- **Milestone 15 — Operational hardening** — sixteen gates; follows
  Milestone 14, though its backup tranche depends on none of the three before
  it.
## Deferred

Nothing on the engineering plan's
[roadmap beyond Milestone 15](../plan/engineering-plan.md#roadmap-beyond-milestone-15)
is authorized until the owner says so and a specification with gates exists
for it. That roadmap holds, among its items: tenant activation of
self-authored skills (B1), dynamic model routing and a second provider
adapter (B2), Slack and email inbound surfaces (B3), email and webhook
notification transports (B4), scheduling residue such as cron, interval
multipliers, and dependency graphs (B5), the memory residue after Milestone 22 — the semantic arm,
`pgvector`, an external memory provider, and a learned memory policy (B6) — device-scoped tools and
presence routing (B7), standing approval grants (B8), the
trajectory-to-fine-tuning loop (B9), S3-compatible artifact storage (B10),
calendar integration and the other B11 surfaces beyond email, and
multi-tenant billing and quotas (B12).

Work each milestone explicitly set aside is recorded as that milestone's
`deferred_scope` in [`project-state.yaml`](project-state.yaml); a deferred
item re-enters only through a new authorization and, where the change is
architectural, an ADR.
