# ADR-0111: Milestone 30 — restrictive-only advisory approval through the judgment port

- Status: Accepted — owner authorized Milestone 30 implementation on 2026-09-19 (decision 10 amended 2026-09-20)
- Date: 2026-09-19
- Related: ADR-0005, ADR-0017, ADR-0054, ADR-0076, ADR-0110
- Design: [Policy and approvals](../plan/policy-and-approvals.md), [Typed judgment](../plan/typed-judgment.md)

## Context

ADR-0017 decided, on 2026-07-20, that the deterministic policy engine stays the
authoritative gate and that an optional model-assisted approval may be added as
a secondary signal that can only make a decision more restrictive. The policy
specification designed it as its advisory layer: output constrained to
abstain, require approval, or deny; consulted only when the deterministic
decision allows; never shown the rules; abstaining on timeout or error. The
combination function and its monotonicity gate shipped with Milestone 4, the
profile carries `advisory.enabled: false`, and nothing else was built. The
engineering plan sequences it "after M6", names the model gateway as its
dependency, and lists it as roadmap item B8 — "LLM-assisted approval as a
restrictive-only signal" — whose entry condition is a policy ADR. This is that
ADR.

Two things changed since. ADR-0110 admitted a typed-judgment port whose first
provider answers a closed question in about a tenth of a second for a
negligible price, which removes the cost ADR-0017 named as the layer's
consequence — "adds latency/cost". And a reading of the executor showed where
the layer can add anything at all. Model-written arguments are untrusted unless
the owner's own message supplied them, and the trust overlay already escalates
every side effect except none, workspace read, and network read. What remains
allowed and outward-facing is the query of a web search and the URL of a page
fetch or a browser navigation. Private data can leave inside either, and no
deterministic rule can see it. The engine's own comment records the gap: origin
trust resets each turn, so an injection from an earlier turn is not caught.

The same reading found a claim in the specification that does not survive a
non-deterministic advisor. The ports section says the layer composes behind
the policy engine's interface "without changing any caller". The executor
evaluates policy before it looks up an existing invocation, and again at
revalidation. With a deterministic engine both repeats are harmless. With an
advisor whose answer can differ between calls, a resumed invocation could be
asked to move from running to waiting-for-approval, which the status table
forbids, or an escalation recorded before a crash could be lost and the action
run unapproved.

## Decisions

1. **The advisory layer enters as Milestone 30, a parallel workstream.** It is
   the second half of roadmap item B8. General standing approval grants, B8's
   first half, stay on the roadmap and are not admitted.
2. **The advisor is a port, and its first implementation uses the judgment
   port rather than the model gateway.** This diverges on the record from the
   plan's sequencing table, which names the model gateway as the dependency.
   The specified output was always a closed choice with no allow and no
   modifications, which is a typed judgment; abstain-on-timeout suits a call
   measured in tenths of a second; and the policy package may not reach the
   model gateway. `PolicyAdvisor.advise(action)` receives only the proposed
   action, so "never sees the rules" is structural: no ruleset, profile, or
   deterministic decision is in its signature. A model-gateway advisor remains
   possible behind the same port.
3. **It can only escalate, and version one never denies.** The composite
   combines by maximum rank through the existing function. The port's verdict
   type admits abstain, require approval, and deny, and cannot represent
   allow. The shipped advisor emits only abstain and require approval: a denial
   from an injectable component would be unappealable, reaches the model as a
   reason code, and would void an approval the owner had just given.
4. **It is consulted only where it can add something.** Only when the
   deterministic decision is a plain allow, and only for a network read aimed
   at the web or browser provider — a search query, a fetch URL, a navigation
   URL. Hardline blocks, denials, approval requirements, and allow with
   modifications are never consulted on: escalating a modified action would
   ask the owner to approve arguments the deterministic layer had not yet
   narrowed. Reads with no outward side, MCP reads that carry mail queries,
   file writes, code execution, and SMS are not consulted on.
5. **It asks narrow questions and code decides.** Three positively phrased
   probabilities — the text addresses an AI system; the query or URL carries
   private details of an identifiable person; the URL carries prose or an
   opaque blob. Any one at or above its threshold escalates; no question can
   veto another, and there is no "is this safe" question. A steered answer
   downward yields exactly the advisor-off state, and upward costs one prompt.
   Thresholds live in code under a hashed advisor version, stricter when the
   turn's origin cannot authorize.
6. **ADR-0017's input hardening is restated for a structured state, not
   relaxed.** Instructions and criteria are platform-authored and travel only
   in question fields. Every untrusted string is XML-entity-escaped and
   wrapped in an untrusted-input element inside a named state field, so
   content cannot close its own delimiter, and comments — HTML comments, a
   fetch URL's fragment — are stripped. The structure is additional to the
   delimiting, not a substitute for it.
7. **The vendor receives the tool name and redacted outbound arguments, and
   nothing else.** Arguments pass through the approval view's existing
   redaction, which masks sensitive keys and credential-shaped values and
   truncates long strings, and the state is capped at 4 KiB. A truncated value
   escalates, because its tail cannot be judged. No identifier, scope, hash,
   trust label, hardline rule, profile content, or deterministic decision is
   sent. These queries and URLs already leave for a web provider; the judgment
   vendor is one further recipient, under the risk acceptance ADR-0110 records.
8. **An unavailable advisor abstains, and the layer is never load-bearing.**
   One call under a fixed sub-second timeout, no retries, and every error
   class abstains; an abstention returns the deterministic decision
   byte-identical. With the layer enabled and no provider composed,
   composition warns and uses the deterministic engine. Nothing here may fail
   closed on the advisor's availability, the specification's own rule.
