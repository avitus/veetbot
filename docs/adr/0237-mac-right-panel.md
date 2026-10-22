# ADR-0237: A right panel for activity, artifacts and thread context on the Mac

- Status: Accepted (owner, 2026-10-21: approved the MagicPath designs "Mac — Chat · Right panel" and "Mac — Email · Right panel")
- Date: 2026-10-21
- Related: ADR-0049, ADR-0122, ADR-0227; Milestone 26 client modes
- Detailed design: `docs/apple-client.md` ("Agent activity and artifacts", "Milestone 26 client modes")

## Context

On the Mac, Chat showed tool calls inline: each run of calls was one sentence
that expanded into a monospaced ledger inside the transcript, the working notes
sat above the first message, and a file opened a viewer sheet. Email kept the
Teach Veetbot card below the conversation and opened People in a sheet to show
who wrote. The secondary detail crowded what the owner reads, and a sheet
covered the conversation it described.

The owner asked for a right-hand panel in both modes and approved its contents
and its MagicPath designs (merry-moon-8243 and clever-village-5022) on
2026-10-21, including moving Teach Veetbot into Email's panel.

There is no call that lists a session's artifacts, and the Email thread does
not carry the draft run's calls; the run's own event stream does.

## Decision

1. **Mac only.** The panel exists under `#if os(macOS)`. iPhone and iPad keep
   inline work strips, the working-state disclosure, the artifact sheet, the
   Teach Veetbot card and the People sheet unchanged.
2. **One owner.** The root window toolbar adds one panel button, contributed
   only by the active mode, acting on that mode's panel (⌥⌘I). The panel also
   has its own hide button. Each mode's panel is a column on `paperDeep` behind
   a hairline the owner can drag between 260 and 440 points.
3. **Kept on this Mac.** Each mode's width and the owner's open-or-closed
   choice are stored in user defaults (`veetbot.inspector.<mode>.width` and
   `.open`), like the Email inbox width, and never sent to the server.
4. **Narrow windows.** A window narrower than 1,100 points starts with Chat's
   panel collapsed, and narrower than 1,300 points Email's (three columns before
   the panel). Crossing the threshold collapses or restores the panel without
   recording a choice; the toolbar button always opens it.
5. **Chat: Activity and Artifacts.** Activity holds the run's status, the
   working notes, and every tool call the conversation holds, grouped under the
   owner's request that started them and numbered across the conversation,
   each opening into its arguments, result and risk. Artifacts lists every file
   or image read from the conversation's tool results and messages, each once,
   with an embedded preview and Download. The transcript keeps its one-line
   summaries; choosing one opens the panel at its group. Approvals and
   clarifying questions stay in the dock above the composer.
6. **Email: Context and Activity.** Context holds the sender's People match (a
   search hint, as People's own lookup reads a correspondent), why the thread
   is in the priority list (its level and reason), Teach Veetbot, the
   attachments (listed, not opened) and the draft's state with a way to request
   one. Activity replays the draft run's events into a reducer of its own and
   shows its calls. The thread's "View in People" opens the sender's card.

## Consequences

- Chat's transcript on the Mac no longer expands calls in place; UI journeys
  that expanded a work strip now find its calls in the panel.
- Email's Mac thread no longer holds Teach Veetbot; journeys that teach open
  the panel first when the window is narrow.
- The artifact list is only as complete as what the conversation has loaded; a
  server list of a session's artifacts would replace the derivation.
- The draft run's replay opens one run event stream while the panel shows a
  thread with a draft.

## Owner-requested presentation refinement (2026-10-22)

The owner requested that Mac tool calls appear only in the right panel after
opening Morning X Trends Summary made the thread flicker while its calls
replayed. This supersedes decision 5's transcript summaries: Mac Chat renders
messages without tool rows, and tool counts do not trigger transcript scrolling.
The toolbar remains the way to open Activity, even in a narrow window. Pending
approvals and questions remain in the dock. On every Apple client, a terminal
run's activity is reconstructed separately and published only when its replay
finishes; its saved answer stays visible meanwhile. Live runs keep streaming.
No server contract, policy, or milestone changes.
