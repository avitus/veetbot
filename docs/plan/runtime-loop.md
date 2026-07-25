---
title: Runtime Loop
status: design
canonical: true
---

# The runtime loop, the step, and the run lifecycle

## Eleven callables and not one of them is a port

Section 12.1 gives the runtime as forty-two lines of pseudocode, and those
forty-two lines name eleven things the loop calls:

```text
# name in 12.1                      where it is declared
load_and_verify_claim               nowhere
checkpoints.load_latest             nowhere (a bullet, not a Protocol)
agents.get_version                  nowhere (a bullet, not a Protocol)
principals.for_run                  nowhere
cancellation.raise_if_requested     nowhere
budgets.check_before_step           nowhere
invoke_model_and_persist_events     nowhere
budgets.record_model_usage          nowhere
process_tool_calls                  nowhere
select_final_message                nowhere
complete_run                        nowhere
```

Section 7 declares eight port Protocols with full signatures. Not one of
them appears in that list. The one call the loop makes to a declared port —
`context_builder.build` — is the single line of the loop whose contract is
known. Everything else in the runtime's central function is a name.

That is not a documentation gap in the ordinary sense, where a reader can
infer the missing piece from context. Each of those eleven names is a seam
where a different specification's requirements land, and in most cases more
than one specification has an opinion about the seam and the opinions
differ. `invoke_model_and_persist_events` is where Section 13's retry table
meets the model gateway's rule that retry ownership splits on
`stream_had_output`. `process_tool_calls` is where the tool system's
fourteen-step pipeline meets the policy engine's approval suspension.
`budgets.check_before_step` is where Section 6.5's *"check limits before and
after every model or tool operation"* meets ADR-0002's *"budget is checked
before an attempt, and failed attempts count against budget"* — two rules at
different granularities, both mandatory, and the pseudocode implements a
third granularity that is neither.

There are twenty such collisions. This document lists them by number and
resolves each one, because the loop is where they actually meet and an
implementer will hit all twenty in the first week.

Three of them are worth stating up front, because they are not disagreements
about detail. They are places where the loop as written cannot do what
another document requires of it.

**The loop cannot resolve its own agent.** Line 1408 reads
`agents.get_version(run.agent_id, run.agent_version)`. Neither field exists
on `Run`. Section 6.3 puts `agent_id` and `agent_version` on `Session`. The
first four lines of the runtime do not compile against the domain model in
Section 6.

**The loop cannot suspend.** Line 1437 handles a paused disposition with
`return`. Section 27.2 requires that entering either `WAITING_*` state
release the worker lease, checkpoint the run, and emit an event. A bare
`return` performs none of the three, and there is no `finally`. A run that
pauses for approval under the pseudocode as written holds its lease until
the lease expires, at which point the queue reclaims it and a second worker
starts executing a run that is waiting for a human.

**The loop cannot compact.** The context engine assigns compaction to the
loop in one sentence — *"the loop measures pressure before the call; if the
body will not fit, it invokes the compactor"* — and Section 12.1 contains no
pressure measurement, no compactor call, and no second `build()`. Compaction
is required by Section 11.4, is a hard gate in Milestone 7, and has no call
site anywhere in the corpus.

This document supplies the missing interfaces, defines the step as a thing
with an identity rather than as a counter, replaces the flat `while` with a
loop that returns an outcome to an executor that owns the terminal actions,
and resolves the twenty contradictions in a table at the end.

## What this document does not change

It expands Sections 12, 13, 14.1, and 14.2, and the loop-facing halves of
Sections 6.4, 6.5, 6.9, 16, 19, and 27. It does not replace the requirements
in those sections and it reorders none of the tool pipeline's fourteen steps.

Where an existing declaration and a later spec's declaration disagree, this
document names one canonical and says so explicitly rather than silently
preferring one. Where a requirement is genuinely ambiguous, the resolution
chosen here is recorded in `docs/status/questions-for-review.md` with its
reversal cost.

Four things are deliberately out of scope. The tool pipeline's internals
belong to [tool-system.md](tool-system.md); this document calls it and
describes what the call returns. Provider translation belongs to
[model-gateway.md](model-gateway.md); this document describes what the
runtime does with an attempt that fails after output. Policy evaluation
belongs to [policy-and-approvals.md](policy-and-approvals.md); this document
describes what the loop does with a decision. Context assembly belongs to
[context-engine.md](context-engine.md); this document describes when the
loop measures pressure and what it does about it.

## Five units, and only three of them are objects

The corpus uses five words for units of work. Two of them name domain
objects that exist, one names an object that should exist and does not, one
names a concept that deliberately has no object, and one belongs to the
model gateway. Sorting them is a prerequisite for everything else here.

```text
# unit     object?   identity                    owner
session    yes       sessions.id                 session service
turn       no        none, by decision           conversation vocabulary
run        yes       runs.id                     this document
step       partly    (run_id, step_number)       this document
attempt    yes       ModelAttempt.attempt_id     model gateway
```

**Turn has no object and gets none.** Section 27.1 defines a turn as one
user input and the agent's complete response to it, then decides `run ==
turn`. A `Turn` model would be a second name for a `Run` row and would
invite a `turns` table that could disagree with `runs`. Turn stays a word
for talking to humans about conversation. Nothing in the runtime reads it.

**Step becomes a first-class unit without becoming a table.** The tool
system already defines it: *"a step is one model call plus the complete
disposition of every tool call it produced."* That definition is exactly
right and it has a consequence nobody drew — a step's identity is its model
call, because by definition every step has exactly one. Today `step_number`
is persisted in one place, as a column on `tool_invocations`, which means a
step that produced no tool calls has no persisted identity at all. That is
the common case: the last step of every successful run produces text and no
tool calls, and it is invisible.

The fix is one additive column rather than a table:

```text
# additive column on the model_calls table
model_calls.step_number  INTEGER NOT NULL
```

`model_calls` is introduced by the model gateway spec as one row per model
attempt. Adding `step_number` to it gives every step a persisted identity,
makes `runs.step_count` reconcilable against a query rather than trusted,
and lets an operator ask what a step cost without joining through a tool
invocation that may not exist. A `steps` table would carry no field that is
not derivable from `model_calls` and `tool_invocations` and would introduce
a third row that can disagree with the two that already exist.

`Step` also exists as a runtime value object, because the loop needs to pass
one around:

```python
class Step(BaseModel):
    run_id: UUID
    step_number: int          # 1-based, monotonic within a run
    started_at: datetime      # from the Clock port
    attempt_count: int = 0    # model attempts made in this step
    tool_call_count: int = 0
    compactions: int = 0      # compactions performed for this step
```

It is not persisted as itself. It is the argument that carries
`step_number` into the idempotency key, into the `ProposedAction`, into the
span, and into `ToolExecutionContext`, all of which already require it and
none of which currently states where it comes from.

**Attempt belongs to the gateway and the loop reads two of its fields.**
`ModelAttempt.attempt_number` is the retry counter and
`ModelAttempt.stream_had_output` is the flag that decides who owns the next
retry. The loop never constructs an attempt; it receives them.

## The run record the loop actually needs

Nine fields are read by the corpus and absent from `Run`. Six of them are
introduced as columns by
[event-log-and-persistence.md](event-log-and-persistence.md) without a
corresponding change to the domain model, which is how a field ends up
being real in the database and imaginary in the type. This section states
the domain model side of the same additions and adds the three that no
document has claimed.

```python
# additive fields on the Section 6.4 Run model
class Run(BaseModel):
    # ... Section 6.4's existing fields, unchanged ...

    tenant_id: UUID           # every scoped repository requires it
    agent_id: UUID            # denormalized from Session at creation
    agent_version: str        # denormalized from Session at creation
    lease_epoch: int          # asserted in every fenced write
    attempts: int             # queue-level attempt counter, cap 3
    priority: int             # 0 interactive, 10 async, 20 maintenance
    scheduled_for: datetime   # retry backoff and Milestone 10
    deadline_at: datetime | None
    failure: RunFailure | None
