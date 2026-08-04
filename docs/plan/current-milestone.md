---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 5 — HTTP API and SSE (local verification
  complete; hosted review pending)
- **Next milestone:** Milestone 6 — isolated execution and artifacts
- **Authorized milestones:** Milestones 0 through 9
- **Project status:** Milestones 0 through 4 are complete. Milestone 5 is active;
  Milestones 6 through 9 are explicitly authorized but remain sequentially
  blocked on the active milestone's review cycle.

Milestone 5 delivers the authenticated HTTP API, session and message routes,
run retrieval and cancellation, user-input suspension and resume, gapless SSE
replay with transient delivery, request and idempotency identifiers, error
envelopes, artifact downloads, and health probes. The
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

Complete the Milestone 5 review cycle before beginning Milestone 6. Milestone 10
is not authorized because the readiness review records that it has no complete
acceptance criteria.

## Completion rule

Milestone 5 will complete only after every canonical acceptance criterion, all
ten API gates plus the cancellation gate and all 105 cumulative gates pass, the
full non-live suite passes against PostgreSQL, hosted CircleCI passes, and
CodeRabbit has no unaddressed actionable review comments on the milestone pull
request.
