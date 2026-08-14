---
title: HTTP API and Streaming
status: design
canonical: true
---

# The HTTP API, the event stream, and what a client can rely on

## The fourteen-route Milestone 5 baseline

Section 16 designs nine endpoints. Three more are named elsewhere —
`GET /v1/approvals` and `GET /v1/approvals/{id}` in
[policy-and-approvals.md](policy-and-approvals.md), and
`POST /v1/runs/{id}/input` in ADR-0009 — and one, the two health probes,
is really two. Thirteen routes, and the corpus writes a response body for
exactly one of them: the session created by `POST /v1/sessions`. This
document writes the other twelve and adds one route of its own, which
leaves a surface of fourteen.

That is the gap this document exists to close, and it is worth being
precise about its shape, because "the API is undocumented" is not true and
is not the problem. Section 16 fixes the method, the path, the request
body where there is one, the SSE frame format, the `Last-Event-ID` replay
rule, the error envelope, and the authentication posture. It is a real
design. What it does not do is say what comes back. An implementer holding
Section 16 knows that `GET /v1/runs/{run_id}` exists and cannot write it,
because nothing in the corpus states which of the run record's
twenty-six columns are public, what a status of `WAITING_FOR_APPROVAL`
tells a client to do next, or whether the failure that ended a run is
visible at all.

Six more questions sit inside Section 16 rather than outside it, and
[readiness.md](readiness.md) names them: the error envelope has one worked
example and no code list; request identifiers are an implement bullet;
`Idempotency-Key` is named twice and specified nowhere; the SSE consumer
side is one sentence; authentication is designed only at its refusal; and
nothing turns an HTTP cancel into an observation by a worker in a
different process.

This document answers all seven — the six and the response bodies — and
adds exactly one route, `GET /v1/sessions/{session_id}`, argued for
where it is specified. Every other route below is a route the corpus
already names, and what this document adds to those is shapes, codes,
orders, and rules.

ADR-0050 later authorizes two post-Milestone 9 additions:
`GET /v1/sessions` and `DELETE /v1/sessions/{session_id}`. They make the
current public surface sixteen routes without rewriting the completed
Milestone 5 baseline or its gate census. The sections labelled as that
baseline remain historical requirements; the authoritative-history section
below specifies the current extension.

## What this document does not change

Section 16 remains the statement of what the API is for. This document is
subordinate to it in the same way [runtime-loop.md](runtime-loop.md) is
subordinate to Section 12: where they overlap, Section 16's sentence is
the requirement and this document's is the mechanism.

Specifically unchanged: the method and path of every endpoint; the
request bodies Section 16 writes; `202 Accepted` for message submission;
the SSE frame format; the rule that a repeated idempotency key returns the
original run; the rule that readiness must not call a model provider on
every probe; the rule that authorization is checked before artifact
metadata or content; and the rule that tracebacks are never exposed.

Also unchanged, and from other documents: cancellation is cooperative and
its mechanism belongs to [runtime-loop.md](runtime-loop.md); the event log
is the durable path and `NOTIFY` is a latency optimization, per ADR-0010
and [event-log-and-persistence.md](event-log-and-persistence.md); a
tenant identifier is never read from the client, per
[policy-and-approvals.md](policy-and-approvals.md); and at most one
non-terminal run exists per session, per ADR-0009.

## Every response has the same two shapes

There are two envelopes and no third. A successful response is the
resource, unwrapped. A failure is Section 16's error object, unchanged:

```json
{
  "error": {
    "code": "tool_validation_error",
    "message": "Tool arguments did not match the schema.",
    "details": {},
    "request_id": "uuid"
  }
}
```

Not wrapping success is a deliberate asymmetry. A client that reads
`response.json()["id"]` is doing the common thing, and an envelope that
exists only to be unwrapped taxes every call site to buy a symmetry
nobody consumes. The failure case earns its wrapper because it carries
four fields that are not the resource.

Three rules govern the envelope.

1. **`code` is a closed vocabulary**, enumerated below. A client may
   switch on it. A code is never removed and never changes meaning; a new
   code is a minor version change and clients must treat an unrecognized
   code as equivalent to the HTTP status class.
2. **`message` is for a human reading a log**, is in English, and is not
   a localization surface. It never contains a provider's raw text, a
   traceback, a SQL statement, or a value the client did not send.
3. **`details` is typed per code** or is `{}`. Where it carries anything,
   the shape is given with the code. It is never a free-form dump.

### The code list is the error taxonomy under one rule

Section 13 declares twenty-three error classes and
[runtime-loop.md](runtime-loop.md) classifies eight, six of them new, for
twenty-nine in the union. Section 16 gives one worked example,
`tool_validation_error`, which is `ToolValidationError` in snake case.
That single example fixes the convention, so the wire vocabulary is not
invented here — it is the taxonomy the corpus already has, snake-cased,
minus the classes that never cross the boundary.

```text
class                     code                       HTTP
------------------------  -------------------------  ----
AuthenticationError       authentication_error       401
AuthorizationError        authorization_error        403
NotFoundError             not_found                  404
ConflictError             conflict                   409
InvalidStateTransition    invalid_state_transition   409
ToolNotFoundError         tool_not_found             404
ToolValidationError       tool_validation_error      422
ToolPolicyDenied          tool_policy_denied         403
ApprovalRequired          approval_required          409
ApprovalDenied            approval_denied            409
ApprovalExpired           approval_expired           409
BudgetExceeded            budget_exceeded            402
DeadlineExceeded          deadline_exceeded          504
RunDeadlineExceeded       run_deadline_exceeded      504
RunCancelled              run_cancelled              409
ContextOverflow           context_overflow           422
ToolLoopDetected          tool_loop_detected         409
ModelTransientError       model_transient_error      503
ModelPermanentError       model_permanent_error      502
ModelProtocolError        model_protocol_error       502
ToolTimeoutError          tool_timeout               504
ToolExecutionError        tool_execution_error       502
ToolResultValidationError tool_result_invalid        502
SandboxProvisionError     sandbox_provision_error    503
SandboxExecutionError     sandbox_execution_error    502
ArtifactStorageError      artifact_storage_error     503
ConcurrencyConflict       concurrency_conflict       409
```

Two of the twenty-nine never reach a client and are deliberately absent
from the table: `WorkerFenced`, which is not a run failure at all, and
`EmptyModelTurn`, which is retried a step below. The other twenty-seven
are the table. Anything raised that is not in it is reported as
`internal_error` with HTTP `500`, an empty `details`, and the request
identifier — which is the only handle support has, and is why the
identifier is mandatory rather than best-effort.

Four codes are added here because the API raises conditions the taxonomy
does not name:

```text
code                       HTTP  raised when
-------------------------  ----  ----------------------------------
malformed_request           400  body is not valid JSON, or a
                                 required field is absent
unsupported_media_type      415  Content-Type is not
                                 application/json where required
payload_too_large           413  request body exceeds the cap
rate_limited                429  reserved; see "Limits" below
```

`rate_limited` is declared and never returned by version 0.1. Fixing its
shape now is free; discovering after release that clients cannot
distinguish it from `conflict` is not.

### Two codes carry `details`, and the rest carry nothing

```json
{
  "error": {
    "code": "conflict",
    "message": "The session already has an active run.",
    "details": {
      "reason": "active_run_exists",
      "run_id": "0192f3c1-...",
      "run_status": "RUNNING"
    },
    "request_id": "0192f3c2-..."
  }
}
```

`conflict` carries a `reason` discriminator because it covers three
unrelated situations — an active run already exists, an idempotency key
was reused with a different body, and an approval was resolved twice with
different decisions — and a client that must tell them apart should not
be parsing English.

```text
code       reason                     details also carries
---------  -------------------------  ------------------------
conflict   active_run_exists          run_id, run_status
conflict   idempotency_key_reused     (nothing)
conflict   approval_already_resolved  approval_id, decision
```

`tool_validation_error` carries `{"tool_name": ..., "errors": [...]}`
where `errors` is the validator's path-and-message list. No other code
carries a populated `details` in version 0.1, and adding one is a minor
version change.

## Request identifiers, and their relationship to traces

Every request gets an identifier. It is a UUIDv7, generated by the
request middleware [development-toolchain.md](development-toolchain.md)
already places there, and it is bound as a context variable for the
lifetime of the request so every log line inside it carries the same
value without being passed one.

Four rules, and the third is the one that matters.

