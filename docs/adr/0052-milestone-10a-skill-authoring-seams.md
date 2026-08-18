# ADR-0052: Milestone 10A governed skill-authoring seams

- Status: Proposed
- Date: 2026-08-17
- Related: Section 30, Milestone 10, ADR-0013, ADR-0030, ADR-0044
- Detailed design: `docs/plan/skills.md`

## Context

Milestones 0 through 9 provide the immutable skill package, catalog, sandbox,
policy and approval pipeline, event log, durable queue, and evaluation harness.
The remaining self-authoring work was grouped under a Milestone 10 containing
three unrelated optional extensions and therefore had no delivery contract of
its own. The owner authorized self-authored skills independently.

Three seams must be fixed before implementation. The detailed skill design and
ADR-0030 disagree about the idempotency key; a background child run needs a
placement that cannot contend with the user's active session; and a newly
created skill needs an activation boundary that does not let authoring rewrite
its own `AgentSpec`.

## Proposed decisions

1. **Milestone 10A is self-authored skills only.** Scheduling, new model-routing
   behavior, `delegate.run`, and general-purpose subagents remain unauthorized.
2. **Construction and rollout are separate.** Foreground authoring and
   background review have independent default-off settings. Background review
   requires foreground authoring. Tenant activation additionally requires the
   recorded evaluation evidence in `skills.md`.
3. **Invocation identity plus canonical arguments is the idempotency key.**
   `expected_revision` is only optimistic concurrency. Reusing an invocation
   identity with different arguments is a conflict; two distinct edits that
   expect the same revision race normally and exactly one wins.
4. **`source = AGENT` is the authoring ownership boundary.** Provenance retains
   the principal and invocation identity durably, plus the authoring run until
   governed session erasure removes that event history. No
   `authored_by_agent_id` column is added. Archive has a separate durable
   invocation marker so crash replay does not overwrite creation provenance.
5. **A background review always uses a dedicated child session.** It records
   `parent_run_id`, never joins or mutates the completed parent, has a bounded
   deadline and budget, and receives the parent transcript as enveloped data.
6. **Background review has no archive permission.** Its tool set is the
   union of `memory.*`, `skill.load`, and the `create`, `edit`, and `patch`
   operations of `skill.manage`. Edit and patch additionally require the review
   to have loaded the current agent-authored revision. The `archive` operation
   is never available to background review and remains a foreground/operator
   action.
7. **A create produces a disabled candidate.** `skill.manage` never edits
   `AgentSpec`. An operator enables a new skill in a later agent version. A
   revision of an already-enabled floating skill appears only in a new session.
8. **Approvals render a canonical diff.** `ActionKind.SKILL_AUTHORING` retains a
   non-null tool invocation id and presents name, operation, current revision,
   proposed revision, and a bounded unified diff. Raw archive bytes never enter
   approval text or events.
9. **The rollout threshold is quantitative and versioned.** Thirty paired
   samples, at least five absolute percentage points of task-completion lift, a
   positive 95 percent Clopper-Pearson lower bound, and zero additional policy failures
   are required for each model-policy, policy-profile, and authoring-version
   combination.

## Consequences

- The authoring implementation can use the existing repository, package store,
  tool pipeline, approvals, and queue without weakening their invariants.
- Newly created skills require an explicit operator activation step. This is
  deliberate friction and prevents a write from expanding its own future
  context.
- Background review consumes a session row and a run row of its own, but cannot
  block a new user run in the parent's session.
- The feature may be fully implemented and tested while remaining unavailable
  in ordinary deployments.

## Alternatives considered

- **Authorize all of Milestone 10:** rejected because the optional extensions do
  not share entry evidence or acceptance criteria.
- **Use `expected_revision` as the idempotency key:** rejected because distinct
  concurrent edits commonly share it.
- **Run the review in the parent's session:** rejected because it can contend
  with the one-active-run constraint and with the user's next turn.
- **Automatically enable a created skill:** rejected because it silently edits
  agent configuration and collapses authoring, review, and activation into one
  action.
- **Let reviews archive:** rejected for the first tranche; false-positive
  autonomous withdrawal has a wider effect than proposing a new revision.
