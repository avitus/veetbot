# ADR-0038: Milestone 2 durable-runtime seam decisions

- Status: Accepted
- Date: 2026-08-03
- Related: Milestone 2, ADR-0003, ADR-0004, ADR-0021, ADR-0023, ADR-0024, ADR-0031
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Context

Milestone 2 adds PostgreSQL, separate worker and maintenance roles, restartable
checkpoints, event-backed projections, and lease fencing for ordinary process
roles. The single-process implementation remains behind the same ports for
deterministic evaluation. The normative plan fixes the guarantees, while several
implementation details still have to be chosen consistently across the
composition root, repositories, runtime, migrations, and tests.

The project owner asked the implementation to continue autonomously and to
record decisions that require review. This ADR collects those decisions. It
does not authorize Milestone 3 provider behavior or weaken a Milestone 2 gate.

## Proposed decision

1. Normal CLI and worker processes explicitly select the PostgreSQL composition;
   deterministic evaluation explicitly retains the in-memory composition. Both
   use one `UnitOfWorkFactory` port, so application and runtime code own short
   transaction boundaries without importing SQLAlchemy.
2. The built-in agent has a fixed UUID and a content-addressed version in the
   PostgreSQL tier. The version is `1.0.0+h<sha12>`, where the digest covers the
   complete behavioral specification except identity and version. Randomly
   creating the agent identity in each process would make sessions submitted by
   the CLI unreadable by a separately composed worker; reusing one literal
   version for different limits, tools, or policy profiles would make valid
   configurations conflict. The memory tier retains injected identifiers for
   deterministic tests.
3. Tenant and principal identifiers remain text in SQL. The current domain,
   evaluation identities, and production-validation rules all define them as
   strings; converting only the persistence pseudocode to UUID would create a
   lossy boundary.
4. The schema includes `session_history_items` and `trajectory_projection`
   materialized-state tables in addition to the specified projection watermark,
   plus `runs.final_message`, `runs.seed_event_sequence`, and
   `tool_invocations.result_item`. These are the smallest persisted values that
   let the shared CLI read a result, seed history at the run's pinned event, and
   return the exact same tool result on deduplication. Events remain the source
   of truth.
5. Checkpoint conversation state is stored as a session-history reference
   through an event sequence, never as an inline copy of portable conversation
   items. Full checkpoints occur at versions 1, 9, 17, and so on, and at
   terminal or suspension boundaries; intervening records are deltas. Deleting every
   non-terminal checkpoint therefore falls back to the same event projection.
6. Queue dispatch treats the committed PostgreSQL row as the durable signal and
   workers poll at the configured 250 ms interval. PostgreSQL notifications are
   used for event consumers, but queue correctness does not depend on an
   ephemeral notification being observed.
7. Every worker mutation accepts the claimed lease and uses the run's owner and
   epoch as a write fence. Heartbeats run independently of provider and tool
   calls; loss cancels the execution with `FENCED`, after which the old worker
   performs no database write. Terminal checkpoint, transition, event, and
   lease release share one transaction.
8. Tool invocations snapshot source, idempotency class, origin trust, effect
   watermark, terminal outcome, and the exact model-facing result. Recovery is
   a total function over persisted state: read-only and idempotent calls rerun,
   conditional calls with a watermark replay the same key, and a watermarked
   non-idempotent call becomes `UNCERTAIN`.
9. The usage and pricing tables are created in Milestone 2 because the
   engineering plan's Milestone 2 schema requirement is normative. The fake
   provider records attempts through that port now; real provider resolution,
   pricing policy, and normalized streaming remain Milestone 3 work as required
   by the model-gateway build order.
10. The in-memory unit of work gains checkpoint, idempotency, usage, and history
    implementations so the shared application/runtime services have one shape.
    Its declared capability gaps remain unchanged: it supplies neither
    cross-process durability, transactional rollback across repositories,
    crash recovery, nor concurrent cross-process deduplication.
11. Architecture enforcement now permits persistence adapters to construct
    request-scoped repositories internally and permits one port to compose other
    port types. It still rejects adapter construction from runtime/application
    code, SQLAlchemy outside `adapters/persistence/`, and ORM row types crossing
    that package boundary.
12. One shared checkpoint-seeding callable is injected at both normative call
    sites: committed run creation and executor recovery after complete
    checkpoint loss. It always reconstructs the same event prefix pinned by
    `runs.seed_event_sequence`; maintaining two implementations would let those
    paths drift and invalidate checkpoint dispensability.
13. The unit-of-work factory exposes only a process-local `is_open` assertion.
    Model and tool external-I/O boundaries use it to refuse an accidental live
    transaction at runtime, complementing the structural transaction-hygiene
    check without leaking SQLAlchemy sessions through a port.
14. Session-history and trajectory projections use versioned watermarks,
    bounded batches, deterministic rebuilds, and monotonic conflict guards.
    A known event with malformed current-schema content remains a hard
    projection error as specified; it is not silently skipped or quarantined.
15. The synchronous `agent run` CLI waits up to 30 seconds for a worker. If the
    run is still non-terminal, it prints the durable run identifier to stderr
    and exits with the documented platform-unavailable code rather than polling
    forever. The queued run remains available to `agent run get` and a later
    worker; ordinary runs that finish in the interval still print only their
    final message to stdout.

## Consequences

- CLI submission and worker execution can run in different processes while
  sharing immutable agent/session identities and durable events.
- Different built-in agent configurations coexist as immutable versions under
  one stable agent identity, and identical configurations resolve identically.
- No transaction remains open during model streaming or tool execution.
- Checkpoint deletion, worker termination, duplicate submission, and lease
  handover are executable integration cases rather than inferred properties.
- PostgreSQL is required for ordinary CLI continuity. Evaluations remain fast,
  deterministic, and network-denied on the in-memory tier.
- The schema contains additive projection and read-model columns beyond the
  minimum table sketch. Removing them later requires a replacement for their
  demonstrated behavior, not merely deleting the columns.
- Real provider costs are intentionally absent even though their durable schema
  exists; implementing them now would begin Milestone 3 speculatively.

## Alternatives considered

- **Generate an agent UUID at every startup:** rejected because independent API
  and worker compositions would not share the version pinned on a session.
- **Store checkpoint conversations inline:** rejected because checkpoint
  dispensability would then be an assertion rather than an event-log property.
- **Hold one unit of work around a run step:** rejected because the model or tool
  await would hold locks and violate transaction hygiene.
- **Use notification delivery as the queue:** rejected because a missed
  notification cannot be a durable claim source.
- **Defer every model-call table to Milestone 3:** rejected because it contradicts
  the normative Milestone 2 schema requirement. Creating the schema without
  real-provider behavior preserves both milestone boundaries.
- **Let recovery consult the current tool registry:** rejected because a later
  tool version could reclassify a previously authorized non-idempotent effect.
