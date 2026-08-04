---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 3 — model adapters and normalized streaming
  (complete)
- **Authorized milestones:** Milestones 0, 1, 2, and 3
- **Project status:** Milestones 0 through 3 are complete. Milestone 4 is not
  authorized, and no later milestone may begin speculatively.

Milestone 3 delivered the OpenAI Responses, Anthropic Messages, and OpenAI-compatible
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
- [Milestone 3 — model adapters and normalized streaming](engineering-plan.md#milestone-3-model-adapters-openai-anthropic-openai-compatible-and-normalized-streaming)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

No milestone later than Milestone 3 is authorized. Do not begin Milestone 4 or
any later milestone speculatively.

## Completion rule

Milestone 3 completed after every canonical acceptance criterion, all 15
Milestone 3 gates and all 72 cumulative gates passed; the 255-test non-live
suite passed without provider credentials; both credentialed live smoke tests
passed; and the hosted CircleCI static, contract, and integration jobs passed.
The exact evidence is recorded in project state.