1. **The response always carries it**, in the `X-Request-Id` header on
   success and in `error.request_id` on failure. A client that logs the
   header can hand support a single string that finds every server-side
   line for that call.
2. **A client may supply `X-Request-Id`** and the server echoes it.
3. **A supplied identifier is never trusted with anything.** It is not a
   deduplication key, not a lookup key, not an authorization input, and
   not written to the event log. It is a correlation label. Idempotency
   has its own header and its own mechanism, and conflating them is how a
   client's retry logic silently becomes the server's dedup logic. A
   supplied value longer than 128 characters or containing anything
   outside `[A-Za-z0-9._-]` is replaced rather than rejected, because
   failing a request over a log label is a worse outcome than ignoring
   the label.
4. **`request_id` and `trace_id` are different things and both exist.**
   `trace_id` is the OpenTelemetry trace, read from the active span, and
   it spans processes — the API's handler and the worker's execution of
   the run it created share one. `request_id` identifies one HTTP call
   and never leaves the API process. Section 19's log-field list carries
   both for that reason. `events.trace_id` is the trace, not the request:
   the event log is written by the worker, which has a trace and does not
   have an HTTP request.

The submit handler propagates its `trace_id` onto the run it creates, so
that the worker's spans join the client's trace rather than starting a
new one. This is the only place an API-side identifier reaches durable
storage.

## Authentication, and the principal it produces

Section 16 designs authentication at its refusal: two modes, and a
startup that fails in production without one. It does not say what
authentication produces. It produces a `Principal`, and the
`Principal` is the only thing the rest of the request is allowed to
consult.

`Principal` is already declared in Section 5 with four fields —
`tenant_id`, `principal_id`, `roles`, and `scopes`. Authentication's
whole job is to turn a credential into one of those, or to fail. Nothing
downstream re-reads the credential, and no handler takes a `tenant_id`
from a path, a query string, a body, or a header.

The two modes fill it differently.

1. **`AUTH_MODE=dev`** binds a fixed principal without consulting a
   credential: a development tenant, a development principal, and the
   full scope set. It is bound to loopback — a request arriving on a
   non-loopback interface is rejected with `unauthorized` even in dev
   mode, so that a developer who exposes the port does not
   accidentally expose an unauthenticated agent. This is the same
   posture ADR-0008 takes when it refuses the development sandbox
   fallback outside dev mode.
2. **`AUTH_MODE=token`** reads `Authorization: Bearer <token>` and
   compares it against the configured `auth_token` in constant time. A
   match binds the single configured principal. There is one token and
   one principal in 0.1; multi-tenancy is designed for and not yet
   populated, which is exactly the state ADR-0011 describes for the
   shared core.

A missing, malformed, or non-matching credential is `unauthorized` with
status 401 and the `WWW-Authenticate: Bearer` header. The response body
is the standard error envelope, and the message never distinguishes
"no token" from "wrong token".

Three consequences worth stating, because each one is a decision an
implementer would otherwise have to make alone.

**The health endpoints are unauthenticated.** A liveness probe that
needs a credential is a liveness probe that fails when the credential
rotates. `GET /health/live` and `GET /health/ready` are the only
unauthenticated routes, and the shape of what they return is chosen so
that being unauthenticated is safe — see the health section below.

**Token comparison is constant time and the token is never logged.**
`Settings.auth_token` is a `SecretStr` for that reason, and the
redaction rules in [development-toolchain.md](development-toolchain.md)
already cover it. An `Authorization` header is stripped from any
structured log line and from any error the request produces.

**There is no token rotation without a restart in 0.1.** One configured
token means the only rotation procedure is to restart with a new one,
which drops in-flight requests. Accepting a set of tokens rather than
one would fix that, and it changes a declared `Settings` field, so it is
recorded as an open question rather than decided here.

## Authorization: scopes, tenancy, and why cross-tenant is a 404

Authorization asks two questions in a fixed order, and both are answered
before a handler touches a repository.

**First, does the principal hold the scope this route requires?** If
not, the response is `forbidden` with status 403.

**Second, does the resource belong to the principal's tenant?** If not,
the response is `not_found` with status 404 —
[policy-and-approvals.md](policy-and-approvals.md) already fixes this
for approvals, and the rule generalizes to every resource. A 403 for a
resource in another tenant confirms the resource exists, which turns
every identifier into an oracle. A tenant boundary must be silent to be
a boundary.

The order matters: scope first, tenant second. Reversing them means a
principal with no scope at all can still probe for the existence of
identifiers by watching 403 turn into 404.

### The scope vocabulary is closed and matched exactly

Scopes are dotted `resource.action` strings, and the set is enumerated:

```text
session.read      session.write
run.read          run.write        run.cancel
approval.read     approval.resolve
artifact.read
skill.write
```

`approval.resolve` is the one the corpus already names; the rest follow
its form. Membership is exact string equality against the principal's
scope set. There is no wildcard, no prefix rule, and no hierarchy in
which `run.write` implies `run.read`.

That last point is deliberate and slightly inconvenient, so it is worth
the sentence. A hierarchy needs a grammar, the grammar needs an
evaluation order, and the evaluation order is a thing that can be subtly
wrong in a way that grants access nobody intended. Exact match is the
version that cannot be subtly wrong. If a caller needs to submit and
read, it holds two scopes. This is the same argument
[policy-and-approvals.md](policy-and-approvals.md) makes for a rule
engine with no arithmetic in it.

Roles are bundles. Authentication resolves a role to a scope set and
puts the scopes on the `Principal`; the API never checks a role. Keeping
`roles` on the `Principal` is useful for audit and for logging, and it
is not an authorization input.

### The scope each route requires

| Route | Scope |
| --- | --- |
| `POST /v1/sessions` | `session.write` |
| `GET /v1/sessions` | `session.read` |
| `GET /v1/sessions/{id}` | `session.read` |
| `DELETE /v1/sessions/{id}` | `session.write` |
| `POST /v1/sessions/{id}/messages` | `run.write` |
| `GET /v1/runs/{id}` | `run.read` |
| `GET /v1/runs/{id}/events` | `run.read` |
| `POST /v1/runs/{id}/cancel` | `run.cancel` |
| `POST /v1/runs/{id}/input` | `run.write` |
| `GET /v1/approvals` | `approval.read` |
| `GET /v1/approvals/{id}` | `approval.read` |
| `POST /v1/approvals/{id}/resolve` | `approval.resolve` |
| `GET /v1/artifacts/{id}` | `artifact.read` |
| `GET /v1/artifacts/{id}/content` | `artifact.read` |
| `GET /health/live` | none |
| `GET /health/ready` | none |

Submitting a message requires `run.write` rather than `session.write`
because submitting is what creates a run; `session.write` gates creating
the session itself. `run.cancel` is separate from `run.write` because a
surface that may stop work is not necessarily a surface that may start
it — an operator console is the obvious case.

`session.read` gates reading one session and listing the principal's sessions.
`session.write` gates both creation and authoritative deletion. Hard gate 5 is
what keeps the rows honest — it walks the route table and fails the build on a
route that declares no scope — so a route added here without a row does not ship
open.

`skill.write` is in the vocabulary and in no row of the table, because it
is checked by the policy engine on a tool call rather than by the API on a
route. It is enumerated here because the vocabulary is closed and a scope
the policy engine checks against a string this document does not contain
is a scope that gets misspelled. [skills.md](skills.md) owns what it
governs. There is no `skill.read`: nothing reads skills over the API in
0.1, and an uncheckable scope is worse than a missing one.

`skill.write` is not the only such scope, and the rest arrive a milestone
earlier than this document does.
[policy-and-approvals.md](policy-and-approvals.md) enumerates the whole
closed vocabulary — these nine plus the six that
`ToolSpec.required_scopes` carries — states the grammar that lets an MCP
server's operator-configured scopes exist outside a closed list, and
specifies the subset test the pipeline runs. Nothing there changes what a
route requires; the table above is the API's half of one namespace.

### Tenancy is a repository argument, never a filter applied afterwards

Every repository method that reads a tenant-scoped resource takes the
`tenant_id` as an argument and includes it in the `WHERE` clause. It is
not applied as a post-filter in the service layer, because a post-filter
is a correct-looking piece of code that leaks on the day somebody adds a
`LIMIT` above it. The `sessions` and `artifacts` tables carry
`tenant_id` directly; `runs`, `events`, `approvals`, and
`tool_invocations` reach it through `session_id`, and the repository
joins rather than trusting the caller.

