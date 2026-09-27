# ADR-0136: Synthesize before the identical-tool-call limit

- Status: Accepted as an engineering decision within the owner's runtime repair
  request of 2026-09-27
- Date: 2026-09-27
- Related: ADR-0023, ADR-0115, ADR-0130; engineering plan Section 12.5
- Detailed design: `docs/plan/tool-system.md`, `docs/plan/model-gateway.md`

## Context

A repository research conversation ended with `ToolLoopDetected` after useful
research. The model had fetched the same document repeatedly among other reads.
Large results make the context builder replace older tool results with pointers;
refetching then consumes the same run-wide identical-call counter. The breaker
was enforcing its specified limit, but the run had no final-answer opportunity
before another tool request failed it. Raising the limit would spend more on
repetition without addressing that failure mode.

## Decision

1. After a completed batch leaves any identical-call counter at one below the
   configured threshold, the next model turn is synthesis-only. With the default
   threshold of five, four completed identical requests trigger this control.
   ADR-0130's evidence resets happen first: progressing browser observations
   do not trigger it. The checkpointed counter also governs resumed runs.
2. The runtime adds a volatile platform instruction to give the best-supported
   answer, identify remaining gaps, and explain that research stopped because
   it was repeating. It must not claim unfinished work is complete. The control
   contains no tool arguments or retrieved text and does not enter the checkpoint
   or frozen prefix.
3. `ModelRequest.tool_choice` is `None` by default, preserving provider defaults,
   or `"none"` for synthesis. OpenAI Responses and native Chat Completions send
   `tool_choice: "none"`; Anthropic sends `tool_choice: {"type": "none"}`.
   Definitions and prior call/result pairs remain intact for history replay.
   The XML fallback omits its tool-use instruction and schemas. The existing
   budget-synthesis control also uses this field.
4. A tool request returned in repeated-tool synthesis fails with
   `ToolLoopDetected` before any dispatch, even if its arguments differ. A batch
   that reaches the hard threshold before it runs still fails under the existing
   breaker. There is no extra execution allowance and no automatic restart.
5. All budget, deadline, cancellation, model-error and empty-turn rules continue
   to apply. Synthesis consumes a normal model call and its usage. Exhausting a
   hard budget can still prevent an answer; overlapping budget synthesis retains
   its existing failure classification. The denial and uncertain-effect rules
   are unchanged.

The provider mappings follow the [OpenAI API tool-choice contract](https://platform.openai.com/docs/api-reference/responses)
and [Anthropic tool-choice contract](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools).

## Verification and limits

Regression coverage exercises the configured threshold, interleaved large web
reads whose results are actually elided, batches, approval resumption, model
retries, exhausted model-call budgets, and refusals before dispatch. The shared
provider contract checks all three native transports and the XML fallback with
both ordinary and synthesis requests. Existing changing-page, evidence-reset
cap, denial and loop evaluations remain authoritative.

This repairs the abrupt loss of a concluding response, not the quality of a
model's research strategy. The response may be partial. It does not add a tool
for rereading an elided event, recover an already failed conversation, alter
context budgets, add a gate or advance a milestone. No migration is required.
