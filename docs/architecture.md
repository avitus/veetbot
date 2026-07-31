---
title: Architecture
---

# Architecture

The platform is an explicitly bounded modular monolith. The normative module
layout and dependency rules are defined by the
[engineering plan](plan/engineering-plan.md#4-repository-structure), expanded
by the [bootstrap specification](plan/bootstrap-and-composition.md), and
recorded by [ADR-0001](adr/0001-modular-monolith.md).

## Implemented in Milestone 0

Milestone 0 contains only the foundation needed before the vertical slice:

- `agent_core.config` owns deployment settings and validates the three
  configuration layers before resource construction.
- `agent_core.observability` owns structured-log setup, correlation context,
  bounded content logging, and recursive secret redaction.
- `agent_core.cli` exposes the installed `agent` entry point; runtime commands
  intentionally begin in later milestones.
- `agent_core.adapters.persistence` owns the pinned migration revision. No ORM
  or repository implementation exists yet.

The structural gate walks imports and signatures rather than source text. It
enforces the domain, port, runtime, application, entry-point, provider-SDK,
ORM, evaluation-package, composition-root, determinism, and database-resource
boundaries before those packages fill in.

No future runtime, provider, policy, memory, skill, MCP, or sandbox behavior is
claimed as implemented by this page.
