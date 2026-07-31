---
title: Events
---

# Events

The canonical event vocabulary, append rules, projection contracts, and
persistence design are defined by the
[engineering plan](plan/engineering-plan.md#68-event-envelope) and the
[event-log specification](plan/event-log-and-persistence.md).

Milestone 0 implements no application events and no event store. It establishes
only the linear Alembic graph and its pinned revision, so later event schemas
cannot begin with a branched or ambiguous migration history. Event types first
become executable with the in-memory vertical slice in Milestone 1 and become
durable in PostgreSQL in Milestone 2.
