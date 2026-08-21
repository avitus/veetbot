---
title: Security
---

# Security

Security decisions follow the normative engineering plan. The platform treats
authorization as deterministic application behavior: model output can propose
an action but cannot authorize it.

## Trust boundaries

| Trust level | Sources |
| --- | --- |
| Trusted | Application code, versioned agent configuration, versioned skills, policy rules |
| Partially trusted | Authenticated user input, approved internal tools |
| Untrusted | Websites, documents, email, MCP servers, tool output, generated code, uploaded files |

## Required controls

The implementation must preserve tenant isolation, principal authorization,
least-privilege tool scopes, schema and output validation, approvals for
consequential actions, secret redaction, sandboxing, network deny-by-default,
timeouts and output limits, budgets, cancellation, audit events, artifact
authorization, governed memory writes, idempotency, fair per-tenant limits,
frozen hardline rules, credential scrubbing, and default-deny inbound surfaces.
Prompt-injection corpora must exercise these controls before the milestones
that admit untrusted content are complete.

The system prompt never contains secrets. Tools obtain credentials from a
server-side resolver after authorization; the model receives only references or
capabilities.

## Controls implemented through Milestone 3

Milestone 0 establishes two executable controls before provider or tool code
exists:

- A committed-file scanner detects provider keys, private keys, bearer
  credentials, inline DSN passwords, and assigned secret literals. Findings
  name only the path, line, and rule; allowlist entries require a documented
  reason.
- The deterministic pytest suites block outbound sockets by default. Live tests
  lift the block explicitly, while integration tests may reach only loopback and
  Unix-domain sockets.

Milestone 1 adds executable controls at the first model/tool boundary:

- Tool registration rejects invalid names, reserved builtin domains, unsupported
  schema dialects, remote schema references, and output limits above the global
  ceiling. External tool sources are forced to untrusted output.
- Arguments are schema-validated and canonically normalized before the tool
  implementation is called. Invalid and unknown calls never enter a builtin.
- The pre-policy vertical slice allows only side-effect-free tools; every other
  classification is denied with fixed platform-authored narration.
- Tool failure messages come only from the checked-in reason-code table. External
  error text remains separately labelled untrusted and cannot enter `message`.
- Production startup refuses `docker` and `fake` when either deployment mode or
  authentication mode is unsafe, and production rejects evaluation tenants,
  principals, and policy profiles.
- Runtime limits, cancellation observation points, retry bounds, and the
  identical-call breaker stop unbounded execution.

Milestone 2 makes those controls durable: tenant and principal predicates live
in every repository query, state changes and their events commit atomically,
lease epochs fence stale workers, checkpoints retain only provider-neutral
conversation state, and raw external error details do not enter terminal run
messages.

Milestone 3 hardens the real provider and export boundaries:

- Strict provider profiles refuse unknown keys, undeclared capabilities,
  unsafe non-loopback HTTP endpoints, alias collisions, and incomplete pricing.
  Provider and price snapshots are pinned durably before execution proceeds.
- Provider SDK objects remain inside adapters. Provider errors are mapped to a
  closed internal vocabulary, and normalized metadata is bounded to declared
  scalar keys with exactly two consumers: persistence and span attributes.
- Credentials remain server-side secret values. Structural tests scan the
  request, normalized events, logs, spans, and persistence rows for synthetic
  provider credentials without embedding a reusable secret in the test source.
- Private reasoning is never emitted to the event log. Provider-only signed or
  opaque reasoning state can live in a checkpoint for same-provider resume, but
  it is excluded from events, model-call rows, logs, spans, and exports.
- Trajectory export is disabled by default and requires both operator
  enablement and prospective per-principal consent. Withdrawal expires existing
  exports, and the maintenance sweeper deletes the corresponding bytes.
- Export redaction structurally excludes sensitive execution fields, applies
  every committed-secret and sensitive-key family plus tenant patterns, and
  fails closed before writing if verification finds a remaining match.
- Static and contract suites still deny network. The OpenAI-compatible local
  path is exercised on loopback at zero cost; remote credentialed smoke tests
  remain explicitly opt-in.

Later controls remain requirements of their owning milestones and are not
claimed as implemented here.

## Milestone 11 scheduling controls

Scheduled task management is default-off at both its HTTP and worker entry
points. Production release validation requires both flags to be Boolean and to
change together. The schedule role is a separate least-privilege process with
PostgreSQL access but no API bearer token, provider key, tool credential,
sandbox access, or object-store credential.

Schedules retain a principal identity and requested scope subset, never a
credential. Materialization resolves configured authority again, verifies the
pinned agent and policy, applies finite per-run and tenant admission limits,
and fails closed before creating a session or run. The occurrence, session,
run, checkpoint, and seed events share one transaction; PostgreSQL uniqueness,
row locks, row-level security, and principal predicates prevent duplicate or
cross-principal materialization. Credential-shaped instructions are rejected
without logging the matched value.

Interactive and asynchronous workers claim disjoint reserved priority classes.
Misfires coalesce in bounded time, one schedule cannot overlap its active run,
terminal accounting is idempotent, and repeated failures pause future firing.
PostgreSQL notification only reduces latency: bounded durable scans remain the
correctness path. Occurrence links provide offline result recovery, while
session erasure clears content links and retains an explicit non-content audit
marker.

## Production delivery controls

The production delivery path is privileged supply-chain code. CircleCI packages
the exact tested commit with `git archive`, records a SHA-256 checksum, and
connects only with a Veetbot-specific project deploy key and a context-provided
pinned host-key record. The production API binds to loopback so the Nginx TLS
site is its only public HTTP entry point. The server serializes releases with
`flock`, keeps dependencies and the sandbox image revision-specific until
promotion, and requires the health probe to report the same release identity
locally and through the public TLS endpoint.

Application services run without Docker-group membership or a container-runtime
socket. A separate `veetbot-exec` systemd service owns gVisor sandbox lifecycle,
loads no application environment file, and accepts the existing
`ExecutionEnvironment` operations over a group-restricted Unix socket. The
separate `veetbot-deploy` identity owns immutable application and documentation
releases. ADR-0062 records this production correction to the older host topology.

The committed-file secret scanner covers `.circleci/`, `deploy/`, `nginx/`, and
`scripts/` in addition to application, client, test, migration, evaluation, and
documentation sources. Production bearer tokens, provider keys, and database
credentials remain in the protected server environment and are never passed
through the CircleCI deployment context or the downloadable client artifact.
The terminal client strips C0/C1 and ANSI/OSC control sequences from remote text
before it writes output or displays an API-provided prompt.

Nginx changes are backed up and must pass `nginx -t` before reload. Application
promotion does not automatically roll back after a migration: reverting code
across a potentially incompatible schema is an operator decision, not a safe
automated response to a failed health check.
