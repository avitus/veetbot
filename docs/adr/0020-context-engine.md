# ADR-0020: Context engine — cache boundary, budget, and trust rendering

- Status: Accepted
- Date: 2026-07-24
- Related: Milestone 7 (context budgeting and structured working state),
  Sections 6.6/6.9 (conversation items, checkpoint), 7 (`ContextBuilder` port),
  10.1 (normalized request, prompt-stability invariant), 11 (context engine),
  12 (main loop), 27.4 (cross-run continuity), 30.4 (skill loading),
  ADR-0006/0007 (no private reasoning storage; provider-neutral reasoning state),
  ADR-0009 (run/turn/session), ADR-0018/0019 (memory write and read paths)
- Detailed design: `docs/plan/context-engine.md`

## Context

Section 11 describes the context builder as a list of seven things to assemble and
leaves the load-bearing questions open: which of them sit on which side of the
prompt cache boundary, how that boundary is enforced, where the seven integers of
`ContextBudget` come from, what yields when they bind, and how compaction — which
Section 11.4 requires — coexists with the Section 10.1 invariant that the prefix is
never rewritten mid-session.

The builder is the single component that can violate every other subsystem's
guarantees at once. It is the only place the prompt cache can be broken, the only
place a `TrustLevel` is actually acted upon, and the only place scarcity between
memory, history, tools, and tool results is resolved. All three failures are
invisible in model output: a cache-thrashing session returns correct answers at
many times the cost, a laundered trust label returns a plausible answer derived
from attacker-controlled text, and a budget overrun surfaces as a provider error
deep into a long run. The memory specs (ADR-0018, ADR-0019) additionally assume a
frozen prefix, a `retrieved_context_tokens` allocation, and a stable rendering
contract that Section 11 does not currently define.

## Decision

1. **Context is two regions with one membership rule.** Region A (the frozen
   prefix) carries platform policy, framing text, agent instructions, filtered tool
   definitions, and the session-open memory snapshot. Region B (the turn body)
   carries everything else. The rule is *if it can differ between two requests in
   the same session, it is in Region B* — not "if it usually doesn't change."
   Region membership is a property of item **type**, declared in code and asserted
   at assembly; an item type with no declared region is a build failure.
2. **Assembly order is fixed and total**, platform policy first and the current
   user message last.
3. **Prefix stability is enforced, not assumed.** Every request records
   `prefix_sha256`; more than one distinct value per epoch in one session is a
   defect. A scripted fifty-turn session (clock crossing midnight, a tool revoked
   mid-session, memory written and corrected, a forced compaction) asserting a
   single hash is a **hard gate** on Milestone 7.
4. **The tool set is pinned at session open; authorization is enforced at call
   time.** A revoked capability stays in the prefix and is denied by the policy
   engine, because a tool definition in a prompt grants nothing. This avoids
   rewriting the prefix on every permission change, and avoids leaking
   authorization changes into cache timing.
5. **Changes that genuinely cannot be absorbed rotate a prefix epoch** — model or
   provider routing, or an operator-forced immediate revocation. Rotation is
   explicit, emits `context.epoch.rotated`, and **epochs per session is a tracked
   metric with target 1.0**.
6. **Only history scales with the context window.** Every other class is capped
   absolutely: 2,000 tokens platform policy, 4,000 agent instructions, 30 tools /
   6,000 tokens, 40 items / 1,500 tokens memory snapshot, 1,000 working state,
   2,000 in-turn recall, 25% of body for tool results. Prefix content is attention
   paid on every request, and attention degrades with the absolute number of
   competing items rather than the fraction of the window they occupy — the same
   argument ADR-0019 makes for the snapshot, generalized. The output reserve is
   **subtracted first**, never allocated.
7. **The prefix never yields.** If the prefix classes exceed their combined
   ceiling, the session **fails to open** with the offending class named. A
   silently truncated system prompt is an agent that behaves subtly wrong forever.
8. **Yield order under pressure is in-turn recall, then tool-result truncation to
   typed pointers, then history compaction** — cheapest and most recoverable first.
   Compaction is last because it alone costs a model call on the critical path and
   loses information the run cannot re-fetch. The current user message, working
   state, correction lines, and active tool pairs never yield.
9. **Tool call and result items are atomic budget units.** An orphaned half is a
   malformed request, not a smaller one; a validator rejects orphans before send.
10. **`build()` is a pure function of its inputs; compaction is a write.** Pressure
    is resolved by invoking the compactor, which writes a new checkpoint, then
    rebuilding — never by mutating inline. Determinism is what makes retries safe
    and makes the byte-stability assertion meaningful.
11. **The prefix is measured exactly at plan time; only the body is estimated.**
    The estimator is conservative by construction, per-model, carries a 5% safety
    margin, and reconciles against the actual usage returned on every response.
