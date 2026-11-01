# ADR-0279: One-press Send with Undo

- Status: Accepted — the owner authorized this change to the exact-send
  approval on 2026-10-31, after the ADR-0278 repairs.
- Amends: `email-experience.md` send flow (steps 1–2); ADR-0095's gesture
  consent is reused for sending.
- Scope: Milestone 26 behavior. Gate 20 (exact send approval), gate 21
  (atomic send claim) and gate 22 (send uncertainty) keep their meaning; no
  gate, milestone or policy ceiling moves. Gmail writes keep
  `REQUIRE_APPROVAL`.

## Context

Sending a reply took two presses and a modal: Review & Send proposed the
exact draft, a sheet repeated the envelope and body the owner had just
written, and Send on that sheet resolved the approval. The sheet existed only
because the design made its Send button the approval. The owner asked for a
flow that is quicker and leaves a way back.

The Archive gesture (ADR-0095) already shows how one clearly labelled press
can be the owner's consent to exactly one Gmail write while the ordinary
approval pipeline stays in force: the gesture is persisted with the durable
task, revalidated at every undispatched attempt and at the provider dispatch
watermark, and a narrow consumer resolves the pending approval once, only
when the frozen invocation matches the consent.

## Decision

1. **Send is one press.** The Email client sends the draft revision and the
   thread revision it displayed to `POST /v1/email/drafts/{id}/send`. The
   server derives the account, the provider thread, the send tool and the
   exact `send_message` arguments from that immutable revision; the client
   cannot supply recipients, subject, body or account. The press is recorded
   as an authenticated `email.send.requested` gesture whose payload carries
   identities and a digest, never mail content. A thread that moved on, a
   stale draft, a draft in another send state, or a revision the client did
   not display refuses the press with a plain sentence.
2. **Undo is a real window.** The send task is admitted in the interactive
   class (ADR-0278) with `scheduled_for` set to now plus
   `AGENT_EMAIL_SEND_UNDO_SECONDS` (10 by default, 0–60). The draft shows
   `send_undo_until`. `POST /v1/email/drafts/{id}/send-undo` cancels the
   parked task and returns the draft to editing with its text intact; once a
   worker has claimed the task, Undo is refused and the send proceeds. The
   client shows "Sending to … · Undo" with a countdown, then Sent. Undo
   names the run it takes back, so a late or repeated Undo meant for an
   earlier press cannot cancel a later press of the unchanged draft. The
   in-memory composition's inline dispatcher parks the run in a background
   task for the same window and skips one taken back meanwhile, so the
   request returns at once and the window holds without a worker.
3. **The consent satisfies the ordinary approval, once.** The task performs
   the identity check and the fresh thread read, revalidates the consent
   (owner, account, servers, draft revision, source revision, expiry, the
   gesture event, and the frozen invocation's arguments), claims the frozen
   revision, and calls the send tool. `REQUIRE_APPROVAL` raises as usual;
   the consumer verifies that the pending approval's tool, arguments and
   argument hash match the consented draft, records `approval.requested`, and
   resolves it `APPROVE_ONCE` under distinct-resolver policy, exactly as the
   Archive consumer does. No notification asks for a consent already given.
   The pre-effect dispatch guard revalidates the consent again. Any mismatch
   ends the send without dispatch and returns the draft to editing.
4. **Older paths stay.** `send-proposal` and Final Send on a pending approval
   remain for older clients and for a draft already awaiting approval; the
   Email client's primary action no longer uses them.

## Consequences

Sending is one press with a ten-second way back, instead of two presses, a
sheet and no way back. The freshness read, the atomic claim, the uncertain
outcome rules and the stated external race are unchanged. The approval record
for every send still exists and is resolved once by the owner's own gesture;
the audit trail gains the gesture event.

What this does not do: it adds no standing grant and no new scope; a client
request still cannot mint account authority. Risk prompts before Send (a
first-time recipient, Bcc, a large reply-all, "attached" with no attachment)
are a possible follow-up and are not part of this decision.
