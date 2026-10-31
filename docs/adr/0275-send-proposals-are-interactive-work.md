# ADR-0275: Send proposals are interactive work and read the mailbox once

- Status: Proposed — the owner asked on 2026-10-31 for a quicker, clearer
  email reply flow and chose these repairs first, ahead of a one-press send.
- Amends: ADR-0095's lane note for Archive; `email-experience.md` send flow.
- Scope: Milestone 26 behavior already verified; no gate, milestone or policy
  ceiling moves. The exact-send approval is unchanged.

## Context

Replying to an email felt slow and the controls looked dead. Four causes sat
in the send path, none of them required by the plan:

1. The send task was queued in the asynchronous class (priority 10) with the
   refreshes and automatic drafts, each of which may run for two minutes. The
   archive gesture already skips that line (ADR-0095); the send did not, so
   Review & Send could wait minutes behind background work.
2. Before showing the review, the task checked the mailbox identity and read
   the whole Gmail thread, then did both again after Final Send. The
   detailed design places the one fresh read after Final Send
   (`email-experience.md`, send flow step 3); the first read was extra.
3. The Apple client polled the proposal for sixty seconds and then stopped
   silently, so a slow proposal left the button looking inert. Review & Send
   was also disabled during every autosave.
4. A send the server ended because new mail arrived closed the review without
   a word, and the stale-draft notice described a held revision rather than
   who had written what. Several refusals reached the owner in the server's
   internal vocabulary.

## Decision

- A send task joins the interactive class (priority 0) at admission, like the
  archive gesture. Refresh and draft generation stay asynchronous.
- The send task performs the identity check and the complete fresh thread read
  only after the approval is resolved, immediately before the serialized claim.
  Before approval it freezes the exact draft value and requests the approval
  without touching the mailbox. A failed freshness read still defers the send
  and preserves the draft; the gates for exact approval, atomic claim and
  uncertainty keep their meaning with one read instead of two.
- Owner-facing refusals and run outcomes in the send and edit paths are written
  in the owner's terms: new mail arrived, the reply changed, Gmail could not be
  checked, Veetbot is still working on this thread.
- The Apple client keeps Review & Send available while an autosave is in
  flight and lets the proposal wait for that save; it reports a proposal that
  outlasts its local wait and opens the review from a later refresh of the
  same durable action; it names and quotes the newest message in the stale
  notice; and it says beside the draft when a send ended without dispatch.

## Consequences

The proposal reaches the review in the time it takes to freeze the draft and
record the approval, not the time of a queued refresh plus a full Gmail read.
Sending still performs one governed read and the identity check before the
claim, so the freshness guarantee and the stated residual race are unchanged.
The Postgres send journey now expects the interactive lane to run the task.

A one-press send with Undo, which would replace the review sheet, is a
separate decision on the exact-send approval and is not part of this ADR.
