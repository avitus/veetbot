---
title: Current Milestone
---

# Current milestone

- **Current milestone:** Milestone 0 — Repository and engineering foundation
- **Authorized next milestone:** Milestone 1 — In-memory vertical slice
- **Project status:** Milestone 0 implementation is in progress
  (`milestone_0_in_progress`).

The repository foundation and all locally runnable Milestone 0 gates are in
place, including the Docker Compose startup, migration round trip, and the four
CircleCI target partitions. The 106 version-controlled defaults are present and
enforced by an executable inventory. Completion remains open until the committed
CircleCI workflow has hosted execution evidence. The `dev` branch is pushed,
but CircleCI reports that `gh/avitus/veetbot` is not yet a connected project.
See the machine-readable
[project state](../status/project-state.yaml) for the recorded checks.

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

Milestone 1 must not be considered complete until Milestone 0 is complete and
**both** milestones satisfy every acceptance criterion defined in the canonical
engineering plan.
