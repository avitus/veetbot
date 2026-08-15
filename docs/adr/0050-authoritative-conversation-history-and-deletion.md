# ADR-0050: Authoritative conversation history and deletion

- Status: Proposed
- Date: 2026-08-14
- Related: Section 16 (HTTP API), Section 29 (multi-device shared core),
  ADR-0003 (event log and projections), ADR-0009 (run, turn, and session
  identity), ADR-0011 (multi-device shared core), ADR-0028 (HTTP API and SSE
  semantics), ADR-0034 (the deferred Device seam), ADR-0049 (native Apple
  client)
- User authorization: make `Delete Everywhere` the default conversation-delete
  behavior so every client converges on one conversation-history view

## Context

The shared core is authoritative for sessions, runs, events, approvals, memory,
and artifacts, but the first Apple client had to build its sidebar from a
device-local cache because the public API exposed neither a session index nor a
session-delete operation. Removing a row from that cache hid a conversation on
one device while retaining the authoritative data and showing it on every other
device. That behavior contradicted the user's expectation that conversation
history is one account-level view.

Adding deletion changes both the public API and the persistence lifecycle. A
simple cascade is insufficient: conversation-derived memory and knowledge can
refer to a session indirectly, artifact bytes live outside PostgreSQL, active
runs must not disappear underneath workers, and an idempotent retry must not
turn a successful delete into `404`. A soft delete that retains conversation
content would not satisfy the requested semantics.

This is an explicitly authorized post-Milestone 9 extension. It does not reopen
or renumber the completed Milestone 5 route census, and it does not introduce
the deferred Device, pairing, presence, notification, or device-scoped-tool
model.

## Proposed decision

1. **Make the server's session index authoritative.** Add a principal-scoped,
   keyset-paginated `GET /v1/sessions` route requiring `session.read`. It returns
   `SessionView` values ordered by activity (`updated_at DESC, id DESC`). The
   view includes both `active_run_id` and `last_run_id`, so a client can attach
   to active work or reopen the most recent completed run without owning a
   second index. The title is authoritative too: the server stores a normalized,
   64-character prefix of the first non-empty text block in the first top-level
   user message and, for sessions that predate that write path, derives the same
   value from the immutable first user-message event. A device-local title is
   only an optimistic cache value.
2. **Advance session activity from persisted events.** Appending an event also
   advances the owning session's `updated_at`. The history order is therefore a
   server projection of conversation activity, not a device's selection order.
3. **Add an idempotent authoritative delete.** `DELETE /v1/sessions/{id}`
   requires `session.write` and returns `204 No Content`. A session outside the
   principal's tenant or ownership is indistinguishable from a missing session
   and returns `404`. Repeating a successful delete as the same principal also
   returns `204`.
4. **Refuse deletion while a run is active.** The route returns `409` with
   `invalid_state`, `details.reason = "active_run_exists"`, and the active run
   identifier. The user must stop the run before deleting the conversation;
   deletion never races a worker or silently converts cancellation into data
   loss.
5. **Purge the conversation graph transactionally.** Deletion removes the
   session and its runs, events, checkpoints, projections, approvals, tool
   invocations, artifact metadata, recall traces, memory rejections,
   session-sourced or session-formed memories, consolidation records, and
   knowledge documents sourced from conversation artifacts. A published skill
   revision remains a separately managed durable resource, but its optional
   authoring-run reference is detached before the run is removed.
6. **Retain only a content-free deletion tombstone.** The tombstone records the
   session identifier, tenant, principal, and deletion time. It contains no
   title, messages, events, model output, artifact content, or other
   conversation data. It provides idempotency and prevents a stale local cache
   from making a successful delete look like an unexplained `404`.
7. **Delete external bytes through a durable outbox.** Artifact references are
   copied into a deletion queue in the same transaction that removes the
   session. The request attempts byte deletion immediately; maintenance retries
   failures until they succeed, then removes the queued reference. The
   tombstone remains after the queue is empty. This makes database deletion
   atomic without claiming that PostgreSQL can transact with an object store.
8. **Treat every client history store as a mirror.** Clients reconcile the
   complete paginated server index when they connect, return to the foreground,
   and periodically while open. A delete is server-first: only a successful
   authoritative response removes the local row and cached artifact bytes.
   Reconciliation inserts server sessions and prunes local rows absent from the
   authoritative index after a scoped point read confirms their absence. The
   point read prevents a session that moved between activity-sorted keyset pages
   during reconciliation from being mistaken for a deletion.

## Consequences

- Conversation history converges across clients. An already open Apple client
  may display a deleted row until foreground reconciliation or the next
  30-second poll; reconnecting clients converge immediately.
- Conversation titles survive a client reinstall or move to another machine.
  Older conversations recover their titles from server history without an
  event-payload migration.
- The current public API contains sixteen routes: the fourteen-route Milestone 5
  baseline plus the post-Milestone 9 list and delete routes authorized here.
- Deletion is irreversible. The confirmation copy must say that the server and
  synchronized clients lose the conversation and associated data.
- A minimal ownership tombstone is retained permanently for retry semantics.
  It is metadata about deletion, not retained conversation content.
- Previously consolidated information whose recorded source is this session is
  removed. If a memory originated elsewhere and was only reinforced by this
  session, the current single-source memory model cannot subtract that later
  reinforcement; changing that provenance model is outside this decision.
- The deletion outbox can briefly retain artifact locator metadata after the
  public session is gone when external storage is unavailable. It never retains
  artifact bytes, and maintenance makes the window finite.
- Project milestone status does not change. This is separately authorized work
  over the completed shared-core and API capabilities.

## Alternatives considered

- **Keep device-local removal as the default:** rejected because it creates a
  different history on every client and does not delete server data.
- **Offer both choices and default to local-only:** rejected because the safe
  default for the requested account-level history is the operation whose result
  all clients can observe. A local hide feature may be designed separately if a
  concrete use case appears.
- **Soft-delete the session and retain its graph:** rejected because the user
  requested deletion, not archival, and retained content would make the copy
  misleading.
- **Delete without a tombstone:** rejected because a network retry after a
  committed response loss would return `404` and clients could not distinguish
  success from a missing or unauthorized resource.
- **Delete object-store bytes before the database transaction:** rejected
  because a later rollback would leave live database metadata pointing to
  missing content.
- **Reuse `run.cancel` or delete an active run implicitly:** rejected because
  cancellation is cooperative and completion must be observed before the data
  on which a worker operates is removed.
- **Introduce device identities or push invalidation:** deferred because history
  synchronization needs neither the Section 29 Device model nor a new presence
  or notification system.