9. **The executor gains a recovery policy, and the specification's "no caller
   changes" claim is corrected.** The tool pipeline takes the deterministic
   engine as its recovery policy and uses it at revalidation — the owner has
   approved those exact bytes, and the advisor's only power is to summon the
   owner — and whenever an invocation row already exists for the idempotency
   key, taking the maximum rank with the decision persisted on that row under
   the same policy version. The advisor is therefore consulted at most once
   per invocation, a verdict cannot flip a running invocation, and an
   escalation recorded before a crash survives it. Standing authorization
   keeps the deterministic engine, so a standing grant can never satisfy an
   advisory escalation.
10. **Observe before enforce.** The profile keeps `advisory.enabled` and gains
    `advisory.mode`, `observe` or `enforce`, default `observe`. In observe mode
    the advisor runs and its verdict is recorded, and the deterministic
    decision is returned unchanged, so thresholds are tuned before the owner
    sees a prompt. Both values hash into the policy version, as every profile
    value does, so the mode is changed when no approval is pending.
11. **Escalations are coarse on the wire and measured from day one.** An
    escalation is a require-approval decision with the one reason code
    `policy.advisory.escalated`, a content-free explanation naming signal
    identifiers and the advisor version, no modified arguments, and the
    deterministic policy version. The specification's two metrics are
    recorded from the first enabled day, and "disagreement rate", which it
    never defined, is the share of owner-resolved advisory approvals that were
    approved.

## Amendment, 2026-09-20: observe by environment, enforce by profile

Decision 10 put both the switch and the mode in the policy profile, where every
value hashes into the policy version. Implementation found what that costs. The
bundled memory-formation, People, and email-People release evidence is bound to
the compiled policy version, and activation compares it with the version the
running composition compiles, operator overlays included. A change to the
shipped profile fails the release-evidence guard, and enabling the layer through
any profile value — even to observe, which changes no decision — would drop
provider-assisted formation to its deterministic fallback until the evidence is
regenerated. That happened once already, on 2026-09-03.

The owner decided on 2026-09-20:

- **Observing is an environment flag.** `AGENT_POLICY_ADVISORY_OBSERVE_ENABLED`
  composes the advisor in observe-only mode. It changes no decision, so it is
  not a policy value, does not move the policy version, and leaves the release
  evidence bound. This is within the composition rule that an environment
  variable may not change an effective rule: observing changes none.
- **Enforcing is the existing profile value.** `advisory.enabled: true` means
  enforce. It changes decisions, so it stays in the hashed document and is
  visible in the audit trail. Setting it moves the policy version: pending
  approvals whose re-evaluation is not an allow are voided, and the release
  evidence must be regenerated on the new version before provider-assisted
  formation activates again. That is the owner's act, taken deliberately.
- **No `advisory.mode` key exists.** The shipped profile is byte-identical and
  the knob census is unchanged. With both set, enforcing wins.

## Scope admission and consequences

The owner authorized Milestone 30 as an independent parallel workstream on
2026-09-19. Its phase 0 updates the engineering plan, project state, the
current milestone, the milestone map, readiness, AGENTS scope, the policy
specification, and the executable gate registry, which admits five further
`gate.policy.*` gates at Milestone 30. Registration is not passing evidence,
and the Milestone 4 gates are unchanged.

The design admits one port with its contract suite, one composite engine, one
judgment-backed advisor, one profile knob, one reason code, one metrics
module, the relocation of the argument-redaction helpers into the domain so
the policy package may use them, and one change to the tool pipeline. It
admits no dependency, no migration, and no new route.

The change to the tool pipeline touches the platform's single authorization
gate. It is bounded by the existing gates — single gate, revalidation,
monotonicity — which must stay green unchanged, and by new tests that a
resumed or revalidated invocation makes no advisor call.

An uncalibrated advisor would produce spurious approval prompts on ordinary
web searches. Observe mode is the mitigation, and enforcement is the owner's
decision, taken on the recorded escalation and disagreement rates.

## Alternatives considered

- **An LLM advisor through the model gateway, as sequenced.** Seconds of
  latency and a generative call's cost on every consulted action, a prose
  response to parse, and an import the policy package is forbidden. The port
  leaves it possible; nothing asks for it.
- **Let the composite call the judgment port directly.** The composite's
  invariants do not depend on the provider, the monotonicity gate needs a
  scripted advisor that can return deny while the shipped one never will, and
  the plan's named advisor is a different provider kind.
- **Consult on every allowed action.** Most allowed actions have no outward
  side, MCP reads would send mail queries to a new recipient, and the common
  path would pay for nothing.
- **Allow deny in version one.** Unappealable, visible to the model, and able
  to void a fresh approval at revalidation.
- **Keep thresholds in the profile.** Every tuning deploy would change the
  policy version and void unrelated pending approvals, and the vendor's model
  can drift regardless, so hashing thresholds would not make advisory
  decisions replayable. The advisor version is recorded instead.
- **Consult the advisor again at revalidation.** Its only power is to summon
  the owner, who has just approved those exact bytes.

## Acceptance status

The owner approved the implementation plan in chat on 2026-09-19. Implementation
may proceed through the design's build sequence under the repository's
red-green rule. Enabling the layer, and moving it from observe to enforce, are
the owner's acts. Pull request creation, merge, and production activation
retain their explicit boundaries.
