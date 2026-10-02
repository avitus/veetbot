# ADR-0143: Notification attention and reconciliation

- Status: Proposed
- Date: 2026-09-30
- Related: ADR-0062, ADR-0091
- Detailed design: `docs/plan/notifications-and-devices.md`

## Context

The owner requested the full notification recommendation on 2026-09-30:
allow time to answer on another device, suppress foreground conversation
alerts, clear obsolete delivered alerts, and share acknowledgement of viewed
scheduled results. Immediate fan-out rings every device before an interactive
answer can supersede the request. APNs does not provide reliable remote recall.

## Decision

1. Keep atomic enqueue, but make approvals, questions, and scheduled outcome
   notifications eligible after thirty seconds. Device invocation wake-ups,
   test notifications, failures and operational alerts keep their existing timing.
2. Check current relevance before each transport send and during client sync.
   Resolved approvals, answered questions, expired notifications, deleted
   subjects and acknowledged terminal results are obsolete. Delivery history
   remains intact; an obsolete pending row settles superseded.
3. Add an idempotent, principal-scoped terminal-run read receipt, independent
   of notification creation. This covers a result viewed before schedule
   accounting enqueues its notification. Only an authorized terminal run can
   be acknowledged; viewing a running conversation never acknowledges its
   future result. Receipts are erased with the run.
4. Add `POST /v1/notifications/sync` under `notification.write`, behind the
   existing notification API flag. A bounded request names delivered notification
   IDs and terminal run IDs actually displayed; the response names only supplied,
   owned notifications now obsolete. Unknown or foreign IDs do not authorize
   removal. A receipt is not approval resolution or question input.
5. Native clients reconcile when active, on conversation changes and relevant
   stream events, and periodically while active. Only the visible Chat transcript
   can acknowledge its terminal run. A selected background conversation, Email
   mode, a covered transcript and a failed restore cannot acknowledge a result.
   Foreground presentation suppresses only a structurally valid push matching
   the visible, loaded conversation. Other notifications present normally.
6. Remove only matching operating-system request identifiers confirmed obsolete
   by the server, guarded against connection changes. Network failures and older
   servers leave delivered notifications intact and retry on a later sync.

## Consequences

This extends the client-only registration and rendering restriction of ADR-0062
with server-authoritative reconciliation and introduces `notification.write`.
It does not add device presence routing, new transports, silent pushes or lock
screen actions. Sleeping devices clean up on their next active sync; a banner
already presented cannot be undone. The send/resolve network race remains best
effort, narrowed by checking before each target. Existing gate counts do not move.

## Verification

The producer regression first observed immediate eligibility instead of the
thirty-second deadline; the API regression first observed 404 for sync; native
regressions first observed foreground presentation and absent cleanup. The
fan-out regression observed a resolved notification settled as dispatched; it
now preserves the first delivery, skips the remaining device and settles
superseded. The focused notification partition and native attention tests pass.

`tests/unit/test_notification_attention.py` covers receipts before enqueue,
repeat acknowledgement, unresolved approvals, scope checks, bounded bodies,
foreign runs and all-or-nothing validation. The shared outbox contract covers
principal-isolated receipt and notification reads; its PostgreSQL binding also
checks run-deletion cascading and forced tenant RLS. The existing migration
suite checks empty-database schema parity and downgrade/re-upgrade.
`NotificationAttentionTests.swift` covers selective OS removal, batched cleanup,
failed requests and a connection change during the network response. The
ChatViewModel journey uses the real HTTP client to prove only a loaded visible
transcript acknowledges its replayed terminal result.

## Amendment: scheduled report indicators (2026-10-01)

The owner requested a subtle new-report indicator that clears across devices
when read. Extend sync with a bounded `query_run_ids` array and additive
`unread_run_ids` response, derived from completed runs and the existing
principal-scoped receipts. Unknown and foreign IDs are indistinguishable.
Queries are read-only; acknowledgements remain restricted to loaded terminal
results. No new table, scope, notification kind or milestone gate is needed.

The native scheduled row and its collapsed group display a six-point dot,
labelled “New report” for accessibility. Active polling refreshes the history
index and receipt state, while connection changes discard presentation state.
Older servers retain their existing acknowledgement path. Offline devices
converge on their next active sync; instantaneous background clearing is not
promised. This extends ADR-0113's presentation with server-authoritative read
state and leaves group expansion device-local.

The API regression first failed because the old sync route rejected
`query_run_ids` with HTTP 400. It covers independent clients sharing receipts,
idempotence, a newer unread result, exclusion of noncompleted and foreign runs,
and bounded requests. Native tests cover batching, read-state replacement,
connection races, offline retention, older-server fallback and acknowledgement
through the loaded-transcript path.
