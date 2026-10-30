# ADR-0271: Widget refreshes are operational work

- Status: Accepted for implementation on 2026-10-30 by the owner's request to
  fix failed widget acquisition and prevent widget updates from creating chats.
- Amends: ADR-0254's scheduled conversations and ADR-0268's feed-job transition.
- Scope: Existing Cockpit upkeep. No milestone, gate or policy ceiling moves.

## Context

The production PR widget's instruction named `job.run`, but its agent-run
schedule pinned an older agent and requested only the Cockpit scopes. Naming a
new tool in an instruction cannot put it in the pinned roster or grant its
scope. Its public-API fallback then received `tool.web.provider_rejected`
from Keenable. An authenticated read through the existing job broker succeeded.
The active saved job returned counts without publishing a feed; the fuller
collector was archived.
The schedule also created a conversation on every refresh. Quiet notifications
suppressed reports but did not suppress Chat history.

## Decision

Feed-keeping occurrences retain their isolated session, run, seed and audit
history. They are operational work, never new Chat conversations. The
materializer sets reserved metadata `cockpit_refresh: true` for every schedule
revision requesting `cockpit.write`, including typed jobs, timer firings,
Run now and refresh on entry. This is independent of success or failure.
Both session repositories exclude these records before conversation pagination;
point reads and the schedule inbox remain available. Apple clients recognize
the marker, remove previously cached rows during reconciliation and retain the
existing operational approval/question path without inserting Chat history.

A migration labels existing occurrences using their immutable schedule revision,
not the current revision, without deleting sessions, runs, messages or approvals.
Ordinary schedules remain conversations. This changes presentation only; it
does not reuse sessions or widen execution authority.

An explicit `schedule.update` with a job task may convert a feed-keeping agent
schedule in place. The published revision and owned approved grant are checked
through the existing schedule service. Scopes and limits derive from that grant;
the replacement grant must permit feed publication, and earlier occurrences
retain their original pins. General agent schedules still
need a separately created job schedule. No tool or permission is silently added
to a legacy agent run, and the normal update approval remains in force.

The PR widget repair uses a saved collector and the existing authenticated
GitHub broker, never the web-scraping fallback. The generic
`samples/jobs/github-widget` package produces the existing feed shape, pages
compact JSON, and publishes only after every requested repository is read.
Unavailable evidence fails rather than becoming a verified zero; incomplete
review or check evidence remains unknown. Public source contains no private
repository inputs or credentials. Existing publication, grant, sandbox and
feed-schema checks remain in force.
The sample retains ten-item pages and the existing 50-request/48 KiB ceilings.
Oversized selections fail without publishing; the owner reduces the selection
through normal approval before resuming. Only the published detail list is
capped to the feed's 300 items, after complete acquisition and count validation.

## Verification and delivery

Regression tests first reproduced the refused agent-to-job transition, all six
combinations of refresh trigger and task appearing in Chat, pagination admitting
an operational row, the missing historical marker, cached rows returning on the
Apple client, and the old collector neither publishing nor surfacing read failure.
The tests cover the repaired transition executing with zero model calls, audit
retention, repository parity, migration, native reconciliation and the collector.
The repository contract also preserves non-boolean historical `cockpit_refresh`
values. A PostgreSQL regression reproduced an invalid boolean cast that broke
the conversation index; JSONB containment now excludes only the literal `true`
marker, matching the in-memory repository without coercing strings or numbers.
Collector regressions reproduced schema rejection at 301 items and an attempted
51st request. They now preserve the complete count with bounded details and
refuse excess requests without publishing a partial snapshot.

The red command
`circleci testsuite run static --run-tests=all -t 'tests/gates/test_cockpit_feed_refresh_adr0254\.py$'`
failed six conversation-index assertions before the fix. The shared repository,
PostgreSQL backfill and native cached-history tests independently reproduced
their failures. The collector also rejects a page/count mismatch before publishing.

A live, read-only preview of the checked-in collector used two authenticated
broker HTTP requests and validated its output against the existing production
feed schema. Publication was replaced by a local preview callback; this verifies
the acquisition and mapping, not a deployed scheduled sandbox execution.

Production diagnosis was read-only. Application/client delivery and conversion
of the existing schedule to the verified collector are separate release steps.
No deployment or milestone completion is claimed by local checks.
