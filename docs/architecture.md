---
title: Architecture
---

# Architecture

Image and video generation use the provider-neutral `MediaGenerationProvider`
port. `image.generate` and `video.generate` initially use TensorScale and write
external-untrusted PNG/MP4 streams through the existing run-bound artifact
writer. The ordinary approval, scope, non-idempotent recovery and reply-file
retention boundaries apply ([ADR-0140](adr/0140-tensorscale-media-generation.md)).
`MediaInputResolver` releases checksum-verified, bounded PNG/JPEG/WebP images
from the caller's own conversation after approval, under `artifact.read`.
The provider adapter alone encodes input bytes; tool arguments carry IDs only.

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
  schedule worker role (Milestone 11). An explicitly bound website schedule
  pins its owned profile in each occurrence session; the run worker checks
  access before model work, retaining the schedule budget and ordinary action
  approvals ([ADR-0150](adr/0150-scheduled-browser-profile-binding.md)).

## Implemented in Milestone 12 and the later workstreams

- `agent_core.application` owns the principal-scoped device registry
  (`device_management`), the content-free notification outbox written in the
  same transaction as its triggering event (`notification_producer`), and the
  provider-partitioned claim-and-retry dispatcher (`notification_dispatcher`,
  `notification_worker`); `agent_core.adapters.apns` is the APNs HTTP/2
  transport, and the device and notification routes joined `agent_core.api`
  behind exact scopes (Milestone 12).
- `agent_core.application.delegations` owns the delegation ledger and the
  one-transaction child-run materializer behind the suspending
  `agent_core.tools.delegate_run` control tool; the runtime added child-run
  suspension, join, and the downward-only cancel cascade (Milestone 13, in
  progress).
- `agent_core.memory` grew Milestone 16's deterministic benchmark arm,
  established-fact admission, decay sweep, and usage feedback, and the
  read-only `/v1/memories` list and detail routes entered `agent_core.api`
  behind the exact `memory.read` scope and a default-off flag (Milestone 17,
  complete); ADR-0117 added the review and deletion routes under
  `memory.write`.
- `agent_core.tools.schedule_create` is the model-callable, approval-gated
  one-time creation bridge over the existing
  `agent_core.application.schedule_service` (Milestone 19, in progress);
  `agent_core.domain.recurrence` adds pure calendar recurrence (Milestone 20),
  and `agent_core.tools.schedule_lifecycle` the governed list, update, pause,
  resume, and cancel tools (Milestone 23).
- `agent_core.adapters.telegram` and `agent_core.adapters.whatsapp` are
  messaging transports behind the transactional pairing and inbound routing of
  `agent_core.application.surfaces`, run by the least-privilege surface worker
  role (Milestones 14 and 25). `agent_core.adapters.device_channel` and
  `agent_core.application.device_ingest` carry SMS through the owner's iPhone
  as one framed, untrusted triage turn per message (Milestone 24).
- `src/gmail_mcp` and `src/bland_mcp` are first-party MCP server packages
  beside `agent_core`, which they never import; the platform reaches them only
  through its MCP process boundary (Milestones 18 and 27). Typed Email work runs
  on the ordinary run, tool, and model path in `agent_core.runtime.email_*`
  (Milestone 26), and `agent_core.adapters.unsubscribe` is the fixed one-click
  unsubscribe transport (Milestone 31). Calling adds the `call-worker` and
  `call-ingress` roles, whose dedicated ingress app never accepts owner runs
  (Milestone 27).
- `agent_core.memory` grew formation@9 adaptive distillation (Milestone 21)
  and People formation, and `agent_core.application.people*` owns the People
  directory, identity, duplicates, imports, and erasure behind the
  `/v1/people` routes and the `agent people` CLI (Milestone 28).
  `agent_core.domain.persona` holds the persona document and nomination types
  behind `agent persona` (Milestone 22).
- `agent_core.folders` groups chat threads into folders and proposals
  (Milestone 29). `agent_core.ports.judgment` is the typed-judgment port, with
  the TypeSafe and scripted adapters in `agent_core.adapters.judgment`
  (ADR-0110); its consumers are judgment-backed folder matching and the
  restrictive-only policy advisory layer in `agent_core.policy.advised` and
  `agent_core.policy.judgment_advisor` (Milestone 30).
- Owner-authorized extensions outside the milestone sequence keep the same
  boundaries: owner model settings (ADR-0119) and chat attachment upload
  (ADR-0120) are exact-scope `agent_core.api` routes; `tool.call` reaches
  deferred tools (ADR-0123); the browser control plane accepts device sign-in
  handoff (ADR-0128) and the API browser task grants (ADR-0129); and
  `agent_core.observability.latency` derives the content-free `agent run
  latency` report from the event log (ADR-0131).

Milestone 15, operational hardening, is authorized and adds nothing to this
page until its implementation lands. Each workstream's status and open items
are in `docs/status/project-state.yaml`.
