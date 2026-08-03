---
title: Events
---

# Events

The canonical event vocabulary, append rules, projection contracts, and
persistence design are defined by the
[engineering plan](plan/engineering-plan.md#68-event-envelope) and the
[event-log specification](plan/event-log-and-persistence.md).

Milestone 2 implements the durable event surface in PostgreSQL. Every append
allocates its per-session sequence by an atomic session-row increment in the
same transaction as the immutable event insert. A state change, its event, and
any checkpoint or lease release committed with it therefore succeed or roll
back together. Concurrent append and injected-rollback cases assert uniqueness
without requiring sequences to be gapless.

Payloads carry a schema version. Pure chained upcasters decode historical
`session.created` version 1 into the current version 2 shape and reject unknown
higher versions. Watermarked session-history projection can catch up or rebuild
from zero deterministically; the trajectory projection does the same for each
run. Both advance in bounded batches, carry a builder version, and use
monotonic conflict guards so a concurrent older apply cannot regress a
watermark or materialized trajectory.

Full and delta checkpoints refer to projected conversation history through an
event sequence instead of copying portable messages into checkpoint JSON. The
worker resumes the latest valid chain, and when non-terminal checkpoints are
deleted it seeds the same conversation from the event projection. Full
snapshots occur at version 1 and every eight versions thereafter, with deltas
between them and explicit removal records for disappearing state keys. Crash,
checkpoint-deletion, projection-rebuild, stale-fence, and two-worker race cases
exercise these properties against PostgreSQL 16.