```

Three of these need justification beyond "something reads them".

**`agent_id` and `agent_version` are denormalized onto the run
deliberately.** They live on `Session` and the obvious fix is for the loop
to load the session. That is the wrong fix for two reasons. A session is
long-lived and its agent version can change; a run must execute against the
version that was current when the turn was submitted, or a deploy mid-run
silently changes the agent underneath a paused approval. And the approval
revalidation table already treats an agent-version change as grounds to void
an approval outright, which requires the run to remember which version it
was authorized under. Copying both fields onto the run at creation makes the
run self-describing, removes a join from the hot path, and makes
"which agent version ran this?" answerable from one row forever.

**`deadline_at` is a copy, not a new concept.** Section 6.5 already declares
`RunLimits.deadline_at`, so the run deadline the tool system's
`ToolExecutionContext` is *"already min'd with"* does exist — it is just
buried inside a nested model with no column, which means no index, which
means the sweep that fails overdue runs would be a full scan. The
`runs.deadline_at` column is `run.limits.deadline_at` denormalized for
indexing. It is computed once at run creation and never recomputed; a run
that pauses for a human approval for six hours is still bound by the
deadline set when it was submitted, which is the behaviour the state machine
already implies by listing deadline as a cause of `FAILED`.

**`failure` gets a type.** `runs.failure JSONB` exists in Section 15 with no
domain counterpart, and `failure.reason = "max_attempts_exceeded"` is
written as a bare string literal.

```python
class FailureReason(StrEnum):
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    TOOL_LOOP_DETECTED = "tool_loop_detected"
    REPEATED_DENIAL = "repeated_denial"
    APPROVAL_EXPIRED = "approval_expired"
    INPUT_DEADLINE_EXCEEDED = "input_deadline_exceeded"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_PERMANENT_ERROR = "model_permanent_error"
    EMPTY_MODEL_TURN = "empty_model_turn"
    AUTHORIZATION_ERROR = "authorization_error"
    CHILD_RUN_FAILED = "child_run_failed"
    INTERNAL_ERROR = "internal_error"


class RunFailure(BaseModel):
    reason: FailureReason
    error_class: str          # the Section 13 class name
    message: str              # operator-facing, never raw provider text
    step_number: int | None
    attempt_number: int | None
    occurred_at: datetime
    details: dict[str, Any] = {}
```

`reason` is the stable key that dashboards and the API group by;
`error_class` is the Section 13 name, which is finer-grained and may change
as the taxonomy grows. Keeping both means the taxonomy can be extended
without invalidating a year of aggregates. `message` is subject to the
secret-scanner gate like every other persisted string.

## The ports the loop calls

Every one of the eleven names in Section 12.1 resolves to exactly one of
four things: a port that is already declared, a port declared here, a pure
function in the runtime module, or an application service the loop does not
call at all.

```text
# 12.1 name                   resolution
load_and_verify_claim         RunQueue.claim (queue owns the claim)
checkpoints.load_latest       CheckpointRepository.latest
agents.get_version            AgentRepository.get_version (declared here)
principals.for_run            PrincipalResolver.for_run (declared here)
cancellation.raise_if_req...  CancellationToken, one per run
budgets.check_before_step     BudgetLedger.check (declared here)
budgets.record_model_usage    BudgetLedger.record_model_usage
invoke_model_and_persist...   StepExecutor.invoke_model, runtime module
process_tool_calls            ToolPipeline.dispatch, tool-system.md
select_final_message          a pure function, runtime module
complete_run                  RunExecutor terminal path, not the loop
```

Two of those resolutions are the substance of the whole document.
`load_and_verify_claim` disappearing into `RunQueue.claim` means the loop is
never handed a run it must verify — it is handed a run it already owns, with
the lease epoch that proves it. And `complete_run` moving out of the loop
into an executor is what makes suspension work, for reasons the next section
gives.

### Ports declared here

Four ports the runtime needs are named in the corpus and declared nowhere.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class IdFactory(Protocol):
    def new_id(self) -> UUID: ...
```

Milestone 1 already requires both *"before anything depends on ambient
time"*, and the evaluation harness pins both as two of its seven
nondeterminism sources. They are declared here because the runtime is the
component that reads them most: lease deadlines, run deadlines, approval
expiry, retry backoff, budget windows, and every `occurred_at` on every
event. A `datetime.now()` anywhere under `agent_core` outside the clock
adapter is a structural-gate violation, and that gate belongs to this port.

```python
class AgentRepository(Protocol):
    async def get_version(
        self, agent_id: UUID, agent_version: str
    ) -> AgentSpec: ...
    async def latest_version(self, agent_id: UUID) -> AgentSpec: ...
```

`get_version` raises `NotFoundError` rather than returning `None`. A run
whose pinned agent version has been deleted is unrecoverable and should fail
loudly at the first step rather than silently fall back to the latest
version, which would execute a turn against an agent the principal never
authorized.

```python
class PrincipalResolver(Protocol):
    async def for_run(self, run: Run) -> Principal: ...
```

The principal is resolved once per execution and held for its duration. It
is not re-resolved per step, because a permission change mid-run would
otherwise produce a run whose early tool calls were authorized under one
principal and later ones under another, with no record of the boundary. A
principal whose permissions changed takes effect on the next run. The one
exception is approval resumption, where the policy engine revalidates
against the current principal by design.

```python
class BudgetLedger(Protocol):
    def check(self, run: Run, scope: BudgetScope) -> None: ...
    async def record_model_usage(
        self, run: Run, usage: ModelUsage, *, step: Step
    ) -> None: ...
    async def record_tool_usage(
        self, run: Run, count: int, *, step: Step
    ) -> None: ...
    async def refund_orchestration_turn(
        self, run: Run, *, step: Step
    ) -> None: ...
```

`check` is synchronous and raises `BudgetExceeded`, because it is a
comparison against fields already loaded on the run and adding an `await` to
it would invite an implementation that queries. The `record_*` methods are
asynchronous because they write, and they write in the same transaction as
the event they account for. `refund_orchestration_turn` implements
ADR-0015's requirement that an orchestration-only turn does not consume the
step and model-call budget.

### Ports declared elsewhere that the loop calls

```text
# port                    declared in
RunQueue                  event-log-and-persistence.md
CheckpointRepository      event-log-and-persistence.md
EventRepository           engineering-plan.md Section 7
ContextBuilder            engineering-plan.md Section 7
ContextPlanner            context-engine.md
Compactor                 context-engine.md
TokenEstimator            context-engine.md
ModelProvider             engineering-plan.md Section 7 (via gateway)
PolicyEngine              engineering-plan.md Section 7 (via pipeline)
ApprovalRepository        policy-and-approvals.md
```

`RunQueue` supersedes `RunRepository.claim_next` and
`RunRepository.heartbeat` for all queue operations. Section 7's
`RunRepository` keeps `create`, `get`, and `transition`; its two queue
methods have signatures that cannot express what the queue requires —
`claim_next` returns no lease epoch, and `heartbeat -> None` cannot say
"you were fenced". Where the two disagree, `RunQueue` is canonical. This is
stated rather than assumed because an implementer reading Section 7 first
will otherwise build the wrong signature and discover the problem when
fencing does not work, which is the hardest possible time to discover it.

### What the loop does not call

The loop does not call the approval service, the expiry reaper, the input
delivery endpoint, the session-history projection builder, the consolidation
job, or the queue's `reclaim_expired`. Every one of those is named in a
specification with the phrase "the runtime" or "the worker" somewhere
nearby, and every one of them belongs to a different process. The ownership
table late in this document says which.

## The loop returns an outcome; the executor owns the ending

The single structural change this document makes is to split Section 12.1's
one function into two.

```python
class RunOutcome(BaseModel):
    kind: OutcomeKind
    final_message: AssistantMessage | None = None
    failure: RunFailure | None = None
    suspension: Suspension | None = None


class OutcomeKind(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    FENCED = "fenced"


class Suspension(BaseModel):
    kind: SuspensionKind            # APPROVAL | USER | CHILD_RUN
    invocation_id: UUID             # the suspended tool_invocations row
    approval_id: UUID | None
    question_id: UUID | None
    child_run_ids: list[UUID] = []
    expires_at: datetime | None
```

`run_loop` computes an outcome and performs no terminal action. `RunExecutor`
performs every terminal action, in one place, in a `finally`. The loop
cannot forget to release a lease because the loop cannot release a lease.

