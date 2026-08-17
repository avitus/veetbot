# ADR-0051: Durable session transcripts for historical clients

- Status: Proposed
- Date: 2026-08-17
- Related: Section 16 (HTTP API), Section 27 (run, turn, and session identity),
  Section 29 (multi-device shared core), ADR-0006 (no private reasoning
  storage), ADR-0010 (live event transport), ADR-0011 (multi-device shared
  core), ADR-0028 (HTTP API and SSE semantics), ADR-0049 (native Apple client),
  ADR-0050 (authoritative conversation history and deletion)
- User authorization: restoring an historical chat after relaunch must show the
  whole conversation rather than only its last question

## Context

ADR-0050 made the session index authoritative and gave each row a
`last_run_id`. The Apple client used that identifier exactly as designed: on
selection it cleared its reducer, fetched the last run, and replayed that run's
events. That restores active state and the final turn, but it cannot restore the
earlier turns in the same session. Relaunch therefore reduced a multi-turn chat
to its last question and answer even though every earlier message remained in
the authoritative event log.

Persisting the rendered transcript only on the device would hide the symptom on
one installation and violate the shared-core boundary. Listing runs would expose
execution identity and still require every client to merge several event streams
into one conversation. The server already owns a safer and narrower source: the
durable user-message and completed-assistant-message events in session order.

## Proposed decision

1. **Add a principal-scoped session transcript route.**
   `GET /v1/sessions/{session_id}/messages?limit=100&cursor=<opaque>` requires
   `session.read` and returns the standard `{"items", "next_cursor"}` page.
   Missing, cross-tenant, and differently owned sessions return `404`.
2. **Return messages, not the internal event log.** Each item contains the
   durable session `sequence`, a `role` of `user` or `assistant`, and public
   content blocks. Only `user.message.created` and
   `assistant.message.completed` contribute items. Tool lifecycle events,
   system state, provider continuation data, reasoning, and transient deltas do
   not cross this boundary.
3. **Page by immutable session sequence.** The cursor is opaque and represents
   the last returned durable message sequence. Results are oldest first, the
   default limit is 100, and the server clamps it to 200. The append-only order
   makes retries and concurrent appends stable without a device-local offset.
4. **Restore before attaching to the latest run.** On selection the Apple client
   reads every transcript page, seeds the reducer in sequence order, fetches the
   active or last run, and then opens its existing run stream. Transcript
   sequences enter the reducer's persisted-event deduplication set, so replay of
   the latest run cannot duplicate its durable user or assistant message.
5. **Retry the read without changing identity.** The client retries transient
   connection and server failures for each safe `GET`; it rejects a repeated
   page cursor and abandons stale results when selection changes.
6. **Do not change local-history authority.** The device cache continues to hold
   only session index metadata. Message content is fetched from the core on
   selection and is not promoted into an offline-authoritative transcript.

## Consequences

- A relaunched or newly installed Apple client renders every durable user and
  completed-assistant message in a selected session, then resumes live state
  from the latest run.
- Historical tool cards are not reconstructed by this route. They remain live
  run activity; exposing a durable tool-activity transcript would require a
  separately reviewed public shape.
- The current public API contains seventeen routes: the fourteen-route
  Milestone 5 baseline, the two ADR-0050 history routes, and this transcript
  route. The completed milestone's acceptance criteria and gate census do not
  change.
- The endpoint can scan internal events between returned messages, but only its
  two closed message event types are serialized. A future read model may
  optimize that scan without changing the wire contract.
- No schema, migration, Device identity, run-list route, session-scoped SSE
  stream, or local credential behavior changes.

## Alternatives considered

- **Persist the full transcript in the Apple history cache:** rejected because
  a second device or fresh installation would still diverge from the server.
- **List every run and replay each run stream:** rejected because it exposes an
  execution index to solve a conversation-read problem and makes ordering a
  client responsibility.
- **Add a session-scoped SSE stream:** rejected because historical restoration
  needs a bounded JSON read, not a second live transport, and raw session events
  include internal event classes that are not a public transcript.
- **Return all persisted session events as JSON:** rejected because an open
  event vocabulary would leak internal payload growth across the API boundary.
- **Embed messages in `SessionView`:** rejected because the same view is used by
  the session index, where an unbounded transcript would destroy pagination.
