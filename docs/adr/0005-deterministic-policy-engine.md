# ADR-0005: The deterministic policy engine

- Status: Proposed
- Date: 2026-07-25
- Related: Milestone 4 (policy, approvals, complete tool lifecycle),
  Sections 2.5 (a prompt instruction is not an authorization mechanism),
  5 (dependency rule 11), 8.1/8.3/8.4 (tool spec, validation order,
  idempotency), 9 (policy and approval model), 11.2 (trust labels),
  13 (error taxonomy), 22 (security baseline), 29 (device execution targets),
  30.4 (skill authoring is gated), ADR-0017 (layered approval),
  ADR-0008 (sandbox isolation), ADR-0015 (in-sandbox tool bridge)
- Detailed design: `docs/plan/policy-and-approvals.md`

## Context

Section 4's repository tree names this ADR and the engineering plan treats a
deterministic policy engine as a foundational decision, but the record was never
written. Section 9 specifies the engine at the level of its interface: a
`PolicyDecision` with four decision types, a default matrix of sixteen rows, an
`ApprovalRequest`, and an eight-step pause sequence.

What the plan does not supply is the vocabulary that interface is written in.
`ProposedAction` — the engine's primary input — appears exactly once, as a
parameter type in Section 7, and is defined nowhere. `ApprovalStatus` appears
once, as a field type in Section 9.3. `SideEffectClass`, `RiskLevel`, and
`IdempotencyClass` each appear once, as field types in Section 8.1. Five
undefined types is not an oversight in documentation; it means five design
decisions are unmade, and each of them determines what the engine is capable of
deciding.

Section 9.2's matrix has the same problem in a different form. It is keyed on a
prose column called "Action category" with no referent in any type, and three of
its cells hold decision strings — "Allow with restrictions", "Allow only in
sandbox", "Deny initially" — that are not `PolicyDecisionType` values. A matrix
that cannot be looked up by a program is documentation of an intent, not a
policy.

There is also a structural hazard the plan does not name. Section 8.3 defines
one validation pipeline, but three surfaces propose actions: the model's tool
calls, the in-sandbox RPC bridge (Section 8.5, ADR-0015), and the device channel
(Section 29). Each is described as passing "the full pipeline". If that turns
into three call sites, the engine has three gates, and the weakest one is the
system's actual policy.

Finally, `PolicyDecision.policy_version` is declared as a bare `str` with no
producer, format, or storage — while the context engine's `ContextPlan` already
consumes it, because the cacheable prompt prefix advertises which tools exist
and how they are gated.

## Decision

1. **The deterministic evaluator is a pure function.** `evaluate_deterministic`
   takes an action, a principal, a run, and a loaded ruleset, and returns a
   decision. It performs no I/O, reads no clock, and touches no database. The
   `PolicyEngine` port keeps its `async` signature from Section 7 so the
   advisory layer can compose behind it; the core inside is synchronous.
   Section 5's eleventh rule — the engine must not depend on prompts or model
   judgment — becomes testable rather than aspirational.

2. **Time is an input.** `ProposedAction.evaluated_at` is passed in. A rule that
   needs the clock receives it as data, so the same decision replays identically
   a year later against the same recorded inputs.

3. **`SideEffectClass` is the matrix key, with one value per Section 9.2 row.**
   Fifteen values for the fifteen action categories; the sixteenth row, "Unknown
   tool", is not a category. A test asserts the correspondence is total in both
   directions, so a new tool classification cannot be added without a rule and a
   rule cannot exist without a classification.

4. **Risk never selects a decision.** `RiskLevel` orders the approval queue,
   sets the default expiry, and groups metrics. Allowing it to select decisions
   would create a second matrix, and two matrices disagree in the allow
   direction.

5. **Section 9.2's three non-enum strings resolve without changing an outcome.**
   "Allow with restrictions" and "Allow only in sandbox" are `ALLOW` guarded by
   a predicate on the arguments or the execution target, denying when the
   predicate fails. "Deny initially" is `DENY` in the `default` profile;
   "initially" describes which ruleset is loaded, not a fifth decision type.

6. **An action that cannot be classified is denied.** The "Unknown tool" row
   generalizes to `policy.unclassifiable_action`, which is reachable through an
   unresolvable name arriving from the sandbox bridge or a device channel, a
   `side_effect` the loaded profile has no rule for, and a malformed action.
   Fail-closed on the unknown was always the row's intent; this gives it a
   reachable home.

7. **Restrictiveness is a total order, combined by `max`.** `ALLOW` <
   `ALLOW_WITH_MODIFICATIONS` < `REQUIRE_APPROVAL` < `DENY`. Because `max` over
   a total order is associative and commutative, the order in which layers run
   cannot change the outcome.

8. **Only the deterministic layer may modify arguments, and at most one rule may
   do so.** Combining two different modification sets is undefined, and a
   layer that could rewrite arguments would be an injection vector with policy
   authority. When a decision does modify, the idempotency key is recomputed
   from the modified arguments and both hashes are persisted, because Section
   8.4 derives the key before policy runs.

9. **Hardline rules are packaged files, frozen at import, and not behind a
   port.** Every other collaborator is a substitutable Protocol. Hardline
   evaluation is a module-level pure function on purpose: a substitutable
   never-bypassable rule is a contradiction, because the substitution point is
   the bypass. Each rule declares a `near_miss` it must permit, enforced by the
   schema and asserted by tests, which is what keeps the list from creeping into
   the deterministic layer.