## Sessions

```http
POST /v1/sessions
```

Section 16 fixes the request and the response. This document adds four
things: the status vocabulary, the `agent_version` resolution rule, the
metadata bound, and the read route the corpus never named.

### `SessionStatus`

```python
class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
```

`SessionStatus` is referenced in Section 5 and declared nowhere. It is
declared here, uppercase, matching `RunStatus` and matching the values
the DDL's guarded updates would compare against. Section 16's example
response shows `"active"` in lowercase; that sample is read as
illustrative rather than as a contract, and Section 16 is not edited.
The mismatch is recorded as an open question, because the alternative —
lowercase session status beside uppercase run status on the same wire —
is the kind of inconsistency a client library encodes as two enums and
a comment.

Two values, not more. A session is open or it is finished. There is no
`ARCHIVED` because nothing in the corpus archives one, and no `DELETED`
because deletion hard-removes the live session row. Its content-free tombstone
is an idempotency record, not a third session state.

### `agent_version` is resolved at creation and frozen

The request names an `agent_id` and never a version. The server resolves
the current version of that agent at creation, writes it to
`sessions.agent_version`, and every run in the session uses it. An agent
upgraded mid-session would otherwise change behaviour underneath a
conversation that already has history, which is the failure
[context-engine.md](context-engine.md) is built to avoid.

A new version therefore requires a new session. This is the same
decision ADR-0013 makes for skill version pinning, applied to the agent
as a whole.

An unknown `agent_id` is `not_found` with status 404, not a validation
error, because the identifier names a resource that does not exist.

### Metadata is opaque, bounded, and never read by the agent

`metadata` is a client-owned JSON object. The server stores it, returns
it, and does not interpret it. Two bounds: it must be a JSON object at
the top level, and its serialized form must not exceed 8 KiB. Exceeding
either is `validation_error`.

The important rule is the third one and it is a security rule.
**Session metadata is never placed in a model prompt.** It is client
input, which makes it untrusted under
[tool-system.md](tool-system.md)'s rule that no external text reaches
the model unlabelled, and a metadata field is exactly the shape an
injection takes when somebody decides it would be convenient to show
the model the client's `"user_note"`. If a value must reach the model it
travels as message content, where the trust label
[context-engine.md](context-engine.md) requires can be attached to it.

### Reading a session

```http
GET /v1/sessions/{session_id}
```

This route is not in Section 16 and is added here, because a client
that reconnects with only a session identifier has no way to learn the
session's status or find its runs. It is the one route this document
adds, and it adds no capability: everything it returns is already
readable by a client that kept its own records.

```json
{
  "id": "uuid",
  "status": "ACTIVE",
  "agent_id": "general",
  "agent_version": 1,
  "title": null,
  "metadata": {},
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z",
  "active_run_id": null,
  "last_run_id": null
}
```

`active_run_id` is the run in this session whose status is not terminal,
or null. It is derived from the partial index
[event-log-and-persistence.md](event-log-and-persistence.md) already
declares — `WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED')` —
and it is what a reconnecting client needs to decide whether to open a
stream or submit a message.

`last_run_id` is the most recently created run in the session, whether active
or terminal, or null if the session has no runs. It lets a history client reopen
the latest transcript without maintaining an authoritative device-local run
index.

`next_event_sequence` is not returned. It is an internal allocation
counter, and a client that reads it will treat it as a stream position,
which it is not.

### Authoritative history index and deletion

ADR-0050 adds two principal-scoped routes after the completed Milestone 5
surface:

```http
GET /v1/sessions?limit=50&cursor=<opaque>
DELETE /v1/sessions/{session_id}
```

The list response uses the standard page shape:

```json
{
  "items": [
    {
      "id": "uuid",
      "status": "ACTIVE",
      "agent_id": "general",
      "agent_version": "1",
      "title": null,
      "metadata": {},
      "created_at": "2026-01-01T00:00:00Z",
      "updated_at": "2026-01-01T00:00:00Z",
      "active_run_id": null,
      "last_run_id": "uuid"
    }
  ],
  "next_cursor": null
}
```

It is ordered by `updated_at DESC, id DESC` and paginated by that keyset.
Appending a persisted event advances the session's `updated_at`, so the order
reflects server-observed conversation activity. The repository applies tenant
and principal ownership before ordering and limiting; the client cannot supply
either identifier.

Delete returns `204 No Content` only after the durable session graph is removed.
A non-terminal run makes deletion unsafe and returns `409 invalid_state` with
`details.reason = "active_run_exists"` and `details.run_id`; the caller must
cancel and observe termination first. A missing, cross-tenant, or differently
owned live session returns `404`. A repeat by the principal who already deleted
the session returns `204`, based on a content-free ownership tombstone.

The database transaction removes runs, events, checkpoints, projections,
approvals, invocations, artifact metadata, session recall traces, session-bound
memory and consolidation records, and knowledge documents sourced from the
session's artifacts. Published skill revisions survive as separately managed
resources, with an authoring-run reference detached if necessary. Artifact
references enter a durable deletion queue in the transaction; the request tries
to delete external bytes immediately and maintenance retries failures until the
queue is empty. The permanent tombstone retains only session, tenant, principal,
and deletion-time identifiers and contains no conversation content.

## Submitting a message, and the two mechanisms that share the name

```http
POST /v1/sessions/{session_id}/messages
Idempotency-Key: <client-generated-key>
```

Section 16 fixes the request body, the `202 Accepted`, the response
`{"run_id", "status"}`, and the rule that a repeated key returns the
original run. What follows is the order of operations, the conflict
behaviour, and the disambiguation of a word the corpus uses for two
unrelated things.

### The handler's order of operations

1. Authenticate, producing the `Principal`.
2. Check `run.write`.
3. Load the session in the principal's tenant, or 404.
4. Reject a `CLOSED` session with `invalid_state` and status 409.
5. Validate the content blocks.
6. Resolve the idempotency key, which either returns an existing run or
   reserves the key.
7. Decide routing: new run, input delivery, or conflict.
8. In one transaction, insert the run, append the user message to the
   session's event log, and commit.
9. `NOTIFY` the worker channel, outside the transaction's critical path
   but after commit, per ADR-0010.
10. Return `202` with the run identifier and status.

Step 8 is one transaction on purpose. A run that exists without its
triggering message is a run the worker claims and cannot execute, and
a message without a run is a message nothing will ever answer.
[event-log-and-persistence.md](event-log-and-persistence.md)'s sequence
allocation applies unchanged: the append increments
`sessions.next_event_sequence` atomically and `UNIQUE(session_id,
sequence)` is the backstop.

Step 9 is after commit because ADR-0010 makes `NOTIFY` transactional and
a hint. If the process dies between commit and notify, the run is still
claimed on the queue's next poll. Nothing is lost; a small amount of
latency is.

### Routing: new run, resumed run, or 409

The session has at most one non-terminal run, enforced by the partial
unique index ADR-0004 declares. What happens when a message arrives for
a session that already has one is decided by that run's status, and it
is decided by the API from stored state, never by the model.

| Active run status | Result |
| --- | --- |
| none | new run, `202` |
| `WAITING_FOR_USER` | routed to input delivery, `202` |
| `QUEUED`, `RUNNING`, `WAITING_FOR_APPROVAL` | `conflict`, `409` |

The middle row is the only case in the system where a `ConflictError`
becomes a `202`, and [runtime-loop.md](runtime-loop.md) says so
explicitly. The 409 body carries
`details.reason = "active_run_exists"` and `details.run_id`, so a client
can stream the run that is already in flight rather than guess.

Section 27.3 permits one configured policy — route the text, or reject
it with guidance — and requires that the server not silently do both.
The configured default is to route, because a user answering a question
the agent asked expects the answer to land, and the alternative asks
every client to implement the routing rule the server already has the
state to apply. The setting exists so a deployment that wants strictly
explicit input delivery can have it.

### Two idempotency mechanisms, two scopes, one word

This is the disambiguation, and it is load-bearing because the corpus
uses "idempotency key" for both.

**The HTTP mechanism** is the `Idempotency-Key` header on this endpoint.
It is scoped to a client request. Its record is the `idempotency_keys`
table [event-log-and-persistence.md](event-log-and-persistence.md)
declares at Milestone 2, keyed on the client-supplied string, carrying
`tenant_id`, `principal_id`, `request_hash`, `run_id`, `created_at`, and
`expires_at`. Its purpose is that a client retrying a submission after a
timeout does not start a second turn.