That is not a stylistic preference. Section 27.2 requires three actions on
entering either waiting state — checkpoint, release the lease, emit an event
— and a flat loop that `return`s has five distinct exit paths (completion,
failure, cancellation, suspension, fencing) that each need a different
subset of those three. Five exits times three actions is where a
half-released lease comes from. One executor with one `finally` and a
five-armed match is the smallest structure that cannot produce one.

```python
# runtime/executor.py -- the only place a run ends
async def execute_run(run: Run, lease_epoch: int) -> None:
    ctx = await build_run_context(run, lease_epoch)
    heartbeat = start_heartbeat(ctx)     # supervisor task
    try:
        outcome = await run_loop(ctx)
    except WorkerFenced:
        outcome = RunOutcome(kind=OutcomeKind.FENCED)
    except Exception as exc:             # never swallowed, always typed
        outcome = classify_unhandled(exc, ctx)
    finally:
        heartbeat.cancel()
    await finalize(ctx, outcome)
```

`finalize` is the five-armed match, and it is the only function in the
runtime that calls `RunRepository.transition` or `RunQueue.release`.

```text
# outcome    transition           lease      event
COMPLETED    RUNNING->COMPLETED   released   run.completed
FAILED       RUNNING->FAILED      released   run.failed
CANCELLED    RUNNING->CANCELLED   released   run.cancelled
SUSPENDED    RUNNING->WAITING_*   released   run.waiting_for_*
FENCED       none                 none       run.fenced
```

Every row but the last performs a checkpoint write before the transition,
and every row but the last performs its transition and its lease release in
one short transaction with the fencing predicate on the `WHERE` clause. The
`FENCED` row transitions nothing, releases nothing, and writes nothing but
its one event, for reasons the fencing section gives.

### The loop itself

```python
# runtime/loop.py -- computes an outcome, ends nothing
async def run_loop(ctx: RunContext) -> RunOutcome:
    while True:
        ctx.token.raise_if_cancelled()          # observation point 1
        ctx.budgets.check(ctx.run, BudgetScope.STEP)

        step = ctx.begin_step()
        request = await build_with_pressure(ctx, step)

        turn = await invoke_model(ctx, step, request)
        if turn.stop_reason is StopReason.CANCELLED:
            return RunOutcome(kind=OutcomeKind.CANCELLED)

        await ctx.budgets.record_model_usage(
            ctx.run, turn.usage, step=step
        )
        ctx.token.raise_if_cancelled()          # observation point 3

        if not turn.tool_calls:
            message = select_final_message(turn, step)
            await ctx.checkpoint(trigger="final", full=True)
            return RunOutcome(
                kind=OutcomeKind.COMPLETED, final_message=message
            )

        await ctx.checkpoint(trigger="model_response")
        disposition = await ctx.pipeline.dispatch(
            run=ctx.run,
            checkpoint=ctx.checkpoint_state,
            tool_calls=turn.tool_calls,
            principal=ctx.principal,
            step=step,
        )

        if disposition.suspension is not None:
            return RunOutcome(
                kind=OutcomeKind.SUSPENDED,
                suspension=disposition.suspension,
            )

        await ctx.apply_runtime_working_state(disposition, step)
        await ctx.checkpoint(trigger="tool_batch")
```

Six things are different from Section 12.1 and each is a resolution stated
later: the cancellation token replaces a free function and is observed at
more than one point; `build_with_pressure` wraps `build()` with the
compaction loop the context engine requires; the model turn's
`CANCELLED` stop reason is an outcome rather than an error; a completed run
checkpoints before it returns; the pipeline's suspension is a value rather
than a `return`; and the runtime's working-state write has a call site.

What is *not* different matters as much. The loop still calls `build()`
once per step in the common case, still makes exactly one model call per
step, still dispatches all of a turn's tool calls through one function, and
still terminates when a turn produces no tool calls. Section 12.1's shape
was right. Its interfaces were missing.

## Step anatomy

A step is one model call plus the complete disposition of every tool call it
produced. Expanded, that is eleven phases, and naming them is what lets the
cancellation, budget, checkpoint, and span sections each say "phase 4"
instead of restating the loop.

```text
# phase  what happens                        can it write?
1        cancellation and budget checks       no
2        pre-turn recall (first step only)    checkpoint only
3        pressure measurement                 no
4        compaction if over budget            checkpoint
5        build() -- pure                      no
6        model attempt(s), streaming          model_calls, events
7        usage recorded, budget re-checked    runs.usage
8        checkpoint after model response      checkpoints
9        tool pipeline for the whole batch    everything
10       runtime working-state carry          checkpoint
11       checkpoint after the batch           checkpoints
```

Phases 1 through 5 touch nothing the world can see, which is why a fenced
worker discovering its fate anywhere in that range costs nothing but tokens.
Phase 6 spends money. Phase 9 is the only phase that can change something
outside the database, and the tool pipeline's own rule — *"nothing before
step 8 has touched the world"* — subdivides it further.

Phase 2 runs on the first step of a run only. Memory retrieval requires an
automatic pre-turn recall when the query former's confidence clears a floor,
injected into the user turn. `build()` is pure and cannot perform retrieval,
so the recall must happen before it and its result must be an input.
The loop performs it once, before the first `build()`, and writes the result
into the checkpoint's recall region. Every subsequent `build()` in the run
reads it from there. This is what makes the purity requirement and the
recall requirement compatible instead of contradictory, and it has the
useful side effect that a resumed run does not re-run recall and get a
different context than the one it paused with.

## Cancellation: one token, six observation points, one rule about effects

Section 16 requires the worker to check cancellation in five places. Section
12.1 checks in one. The tool system puts a `CancellationToken` inside
`ToolExecutionContext`, which looks like a sixth mechanism. All three are
the same mechanism once the token is constructed once per run.

```python
class CancelReason(StrEnum):
    REQUESTED = "requested"     # a principal called the cancel endpoint
    DEADLINE = "deadline"       # run.deadline_at passed
    FENCED = "fenced"           # the lease was lost


class CancellationToken(Protocol):
    @property
    def reason(self) -> CancelReason | None: ...
    def raise_if_cancelled(self) -> None: ...
    async def wait(self) -> CancelReason: ...
```

One token is created per run execution by `build_run_context` and handed to
everything: the loop reads it at its observation points, the tool executor
puts the same object on every `ToolExecutionContext`, and the sandbox
adapter selects on `token.wait()` alongside its own timeout. It is set by
exactly three writers — the cancellation poller, the deadline timer, and the
heartbeat supervisor — and never cleared.

```text
# point  where                              phase
1        top of the loop                    1
2        before each model attempt          6
3        after the model stream closes      7
4        before each tool call              9 (pipeline step 7)
5        after each tool call               9 (pipeline step 13)
6        inside long sandbox execution      9 (via the same token)
```

Points 1 through 5 are Section 16's five, mapped onto the phases. Point 6 is
Section 16's *"during long-running sandbox execution where possible"*, and
it is the same object rather than a separate flag, which is what removes the
apparent sixth mechanism.

The token's state is refreshed by a poller in the same supervisor task as
the heartbeat, at the heartbeat interval, reading `runs.cancel_requested_at`
in the same query that refreshes the lease. Cancellation therefore costs no
additional database round trip, and its worst-case latency is one heartbeat
interval plus the time to reach the next observation point — which for a run
inside a ten-minute model call is bounded by the model call, and is why
point 3 exists.

**The rule about effects is the part that is not obvious.** Cancellation
observed at points 1, 2, 3, or 4 stops the run cleanly: nothing has been
sent, and the invocation, if any, is completed as `cancelled` with
`tool.run_cancelled`. Cancellation observed at point 5 or 6 — after
`effect_sent_at` has been set on the invocation — does **not** abandon the
call. The call runs to completion, its result is persisted, and the run
cancels at the batch boundary.

This is the same reasoning the tool system uses for `UNCERTAIN`. A cancelled
run that abandoned a call whose effect watermark was set would leave the
system unable to say whether the effect happened, and "the user cancelled"
is not a licence to lose that information. A cancellation therefore has a
bounded tail equal to the longest in-flight tool call, and the API's cancel
endpoint returns `202` rather than `200` for that reason.

