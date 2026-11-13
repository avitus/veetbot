# ADR-0321: A follow-up message steers the run that is already working

- Status: Proposed. The owner asked on 2026-11-13 to steer a working chat with
  a follow-up and asked for this draft. Implementation waits for the owner's
  acceptance of the decisions and open questions below.
- Date: 2026-11-13
- Amends: ADR-0009 (run, turn and session model), ADR-0023 (the run loop;
  contradiction 14, "single active run"), ADR-0028 (message routing)
- Related: ADR-0004 (one active run per session), ADR-0166 (resume adopts
  committed history), ADR-0172 and ADR-0302 (consent and grants a later owner
  message ends), ADR-0064 (surface run budget)
- Detailed design: `docs/plan/runtime-loop.md`,
  `docs/plan/http-api-and-streaming.md`,
  `docs/plan/event-log-and-persistence.md`

## Context

The owner cannot send a follow-up while Chat is working. The composer is
disabled until the run ends, the message route answers `409 conflict` with
`active_run_exists`, and the plan defines a run as exactly one user message
(Section 27.1). Section 27.5 allowed "reject or queue" for a second message;
`runtime-loop.md` chose reject because the one-active-run index makes a second
run impossible.

Long runs make this costly. A browser or research run can work for many
minutes. When the owner sees it heading the wrong way, the only choices are to
wait or to stop it and lose its work.

Three constraints shape any answer:

1. **Provider pairing.** A user message cannot sit between a tool call and its
   result. The session history is ordered by event sequence, so a message
   appended the moment it arrives could land inside an unanswered batch.
2. **The worker owns the conversation.** A `RUNNING` run's checkpoint is held
   by its worker. The API cannot write it, as it does for a suspended run.
3. **Effects are never abandoned.** A tool call whose effect has been sent
   finishes (the cancellation rule in `runtime-loop.md`). Steering cannot be
   faster than that.

## Decision

1. **A follow-up joins the active run; it does not start a second one.** The
   one-active-run index, the state machine and the single terminal writer are
   unchanged. Section 27.1 is amended: a turn is one initiating user input,
   any follow-ups the same principal sends while it runs, and the agent's
   response. A run still executes exactly one turn.

2. **Follow-ups wait in a durable inbox.** `POST /v1/sessions/{id}/messages`
   for a session whose active run is `QUEUED`, `RUNNING` or
   `WAITING_FOR_APPROVAL` stores the content in `run_pending_inputs` and
   answers `202` with the active run and a `pending_input_id`. The insert
   locks the run row, rechecks that the run is not terminal, and appends a
   content-free `run.input.queued` event in the same transaction. The content
   lives in the inbox only until it is read, so the event log never holds a
   message twice. `WAITING_FOR_USER` keeps its existing rule: the text is the
   awaited answer.

3. **The loop reads the inbox at the top of a step.** In phase 1, when every
   earlier tool call has its result, the loop moves each pending row, in
   arrival order, into a `user.message.created` event under the run's lease,
   appends it to the checkpoint conversation, deletes the row and checkpoints.
   The event carries `pending_input_id`, and its derivation key is that id, so
   a crash and resume cannot read a follow-up twice. The model's next request
   then ends with the follow-up. The provider continuation is kept, because
   the message follows a complete batch.

4. **No interruption in the first version.** A follow-up waits for the model
   call or tool batch in flight. It does not cancel a model stream or a tool.
   The worst-case delay is the longest step, the same bound as cancellation.

5. **A follow-up the run never read starts the next run, or is dropped.** The
   terminal transition, in its one transaction, looks at the inbox:
   - `COMPLETED`: the pending rows become the user messages of one new
     `QUEUED` run through the shared submission function, with ordinary
     admission. If admission refuses, they are dropped.
   - `CANCELLED` at the owner's request: rows that arrived after the cancel
     request start the next run ("stop, then say something else"). Rows that
     arrived before it are dropped.
   - `FAILED`, or cancelled by deadline: dropped.

   A drop appends a content-free `run.input.dropped` event with the reason,
   and the client returns the text to the composer as a draft. The loop does
   not read the inbox once a synthesis reserve or a report-only stop is
   active; those follow-ups fall to this rule.

6. **A follow-up never answers an approval.** While the run waits on an
   approval card, a follow-up is held and read when the run resumes, after
   the owner has resolved the card. A run waiting on a child run holds it the
   same way. Child runs, scheduled runs and delegated sessions take no
   follow-ups and keep the `409`.