**The tool mechanism** is `tool_invocations.idempotency_key`, owned by
the `ToolInvocationRepository` port that
[milestone-map.md](milestone-map.md) schedules at Milestone 1. It is
scoped to a tool call inside a run, and its purpose is that a step
retried after a crash does not execute the same side effect twice. No
HTTP client ever sees it.

They share a name, a column name, and nothing else. An implementer who
reads them as one mechanism will put the HTTP table at Milestone 1,
where its DDL does not exist, or will try to satisfy the tool port with
a header nobody sends.

### How the HTTP key behaves

1. **The header is optional.** Without it, every submission creates a
   run. Section 16 shows the header on the request and does not require
   it, and requiring it would break the simplest possible client for a
   guarantee it did not ask for.
2. **The key is scoped to `(tenant_id, principal_id, key)`.** Two
   tenants using the string `1` do not collide. The table's primary key
   is the client string; the scoping columns are part of the lookup
   predicate.
3. **A repeat with a matching `request_hash` returns the original
   run**, with `200` rather than `202`, because nothing was accepted.
   The body is identical otherwise. This is Section 16's rule, and the
   status distinction is what tells a client its retry was a retry.
4. **A repeat with a different `request_hash` is `conflict` with status
   409** and `details.reason = "idempotency_key_reused"`. This is the
   `ConflictError` case [runtime-loop.md](runtime-loop.md) names. The
   hash exists precisely so that a client bug — reusing a key across
   different messages — surfaces as an error rather than as a silent
   return of an unrelated run.
5. **`request_hash` is over the canonicalized request body**, not the
   headers and not the path. Canonicalization is the same JSON
   canonicalization the tool system uses for
   `normalized_arguments_hash`, so there is one such function in the
   codebase.
6. **A key expires after 24 hours** and expired rows are deleted by the
   same maintenance path that prunes other bounded tables. After
   expiry the key is reusable, which is safe because a client retrying
   a day-old submission is not retrying, it is submitting.
7. **A concurrent repeat waits rather than racing.** The insert of the
   key row happens in the same transaction as the run insert, so the
   second request blocks on the primary key and then reads the
   committed row. This is the property the in-memory adapter cannot
   honestly provide, which is why the concurrent-dedup gate is
   Milestone 2 — the same reasoning ADR-0027 decision 10 applies to
   the tool port.
8. **A key whose run creation failed is not retained.** The row is
   written in the failed transaction and rolls back with it, so a
   retry after a server error creates the run rather than returning a
   conflict about a run that does not exist.

A key longer than 255 characters is `validation_error`. There is no
format requirement beyond that; a UUID is the obvious choice and the
server does not care.

## Runs: the response body the corpus never wrote

```http
GET /v1/runs/{run_id}
```

Section 15 gives `runs` fifteen columns and four other documents add
eleven more, so the table is twenty-six columns by the time the corpus
is built. This returns thirteen of them, reshaped, and withholds
thirteen, and the split is almost exactly the seam between the two.
Every Section 15 column is in the body except `lease_owner` and
`lease_expires_at`; every column added afterwards is withheld. Of the
thirteen withheld, one value still reaches the client: `deadline_at`
arrives inside `limits`, where it was a domain field before
[runtime-loop.md](runtime-loop.md) gave it a column.

`lease_owner`, `lease_expires_at`, `lease_epoch`, and `attempts` are
queue internals and stay internal — a client that can see the lease
will build something on top of it, and the lease is the worker's
business. `tenant_id` and `principal_scopes` belong to the
authorization record and are not a client's to read back.

```json
{
  "id": "uuid",
  "session_id": "uuid",
  "parent_run_id": null,
  "status": "RUNNING",
  "step_count": 3,
  "model_call_count": 3,
  "tool_call_count": 2,
  "usage": {
    "input_tokens": 4120,
    "output_tokens": 388,
    "cost_usd": "0.0214"
  },
  "limits": {
    "max_steps": 40,
    "deadline_at": "2026-01-01T00:15:00Z",
    "max_cost_usd": "1.00"
  },
  "failure": null,
  "cancel_requested_at": null,
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:12Z"
}
```

Six rules govern that body.

1. **`status` is the `RunStatus` value verbatim**, uppercase, one of the
   seven [runtime-loop.md](runtime-loop.md) declares. It is the client's
   entire state machine: `QUEUED` and `RUNNING` mean wait,
   `WAITING_FOR_APPROVAL` means a human must act,
   `WAITING_FOR_USER` means this client must answer, and the three
   terminal values mean stop polling.
2. **`failure` is null unless `status` is `FAILED`**, and when present
   it is the `RunFailure` shape with `reason`, `message`,
   `step_number`, `attempt_number`, and `occurred_at`. Its
   `error_class` and `details` are not exposed: the class is an
   internal type name and the details may carry tool output. `reason`
   is the `FailureReason` enum, which is a closed vocabulary a client
   can branch on, and that is what a client actually needs.
3. **Monetary values are strings**, always, in both `usage` and
   `limits`. A cost serialized as a JSON number is a cost rounded by
   whatever parsed it, and a budget comparison performed on a rounded
   number is a budget that can be exceeded by rounding. This is the
   same reason the corpus uses `Decimal` internally.
4. **`usage` accumulates and is readable mid-run.** A client polling a
   `RUNNING` run sees the cost so far, which is the point.
5. **`cancel_requested_at` is exposed** because it is the only way a
   client can distinguish a run that will stop shortly from one that is
   still working. A run may be `RUNNING` with a cancel request pending
   for up to one heartbeat interval plus the time to the next
   observation point.
6. **`parent_run_id` is exposed and child runs are not enumerated.**
   The field already exists on the table; a `GET /v1/runs?parent=` list
   is not designed and is not needed for 0.1, where the only client of
   child runs is the runtime itself.

`agent_id` and `agent_version` are not on the run, because they are on
the session and a run cannot change them.

## Cancellation across a process boundary

```http
POST /v1/runs/{run_id}/cancel
```

This is [readiness.md](readiness.md)'s sixth gap, and it is half-closed
already: [runtime-loop.md](runtime-loop.md) specifies the worker's half
completely — a poller in the same supervisor task as the heartbeat reads
`runs.cancel_requested_at` in the same query that refreshes the lease,
at the heartbeat interval, and the token it refreshes is observed at six
points. What was missing is the sentence that says the endpoint is what
writes that column. It writes that column.

The endpoint's behaviour depends on the run's status, and the split is
the one Section 6.4 already draws between a run a worker holds and a run
nobody holds.

| Status | Effect | Response |
| --- | --- | --- |
| `QUEUED` | transition directly to `CANCELLED` | `200` |
| `WAITING_FOR_APPROVAL` | transition directly to `CANCELLED` | `200` |
| `WAITING_FOR_USER` | transition directly to `CANCELLED` | `200` |
| `RUNNING` | set `cancel_requested_at`, `NOTIFY` | `202` |
| terminal | no effect | `200` |

The direct transitions are safe because no worker holds the lease: a
`QUEUED` run has not been claimed, and both `WAITING_*` states released
the lease on suspension, which
[runtime-loop.md](runtime-loop.md) makes one mechanism with three kinds.
The transition is a guarded `UPDATE` with the expected status in the
`WHERE` clause, so a run claimed between the read and the write updates
zero rows and the handler falls through to the `RUNNING` path.

The `RUNNING` path is a write and a hint. The write is
`cancel_requested_at`; the hint is a `NOTIFY` on the run's channel that
lets the supervisor poll immediately instead of at its next heartbeat.
ADR-0010's rule holds without amendment: **the notification is a
latency optimization and no consumer may depend on receiving it.** With
the notification, cancellation is observed in milliseconds. Without it,
it is observed within one heartbeat interval. Both are correct; only one
is fast.

`202` rather than `200` for the `RUNNING` case is deliberate and
[runtime-loop.md](runtime-loop.md) already gives the reason: a
cancellation observed at points 5 or 6, after `effect_sent_at` is
stamped, does not abandon the tool call in flight. The request was
accepted; the work has not necessarily stopped. A `200` would claim
otherwise.

Cancelling an already-terminal run is `200` and does nothing. It is not
a `409`, because the client's intent — that this run not be running —
is satisfied, and returning an error for a satisfied intent makes
correct retry logic harder to write than it needs to be. Cancellation
is the one operation in this API that is idempotent by nature, and the
response reflects that.