A batch that is partly executed when cancellation arrives finishes the calls
whose effects are in flight and marks the rest `cancelled`. The model is
never asked to respond to a cancelled batch; the run ends.

## Budget: three scopes, one ledger, and "after" means "record"

Three documents place the budget check in three different places and all
three are right about their own concern.

```text
# scope      checked            enforces
ADMISSION    once, at claim     deadline, max_cost already exceeded
STEP         phase 1            max_steps, max_tool_calls, cost, deadline
ATTEMPT      phase 6, per try   max_model_calls, max_*_tokens, cost
```

`BudgetScope.ATTEMPT` is what reconciles ADR-0002's *"budget is checked
before an attempt, and failed attempts count against budget"* with Section
12.1's once-per-step check. A retried attempt after a mid-stream failure has
already spent tokens; checking only at the top of the step would let three
attempts spend three times the remaining budget before the next check.

Section 6.5's *"check limits before and after every model or tool
operation"* is satisfied without a fourth mechanism, because the "after"
check is the `record_*` call. `record_model_usage` adds the attempt's usage
to `runs.usage`, in the same transaction that writes the `model_calls` row,
and raises `BudgetExceeded` if the new total is over. Recording and checking
are one operation because separating them creates a window in which the
totals are over budget and nothing has noticed.

```text
# operation           before          after
model attempt         check(ATTEMPT)  record_model_usage
tool call batch       check(STEP)     record_tool_usage
```

`BudgetExceeded` raised anywhere in the loop produces
`OutcomeKind.FAILED` with `FailureReason.BUDGET_EXCEEDED`. It is a permanent
classification: the queue never requeues it, because the second attempt
would be over budget before it started.

Two refinements the corpus already requires and the loop must honour. An
orchestration-only turn refunds its step and model-call budget, so
`check(STEP)` on the following step sees the refunded totals; the refund is
applied in phase 10, before the checkpoint that ends the step. And usage is
additive across subagent fan-out, so a parent's `record_model_usage` is
called again with the child's rollup when the child run reaches a terminal
state, which means a parent can fail on budget while suspended and is one of
the two cases where a waiting run transitions to `FAILED` without ever
becoming `RUNNING` again.

## The heartbeat is a supervisor, not a statement in the loop

Section 14.1 step 3 says *"refresh the lease periodically"*. The event log
spec sets the interval at one third of the lease duration. Nothing states
who runs the timer, and the three plausible answers behave very differently.

A per-iteration call at the top of the loop is the cheapest to write and it
is wrong: a single model call may run for ten minutes, which is longer than
any sane lease, so the lease expires mid-call and the run is reclaimed while
a worker is still streaming into it. A call inside the model-call wrapper
fixes that case and not the tool case. Only a task running independently of
what the loop is doing refreshes the lease during both.

```python
# runtime/supervisor.py
async def supervise(ctx: RunContext) -> None:
    interval = ctx.lease_seconds / 3
    while True:
        await ctx.clock.sleep(interval)
        alive = await ctx.queue.heartbeat(
            ctx.run.id, ctx.worker_id, ctx.lease_epoch
        )
        if not alive:
            ctx.token.cancel(CancelReason.FENCED)
            return
        if ctx.deadline_passed():
            ctx.token.cancel(CancelReason.DEADLINE)
            return
        if await ctx.cancel_requested():
            ctx.token.cancel(CancelReason.REQUESTED)
            return
```

One task per executing run, started by the executor before the loop and
cancelled in its `finally`. It refreshes the lease, watches the deadline,
and polls for cancellation, because all three are the same "has the outside
world changed its mind" question and doing them in one place means one timer
and one query rather than three.

`heartbeat` returning `False` rather than raising is deliberate in the event
log spec, and this is where that pays off: the supervisor's handling of
being fenced is to set a token and return, which is a statement, not an
unwind through error paths that might themselves try to write.

### What happens when the heartbeat fails mid-model-call

The supervisor sets `CancelReason.FENCED`. The in-flight model stream is
aborted immediately rather than allowed to finish, because its output can
never be committed — every write the fenced worker attempts will affect zero
rows — so finishing the stream buys nothing and spends output tokens the
tenant is billed for. This is the one case where the loop abandons work in
progress, and it is safe precisely because none of it can be persisted.

The loop then observes the token at its next observation point and returns
`OutcomeKind.FENCED`. `finalize` performs no transition, no lease release,
and no checkpoint write. It appends exactly one event.

```text
# run.fenced payload
run_id, worker_id, lease_epoch, phase, step_number, observed_at
```

Appending an event while fenced looks like it contradicts *"it stops
immediately, does not retry, does not append"*, and it does not, for a
structural reason worth stating. Event append is guarded by the per-session
sequence, not by the run's lease epoch; it is an append to an immutable log,
not a mutation of contested state. What the fenced worker must never do is
transition the run, write a checkpoint, or complete an invocation — all of
which are epoch-guarded and would all affect zero rows anyway. The one
append is what makes a split-brain incident diagnosable afterwards, and
`lease_epoch` on the payload is what lets an operator order it against the
new owner's events unambiguously.

A fenced worker that has already set an effect watermark cannot undo it.
That is a known and accepted consequence: the new owner reads the watermark
and applies the recovery table, and the effect is executed once even though
two workers believed they owned the run.

## Compaction has a call site

The context engine's most load-bearing sentence for this document is that
*"compaction is a write, and it is not something `build()` does. The loop
measures pressure before the call; if the body will not fit, it invokes the
compactor, which writes a new checkpoint, and then calls `build()` again on
that checkpoint."* Phases 3, 4, and 5 are that sentence.

```python
# runtime/context.py -- phases 3-5
MAX_COMPACTIONS_PER_STEP = 2

async def build_with_pressure(
    ctx: RunContext, step: Step
) -> ModelRequest:
    plan = await ctx.planner.current(ctx.run.session_id)
    while True:
        pressure = ctx.estimator.measure(ctx.checkpoint_state, plan)
        if pressure.fits:
            return await ctx.builder.build(
                run=ctx.run,
                checkpoint=ctx.checkpoint_state,
                agent=ctx.agent,
                principal=ctx.principal,
            )
        if step.compactions >= MAX_COMPACTIONS_PER_STEP:
            raise ContextOverflow(pressure)
        result = await ctx.compactor.compact(
            ctx.checkpoint_state, plan.budget, pressure.reason
        )
        ctx.checkpoint_state = result.checkpoint
        step.compactions += 1
```

Three details in that loop are decisions rather than mechanics.

**The pinned tool set reaches `build()` through the plan, not through a new
parameter.** ADR-0020 pins the tool set at session open and
`ContextBuilder.build` takes `run`, `checkpoint`, `agent`, and `principal`
with no session — which looked like a signature gap. It is not:
`run.session_id` is already on `Run`, and `ContextPlanner.current` resolves
the plan from it. The four-parameter signature stands unchanged and the
builder reaches the pinned set the same way it reaches the prefix epoch.

**Compaction is capped at two per step.** A third compaction on the same
step means the body does not fit even after two rounds of yielding, which is
either a single tool result larger than the budget or a prefix that has
outgrown the model. Both are `ContextOverflow`, which is permanent and fails
the run rather than looping. ADR-0020 already caps summary depth at 2 and
escalates beyond it to a child run; this cap is the same number for the same
reason and the two should move together if either moves.

**Compaction writes a checkpoint and the loop adopts it.** `compact` returns
a `CompactionResult` carrying the new checkpoint, which becomes the loop's
working state immediately. The alternative — re-reading the checkpoint from
the repository — would be a read of a row the loop just wrote and would make
the compactor's return value decorative.

Compaction gets a span, `context.compact`, nested under the step span. It is
a model call on the critical path of an interactive turn and it is currently
invisible in the trace tree, which is the kind of gap that turns into a
week of "why is p99 latency bimodal".

## Checkpoints: six triggers, one writer, one API

Section 6.9 lists six triggers. Section 12.1 writes one checkpoint, in one
place, with an elided argument list. The gap between those is the largest
single source of "resume did something surprising" in a system like this,
so the triggers are restated here as a table against the phases, with the
`full` discriminator resolved.

