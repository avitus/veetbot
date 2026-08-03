---
title: Architecture
---

# Architecture

The platform is an explicitly bounded modular monolith. The normative module
layout and dependency rules are defined by the
[engineering plan](plan/engineering-plan.md#4-repository-structure), expanded
by the [bootstrap specification](plan/bootstrap-and-composition.md), and
recorded by [ADR-0001](adr/0001-modular-monolith.md).

## Implemented through Milestone 3

Milestone 0 supplies the repository foundation, Milestone 1 adds the first
complete provider-neutral vertical slice, and Milestone 2 makes that slice
durable and separately executable. Milestone 3 adds real provider adapters,
pinned routing, exact accounting, and governed trajectory export:

- `agent_core.config` owns deployment settings and validates the three
  configuration layers before resource construction.
- `agent_core.observability` owns structured-log setup, correlation context,
  bounded content logging, and recursive secret redaction.
- `agent_core.domain` contains provider-neutral agent, session, run, message,
  model-event, capability, usage, pricing, provider-pin, trajectory, tool,
  policy-classification, and event-envelope types.
- `agent_core.ports` defines time, identity, unit-of-work, repository, model,
  trajectory-artifact, tool, context, queue, dispatch, cancellation, and budget
  contracts.
- `agent_core.adapters` supplies fixed/system determinism, the scripted fake
  model, OpenAI Responses, Anthropic Messages, OpenAI-compatible chat
  completions, recorded-stream and missing-credential model adapters, identity
  resolution, local content-addressed trajectory bytes, inline and PostgreSQL
  dispatch, the process-local tier, and the SQLAlchemy/PostgreSQL persistence
  tier. ORM rows and hand-written translations remain confined to
  `adapters/persistence/`.
- `agent_core.model` owns stream normalization, the strict provider-profile
  registry and router, immutable provider pins, and Decimal cost calculation.
  Provider SDK types never cross an adapter boundary.
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
  boundaries assert that the composed unit of work is closed. A first model
  call resolves and persists its provider pin; resume reconstructs the same
  provider, model, capability, price, and opaque continuation state.
- `agent_core.application` exposes session, run, and governed trajectory-export
  services used by the CLI. Export is operator-disabled by default, requires a
  prospective principal grant, structurally excludes private execution data,
  applies and verifies all secret-scanner families, and expires through the
  maintenance role.
- `agent_core.evals` loads authored fake-model scripts and runs twelve cases
  through the ordinary composition only when requested. Its trajectory
  converter consumes only already-redacted artifact bytes and cannot read the
  event log.
- `agent_core.cli` exposes the installed `agent` entry point, including
  `agent run`, model-policy selection, governed export and consent commands,
  `agent session create`, `agent worker`, and `agent eval run`.
- `agent_core.adapters.persistence` owns the pinned linear migration, async
  engine/session factory, short unit of work, repositories, event upcasters,
  session-history and trajectory projections, full/delta checkpoints, queue,
  leases, normalized model-call accounting and bounded provider-metadata
  columns, export consent and trajectory records, artifact metadata and expiry,
  and startup revision refusal.

The structural gate walks imports and signatures rather than source text. It
enforces the domain, port, runtime, application, entry-point, provider-SDK,
ORM, evaluation-package, composition-root, determinism, and database-resource
boundaries before those packages fill in.

The in-memory tier still claims no durability, recovery, or cross-repository
transaction. PostgreSQL supplies those guarantees for the normal CLI and worker
roles. Milestone 3's local trajectory byte store is a narrow bridge to the
general artifact store owned by Milestone 6; it does not claim sandbox, upload,
streaming-object-store, or general artifact behavior. No policy engine,
long-term memory, skill, MCP, HTTP API, or sandbox execution behavior is claimed
as implemented by this page.
