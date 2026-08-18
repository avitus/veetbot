# ADR-0002: The provider-neutral model protocol

- Status: Accepted
- Date: 2026-07-25
- Related: Sections 2.3 (provider list), 6.5 (usage, cost, and cost source),
  6.6/6.7 (conversation items, reasoning items), 6.8 (event types), 6.9
  (checkpoints), 7 (`ModelProvider` port), 10 (all subsections — normalized
  request, streaming field mappings, model policy, turn shape, routing), 12.3
  (attempt IDs and crash recovery), 13 (error taxonomy and retries), 15
  (schema), 19 (telemetry), 20 (evaluation), Milestones 1 and 3;
  ADR-0006/0007 (no private reasoning storage; provider-neutral reasoning
  state), ADR-0010 (live event transport), ADR-0012 (open and self-hosted
  models)
- Detailed design: `docs/plan/model-gateway.md`

## Context

Section 10.2 pins the exact streaming field mappings for OpenAI and Anthropic,
which is the hard part of provider neutrality and is already done. What is not
done is the layer those mappings are supposed to feed. Roughly twenty types the
plan uses at call sites are never defined — `ModelUsage`, the six streaming
event classes, the five `ConversationItem` members, `ContentPart` and its
variants, `PendingToolCall`, `FakeModelScript`, the neutral `stop_reason`
vocabulary, the three error classes, the usage repository port, and the model
registry schema among them. Nine cells of the mapping table are blank or
ambiguous, including Anthropic's `server_tool_use`, the in-band `<think>`
representation that ADR-0012 calls "the third representation", and the fifth
token class the plan says to "track" without giving it a home.

Twelve statements across the plan and the specs are in tension. Retry ownership
is placed in the adapter in one sentence and in application code in another.
Provider pinning per run is required by Section 10 and contradicted by
Milestone 10's availability routing. There is no model-call timeout field
anywhere, while tools have one. There is no usage table in Section 15, only a
JSONB column on the run, which cannot answer per-attempt questions that budget
enforcement needs to ask. `ProviderReasoningItem.trust_level` defaults to
`PLATFORM`, the highest tier in a system whose policy engine reads trust tiers
to decide restrictiveness.

Meanwhile the acceptance criteria are specific and unforgiving: no
provider-specific code in the runtime, the OpenAI SDK importable only from the
OpenAI adapter, tool-call IDs preserved, no API keys in logs or events, and one
contract suite passing against OpenAI, Anthropic and an OpenAI-compatible
endpoint. Those criteria cannot be tested against a protocol whose types do not
exist and whose stream has no stated shape.

The failure mode this ADR exists to prevent is specific. A provider-neutral
protocol with undefined edges becomes the first provider's protocol with
translation layers bolted on, and the second adapter is where that becomes
visible and expensive.

## Decision

1. **The gateway is a translator, not a decision-maker.** It converts a
   `ModelRequest` into a provider call and a provider stream into normalized
   events. It does not choose models by looking at content, decide when to
   retry after output has been seen, place cache boundaries, or interpret
   reasoning. Every one of those belongs to a component that has the context to
   do it well, and the gateway has none of them.
2. **The normalized stream has six invariants**, enforced by a validator shared
   by all adapters: contiguous `sequence` from zero, exactly one terminal event
   (completed or failed), contiguous and ordered deltas per `item_index`,
   `call_id` and `name` known at tool-item start, `UsageEvent` advisory only,
   and no raw provider error text, keys, or authorization headers on any event.
   A violation is an adapter defect, not a condition callers tolerate.
3. **`UsageEvent` is provisional; `ModelCompletedEvent.turn.usage` is
   authoritative.** This resolves the ordering contradiction with the
   one-terminal-event rule without inventing a reconciliation step, and it lets
   the OpenAI adapter emit no `UsageEvent` at all while the Anthropic adapter
   emits an early one for live cost meters.
4. **One shared assembler folds events into turns for every adapter.** Adapters
   emit events only. This is what makes the contract suite meaningful: the same
   code produces the turn on every provider, so a difference in the turn is a
   difference in the events, which is the thing under test.
5. **Malformed tool arguments are a modelling failure, not a run failure.** The
   assembler produces a tool call with empty arguments and a recorded parse
   error, and the runtime answers it with an error tool result so the model can
   correct itself. `raw_arguments` is retained on every tool call, successful or
   not, because replay and trajectory export need the emitted bytes rather than
   a re-serialization.