```text
# trigger              phase  full?    written by
model response         8      no       the loop
tool call completed    9      no       the pipeline
tool call failed       9      no       the pipeline
approval requested     9      yes      the pipeline
approval resolved      --     yes      the approval service
compaction             4      yes      the compactor
final completion       --     yes      finalize
final failure          --     yes      finalize
```

Two triggers in Section 6.9's list are written by components that are not
the loop, which is the correct reading of *"an approval resolution"* — the
loop is not running when an approval resolves.

**The `full` flag has a rule, so the call site can supply it.**
`CheckpointRepository.write(run_id, checkpoint, *, full: bool) -> int`
requires the caller to decide, and Section 12.1's call site could not,
because nothing told it how. The rule:

```text
full = True when any of:
  - it is the run's first checkpoint (version 1)
  - version % 8 == 1  (a full snapshot every eighth write)
  - the trigger is compaction
  - the trigger is a suspension or a terminal transition
otherwise full = False
```

Every input is known at the call site: the version is the previous version
plus one, and the trigger is a literal at each of the eight sites. The
eighth-write interval bounds delta-chain reconstruction at seven deltas, and
forcing a full snapshot before any suspension means a resume in a different
process — possibly hours later, possibly after a deploy — does exactly one
read.

The `checkpoints` table needs two additive columns to make the chain
walkable, which the delta design requires and Section 15 does not carry:

```text
# additive columns on the checkpoints table
checkpoints.full          BOOLEAN NOT NULL DEFAULT TRUE
checkpoints.base_version  INTEGER NULL     -- the full this delta rebases
```

**The persisted form and the materialized form are different types, and
saying so resolves a standing contradiction.** Section 6.9 declares
`conversation: list[ConversationItem]` inline; the storage note in the same
section, ADR-0003, and the event log spec all require conversation stored as
event references with deltas. Both are true of different things:

```python
# what CheckpointRepository.latest() returns, after chain walk
class RunCheckpoint(BaseModel):
    conversation: list[ConversationItem]   # Section 6.9, unchanged
    # ... Section 6.9's other fields ...


# what is persisted in checkpoints.state
class StoredConversationItem(BaseModel):
    ref: EventRef | None        # (session_id, sequence) for log-backed
    inline: ConversationItem | None   # opaque items and summaries only
```

The repository materializes on read and dereferences on write. Section 6.9's
type is the loop's view and does not change; the storage note describes the
repository's internals. Opaque provider continuation payloads and compacted
summary text are always inline because they are not reconstructible from the
log — which is exactly ADR-0006's statement that losing a checkpoint
mid-loop loses the continuation payload and the loop restarts rather than
resumes.

**Checkpoint dispensability has a consequence for the seeder.** Milestone 2
gates on *"delete a run's non-terminal checkpoints, resume, and reach the
same terminal state"*. A run whose checkpoints are all deleted has no
version 1, so the loop must be able to rebuild it — which means seeding is
not something that happens once at submission. It is a function:

```python
async def seed_checkpoint(ctx: RunContext) -> RunCheckpoint: ...
```

called by the application service when it creates the run, and called again
by the loop when `CheckpointRepository.latest` returns `None`. It reads the
session-history projection, applies the context budget, appends the new user
message, and writes version 1. Having one function with two call sites is
what makes the dispensability gate pass rather than being a gate nobody can
satisfy.

## Suspension and the resume ladder

A run suspends three times for three reasons and the corpus describes each
of them in a different document with a different owner. They are one
mechanism.

```text
# kind    state                 entered by            resumed by
APPROVAL  WAITING_FOR_APPROVAL  a REQUIRE_APPROVAL    approval service
USER      WAITING_FOR_USER      conversation.ask_user input endpoint
CHILD_RUN WAITING_FOR_APPROVAL  delegate.run          child terminal path
```

The third row is deliberately not a fourth state. A run waiting on a child
run is not waiting for a human, so `WAITING_FOR_USER` is wrong, and adding a
`WAITING_FOR_CHILD` state would mean amending Section 27.2's state machine
and every projection that reads it. It uses `WAITING_FOR_APPROVAL` with a
`Suspension.kind` of `CHILD_RUN` on the event payload, so the state machine
is unchanged and the distinction is queryable. This is recorded as an open
question, because the alternative reading — that a child-run wait deserves
its own state — is defensible and cheap to adopt later.

**The lease is released exactly once, by `finalize`.** The tool system says
a suspending control tool *"commit[s] the invocation row as `RUNNING` with a
suspension marker, release[s] the lease"*, and the loop's executor also
releases the lease on every outcome, which reads as a double release. The
resolution preserves both requirements: the pipeline commits the invocation
row and returns a `Suspension`, the executor performs the release. Suspending
*does* release the lease, exactly as the tool system requires; the release
is one statement in one place rather than one per suspending tool. A
`release` against a lease already released is a zero-row update, which the
fencing rules would classify as being fenced — so making it impossible is
worth more than making it idempotent.

**Resumption is a cold start of the process and a warm start of the step.**
Section 27.3 has the application service re-enqueue; Section 9.3 says the
two approval edges belong to the application service and not the worker
loop; Section 14.1 describes the worker as executing "until completion or
pause"; and the tool system says *"resume re-enters at 6"*. Those are
consistent once the two levels are separated:

```text
# level      what happens on resume
process      a worker claims the re-enqueued run, cold, from scratch
run          run_loop starts at phase 1 of a new execution
step         the pipeline re-enters at step 6 for each pending call
```

The worker never resumes in the sense of continuing a suspended coroutine.
It claims a `QUEUED` run, loads the latest checkpoint, and starts the loop.
What makes it a resume rather than a restart is that the checkpoint carries
`pending_tool_calls`, and the loop's first action when it finds them is to
dispatch them into the pipeline at its step 6 — validation and policy —
rather than proposing them again. The pipeline's "re-enter at 6" is a
statement about the pipeline's fourteen steps, not about the process.

```python
# runtime/loop.py -- run_loop's first act, before the while
if ctx.checkpoint_state.pending_tool_calls:
    disposition = await ctx.pipeline.resume(
        run=ctx.run,
        checkpoint=ctx.checkpoint_state,
        pending=ctx.checkpoint_state.pending_tool_calls,
        principal=ctx.principal,
    )
    if disposition.suspension is not None:
        return RunOutcome(
            kind=OutcomeKind.SUSPENDED,
            suspension=disposition.suspension,
        )
    await ctx.checkpoint(trigger="tool_batch")
```

**`run.resumed` has one emitter and one condition.** No document says which
of the four resume paths emits it. All of them do, because the condition is
a property of the execution rather than of the path: `run_loop` emits
`run.resumed` at its start when `checkpoint.version > 1` or
`run.attempts > 0` — that is, exactly when this execution did not start the
run. Approval resumption, input resumption, child-run resumption, and lease
reclaim all satisfy it, and a first execution never does.

**Revalidation on approval resumption is the pipeline's job, not the
loop's.** The policy spec's revalidation table voids an approval outright on
an argument, scope, or agent-version change and re-evaluates only on a
policy-version change, and a second `REQUIRE_APPROVAL` becomes a denial.
That is all inside `pipeline.resume` at step 6. The loop sees a
disposition, the same as any other batch.

## Termination, and what happens when a turn says nothing

`select_final_message` is undefined in Section 12.1 and there are three
cases, one of which is a genuine judgement call.

```text
# turn shape                           outcome
text, no tool calls                    COMPLETED with that text
text and tool calls                    not terminal; the batch runs
no text, no tool calls                 see below
```

The third case — a model turn with `StopReason.STOP`, no text, and no tool
calls — is a provider anomaly rather than a design case, and it must produce
something deterministic. Failing the run immediately is the safest reading
and it is wrong in practice: empty turns are overwhelmingly transient, and a
run that fails on one has spent its entire context assembly for nothing.
The loop treats an empty terminal turn as a failed attempt and retries the
step under the ordinary attempt budget; on exhaustion the run fails with
`EmptyModelTurn` and `FailureReason.EMPTY_MODEL_TURN`. This is recorded as
an open question, because the argument for failing immediately — that
retrying a model which produced nothing is unlikely to help and certain to
cost — is not weak.

`StopReason.MAX_TOKENS` on a turn with no tool calls is not an empty turn
and is not a failure: it produces a truncated final message and the run
completes. The gateway's pre-send pairing validation guarantees no dangling
tool call reaches the loop.

