# ADR-0057: Evaluation-gated provider-assisted memory extraction

- Status: Accepted
- Date: 2026-08-18
- Related: Milestone 10, ADR-0002, ADR-0018, ADR-0023, ADR-0045,
  ADR-0051
- User authorization: replace unconditional provider-memory rollout with an
  evidence-gated implementation that does not burden ordinary users

> **Updated by ADR-0068:** the semantic deterministic expansion and retryable
> provider-failure lifecycle are deterministic `formation@5` and
> provider-assisted `formation@6`. The `formation@4` evidence and version in this
> record are historical and do not activate the repaired tuple.
>
> **Updated by ADR-0069:** admitting working-state established facts as
> `AFFIRMED` candidates moves the active policies again, to deterministic
> `formation@7` and provider-assisted `formation@8`. For an exact runtime tuple
> with no matching reviewed `formation@8` evidence, `auto` falls back to the
> deterministic extractor and `required` refuses startup; a tuple with matching
> published evidence activates provider assistance.

## Context

ADR-0051 completed the ordinary-conversation lifecycle and introduced a bounded
hybrid extractor, but its routed-policy activation relied on focused static tests
rather than a version-bound evaluation artifact checked at startup. Activating
an ordinary provider call inside the interactive run would evade the idle
boundary, and accepting a provider's structured output as a write would evade
the formation service's provenance and safety gates. Requiring evidence only in
documentation would still leave startup able to enable a model-policy combination
that had never passed the required no-fabrication and no-policy-regression tests.

The first live run against the intended production policy exposed three defects
that focused fake-provider tests could not: the free-form candidate schema did not
reliably express normalized corpus concepts, provider paraphrasing could remove a
source-text safety marker before statement-only eligibility checks, and a failed
evaluation discarded the per-arm data needed to diagnose either problem. Aggregate
lift alone also permitted unacceptably sparse positive coverage.

The general-purpose subagent path remains outside the authorized tranche. The
memory design separately permits a dedicated maintenance job, so provider
assistance does not require introducing `delegate.run` or changing routing
behavior.

## Decision

1. **Use a dedicated maintenance extractor.** `ProviderAssistedCandidateExtractor`
   implements the existing `MemoryCandidateExtractor` port and is called only by
   session-close or idle consolidation. It never runs in the interactive terminal
   hook, has no tools, and does not introduce a general-purpose child-run surface.
2. **Make safe selection automatic.** Provider assistance has three rollout
   modes: `auto` (the default), `off`, and `required`. `auto` searches an explicit
   operator artifact and then release-bundled evidence, activates only an exact
   match, and otherwise continues with deterministic formation while recording
   why. `off` never resolves or calls a formation provider. `required` preserves
   fail-closed deployment semantics and refuses startup without a matching
   artifact. A legacy explicit enable flag maps to `required` so an operator's
   demand is never silently weakened. Every artifact must cover at least twenty
   labeled samples with at least twenty positive cases, fully support at least
   eighty percent of positive cases, and
   demonstrate strictly more supported candidates than the deterministic baseline,
   zero fabricated candidates in either arm, and no increase in policy failures.
   The artifact schema derives the coverage minimum from the positive-case count;
   an artifact cannot select a weaker floor. It must match the exact extractor
   version, formation policy, model
   policy, provider, model, policy profile, and compiled policy version resolved
   by the composition.
3. **Give the original maintenance call its own fixed budget.** This decision
   established the budget under historical `formation@4`: one structured-output
   model call, at most 16,000 input tokens, 4,096 output tokens, USD 0.05, and
   30 seconds. ADR-0068 and ADR-0069 later advanced the active
   provider-assisted policy to `formation@8` without changing that budget.
   Catalog pricing and a conservative input estimate
   cap requested output tokens before the call so the cost limit is preventive,
   not only retrospective. The request advertises no tools and requires the
   schema of a bounded semantic-claim batch. The provider selects a closed claim
   kind, source-grounded values, and an exact evidence quote. Deterministic local
   code owns the authorized scope and renders the final canonical
   `MemoryCandidate`; the provider cannot author either. Crossing a recorded
   ceiling rejects the provider result and uses the deterministic fallback.
4. **Treat episodes as data.** The provider sees only selected
   `user.message.created` events authored by the owning principal, labeled with
   their exact event sequences, plus a compact view of at most fifty existing
   beliefs. The prompt and schema state that episode text is data rather than
   instructions and prohibit assistant or tool content as evidence.