`cancel_requested_at` is set once. A second cancel on a run that already
has one is `202` and leaves the original timestamp, so that the audit
record shows when cancellation was first requested rather than when it
was last retried.

## Delivering an answer to a waiting run

```http
POST /v1/runs/{run_id}/input
```

ADR-0009 names this route and Section 27.3 designs its behaviour: the
service validates the run is `WAITING_FOR_USER`, appends the answer as
the resolution of the outstanding question, re-enqueues the run, and
the loop continues from its checkpoint. Three things this document
adds.

**The request carries content blocks and optionally the question
identifier.**

```json
{
  "content": [{"type": "text", "text": "Use the EU region."}],
  "question_id": "uuid"
}
```

**Idempotency is on `(run_id, question_id)`**, which is Section 27.3's
own rule. `question_id` is the identifier of the outstanding
`conversation.ask_user` invocation, and it is carried on the
`run.waiting_for_user` event so a client that streams the run has it
without a second request. When omitted, the server uses the run's
current outstanding question, which is unambiguous because there is at
most one. Supplying a stale `question_id` — one already resolved — is
`conflict` with status 409 rather than a silent second resume, and that
is the entire reason the field exists.

**A run not in `WAITING_FOR_USER` is `invalid_state` with status 409.**
Not 404: the run exists, and telling a client its answer arrived too
late is more useful than telling it the run is gone. A run that timed
out waiting has already failed with `INPUT_DEADLINE_EXCEEDED`, and the
409 body's `details` names that.

The response is `202` with the same body as message submission, so a
client that answered a question and a client that started a turn hold
the same shape and can share a code path.

## The event stream

```http
GET /v1/runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: 41
```

Section 16 fixes the frame format and one sentence of consumer
behaviour: *"On reconnect, replay persisted events after
`Last-Event-ID`, then continue streaming new events."* ADR-0010 fixes
the transport underneath it — `LISTEN`/`NOTIFY` for wakeup and live
delivery, persisted events as the replayable source of truth, the SSE
`id` as the per-session sequence, and transient events never replayed.
This section is what sits between those two: the framing rules, the
handoff that makes replay gapless, and the four things a client must
not infer.

### Frame shapes

Two kinds of frame travel on this stream and they are distinguished by
one field.

A **persisted** frame carries an `id`, and its `id` is the event's
`sequence` — the per-session number
[event-log-and-persistence.md](event-log-and-persistence.md) allocates
by atomic increment.

```text
id: 42
event: tool.call.completed
data: {"run_id":"...","tool_name":"math.calculate",
       "status":"succeeded"}
```

A **transient** frame carries no `id` at all.

```text
event: message.delta
data: {"run_id":"...","text":"twelve times nine is "}
```

That absence is a specification, not an omission, and it is the single
most load-bearing rule in this section. The EventSource specification
advances a client's last-event-ID only when a frame carries an `id`
field. A transient token delta has no sequence, because it was never
persisted and ADR-0010 rejected persisting it. An implementation that
stamps a synthetic `id` on a delta — a counter, the previous sequence,
a timestamp — corrupts every subsequent reconnect: the browser sends
that value as `Last-Event-ID`, the server replays persisted events
after it, and events the client never received are skipped. Omitting
`id` on transient frames is what keeps `Last-Event-ID` meaning exactly
"the last persisted event I hold".

The `data` payload is a single line of compact JSON. A payload
containing a newline would terminate the frame early, so serialization
uses no pretty-printing and escapes newlines inside strings, which
compact JSON does by construction.

### The ids are per-session and the endpoint is per-run

A run's stream shows the sequences belonging to that run's events, and
those sequences come from a counter shared with every other run in the
session. A stream that shows 41, 42, then 57 is a correct stream: 43
through 56 belong to a different run, or to session-level events this
endpoint does not carry.

**A client must never infer a gap from non-contiguous ids.** This is
the first of the four non-inferences, and it is the one a client author
gets wrong by default, because a monotonically increasing integer
labelled `id` looks like a sequence number for the thing being
streamed. It is a sequence number for the session.

The alternative — a per-run counter — was considered and rejected here
because ADR-0010 already fixed the id as the session sequence, and
because a per-run counter would require a second allocation column and
a second uniqueness constraint to protect it, doubling the machinery
[event-log-and-persistence.md](event-log-and-persistence.md) built once
and carefully.

### Replay, and the handoff that makes it gapless

The naive implementation reads the persisted events after
`Last-Event-ID`, writes them, then subscribes to the notification
channel. It drops every event that is committed between the read and
the subscribe. The window is small and it is not empty, and a stream
that silently omits `tool.call.completed` is worse than one that dies.

The correct order inverts those steps and adds a buffer.

1. **Subscribe first.** Issue `LISTEN` on the session's channel before
   any read. Arrivals are buffered in memory, not written.
2. **Read the persisted prefix.** Select the events for this run with
   `sequence > Last-Event-ID`, ordered by sequence, and write each one.
   Note the highest sequence written as `w`. With no `Last-Event-ID`,
   the prefix starts at the beginning of the run.
3. **Drain the buffer, discarding anything at or below `w`.** Those
   are the events the read already returned. What remains is exactly
   what committed during the read.
4. **Go live.** Subsequent notifications are written as they arrive.

Subscribing before reading is what makes it gapless. Discarding by
sequence is what makes it duplicate-free. Neither works without the
other, and the buffer is what lets both hold at once.

Two details make step 4 safe.
[event-log-and-persistence.md](event-log-and-persistence.md)'s rule
that *"a reader asks for events after a watermark; it never waits for a
specific next sequence"* applies here unchanged: gaps in the sequence
are normal, and a stream that blocked waiting for sequence `w+1` to
appear would block forever the first time a concurrent run took that
number. And ADR-0010's rule that *"notification is a hint, never a
delivery"* means the live path cannot be the only path — a stream that
has seen no notification for its poll interval re-reads from `w`, which
costs an indexed query on `(run_id, id)` and removes any dependence on
a notification arriving.

**A client must never assume replay includes transient events.** This
is the second non-inference and ADR-0010 states it: transient events
are never replayed. A client that reconnects mid-answer will not
receive the token deltas it missed. It receives
`assistant.message.completed` with the finished text, which is the
durable record of what the deltas were building. A UI that renders
deltas and never reconciles against the completed message will show a
truncated answer after a reconnect, and the fix is to reconcile, not
to persist deltas.

### Heartbeats

The server writes an SSE comment every fifteen seconds when nothing
else has been written.

```text
: heartbeat
```

A comment frame carries no `id`, no `event`, and no `data`, so it
cannot affect `Last-Event-ID` and cannot be mistaken for an event. It
exists because a run can sit in `WAITING_FOR_APPROVAL` for four hours
under [policy-and-approvals.md](policy-and-approvals.md)'s expiry
schedule, and an idle TCP connection through a proxy does not survive
four hours. Fifteen seconds is comfortably under the shortest default
idle timeout in common proxies.

### When the stream ends, and when it does not

The stream closes after writing the run's terminal event —
`run.completed`, `run.failed`, or `run.cancelled` — and the server
closes it rather than waiting for the client. A client that reconnects
to a terminal run receives the persisted events after its
`Last-Event-ID` and then an immediate close, which is what makes a
reconnect-after-completion return the tail of the run rather than
hanging.

The stream does **not** close when a run enters `WAITING_FOR_APPROVAL`
or `WAITING_FOR_USER`. Those are not terminal, the run resumes on the
same identifier, and a client watching a run through an approval should
not have to re-establish anything. This is the third non-inference: **a
client must not treat a suspension event as an end of stream.**

A stream opened on a run identifier that does not exist in the
principal's tenant is `not_found` with status 404, delivered as a
normal JSON error response before any SSE framing begins. Once framing
has begun the status is already `200` and cannot be revised, so every
check that can fail — authentication, scope, tenancy, existence —
happens before the first byte of the stream.

### Overflow

A client that reads slower than the run produces will eventually fill
the server's send buffer. The server does not buffer without bound, and
it does not drop events silently.

When a stream's buffer exceeds its limit, the server writes a final
frame and closes:

```text
event: stream.overflow
data: {"last_sequence": 812}
```

`stream.overflow` is a transport frame, not a persisted event type, and
it carries no `id` for the same reason transient frames do not. The
client's recovery is to reconnect with `Last-Event-ID: 812`, which
replays from the durable log at whatever rate the client can manage.
This is the fourth non-inference and the reason it is safe to state so
briefly: **a client must not treat the stream as the source of truth.**
The event log is. The stream is a fast path to it, and every failure
mode of the fast path resolves to reading the log.

