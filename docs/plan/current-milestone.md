---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 4 — policy, approvals, and complete tool
  lifecycle (complete)
- **Next authorized milestone:** Milestone 5 — HTTP API and SSE (not started)
- **Authorized milestones:** Milestones 0 through 5
- **Project status:** Milestones 0 through 4 are complete. Milestone 5 is
  explicitly authorized; its implementation state advances with the code and
  tests that activate its gates.

Milestone 4 delivered the deterministic policy engine, principal scope checks,
durable approvals and pause/resume, the workspace tools, and the complete tool
lifecycle. Milestone 5 will deliver the authenticated HTTP API, session and
message routes, run retrieval and cancellation, gapless SSE replay, request and
idempotency identifiers, error envelopes, and health probes. The
machine-readable [project state](../status/project-state.yaml) records progress
and evidence.

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
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

No milestone later than Milestone 5 is authorized. Complete the Milestone 5
review cycle before beginning Milestone 6.

## Completion rule

Milestone 4 is complete with its exact evidence recorded in project state.
Milestone 5 will complete only after every canonical acceptance criterion, all
ten API gates plus the cancellation gate and all 105 cumulative gates pass, the
full non-live suite passes against PostgreSQL, hosted CircleCI passes, and
CodeRabbit has no unaddressed actionable review comments on the milestone pull
request.
