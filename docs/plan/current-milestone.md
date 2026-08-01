---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 1 — In-memory vertical slice (in progress)
- **Authorized milestones:** Milestone 0 and Milestone 1
- **Project status:** Milestone 0 is complete; Milestone 1 implementation and
  all 28 of its hard gates are locally passing, with hosted CircleCI evidence
  still to be recorded before completion.

The in-memory vertical slice now contains the provider-neutral domain and ports,
five in-memory repositories, fake model, tool registry and execution pipeline,
calculator and current-time builtins, deterministic context assembly, the
inline runtime, CLI, and eleven evaluation cases. The machine-readable
[project state](../status/project-state.yaml) records completion evidence as it
is verified.

Authoritative acceptance criteria for every milestone are defined only by the
canonical [engineering plan](engineering-plan.md); this page is a pointer, not a
substitute.

## Authorized work

- [Milestone 0 — Repository and engineering foundation](engineering-plan.md#milestone-0-repository-and-engineering-foundation)
- [Milestone 1 — In-memory vertical slice](engineering-plan.md#milestone-1-in-memory-vertical-slice)
- [First assignment for the coding agent](engineering-plan.md#26-first-assignment-for-the-coding-agent)

No milestone later than Milestone 1 is authorized. Do not begin Milestone 2 or
any later milestone speculatively.

## Completion rule

Milestone 1 is complete only when every acceptance criterion in the canonical
engineering plan, all 41 gates active through Milestone 1, local verification,
and the hosted CircleCI workflow pass.
