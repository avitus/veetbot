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

## Owner-approved clean reader (2026-10-22)

The owner asked for the Mac thread to be a clean reading surface and approved
the MagicPath design "Mac — Email · Clean reader" (cozy-evening-6871). This
extends decision 6: on the Mac everything that describes a thread moves from
the thread into Context, which now leads with a spam flag (Report spam, Not
spam), then Veetbot's summary (the gist and the state line; a thread not yet
assessed says "Assessment pending" once), why it is here, the sender's card
with the chosen message's From, To, Cc and date and, for an eligible bulk
sender, Unsubscribe, then Teach Veetbot, the attachments, the draft, and a
Thread card (the account, the Gmail state, why archiving is unavailable, and
one Show original switch for every message in the thread). The thread keeps
Archive in Gmail and the thread menu, the subject, who wrote and when, each
message's text, the incomplete-message warning, Gmail's answer to an archive
just asked for, the reply and the Discuss in Chat bar. A sender's name opens
their card and that message's envelope; one line per message counts its
attachments and opens their list. These items are visible only while the
panel is open, as Teach Veetbot already was; journeys open it first. iPhone
and iPad keep every item in the thread. No server contract, policy, or
milestone changes.

Because the panel now matters more, a narrow window no longer squeezes the
thread to its minimum when the owner opens Email's panel there (below the
1,300 points of decision 4): the mailbox sidebar and the panel take turns.
Opening the panel folds the sidebar, whose mode switch, search and collections
move above the inbox as when the owner hides it; closing the panel or widening
the window brings it back. Showing the sidebar with the window's sidebar button
folds the panel instead. Neither is recorded as the owner's choice, and the
fold leaves the window's own sidebar state untouched, since macOS restores that
state at the next launch. A sender too long to share a line with the date puts
the date below it.

## Owner-requested resizable Email columns (2026-10-23)

The owner asked for all four Mac Email columns to be resizable with their
widths remembered on each device, and for the shipped Email surface to match
the MagicPath design "Mac — Email · Clean reader" (cozy-evening-6871), which
was refined first: its sidebar, inbox and panel each gained the same draggable
hairline, a remembered width and a double-click reset, and the reader a
420-point minimum. The Mac follows it: the sidebar (200 to 340, starting at
260) joins the inbox (now 260 to 480, starting at 300) and the panel (260 to
440) as an owner-sized column kept in this Mac's defaults, and the reader takes
what they leave. A drag of the sidebar or the panel stops before the reader
falls below 420 points; a narrow window may still squeeze it to 250. The Mac
also takes the design's chrome: the window toolbar's "Inbox · N need you"
title and appearance toggle replace the Email heading, the sidebar leads with
the mode switch and Search mail and groups Mail and More, and the reader takes
the design's padding and bottom bar. The owner chose to keep every control the
design does not draw (the account picker, Find other mail, New chat, pending
chat attention, Email learning, the shortcuts row, the mailbox status and
Refine with Veetbot), restyled into the design's sections rather than removed.
iPhone and iPad are unchanged. The same pass carried the Cockpit · Chat ·
Email switch of "Mac — Chat · Right panel" into the clean reader design and
into each Mac left pane (ADR-0240's build step 9). No server contract, policy,
or milestone changes.
