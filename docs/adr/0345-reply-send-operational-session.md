# ADR-0345: One-press reply sends use an operational session

- Status: Accepted
- Date: 2026-11-23
- Amends ADR-0103, ADR-0104 and ADR-0279 for the one-press Send action.

A production reply took 42 seconds. The API and worker both prepared a Chat tool
catalog for a first manual reply. The ten-second Undo deadline passed before
the API returned. The worker made zero model calls.

Run the exact approved send in a dedicated operational email session. The
durable task binds the mailbox, thread and draft, and the draft retains its
conversation binding. Discuss in Chat can open the regular conversation on
demand. The operational session records the thread ID so a failed-send alert
can open the reply without adding a Chat history entry. The API performs no tool discovery. The worker opens the originating
mailbox’s read and send tools. This owner action reserves zero model dollars
and does not need the rolling automatic-email spending calculation. Admission
looks up the draft’s pending run directly to handle an earlier press or proposal.

After admission checks and checkpoint creation, set one Undo deadline for the
queue, frozen consent and draft in the same transaction. Return the queued
draft from that admission. A replay reads current state. The existing worker
claim, account and thread checks, attachment validation, approval, erasure
and uncertain-delivery rules still govern dispatch. Previously queued sends
retain their recorded session and deadline.
