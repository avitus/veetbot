# ADR-0115: The tool-call budget ends research, never the answer

- Status: Accepted (authorized by the repository owner, 2026-09-21); amends
  ADR-0078
- Date: 2026-09-21
- Related: Sections 6.5 and 12 of the engineering plan; ADR-0002, ADR-0023,
  ADR-0078
- Detailed design: `docs/plan/runtime-loop.md`

## Context

On 2026-09-21 an interactive research run — a table of independent-living
facilities with phone, address and rent — failed on step 8 with
`BudgetExceededError: tool-call budget exceeded`. It had used 29 of its 32 tool
calls and asked for five more. The runtime saw that the batch would not fit,
used that fact only to disable parallel execution, ran all five calls serially,
recorded 34, and failed the run. Every result was discarded and the model never
received the turn in which it would have written the table it had nearly
finished. The calls were legitimate: 22 searches and 12 fetches over roughly
ten facilities, with context at 85k of 272k tokens and 8 of 32 steps used.

Three properties of the loop combined to produce this outcome.

1. `BudgetScope.STEP` failed a run whose tool-call count *equalled* its limit,
   so a run that spent exactly its last allowed call was failed before the
   model could answer from what it had.
2. The final-synthesis reserve (ADR-0078) watched steps, model calls and cost
   but not tool calls, so a run could run out of tool calls without ever
   being told to answer.
3. The interactive default agent set no reserve at all: ADR-0078 rejected
   "automatically reserve a fixed fraction of every run" because it would
   change existing interactive budgets. Those budgets are now what fails.

## Decisions

1. **A batch is fitted to the remaining tool-call budget before anything
   runs.** The loop dispatches only the calls that fit and answers each
   refused call with a platform-trusted `tool.budget_exhausted` result that
   tells the model to answer from the evidence it already has. A refused call
   never reaches the tool executor, so nothing runs unaccounted for, and the
   refusals join the conversation before the `tool_pending` checkpoint so a
   resumed step re-dispatches only the fitted calls.
2. **Reaching `max_tool_calls` is not a failure while an answer is owed.** An
   exhausted tool-call budget puts the next request into synthesis-only mode
   even when every reserve is zero. A tool call returned in that mode fails
   closed with `SynthesisReserveViolation` naming `tool_calls`, exactly as the
   other dimensions do, so the loop still terminates within one step.
3. **`BudgetScope.STEP` no longer enforces `max_tool_calls`.** A fourth scope,
   `BudgetScope.TOOL_CALL`, carries that rule for flows that dispatch a single
   call outside the model loop and cannot fit a batch (email tasks). The
   post-dispatch `record_tool_usage` check is unchanged and remains the
   authoritative accounting; after decision 1 it can no longer fire in the
   model loop.
4. **`RunLimits` gains `synthesis_reserve_tool_calls`**, validated strictly
   below `max_tool_calls` like its siblings. Once the remaining tool calls are
   at or below the reserve, the next request is synthesis-only.
5. **The interactive defaults change.** `run_defaults` in
   `runtime/limits.yaml` become 32 steps, 24 model calls, 64 tool calls, a
   model-call reserve of 2 and a tool-call reserve of 4. The composition root
   copies the reserves onto the default agent's limits. ADR-0078's rejection
   of a fixed automatic fraction stands: these are explicit, versioned knobs,
   not a percentage, and all-zero reserves still preserve the ordinary loop.

## Consequences

- A research run that legitimately needs its whole budget ends with an answer
  built from what it found, and states the gap, instead of a failure that
  discards its work.
- Interactive runs are told to write up with 4 tool calls or 2 model calls
  left, so the wall is rarely reached; when it is, the fit rule guarantees no
  call runs past it.
- Schedule revisions and delegated children keep their existing limits; the
  new reserve field defaults to zero for them and is available to opt in.
- The evaluation-case defaults (`max_tool_calls` 32, `max_model_calls` 16) are
  unchanged; they bound fixtures, not production runs.
- `tool.call.proposed` still carries no arguments. The rule in
  `tool-system.md` that tool events carry identity and classification while
  `tool_invocations` carries the payload was considered and kept; auditing a
  run's queries after the fact remains a read of the invocation rows.

## Alternatives considered

- **Raise `max_tool_calls` alone:** rejected because the run would still fail
  hard at the new limit with its work discarded, and would then hit
  `max_model_calls` at 16 first.
- **Refuse the whole over-budget batch:** rejected because it wastes the calls
  that would have fitted; the model gets more evidence from a trimmed batch
  and a refusal for the remainder.
- **Fail the run before dispatch instead of after:** rejected because it fixes
  the accounting but not the outcome; the answer is still lost.
- **Keep the tool-call rule in `BudgetScope.STEP` and special-case the model
  loop:** rejected because the ledger would then have to know which caller
  can trim a batch; a named scope states the rule once.
