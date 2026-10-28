# ADR-0266: Spent one-time repairs are retired

- Status: Accepted (the owner asked for it on 2026-10-28, after the
  production reads below)
- Date: 2026-10-28
- Related: ADR-0116, ADR-0122, ADR-0123, ADR-0126, ADR-0134, ADR-0140,
  ADR-0250
- Amends: ADR-0134 (the re-key "stays as long as older chats can resume");
  ADR-0250 decision 4 (the reshape command); ADR-0116 decision 4 (`agent
  email exclude-bulk` "remains available"); the retention of a previous tool
  version "for pinned chats" that ADR-0122, ADR-0126, ADR-0140 and the
  browser, schedule, skill, delegation and memory tool bumps recorded
- Detailed design: `docs/plan/context-engine.md`,
  `docs/plan/builtin-tools.md`,
  `docs/plan/memory-formation-and-consolidation.md`,
  `docs/plan/email-experience.md`

## Context

The 2026-10-28 repository audit (PR 207) listed the code that exists only to
repair or bridge a state production no longer has. Five items were settled
with read-only production queries over the latest context plan of every
session (2,676 plans), the parked runs, the host environment, and Doppler:

- **The ADR-0134 plan re-key.** A plan is reused only while its builder
  version is current; every other plan re-plans from the live registry at the
  chat's next message. 83 plans still hash unsorted scope lists, all planned
  between 2026-09-12 and 2026-09-25. Since `context-builder@13` changed the
  framing row, none of them renders its stored bytes, so the re-key branch
  can no longer match; it fired once in production, ever.
- **Previous tool versions kept registered.** Thirteen `Legacy*` classes and
  one `Previous*` class retained old contracts for chats whose plan pinned
  them. Only three of those versions are pinned by a plan on a reusable
  builder (`context-builder@11`: 39 chats planned 2026-09-17 to 09-25), and
  those plans fail the same framing recheck. No run parked on an approval
  pins any of them. Twelve other old versions are already pinned in
  production with no registered class, so a pinned version without a class is
  the normal case, not a new hazard: such a chat re-plans.
- **`agent memory reshape` (ADR-0250).** Ran on 2026-10-26 and superseded all
  55 `owner-source-learning@1` rows; nothing forms that shape any more.
- **`agent email exclude-bulk` (ADR-0116).** Never ran: its preview listed
  450 threads and the 2026-09-23 cleanup used `agent memory delete` instead.
- **`AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED`.** A shim mapping a retired
  boolean onto `AGENT_MEMORY_PROVIDER_EXTRACTION_MODE`. The name is absent
  from the production environment file, every unit's environment, Doppler's
  production config and the deploy templates.

## Decision

1. The planner no longer re-keys a plan hashed with unsorted scopes and no
   longer treats `context-builder@11` as equivalent to the current builder.
   A plan from any earlier builder rotates through the ordinary logged epoch
   with `builder_version_changed`. `prefix_bytes` has no recorded-order mode.
2. A tool version bump no longer keeps the previous contract registered. The
   fourteen retained classes are deleted with the v1 People tool schemas. A
   chat whose plan pins a version the registry lacks re-plans at its next
   message; a run parked on an approval whose plan pins one fails with
   `tool_pin_mismatch`, as any re-plan under a parked run does today. The
   Cockpit's `schedule.create` 1.5.0 and `schedule.list` 1.0.2 stay: they
   are the versions a deployment without the Cockpit flag offers, not
   retained history.
3. `agent memory reshape` and `SourceLearningService.reshape` are removed.
   Receipts are still read under both policy versions. A backup restored from
   before 2026-10-26 would bring back superseded `@1` rows as live rows; they
   expire within thirty days, and the `@1` identity rule still blocks a
   deleted or corrected claim from returning as `@2`.
4. `agent email exclude-bulk` and `EmailService.exclude_bulk_sources` are
   removed. Governed deletion of each derived belief remains the cleanup path,
   and per-thread source exclusion is unchanged.
5. The `AGENT_MEMORY_PROVIDER_EXTRACTION_ENABLED` shim is removed; only
   `AGENT_MEMORY_PROVIDER_EXTRACTION_MODE` is read.

## Consequences

- A dormant chat from before 2026-09-26 re-plans once on its next message,
  which it already did for every other reason; no cache or history is lost.
- Future tool bumps follow the same rule: the new version replaces the old at
  the registry, and the planner's prefix recheck moves pinned chats to it.
  Retaining a previous version needs a stated reason in its ADR.
- The evidence for each removal is the production read recorded here; the
  CLI help, the deployment runbook and the plan documents name none of the
  five.
