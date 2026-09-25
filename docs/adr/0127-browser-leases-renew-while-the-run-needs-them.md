# ADR-0127: Browser leases renew while the run needs them

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: ADR-0058, ADR-0106
- Amends: ADR-0058 decision 15 (run-attempt-scoped leases);
  `docs/plan/browser-automation.md` (hosted session and lease contract)
- Detailed design: `docs/plan/browser-automation.md`

## Context

A hosted browser lease belongs to one run attempt, and the isolated service
caps its deadline at fifteen minutes. Until 2026-09-25 the provider requested
only the current tool call's deadline, so every browser call after the first
got a new blank browser and Veetbot could not click anything. That was fixed by
requesting the run attempt's horizon. An adversarial review of the fix then
reproduced five further faults against the real session service:

- **A lost acquire answer locked the profile.** A retry asked for a later
  fifteen-minute horizon, and the service treated the different deadline as a
  second lease and refused it. The profile, and any login ceremony, stayed
  locked for up to fifteen minutes.
- **A dead lease was never dropped.** After a service restart, a runtime crash,
  or clock skew, every later call reused a reference the service no longer
  honoured.
- **A lost action answer desynchronized the lease.** The service had applied
  the action and advanced its sequence; the provider had not, so every later
  action on the lease was refused.
- **A parent waiting on a delegated child kept its lease.** The child, running
  in another worker, could not acquire the same profile.
- **Runs cancelled while parked, or failed by the reclaim sweep, never released
  their lease.** Those paths do not reach the run worker's completion hook.

Separately, the fifteen-minute cap is shorter than an ordinary approval wait.
`browser.act` needs the owner's approval by default, so an owner who answered
after fifteen minutes found the page gone and the action refused.

## Decisions

1. **A repeated acquire for the same run attempt reattaches.** If a live lease
   matches the profile, tenant, principal, provider reference, run, and attempt
   of an acquire, and differs only in the deadline asked for, the service
   returns that lease and its current expiry. Any other scope still conflicts.
2. **The provider drops a lease it can no longer trust.** On
   `profile_unavailable` or `provider_unavailable` from navigate, observe, or
   act, or on any unexpected failure, it stops using the lease, closes it where
   it still can, and acquires a fresh one on the next call.
3. **An action with no answer retires the lease.** If the service did not
   answer, or answered anything other than a refusal the runtime gives before
   dispatch (`page_changed`, `element_not_found`, `action_not_allowed`,
   `url_disallowed`), the action is `tool.browser.outcome_unknown`, or
   `profile_unavailable` when the service refused the lease itself. The
   provider retires the lease and never retries the action. A lease it could
   not close is closed before the provider acquires again, so a new
   acquisition never reattaches to an out-of-step lease.
4. **Only the run's own approval keeps the page.** A lease stays open while its
   run is running or parked on an approval of its own. When an execution ends
   in any other way, the lease is sealed and closed: the run finished, waits for
   the user or a delegated child, or was requeued after fencing.
5. **Leases renew while the run needs them.** A lease may be renewed in steps
   of at most fifteen minutes, up to sixty minutes after it was acquired, never
   past the run's own deadline, and only while its run is running or parked on
   its own approval. The service caps each request at fifteen minutes and the
   whole lease at sixty. Renewing an expired or revoked lease fails, and the
   service closes that lease without sealing it. The data plane gains an
   authenticated renew route under the same boundary, body ceiling, and exact
   idempotency rule as acquire and close.
6. **The run worker keeps its leases.** Leases live in the process that ran
   their runs, so each run worker runs a lease upkeep once a minute beside its
   claim loop. The maintenance role is a different process and holds no leases.
   The upkeep closes a lease whose run has ended. It renews a lease within five
   minutes of expiry whose run is running or parked on its own approval.
   Renewal therefore continues while the run waits for the owner, when no tool
   call arrives. A browser call in another session also releases an ended run's
   lease first. In a single process, cancelling a parked run releases its lease
   directly.

## Consequences

- One browser can now serve a run for up to an hour, including an approval
  wait. An approval answered after that finds a fresh browser, and the model
  must navigate again.
- A run cancelled while parked, or failed by the reclaim sweep, holds its lease
  until the next upkeep, about a minute, not fifteen.
- A worker that dies still leaves its leases to expire on the service. The
  resumed run can reacquire once they do: at most fifteen minutes after the
  last renewal.
- The provider does not ask the service to close an expired lease; the service
  discards it unsealed, as the lease contract requires.
