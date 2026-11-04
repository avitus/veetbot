# ADR-0295: An email slice's time starts at its first claim, and the async role runs two processes

- Status: Accepted (the owner chose both repairs, 2026-11-04)
- Date: 2026-11-04
- Related: ADR-0256, ADR-0286
- Amends: `docs/plan/runtime-loop.md`, "`deadline_at` is a copy, not a new
  concept": one kind of run now has its deadline set at its first claim
- Detailed design: `docs/plan/email-experience.md`,
  `docs/plan/runtime-loop.md`, `docs/plan/scheduling.md`; operations in
  `docs/deployment.md`

## Context

On 2026-11-04 the owner reported email refreshes failing. Production showed
refreshes that never got to work:

| Admitted (PST) | Wait for a worker | Outcome |
| --- | --- | --- |
| Nov 3, 11:35 PM | 103 s | `RunDeadlineExceeded` after 8 tool calls |
| Nov 4, 9:18 AM | 209 s | `RunDeadlineExceeded`, no model or tool call |
| Nov 4, 9:22 AM | 1 s | Completed in 94 s |
| Nov 4, 9:24 AM | 199 s | `RunDeadlineExceeded`, no model or tool call |

Three facts combine. A slice's 120 seconds were counted from admission, though
the design calls them an execution deadline. A foreground refresh shares the
async class with scheduled and Cockpit work. And production ran one async
worker, which runs one thing at a time, oldest first. So each app open, which
refreshes the Cockpit and queues its Emerging-market radar for four to five
minutes, could fail a refresh admitted behind it. The Cockpit's own short jobs
failed the same way that morning.

## Decision

1. **`RunLimits.execution_seconds`.** A run may carry a window of working time.
   The first claim sets `deadline_at` to the sooner of the claim time plus the
   window and the deadline set at creation, in the column and in the limits, and
   clears the window in the same statement's transaction. A reclaim after a crash
   therefore keeps that deadline rather than earning a new one. No other run
   changes: its deadline is still computed once at creation.
2. **Unset windows do not serialize.** `execution_seconds` is left out of a dump
   when unset, so agent and delegation digests over `RunLimits` do not move and
   no stored run changes shape. Earlier releases ignore the field.
3. **Every email task but a send uses a window** of its slice, 120 seconds or
   300 for a subscription batch. Its creation deadline is the slice plus
   `EMAIL_QUEUE_ALLOWANCE_SECONDS` (300, one scheduled run's usual deadline), so
   a task still fails if no worker reaches it. A send keeps its agent deadline.
4. **`worker.reserved_async_slots` becomes 2 and takes effect.** The value was
   validated and read by nothing. `agent worker --role async` now starts one
   `--single-slot` process per slot and supervises them: a signal stops them all
   and exits cleanly; a slot that exits takes the rest down with a failure status
   so the unit's `Restart=on-failure` brings back the set.

## Why processes under one unit

A slot is a whole process because the executor shares state across a process's
runs and no process has ever run two at once; two processes already share the
queue safely through its leases. One unit keeps the rollout code-only. A second
systemd unit would need the deploy account's exact sudoers rules reinstalled by
root before the release that uses them, an existence check for the release's stop
that precedes unit installation, and a manual step before rolling back past this
change.

## Consequences

- A refresh behind a long run shows as still updating instead of failing, and is
  rarer with two slots. It fails only if both slots stay busy past 300 seconds.
- Two long runs can still fill both slots. Foreground refresh stays in the async
  class, because the interactive worker has one slot and a 120-second refresh
  there would hold up Chat.
- An in-process composition (`storage="memory"`) has no queue and no claim, so
  its email tasks run to the creation deadline. Production always claims.
- The async unit uses about one more GB of memory (954 MB per process, with 6.3 GB
  available when measured) and a few more PostgreSQL connections, and one more
  process competes for CPU at restart.
- Rolling back needs no step: the earlier code reads no slots, and a run already
  admitted keeps its longer creation deadline.
- Slices that run out of time while working (October 27, November 2) are a
  separate cause, not addressed here.
