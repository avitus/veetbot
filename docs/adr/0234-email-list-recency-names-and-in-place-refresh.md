# ADR-0234: Email list in recency order, named correspondents and refresh in place

- Status: Accepted — owner requested on 2026-10-21
- Date: 2026-10-21
- Related: ADR-0092, ADR-0095, ADR-0231
- Detailed design: `docs/plan/email-experience.md`

## Context

The owner found the Email list hard to read. Each row led with raw sender
addresses even when the mail carried a display name, an amber line repeated
the state with its full reason, and the order followed the server's ranking,
so a conversation with new mail could sit under older ones. Leaving Email for
Chat and coming back cleared every row and showed the initial loading
indicator again, because a new foreground visit reloaded from nothing.

Production thread projections carry senders as RFC 5322 mailboxes
(`Name <address>`, sometimes with a quoted name), so the name is available on
the client without a server change.

## Decision

The list is ordered by each conversation's latest activity, newest first,
whatever ranking selected its rows. Ties keep the server's order. A foreground
refresh keeps the rows already on screen, places arrivals by recency, and moves
a conversation with new mail up; the insertion and move animate unless Reduce
Motion is on. This replaces the earlier rule that survivors keep their relative
order and arrivals append after them. Selection, pagination and the five-row
initial page are unchanged: the ranking still decides which conversations
appear, and recency decides where.

A row names its correspondents by display name, falling back to the address
only when the sender gave no name, and shows each person once. Its state is a
short chip; the reason for inclusion is read in the conversation. The recency
label reads the time today, Yesterday, the weekday within the week, then the
date. Feedback targets and the people lookup keep the full address.

Returning to Email starts a new foreground activation as before, but when rows
are already loaded the first read of that activation merges into them instead
of clearing them, and no initial loading indicator restarts. A replaced
connection still clears everything. Chat and Email cross-fade on a mode switch.

## Consequences

Client-only: no API, storage, scope or policy change, and no milestone or gate
moves. `gate.email.experience_foreground_admission` and the archive journeys
keep their observed behavior; the order they observe is now recency with the
server's order as the tie-break.
