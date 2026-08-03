---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 2 — PostgreSQL persistence and durable worker
  (in progress)
- **Authorized milestones:** Milestones 0, 1, and 2
- **Project status:** Milestones 0 and 1 are complete. Milestone 2 is authorized
  and implementation is in progress against its 16 new hard gates.

Milestone 2 adds the specified SQLAlchemy/PostgreSQL repositories, append-only
event log, durable checkpoints, fenced run queue, worker and maintenance roles,
recovery path, and projection scaffolds for ordinary process roles. The
process-local composition remains a supported implementation of the same ports
for deterministic evaluation and application-service tests. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.

Authoritative acceptance criteria for every milestone are defined only by the
canonical [engineering plan](engineering-plan.md); this page is a pointer, not a
substitute.

## Authorized work

- [Milestone 0 — Repository and engineering foundation](engineering-plan.md#milestone-0-repository-and-engineering-foundation)
- [Milestone 1 — In-memory vertical slice](engineering-plan.md#milestone-1-in-memory-vertical-slice)
- [Milestone 2 — PostgreSQL persistence and durable worker](engineering-plan.md#milestone-2-postgresql-persistence-and-durable-worker)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

No milestone later than Milestone 2 is authorized. Do not begin Milestone 3 or
any later milestone speculatively.

## Completion rule

Milestone 2 is complete only when every acceptance criterion in the canonical
engineering plan, all 57 gates active through Milestone 2, local database and
resilience verification, and the hosted CircleCI workflow pass.