6. **Anthropic `server_tool_use` is a protocol error in 0.1.** A tool executed
   inside the provider bypasses the policy engine entirely, so mapping it onto
   an ordinary tool call would let a class of side effects through with no
   policy decision. Refusing loudly forces the policy conversation instead of
   deferring it.
7. **In-band `<think>` gets a mapping table and a streaming scrubber** in the
   `chat_completions` adapter, with a bounded one-token lookahead and a
   per-profile configurable tag pair, because open models do not agree on the
   delimiter.
8. **`cache_creation_input_tokens` becomes a fifth tracked token class** on
   both `ModelUsage` and `RunUsage`, and `reasoning_tokens` is `None` rather
   than zero where a provider does not report it separately. Pricing carries
   `reasoning_priced_separately` per model. `None` and `0` mean different
   things and the types say so.
9. **The context engine owns cache boundaries; the gateway translates hints.**
   Where hints exceed a provider's breakpoint budget the earliest survive, since
   an earlier breakpoint protects a larger prefix, and the drop is recorded on
   the attempt. Where a provider ignores hints entirely, that is recorded and is
   not an error.
10. **A `ModelRouter` port resolves `model_policy` into a `ResolvedModel`**
    carrying provider, model, capabilities, limits, pricing and a credential
    reference. This gives `ModelCapabilities` a resolution path, gives
    `context-engine.md`'s "the model's default" output reserve a carrier, and
    lets Milestone 10's availability routing arrive behind an existing port.
11. **Provider selection happens once, at run start, and the pin is absolute
    and persisted for the life of the run.** Milestone 10 routes selection; it
    never re-routes a live run. A provider outage fails a pinned run rather than
    silently switching, because continuation state is meaningless across
    providers and switching either discards it invisibly or attempts a
    translation that cannot be done.
12. **Retry ownership splits on `stream_had_output`.** Before the first event
    reaches the caller the adapter may retry, at most three times, with
    `Retry-After` honoured. After any output the adapter never retries; it fails
    and the caller decides, because only the caller knows whether partial output
    was shown, whether the step is repeatable, and whether budget and deadline
    permit another attempt. `max_attempts = 3` lives in application code,
    matching the worker's existing figure.
13. **`ModelRequest` gains `timeout_seconds` and `stream_idle_seconds`.** The
    idle timeout is the load-bearing one: a total timeout generous enough for a
    long reasoning turn is also generous enough to sit on a dead socket.
    Cancellation produces `StopReason.CANCELLED` on a partial turn, not an
    error, because cancellation is not failure and the caller's next question is
    about retry eligibility.
14. **Raw reasoning text is never persisted; the opaque continuation payload is
    persisted and never parsed.** This is ADR-0006 and ADR-0007 applied at the
    stream boundary rather than restated.
15. **The `PLATFORM` default on `ProviderReasoningItem.trust_level` is bounded
    by four properties**: the payload is never parsed, never rendered as prompt
    text, never reaches the policy engine, and never enters memory or a
    user-facing renderer as trusted content. Those four leave the label no
    consumer able to act on it. The name remains misleading and the fix is
    raised as an open question rather than taken unilaterally, because it means
    editing a plan sentence.
16. **The gateway enforces tool call and tool result pairing before sending a
    request.** Dangling calls and orphan results are protocol errors caught
    pre-send. The denial-as-tool-result mechanism in the policy spec and the
    compaction-atomicity rule in the context engine spec both depend on this
    invariant, and until now neither document owned it.
17. **Two schema tables are added, `model_calls` and `model_prices`.**
    `model_calls` is one row per attempt with all five token classes, cost, cost
    source and price reference. `model_prices` is append-only so a three-month-old
    invoice stays reconcilable after a vendor price change. `runs.usage` is
    unchanged in shape and becomes a rollup maintained in the same transaction.
18. **Failed attempts count against budget, and budget is checked before an
    attempt.** A run that crashes its way to its ceiling stops for a stated
    reason rather than being mysteriously expensive. There is no model-call
    idempotency key because neither first provider offers one that would let us
    reclaim the cost; that is an accepted cost, not an oversight.
19. **The contract suite runs against five adapters** — fake, recorded, OpenAI,
    Anthropic and `chat_completions` — and is written against the fake before
    any real adapter exists. Milestone 3's fixture list naming only OpenAI is an
    incomplete enumeration, and its acceptance criterion naming three providers
    is controlling.
20. **Import boundaries are tested by walking the import graph**, not by
    grepping for SDK names, because the failure that matters is a transitive
    import through a shared helper.

## Consequences

