---
title: Architecture
---

# Architecture

The platform is an explicitly bounded modular monolith. The normative module
layout and dependency rules are defined by the
[engineering plan](plan/engineering-plan.md#4-repository-structure), expanded
by the [bootstrap specification](plan/bootstrap-and-composition.md), and
recorded by [ADR-0001](adr/0001-modular-monolith.md).

## Implemented through Milestone 2

Milestone 0 supplies the repository foundation, Milestone 1 adds the first
complete provider-neutral vertical slice, and Milestone 2 makes that slice
durable and separately executable:

- `agent_core.config` owns deployment settings and validates the three
  configuration layers before resource construction.
- `agent_core.observability` owns structured-log setup, correlation context,
  bounded content logging, and recursive secret redaction.
- `agent_core.domain` contains provider-neutral agent, session, run, message,
  model-event, tool, policy-classification, and event-envelope types.
- `agent_core.ports` defines time, identity, unit-of-work, repository, model,
  tool, context, queue, dispatch, cancellation, and budget contracts.
- `agent_core.adapters` supplies fixed/system determinism, the scripted fake
  model, identity resolution, inline and PostgreSQL dispatch, the process-local
  tier, and the SQLAlchemy/PostgreSQL persistence tier. ORM rows and hand-written
  translations remain confined to `adapters/persistence/`.
- `agent_core.tools` owns registration, Draft 2020-12 validation, canonical
  arguments, the single execution pipeline, and the calculator and current-time
  builtins.
- `agent_core.context` assembles a stable three-item prefix and volatile user
  region with checked hashes.
- `agent_core.runtime` computes provider-neutral model/tool steps; its executor
  is the only terminal run-state writer. The durable worker claims with
  `FOR UPDATE SKIP LOCKED`, supervises an epoch-fenced lease, and resumes the
  latest checkpoint while the maintenance role reclaims expired leases.
  Injected checkpoint seeding uses the run's pinned event prefix both at
  creation and after total checkpoint loss. Model and tool external-I/O
  boundaries assert that the composed unit of work is closed.
- `agent_core.application` exposes session and run services used by the CLI.
- `agent_core.evals` loads authored fake-model scripts and runs the eleven
  Milestone 1 cases through the ordinary composition only when requested.
- `agent_core.cli` exposes the installed `agent` entry point, including
  `agent run`, `agent session create`, `agent worker`, and `agent eval run`.
- `agent_core.adapters.persistence` owns the pinned linear migration, async
  engine/session factory, short unit of work, repositories, event upcasters,
  session-history and trajectory projections, full/delta checkpoints, queue,
  leases, model-call usage rows, and startup revision refusal.

The structural gate walks imports and signatures rather than source text. It
enforces the domain, port, runtime, application, entry-point, provider-SDK,
ORM, evaluation-package, composition-root, determinism, and database-resource
boundaries before those packages fill in.

The in-memory tier still claims no durability, recovery, or cross-repository
transaction. PostgreSQL supplies those guarantees for the normal CLI and worker
roles. No real provider, policy engine, long-term memory, skill, MCP, or sandbox
execution behavior is claimed as implemented by this page.
