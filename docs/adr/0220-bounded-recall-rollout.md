# ADR-0220: Owner-reviewed bounded recall rollout

- Status: Accepted scope by the owner on 2026-10-19; activation awaits evidence
- Related: ADR-0178, ADR-0185, ADR-0199, ADR-0212, ADR-0218
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision and evidence

The completed comparison retains all 1,600 observations across 400 scenarios.
It fails the original quality thresholds. Held-out required-evidence recall is
76.744% for both hybrid paths, versus 66.047% automatic and 63.256% deliberate
sparse control. Strict answer success is 44% and 44.5%, versus 41.5% and 38%.
The original 95% recall and ten-point implicit-answer-lift goals remain failed.
The sparse control is not an exact measurement of the current production tree.

The owner explicitly approved a bounded rollout with revised acceptance criteria,
human failure review, production compatibility checks, a recall pull request,
deployment with rollback, and continued improvement after release. The owner
will perform the human review. That designation is not a completed review.

This release-specific exception preserves the original scorer and exact-input
binding. It does not relabel their failed results, advance milestone completion,
authorize resampling, or transfer old evidence to an arbitrary descendant.

## Separate admission

A separately versioned bounded-rollout artifact must bind the original complete
checkpoint comparison, all interrupted attempts and uncertain charges, actual
human findings and the owner's rollout authorization. Recompute complete matrix
coverage, original measurement identities, incident results, and paired metrics.
Both held-out paths must improve ranked and rendered evidence recall over their
sparse controls and must not reduce overall strict answer success. The original
numerical goals and all deviations remain reported, including family regressions,
invalid-current flags, and attributed per-case cost overruns. The human must
explicitly accept their bounded residual risks; an empty review or aggregate
approval without the linked failed evidence cannot supply that decision.

An exact source found during search but omitted from a certified final context
is candidate evidence only. Its original absent rank and unrendered status stay
unchanged and receive no ranked, rendered or answer credit. This bounded route
retains those negative observations; it still rejects uncertified global order
or an unranked source claimed as rendered. Legacy admission remains unchanged.

Access boundaries, erasure, correction and lifecycle enforcement are unchanged.
No observed access-boundary failure is admissible. Context, search/round and
latency limits remain enforced; known-incident answers must still succeed. Every
charge and uncertain reservation remains counted. A measured per-case cost
exception is not permission to increase runtime work budgets or forgive a bill. A failed
operational gate additionally requires its exact hash-bound original report,
matching identity and check counts, with only the measured per-answer cost check
failing. Missing evidence or any retry, deadline, cancellation, partial-failure,
context, search or latency failure refuses bounded admission.

Use the ADR-0218 compatibility inventory to enumerate all measured/target input
differences with individual hash pairs, dispositions and actual verification.
Bind the final reviewed target inputs, effective configuration, schema, reader,
formation and local embedding tuple independently at composition. Missing files,
unassessed changes or mismatched configuration refuse admission. The assessment
is explicitly compatibility evidence, never a claim of byte equality.

## Rollout bounds and rollback

The first admission lasts at most seven days from its fixed activation time and
names one tenant and owner principal. It permits only the existing local encoder
and authenticated private interactive owner recall. Scheduled, delegated and
other-principal query work cannot use this exception. Source recovery retains its
current local-reader, visibility and correction checks. Existing request and
daily indexing budgets remain, with the existing maximum USD 2 daily ceiling; the local
encoder makes no paid embedding request. Unattended reconsolidation stays off.

Admission checks expiry on each use. Evidence, manifest or target-source mutation,
loss of authority, or expiry withdraws optional semantic processing and cached
query authority. Withdrawal stays latched for that composed admission handle;
restoring a file cannot revive the handle. Restarting composition revalidates the
original evidence and fixed expiry and cannot extend the admission. The established sparse
path remains available without advertising a successful semantic result.

Before activation, retain the previous production configuration and a compatible
application/documentation rollback target, rehearse withdrawal and restoration
without destructive database downgrade, and verify the current deployment tree.
After delivery, verify public release identities and actual owner-visible recall.
Withdraw the feature immediately for a confirmed access/lifecycle violation or
an unacceptable obsolete-fact incident, and retain the failure evidence. Record
the seven-day result and seek an explicit renewed decision rather than silently
extending expiry. New recall improvements use development cases and independent
evaluation; they cannot overwrite or tune against this held-out result.

## Verification requirements

Red-first tests cover missing human findings, incomplete evidence, mismatched
source/configuration/principal, unassessed differences, expiry, immutable
withdrawal, local-only composition and interactive-only authority. Preserve all
legacy refusal tests. Synthetic test reviews never become operator evidence.
CodeRabbit's GitHub review, every required final-head CI job, deployment checks
and the actual human review remain required before activation.
