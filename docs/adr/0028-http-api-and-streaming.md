# ADR-0028: The HTTP API surface, the error vocabulary, and the event stream

- Status: Accepted
- Date: 2026-07-27
- Related: Sections 5 (principal), 13 (error taxonomy), 16 (API
  contract), 17 (CLI), 19 (observability), 21 (Milestone 5), 22
  (security baseline), 27 (run, turn, session), ADR-0004 (the run
  queue), ADR-0006 (no private reasoning storage), ADR-0009 (run,
  turn, and session model), ADR-0010 (live event transport), ADR-0017
  (layered approval), ADR-0023 (the run loop), ADR-0024 (composition
  root)
- Detailed design: `docs/plan/http-api-and-streaming.md`

## Context

The readiness review's verdict on Milestone 5 was that coding can reach
it and should not enter it, and that an API specification was the
single most valuable document not yet written. That verdict is narrower
than "the API is undocumented", which is not true.

Section 16 is a real design. It fixes the method and path of nine
endpoints, the request body where there is one, the `202` on
submission, the SSE frame format, the `Last-Event-ID` replay rule, the
shape of the error envelope, the authentication posture, and the rule
that tracebacks are never exposed. Three further routes are named
elsewhere — the two approval reads in the policy spec, and
`POST /v1/runs/{id}/input` in ADR-0009 — and the health probe is two
routes rather than one. Thirteen routes.

What the corpus does not contain is a response body for twelve of the
thirteen. Only the session created by `POST /v1/sessions` has one
written down. An implementer holding Section 16 knows
`GET /v1/runs/{run_id}` exists and cannot write it, because nothing
states which of the run row's columns are public, what
`WAITING_FOR_APPROVAL` tells a client to do, or whether the failure
that ended a run is visible at all.

Six more gaps sit inside Section 16 rather than outside it, and the
readiness review names each: the error envelope has one worked example
and no code list; request identifiers are an implement bullet with no
semantics; `Idempotency-Key` is named twice and specified nowhere; the
SSE consumer side is one sentence; authentication is designed only at
its refusal, so nothing says what a successful authentication produces;
and nothing turns an HTTP cancel into an observation by a worker in a
different process.

Two further problems became visible only while writing the design.
`SessionStatus` is referenced in Section 5 and declared nowhere, and
Section 16's one sample shows it lowercase while every `RunStatus`
value in the corpus is uppercase. And the word "idempotency key" names
two unrelated mechanisms — the HTTP header on submission, and
`tool_invocations.idempotency_key`, which the milestone map schedules
as a Milestone 1 port under `ToolInvocationRepository`.

## Decision

1.  **The wire error vocabulary is the existing error taxonomy under
    one rule, not a new vocabulary.** Section 16's single worked
    example, `tool_validation_error`, is `ToolValidationError`
    snake-cased. That is the convention; applying it mechanically to
    Section 13's twenty-three classes and the runtime loop's eight
    produces the code list. Four API-specific codes are added for
    conditions that have no domain class — `malformed_request`,
    `unsupported_media_type`, `payload_too_large`, `rate_limited`.
2.  **Four classes deliberately never cross the boundary.**
    `WorkerFenced` is not a run failure, `EmptyModelTurn` is retried
    internally, and two remaining classes have no client-actionable
    meaning. An unmapped class resolves to `internal_error` and 500.
3.  **Authentication produces a `Principal` and nothing downstream
    re-reads the credential.** Section 5's four-field `Principal` is
    the only authorization input. No handler takes a `tenant_id` from
    a path, query, body, or header.
4.  **Scopes are exact-match strings over a closed dotted
    vocabulary.** No wildcard, no prefix rule, no hierarchy in which
    `run.write` implies `run.read`. Roles are bundles resolved at
    authentication; the API never checks a role.
5.  **A resource in another tenant is 404, never 403**, generalizing
    the rule the policy spec already fixes for approvals. Scope is
    checked before tenancy, so a principal with no scope cannot probe
    for existence by watching 403 become 404.
6.  **`SessionStatus` is declared here, uppercase, with two values.**
    Section 16's lowercase sample is read as illustrative and Section
    16 is not edited. Recorded as an open question.
7.  **The HTTP idempotency key and the tool idempotency key are two
    mechanisms with one unfortunate name.** Different scope, different
    table, different milestone. A repeat with a matching
    `request_hash` returns the original run with 200; a repeat with a
    different hash is 409, which makes a client bug loud rather than
    returning an unrelated run.
8.  **A second message to a session with a non-terminal run is 409,
    except where that run is `WAITING_FOR_USER`, where it is routed to
    input delivery and returns 202.** This is Section 27.3's
    deterministic routing rule with "route" as the configured default.
9.  **The cancel endpoint writes `runs.cancel_requested_at` and
    returns 202 for a `RUNNING` run; for `QUEUED` and both `WAITING_*`
    states it transitions directly to `CANCELLED` and returns 200.**
    The direct transitions are safe only because those states hold no
    lease. Cancelling a terminal run is 200 and does nothing.
10. **Transient SSE frames carry no `id` field.** The EventSource
    specification advances a client's last-event-ID only on a frame
    carrying `id`, so a synthetic id on a token delta silently
    corrupts every subsequent reconnect.
11. **Replay subscribes before it reads.** `LISTEN` first, buffer
    arrivals, read the persisted prefix, note the high-water mark,
    drain the buffer discarding at or below it, then go live.
    Subscribe-before-read is what makes it gapless; discard-by-
    sequence is what makes it duplicate-free.