`StopReason.CANCELLED` produces `OutcomeKind.CANCELLED` on a partial turn
and never an error, per ADR-0002.

### After the run

Four things happen after a terminal transition and none of them is in
Section 12.1, because Section 12.1's `complete_run` was where they would
have gone.

```text
# hook                     when              failure is
child-run join wake        parent suspended  a parent failure
memory formation flag      any terminal      logged, never fatal
skill background review    COMPLETED only    logged, never fatal
trajectory export marker   any terminal      logged, never fatal
```

All four are enqueued, not executed inline, and all four are enqueued after
the terminal transition has committed. A post-run hook that failed and took
the run's terminal state with it would make a completed run appear failed,
which is the worst available outcome for a hook whose purpose is background
enrichment.

The child-run join is the exception that is genuinely part of the run
lifecycle. When a child run reaches a terminal state, its `finalize` checks
whether every sibling under the same `parent_run_id` is also terminal, and
if so completes the parent's suspended `delegate.run` invocation with the
aggregated result and re-enqueues the parent. Section 27.6's *"the parent
turn does not complete until its child runs reach a terminal state"* is
enforced by the parent being suspended, not by the parent polling. A child
that fails completes the parent's invocation with an error tool result; a
child that exceeds the parent's deadline is cancelled with it, because the
parent's `deadline_at` is copied onto every child at creation.

## Section 13 additions

Section 13 lists twenty-three error classes. Eight more are raised by
documents written since and appear in no taxonomy, and the taxonomy is what
the retry table keys on, so an unclassified error is an error with undefined
retry behaviour.

```text
# class                  raised by            classification
WorkerFenced             heartbeat supervisor  stop, no requeue
BudgetExceeded           BudgetLedger          permanent
RunDeadlineExceeded      deadline timer        permanent
ContextOverflow          build_with_pressure   permanent
ToolLoopDetected         circuit breaker       permanent
ApprovalExpired          approval reaper       permanent
EmptyModelTurn           select_final_message  transient, step retry
ConflictError            run creation, keys    permanent, 409
```

"Permanent" has the meaning the event log spec gives it: the queue never
requeues the run, `runs.failure` is the dead letter, and `max_attempts` is
irrelevant. `WorkerFenced` is in a class of its own — it is not a run
failure at all, because the run is fine and now belongs to someone else.

`ConflictError` covers two cases the corpus raises and never names: an
idempotency-key hash mismatch, and a second run submitted to a session that
already has a non-terminal one. Both surface as HTTP 409. The second is the
user-visible error ADR-0004 predicted the partial unique index would
produce, and the API layer handles it deliberately: if the active run is
`WAITING_FOR_USER` the API routes the text to `POST /runs/{id}/input`
instead, which is Section 27.3's deterministic routing decision and the only
case where a 409 becomes a 202.

### Retry: three loops, three owners

Three retry loops exist and they are frequently confused because all three
have a maximum of three.

```text
# loop        owner              counter            on exhaustion
attempt       model gateway      attempt_number     caller decides
step          run_loop           step.attempt_count run fails
run           the queue          runs.attempts      dead letter
```

The gateway retries within an attempt only while `stream_had_output` is
false. After any output it never retries; it fails, and the loop decides,
because only the loop knows whether partial output was shown, whether the
step is repeatable, and whether budget and deadline permit another try. The
loop's decision rule is short: retry the step if the error is transient, the
attempt count is below three, and both budget and deadline permit; otherwise
fail the run. A retried step rebuilds its request from the same checkpoint,
which is why `build()` purity matters — two builds of one checkpoint must be
byte-identical or the prefix cache is lost on every retry.

The queue's loop is the only one that requeues a run, and *only lease expiry
requeues*. A permanent classification fails immediately and is never
retried, so a run that failed on budget does not come back three times to
fail on budget again.

## Observability

### The step span exists now

The model gateway nests `model.attempt` under "the step span". Section 19's
tree has no step span at any level, so the gateway's statement currently
refers to nothing. The tree gains one level:

```text
agent.run
|-- run.step                      (new; one per step)
|   |-- context.compact           (new; zero or more)
|   |-- context.build
|   |-- model.invoke
|   |   `-- model.attempt         (one or more)
|   |       `-- model.stream
|   |-- policy.evaluate
|   |-- tool.execute
|   |   `-- sandbox.execute
|   `-- checkpoint.save
`-- artifact.store
```

`run.step` carries `step.number`, `step.attempt_count`,
`step.tool_call_count`, `step.compactions`, and `context.prefix_sha256` —
which ADR-0020 requires to be "recorded on every request" and which
currently has no stated carrier anywhere. A span attribute is the right
carrier: it is queryable, it costs nothing at rest, and the invariant it
supports ("more than one distinct value per epoch in one session is a
defect") is a query over spans.

### Six counters that do not exist

Section 19 has ten loop-named metrics and two of them are lifecycle
counters. The following are what an operator actually pages on:

```text
# metric                          type       labels
agent_run_steps                   histogram  outcome
agent_runs_cancelled_total        counter    reason
agent_run_leases_lost_total       counter    phase
agent_runs_requeued_total         counter    reason
agent_context_compactions_total   counter    reason
agent_tool_uncertain_total        counter    tool
agent_run_waiting_seconds         histogram  kind
```

`agent_run_leases_lost_total` labelled by phase is the one that earns its
place fastest: fencing in phases 1 through 5 is free and fencing in phase 9
means two workers may both have touched the world, so the split between them
is the difference between "leases are a bit short" and "there is an
incident". `agent_run_waiting_seconds` is the only measure of whether
approvals are answered, which is a product question the runtime is uniquely
positioned to answer and currently does not.

## Events

This document introduces **no new event types.** Every event the loop emits
is already declared somewhere in the corpus; several are declared in specs
written after Section 6.8 and never folded back into its list. Consolidating
them is a bookkeeping act, not a design act, and it matters because Section
6.8's list is what an implementer reads.

```text
# event                     introduced by
run.fenced                  event-log-and-persistence.md, ADR-0004
run.requeued                event-log-and-persistence.md, ADR-0004
run.waiting_for_user        engineering-plan.md Section 27.3
approval.invalidated        policy-and-approvals.md
policy.profile.loaded       policy-and-approvals.md
model.response.failed       model-gateway.md
context.plan.created        context-engine.md
context.epoch.rotated       context-engine.md
context.compacted           context-engine.md
context.working_state.updated  context-engine.md
context.budget.pressure     context-engine.md
context.budget.exceeded     context-engine.md
projection.rebuild.started  event-log-and-persistence.md
projection.rebuild.completed   event-log-and-persistence.md
```

Two ownership assignments close gaps that were nobody's:

`run.claimed` is appended by `RunQueue.claim`, in the same transaction as
the claim, before the executor is entered. It is the queue's event, not the
loop's, which is why the loop never emits it and why Section 26's first
demonstration trace does not show it — the trace starts at `run.started`.

`run.waiting_for_user` and `run.waiting_for_approval` are appended by
`finalize`, not by the tool that caused the suspension. The tool commits its
invocation row; the executor emits the run-level event. Same reasoning as
the lease: one emitter for one fact.

### Section 26's demonstration trace

Section 6.9 requires a checkpoint after a completed model response. Section
26's mandatory first-demonstration trace shows `run.checkpointed` once,
after the tool call, and not after either model response. A conforming
implementation emits three checkpoints in that scenario and the trace shows
one.

They are compatible because the evaluation harness already defines
`event_order` as a **subsequence** assertion rather than an equality: the
listed events must appear in the listed order, and other events may appear
between them. The trace as written is satisfied by an implementation that
checkpoints six times. This is worth stating explicitly because a reader
takes a mandatory trace for an exact expectation, and an implementer who
suppresses the checkpoint after the first model response to match it has
broken recovery to pass a documentation example.

## Ownership: which process runs what

Six responsibilities in the corpus are attributed to "the runtime" or "the
worker" and belong to neither. The deployment has three roles and this is
the assignment.

```text
# role         hosts
api            FastAPI, approval resolution, input delivery,
               run creation
worker         claim, run_loop, finalize, post-run hook enqueue
maintenance    lease sweep, approval reaper, checkpoint prune,
               projections
