# ADR-0063: Milestone 13 general-purpose subagents and delegation

- Status: Proposed
- Date: 2026-08-20
- Related: Sections 6.5, 21, 26, 27.5, and 27.6 of the engineering plan;
  ADR-0004, ADR-0009, ADR-0021, ADR-0023, ADR-0052, ADR-0056, ADR-0059,
  ADR-0061, ADR-0062
- Detailed design: `docs/plan/subagents-and-delegation.md`

## Context

The plan designed subagents under Milestone 10 as `delegate.run`, a control
tool that spawns a child run, with nine required child-run properties, and
deferred them behind the gate for multi-agent work: add subagents only when
evaluation evidence shows a single agent fails for one of five named reasons.
The tool system, the run loop, the context engine, and the memory retrieval
design each already carry their share of the mechanism — the control-tool
contract, the `CHILD_RUN` suspension kind in place of an eighth state, the
post-terminal join, the untrusted result label, the brief-seeded child context,
the smaller recall class — and Milestone 10A built a confined child run for
background skill review. The readiness review measured four gaps (no brief
schema, no child-limits rule, no carrier for the separate trace or artifact
references) and one conflict (a child run cannot be inserted into its parent's
session under the one-active-run index).

The owner authorized Milestone 13 on 2026-08-20 (ADR-0061), after Milestone
12, for long research and multi-part tasks in a personal daily-driver
deployment.

## Proposed decisions

1. **Construction now, activation on evidence.** The gate for multi-agent work
   stands as written. `delegate.run` is built default-off; tenant activation
   requires a capability scenario admitted from a real failed trajectory whose
   delegating re-run scores above its single-agent baseline, and a
   deterministic two-arm case whose delegating arm improves the outcome with no
   added policy failure. This is ADR-0052's construction-versus-rollout split
   applied again.
2. **The input is an ordered list of structured briefs, one child each.**
   Each brief carries objective, success condition, optional inline context and
   artifact references, an explicit allowed-tool subset, and optional limits;
   the request carries the briefs and a return shape — because the child
   seeds from its brief and nothing else, and fan-out from one suspending
   call is how independent parallel work happens. A plain string and a
   one-child-per-call tool were both rejected.
3. **A child always gets a dedicated session.** Section 27.6's "or the parent's
   session per policy" is deleted; the one-active-run index is neither widened
   nor contended. Child sessions appear in the session index with metadata and
   are not hidden.
4. **Materialization is one transaction.** Child session, child run, seed
   message, seed checkpoint, ledger row, and the parent's suspended invocation
   commit together, in the scheduling materializer's shape; a retry with the
   same invocation identity returns the existing delegation.
5. **No new run status; one new requeue edge.** The parent waits in
   `WAITING_FOR_APPROVAL` with a `CHILD_RUN` suspension, as the run loop
   decided; the join lives in the post-terminal hook and re-queues the parent
   through a `requeue_after_child` edge in the single terminal writer, because
   no other module may transition a run.
6. **Limits are derived, usage is additive, cost is reserved.** Each child
   limit is the minimum of the requested or default value and the parent's
   remaining value; the child deadline is bounded by the parent's; the sum of
   children's maximum cost is reserved against the parent at materialization;
   the parent's usage is debited by each child's terminal usage at the join.
7. **Scopes and tools are subsets; results are untrusted.** Child scopes are the
   parent's intersected with what the allowed tools require; the child's tools
   are a subset of the parent's pinned set and never include `delegate.run`
   or `skill.manage`; every child result enters the parent labelled
   `EXTERNAL_UNTRUSTED` and cannot authorize.
8. **Depth one, capped fan-out, tenant admission, async priority.** Three
   children per call and eight per parent are starting values; a child never
   takes an interactive slot.
9. **A `delegations` ledger carries the separate trace and artifact
   references.** The child's own events live only in its session; the parent
   holds the link, the status, the bounded summary, and artifact identifiers.
10. **One scope, no route, one flag.** `run.delegate` gates the tool;
    `AGENT_DELEGATION_ENABLED` leaves the tool unregistered when off; the run
    view's existing `parent_run_id` and the suspension payload carry the
    parent–child link to clients.
11. **Cancellation cascades downward only; a failed child is a tool error.**
12. **One gate area, `delegate`, twenty-one gates.**

## Consequences

- Long tasks can fan out to bounded, isolated children without a new queue, a
  new state, or a new trust level.
- One table, one run kind value, one scope, one flag, a `delegation:` limits
  block, and two evaluation artifacts are added.
- The terminal writer's finalize and resume paths learn the `child_run`
  suspension kind; missing either would leave a parent stuck or emit a
  spurious approval request, which gates 4 and 12 exist to catch.
- Section 27.6 of the plan is tightened (ADR-0061, decision 7).
- The owner must supply one real, redacted failed long-research trajectory;
  without it the milestone is buildable but not activatable, which is the gate
  working as intended.

## Alternatives considered

- **Handoffs or role-named agents:** rejected by the plan's own text.
- **A `WAITING_FOR_CHILD` status:** rejected by the run loop's standing
  decision; the typed suspension keeps the state machine and every projection
  unchanged.
- **A child in the parent's session:** rejected; it needs the one-active-run
  index predicate widened, and that predicate is what keeps the invariant.
- **Unlimited depth or fan-out:** rejected; runaway cost is the primary risk.
- **Interactive priority for children:** deferred as an open question;
  conservative start at async priority.
- **Activating on construction alone:** rejected; the plan's gate names
  evidence, and the evidence is cheap to record once the tool exists.
