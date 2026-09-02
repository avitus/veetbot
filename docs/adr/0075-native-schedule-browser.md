# ADR-0075: Native schedule browser over the existing control plane

- Status: Proposed
- Date: 2026-08-29
- Related: Sections 16, 21, and 29 of the engineering plan; ADR-0049,
  ADR-0050, ADR-0059, ADR-0070, ADR-0073
- Detailed design: `docs/plan/scheduling.md`

## Context

Veetbot can create, persist, execute, pause, complete, and cancel schedules,
but the native Apple client has no way to show the schedules that already
belong to its principal. The control plane is not missing: Milestone 11
delivered a principal-scoped, cursor-paginated `GET /v1/schedules` index and a
`GET /v1/schedules/{schedule_id}` point read, both protected by
`schedule.read` and mounted only when the schedule API is enabled. The gap is
the same one the memory browser made visible in the client: authoritative
cloud state exists but is not inspectable from the owner's normal surface.

Unlike the memory-browser work, this change needs no new HTTP route, scope,
feature flag, projection, or store predicate. ADR-0049 already authorizes a
native Apple client as a secure transport-only surface and records that a
client extension over completed API capabilities does not change milestone
status. This ADR authorizes that client extension for schedule inspection and
records the display and compatibility contract that the existing API alone
does not decide.

## Proposed decisions

1. **The Apple client gains a read-only schedule browser without reopening a
   milestone.** The browser is an authorized transport-only extension over the
   completed Milestone 11 control plane. It changes neither the verified gate
   ceiling nor Milestone 20's calendar-recurrence acceptance contract.
2. **The existing two GET routes are the only server surface.** The index
   pages summaries and bounded instruction previews. Opening one row performs
   the existing point read before presenting the complete instruction,
   cadence, limits, revision, and lifecycle metadata. The client does not
   reconstruct full content from a preview.
3. **"Current schedules" means every schedule record currently retained for
   the authenticated principal.** ACTIVE, PAUSED, COMPLETED, and CANCELLED
   records remain visible and carry text-labeled state. Filtering terminal
   records out by default would make an executed one-time schedule disappear
   precisely when the owner may need to inspect it.
4. **The browser is read-only.** It exposes no create, update, pause, resume,
   cancel, delete, occurrence, or run mutation. Those operations retain their
   existing authority and application-service paths; viewing never requests
   `schedule.write` or `schedule.cancel`.
5. **Server truth is refreshed, not copied into device authority.** Opening the
   browser reloads page one, list paging follows opaque cursors, duplicate IDs
   are ignored, repeated cursors stop pagination, and a detail request reads
   the current revision. The client cache is presentation state only.
6. **Version skew degrades at the index boundary.** A 404 or 405 from the list
   route is presented as schedule browsing unavailable, matching the memory
   browser's older-server behavior. A 404 from a point read remains an
   ordinary not-found because it can mean the schedule disappeared after the
   index loaded.
7. **Wire values remain forward compatible.** Schedule state and cadence kind
   decode as raw strings with typed accessors for known values. An upgraded
   server may therefore display a new state or cadence generically instead of
   making the entire browser undecodable.
8. **Native verification covers transport, paging, presentation, and real
   navigation.** Swift package tests pin the wire and view-model contracts;
   structure tests pin the read-only entry point and resilient states; and the
   existing in-process iOS UI fixture lists a schedule and opens its point-read
   detail. The Apple CI lanes remain the governing verification under
   ADR-0049 decision 9.

## Consequences

- The owner can inspect schedule title, current state, cadence, next firing,
  full instruction, revision, execution bounds, and timestamps from the same
  native navigation surface used for memory.
- Schedule instructions still cross the API only under `schedule.read`; list
  rows retain the existing bounded preview and push notifications remain
  content-free.
- The browser remains useful across calendar kinds and future additive kinds
  without embedding scheduler calculations in Swift. It renders server data;
  it does not predict recurrence.
- Occurrence history and schedule management remain absent from the native
  client. Adding either requires its own authorization because they introduce
  materially different navigation and, for management, write authority.

## Alternatives considered

- **Add a new browser-specific server route:** rejected. The existing list and
  point-read split already exposes exactly the safe summary/detail boundary.
- **Use only list summaries:** rejected. A bounded preview is intentionally not
  the full authorized instruction and cannot support an honest detail view.
- **Show only ACTIVE schedules:** rejected. PAUSED schedules are still current
  configuration, and terminal one-time schedules remain necessary audit and
  troubleshooting context.
- **Add pause, resume, and cancel buttons now:** rejected. The request is to
  view schedules, and read-only inspection can ship without taking new write
  authority onto the device surface.
