---
title: Architecture
---

# Architecture

The platform is an explicitly bounded modular monolith. The normative module
layout and dependency rules are defined by the
[engineering plan](plan/engineering-plan.md#4-repository-structure), expanded
by the [bootstrap specification](plan/bootstrap-and-composition.md), and
recorded by [ADR-0001](adr/0001-modular-monolith.md).

## Implemented through Milestone 1

Milestone 0 supplies the repository foundation; Milestone 1 adds the first
complete provider-neutral vertical slice:

- `agent_core.config` owns deployment settings and validates the three
  configuration layers before resource construction.
- `agent_core.observability` owns structured-log setup, correlation context,
  bounded content logging, and recursive secret redaction.
- `agent_core.domain` contains provider-neutral agent, session, run, message,
  model-event, tool, policy-classification, and event-envelope types.
- `agent_core.ports` defines time, identity, repository, model, tool, context,
  dispatch, cancellation, and budget contracts.
- `agent_core.adapters` supplies fixed/system determinism, the scripted fake
  model, identity resolution, the inline dispatcher, and exactly five
  process-local repositories.
- `agent_core.tools` owns registration, Draft 2020-12 validation, canonical
  arguments, the single execution pipeline, and the calculator and current-time
  builtins.
- `agent_core.context` assembles a stable three-item prefix and volatile user
  region with checked hashes.
- `agent_core.runtime` computes provider-neutral model/tool steps; its executor
  is the only terminal run-state writer.
- `agent_core.application` exposes session and run services used by the CLI.
- `agent_core.evals` loads authored fake-model scripts and runs the eleven
  Milestone 1 cases through the ordinary composition only when requested.
- `agent_core.cli` exposes the installed `agent` entry point, including
  `agent run`, `agent session create`, and `agent eval run`.
- `agent_core.adapters.persistence` owns the pinned migration revision. No ORM
  or PostgreSQL repository implementation exists yet.

The structural gate walks imports and signatures rather than source text. It
enforces the domain, port, runtime, application, entry-point, provider-SDK,
ORM, evaluation-package, composition-root, determinism, and database-resource
boundaries before those packages fill in.

The Milestone 1 repositories have no durability, recovery, or cross-repository
transaction. No real provider, policy engine, memory, skill, MCP, or sandbox
execution behavior is claimed as implemented by this page.
