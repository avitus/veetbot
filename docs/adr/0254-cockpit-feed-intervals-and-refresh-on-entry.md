# ADR-0254: Cockpit feeds refresh every minute to every day, and on entry

- Status: Accepted (the owner asked for both on 2026-10-26)
- Date: 2026-10-26
- Related: ADR-0072, ADR-0073, ADR-0088, ADR-0225, ADR-0240
- Amends: ADR-0240's assistant feeds, which a schedule refreshed at most daily
- Detailed design: `docs/plan/cockpit.md`, `docs/plan/scheduling.md`

## Context

An assistant feed is kept current by a schedule the owner approved, and a
schedule fires at most once a day at a local time. The owner's first widget,
`github-pull-requests`, is fed by a daily schedule, so its pull requests are up
to a day old. On 2026-10-26 the owner asked for two things:

- refresh times anywhere from one minute to one day;
- every widget refreshed whenever the owner switches to Cockpit.

Scheduling keeps a closed calendar union. Its open question 3 leaves interval
multipliers out until they have an explicit anchor, so that a revision update
cannot shift their phase.

## Decision

1. **A fifth recurring cadence, `INTERVAL`.** It has `every_minutes` (1 to
   1,440) and an IANA `timezone`, which is used only to show its times. Its
   instants are the whole multiples of `every_minutes` minutes since
   1970-01-01T00:00Z. The phase therefore depends only on the value: never on
   when the schedule was created or revised, and never on daylight saving. A
   divisor of 1,440 lands on the same UTC minutes every day.
2. **Only a feed-keeping schedule may use it.** That is a schedule whose
   requested scopes include `cockpit.write`. Any other schedule is refused
   with `schedule.interval_needs_feed`, so the calendar union stays closed for
   general schedules.
3. **New tool versions.** `schedule.create` 1.6.0 offers `INTERVAL` with
   `publish_cockpit_feed=true`. `schedule.update` 1.1.0 can change a feed
   schedule to `INTERVAL`. Both are registered in Cockpit deployments only.
   `schedule.list` 1.1.0 lists `INTERVAL` schedules and is always registered.
   Every earlier version stays registered unchanged for pinned runs, and the
   earlier list versions leave interval schedules out of their pages, because
   their output cannot describe one. The roster is at its definition cap
   (ADR-0253), so 1.6.0 is worded to cost no more tokens than 1.5.0, and every
   chat plans the same tools as before.
4. **Feed upkeep stays quiet.** A feed-keeping schedule created through
   `schedule.create` notifies for attention only, because its widget is its
   report. An interval schedule records an overlap skip without notifying the
   owner, because a refresh that outlasts its interval is routine. Failures,
   approvals and questions still notify.
5. **Refresh on entry.** `POST /v1/cockpit/refresh` (scope `cockpit.write`)
   starts one run, now, of each active, recurring, feed-keeping schedule. Each
   run is a manual occurrence keyed `cockpit.refresh:<UTC minute>`.
   - A schedule is skipped when its latest run is still going, or when that
     run started under a minute ago.
   - Authority, scopes, the pinned agent and tenant admission are checked as
     for Run-now. A refusal is reported for that schedule, not as an error.
   - These runs do not count toward Run-now's five a day.
   - A completed refresh run sends no report notification.

   Platform feeds are not re-projected, because the projector already
   publishes their changes every thirty seconds. The native client calls the
   route, and reloads the board, each time Cockpit becomes the visible mode of
   an active app.

## Consequences

- Every refresh is a model run, and every run is a new scheduled
  conversation. A one-minute feed costs 1,440 runs a day. The per-run limits,
  the run timeout and the tenant's daily and monthly cost ceilings still
  apply. A refresh that outlasts its interval skips the instants it overlaps.
- Opening Cockpit costs at most one run per feed-keeping schedule per minute.
- The `github-pull-requests` schedule is daily and still notifies on every
  report, because it predates decision 4. Chat can change its interval with
  `schedule.update`, and the owner can quiet its reports in the schedule
  browser.
- The native schedule browser summarizes the cadence as, for example,
  "Every 15 min", "Every hour" or "Every 2 h". An older client shows it as
  "Interval".
- A release older than this one cannot read an interval revision. Rolling
  back past it needs every interval schedule moved to a calendar cadence
  first.

## Alternatives considered

- **Arbitrary cron or RFC 5545 for every schedule.** This is a far larger
  surface than the owner asked for, and it is still scheduling's open
  question 2.
- **A fixed menu of intervals.** The owner asked for the whole range, and the
  epoch anchor makes every minute value deterministic.
- **Anchor at creation time.** A revision update would then have to define a
  phase, which is exactly what scheduling's open question 3 avoids.
- **Only reload the board on entry.** The client already resumes the stream,
  so this would refresh nothing the owner can see.
- **LLM-free collectors.** These remain out of scope (ADR-0240), because they
  need an egress allowlist and their own design.
