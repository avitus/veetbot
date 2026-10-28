# ADR-0242: Tool-result truncation moves in blocks to keep the body cached

- Status: Accepted as an engineering decision within the owner's repair request
- Date: 2026-10-23
- Amends: `context-engine.md` "Yield order under pressure" item 3 and "The
  history cache window"
- Related: ADR-0078, ADR-0130, ADR-0135, ADR-0229
- Amended by: ADR-0258 (2026-10-26), which keeps definition answers whole and
  notes truncation; no model-callable tool reads a pointer back
- Scope: Existing context-builder behavior; no milestone or gate moves

## Context

On 2026-10-23 the owner asked a chat bound to the Amazon website profile to
compile every book they had read from the Kindle reading history. The run made
76 model calls and 74 tool calls, cost USD 27.63 against the browser-task limit
of USD 30 (ADR-0130), and ended in the cost reserve with a partial list of 22
titles. The refusal the owner saw, `tool.budget_exhausted` on `browser.observe`,
was the designed cost-reserve transition (ADR-0229). The defect was upstream of
it.

Through step 43 the run behaved as ADR-0130 sized it: each step added about
2,400 fresh tokens, the provider cached the rest, and a step cost USD 0.05 to
0.10. At step 44 the aggregate tool-result estimate reached its budget (a
quarter of the body, about 64,000 tokens on GPT-6 Astra) and the builder began
truncating the oldest tool result to a pointer. Because the minimal truncation
moves by exactly one result per step, every later request rewrote the oldest
bytes of the body. The provider's prefix cache then covered only Region A:
cached input fell from about 62,000 tokens to 6,858 and each step wrote 58,000
to 64,000 tokens fresh at the uncached price. Thirty of the thirty-two steps
from 44 to 75 cost about USD 0.78 each, USD 23 of the USD 27.63 total, and the
run spent its budget on re-reading a context that had not changed.

The specification already said that truncation "can change history itself and
legitimately lose that hit". It did not anticipate that the loss would recur on
every step for the rest of a long tool loop.

## Decision

Tool-result truncation stays oldest first and stays a pure function of the
items the builder is given, but it advances in blocks. When the aggregate
tool-result estimate exceeds its budget, the builder truncates the oldest
results until the aggregate fits, then continues truncating until the tokens
it has truncated reach the next multiple of one quarter of the tool-result
budget. The truncated prefix therefore changes at most once per quarter budget
of new results, and the requests between those changes repeat the whole body
before the newest exchange byte for byte.

The block is derived from the budget alone, so the same checkpoint and plan
produce the same request on every rebuild. The pointer text, the artifact or
event reference it names, the never-yield set, the yield order, and the
budgets are unchanged. Nothing is frozen or suppressed to improve caching: the
model loses up to one quarter budget of old tool results a little earlier than
the minimal rule would have taken them. Each pointer still names the event or
artifact that keeps its content, but no model-callable tool reads it back
(corrected by ADR-0258).

The history cut and compaction are not changed by this decision. The history
cut did not bind in the run above; if a long run shows the same rolling loss
there, it needs its own evidence and decision.

## Consequences

- A long browser or research loop keeps its cached body across the steps
  between block boundaries. On the run above the thirty-two affected steps
  would have paid for about three uncached reads instead of thirty.
- A quarter budget of recent tool results may become pointers up to a quarter
  budget earlier than before. No model-callable tool reads a pointer's content
  back (corrected by ADR-0258): a finding the model still needs belongs in
  working state, citing the event of the result it came from.
- `context.budget.pressure` goes on recording `tool_results` on every step on
  which anything is truncated; the event does not distinguish a step that
  moved the boundary from one that repeated it.

## Verification

`tests/unit/test_context_tool_result_truncation_blocks.py` pins that the first
truncation extends to the quarter-budget boundary, that the truncated set is
identical across consecutive steps within a block, and that it advances one
block at a time. The test failed against the minimal rule on all three counts.
The existing context and synthesis partitions still pass.
