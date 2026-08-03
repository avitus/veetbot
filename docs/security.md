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