### What the payloads may not contain

Three rules constrain every frame on this stream, and all three come
from documents that already state them.

**No raw reasoning text, ever.** ADR-0006 forbids persisting it and
ADR-0007 keeps the provider-opaque continuation in the run checkpoint.
Reasoning deltas may be streamed as transient frames carrying text the
provider marked as reasoning, and they are never persisted and never
replayed; what persists is that reasoning occurred and its token count.

**No tracebacks.** Section 16's rule applies to the stream as much as
to a response body. A `tool.call.failed` payload carries the failure
class and message that the error taxonomy above defines, not the
exception that produced it.

**Tool results are subject to the same trust labelling as everywhere
else.** [tool-system.md](tool-system.md)'s rule that external text
reaches the model only under a label does not bind the API, which is
not the model — but a client rendering a tool result is rendering
untrusted text, and the payload therefore carries the same
`trust` label the context engine uses so that a UI can mark it.

## Approvals

[policy-and-approvals.md](policy-and-approvals.md) owns this surface
and writes the two read routes verbatim. This document adds only the
response bodies and the resolve semantics that the API layer must
implement.

```http
GET /v1/approvals?status=pending&run_id=&session_id=&limit=&cursor=
GET /v1/approvals/{approval_id}
POST /v1/approvals/{approval_id}/resolve
```

The list is tenant-scoped from the authenticated principal. **Tenancy
is never a query parameter**, which is the spec's own rule and worth
repeating here because a list endpoint is where somebody adds one.

An approval reads as:

```json
{
  "id": "uuid",
  "run_id": "uuid",
  "session_id": "uuid",
  "status": "PENDING",
  "tool_name": "shell.exec",
  "action_summary": "Delete 3 files under /tmp/build",
  "arguments": {"command": "rm -rf /tmp/build/*"},
  "risk": "HIGH",
  "policy_reason": "Destructive filesystem operation",
  "expires_at": "2026-01-01T04:00:00Z",
  "created_at": "2026-01-01T00:00:00Z",
  "resolved_at": null,
  "resolved_by": null,
  "decision": null
}
```

`policy_reason` is a human-readable statement of why approval was
required. **The rule that fired is not exposed**, which is
[policy-and-approvals.md](policy-and-approvals.md)'s decision: a rule
identifier on the wire becomes a rule identifier in a client's
conditional, and the rule set is meant to be editable without breaking
clients.

Resolution takes the two-value decision vocabulary and nothing else.

```json
{"decision": "approve_once"}
{"decision": "deny", "reason": "Do not perform this action."}
```

Four behaviours the API layer owns:

1. **`approval.resolve` is required**, and it is the one scope the
   corpus already names.
2. **Resubmitting the same decision is `200`.** Resubmitting a
   different one is `conflict` with status 409 and
   `details.reason = "approval_already_resolved"`. This is the spec's
   rule, and it makes a client's retry safe without making a client's
   change of mind silent.
3. **Resolving an expired or cancelled approval is `409`**, carrying
   the current status in `details`. An expired approval has already
   failed its run with `APPROVAL_EXPIRED`, and the 409 is what tells a
   human why their click did nothing.
4. **Cross-tenant is `404`.** Never `403`.

Resolution writes the decision and then `NOTIFY`s, on the same terms as
every other notification in this document: the run resumes from its
checkpoint whether or not the notification arrives.

## Artifacts

```http
GET /v1/artifacts/{artifact_id}
GET /v1/artifacts/{artifact_id}/content
```

Section 16's rule — check authorization before returning either
metadata or content — is unchanged and is why both routes require
`artifact.read` and both apply the tenant check before touching
storage.

Metadata is the `artifacts` row minus its storage location:

```json
{
  "id": "uuid",
  "session_id": "uuid",
  "run_id": "uuid",
  "name": "report.csv",
  "media_type": "text/csv",
  "sha256": "e3b0c44298fc1c14...",
  "size_bytes": 41200,
  "metadata": {},
  "created_at": "2026-01-01T00:00:00Z"
}
```

`storage_uri` is not returned. It names a bucket and a key in the
deployment's object store, it is the one field that would let a client
attempt to address storage directly, and a client has no use for it
that the content route does not serve.

Content is served as the bytes with three headers, and the third is a
security control rather than a convenience.

```text
Content-Type: <media_type>
Content-Length: <size_bytes>
Content-Disposition: attachment; filename="report.csv"
```

**`Content-Disposition` is always `attachment`, never `inline`, for
every media type.** An artifact is bytes an agent produced, frequently
from a tool that processed untrusted input. Serving one inline from the
API's origin means an artifact whose media type is `text/html`
executes as script in the API's origin with the user's session — which
is stored cross-site scripting with extra steps. The filename is
sanitized and quoted, and a name containing a quote, a newline, or a
path separator is rejected at creation rather than escaped at read.

`ETag` is the `sha256`, which makes conditional requests free: the hash
is already stored and an artifact is immutable once written. A
`If-None-Match` that matches returns `304`.

Range requests are not supported in 0.1. A `Range` header is ignored
and the full body is returned with `200`, which is the behaviour the
HTTP specification permits for a server that does not implement ranges.

## Health

```http
GET /health/live
GET /health/ready
```

Both are unauthenticated, and their bodies are designed so that being
unauthenticated is safe: neither reveals a version, a hostname, a
dependency address, a queue depth, or a count of anything.

`GET /health/live` returns `200` with `{"status": "ok"}` if the process
is running. It checks nothing else. A liveness probe that checks a
dependency restarts a healthy process when a dependency blinks, which
converts a partial outage into a full one.

`GET /health/ready` returns `200` with `{"status": "ready"}` or `503`
with `{"status": "not_ready"}`. It verifies two things: a database
round-trip on a pooled connection, and that configuration validation
passed at startup. Section 16's rule that it must not call a model
provider on every probe is unchanged and is the reason readiness does
not verify provider reachability at all — a probe that costs a provider
call is a probe that costs money proportional to its frequency, and a
readiness signal that flaps with a third party's availability takes the
service out of rotation for something the service cannot fix.

Neither endpoint returns a reason for failure. A readiness probe is
read by a load balancer, and the operator's diagnostic path is the
structured log, which carries the reason with full detail and is not
public.

## Pagination

The Milestone 5 baseline has one paginated route, `GET /v1/approvals`.
ADR-0050 applies the same rule to the later `GET /v1/sessions` route.
The rule is written here rather than in
[policy-and-approvals.md](policy-and-approvals.md) because both list routes
share it.

```json
{
  "items": [],
  "next_cursor": "eyJrIjoiMjAyNi0wMS0wMVQwMDowMDowMFoiLCJpIjoi..."
}
```

Four rules.

1. **The cursor is keyset, never offset.** It encodes the last item's
   `(sort_key, id)` pair. An offset paginator over a table that is
   still being written skips rows and repeats rows, and an approvals
   list is exactly such a table.
2. **The cursor is opaque.** base64url over compact JSON, and a client
   that decodes it is relying on something that will change. It is not
   signed, because it carries no authorization — the tenant check is
   applied to the query it seeds, so a forged cursor can only reach
   rows the principal could already reach.
3. **`limit` defaults to 50 and caps at 200.** A larger value is
   clamped rather than rejected, because a client asking for more than
   the server will give is not making an error, it is making a request
   the server can partially honour.
4. **`next_cursor` is null on the last page.** A client stops when it
   is null, not when `items` is empty, because the final page can be
   both full and last.

Approvals use `created_at DESC, id DESC`; sessions use
`updated_at DESC, id DESC`. In both, `id` is the tiebreaker that makes the key
total. A cursor from a different sort or a different filter is not detected and
produces a wrong-looking page, which is acceptable because the client that
constructs one is the client that took the cursor apart.

## Limits

Two limits have shapes here and mechanisms later, which is a deliberate
split: fixing the response shape now means a client written against 0.1
already handles the day the mechanism arrives, and inventing the
mechanism now would be inventing it without the operational data that
should set its numbers.

**Request bodies are bounded at 1 MiB.** A larger body is
`payload_too_large` with status 413, checked from `Content-Length`
before the body is read and enforced again while reading it, because a
chunked request has no `Content-Length` to check. The bound is on the
request, not on message content, and it is generous for a JSON body of
text blocks — an artifact is not uploaded through this API in 0.1.

