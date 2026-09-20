# ADR-0109: Conversational schedule failure tolerance and synthesis headroom

- **Status:** Accepted
- **Date:** 2026-09-19
- **Related:** ADR-0059, ADR-0072, ADR-0073, ADR-0078, ADR-0088
- **Detailed designs:** `docs/plan/scheduling.md`
- **User authorization:** on 2026-09-19 the owner chose, for schedules created
  in chat, an automatic pause on the second consecutive failure, a USD 5 run
  budget with a final-synthesis reserve, and a content-free failure
  classification on the retained accounting event.

## Context

Between 2026-09-01 and 2026-09-17 the two recurring briefings failed six
times, for unrelated reasons: a USD 1 budget exhausted before
synthesis, provider errors misclassified as permanent, a scheduler role
lacking a row-lock privilege, and three failures, on two mornings, whose
cause is no longer recoverable. Those defects were repaired as they were
found. Every failure nonetheless paused its schedule, and each schedule stayed
paused until the owner resumed it, from under an hour to seven days later.

The pause came from the `schedule.create` definition. Milestone 19 introduced
that tool for one-time reminders and pinned `max_consecutive_failures = 1`,
under which a one-time schedule completes after its only occurrence. Milestone
20 made the same tool create recurring schedules without revisiting the value,
so a daily briefing paused on its first failed morning.

The same definition pins a USD 1 budget with no synthesis reserve. ADR-0078
repaired the 2026-09-01 failure for one schedule by hand, with a USD 5 budget
and a reserve; schedules created in chat afterward still received USD 1. The
technology briefing created on 2026-09-04 spent USD 0.59 to USD 0.84 on its
successful runs.

Three failures could not be diagnosed. Deleting a conversation erases its runs
and their failure records, and `schedule.run_accounted` recorded only the
terminal status.

## Decision

1. A schedule created by `schedule.create` pins
   `max_consecutive_failures = 2`: one failed occurrence is tolerated, and the
   second consecutive failure pauses the schedule. Counting, reset, and the
   failure-limit pause are unchanged.
2. Its cost is the minimum of the active agent's finite cost or USD 5 and the
   schedule cost ceiling. It pins final-synthesis reserves of 2 steps, 2 model
   calls, and USD 1, each kept only when strictly below its pinned total, so
   a low ceiling yields a valid definition with that reserve omitted.
3. `schedule.run_accounted` carries `failure`: null unless the run failed,
   otherwise its failure reason, error class, and whichever sanitized provider
   diagnostics (`provider`, `provider_code`, `http_status`,
   `provider_parameter`) the run recorded. The failure message and every other
   detail stay with the run.
4. Existing revisions keep their pinned values. `schedule.update` still cannot
   change limits or the failure limit, so an existing schedule adopts these
   values only through an explicit revision from an authorized API client.

## Consequences

- A transient failure no longer costs every following occurrence until the
  owner resumes the schedule. A defect that persists pauses after two failed
  runs, each still bounded by its own budget.
- A research run that approaches its budget writes its answer instead of
  failing. The admission reservation for an in-flight run rises to USD 5; the
  default daily and monthly ceilings still admit both current briefings.
- The next unexplained pause is diagnosable after its conversation is deleted,
  without retaining any run content.

## Alternatives considered

- **Pause on the third consecutive failure:** offered and not chosen; a
  persistent defect would spend three bounded runs before pausing.
- **Retry a failed occurrence within its grace window:** not pursued; a
  retry is a second billed run of the same occurrence and needs its own
  idempotency and overlap design.
- **Reset `consecutive_failures` on resume:** not adopted, because it would
  change the documented reset rule. A schedule resumed after a failure-limit
  pause keeps its count, so until one of its runs completes, its next failure
  pauses it again.
