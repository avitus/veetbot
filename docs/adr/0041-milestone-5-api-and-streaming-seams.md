# ADR-0041: Milestone 5 API, identity, and streaming seams

- Status: Proposed
- Date: 2026-08-03
- Related: Milestone 5, ADR-0010, ADR-0017, ADR-0024, ADR-0028,
  ADR-0031
- Detailed design: `docs/plan/http-api-and-streaming.md`,
  `docs/plan/runtime-loop.md`, and
  `docs/plan/event-log-and-persistence.md`

## Context

Milestone 5 is the first public transport over the application core. The plan
fixes its fourteen routes, principal-first service signatures, status and error
vocabulary, replay behavior, request identifiers, authentication modes, and
durable cancellation semantics. The implementation still has to choose concrete
environment names, a bounded in-process queue size, and how to reconcile one
literal table-key sentence with the behavioral requirement that HTTP
idempotency keys are scoped to a tenant principal.

These choices are proposed for owner review. They preserve the route table,
authorization order, tenant concealment, durable event log, and hard gates.

## Proposed decisions

1. **The four public services and their views live above every transport.** The
   CLI and FastAPI routes call the same principal-first session, run, approval,
   and artifact protocols. Route handlers translate HTTP only; they never load
   repository rows or accept a tenant identifier.
2. **The static token's principal is configured explicitly.** Token mode
   requires `AUTH_TENANT_ID`, `AUTH_PRINCIPAL_ID`, and `AUTH_SCOPES`, with
   optional comma-separated `AUTH_ROLES`. Development mode ignores those values
   and binds the fixed local principal with the complete platform scope set.
   Token comparison uses a constant-time digest comparison.
3. **HTTP idempotency uses a composite primary key.** The table key is
   `(tenant_id, principal_id, key)`. The detailed design requires that scope and
   concurrent isolation, but also retains an older sentence saying the client
   string alone is the primary key. A global primary key cannot satisfy the
   scoped behavior. The behavioral rule and hard gate take precedence; the
   migration makes the database enforce the same boundary as the lookup.
4. **Canonical request hashing and tool-argument hashing share one domain
   function.** It recursively normalizes the JSON value and emits compact,
   sorted JSON. There is no API-to-tool dependency and no second canonicalizer.
5. **PostgreSQL `LISTEN`/`NOTIFY` is a bounded hint over durable replay.** Each
   session has a channel. A stream subscribes before its first persisted read,
   discards duplicate wakeups by the durable watermark, and polls every 5 seconds
   as the missed-notification fallback. Transient model, reasoning, and
   provisional-usage frames travel over the same adapter and are never written
   to the event log.
6. **Each live subscription holds at most 256 notifications.** On overflow the
   service emits the unnumbered `stream.overflow` frame with its last durable
   sequence and closes. The number is an implementation bound, not a public API
   promise; reconnect and replay remain the recovery contract.
7. **A request identifier is not a trace identifier.** The boundary echoes a
   syntactically valid client request ID or creates UUIDv7 and returns it on
   every response. In the absence of an active tracing span, submission passes
   no `trace_id`; it never copies the client-controlled request identifier into
   durable trace fields.
8. **`conversation.ask_user` is a control tool, not an executable effect.** Its
   validated call persists a suspended invocation and parks the run in
   `WAITING_FOR_USER`. Input completes that same invocation, appends the user
   message and checkpoint atomically, and requeues the same run. Retries are
   keyed by the outstanding question identifier.
9. **The generic artifact service initially opens every artifact the current
   artifact repository can resolve.** At Milestone 5 those are trajectory
   artifacts in the shared local content store. The service exposes a reopenable
   handle rather than bytes or a storage URI, so Milestone 6 can add sandbox
   artifacts without changing the HTTP route.
10. **Body authentication and buffering happen together at the ASGI edge.**
    Starlette translates an exception raised lazily while it parses a chunked
    body into a generic 400 response, so preserving the required 413 response
    needs a bounded pre-read. The boundary authenticates `/v1/` body requests
    before that read, permits at most 16 concurrent one-MiB buffers per process,
    and releases each queued chunk as the application consumes it. Route
    dependencies still apply the exact required scope.
11. **Live model publication cannot hold the provider-consumption path.** The
    best-effort callback has a 100 ms deadline and drops a transient event after
    timeout or transport failure. The durable completed message remains the
    recovery source, and the runtime callback contract explicitly forbids
    unbounded I/O.
12. **Client-controlled persisted strings are bounded at the API boundary.**
    HTTP idempotency keys retain the specified 255-character maximum and an
    approval resolution reason is limited to 4,096 characters. Stored artifact
    names are sanitized for `Content-Disposition` instead of turning permanent
    legacy metadata into a retryable storage outage.

## Consequences

- Static-token deployments cannot start with an implicit all-powerful local
  identity.
- Two tenants can safely use the same client idempotency key, including under
  concurrent submission, and one tenant cannot block another through a global
  primary key.
- SSE reconnect correctness depends only on persisted sequences. Notifications
  improve latency, while polling proves progress when notifications are lost.
- Slow consumers fail explicitly and recover from the event log instead of
  consuming unbounded process memory.
- Unauthenticated requests cannot reserve body buffers, and a worker holds at
  most 16 MiB of buffered request bodies even under concurrent load.
- A delayed live notification cannot stall provider stream consumption; the
  client recovers the eventual durable state after omitted transient frames.
- The public API can evolve its adapters without moving authorization or state
  transitions into route code.

## Alternatives considered

- **Keep the global idempotency primary key and filter by tenant:** rejected
  because identical keys in different tenants would still collide at insert.
- **Use request IDs as trace IDs:** rejected because a client-controlled
  correlation label is not trusted tracing context.
- **Poll without subscribing:** rejected because ADR-0010 requires live deltas
  and a subscribe-before-read handoff.
- **Persist token and reasoning deltas:** rejected because they are transient,
  high-volume data superseded by the completed assistant message.
- **Buffer without a limit:** rejected because a slow client could grow API
  memory without bound.
