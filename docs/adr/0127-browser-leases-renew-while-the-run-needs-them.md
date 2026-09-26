# ADR-0127: Browser leases renew while the run needs them

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: ADR-0058, ADR-0106
- Amends: ADR-0058 decision 15 (run-attempt-scoped leases);
  `docs/plan/browser-automation.md` (hosted session and lease contract)
- Detailed design: `docs/plan/browser-automation.md`
- Amended by: ADR-0129 (decision 3: tool.browser.grant_not_applicable is a
  refusal given before dispatch)

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

A second review found four more:

- **Reattaching restarted the action sequence.** Tool calls always carry
  attempt 1, so a run resumed in another worker reattached to its own live
  lease but sent action 1 again, and the service refused it.
- **The end of an execution read the run's current state.** An owner who
  approved before that read left the run queued or running, and the approved
  action then met a fresh browser.
- **A newcomer run closed a page another run still needed.** With one profile
  pinned for the deployment, a different run's first browser call sealed and
  replaced the lease of a run parked on its own approval.
- **A late close sealed an expired lease.** A close retried after expiry
  reached the service before its sweep, and the service sealed the state.

Separately, the fifteen-minute cap is shorter than an ordinary approval wait.
`browser.act` needs the owner's approval by default, so an owner who answered
after fifteen minutes found the page gone and the action refused.

## Decisions

1. **A repeated acquire for the same run attempt reattaches.** If a live lease
   matches the profile, tenant, principal, provider reference, run, and attempt
   of an acquire, and differs only in the deadline asked for, the service
   returns that lease with its current expiry and action sequence. Any other
   scope still conflicts. The provider continues that sequence, so a run
   resumed in another process reattaches to its own live lease and keeps its
   page.
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
   not close is closed before the provider acquires again, so the next call
   starts from a fresh page.
4. **A lease stays open while its run can still use the page.** That is
   while the run is running, in this worker or another, queued to resume, or
   parked on an approval of its own. When an execution ends, the lease is
   sealed and closed only if the run is done with it: it finished, or waits for
   the user or a delegated child. The run's status is read again after its
   pending approvals, so a concurrent approval or park never reads as an end.
5. **Leases renew while the run needs them.** A lease may be renewed in steps
   of at most fifteen minutes, up to sixty minutes after it was acquired, never
   past the run's own deadline, and only while its run is running, queued to
   resume, or parked on its own approval. The service caps each request at
   fifteen minutes and the
   whole lease at sixty. Renewing an expired or revoked lease fails, and the
   service closes that lease without sealing it. The data plane gains an
   authenticated renew route under the same boundary, body ceiling, and exact
   idempotency rule as acquire and close.
6. **The run worker keeps its leases.** Leases live in the process that ran
   their runs, so each run worker runs a lease upkeep once a minute beside its
   claim loop. The maintenance role is a different process and holds no leases.
   The upkeep closes a lease whose run has ended. It renews a lease within five
   minutes of expiry whose run still needs it.
   Renewal therefore continues while the run waits for the owner, when no tool
   call arrives. A browser call in another session also releases an ended run's
   lease first. In a single process, cancelling a parked run releases its lease
   directly.
7. **A run cannot take a page another run still needs.** When a provider holds
   a live lease for a different run, a newcomer is refused with
   `profile_unavailable` while that run still needs the page. The provider
   seals and replaces the lease only once that run has ended, the lease has
   expired, or no run-state reader is configured.
8. **An expired lease never seals.** The service closes a lease past its expiry
   without sealing its state, whoever asks and however late the close arrives.

## Consequences

- One browser can now serve a run for up to an hour, including an approval
  wait. An approval answered after that finds a fresh browser, and the model
  must navigate again.
- A run cancelled while parked, or failed by the reclaim sweep, holds its lease
  until the next upkeep, about a minute, not fifteen.
- A run resumed after its worker died reattaches to its own live lease and
  continues from its page and action sequence. A different run waits until that
  run ends or the lease expires, at most fifteen minutes after its last
  renewal.
- An expired lease is discarded unsealed, as the lease contract requires. The
  provider does not ask to close one, and the service never seals one.