**Rate limiting is declared and not implemented.** `rate_limited` with
status 429 is in the code table, and no handler returns it in 0.1.
Section 22 names per-tenant rate limits as a baseline security item,
which is where the mechanism belongs. What is fixed now is the response
shape a client must handle:

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 30
```

with the standard error envelope as the body. A client that implements
`Retry-After` handling against 0.1 will find nothing to handle and will
be correct when something does.

The stream has no explicit connection limit in 0.1. Concurrency is
bounded by the connection pool and by the per-stream buffer that
produces `stream.overflow`, and a real limit needs a real number that
0.1 has no basis for choosing.

## The four application services

[bootstrap-and-composition.md](bootstrap-and-composition.md) declares
`ApplicationServices` with four fields and no method on any of them.
Section 17 says the CLI calls the same application service the API
calls, which makes these signatures a shared contract rather than an
API implementation detail, and that is why they are written here: this
is the document where every one of them is exercised.

```python
class SessionService(Protocol):
    async def create(
        self, principal: Principal, agent_id: str,
        metadata: dict[str, Any],
    ) -> SessionView: ...

    async def get(
        self, principal: Principal, session_id: UUID,
    ) -> SessionView: ...

    async def list(
        self, principal: Principal, limit: int,
        cursor: str | None,
    ) -> Page[SessionView]: ...

    async def delete(
        self, principal: Principal, session_id: UUID,
    ) -> None: ...

    async def close(
        self, principal: Principal, session_id: UUID,
    ) -> SessionView: ...
```

```python
class RunService(Protocol):
    async def submit(
        self, principal: Principal, session_id: UUID,
        content: list[ContentBlock],
        idempotency_key: str | None,
        trace_id: str | None,
    ) -> SubmitResult: ...

    async def get(
        self, principal: Principal, run_id: UUID,
    ) -> RunView: ...

    async def cancel(
        self, principal: Principal, run_id: UUID,
    ) -> CancelResult: ...

    async def deliver_input(
        self, principal: Principal, run_id: UUID,
        content: list[ContentBlock],
        question_id: UUID | None,
    ) -> SubmitResult: ...

    async def stream(
        self, principal: Principal, run_id: UUID,
        after_sequence: int | None,
    ) -> AsyncIterator[StreamFrame]: ...
```

```python
class ApprovalService(Protocol):
    async def list(
        self, principal: Principal, filters: ApprovalFilters,
        limit: int, cursor: str | None,
    ) -> Page[ApprovalView]: ...

    async def get(
        self, principal: Principal, approval_id: UUID,
    ) -> ApprovalView: ...

    async def resolve(
        self, principal: Principal, approval_id: UUID,
        decision: ApprovalDecision, reason: str | None,
    ) -> ApprovalView: ...
```

```python
class ArtifactService(Protocol):
    async def get(
        self, principal: Principal, artifact_id: UUID,
    ) -> ArtifactView: ...

    async def open_content(
        self, principal: Principal, artifact_id: UUID,
    ) -> ArtifactContent: ...
