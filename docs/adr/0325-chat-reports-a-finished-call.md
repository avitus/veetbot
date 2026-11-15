# ADR-0325: Chat reports a finished call in the conversation that placed it

- Status: Accepted — the owner accepted it and asked for its implementation on 2026-11-14
- Date: 2026-11-14
- Amends: ADR-0097 (Bland calling and public receptionist)
- Detailed design: `docs/plan/bland-calling.md`
- Scope: calling; no milestone or gate moves

## Context

On 2026-11-13 the owner asked Chat to place several calls. Each time Chat
answered that the call was placed, and nothing afterwards said whether anyone
answered or what they said. The owner asked for post-call hooks in Bland.

Those hooks already worked. Every call that day carried the per-call callback,
Bland delivered it within about a second of the call ending, the listener
answered 202, and the call worker stored the outcome, transcript and summary.
What was missing sits on this side:

1. **Dispatch is not the outcome.** `start_call` returns `accepted` while the
   phone is still ringing. The run that placed the call ends there.
2. **The only notice is a push.** A finished call enqueues `call_finished`,
   whose deep link opens a result sheet. Nothing is written to the
   conversation, so a chat that placed a call never mentions it again.
3. **A run cannot wait.** A call lasts up to fifteen minutes. Holding the
   placing run open that long would tie up the chat and lose the result if
   the run were interrupted.

## Decision

**When a call's result is recorded, the platform queues one read-only run in
the conversation that placed it, and that run tells the owner the outcome.**

- Each dispatch is marked at creation as wanting a report. A dispatch without
  the marker is never reported, so deploying this posts nothing into older
  conversations.
- The maintenance pass, in the application role, checks marked dispatches
  beside the call-plan sweep. It reads the closed dispatch status and whether
  the content has arrived. It never reads a summary or transcript.
- A report is queued when the status is final and the call was placed by the
  owner's own message in an open, top-level chat. Scheduled occurrences and
  call plans are left out: an occurrence reads its own outcome and a plan
  reports once (ADR-0221). A chat bound to a website profile is left out too,
  because its plan must hold browser tools this run may not have.
- The pass waits while the conversation has an active run. It never delivers
  the note as the answer to an open question. It waits up to three minutes
  for a summary the provider has not sent yet. Thirty minutes after the call
  ended it gives up and the push remains the only notice.
- The queued run holds exactly `mcp.bland_read.use`. It can read the call
  through the governed reader and cannot place a call, send mail, or use any
  other scoped tool. Its seed is a platform note with the actor type
  `calling`. The note states the time the call was placed and its closed
  outcome, names the call, asks for a report, and says the record is
  untrusted. It names the call by a reference: the call identifier spelled in
  the letters `g` to `v`, which `get_call` resolves. Two calls placed in the
  same minute with the same outcome therefore get different notes. The note
  contains no call content, no call identifier in its usual form, and no
  digit run that ADR-0213's number check could read as dialable.
- The note is context only, like the scheduler's seed. Conversation reads and
  the event stream leave it out, so the owner sees the report and not a
  message they did not write. Memory formation already ignores user messages
  whose actor is not the owner.
- The report run takes no follow-ups. With ADR-0324 steering on, a message
  sent while it works is refused with `active_run_exists` and is never handed
  to a run that cannot act on it.
- The dispatch records that its report was queued, skipped or expired, in the
  same transaction that queues the run, so a call is reported at most once.
- `AGENT_CALL_RESULT_FOLLOW_UP_ENABLED` defaults to `1`; `0` leaves results to
  the push alone.

The `call_finished` push and its result sheet are unchanged.

## Consequences

- The report is call-record content, labelled untrusted. After it, ADR-0213
  no longer authorizes a call from that conversation without a card. That is
  the existing rule for any conversation that has read a call record.
- The report run's scopes differ from the owner's, so the conversation's
  context plan is rebuilt for it and again for the owner's next message. That
  costs two uncached prefixes per reported call.
- The report arrives after the next maintenance pass, the provider's summary
  and any active run, so it trails the end of the call by a minute or more.
- Calls placed through Telegram or WhatsApp, from a scheduled conversation,
  or from a website chat still rely on the push.
- Whether an open Apple chat shows the new run without being reopened has not
  been verified.

## Alternatives rejected

- **Hold the placing run open until the call ends.** Simpler, but it blocks
  the chat for the length of the call and loses the result on interruption.
- **Give the report run the owner's scopes.** It would avoid rebuilding the
  context plan, but an unattended run that reads third-party speech should
  hold no authority to act on it.
- **Write the summary into the conversation without a model run.** It would
  place untrusted text in the transcript outside the governed reader and its
  trust label.