12. **Overflow closes the stream with a resumable marker** rather than
    buffering without bound or dropping silently. The client
    reconnects against the durable log.
13. **Artifact content is always served `Content-Disposition:
    attachment`, for every media type.** An artifact served inline
    from the API origin is stored cross-site scripting.
14. **Health is the only unauthenticated surface, and its bodies carry
    no version, host, dependency, or count**, so that being
    unauthenticated is safe.
15. **The four application services get their method signatures
    here**, with `Principal` as the first argument of every method and
    view types rather than rows as returns. Section 17 makes the CLI a
    second caller, which makes these a shared contract rather than an
    API detail.
16. **413 and 429 have response shapes now and mechanisms later.**
    Section 22 owns per-tenant rate limiting; fixing the shape means a
    client written against 0.1 already handles the day it arrives.
17. **One route is added: `GET /v1/sessions/{session_id}`.** A client
    reconnecting with only a session identifier otherwise cannot learn
    the session's status or find its active run. It exposes no
    capability a client with its own records lacked.

## Consequences

- Milestone 5 becomes implementable. Every route now has a request
  shape, a response shape, a status code, a required scope, and an
  error mapping, and the readiness review's six gaps plus the missing
  response bodies are closed.
- Ten hard gates are added, all at Milestone 5, taking that milestone
  from one gate to eleven. It was the largest under-gated milestone in
  the corpus and the one with the most externally visible surface.
- The gate registry gains an eleventh area, `api`. Registry entries go
  from ninety-four to one hundred and four. The milestone map's table
  and census and the harness's gate table are updated; ADR-0027 is
  not, because its arithmetic is a record of what was true when it was
  decided and this ADR is where the new total is stated.
- `SessionStatus` acquires a declaration, which removes one of the two
  types the readiness review found referenced and undeclared.
  `ArtifactMetadata` and `ArtifactRef` remain undeclared and are now
  the only ones.
- The `idempotency_keys` table stays at Milestone 2 and nothing reads
  it until Milestone 5. This is stated explicitly so that an
  implementer moves neither the DDL forward nor the endpoint back.
- A client library can be written from this document alone. That was
  the test applied while writing it: every question a client author
  would have to ask a server author is answered, including the four
  things a client must not infer from the stream.
- Section 16 is unedited. Where the two disagree — the session status
  case — the disagreement is recorded as an open question rather than
  resolved by changing the plan, per the conversion rules.

## Alternatives considered

- **Inventing a fresh, small error vocabulary designed for clients**:
  rejected. It would require a mapping from the internal taxonomy that
  somebody maintains by hand, and Section 16 had already chosen the
  convention in its only example. A second vocabulary is a second
  thing that drifts.
- **Returning 403 for a resource in another tenant**: rejected. It is
  the more informative answer and that is exactly the problem — it
  confirms existence, which makes identifier enumeration a working
  attack.
- **A scope hierarchy where `run.write` implies `run.read`**:
  rejected. It is more convenient and it needs a grammar, and a
  grammar has an evaluation order that can be subtly wrong in the
  direction of granting access. Exact match cannot be subtly wrong.
- **Lowercase `SessionStatus` to match Section 16's sample**:
  rejected in favour of consistency with `RunStatus` and the DDL's
  guarded updates, and recorded as an open question rather than
  decided silently. Two cases on one wire is what a client library
  encodes as two enums and a comment.
- **Treating the HTTP and tool idempotency keys as one mechanism**:
  rejected on evidence. The milestone map schedules the tool key as a
  Milestone 1 port on `ToolInvocationRepository`, a tool-call concern,
  while the `idempotency_keys` table is Milestone 2 and carries
  `request_hash` for an HTTP body. Unifying them would put a table at
  a milestone whose DDL does not exist.
- **Queueing a second message rather than rejecting it**: rejected,
  and not by this document — ADR-0004's partial unique index makes
  queueing impossible at the database level as specified, which
  Section 27.5's resolution already records.
- **Stamping a synthetic `id` on transient frames for uniformity**:
  rejected, and it is the most tempting wrong decision in the
  document. Uniform framing costs correct reconnects.
- **Reading the persisted prefix before subscribing**: rejected. It is
  the obvious order and it drops every event committed between the
  read and the subscribe. The window is small and it is not empty.
- **Persisting token deltas so replay could include them**: rejected
  by ADR-0010 already, for write volume against data that is
  reconstructible from `assistant.message.completed`. This document
  adds the client-side consequence: a UI that renders deltas must
  reconcile against the completed message.
- **A per-run SSE id counter**: rejected. ADR-0010 fixed the id as the
  session sequence, and a per-run counter needs a second allocation
  column and a second uniqueness constraint to protect it, duplicating
  machinery the event log spec built once.
- **Serving artifacts inline when the media type is safe**: rejected.
  The safe list is a thing that grows by argument, and the argument
  happens in a pull request rather than a threat model.
- **Offset pagination**: rejected. Over a table still being written it
  both skips and repeats rows, and an approvals list is such a table.
- **Deferring the application service signatures to the
  implementation**: rejected. Section 17 makes the CLI a second
  caller, and two callers discovering a signature independently is how
  the CLI ends up importing a web framework.
- **Specifying rate limiting now**: rejected. Its numbers should come
  from operational data that does not exist, and Section 22 already
  owns it. Fixing only the response shape gets the client-side benefit
  without inventing a mechanism blind.