```

All three are the same binary with a role flag, per ADR-0001's one
deployable and several entry points. The maintenance role is the addition:
`RunQueue.reclaim_expired`, the approval expiry reaper, checkpoint pruning,
and projection catch-up are each a periodic sweep that must run exactly
once per interval across the fleet, and putting them in the worker means
every worker runs them and they contend.

```text
# sweep                cadence   guard
reclaim_expired        5s        advisory lock 'sweep.leases'
approval reaper        30s       advisory lock 'sweep.approvals'
checkpoint prune       300s      advisory lock 'sweep.checkpoints'
projection catch-up    1s        advisory lock per projection
```

A PostgreSQL advisory lock makes the maintenance role safe to run on every
node: whichever node holds the lock performs the sweep and the others skip
it, so there is no singleton to deploy and no leader election to operate.

The lease sweep must exclude suspended invocations by predicate, per the
tool system: a `tool_invocations` row that is `RUNNING` with
`suspended_kind` set and a released lease is not a dead worker, it is a run
waiting for a human. That predicate is the single most consequential line in
the sweep and it is the one most likely to be omitted, because the row looks
exactly like abandoned work.

Two other assignments that documents left open: the **consolidation
watermark** is advanced by the consolidation job, not by the loop; the
**session `snapshot_watermark`** is written by the session service at
session open, not by the loop. Both were phrased in the memory specs in a
way that could be read as the runtime's responsibility, and neither is.

## Working state has two writers and both are now named

The context engine says a typed control tool writes working state and
*"the runtime is the second writer"*, with no function and no call site.
The runtime's write is phase 10, and it is narrow by design:

```python
# runtime/working_state.py -- pure; the write is the checkpoint
def apply_runtime_carry(
    state: WorkingState, disposition: BatchDisposition, step: Step
) -> WorkingState: ...
```

It sets exactly four fields: the last tool error's reason code, the count of
consecutive denials the breaker is tracking, the outstanding question id
when a step suspended on `ask_user`, and the step number. It never writes
anything the model produced, because working state parsed from prose is
exactly what ADR-0020 forbids, and it never evicts a constraint — at the cap
the write fails visibly rather than dropping the oldest entry.

Because working state lives in the checkpoint, the runtime's write *is* the
checkpoint write. There is no second persistence path and no window in which
the two writers disagree.

At a turn boundary the carry rules table in the context engine applies per
field; the loop applies it in `seed_checkpoint`, when the next run's version
1 is built, rather than at the end of the previous run. Applying it on the
way in means a run's working state is a function of the session's history
rather than of what the previous run's last write happened to be, which is
the same reasoning that makes the session-history projection authoritative
over the previous run's checkpoint.

## Transaction boundaries in the loop

Section 12.2 forbids holding a transaction across provider, tool, or sandbox
I/O, and Milestone 2 gates on it: *"no transaction is open across an `await`
that performs provider, tool, or sandbox I/O."* Mapped onto the phases,
there are seven short transactions per step in the worst case and the rule
is that every one of them closes before the next `await` on the network.

```text
# transaction                        phase  contains
compaction checkpoint write          4      one insert
model_calls row + usage rollup       7      two writes, one event
checkpoint after model response      8      one insert
per tool call: authorize             9      invocation + event
per tool call: effect watermark      9      one update
per tool call: result + event        9      two writes
checkpoint after the batch           11     one insert
terminal transition + lease release  --     two updates, one event
```

Each row is one transaction that opens and closes without an intervening
network `await`. The two that would be easiest to get wrong are the model
call — where the temptation is to open a transaction before the stream and
commit after it — and the tool result, where the temptation is to hold the
authorize transaction open across the tool's own I/O. The structural gate
that detects both is an AST check for an `await` on a call that reaches an
adapter inside a session context manager, and it belongs to the same
registry as the import-boundary walk.

Every worker write in these transactions carries the fencing predicate:

```sql
-- every worker write asserts its own epoch
UPDATE runs
   SET status = :new_status,
       updated_at = :now
 WHERE id = :run_id
   AND lease_owner = :worker_id
   AND lease_epoch = :lease_epoch;
-- zero rows affected means fenced: stop, do not retry
```

## Milestones

The loop is not one deliverable. Section 21 already distributes its
capabilities across five milestones and this table makes the distribution
explicit, because "implement the runtime loop" in Milestone 1 currently
reads as though the whole of this document were M1 work.

```text
# capability                              milestone
Step, run_loop, RunExecutor, finalize     M1
Clock, IdFactory                          M1
BudgetLedger, STEP and ATTEMPT scopes     M1
select_final_message, EmptyModelTurn      M1
CancellationToken, points 1-3             M1
RunQueue claim, lease, heartbeat          M2
supervisor task, fencing, run.fenced      M2
CheckpointRepository, delta chain         M2
seed_checkpoint, resume, run.resumed      M2
maintenance role and the four sweeps      M2
Suspension APPROVAL, pipeline.resume      M4
cancellation of a waiting run             M4
cancel endpoint, points 4-6, sandbox      M5
Suspension USER, ask_user, input          M5
Suspension CHILD_RUN, the join            M6
build_with_pressure, Compactor, cap 2     M7
apply_runtime_carry                       M7
```

**Cancellation is split rather than assigned to one milestone**, which
resolves a real disagreement: Section 21 places cancellation in M5 while the
evaluation harness tags case 15 "cancellation reaches the worker" as M4. Both
are right about different halves. The state machine's `CANCELLED` terminal
state and the loop's observation of a token are M1 — they are a property of
the loop and case 10's budget stop already needs the same machinery. Cancelling
a *waiting* run needs pause to exist, so it is M4, which is what case 15
actually tests. The public endpoint, propagation into an executing sandbox,
and the operator surface are M5, which is what Section 21 means. Recorded as
an open question in case the intent was that no cancellation at all ships
before M5.

## Contradictions resolved

Twenty conflicts between documents meet in the loop. Each is listed with the
resolution and the section that argues it, so a reader who hits one knows
whether it was seen.

```text
# conflict                        resolution
1  cancellation cadence           one token, six points, effect rule
2  cancellation milestone         split M1 / M4 / M5
3  budget check placement         three scopes; "after" is record
4  retry ownership                three loops, three owners, named
5  checkpoint API name/shape      CheckpointRepository canonical
6  checkpoint content model       stored vs materialized types
7  trigger vs Section 26 trace    event_order is a subsequence
8  flat loop vs suspension        outcome + finalize, one finally
9  who resumes                    process cold, pipeline at step 6
10 heartbeat return type          RunQueue canonical, bool
11 claim signature                RunQueue canonical
12 token classes                  five; RunUsage gains cache-create
13 tool event names               Section 6.8 canonical; case fixed
14 single active run              409, except WAITING_FOR_USER
15 loop-detection ownership       one breaker, one counter
16 build() purity                 build_with_pressure, cap 2
17 tool set into build()          via run.session_id and the plan
18 trajectory-export milestone    already open; not the loop's
19 Clock ownership                declared here, M1
20 two approval timers            reaper owns the approval, bridge
                                  owns the sandbox
