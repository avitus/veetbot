# ADR-0010: Live event transport

- Status: Accepted
- Date: 2026-07-17
- Related: Sections 6.8, 14, 16; ADR-0004 (Postgres run queue)

## Context

The API and worker run as separate processes, and PostgreSQL is the only durable
channel between them. Token and reasoning deltas are intentionally **not**
persisted (Section 6.8), so there is no defined path for live deltas to reach an
SSE client. Separately, a `FOR UPDATE SKIP LOCKED` claim implies polling, which
adds latency before a run even starts - visible in interactive chat - and a long
asynchronous run can head-of-line-block an interactive turn. The project's scope
forbids adding Redis or Celery for version 0.1.

## Decision

1. Use PostgreSQL **`LISTEN`/`NOTIFY`** as the event-broadcaster transport for
   two purposes:
   - **Worker wakeup**: notify on enqueue so a worker claims immediately instead
     of waiting for the next poll. Keep a bounded poll as a fallback so a missed
     notification cannot strand a run.
   - **Live delivery** of transient events (token deltas, reasoning deltas,
     provisional usage) from the worker to the API for SSE fan-out.
2. **Persisted events remain the replayable source of truth.** The SSE `id` is
   the per-session sequence; `Last-Event-ID` replays the persisted prefix, then
   live streaming resumes. Transient events are never replayed.
3. Add a `priority` column to `runs` and order the claim query by
   `(priority, created_at)` (still `FOR UPDATE SKIP LOCKED`) so asynchronous jobs
   cannot starve interactive turns. Priority is set from the run's origin.
4. Do not add Redis or Celery for 0.1.

## Consequences

- Low claim latency and live streaming with no new infrastructure.
- `NOTIFY` is best-effort; that is acceptable because it carries only transient
  data, while the durable path is the event log.
- The broadcaster stays behind a port, so if Postgres pub/sub proves inadequate
  at scale, the adapter can be swapped (e.g. for a dedicated bus) without
  touching the application layer.

## Alternatives considered

- **Poll-only**: rejected; adds avoidable interactive latency.
- **Redis / Celery / dedicated broker**: rejected for 0.1 on the scope rule;
  reconsider only if Postgres becomes demonstrably inadequate.
- **Persist token deltas**: rejected; high write volume for data that is
  superseded by the next persisted event.
