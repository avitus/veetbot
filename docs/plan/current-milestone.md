---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 4 — policy, approvals, and complete tool
  lifecycle (in progress)
- **Authorized milestones:** Milestones 0 through 4
- **Project status:** Milestones 0 through 3 are complete. Milestone 4 is
  explicitly authorized and in progress; no later milestone may begin until
  its reviewed pull request is complete.

Milestone 4 delivers the deterministic policy engine, principal scope checks,
durable approvals and pause/resume, the three workspace text tools,
`demo.external_write`, and the complete tool lifecycle. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.

Authoritative acceptance criteria for every milestone are defined only by the
canonical [engineering plan](engineering-plan.md); this page is a pointer, not a
substitute.

## Authorized work

- [Milestone 0 — Repository and engineering foundation](engineering-plan.md#milestone-0-repository-and-engineering-foundation)
- [Milestone 1 — In-memory vertical slice](engineering-plan.md#milestone-1-in-memory-vertical-slice)
- [Milestone 2 — PostgreSQL persistence and durable worker](engineering-plan.md#milestone-2-postgresql-persistence-and-durable-worker)
- [Milestone 3 — model adapters and normalized streaming](engineering-plan.md#milestone-3-model-adapters-openai-anthropic-openai-compatible-and-normalized-streaming)
- [Milestone 4 — policy, approvals, and complete tool lifecycle](engineering-plan.md#milestone-4-policy-approvals-and-complete-tool-lifecycle)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

No milestone later than Milestone 4 is authorized. Complete the Milestone 4
review cycle before beginning Milestone 5.

## Completion rule

Milestone 4 completes only after every canonical acceptance criterion, all 22
Milestone 4 gates and all 94 cumulative gates pass, the full non-live suite
passes against PostgreSQL, hosted CircleCI passes, and CodeRabbit has no
unaddressed actionable review comments on the milestone pull request. Exact
evidence will be recorded in project state before the milestone is marked
complete.
