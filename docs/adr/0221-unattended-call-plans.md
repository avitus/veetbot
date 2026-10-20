# ADR-0221: Unattended call plans, with retries and a follow-up

- Status: Proposed — awaiting the owner's decision
- Date: 2026-10-20
- Amends, if accepted: ADR-0213
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
2. **Nothing can arrange a later attempt.** An occurrence holds exactly its
   schedule's requested scopes, and `schedule.write` is never among them. The
   run lasts at most 300 seconds and has no way to wait. A two-minute retry or
   a five-minute follow-up therefore has no mechanism at all.
3. **Chat confirmed a plan that could not run.** This is fixed with this
   proposal, without changing authorization: the `schedule.create` result for
   a call schedule now says that no retry or follow-up is arranged, and tells
   Chat to say so when the owner asked for one.

Causes 1 and 2 are decisions about authorization, so they are put to the owner
here.

## Decision proposed

**The platform runs the call plan. The model only places each call.**

- `schedule.create` with `place_call=true` takes an optional closed
  `call_plan`: `retry_every_minutes`, `retry_until`,
  `follow_up_after_answer_minutes` with a `follow_up_instruction`, and
  `alert_if_unreached`. Proposed bounds: at most ten attempts in sixty minutes,
  at least one minute apart, plus one follow-up. Every attempt dials only the
  numbers in the owner-approved instruction. The owner authorizes the whole
  plan once, on the card or through ADR-0213's owner request.
- After each attempt, the scheduler reads only the closed status that call
  reconciliation already records: `no_answer`, `busy`, `voicemail`, `failed`,
  `completed`, or an uncertain dispatch. It never reads summaries or
  transcripts.
  - Not reached, with time left: it fires the next attempt as an occurrence of
    the same revision.
  - `completed`: retries stop. If the plan has a follow-up, it fires that many
    minutes after the answered call.
  - Uncertain, or no result by the next interval: it does not dial again and
    alerts the owner, because a duplicate call is worse than a missed retry.
  - Out of attempts: the existing scheduled-run alert tells the owner that the
    person was not reached.
- Each attempt's seed is the owner-approved instruction plus a platform line
  built only from those statuses and times, for example "Attempt 3 of 6.
  Earlier: no answer at 9:30, voicemail at 9:32." It carries `PLATFORM` trust.
  Attempt runs request only `mcp.bland_call.use`, so they read no call record,
  and ADR-0213 allows their call without a card, just as it allows the first.

ADR-0213's checks all stay: number provenance, the conversation-wide content
check, dispatch's pinned-configuration checks, one-active-call admission, the
kill switch, and the card for any content-derived call.

## Open questions for the owner

1. Approve the platform-run plan, or choose an alternative below?
2. Are the bounds right: ten attempts, sixty minutes, one minute apart, one
   follow-up?
3. Should retries ever leave voicemail? Proposed: never; only the plan's own
   instruction can ask for it.
4. If the person says "stop calling" on an answered call, the platform cannot
   tell from the status alone. Should the follow-up still be placed, opening by
   asking whether to continue? Or should follow-ups wait until the in-call
   agent records a structured do-not-call result?
5. When an outcome is uncertain, stop and alert (proposed), or retry?

## Consequences

- This depends on telling a person from a voicemail reliably. A voicemail
  recorded as `completed` would stop the retries early. Work to classify
  voicemail-detection hang-ups as voicemail is a prerequisite.
- When accepted, a new gate covers the plan end to end: request, retries,
  follow-up, and the unreached alert. Hard gate 3's wording is unchanged.
- Until then, Chat says which parts of such a request will not happen. A
  scheduled single call still needs no card, as long as the occurrence reads no
  call record first.

## Alternatives considered

- **Let the occurrence orchestrate.** Give call occurrences a narrowed
  `schedule.write` for one-time call schedules to the same numbers, plus a
  closed-schema outcome read that does not taint. This keeps the model in the
  loop: on 2026-10-20 the model read the full list anyway, and that would
  still taint the run. It also widens what `schedule.write` delegates, and
  loop bounds would have to hold across schedules.
- **A durable wait inside one occurrence.** This needs the same untainted
  outcome read, holds a run for the whole plan beyond the 300-second limit, and
  still leaves the model orchestrating.
- **Exempt the run's own call reads from the taint.** Call records carry what
  callees and inbound callers said, which is exactly what ADR-0213 keeps from
  choosing calls.
- **Count the owner's in-run "yes" as authorization.** It still asks the owner,
  which is the complaint, and it lets content-shaped questions collect
  approvals.
- **Do nothing more.** Chat now tells the owner the truth, but each later call
  needs the owner present.
