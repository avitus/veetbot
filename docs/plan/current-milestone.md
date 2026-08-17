---
title: Current Milestone
---

# Current milestone

- **Active milestone:** Milestone 10 — scheduling, model routing, subagents, and
  memory maturation (in progress)
- **Verified gate ceiling:** Milestone 9
- **Authorized milestones:** Milestones 0 through 10
- **Project status:** Milestones 0 through 9 are complete. The repository owner
  authorized Milestone 10 on 2026-08-17 and selected memory maturation as its
  first workstream. Milestone 10 is not complete.

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
- [Milestone 10 — scheduling, model routing, and subagents](engineering-plan.md#milestone-10-scheduling-model-routing-and-subagents)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

The machine-readable `current_milestone` remains 9 while Milestone 10 is in
progress because it is the hard-gate enforcement ceiling: all six pre-existing
skill-authoring gates and the memory-maturation gates must be implemented before
that ceiling can advance. `active_milestone: 10` and the authorization list
record the work now permitted. The memory-formation specification supplies the
acceptance gates for this workstream; the other Milestone 10 extensions retain
their own entry conditions and incomplete gates.

## Completion rule

Milestone 10 remains in progress until every Milestone 10 gate is implemented,
all cumulative checks pass on one head commit, the finding-free CodeRabbit loop
completes, and the reviewed pull request merges. Partial work does not advance
the verified gate ceiling or mark the milestone complete.
