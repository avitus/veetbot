---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 9 — long-term memory and knowledge retrieval
  (complete)
- **Next milestone:** None authorized. Milestone 10 remains an open direction.
- **Authorized milestones:** Milestones 0 through 9
- **Project status:** Milestones 0 through 9 are complete. Milestone 10 remains
  unauthorized and has no acceptance criteria of its own.

Milestone 9 adds governed, provenance-linked belief formation; deterministic
hybrid retrieval with frozen session snapshots and faithful recall traces; a
human memory-management surface; and separately governed knowledge-document
ingestion, passage retrieval, citations, versions, and deletion. The
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
- [Milestone 6 — isolated execution and artifacts](engineering-plan.md#milestone-6-isolated-execution-and-artifacts)
- [Milestone 7 — context budgeting and structured working state](engineering-plan.md#milestone-7-context-budgeting-and-structured-working-state)
- [Milestone 8 — skills and MCP integration](engineering-plan.md#milestone-8-skills-and-mcp-integration)
- [Milestone 9 — long-term memory and knowledge retrieval](engineering-plan.md#milestone-9-long-term-memory-and-knowledge-retrieval)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

Milestone 9 began after the reviewed Milestone 8 pull request passed hosted CI
and merged into `dev`. Milestone 10 is not authorized because the readiness
review records that it has no complete acceptance criteria.

## Completion rule

Milestone 9 is complete. All 26 new gates and all 166 cumulative gates, 526
non-live tests, all hosted CircleCI lanes, and the final finding-free CodeRabbit
review passed. Every review thread is resolved. Its reviewed pull request must
merge into `dev` before any later milestone may begin.