7. **A follow-up never widens authority, and it ends single-request
   consent.** It carries the sender's trust exactly as an initiating message
   does. Reading one ends the run's request grant (ADR-0302) and any pending
   specific Follow consent (ADR-0172), which both already end at the next
   owner message. Task grants, pinned tools, the agent version, the policy
   version and scopes are unchanged. The sender must be the run's principal;
   anyone else gets the `409`.

8. **Budgets do not reset.** Cost, step, tool-call and deadline limits are the
   run's original ones, and the surface run budget is reserved once. A steered
   run that reaches a limit fails as any run does.

9. **Bounded and text-only.** At most eight follow-ups may be pending on a
   run; a ninth is `409 conflict` with `pending_input_limit`. A follow-up is
   text. Attachments staged in the composer wait for the next initiating
   message, as they do for an answer to a question.

10. **One configured rule, default off.** `AGENT_RUN_STEERING_ENABLED` selects
    this routing. Off, every route and gate behaves as today. Section 27.8
    requires a deterministic, configured rule, and this is its third value.

11. **Every surface inherits it.** Telegram, SMS and WhatsApp submit through
    the shared function, so a message to a working chat is held as a
    follow-up and the "still working" notice becomes an acknowledgement.
    `/stop` is unchanged.

12. **The Apple client sends while a run works.** The composer stays enabled
    and Stop stays available beside Send. A pending follow-up shows in the
    transcript as waiting, moves into place when its `user.message.created`
    arrives, and returns to the composer on `run.input.dropped`. While an
    approval card is open the composer says the follow-up will be read after
    the card is answered.

## Alternatives considered

- **Queue only.** Hold the message and start a new run when the current one
  ends. It unblocks typing and does not steer. Decision 5 keeps it as the
  fallback for a follow-up that arrives too late.
- **Stop and restart.** Cancel the run and submit the follow-up as a new one.
  It discards in-progress work and still waits for any sent effect.
- **Append the user message when it arrives.** Rejected under constraint 1: it
  can split a tool call from its result in the stored history.
- **Hold the content in a new event.** It needs no table, but the immutable
  log would carry each follow-up twice, and every erasure, export and
  formation reader would have to learn the second type.
- **A second concurrent run.** Rejected by Section 27.5; it needs a different
  constraint and concurrent writers on one conversation.

## Consequences

- One additive table, three additive fields (`pending_input_id` on the
  submission response and on `user.message.created`, `pending_inputs` on the
  run view) and two content-free events. Session deletion and erasure must
  remove inbox rows; a foreign key to the run does this by cascade.
- Section 27.1's "submitting a user message creates exactly one run" becomes
  false when steering is on. Readers that assume one user message per run
  (history exchanges, titles, formation, the transcript route) must accept
  several. The context engine already keeps an unanswered trailing user
  message last.
- Pre-turn recall runs on the first step only, so a follow-up that changes
  the subject is answered without a fresh recall pass.
- A steered run is longer, so it reaches its cost or deadline limit sooner
  than two separate runs would.

## Open questions for the owner

1. Should a follow-up interrupt a model call that has not yet produced a tool
   call? It makes steering faster and wastes the tokens already streamed.
2. Should a follow-up typed before Stop be dropped, as decided, or sent?
3. Should a follow-up re-run recall when it is read?
4. Is eight pending follow-ups the right bound?

## Validation required before acceptance

No code exists. The implementation must begin with red tests for:

- routing per run status with the setting off and on, including a different
  principal, the pending limit and an attachment block;
- a follow-up read at the next step with tool pairs intact, for both provider
  adapters, with the continuation preserved;
- a crash between the read and the checkpoint, proving one
  `user.message.created` per follow-up after resume;
- the completion race: a follow-up inserted during the final model call
  starts exactly one new run and is never lost;
- each drop reason, and the stop-then-send order;
- a follow-up during an approval wait neither resolves nor voids the card;
- the request grant and Follow consent ending when a follow-up is read;
- session deletion and erasure with a pending follow-up;
- the surface gate `gate.surface.input_routing`, which today asserts a notice
  and no second run, restated for both values of the setting;
- the Apple reducer and composer, including a dropped follow-up returning to
  the draft.

No milestone or gate moves with this proposal. Gate registration is decided at
acceptance.