```

Three of those need a sentence more than the table gives them.

**Token classes (12).** `RunUsage` has four token fields; the model gateway
adds `cache_creation_input_tokens` as a fifth class on both `ModelUsage` and
`RunUsage`; and the event log spec says `runs.usage` is "unchanged in
shape". The last statement is about the *rollup relationship* — that
`runs.usage` remains a rollup of `model_calls` maintained in the same
transaction — not about the field count. `RunUsage` has five token classes.

**Tool event names (13).** Section 6.8 declares `tool.call.proposed`,
`tool.call.authorized`, `tool.call.completed`, `tool.call.failed`;
ADR-0021 explicitly adds no tool event. One case in the evaluation harness
asserts `tool.proposed` / `tool.authorized` / `tool.succeeded`, which are
not events. The case is wrong and is corrected in that document; Section
6.8's names are canonical.

**Single active run (14).** Section 27.5 allows "reject **or** queue";
ADR-0004's partial unique index makes queueing impossible at the database
level. 0.1 rejects, with `ConflictError` and HTTP 409, except where the
active run is `WAITING_FOR_USER` and the deterministic routing rule sends
the text to that run's input endpoint instead. Queueing is a later feature
that would need a different constraint, and adopting it later costs a
migration rather than a redesign.

## Hard gates

Failing one of these blocks the milestone. They are registered in the gate
registry with identifiers, like every other gate.

1. **One terminal writer.** A structural gate asserts that
   `RunRepository.transition` and `RunQueue.release` are called from exactly
   one module, `runtime/executor.py`. A second call site anywhere under
   `agent_core` fails the build. This is the gate that keeps the
   suspension fix from eroding. **M1.**
2. **No ambient time.** No module under `agent_core` outside the clock
   adapter references `datetime.now`, `datetime.utcnow`, `time.time`, or
   `time.monotonic`. **M1.**
3. **No ambient identity.** No module under `agent_core` outside the id
   adapter calls `uuid.uuid4` or `uuid.uuid1`. **M1.**
4. **Every step has a persisted identity.** For every run in the suite,
   `runs.step_count` equals the count of distinct `step_number` values in
   `model_calls` for that run. **M1.**
5. **The budget stops the loop.** A run whose `max_steps` is reached fails
   with `FailureReason.MAX_STEPS_EXCEEDED` and is never requeued; a run
   whose cost limit is reached mid-retry fails at the attempt check rather
   than after the attempt. **M1.**
6. **No transaction across external I/O.** No `await` on a call that
   reaches a provider, tool, or sandbox adapter occurs inside an open
   session context. Asserted by AST walk, not by convention. **M2.**
7. **The lease is released exactly once.** In every case in the suite,
   including the crash and fence cases, the count of `release` calls per run
   execution is at most one, and a suspended run's lease is released before
   its `run.waiting_*` event is visible to a second worker. **M2.**
8. **Fenced workers do not write.** In the `kill_worker` intervention cases,
   after a fence the fenced worker performs zero row-affecting writes other
   than the single `run.fenced` append. **M2.**
9. **Checkpoint dispensability.** Deleting all of a run's non-terminal
   checkpoints and resuming reaches the same terminal state, including for a
   run whose version 1 was deleted. **M2.**
10. **Resume is idempotent on the pipeline boundary.** Resuming a run whose
    checkpoint carries `pending_tool_calls` re-enters the pipeline at step 6
    and produces no duplicate `tool.call.proposed` event and no second
    effect for any call whose watermark was already set. **M2.**
11. **A waiting run holds nothing.** While a run is in either `WAITING_*`
    state, it holds no lease, no worker slot, and no open transaction, and
    the lease sweep does not reclaim it. **M4.**
12. **Cancellation never abandons an effect.** No case produces a run in
    `CANCELLED` with a `tool_invocations` row whose `effect_sent_at` is set
    and whose status is `cancelled`. **M5.**
13. **`build()` is called with a checkpoint that fits.** Every
    `context.build` span is preceded in its step by a pressure measurement,
    and no `model.request.started` carries a request over the plan's budget.
    **M7.**
14. **Two builds of one checkpoint are byte-identical.** The step-retry path
    rebuilds from the same checkpoint and produces the same
    `prefix_sha256`. **M7.**

## Decisions

1. **The loop computes an outcome; one executor performs every terminal
   action.** `run_loop` never transitions a run, releases a lease, or writes
   a terminal event. `finalize` does all three, in one place, for all five
   outcomes.
2. **`Step` is a runtime value object with a persisted identity on
   `model_calls`, not a table.** One additive column gives every step an
   identity, including the steps that produce no tool calls, which are
   currently invisible.
3. **`Turn` gets no domain object.** `run == turn`; a `Turn` model would be
   a second name for a run row.
4. **Nine fields are added to `Run`**, six of which are already columns
   introduced by the event log spec and three of which — `agent_id`,
   `agent_version`, `deadline_at` — are denormalized copies that make the
   run self-describing and the deadline indexable.
5. **`Clock` and `IdFactory` are ports and ambient time and identity are
   structural-gate violations.** The evaluation harness pins both; the
   runtime is their heaviest reader.
6. **The agent version is pinned at run creation and never re-resolved.** A
   deploy mid-run does not change the agent underneath a paused approval.
7. **The principal is resolved once per execution**, except at approval
   resumption, where the policy engine revalidates by design.
8. **One `CancellationToken` per run serves the loop, the tool executor, and
   the sandbox.** Six observation points, and a cancellation observed after
   an effect watermark is set does not abandon the call.
9. **Budget has three scopes and "after" means "record".** Recording usage
   and evaluating the limit are one operation, in one transaction, because
   separating them creates a window in which the run is over budget and
   nothing knows.
10. **The heartbeat is a supervisor task that also watches the deadline and
    polls for cancellation.** One timer, one query, three concerns that are
    all "has the outside world changed its mind".
11. **A fenced worker aborts its in-flight model stream and appends exactly
    one event.** The abort is safe because nothing it produced could be
    committed; the append is legal because the event log is sequence-guarded
    rather than epoch-guarded.
12. **Compaction happens in the loop, before `build()`, capped at two per
    step, and `ContextOverflow` is permanent.** The compactor returns the new
    checkpoint and the loop adopts it.
13. **The pinned tool set reaches `build()` through `run.session_id` and
    `ContextPlanner.current`.** Section 7's four-parameter signature is
    unchanged.
14. **`full` has a rule the call site can evaluate**: version 1, every
    eighth version, compaction, suspension, and terminal. The `checkpoints`
    table gains `full` and `base_version`.
15. **The checkpoint has a stored form and a materialized form.** Section
    6.9's inline `conversation` is what the repository returns; event
    references and deltas are what it persists. Both existing statements are
    true of different types.
16. **`seed_checkpoint` is a function with two call sites**, so deleting a
    run's checkpoints and resuming can reach the same terminal state.
17. **Suspension is one mechanism with three kinds**, and a child-run wait
    reuses `WAITING_FOR_APPROVAL` rather than adding a state.
18. **Resumption is a cold process start and a warm pipeline entry.** The
    worker claims a `QUEUED` run from scratch; the pipeline re-enters at
    step 6 for each pending call.
19. **`run.resumed` is emitted by the loop whenever the execution did not
    start the run**, which covers all four resume paths without enumerating
    them.
20. **An empty terminal turn is retried as a failed step**, and fails the run
    with `EmptyModelTurn` on exhaustion.
21. **Post-run hooks are enqueued after the terminal transition commits and
    can never fail the run.** The child-run join is the exception and is part
    of the lifecycle.
22. **This document adds no event types.** It consolidates fourteen
    introduced elsewhere and assigns owners to `run.claimed` and the two
    `run.waiting_*` events.
23. **`run.step` is a span and `prefix_sha256` is one of its attributes**,
    which gives the model gateway's "nested under the step span" something
    to refer to and ADR-0020's per-request hash a carrier.
24. **Deployment has three roles and the sweeps live in `maintenance`,
    guarded by advisory locks.** No leader election, no singleton to
    operate.
25. **Section 26's demonstration trace is a subsequence.** An implementation
    that checkpoints after every model response satisfies it; one that
    suppresses checkpoints to match it literally has broken recovery.

## Open questions for review

1. Should cancellation ship in three milestone slices as this document
   proposes, or does Section 21's Milestone 5 placement mean no cancellation
   at all before M5? The slice is cheap to collapse and expensive to
   introduce late.
2. Should a run waiting on a child run get its own `WAITING_FOR_CHILD`
   state? Reusing `WAITING_FOR_APPROVAL` with a typed suspension kind avoids
   amending the state machine, at the cost of a status that does not mean
   what it says.
3. Should an empty terminal model turn retry the step or fail the run
   immediately? Retrying costs a second context assembly on a model that
   just produced nothing.
4. Is eight the right full-snapshot interval for checkpoints? It bounds
   delta reconstruction at seven reads and is otherwise arbitrary.
5. Should the maintenance sweeps run in every process under advisory locks,
   or in a dedicated deployment? The advisory-lock form has no singleton to
   operate and makes every node do a little wasted work every interval.
6. Should the fenced worker's in-flight model stream be aborted, as decided
   here, or allowed to finish so its output can be logged for diagnosis?
   Aborting saves tokens; finishing preserves evidence.