5. **Keep the deterministic service authoritative.** A valid provider batch is
   merged with deterministic proposals and still passes the service-owned source,
   scope, portability, salience, rejection, and conflict gates. Automatic
   formation rechecks the complete authoritative cited source text for secret,
   injection, and transient markers so normalization cannot erase the hazard.
   The twelve-candidate service ceiling remains unchanged. A failed, timed-out,
   malformed, or over-budget provider call is non-fatal and falls back to the
   deterministic extractor. Cancellation is audited and propagated rather than
   converted into a successful fallback.
6. **Persist a content-free model-call audit.** Every attempted provider extraction
   appends one idempotent process event with the principal and session identity,
   agent and policy versions, authorized scope, empty tool scopes, provider and
   model, evidence identifiers, fixed budget, deadline, exact selected source
   event sequences, usage and cost, outcome, and error class. It stores SHA-256
   hashes rather than raw prompts or responses.
7. **Separate evaluation from activation.** The extractor has an explicit
   non-activating evaluation constructor that requires no prior evidence and marks
   every audit as evaluation mode. The composition exposes that constructor only
   through an explicit code-level evaluation flag that is mutually exclusive with
   activation and refused in production. Normal deployment composition constructs
   the provider extractor only after the evidence check passes.
8. **Record the original provider-assistance version as `formation@4`.** At this
   ADR's acceptance, the deterministic default was `formation@2`, and activating
   provider assistance recorded `formation@4` on consolidation audits and
   beliefs so the two policies remained distinguishable during replay,
   comparison, and later re-derivation. ADR-0068 and ADR-0069 subsequently
   advanced the active versions to deterministic `formation@7` and
   provider-assisted `formation@8`; the older values remain historical audit
   identifiers only.
9. **Generate evidence instead of asking users to author it.** `agent eval
   memory-formation` runs the checked-in labeled corpus through isolated paired
   deterministic and provider-assisted arms. It computes supported, fabricated,
   and rejected-policy counts and atomically writes an artifact only when the
   activation schema passes. At this ADR's acceptance, the checked-in corpus
   contained twenty positive and four protected no-memory cases, scored against
   explicit normalized labels
   without another model judge. Both arms use identical scoring and fabrication
   accounting. Every result retains normalized beliefs, consolidation counts,
   content-free provider audit counts, and shared-versus-provider-added belief
   attribution per case. A failure returns those diagnostics, exits non-zero, and
   writes no activation artifact. Real provider execution retains the capability
   track's explicit live opt-in. The corpus hash and active model tuple are derived
   by the command rather than entered by hand.
10. **Use the installed release as the bundle trust boundary.** Evidence under
    the package's release-evidence directory is reviewed and shipped with the
    extractor code. The repository has no release-signing trust root; a detached
    evidence signature would therefore add a private-key workflow without
    establishing who is trusted. Release-bundled evidence inherits the same
    authenticity mechanism as the installed code, while an external evidence
    path is explicitly operator-trusted. A future signed release automatically
    covers its bundled evidence without a second signature format.

## Consequences

- Open-ended phrasing can select a relationship claim that local code renders as
  “User has at least one daughter” while the existing service remains the only
  writer.
- Provider outages cannot suppress deterministic automatic formation and cannot
  change the completed interactive run.
- A deployment cannot activate a different model, policy profile, or compiled
  policy version using evidence gathered for another combination.
- Ordinary users do not prepare evidence or opt into a provider manually. An
  officially evaluated release activates a matching tuple automatically; an
  unevaluated tuple remains deterministic and reports that decision.
- The evidence command and strict schema do not claim that any live provider has
  already passed. Until a real artifact is reviewed and bundled, `auto` selects
  deterministic formation.
- Model call details are durable without storing episode text, model output, or
  private reasoning in process events.
- Failed live runs are inspectable without turning their output into activation
  evidence, and a weak aggregate lift cannot conceal broad positive-case misses.

## Alternatives considered

- **Call the provider inline after every turn:** rejected because it changes
  interactive latency and cost and violates the cheap-flag/idle split.
- **Create a general-purpose subagent:** rejected because that Milestone 10 scope
  remains unauthorized and is unnecessary for a restricted maintenance call.
- **Trust schema-constrained output directly:** rejected because schema validity
  says nothing about source authority, scope, fabrication, or secrets.
- **Enable with a boolean alone:** rejected because it turns the required
  evaluation into an unenforced operating convention.
- **Require every user to provide an artifact:** rejected because evidence is a
  release-engineering concern for supported models, not an end-user preference.
- **Add a standalone evidence signature immediately:** rejected because no
  trusted release key or verification root exists; signing one file would not
  authenticate the code selecting it.
- **Fail consolidation when the provider fails:** rejected because provider
  assistance must not regress the verified deterministic path.
