---
title: Events
---

# Events

The canonical event vocabulary, append rules, projection contracts, and
persistence design are defined by the
[engineering plan](plan/engineering-plan.md#68-event-envelope) and the
[event-log specification](plan/event-log-and-persistence.md).

Milestone 1 implements the `EventRepository` contract and a process-local,
append-only adapter with monotonically increasing per-session sequence numbers.
The run, model, tool, assistant-message, and checkpoint events emitted by the
vertical slice are executable and covered by the deterministic cases. This is
event evidence, not durable event storage: all rows disappear with the process,
and PostgreSQL transactions, payload upcasting, and recovery remain Milestone 2
work.
