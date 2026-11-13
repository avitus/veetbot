# ADR-0320: A failed refresh shows on its tile, and one job request waits ten seconds

- Status: Accepted — owner accepted on 2026-11-13
- Date: 2026-11-13
- Related: ADR-0225, ADR-0254, ADR-0268, ADR-0271; Milestone 35
- Amends: ADR-0254 decision 5, under which a failed refresh on entry notified
  as any scheduled run does; ADR-0225 part D for that one kind of run
- Detailed design: `docs/plan/cockpit.md`, `docs/plan/scheduling.md`,
  `docs/plan/notifications-and-devices.md`

## Context

On 2026-11-13 the owner received a "Run failed" notification and found nothing
in the app. Opening Cockpit had started a refresh of the garden widget's feed.
Its job made one weather request, which normally answers in a tenth of a
second, and received no answer before the job's 30-second limit. It was the
fourth such failure in a day, all on refreshes started by entering Cockpit.
Why the request stalled is not known: ten requests from the same server
shortly afterwards each answered at once, and broker events carry no times.

Three rules combined to make that failure both certain and invisible:

- One job request could wait 30 seconds, the garden job's whole limit. A
  stalled host therefore always ended the run, although the program already
  handles a failed request by publishing a partial result with a message.
- A failed run published nothing, so the tile kept its last value and looked
  current until it aged past three intervals.
- Every failed scheduled run notifies (ADR-0225 part D), and a refresh run's
  conversation is operational work that clients never show (ADR-0271). The
  server sent two alerts, `run_failed` and `schedule_run_finished`, and
  neither opened anything.

## Decision

1. **One job request waits ten seconds at most.** The limit covers name
   resolution, the credential, the connection and the whole response. Past
   it, the request fails as `job.service_unavailable`, retryable, exactly as
   an unreachable host does. The job's own limit still bounds the run.
2. **A failed job run marks its feeds.** When a run that is not a trial ends
   as failed, each feed its grant may publish receives one snapshot that
   repeats the feed's last value, observation time and expected interval, and
   carries the error "The last refresh did not finish." The reason is logged
   and never stored, because it can name a host. The kit already renders a
   feed error on the tile. A feed with no value yet stays pending. The next
   successful publish clears the error. Failing to record the mark never
   hides the run's own failure.
3. **A refresh on entry never pushes its outcome.** Its occurrence session
   carries the reserved marker `cockpit_entry_refresh: true`. The accountant
   enqueues no `schedule_run_finished` for it, whatever the outcome, and a
   `run_failed` alert for it is superseded before delivery, as an operational
   Email failure's is. Approvals and questions still notify.
4. **Timer firings are unchanged.** A feed-keeping schedule's own failed run
   still notifies, still counts toward the failure pause, and now also marks
   its tile.

## Consequences

- A stalled host costs a job ten seconds, not its run. A source that
  legitimately needs longer than ten seconds for one answer can no longer be
  read by a job; none of the installed jobs comes near that.
- A request the job does not handle still ends the run, and the tile then
  says so.
- The error snapshot is one more entry in the feed's history, with the
  previous value repeated.
- A refresh on entry of a legacy feed schedule that runs an agent, not a job,
  is quiet too, but nothing marks its tile; its tile ages to stale. Both
  installed feeds are jobs.
- A release older than this one ignores the marker and notifies as before.

## Alternatives considered

- **Scale the request limit with the job's limit.** A third of a 30-second
  job is the same ten seconds, and one fixed value is easier to explain to a
  job's author than a derived one.
- **Retry the request in the broker.** The job decides what a missing source
  means; a silent second attempt would double a slow run's time.
- **Quiet every failed feed run.** The second scheduled failure pauses the
  schedule, which the owner needs to hear about.
- **Open the schedule's history from the alert.** It would still interrupt
  the owner for a failure the tile now shows and the next refresh may clear.
