# ADR-0215: Native schedule lifecycle, plain-words creation, Run now and delivery preference

- Status: Accepted (owner, 2026-10-20: "implement all 4")
- Date: 2026-10-19
- Related: ADR-0049, ADR-0072, ADR-0075, ADR-0080, ADR-0089, ADR-0091,
  ADR-0109, ADR-0150, ADR-0154, ADR-0213; Sections 16 and 29 of the
  engineering plan
- Would amend: ADR-0075 decision 4 (as amended by ADR-0154), ADR-0089
  decision 4, ADR-0072 decision 4 (part B only)
- Detailed design: `docs/plan/scheduling.md`,
  `docs/plan/notifications-and-devices.md`

## Context

The A2 Schedules design has a New schedule sheet where the owner types plain
words and sees "Veetbot understood: Weekdays at 7:00 am · first run tomorrow"
before saving; Run now, Pause, Resume and Delete; "Notify when a report is
ready" and "Only notify when something needs you" switches beside the
spending limit; and Recent runs with status and "Open report in Chat".

The native surface is read-only except for website access: it "has no other
create, update, pause, resume, cancel, delete, occurrence, or run-history
control" (scheduling.md:676-687; ADR-0075 decision 4, ADR-0089 decision 4).
The server already has every lifecycle route (scheduling.md:543-570): create
with a structured `ScheduleDefinition` under `schedule.write` and
`Idempotency-Key`; full-definition `PATCH`, pause and resume under
`schedule.write` with `expected_revision`; `DELETE` as terminal cancel under
`schedule.cancel` (ADR-0080 rejected hard deletion; ADR-0089 keeps terminal
records 30 days); and the occurrence ledger (scheduling.md:446-450).

Four gaps remain:

1. **No parser.** Natural-language parsing is not part of the tool; the model
   parses in chat (scheduling.md:1032-1037, ADR-0072 decision 4).
2. **An asymmetry.** Chat creation is an approval-gated `EXTERNAL_WRITE`/HIGH
   tool call whose agent and policy pins, limits, grace, timeout and failure
   limit are derived server-side (`src/agent_core/tools/schedule_create.py`,
   `ScheduleDefinition` construction and `_run_limits`). HTTP creation needs
   only `schedule.write` and takes those values from the client, validated
   against tenant ceilings (scheduling.md:607-612).
3. **No run-now.** Occurrences are unique on `(schedule_id, nominal_fire_at)`
   and follow the cadence; ADR-0109 set retry and run-now aside as needing
   their own idempotency and overlap design.
4. **No per-schedule delivery preference.** Only per-device `muted_kinds`
   exist (notifications-and-devices.md:155, notifications-and-devices.md:176-178),
   with no device update route; `schedule_run_finished` fires after every
   accounted run (scheduling.md:1072-1077). Email and webhook delivery are
   undesigned.

## Decision

Four parts; the owner can accept each separately. Until a part is accepted
the client hides its control.

### A. Native pause, resume, cancel and run history (client only)

The schedule detail gains Pause (ACTIVE), Resume (PAUSED) and a cancel
control on ACTIVE and PAUSED schedules, through the existing routes with a
fresh point read's `expected_revision`; a `schedule.revision_conflict` is read
again and retried once, as ADR-0154 decision 5 does. Pause and resume need
`schedule.write`; cancel needs `schedule.cancel`. The cancel control is
labelled for what it does — "Stop schedule", confirmation "Stops future runs.
A run in progress finishes. The record stays in Recent History for 30 days."
— never "Delete", because the record is not deleted. Resuming a
failure-limit pause keeps its failure count (ADR-0109).

Recent runs reads `GET /v1/schedules/{id}/occurrences` under `schedule.read`:
nominal time, disposition, and for `MATERIALIZED` rows the linked run's status.
"Open report in Chat" appears only when `session_id` is non-null and opens
that session; erased links show "Conversation deleted". No route, scope or
migration is added.

### B. A server-side draft from plain words

`POST /v1/schedules/drafts` under `schedule.write` takes `{text, timezone}`
(text at most 1,000 characters; timezone a valid IANA zone from the device)
and never persists a schedule. It makes one bounded model call: no tools, no
memory, no conversation history, structured output constrained to
`{title, instruction, at | cadence}` from the existing closed cadence union,
or a closed `needs_clarification` reason. It returns a typed draft:

- the parsed `at` or `cadence`, title and instruction, validated by the same
  validators as `POST /v1/schedules`, including the secret-like-instruction
  refusal;
- the server-derived values `schedule.create` pins today: agent id, version
  and policy profile, `RunLimits` with synthesis reserves, run timeout,
  misfire grace, `max_consecutive_failures = 2`, and empty
  `requested_scopes`;
- the next three fire times from the deterministic recurrence engine, and a
  summary rendered deterministically from the typed cadence. The model writes
  no part of the "Veetbot understood" line.

The call is charged to the tenant's model budget, capped at USD 0.05, and
rate-limited to 30 drafts per principal per hour (`429 rate_limited`).

Creation then posts the confirmed draft to `POST /v1/schedules` with an
`Idempotency-Key`. To close the asymmetry, `POST /v1/schedules` derives the
pins, limits, grace, timeout and failure limit server-side when the client
omits them, exactly as `schedule.create` does, and still refuses supplied
values above tenant ceilings or mismatched with the agent's policy. Website
access and calling remain separate explicit owner choices (ADR-0150, ADR-0154,
ADR-0213); neither the draft model nor the text can request a scope. The
owner's tap in an authenticated client is the authorization, as in ADR-0154
decision 6. The chat tool and its approval card are unchanged.

