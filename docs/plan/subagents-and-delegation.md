---
title: Subagents and Delegation
status: design
canonical: true
---

# General-purpose subagents and delegation

This document specifies Milestone 13. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md), and it reuses rather than replaces
the run loop, the single terminal writer, the tool pipeline, the policy engine,
the event log, the scheduling materializer's one-transaction pattern, and the
confined child run Milestone 10A already builds for background skill review.
[ADR-0063](../adr/0063-milestone-13-subagents-and-delegation.md) records the
architectural decisions; ADR-0061 records the authorization.

The plan designed subagents under Milestone 10 and deferred them behind a gate:
"add subagents only when evaluation evidence shows that a single agent fails"
for one of five reasons — independent parallel work, context isolation,
specialized permissions, specialized tools, or independent verification
(engineering-plan.md:2994-3004). This document honours that gate as written.
Construction is authorized now; tenant activation requires the evidence the
gate names, and the evidence is part of the milestone rather than a
precondition for starting it, because the platform has to be able to delegate
before a delegating arm can be measured.

What the corpus already decided is most of the mechanism. `delegate.run` is a
control tool that spawns a child run and suspends the parent
([tool-system.md](tool-system.md#control-tools)); the parent waits in
`WAITING_FOR_APPROVAL` carrying a typed `CHILD_RUN` suspension rather than an
eighth run state (runtime-loop.md:284-292); the child-run join is the one
post-terminal hook that is genuinely part of the run lifecycle
(runtime-loop.md:1136-1147); a child's result enters the parent labelled
`EXTERNAL_UNTRUSTED` and the child's tool set is resolved with the child's
principal (tool-system.md:971-977); the child seeds from the parent's concise
instruction and recalls under its own, smaller, recall class
(context-engine.md:291-294, memory-retrieval-and-ranking.md:87); and the
background-review child run of Milestone 10A already materializes a dedicated
child session and child run with a restricted tool allow-list and
failure isolation ([skills.md](skills.md#the-background-review-is-a-child-run-with-four-restrictions)).
The readiness review measured what was left (readiness.md:986): the
objective had a carrier and no schema, the child budget was additive with no
rule deriving a child's own limits, the separate trace and the artifact
references were picked up by no specification, and a child run could not be
inserted into its parent's session. This document supplies those four and
resolves the fifth.

## Scope

Milestone 13 delivers `delegate.run` as a suspending control tool that
materializes bounded child runs. It includes:

- a structured brief as the tool's input: objective, success condition,
  optional inline context and artifact references, an explicit allowed-tool
  subset, optional limits, and a return shape;
- a dedicated child session and child run created in one transaction, with
  `parent_run_id`, scopes intersected with the parent's, limits derived from
  the parent's remaining budget, and a deadline no later than the parent's;
- parent suspension while children run, a join that completes the parent's
  invocation exactly once with each child's concise result and artifact
  references, and resumption through the single terminal writer;
- cancellation and deadline propagation from parent to children;
- depth and fan-out caps, per-tenant admission, and additive usage accounting;
- a `delegations` ledger that carries the separate trace and the artifact
  references the plan requires;
- a default-off flag that leaves the tool unregistered;
- the evaluation evidence the gate for multi-agent work requires: a capability
  scenario admitted from a real failed trajectory and a deterministic two-arm
  case whose delegating arm improves the outcome without adding policy
  failures.

The milestone does not include handoffs (the parent retains the user
interaction and the final response, engineering-plan.md:2992); role-named
agents for planning, writing, or criticism (engineering-plan.md:3004);
delegation deeper than one level; cross-tenant or cross-principal delegation;
any change to model routing; a new `WAITING_FOR_CHILD` run status; a child that
may itself call `delegate.run` or `skill.manage`; or push notification of child
events, which is Milestone 12's contract and reaches the parent's principal
through the parent's own approval and question triggers.

## The boundary: the parent delegates; the child is an ordinary run

A child run is an ordinary durable run. It is leased, checkpointed, fenced,
budgeted, cancellable, recoverable, and audited by the same components as an
interactive run, in its own session, under the same principal, with a subset of
the parent's scopes and tools. The parent does not poll; it is suspended, and
the runtime wakes it when the last child is terminal:

```text
parent run ---- delegate.run ---- one transaction ----> child session
    |                                                   + child run (QUEUED)
    |  suspended: WAITING_FOR_APPROVAL, kind CHILD_RUN   + seed message
    |                                                   + seed checkpoint
    |                                                   + delegations row
    v                                                         |
 released lease                                               v
                                              existing queue, worker, loop
                                                              |
                                                              v
                                      child terminal ---- join ---- parent
                                                          invocation completed,
                                                          parent re-queued
```

This yields four load-bearing invariants:

1. A committed delegation either has its committed child session, child run,
   seed, and ledger row, or has none of them; the parent's invocation is
   marked suspended in the same transaction.
2. A child has no wider authority than its parent: scopes are intersected,
   tools are a subset of the parent's pinned set, limits are bounded by the
   parent's remaining budget, and the deadline is bounded by the parent's.
3. The parent resumes exactly once, through the single terminal writer, with
   every child's result labelled `EXTERNAL_UNTRUSTED`; a child cannot alter
   its parent's outcome or history by any other path.
4. Delegation is default-off. When the flag is off the tool is not registered,
   so the advertised set, the policy evaluation, and every existing gate that
   counts the registry are unchanged.

## The brief

The plan's "explicit objective" requirement had a carrier and no schema. One
`delegate.run` call carries an ordered list of briefs, one child per brief,
because independent parallel work — the first of the gate's five reasons — is
exactly fan-out from one invocation, and a parent that suspends on the call
cannot fan out any other way. Each brief is structured, because the child
seeds from its brief and from nothing else (context-engine.md:291-294), so
everything the child needs to stop correctly has to be in it:

```python
class DelegationReturn(StrEnum):
    SUMMARY = "summary"
    SUMMARY_AND_ARTIFACTS = "summary_and_artifacts"


class DelegationLimits(BaseModel):
    max_steps: int | None
    max_model_calls: int | None
    max_tool_calls: int | None
    max_cost: Decimal | None
    wall_seconds: int | None


class DelegationBrief(BaseModel):
    objective: str                      # 1..4096 characters
    success_condition: str              # 1..2048 characters
    context: str | None                 # 0..16384 characters, inline
    context_refs: list[UUID]            # artifact ids, at most 8
    allowed_tools: list[str]            # 1..16 registry names
    limits: DelegationLimits | None


class DelegationRequest(BaseModel):
    briefs: list[DelegationBrief]       # 1..max_children_per_call, ordered
    return_shape: DelegationReturn      # default SUMMARY
```

`DelegationRequest` is the tool's input; each brief becomes exactly one child,
in order, and the result carries one `ChildOutcome` per brief in the same
order.

`allowed_tools` must be a subset of the parent's pinned tool set minus
`delegate.run` and `skill.manage`, resolved against the registry view the
parent was advertised. `context_refs` name artifacts the parent's principal can
read; the child receives read access to exactly those. The request is validated by the ordinary schema validator before policy, like
every other tool input, and a request in which any brief fails validation — or
whose brief count exceeds the per-call cap — is a tool validation error with a
stable reason code under `delegation.*`, and no child is created for any of
its briefs.

The `ToolSpec` for `delegate.run` is `kind = CONTROL`, `side_effect = NONE` (its
effect is a run-state transition, which is what the control kind means),
`idempotency = IDEMPOTENT` (a retried call with the same invocation identity
returns the existing children), `output_trust = EXTERNAL_UNTRUSTED`,
`allow_parallel = false`, and `required_scopes = {"run.delegate"}`, a new exact
platform scope. The tool's output is the join result below.

## The child

Materialization generalizes two things the corpus already has: the
background-review child run (skills.md:1086) and the scheduling materializer's
one-transaction session-plus-run creation ([scheduling.md](scheduling.md#materialization-transaction)).
In one unit of work, `DelegationMaterializer` does the following:

1. Validates every brief against the parent: tools are a subset, depth is
   zero (the parent is not itself a delegated run), the number of briefs is
   within the per-call cap, the per-parent live-children cap holds for all of
   them together, and the tenant's active-children cap holds.
2. Derives each child's limits and deadline (below) and reserves the sum of
   the children's `max_cost` against the parent's remaining cost; if the sum
   cannot be reserved, no child is created.
3. For each brief, in order, creates the child `Session` with metadata
   `{"run_kind": "delegated", "parent_run_id", "parent_session_id",
   "delegation_id"}` and a title derived from the objective's first line, and
   appends `session.created`.
4. Creates the child `Run` with `kind = DELEGATED`, `parent_run_id`, the
   intersected scopes, the derived limits and deadline, and async priority 10
   (a child never takes an interactive slot from a human), through the
   ordinary queue.
5. Appends the seed `user.message.created` carrying the brief as enveloped
   data — objective, success condition, and inline context wrapped exactly as
   the review child wraps its transcript — and sets the session's seed event
   sequence.
6. Appends `run.queued` with `parent_run_id`, `run_kind`, and `delegation_id`,
   and seeds the child's checkpoint through the injected checkpoint seeder.
7. Inserts one `delegations` row carrying every child and marks the parent's
   `tool_invocations` row `RUNNING` with `suspended_kind = child_run` and
   `suspended_ref` equal to the delegation identifier (the nullable columns
   tool-system.md:963-966 already declares). Steps 3 through 6 repeat per
   brief inside the one transaction.
8. Commits, then dispatches the child through the existing run dispatcher.

A crash before commit leaves no child, no ledger row, and an unsuspended
parent invocation, so the step is retried as an ordinary tool failure; a crash
after commit leaves one complete delegation, and a retry with the same
invocation identity returns it.

The child's agent is a derived `AgentSpec` in the shape the review child uses:
the parent's spec with `enabled_tools = allowed_tools`, instructions replaced
by the objective and success condition, a metadata `run_kind`, and a
deterministic identifier and version derived from the parent's. The child's
policy profile is the parent's. The child recalls memory under the child-run
recall class, never the parent's conversation.

## Suspension and join

No new run status. The parent's step ends when the pipeline raises a
`ChildRunRequired` outcome; the loop and the executor produce
`RunOutcome(SUSPENDED)` with `Suspension(kind = CHILD_RUN, invocation_id,
child_run_ids)`, the executor writes `WAITING_FOR_APPROVAL` and the
`run.waiting_for_approval` event whose payload carries the suspension kind and
child identifiers, and releases the lease exactly once, as
runtime-loop.md:1000-1030 specifies for all three suspension kinds. The single
terminal writer's finalize path, which today treats every non-user suspension
as an approval, branches on the kind: a `CHILD_RUN` suspension appends no
`approval.requested` and enqueues no Milestone 12 notification.

The join is the post-terminal hook runtime-loop.md:1136-1147 describes. When a
`DELEGATED` child reaches a terminal state, the hook checks whether every
sibling under the same `parent_run_id` is terminal. If so, it completes the
parent's suspended invocation as `tool.call.completed` with one result item
per child — `ToolResultItem(trust = EXTERNAL_UNTRUSTED, content = [summary])`
— and a structured result:

```python
class DelegationResult(BaseModel):
    delegation_id: UUID
    children: list[ChildOutcome]


class ChildOutcome(BaseModel):
    child_run_id: UUID
    child_session_id: UUID
    status: RunStatus               # COMPLETED | FAILED | CANCELLED
    summary: str | None             # the child's final message, bounded
    artifact_refs: list[UUID]
    usage: UsageSummary
    failure_reason: str | None
```

`summary` is the child's final assistant message truncated to a configured
byte ceiling; `artifact_refs` are the artifacts the child exported, which the
parent's principal can read through the ordinary artifact routes. The child's
transcript is never copied. The parent is then re-queued through a new
`requeue_after_child` edge in the terminal writer, beside
`requeue_after_approval` and `requeue_after_input`, because the
one-terminal-writer gate admits no other transition site. A failed or
cancelled child completes the invocation as an error result with the child's
failure reason; it is a tool error the parent reads, not a parent failure.
`FailureReason.CHILD_RUN_FAILED`, which the domain already declares, is
reserved for the join itself failing.

Two children finishing concurrently race on the join; the join is idempotent
on `delegation_id` and the invocation's row lock, so the parent's invocation
completes once and the parent re-queues once.

## Limits, budget, and deadline

The plan requires a child budget and a child deadline and says fan-out usage
is additive (engineering-plan.md:593). The rule that derives a child's own
limits — the partial the readiness review named — is:

```text
child.max_steps        = min(requested or default, parent.remaining_steps)
child.max_model_calls  = min(requested or default, parent.remaining_model_calls)
child.max_tool_calls   = min(requested or default, parent.remaining_tool_calls)
child.max_cost         = min(requested or default, parent.remaining_cost - reserved)
child.deadline_at      = min(parent.deadline_at, now + (requested or default wall_seconds))

research.max_steps       = child.max_steps - synthesis_reserve_steps
research.max_model_calls = child.max_model_calls - synthesis_reserve_model_calls
research.max_cost        = child.max_cost - synthesis_reserve_cost
```

The rule is applied per brief, in order, against what remains after the
briefs before it have been reserved. Defaults come from a `delegation:` block
in the versioned limits file beside the `scheduling:` block, and every value
must be positive: a parent with nothing remaining cannot delegate. At
materialization the materializer reserves the sum of the children's `max_cost`
against the parent's remaining cost before creating any child, so three
children cannot each be granted the whole remainder. At the
join, the parent's usage is debited by each child's terminal usage through the
existing usage-recording path, and the parent may fail on budget while
suspended if the children's actual spend exceeds what remains — the behaviour
[runtime-loop.md](runtime-loop.md) already describes for the child-run join
wake.

The derived child total includes a closed final-synthesis reserve: one step,
one model call, and USD 0.25 by default. A child limit that cannot contain both
research work and those reserves is rejected before materialization. Once any
research boundary is reached, the runtime adds a platform-trusted,
synthesis-only control to the volatile request and refuses another tool call;
the child must return the best-supported answer from evidence already in its
conversation or fail closed. The reserve does not widen the child's total or
the amount charged to the parent.

Caps are closed and configured: briefs per `delegate.run` call (default
three), live children per parent run (default eight, counted across calls),
depth (one), and live delegated runs per tenant, checked under the tenant admission pattern the
scheduling materializer uses so two parents racing for the last tenant slot
serialize before either creates a child.

## Scopes, tools, and trust

`child.principal_scopes = parent.principal_scopes ∩ scopes required by
allowed_tools`, never wider. The child's registry view is resolved through
`specs_for_session` with the child's principal and the child's session, so the
child advertises exactly `allowed_tools` and a call outside them is denied by
the ordinary policy path. Web, browser, sandbox, and memory tools are allowed
in a child when they are in the parent's pinned set and the corresponding
scope survives the intersection — research is what delegation is for. A child
never advertises `delegate.run` (depth one) or `skill.manage` (a child must
not author the parent's procedural memory).

Trust is already decided: the brief goes down as enveloped data in a `USER`
message the child's own instruction frames; the result comes up labelled
`EXTERNAL_UNTRUSTED`, exactly as the model gateway labels any model output; the
child cannot raise its trust and the parent cannot inherit the child's
authorizations (tool-system.md:971-977). A child result that instructs the
parent is therefore in the same position as web content — it can be read and
it cannot authorize. The result enters the parent's working conversation as a
tool result, not its stable prefix, so the prefix-stability invariant is
unchanged.

A `REQUIRE_APPROVAL` decision inside a child waits in the existing approval
queue with no human on the run, exactly as the review child does; Milestone
12's `approval.requested` trigger carries it to the parent's principal. A
child approval expires with the child's deadline.

## Cancellation and recovery

Cancelling a suspended parent cascades: `cancel_parked_run` requests
cancellation of every non-terminal `DELEGATED` child (a queued child through
the same parked path, a running child through the lazy cancellation token) and
completes the suspended invocation as `failed` with `tool.run_cancelled`, the
rule tool-system.md:966-970 already states for a suspended invocation whose run
is cancelled. A child's deadline is never later than its parent's, so the
deadline sweep ends children first and the parent's own sweep handles the
parent. A child's cancellation or failure never cancels its siblings.

Recovery needs nothing new. A parent invocation `RUNNING` with a
`suspended_kind` and a released lease is excluded from the lease-expiry sweep
and the reaper, as the tool system specifies for `user_input`; the resume path
learns `child_run` the way it knows `user_input`, and a parent whose children
finished while the parent's worker was away is re-queued by the join and
resumes from its checkpoint.

## The recorded conflict, resolved

A child run cannot be inserted into its parent's session: the partial unique
index that keeps one active run per session admits no row while the parent
waits in `WAITING_FOR_APPROVAL`, and Section 27.6's "parent's session or a
dedicated child session per policy" had no policy written
(readiness.md:1004). The resolution is the one the review log recorded as
a weak preference and ADR-0061 adopted: a dedicated child session, always. The
index is untouched, which is the point; the branch is deleted rather than
policed. Child sessions are principal-owned rows carrying the delegation
metadata, and they appear in the session index the way review and schedule
sessions already do — with metadata a client can label or fold. Hiding them
by default was considered and rejected: a child that ends suspended on an
approval would become invisible.

## The delegation ledger

The plan requires a separate trace and artifact references rather than a
transcript. Both have a carrier in the `delegations` table, the row that links
one suspended parent invocation to its children and records, content-free,
what was asked and what came back:

```python
class DelegationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    JOINED = "JOINED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Delegation(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    parent_run_id: UUID
    parent_session_id: UUID
    invocation_id: UUID
    depth: int
    brief: DelegationBrief
    derived_limits: RunLimits
    granted_scopes: frozenset[str]
    status: DelegationStatus
    children: list[DelegationChild]
    result: DelegationResult | None
    links_erased_at: datetime | None
    created_at: datetime
    joined_at: datetime | None
```

Each `DelegationChild` carries `child_run_id`, `child_session_id`, and the
child's terminal status, summary, artifact references, and usage once known.
The ledger is the parent's view of the delegation; the child's own trace —
its events, its tool invocations, its checkpoints — lives only in the child
session. The parent's event log carries the link and the summary and never the
child's transcript. That is the separation the plan means by "separate trace".

## Persistence

Milestone 13 adds one table and extends one column's vocabulary:

```text
delegations
  id UUID PRIMARY KEY
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  parent_run_id UUID NOT NULL REFERENCES runs(id)
  parent_session_id UUID NOT NULL REFERENCES sessions(id)
  invocation_id UUID NOT NULL REFERENCES tool_invocations(id)
  depth SMALLINT NOT NULL
  brief JSONB NOT NULL
  derived_limits JSONB NOT NULL
  granted_scopes JSONB NOT NULL
  status TEXT NOT NULL
  children JSONB NOT NULL
  result JSONB NULL
  links_erased_at TIMESTAMPTZ NULL
  created_at TIMESTAMPTZ NOT NULL
  joined_at TIMESTAMPTZ NULL
  UNIQUE (invocation_id)

runs.run_kind            gains the value 'delegated'
```

`runs.parent_run_id` already exists; a partial index on `(parent_run_id,
run_kind) WHERE parent_run_id IS NOT NULL` serves the join's sibling check.
The existing partial unique index that allows one review child per parent is
kind-scoped, so several delegated children per parent are legal. The table
carries the tenant row-level-security policy. The migration follows the
Milestone 12 head in the linear chain.

The brief is user-directed content authored by the parent model; it is stored
for audit and for idempotent retry, and the secret scanner's families apply to
it at validation so credential-shaped briefs are rejected without storing the
matched value. Erasure follows the existing deletion contract: deleting the
parent session erases its children's sessions, which exist only for the
parent; deleting a child session alone stamps `links_erased_at` and clears
that child's identifiers on the ledger row in the same transaction, the
pattern the scheduling occurrence uses for erased links.

## Public surface, scopes, and flags

No new route. `RunView.parent_run_id` already exists on the run view, and the
`run.waiting_for_approval` event's payload carries the suspension kind and
`child_run_ids`, so a streaming client learns of the delegation without a
second request; a child is an ordinary principal-owned run, so
`GET /v1/runs/{id}` and its event stream work unchanged. One exact platform
scope, `run.delegate`, is added for the tool; a principal without it sees
`delegate.run` denied by the ordinary scope check and never reaches
materialization.

Delegation is default-off through `AGENT_DELEGATION_ENABLED=0`. When the flag
is off, `delegate.run` is not registered at all, so the advertised tool set,
the prefix, and every gate that counts the registry are unchanged. Enabling it
requires PostgreSQL storage and the `delegation:` limits block; caps must be
positive and finite.

## Events and audit

Session events are the existing ones. The child session carries
`session.created`, the seed `user.message.created`, `run.queued` with
`parent_run_id` and `run_kind`, and the normal run sequence. The parent
carries `tool.call.started`, `run.waiting_for_approval` with the `CHILD_RUN`
suspension, and later `tool.call.completed` with the result items and the
structured result, then `run.queued`. Process events, in the shape the review
child and the scheduler use, record the cross-session facts:

```text
delegation.requested
delegation.materialized
delegation.rejected
delegation.child_terminal
delegation.joined
delegation.cancelled
```

Reason codes are stable under `delegation.*`: `tools_not_subset`,
`depth_exceeded`, `fanout_exceeded`, `budget_insufficient`, `tenant_cap`,
`brief_invalid`. Events carry identifiers, status, usage, and reason codes,
never the brief's text or a child's content.

## The evidence the gate requires

The plan's gate for multi-agent work is satisfied in two steps, both recorded
before the flag may be turned on for a tenant:

1. **A capability scenario admitted from a real failed trajectory.** The
   capability track already admits a scenario only when its source is a
   checked-in, redacted trajectory that failed, with a diagnosis
   ([evaluation-harness.md](evaluation-harness.md)). The owner supplies one
   real long-research trajectory that a single agent failed, diagnosed as one
   of the gate's five causes; its single-agent baseline is scored first, and
   the delegating re-run is scored as the milestone's exit evidence. The
   scenario's milestone bound, currently ten, moves to thirteen.
2. **A deterministic two-arm case.** Case 32 uses the `arms` and `delta` form
   cases 27 and 31 established (evaluation-harness.md:490-540): the first arm
   runs without `delegate.run` and is scripted to fail, the second runs with
   it and completes, with `delta: {policy_failures: same, outcome: improves}`.
   The arm model gains a `tools` overlay for this; it is an arm overlay, not
   a third carried thing.

Construction does not wait on the evidence; activation does. That is the
relationship ADR-0052 established between building and enabling for skill
authoring and ADR-0061 generalized.

## Configuration and deployment

Delegation adds no role and no unit. The `delegation:` block in the versioned
limits file declares `max_children_per_call`, `max_live_children_per_parent`,
`max_depth`, `max_live_delegated_runs_per_tenant`, the default child
`max_steps`, `max_model_calls`, `max_tool_calls`, `max_cost`, and
`wall_seconds`, the final `synthesis_reserve_steps`,
`synthesis_reserve_model_calls`, and `synthesis_reserve_cost`, and the summary
byte ceiling. Children use async priority 10
and the existing reserved-capacity rule keeps them from starving interactive
work.

## Tracked metrics

Track:

- delegations requested, materialized, rejected by reason, joined, cancelled;
- children per delegation and per parent; live children per tenant;
- child outcome by status; summary truncations;
- delegation wall time from materialization to join, p50, p95, p99;
- parent budget failures while suspended; reserved versus actual child cost;
- the capability scenario's baseline and delegating scores by judge version.

Metrics carry tenant-safe identifiers or aggregates, never a brief or a
summary.

## Build sequence

1. Add the brief, result, and ledger domain values and the limits derivation
   with property tests. **M13.**
2. Add the `delegations` migration, the `delegated` run kind, ORM models,
   in-memory and PostgreSQL repositories, RLS, erasure, and the repository
   contract. **M13.**
3. Add `delegate.run` as a control tool and the materializer in one unit of
   work, beginning with the every-write crash, dedicated-session, and
   subset regressions. **M13.**
4. Add the `CHILD_RUN` suspension branch in the loop and the terminal writer,
   the join, `requeue_after_child`, and the cancel cascade. **M13.**
5. Add the caps, tenant admission, cost reservation, additive usage, and the
   default-off flag. **M13.**
6. Add case 32 and the `tools` arm overlay; admit the capability scenario from
   the owner's failed trajectory and score its baseline. **M13.**
7. Run the full non-live suite, the PostgreSQL integration and resilience
   lanes, hosted CI, and the required GitHub CodeRabbit loop on one final
   head; score the delegating re-run. **M13.**

## Hard gates

1. **The request is validated before anything exists.** A request with no
   briefs, more briefs than the per-call cap, or any brief missing its
   objective, success condition, or allowed tools or exceeding a cap, is
   rejected with a stable reason and creates no child, session, ledger row, or
   suspension for any of its briefs. Registered as
   `gate.delegate.brief_schema`, case. **M13.**
2. **Materialization is atomic across a crash.** Inject a crash after every
   write. Before commit, no child session, run, seed, ledger row, or suspended
   invocation survives; after commit, a retry with the same invocation
   identity returns the one delegation. Registered as
   `gate.delegate.materialize_atomic`, case. **M13.**
3. **A child never shares its parent's session.** Every child is created in a
   dedicated session; the parent session's one-active-run index is never
   contended and never widened. Registered as
   `gate.delegate.dedicated_session`, case. **M13.**
4. **The parent suspends as a child-run wait.** The parent enters
   `WAITING_FOR_APPROVAL` with `suspension.kind = CHILD_RUN` and the child
   identifiers, releases its lease exactly once, appends no
   `approval.requested`, and enqueues no notification. Registered as
   `gate.delegate.parent_suspends`, case. **M13.**
5. **The child's tools are a subset.** The child advertises exactly the
   allowed tools, all within the parent's pinned set, never `delegate.run` or
   `skill.manage`; a call outside the set is denied by policy. Registered as
   `gate.delegate.tools_subset`, case. **M13.**
6. **The child's scopes are intersected.** The child's scopes are a subset of
   the parent's and of what its tools require; cross-tenant and
   cross-principal reads of a child return not found. Registered as
   `gate.delegate.scopes_intersected`, case. **M13.**
7. **Child limits are derived and bounded.** Over generated parents and
   briefs, every child limit is at most the parent's remaining value, the
   child deadline is at most the parent's, and a parent with nothing remaining
   cannot delegate. Registered as `gate.delegate.limits_derived`, property.
   **M13.**
8. **Usage is additive.** After the join the parent's usage equals its own
   plus every child's, and a parent whose children exceed its remaining cost
   fails on budget while suspended. Registered as
   `gate.delegate.usage_additive`, case. **M13.**
9. **Delegation is one level deep.** A child's advertised tools never include
   `delegate.run`, and a forged call from a child is denied before
   materialization. Registered as `gate.delegate.depth_one`, case. **M13.**
10. **Fan-out is capped.** A request whose brief count exceeds the per-call
    cap, or whose briefs would exceed live children per parent or live
    delegated runs per tenant, is rejected whole with a stable reason and no
    rows for any brief; two parents racing for the last tenant slot
    serialize. Registered as `gate.delegate.fanout_capped`, case.
    **M13.**
11. **A child's result is external and untrusted.** The re-entered result item
    carries `EXTERNAL_UNTRUSTED`, and a child result instructing the parent to
    take a `REQUIRE_APPROVAL` action produces an approval request and no
    execution. Registered as `gate.delegate.result_untrusted`, case. **M13.**
12. **The join completes once.** Siblings finishing concurrently complete the
    parent's invocation exactly once and re-queue the parent exactly once,
    through the single terminal writer. Registered as
    `gate.delegate.join_once`, case. **M13.**
13. **A failed child is a tool error.** A failed or cancelled child completes
    the parent's invocation as an error result with the child's reason; the
    parent continues and its siblings are unaffected. Registered as
    `gate.delegate.child_failure_is_tool_error`, case. **M13.**
14. **Cancellation propagates downward only.** Cancelling a suspended parent
    cancels every non-terminal child and fails the suspended invocation with
    `tool.run_cancelled`; cancelling a child cancels nothing else. Registered
    as `gate.delegate.cancel_propagates`, case. **M13.**
15. **Child results leave the prefix stable.** Injecting child results into
    the parent does not change the parent's prefix hash across the join.
    Registered as `gate.delegate.prefix_stable`, case. **M13.**
16. **The trace is separate.** The child's events, invocations, and
    checkpoints exist only in the child session; the parent's log holds the
    link, the status, and the bounded summary, never the child's transcript.
    Registered as `gate.delegate.trace_separate`, case. **M13.**
17. **Artifacts come back as references.** With the artifact return shape the
    parent receives artifact identifiers it can read; no artifact content or
    transcript is inlined. Registered as `gate.delegate.artifact_refs`, case.
    **M13.**
18. **The delegation schema encodes its trust boundaries.** Metadata inspection
    proves keys, the invocation uniqueness, the sibling index, the erasure
    marker, and row-level security. Registered as
    `gate.delegate.persistence_schema`, structural. **M13.**
19. **The delegation migration is reversible at its boundary.** Upgrade,
    downgrade, and re-upgrade from the immediate predecessor leave a valid
    schema. Registered as `gate.delegate.migration_stepwise`, case. **M13.**
20. **Delegation is default-off.** With the flag off `delegate.run` is not
    registered and the advertised set, prefix, and registry census are
    unchanged. Registered as `gate.delegate.default_off`, case. **M13.**
21. **Delegation changes the outcome.** Case 32's delegating arm completes
    where the single-agent arm fails, with no additional policy failure.
    Registered as `gate.delegate.changes_outcome`, case. **M13.**

## Open questions

1. The caps — three children per call, eight per parent, depth one — are
   starting values, not evidence. The capability scenario should inform them.
2. Whether a child should ever run at interactive priority when its parent is
   interactive. Priority 10 is the conservative start.
3. Whether `RunView` should gain `children` and `suspension_kind` fields so a
   client need not read event payloads. The payload is sufficient for the
   first client.
4. Whether child approvals should expire faster than the child's deadline.
5. Handoffs remain excluded. If evidence ever shows a parent should relinquish
   the user interaction, that is a plan amendment, not a configuration.
