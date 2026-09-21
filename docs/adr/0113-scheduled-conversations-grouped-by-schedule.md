# ADR-0113: Scheduled conversations are grouped by schedule, not filed in folders

- Status: Accepted (authorized by the repository owner, 2026-09-21)
- Date: 2026-09-21
- Related: Sections 16 and 29 of the engineering plan; ADR-0049, ADR-0089,
  ADR-0102
- Detailed design: `docs/plan/scheduling.md`, `docs/plan/thread-folders.md`

## Context

Each occurrence of a schedule runs in a session of its own, so a weekday
briefing adds a conversation to the native sidebar every weekday. ADR-0102
keeps folders to chat conversations: the proposal pass never considers a
session carrying `schedule_id`, and the move route refuses one with
`session_not_chat`. Recurring briefings therefore pile up, ungrouped, in the
unfiled history, and each row offers a move menu the server always refuses.

The owner asked for the briefings to be grouped. Two routes were weighed:
letting scheduled sessions into folders, or grouping them by schedule as a
presentation of the scheduler's own data.

## Decisions

1. **Folders stay chat-only.** Milestone 29's move-semantics and
   proposal-eligibility gates are unchanged; no scheduled session is filed,
   proposed, or moved.
2. **The native sidebar groups scheduled sessions by schedule.** The history
   cache records each session's `schedule_id` metadata. A schedule with two or
   more cached sessions renders as one collapsible group in a Scheduled
   section between the folders and the unfiled history, labelled with its
   most recent session's title. A single session stays in the history.
3. **Grouping is client-derived presentation.** It adds no route, scope,
   event, or server state, fetches no schedule record, and does not depend on
   the folder flag.
4. **Expansion is device state.** Groups start collapsed, the owner's toggles
   are remembered on the device, and a new firing never reopens a group.
5. **Scheduled rows lose the move menu.** A control the server always refuses
   is not offered.

## Consequences

- Recurring schedules collapse to one row each, with or without folders.
- A briefing cannot share a folder with related chats. Filing scheduled
  sessions would need a new decision amending ADR-0102.
- The label follows the most recent session's pinned title, so a renamed
  schedule relabels its group on its next firing.
- The evidence is Swift model and view-model tests under ADR-0049; no gate is
  registered and the historical milestone gate counts do not change.

## Alternatives considered

- **Admit scheduled sessions to folders, by hand or by a folder named on the
  schedule:** rejected because it weakens two Milestone 29 acceptance gates
  and couples the materializer to folder membership, rename, and deletion.
- **A server-side grouped index:** rejected because the session index already
  carries `schedule_id`, so the grouping needs no new server surface.
- **Label groups from the schedule record:** rejected because it adds a point
  read per group and fails for schedules removed by terminal retention
  (ADR-0089) while their sessions remain.