### C. Run now

`POST /v1/schedules/{id}/run` with `expected_revision` and an
`Idempotency-Key`, under a new exact scope `schedule.run` (running spends money
and exercises the schedule's pinned authority, which pause and resume do not).

- The occurrence ledger gains `trigger` (`scheduled` | `manual`, default
  `scheduled`). A manual occurrence's `nominal_fire_at` is the request time;
  uniqueness becomes `(schedule_id, trigger, nominal_fire_at)` plus a unique
  `(schedule_id, manual_request_key)` for manual rows.
- Allowed on ACTIVE and PAUSED recurring schedules; one-time schedules refuse
  with `schedule.run_now_one_time`, since a second run would duplicate their
  only purpose. Terminal states refuse with the existing `schedule.terminal`.
- Overlap: refused with `409 schedule.run_active` while any linked run is
  non-terminal. A scheduled instant due while a manual run is active receives
  `SKIPPED_OVERLAP` by the existing rule (scheduling.md:313-323).
- The same admission checks as materialization: current authority, scopes,
  per-run budget reservation, and tenant daily and monthly ceilings. At most
  five manual runs per schedule per day.
- `next_fire_at` does not move. A manual run neither increments nor resets
  `consecutive_failures`, so testing a fix cannot pause the schedule and one
  manual success cannot hide a failing cadence.
- ADR-0213's scheduler-seed authorization applies unchanged: a call schedule
  run now places its call.

### D. Per-schedule notification preference

`ScheduleDefinition` gains `notify: "every_report" | "attention_only"`,
default `every_report` (today's behavior; absent in stored revisions upcasts
to it). With `attention_only`, the accountant does not enqueue
`schedule_run_finished` for a `COMPLETED` run. Approvals, questions,
`run_failed` and `schedule_occurrence_skipped` are never suppressed by a
schedule preference, because each needs the owner. The two designed switches
collapse to this one field; the client shows a single "Only notify when
something needs you" switch. A `none` value is not offered: an unseen approval
would stall the run. The field travels through ADR-0154's full-definition
update and is a revision change; `schedule.create` keeps the default.

## As built (2026-10-20)

Accepted parts were built with these choices, each no weaker than the text
above:

- **B.** The model returns only the timing and a short title; the instruction
  is the owner's sentence verbatim, so the model writes nothing the schedule
  will act on. The draft returns a complete server-derived definition, which
  the client posts unchanged; `POST /v1/schedules` keeps validating every
  supplied pin against the tenant ceilings and the agent's policy, so deriving
  omitted fields on `POST` was not needed and was not added. The route is on
  with the schedule API (`AGENT_SCHEDULE_DRAFTS_ENABLED=0` removes it), its
  model is the title-generation policy, and usage is recorded as a
  content-free `schedule.draft_interpreted` process event that also counts
  the hourly allowance; the "Veetbot understood" line is rendered on the
  device from the typed cadence.
- **C.** Uniqueness of scheduled instants is a partial index on
  `trigger = 'SCHEDULED'` rather than a three-column key, so two manual runs
  can never collide on an instant; a manual occurrence is unique per schedule
  by its request key and is only recorded once its run starts. Refusals for
  authority, policy and admission are returned to the owner rather than
  recorded as failed occurrences. The daily cap is a rolling 24 hours.
- **D.** The occurrence's pinned revision decides, so a change applies from
  the next run.

## Consequences

- The owner manages schedules from the app with the same audit trail as chat.
- HTTP creation becomes no weaker than chat creation: the same derived pins,
  ceilings and validations, with the owner's explicit confirmation in place of
  the approval card.
- Run now adds billed runs; the scope, overlap refusal, daily cap and existing
  ceilings bound them. Device-level mutes remain the outer control.

## What changes if accepted

- A: scheduling.md:676-687 rewritten; ADR-0075 decision 4 and ADR-0089
  decision 4 amended. Evidence: Swift transport, view-model and iOS UI
  journeys under ADR-0049.
- B: new route behind `AGENT_SCHEDULE_DRAFTS_ENABLED` (default off; the cost
  cap and rate limit above are proposed values for the owner to confirm);
  `POST /v1/schedules` derivation of omitted fields;
  scheduling.md public API and a new "Drafts" section; ADR-0072 decision 4
  amended for this route only (the tool still accepts no natural language).
  Gate cases: no persistence, derived pins equal `schedule.create`'s, scope
  requests refused, budget and rate limit, deterministic summary.
- C: route, migration for `trigger` and the uniqueness change, scope
  `schedule.run` in `src/agent_core/policy/scopes.py` (the count asserted in
  `tests/gates/test_policy_m4.py` moves from 43 to 44) and in deployment
  `AUTH_SCOPES`; scheduling.md overlap, failure-counting
  (scheduling.md:478-483) and occurrence sections. Gate cases: idempotency,
  overlap refusal, no failure-count change, ceilings, one-time refusal.
- D: definition field and upcast, accountant change, the trigger table
  (notifications-and-devices.md:361-378) and scheduling.md:1072-1077.

## Alternatives considered

- **Parse on the device.** Rejected: a second parser would drift from the
  model path and could not derive server pins.
- **Create through a chat turn behind the sheet.** Rejected: it costs a full
  run, writes a conversation, and still needs the approval card.
- **Run now as a retry of the last occurrence.** Rejected for ADR-0109's
  reason: it re-bills an occurrence whose identity is already decided.
- **Per-device mutes for this.** Rejected: they mute every schedule alike and
  have no update route.
