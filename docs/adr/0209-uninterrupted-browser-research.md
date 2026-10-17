# ADR-0209: Uninterrupted browser research and visible deadlines

- Status: Accepted under the owner's implementation instruction
- Date: 2026-10-16
- Amends: ADR-0058, ADR-0078, ADR-0168
- Scope: Existing browser, runtime and native approval surfaces; no gate moves

## Context

The owner requested removal of low-risk browser interruptions, reliable iPhone
approval feedback and a result or explicit failure from Friday's X recommendation.
An ordinary post link required approval. The approval outlived the run deadline;
resuming after that deadline recorded cancellation without a visible failure.

## Decision

The application authorizes bounded ordinary interactions in a READY website
profile selected through trusted composition. This permission includes owner
chat and owner-created scheduled research. It does not depend on an approval
card or a task-grant scope. `BROWSER_RESEARCH_ENABLED` defaults to `1`; setting
it to `0` omits research permission; existing follow consent and standing/task
grants still apply. Composition requires the existing bounded
scroll policy rule, with approval for other interactions; custom allow/deny or
unconditional approval rules do not acquire this permission. Like existing
standing grants, this is an application authorization receipt after deterministic
policy evaluation. It does not change the compiled policy or the independent
memory release evidence bound to it.

The application checks the persisted principal or scheduler source, selected
profile, current policy and fresh observation. The isolated browser repeats the
interaction checks on the live page. Existing task-grant field, label, text,
consequence and same-origin checks apply across the current profile origin,
without a task's single-path restriction. Unknown ordinary controls are covered;
named consequences, sensitive paths, credentials, uploads, publication, messages,
payments and destructive actions retain their existing approval or denial rules.
Page content cannot choose the profile or authorize another origin. Each action
has an audit receipt, deadline and dispatch constraint. Existing budgets,
revision fences, effect accounting and uncertain-write non-replay remain intact.

Deadline-bound runs with synthesis reserves begin a tool-free answer phase with
the last fifth of their time remaining, capped at sixty seconds. This takes time
from the existing budget, never extends it. If a top-level run's deadline still
expires, its terminal outcome is FAILED with deadline_exceeded and an actionable
explanation, including when a worker resumes after an approval. Child runs retain
their cancellation outcome so the existing join reports a tool error to the
parent. Approval expiry cannot exceed
the run deadline, so maintenance can end an abandoned wait promptly.

Native approval state is monotonic: a delayed pending read cannot replace a
stored resolution. Resolution events refresh the stored card on every device.
After a successful decision, the client reconciles the run's current state;
errors and late decisions remain visible. Old cancellation events with a deadline
reason also display a deadline failure, without rewriting historical records.

## Verification

The original backend regressions failed on approval suspension, four-hour
approval expiry, silent deadline cancellation and missing time headroom. The
native regressions reproduced a stale pending card and a missed resolution from
another device. After repair, 170 focused backend/configuration tests and all
714 Apple tests pass. The browser boundary cases cover the dated post link,
scheduler provenance, changed controls, stale pages and excluded consequences.
Final sidecar verification and the authorized PR's hosted checks provide the
remaining release evidence; no production state is changed by this repair.
Production acceptance requires delivery and a completed scheduled recommendation,
or an explicit evidence/deadline failure.