10. **The advisory layer may only escalate.** Its output schema admits only
    abstain, require-approval, and deny; it never sees the rules it protects; it
    runs only when the deterministic decision was an allow; and it abstains on
    timeout. Abstaining on failure is safe precisely because it cannot lower a
    rank — an unavailable advisor leaves the system exactly as safe as the
    authoritative gate.

11. **There is exactly one function that transitions a tool invocation from
    `PROPOSED` to `AUTHORIZED`.** All three proposing surfaces reach it. An
    import-boundary test asserts nothing else performs that transition.

12. **`policy_version` is a content hash of the whole evaluated ruleset**, in
    the form `{profile}@{profile_sha256[:12]}+h{hardline_sha256[:8]}`. A hash
    rather than a counter because a counter requires someone to remember to
    increment it, and a stale counter asserts a falsehood that a hash cannot.

13. **Rules live in version-controlled files and are frozen per process.**
    Section 22 classifies policy rules as *Trusted*; a database table is
    editable at runtime by anyone with a connection string, which is not a trust
    boundary. `policy_profiles` records that a ruleset was loaded so an old
    `policy_version` can be resolved during an audit; it does not store rules.
    Because rulesets change only across a deploy, Section 9.3's step 8 —
    revalidate in case policy changed — describes a real and analyzable window
    rather than a race.

14. **Trust labels can inform a decision; only platform configuration and the
    principal's own scopes can authorize one.** Section 11.2's seven labels map
    onto Section 22's three tiers, with `MEMORY` and `KNOWLEDGE` — which had no
    tier — placed as partially trusted content that cannot authorize. A rule may
    raise a decision when an argument derives from `EXTERNAL_UNTRUSTED` content.
    This is Section 2.5 expressed as a table instead of a warning.

15. **Approval is not tool-only.** `ActionKind` covers tool calls, memory
    writes, skill authoring, and artifact export, so Section 30.4's requirement
    that skill authoring be approval-gated has somewhere to live.
    `approvals.tool_invocation_id` becomes nullable and a general `action_id`
    carries the reference, rather than fabricating tool invocations that no tool
    ever executed.

16. **Denial is a structured tool result with a field allowlist.** Both first
    adapters treat a tool-use block without a result as malformed, so a denial
    must occupy the slot. It carries a stable `reason_code`, a fixed message per
    code, and a remediation hint — never the rule, the pattern, or the profile.
    The model is a partially trusted consumer; telling it exactly what blocked
    it hands it a search gradient. Three identical denied proposals fail the run.

## Consequences

- Every decision is replayable from its recorded inputs, which makes policy
  regressions catchable in the evaluation harness rather than in production.
- The evaluation harness gains ten hard gates that are mechanical rather than
  judgemental, including a determinism property test and an import-boundary test
  for the single gate.
- Five undefined types become five defined ones, which unblocks Milestone 4 and
  resolves the context engine's dangling `policy_version` reference.
- Rule changes require a deploy. This is a real cost — an urgent policy change
  cannot be made from a console — and it is accepted because runtime-editable
  rules would make every recorded decision unexplainable.
- The hardline list is small and stays small by construction, so it is not a
  general policy mechanism and should not be reached for as one.
- Denial messages are deliberately unhelpful to the model. Some legitimate
  self-correction will be slower than it would be with a richer message.
- Nothing here weakens Section 9. The matrix's outcomes are unchanged; the three
  non-enum strings resolve to the same behaviour they describe.

## Alternatives considered

- **Leaving the five types undefined until implementation**: rejected; each one
  encodes a decision about the engine's power, and deciding them at a keyboard
  under milestone pressure is how a policy engine acquires an accidental design.
- **Keying the matrix on tool name**: rejected; it grows with the tool registry,
  a new tool defaults to unmatched, and every MCP server (Milestone 7) would
  need matrix entries nobody writes.
- **Letting `RiskLevel` select decisions**: rejected; two matrices, disagreeing
  in the direction that fails open.
- **Adding a fifth `PolicyDecisionType` for "allow with restrictions"**:
  rejected; it would change Section 9.1's enum, and the restriction was always a
  condition on the argument rather than a distinct kind of answer.
- **Storing rules in the database with an admin UI**: rejected; it moves policy
  out of version control and out of Section 22's trusted tier, and it makes
  `policy_version` unreconstructable after the fact.
- **A monotonic `policy_version` counter**: rejected; it depends on a human
  remembering, and a stale counter is worse than none because it asserts
  equality that does not hold.
- **Hardline rules behind a `HardlineEvaluator` port**: rejected; substitutable
  and never-bypassable are contradictory, and the port would be the bypass.
- **An LLM as the primary gate, with deterministic rules as a fallback**:
  rejected, as ADR-0017 already rejected it — nondeterministic, injectable, and
  unable to support an audit.
- **Letting the advisory layer return `allow` to reduce approval fatigue**:
  rejected; it would make a model judgment authoritative over a deterministic
  rule, which is exactly the inversion Section 5's eleventh rule forbids.
- **Blocking on the advisory layer when it times out**: rejected; it makes an
  optional component load-bearing for availability while adding no safety, since
  the authoritative decision was already an allow.
- **Fabricating a tool invocation for memory writes and skill authoring so the
  existing approval shape fits**: rejected; it puts rows in `tool_invocations`
  that no tool executed and corrupts every metric computed over that table.
- **Returning the full policy explanation to the model so it can self-correct**:
  rejected; the explanation names the rule, and a model that reformulates
  against a named rule is performing a guided search against the gate.
