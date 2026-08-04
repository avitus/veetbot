---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 6 — isolated execution and artifacts (review)
- **Next milestone:** Milestone 7 — context budgeting and structured working state
- **Authorized milestones:** Milestones 0 through 9
- **Project status:** Milestones 0 through 5 are complete. Milestone 6 is locally
  implemented; the latest CodeRabbit follow-up findings are remediated locally
  and await hosted CI and another clean review. Milestones 7 through 9 are
  explicitly authorized and remain sequentially gated.

Milestone 6 adds container-backed and deterministic fake execution adapters,
lease-scoped isolated workspaces, resource and egress enforcement,
`sandbox.run_command`, the in-sandbox programmatic tool bridge, filesystem
artifacts with tenant-authorized verified downloads, output artifactization,
cleanup, and real-runtime security tests. The
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

Begin Milestone 7 only after the Milestone 6 pull request is merged. Milestone 10
is not authorized because the readiness review records that it has no complete
acceptance criteria.

## Completion rule

Milestone 6 is ready for follow-up review: every canonical acceptance criterion
and all 116 cumulative gates pass locally, including the real-runtime security
partition. Completion still requires hosted CircleCI to pass on the remediation
commit and CodeRabbit to have no unaddressed actionable review comments on the
milestone pull request.
