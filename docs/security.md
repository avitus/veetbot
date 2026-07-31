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

## Milestone 0 controls

Milestone 0 establishes two executable controls before provider or tool code
exists:

- A committed-file scanner detects provider keys, private keys, bearer
  credentials, inline DSN passwords, and assigned secret literals. Findings
  name only the path, line, and rule; allowlist entries require a documented
  reason.
- The deterministic pytest suites block outbound sockets by default. Live tests
  lift the block explicitly, while integration tests may reach only loopback and
  Unix-domain sockets.

Later controls remain requirements of their owning milestones and are not
claimed as implemented here.