- The twenty-odd undefined types now exist, which is what makes Milestone 1's
  "no provider-specific code exists in the runtime" a testable statement rather
  than an aspiration.
- Writing the validator and the contract suite before the first real adapter
  adds work ahead of the first live model call and is the only ordering that
  prevents the suite from being shaped around whichever provider was written
  first.
- Cost accounting moves from a JSONB column to a per-attempt table, which adds a
  write on every model call and makes per-step cost, retry waste, and
  cache-effectiveness answerable by query instead of by log archaeology.
- The retry split means a transient failure mid-stream now reaches the runtime
  instead of being absorbed. That is more visible failure, deliberately: an
  absorbed retry after partial output is a duplicate-output bug waiting for the
  first streaming UI.
- Provider pinning being absolute means a vendor outage fails runs that a
  switching design would have completed. The alternative silently degrades
  reasoning quality mid-run, which is worse and much harder to notice.
- Refusing `server_tool_use` means a provider feature is unavailable until it
  has a policy story. That is the intended trade.
- The gateway acquires a pre-send validation pass over the whole message list,
  costing one traversal per request and converting a class of provider 400s into
  local errors that never leave the process.
- Three new spans and six new metrics land in Milestone 3, and Section 19's
  telemetry attribute list grows by five.
- Ten hard gates are added to Milestone 3, which is correspondingly larger than
  its current acceptance criteria imply.

## Alternatives considered

- **Treating Section 10.2's mapping table as sufficient and writing adapters
  directly against it**: rejected; the table maps fields but does not define the
  stream's shape, and two adapters written to the same table with different
  assumptions about ordering and terminal events would pass their own tests and
  disagree with each other.
- **Offering both a streaming and a non-streaming method on `ModelProvider`**:
  rejected; two code paths per adapter is exactly the divergence the contract
  suite exists to catch, and non-streaming is a trivial fold over streaming.
- **Letting adapters build turns**: rejected for the same reason; the assembler
  is the control in the experiment, and per-adapter assemblers would make turn
  differences uninterpretable.
- **Raising an error on malformed tool JSON**: rejected; it converts a
  recoverable modelling failure into a failed run, and the runtime's answer to a
  modelling failure is to tell the model.
- **Mapping `server_tool_use` onto `ToolCallItem`**: rejected; it would make
  provider-executed side effects indistinguishable from policy-approved ones.
- **Folding cache-write tokens into `input_tokens`**: rejected; they are priced
  differently on one provider and absent on the other, and a single summed field
  makes the two providers' numbers incomparable exactly where comparison matters.
- **Reporting `reasoning_tokens` as `0` on Anthropic**: rejected; zero asserts
  the model did no reasoning, which is false and would silently corrupt any
  analysis of reasoning cost.
- **Letting the gateway place cache breakpoints**: rejected; the context engine
  is the only component that knows what is stable, and a gateway that optimizes
  caching is a gateway that has started making context decisions.
- **Resolving `model_policy` inside each adapter**: rejected; capabilities,
  limits and pricing are needed before an adapter is chosen, most obviously by
  the context engine sizing its output reserve.
- **Allowing mid-run provider failover**: rejected; see decision 11. The
  degradation is invisible, which is the disqualifying property.
- **Putting all retries in the adapter**: rejected; the adapter cannot know
  whether partial output was streamed to a user, so it cannot decide whether a
  repeat is safe.
- **Putting all retries in application code**: rejected; it pushes connection
  resets and pre-stream 429s, which are invisible and safely repeatable, into
  runtime logic that would then have to re-derive their safety.
- **A single total timeout with no idle timeout**: rejected; the value large
  enough for extended thinking is also large enough to hang on a dead socket for
  ten minutes.
- **Persisting raw reasoning text for replay fidelity**: rejected by ADR-0006
  already; the opaque continuation payload gives replay what it needs without
  storing model reasoning.
- **Changing `ProviderReasoningItem.trust_level` to `EXTERNAL_UNTRUSTED` in this
  ADR**: not taken; it is the right change and it is an edit to a plan sentence,
  so it is raised for review while the four bounding properties make the current
  default harmless.
- **Keeping usage in `runs.usage` alone**: rejected; a JSONB rollup cannot
  answer which attempt burned the tokens, which is precisely the question a run
  that retried three times raises.
- **Failing streams on unrecognized provider event types**: rejected; providers
  add event types without warning and a strict adapter breaks on a vendor deploy
  we did not perform. Logging once per process per type keeps it visible without
  flooding.