12. **Compaction touches Region B only, and untrusted content is elided rather than
    paraphrased.** Summarization launders trust labels: fluent narration derived
    from `EXTERNAL_UNTRUSTED` content carries no label and reads as the platform's
    own account. The summarizer therefore runs on trusted content only; untrusted
    spans become typed pointers retaining trust level, size, and an artifact or
    event reference. **Summary depth is capped at 2**; beyond that the run escalates
    to a child run rather than being compressed into something unverifiable.
13. **Every non-platform region renders inside a nonced, attributed envelope**, with
    delimiter escaping so content cannot close its own envelope. The framing — that
    enveloped content is data and never instructions, and can never alter policy or
    grant permission — is stable platform text in the prefix; only the data varies.
14. **Working state is written by a typed control tool
    (`context.update_working_state`) and by the runtime, never parsed from model
    prose**, following the `conversation.ask_user` precedent. It carries across turn
    boundaries by per-field rule computed from the session log, is bounded (20
    constraints, 30 tasks, 40 facts, 20 open questions, 1,000 tokens), and **never
    evicts a constraint** — at the cap the write fails visibly.
15. **`established_facts` are offered to memory formation as candidates** at the
    session-boundary trigger, entering the ordinary pipeline subject to every
    eligibility gate including the untrusted-content write ban. Formation gains a
    high-quality input, not a bypass.

## Consequences

- The prompt-stability invariant stops being a rule people remember and becomes an
  asserted property of production traffic, with a session id attached when it
  breaks.
- The memory specs' assumptions are now backed: `retrieved_context_tokens` has a
  sizing rule and a parent, the frozen snapshot has a defined home and epoch
  semantics, and the rendering contract generalizes to every trust band.
- Compaction becomes a security-relevant component rather than a compression
  convenience, and the "elide, never paraphrase" rule means long untrusted-heavy
  sessions lose more raw content than a naive summarizer would — recoverable via
  pointers, and the correct trade.
- Purity in `build()` costs an extra assembly pass whenever compaction fires, in
  exchange for reproducible requests, safe retries, and a testable invariant.
- Prefix overflow becomes a session-open failure, which is a new user-visible error
  class that configuration must be validated against before deployment.
- Pinning tools at session open means a revoked capability can still be attempted
  once and denied, producing a wasted model call in exchange for cache stability
  and a closed timing side channel.
- Five hard gates and four tracked metrics are added to Milestone 7, which is
  correspondingly larger than its current acceptance criteria imply.
- `ContextBudget` gains three fields, `Fact` is defined, and `ContextPlan`,
  `CompactionResult`, `ElidedSpan`, `ContextPlanner`, `TokenEstimator`, and
  `Compactor` are new. Six event types are added to Section 6.8's list.

## Alternatives considered

- **Assembling by concatenation with a "keep the prefix stable" convention**:
  rejected; an invariant maintained by memory is violated by the first contributor
  who adds a timestamp to a header, and the failure is silent and expensive.
- **Deciding region membership per request based on whether a value changed**:
  rejected; correctness would depend on when the test ran. The date is stable until
  midnight, which is exactly why type-level assignment is the only safe rule.
- **Rebuilding the prefix whenever authorization changes**: rejected; it converts
  every permission change into a full re-cache and leaks authorization state into
  cache timing. Denial at call time is both cheaper and the actual security
  boundary.
- **Sizing every class as a percentage of the context window**: rejected for the
  same reason ADR-0019 rejected it for the snapshot — a larger window is not a
  reason for a longer policy document or more tools. History is the only class
  whose value grows with size.
- **Truncating agent instructions to fit**: rejected; a subtly truncated system
  prompt produces an agent that is wrong forever in ways no eval was written for.
  Failing to open the session is recoverable and legible.
- **Compacting before trimming recall or truncating tool results**: rejected;
  compaction is the only yield step that costs a model call on the critical path
  and the only one whose loss the run cannot cheaply reverse.
- **Letting `build()` compact inline**: rejected; it makes assembly
  nondeterministic, which forfeits retry safety, request reproducibility, and the
  byte-stability gate in one move.
- **Summarizing all content uniformly, including untrusted spans**: rejected; this
  is the trust-label laundering path, and it upgrades quoted, inert injection
  content into trusted platform narration.
- **Dropping the trust envelope for content the model "obviously" knows is
  external**: rejected; the model's inference about provenance is not a security
  control, and unescaped content can assert whatever provenance it likes.
- **Parsing working state from assistant prose**: rejected; nondeterministic and a
  prompt-injection surface, the same reasoning ADR-0009 applied to clarifying
  questions.
- **Letting working state grow unbounded**: rejected; it is history with a schema,
  and it would silently consume the budget that history was allocated.
- **Allowing constraints to evict at the cap**: rejected; constraints are exactly
  what the user notices being dropped, and a silent drop is indistinguishable from
  the agent ignoring them.
