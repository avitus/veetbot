---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 3 — model adapters and normalized streaming
  (in progress)
- **Authorized milestones:** Milestones 0, 1, 2, and 3
- **Project status:** Milestones 0, 1, and 2 are complete. Milestone 3 is in
  progress against 15 new hard gates and 72 cumulative gates. No later
  milestone is authorized.

Milestone 3 adds the OpenAI Responses, Anthropic Messages, and OpenAI-compatible
chat-completions adapters behind one normalized streaming contract. It also owns
declarative provider profiles and routing, model-call usage and cost accounting,
reasoning-state handling, redacted consent-gated trajectory export, and optional
live-provider verification. The machine-readable
[project state](../status/project-state.yaml) records progress and evidence.

Authoritative acceptance criteria for every milestone are defined only by the
canonical [engineering plan](engineering-plan.md); this page is a pointer, not a
substitute.

## Authorized work

- [Milestone 0 — Repository and engineering foundation](engineering-plan.md#milestone-0-repository-and-engineering-foundation)
- [Milestone 1 — In-memory vertical slice](engineering-plan.md#milestone-1-in-memory-vertical-slice)
- [Milestone 2 — PostgreSQL persistence and durable worker](engineering-plan.md#milestone-2-postgresql-persistence-and-durable-worker)
- [Milestone 3 — model adapters and normalized streaming](engineering-plan.md#milestone-3-model-adapters-and-normalized-streaming)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

No milestone later than Milestone 3 is authorized. Do not begin Milestone 4 or
any later milestone speculatively.

## Completion rule

Milestone 3 completes only when every canonical acceptance criterion, all 15
Milestone 3 gates and all 72 cumulative gates pass; the non-live suite passes
without provider credentials; optional credentialed live smoke tests are
reported accurately; and the hosted CircleCI workflow passes. Exact evidence
will be recorded in project state before the milestone is marked complete.
