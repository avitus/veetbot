# ADR-0135: OpenAI Chat cache identity and discoverable history boundaries

- Status: Proposed (owner-requested repair, 2026-09-26)
- Date: 2026-09-26
- Amends: ADR-0002 and ADR-0007's automatic-only OpenAI translation
- Related: ADR-0131, ADR-0132; engineering plan Sections 10.1 and 11
- Detailed design: `docs/plan/model-gateway.md`, `docs/plan/context-engine.md`

## Diagnosis

The reported production run's three completed-model events confirm inputs of
53,571 / 55,273 / 56,035 tokens and cache reads of 4,543 / 0 / 4,543.
Its Region A hash and epoch remain unchanged. Recall hashes change on every
step. The first call asks the owner a question, then resumes with another input;
the second calls the calculator. These are not three identical prompts.

The offline regression uses the real EventContextPlanner, BudgetedContextBuilder
and Responses payload translator. With small tool results, unchanged state and
no recall, the first wire difference is byte 1,149 at input item 8: the old
reasoning item becomes a tool call. All earlier items and tool definitions are
identical, with history cut zero and no pressure yields. Truncation is separately
reproduced by exceeding the tool-result budget; it rewrites the oldest result.
Changing recall, working state or the date changes bytes after carried history.
Those legitimate changes must stay visible. A history cut under pressure also
invalidates history; production's three aggregate events alone cannot prove
which historical result bytes were retained. No claim is made to have captured
the production wire payloads.

The live control reproduces the miss without any history truncation: a changing
working-state row after stable history is sufficient. Automatic caching writes
the later, volatile input boundary, not the unchanging history boundary. A key
alone does not fix it. The latest-only reasoning replay can prevent growing
active-loop prefix reuse, but cannot explain invalidating earlier history.

## Current provider evidence

Checked 2026-09-26 against the official
[caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
[Astra profile](https://developers.openai.com/api/docs/models/gpt-6-astra) and
[reasoning guide](https://developers.openai.com/api/docs/guides/reasoning):

- Astra routes automatically; keys separate cache accounting. Earlier models
  use keys for routing, with overflow possible above about 15 requests/minute.
  Keys do not guarantee machine affinity.
- Astra's minimum is 1,024 visible tokens, with exact boundary reporting.
  The 128-token rounding rule belongs to earlier models.
- Astra supports explicit markers alongside implicit caching and a 30-minute
  TTL. Older `in_memory`/`24h` retention choices do not apply to this profile.
- Its standard per-million input/read/write/output prices are $10/$1/$12.50/$50,
  matching `openai.yaml`. Writes replace ordinary input charges. The adapter
  previously ignored reported write tokens, understating cost.
- `store=False` returns encrypted reasoning without `include`; the legacy
  include value remains accepted. The live response confirmed encrypted content
  without requesting it. The existing translator retains and replays it, so
  missing `include` is not a reasoning correctness bug on this API.

## Decision

1. Add optional `CacheHints.session_key`. The context engine hashes canonical
   tenant/session identity to 128 bits with a `session-` prefix. Exclude run,
   step and plan epoch. OpenAI translates it verbatim; the other adapters ignore
   it. Hashing avoids raw identifiers, not linkability. Separate sessions lose
   shared Region A reuse in exchange for isolated accounting and routing on
   earlier models. Epoch changes already change the prompt where necessary.
2. Add optional `CacheBreakpoint.through_input_item`. The builder chooses the
   last user/tool-result boundary no later than its existing history boundary.
   This remains discoverable through implicit lookback when a later request
   moves the explicit marker. Anthropic retains `through_item`. Both fields
   are optional so old plans and requests remain readable.
3. Enable explicit caching on the shipped OpenAI profile. Translate the system
   and supplied history boundaries only when support and write pricing are
   declared. Keep implicit caching's slot and use at most three supplied slots.
   Count unsupported, duplicate and excess hints as dropped. Do not place a
   tool-definition marker or invent boundaries in the adapter.
4. Keep the provider's retention default. Neither a one-hour nor a 24-hour
   option is supported for these models; no speculative retention price or
   Anthropic TTL mapping is introduced.
5. Normalize and price OpenAI write usage. Keep the existing checkpoint-only,
   latest-turn opaque reasoning lifetime and `store=False`.
6. Reuse the domain-level identity hashing primitive in operational Email and People email
   import requests, which already have tenant and session identities. Memory
   extraction and distillation have job/source scopes that need a separate
   identity decision and remain open. Folder grouping and comparison evals also
   remain unchanged. Structured compaction is extractive and makes no model
   request. Ordinary People import tool loops use the Chat builder.

This extends the request sketch without changing its provider-neutral ownership,
security boundaries, context budgets, or acceptance criteria. No migration or
new dependency is required. The profile changes registry identity; existing
runs must drain or use the existing registry-change handling at deployment.

## Live verification

The dev Doppler key drove the real planner, budgeted builder and streaming
Responses adapter with Astra medium reasoning. The prompt used 65,000 characters
of the engineering plan as genuine reference history, five sequential dependent
calculator calls, a final answer and two follow-up turns. A changing working-state
objective models the production instability. Continuations follow `loop.py`:
only the latest tool turn's opaque items return, cleared at the end of a run.
No repeated filler, production message content or raw reasoning was used.

| Call | Without key/markers: input / cached | Key only: input / cached | Key + markers: input / cached |
| --- | --- | --- | --- |
| 1 | 14,966 / 0 | 14,966 / 0 | 14,966 / 0 |
| 2 | 15,097 / 0 | 15,102 / 0 | 15,102 / 14,686 |
| 3 | 15,231 / 0 | 15,238 / 0 | 15,235 / 14,686 |
| 4 | 15,361 / 0 | 15,376 / 0 | 15,368 / 14,686 |
| 5 | 15,512 / 0 | 15,529 / 0 | 15,517 / 14,686 |
| 6, answer | 15,676 / 0 | 15,693 / 0 | 15,681 / 14,686 |
| 7, follow-up | 15,790 / 0 | 15,795 / 0 | 15,800 / 14,686 |
| 8, follow-up | 15,913 / 0 | 15,915 / 0 | 15,924 / 15,576 |
| Total cost, including writes | $1.5643650 | $1.5651150 | $0.3724445 |

The fixed calls after the first read 93.7–98.6% of the preceding input, and cost
76.2% less in this small live sample. Without changing state, an earlier control
already achieved roughly 99% within a tool loop without a key, then missed on
both follow-ups. This rules out a missing key as a sufficient cause; it does not
measure production routing load or guarantee a cache hit under provider overflow.
Production verification remains pending deployment and a subsequent Chat run.

## Limits

Implicit lookback is bounded. Long runs, history truncation, compaction, new
instructions, model changes, expiry and provider availability can still miss.
Do not freeze recall corrections or working state to improve a cache metric.
Persisting every reasoning item across turns would be a separate privacy and
runtime decision, not part of this repair. The repair uses the history-window and ADR-0132 prerequisites now present on
origin/dev. The provider-neutral request extension is the only new cache policy.
