# ADR-0221: Unattended call plans, with retries and a follow-up

- Status: Accepted — the owner accepted it and asked for its implementation on 2026-10-20
- Date: 2026-10-20
- Amends: ADR-0213
- Detailed design: `docs/plan/bland-calling.md`, `docs/plan/scheduling.md`
- Scope: calling and scheduling; no milestone or gate moves

## Context

On 2026-10-19 the owner asked: call Cheryl at 9:30 to wake her; if she does
not answer, call every two minutes until 9:40, then alert me; if she answers,
call again five minutes later to check she is still awake. Chat made one
`schedule.create` with `place_call=true`. ADR-0213 authorized it without a
card, and its revision requests exactly the two calling scopes. Its instruction
told the occurrence to check earlier outcomes before each retry and to arrange
later attempts as one-time call schedules. Chat confirmed the retries and the
follow-up to the owner.

At 9:30 the occurrence read the call list, then asked the owner whether to
place just one call, because it had no scheduling tool. The owner said yes.
The call then waited on a confirmation card, which the owner approved. It
reached voicemail. No retry or follow-up followed.

Three causes, each confirmed against the code:

1. **The occurrence's own read withdrew ADR-0213's authority.** The call-list
   result is call-record content, labelled `EXTERNAL_UNTRUSTED`. That one read
   made the turn's origin untrusted and marked the conversation untrusted, and
   ADR-0213 refuses on either. The label comes from the tool's declared output
   trust, so even an empty list has this effect. On 2026-10-20 the list
   included an inbound stranger's call summary, which is exactly the content
   ADR-0213 keeps from choosing a call. But every retry or follow-up decision
   needs to know how the last call went, so under ADR-0213 any
   outcome-dependent second call needs a card.
2. **Nothing could arrange a later attempt.** An occurrence holds exactly its
   schedule's requested scopes, and `schedule.write` is never among them. The
   run lasts at most 300 seconds and has no way to wait.
3. **Chat confirmed a plan that could not run.** The `schedule.create` result
   for a call schedule now says when no retry or follow-up is arranged.

## Decision

**The platform runs the call plan. The model only places each call.**

- `schedule.create` 1.4.0 takes `call_plan` with `place_call=true` and a
  one-time `at`: `retry_every_minutes` (1–30) with `retry_until`, and/or
  `follow_up_after_answer_minutes` (1–60). A plan places at most ten calls
  within an hour of `at`, then at most one follow-up. Version 1.3.0 stays
  registered, unchanged, for runs pinned to it. The owner authorizes the whole
  plan once, on the card or through ADR-0213's owner request. A plan cannot
  change after its first call.
- A plan's revision requests only `mcp.bland_call.use`. No call run can read
  a call record, so no run taints itself, and ADR-0213 allows each call
  without a card, exactly as it allows the first.
- The scheduler cannot read the call ledger, and this decision gives it no new
  grant. Instead the maintenance sweep, which runs in the application role,
  reads each call's closed dispatch status every pass: `no_answer`, `busy`,
  `voicemail`, `failed` (not reached), `completed` (answered), `not_dispatched`
  (no call), or one still in flight. It never reads a summary or transcript.
  - Not reached, with time and calls left: the next call fires at the
    previous call's time plus the interval, or at once if the result came
    late.
  - Answered: retries stop. A follow-up fires that many minutes after the
    answered call ended, even past `retry_until`.
  - Still in flight: the plan waits. After 20 minutes without a result, it
    ends with an unknown outcome and never redials, because a duplicate call is
    worse than a missed retry. A run that placed no call also ends the plan.
  - Ended, for any reason: one more occurrence runs with no scopes at all. It
    can tell the owner how the plan went, but it cannot call. Its
    scheduled-run alert is the plan's only alert; each completed call stays
    quiet.
- The sweep records each decided step as a `schedule.call_plan.step` process
  event and sets the schedule's next firing. The scheduler materializes that
  firing and reads the step to choose the run's scopes and its note.
- Each run's seed is the owner-approved instruction plus a platform note,
  built only from those statuses and times, for example "this is call 3 of up
  to 7 in a call plan. Earlier calls: 16:30 UTC no answer; 16:32 UTC
  voicemail." The note states hours and minutes and never contains a digit run
  that the number check could read as a phone number.

ADR-0213's checks all stay: number provenance, the conversation-wide content
check, dispatch's pinned-configuration checks, one-active-call admission, the
kill switch, and the card for any content-derived call.

## Answers to the open questions

1. The platform-run plan is accepted.
2. The bounds are accepted: ten calls, sixty minutes, at least one minute
   apart, and one follow-up.
3. Retries leave no voicemail unless the instruction asks for one. The note
   tells each run so.
4. The owner did not choose. The implemented default is that the follow-up is
   placed, and the note asks its brief to open by asking whether the person is
   happy to talk and to end politely if they ask not to be called again. A
   structured do-not-call result from the in-call agent remains possible
   later.
5. An uncertain outcome stops the plan and reports it.

## Consequences

- Each plan's correctness depends on telling a person from a voicemail. A
  call ended by the provider's own voicemail detection is recorded as
  `voicemail` (the calling design's provider notes). A voicemail the provider
  reports as a completed conversation would still stop the retries early.
- Gate tests in `tests/gates/test_owner_requested_calls.py` run a plan end to
  end: the retry, the follow-up, the report run that cannot call, and an
  unanswered plan alerting once. No hard gate is added or renumbered, and hard
  gate 3's wording is unchanged.
- Only one-time call schedules carry a plan. A plan paused after its first
  call cannot be resumed: its one-time `at` has passed, so resuming fails with
  `schedule.no_future_occurrence`.
- A revision without a plan is stored exactly as before, so a release that
  predates call plans still reads it. A release that predates call plans
  cannot read a plan's revision.

## Alternatives considered

- **Let the occurrence orchestrate.** Give call occurrences a narrowed
  `schedule.write` for one-time call schedules to the same numbers, plus a
  closed-schema outcome read that does not taint. This keeps the model in the
  loop: on 2026-10-20 the model read the full list anyway, and that would
  still taint the run. It also widens what `schedule.write` delegates, and
  loop bounds would have to hold across schedules.
- **Let the scheduler read call results.** The scheduler role is an exact
  allowlist that excludes the call ledger, which holds transcripts. The
  maintenance role can already read it.
- **A durable wait inside one occurrence.** This needs the same untainted
  outcome read, holds a run for the whole plan beyond the 300-second limit, and
  still leaves the model orchestrating.
- **Exempt the run's own call reads from the taint.** Call records carry what
  callees and inbound callers said, which is exactly what ADR-0213 keeps from
  choosing calls.
- **Count the owner's in-run "yes" as authorization.** It still asks the owner,
  which is the complaint, and it lets content-shaped questions collect
  approvals.
- **A new notification kind for an unreached person.** It would need a
  database constraint change and client support. The report run's ordinary
  scheduled-run alert says the same thing in the session.
