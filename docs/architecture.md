---
title: Architecture
---

# Architecture

The platform is an explicitly bounded modular monolith. The normative module
layout and dependency rules are defined by the
[engineering plan](plan/engineering-plan.md#4-repository-structure), expanded
by the [bootstrap specification](plan/bootstrap-and-composition.md), and
recorded by [ADR-0001](adr/0001-modular-monolith.md).

## Implemented through Milestone 3 (foundation)

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
roles.

## Implemented in Milestones 4 through 11

The same boundaries hold as the package map filled in; each addition is owned
by the detailed-design document the routing table in `AGENTS.md` names.

- `agent_core.policy` owns deterministic policy loading and evaluation, the
  exact scope vocabulary, and the hardline rules; approvals pause a durable run
  and resume it through the single terminal writer (Milestone 4).
- `agent_core.api` is the FastAPI boundary: sessions, messages, runs, approvals,
  artifacts, the SSE stream with `Last-Event-ID` replay, session history and
  deletion, schedules, and the browser profile, authentication, and grant
  routes — every route behind an exact scope (Milestones 5 and 10–11).
- `agent_core.adapters.execution` and `agent_core.execution` supply the
  container-backed sandbox, resource limits, egress policy, the in-sandbox RPC
  bridge, and credential scrubbing; `agent_core.adapters.artifacts` owns the
  content-addressed artifact store (Milestone 6).
- `agent_core.context` grew budgeting, history selection, compaction with
  provenance, structured working state, and trust labelling over a byte-stable
  prefix (Milestone 7).
- `agent_core.skills` and `agent_core.mcp` own the static skill substrate,
  the version-pinned catalog, `skill.load`, and MCP as a boundary adapter with
  namespaced servers and untrusted output (Milestone 8); `skill.manage`
  authoring and its confined background-review child run are Milestone 10A.
- `agent_core.memory` and `agent_core.knowledge` own formation, consolidation,
  hybrid retrieval with recall traces, governed inspection and deletion,
  document ingestion, and passage retrieval; provider-assisted extraction is
  evidence-gated (Milestones 9 and 10).
- `agent_core.adapters.web` and `agent_core.adapters.browser`, with
  `agent_core.browser_control_plane` as a separately deployed secret-bearing
  process, supply provider-neutral public-web search and fetch and
  authenticated browser automation, all external-untrusted (Milestone 10).
- `agent_core.scheduling` owns recurrence, civil time, atomic occurrence
  materialization into ordinary runs, admission, and the least-privilege
  schedule worker role (Milestone 11).

Milestones 12 through 15 — notifications and device identity, subagents and
delegation, inbound surfaces and pairing, operational hardening — are
authorized and add nothing to this page until their implementations land.