```

Five properties hold across all of them, and each one is a rule an
implementer would otherwise decide per method.

1. **`Principal` is the first argument of every method.** Not a context
   variable, not a thread local, not ambient. Authorization is
   therefore visible in the signature, and a method that forgets it
   cannot be called.
2. **The return types are views, not rows.** `SessionView`, `RunView`,
   `ApprovalView`, and `ArtifactView` are the shapes this document
   specifies, and the exclusions above — `storage_uri`, `lease_owner`,
   `next_event_sequence`, `error_class` — are enforced by the view
   existing rather than by each caller remembering.
3. **Services raise the taxonomy's exceptions and never HTTP.** The API
   layer maps an exception to a status through the one table above,
   and the CLI maps the same exception to an exit code. A service that
   raised an HTTP error would make the CLI import a web framework.
4. **No service method takes a `tenant_id`.** It is on the
   `Principal`, and a second source for it is a second thing that can
   disagree.
5. **`stream` returns frames, not events.** A `StreamFrame` is either
   persisted-with-a-sequence or transient-without-one, which is what
   makes the "no `id` on transient frames" rule a type property rather
   than a convention the SSE writer has to remember.

`open_content` returns a handle rather than bytes so that a large
artifact streams from object storage without being buffered in the API
process.

## Milestones

The fourteen-route baseline in this document is Milestone 5 work, which is what
Section 21 already says. The two ADR-0050 routes are separately authorized
post-Milestone 9 work and do not change a completed milestone's acceptance
criteria or gate count. The table exists because several things this document
specifies are not routes or do not land with the baseline.

```text
# capability                                milestone
Principal, scope set, exact-match check     M5
AUTH_MODE dev and token, startup refusal    M5
error envelope, code table, status map      M5
request identifiers and trace propagation   M5
sessions create, read, close                M5
sessions list and delete                    post-M9 explicit assignment
submit, HTTP idempotency, routing table     M5
run read, RunView                           M5
cancel endpoint, cancel_requested_at        M5
input delivery, question_id idempotency     M5
SSE framing, replay, gapless handoff        M5
approvals list, read, resolve               M5
artifacts metadata and content              M5
health live and ready                       M5
keyset pagination                           M5
request size limit and 413                  M5
the four application services               M5
idempotency_keys table and its DDL          M2
rate limiting and 429                       deferred
```

The `idempotency_keys` table is Milestone 2 because
[event-log-and-persistence.md](event-log-and-persistence.md) already
schedules it there with the rest of the schema. Nothing reads it until
Milestone 5. This is the same shape as the tool idempotency port —
port and semantics early, index late — and it is stated here so that
an implementer does not move the DDL forward or the endpoint back.

## Contradictions resolved

```text
# conflict                          resolution
1  session status case              uppercase; §16 sample illustrative
2  "idempotency key" names two      two mechanisms, two scopes
3  cancel unspecified end to end    §16 endpoint writes the column
4  second message in a session      409, except WAITING_FOR_USER -> 202
5  cancel status code               200 when no lease, 202 when RUNNING
6  SSE ids non-contiguous per run   correct; ids are per session
7  one error code, no vocabulary    the taxonomy, snake-cased
8  no route reads a session         GET /v1/sessions/{id} added
9  five vs six observation points   already reconciled by the loop spec
10 approval routes at M4 or M5      M5; the service and CLI are M4
11 added M5 route has no scope row  session.read; M5 surface is 14
12 two run column counts, neither   fifteen in §15, twenty-six live
13 current history routes           session.read/write; current surface is 16
```

Row 3 is the one worth expanding, because the readiness review stated
it one clause too widely — as the whole cross-process cancel path
being unspecified rather than half of it. The worker half is
specified: [runtime-loop.md](runtime-loop.md) gives the poller, its
cadence, its query, and the six observation points. Only the API half
was missing, and it was one sentence — the endpoint writes
`runs.cancel_requested_at`. The review's verdict was right either way,
and [readiness.md](readiness.md) has since been narrowed to match.

Row 9 is listed because a reader who finds Section 16's five
observation points and the loop's six will look for a reconciliation,
and it exists: the loop's own text maps points 1 through 5 onto the
phases and identifies point 6 as Section 16's "during long-running
sandbox execution where possible". This document introduces nothing
there.

Row 10 is a milestone reading rather than a design conflict.
[policy-and-approvals.md](policy-and-approvals.md) added
`GET /v1/approvals` and `GET /v1/approvals/{id}` so that
`agent approval list` had something to call, and made them step 11 of
its Milestone 4 build order. What `agent approval list` calls is the
application service, not the route — that document says so in the same
paragraph, and decision 3 above is why: a service that raised an HTTP
error would make the CLI import a web framework. So the dependency is
Milestone 4 and the route is not. Section 21 agrees: "Approval API and
CLI" is a Milestone 4 implement item, and Milestone 5's implement list,
which is the entire HTTP surface down to the error envelope and the
health endpoints, names no approval work. The build order has been
narrowed to the service methods, and all three approval routes land
here at Milestone 5 with every other route.

## Hard gates

Failing one of these blocks the milestone. They are registered in the
gate registry with identifiers, like every other gate. All ten are new
and all ten are Milestone 5, which is the milestone that had one.

1. **Every returned code is in the vocabulary.** A test walks the
   registered routes, exercises each error path, and asserts every
   `error.code` appears in the code table. A code not in the table
   fails the build. **M5.**
2. **Every error maps to exactly one status.** The class-to-code-to-
   status table is a single mapping in code; a test asserts it is
   total over the error taxonomy and that no class appears twice.
   An unmapped class resolves to `internal_error` and 500, and the
   test asserts that fallback is reached only by classes deliberately
   absent from the table. **M5.**
3. **No handler reads a tenant from a request.** An AST walk over the
   API package asserts that no handler binds `tenant_id` from a path
   parameter, query parameter, header, or request body. The only
   source is the `Principal`. **M5.**
4. **Cross-tenant reads are not found.** For every tenant-scoped
   route, a request carrying a resource identifier belonging to
   another tenant returns 404. A 403 from any of them fails. **M5.**
5. **Health is the only unauthenticated surface.** A walk over the
   route table asserts every route except `/health/live` and
   `/health/ready` declares a required scope, and that the two health
   routes declare none. A new route with no scope declaration fails
   the build rather than shipping open. **M5.**
6. **Transient frames carry no id.** Every frame the SSE writer emits
   without a persisted sequence has no `id:` line. Asserted over a
   captured stream containing token deltas, heartbeats, and a
   `stream.overflow`. **M5.**
7. **Replay is gapless and duplicate-free.** A stream disconnected
   mid-run and reconnected with `Last-Event-ID` yields each persisted
   sequence for that run exactly once across both connections, with
   none missing. Run under concurrent writes to the same session so
   the sequence gaps are real. **M5.**
8. **Cancellation reaches a running worker.** A run cancelled through
   the endpoint while `RUNNING` in a separate process reaches
   `CANCELLED`, and it does so with the notification suppressed —
   proving the poller is sufficient and the notification is an
   optimization. **M5.**
9. **Submission is idempotent and a reused key is a conflict.** The
   same key with the same body returns the same `run_id` with 200 and
   creates exactly one run under concurrent submission; the same key
   with a different body returns 409. **M5.**
10. **Artifact content is never inline.** Every response from the
    content route carries `Content-Disposition: attachment`,
    including for `text/html` and `image/svg+xml`. **M5.**

## Tracked metrics

Not gates. Watched, and a regression is an argument rather than a
build failure.

- **Time to first frame** on a stream opened against a `RUNNING` run,
  which is the number a user experiences as responsiveness.
- **Replay depth on reconnect**, the count of persisted events written
  before live streaming resumes. A number that climbs means clients
  are disconnecting more often than they should.
- **Cancellation latency**, from the endpoint's write to the run
  reaching `CANCELLED`, reported separately with and without the
  notification so the optimization's value stays measurable.
- **`stream.overflow` rate.** A non-zero rate is not a bug; a rising
  one means the buffer is sized wrong or a client is pathological.
- **Idempotent-replay rate**, the share of submissions returning 200
  rather than 202. A rise usually means clients are timing out on a
  server that got slower.

## Decisions

1. **The wire error vocabulary is the existing taxonomy under one
   rule.** Section 16's single example, `tool_validation_error`, is
   `ToolValidationError` snake-cased. That is the whole convention, it
   was already chosen, and applying it mechanically produces a code
   list nobody had to invent.
2. **Four classes never cross the boundary.** `WorkerFenced` is not a
   run failure, `EmptyModelTurn` is retried internally, and two
   remaining internal classes have no client-actionable meaning. They
   are absent from the table deliberately rather than by oversight.
3. **A supplied `X-Request-Id` is echoed and trusted with nothing.**
   Correlation and deduplication are different problems and a client's
   retry logic must not silently become the server's dedup logic.
4. **Scopes are exact-match strings over a closed vocabulary.** No
   wildcard, no hierarchy, no implication. A hierarchy needs a grammar
   and a grammar can be subtly wrong in the direction of granting
   access.
5. **Cross-tenant is 404, never 403.** A 403 confirms existence, which
   turns every identifier into an oracle.
6. **`SessionStatus` is uppercase.** Section 16's lowercase sample is
   read as illustrative and Section 16 is not edited. Recorded as an
   open question.
7. **The HTTP idempotency key and the tool idempotency key are two
   mechanisms.** Different scope, different table, different
   milestone, one unfortunate name.
8. **A reused key with a different body is a 409.** `request_hash`
   exists to make a client bug loud instead of returning an unrelated
   run.
9. **A second message to a busy session is 409, except when the run is
   `WAITING_FOR_USER`, where it is routed and returns 202.** This is
   Section 27.3's deterministic routing rule, with "route" as the
   configured default.
10. **The cancel endpoint writes `cancel_requested_at` and returns
    202 for a `RUNNING` run, 200 otherwise.** The direct transitions
    are safe only because both `WAITING_*` states released the lease.
11. **Cancelling a terminal run is 200 and does nothing.** The intent
    is satisfied; an error for a satisfied intent makes correct retry
    logic harder to write.
12. **Transient SSE frames carry no `id`.** This is what keeps
    `Last-Event-ID` meaning "the last persisted event I hold", and a
    synthetic id would silently corrupt every subsequent reconnect.
13. **Replay subscribes before it reads.** Subscribe, read, drain the
    buffer discarding at or below the high-water mark, then go live.
    Any other order has a window.
14. **The stream survives suspension and closes on a terminal event.**
    A run resuming after an approval keeps its stream.
15. **Overflow closes the stream with a resumable marker.** The client
    reconnects against the durable log, which is the source of truth
    the stream was only ever a fast path to.
16. **Artifact content is always an attachment.** An inline artifact
    is stored cross-site scripting.
17. **Pagination is an opaque keyset cursor.** Offset pagination over
    a table still being written both skips and repeats.
18. **413 and 429 have shapes now and mechanisms later.** A client
    written against 0.1 handles the day the mechanism arrives.
19. **`Principal` is the first argument of every application service
    method.** Authorization visible in the signature is authorization
    that cannot be forgotten.
20. **The Milestone 5 baseline adds only
    `GET /v1/sessions/{id}`.** It is the one route a reconnecting client
    could not do without at that milestone, and it exposes no capability a
    client with its own records lacked.
21. **The completed Milestone 5 surface is fourteen routes.** Thirteen
    inherited and one added. ADR-0050 later adds two separately authorized
    routes; it does not rewrite this historical route census.
22. **`runs` is twenty-six columns and this returns thirteen.**
    Section 15 declares fifteen and four other documents add eleven.
    A body that withholds half a table should say which half, so that
    the next document to add a column knows it is adding a private
    one unless it says otherwise.
23. **The current history index is server-authoritative.** Clients may cache
    `GET /v1/sessions`, but they reconcile and prune from the complete server
    page set rather than treating local history as a second source of truth.
24. **Deletion is hard, idempotent, and asynchronous only at the external-byte
    boundary.** The database graph is gone before `204`; a content-free
    tombstone makes retries stable; queued artifact references let maintenance
    finish byte deletion without retaining conversation content.

## Open questions for review

1. Should `auth_token` become a list so a token can be rotated without
   restarting? One token means the only rotation procedure drops
   in-flight requests. It changes a declared `Settings` field, which
   is why it is asked rather than decided.
2. Is uppercase `SessionStatus` right, given Section 16's sample shows
   `"active"`? The alternative is lowercase session status beside
   uppercase run status on one wire, which a client library encodes as
   two enums and a comment. Editing Section 16's sample would settle
   it and this assignment does not edit Section 16.
3. Should a second message to a session with a `WAITING_FOR_USER` run
   route to that run by default, as decided, or reject with guidance?
   Section 27.3 permits either and requires one. Routing matches what
   a user answering a question expects; rejecting makes every client
   implement the rule the server already has the state to apply.
4. Should `GET /v1/runs` exist — a list of runs in a session? It is
   the obvious next route, but the current history client needs only
   `active_run_id` and `last_run_id`; a complete multi-run transcript browser
   would need a separately authorized list.
5. Is fifteen seconds the right heartbeat interval? It is chosen to
   sit under common proxy idle timeouts and is otherwise arbitrary.
6. Should the event stream be available per session as well as per
   run? The ids are already per session, so a session-scoped stream
   route is nearly free, and no client in 0.1 wants it.
7. Should the API expose the policy rule that fired on an approval,
   behind an operator scope? The policy spec withholds it from clients
   for good reason, and an operator debugging a denial currently has
   only the structured log.
8. Is there an event retention policy? The corpus bounds checkpoints,
   memory, and artifacts, and never bounds `events`. Replay depends
   on the log being complete for a session's life, so any retention
   rule interacts directly with the reconnect guarantee this document
   makes.
