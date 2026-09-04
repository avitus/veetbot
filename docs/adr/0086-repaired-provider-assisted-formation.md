# ADR-0086: Repair provider-assisted formation as formation@10 and bind release evidence to the compiled policy

- Status: Proposed
- Date: 2026-09-03
- Related: Milestone 21 of the engineering plan; ADR-0057, ADR-0068, ADR-0077
- Detailed design: `docs/plan/adaptive-memory-distillation.md`,
  `docs/plan/memory-formation-and-consolidation.md`

## Context

A long strength-training conversation on 2026-09-02 formed one garbled memory
in production. The audit in the platform's own process events showed why:
every `formation@8` provider attempt for that session failed with a truncated
stream or a `permanent integer_below_min_value` refusal. The frozen policy's
fixed budget of USD 0.05, its one-byte-per-token preflight, and its compact
view of the fifty most recent beliefs shrink the affordable output to a few
hundred tokens once a store holds a dozen beliefs and skip the call entirely
past about twenty. The deterministic fallback then formed what its patterns
could parse. `formation@9` superseded that path for the production tuple on
2026-09-03, but `formation@8` remained the only provider-assisted policy for
every other tuple and the only rollback target for the operator pin, and both
were starved by construction.

The same day showed a second defect. The first `formation@9` artifact had been
evaluated one commit before `dev` was merged, and Milestone 24's device-channel
policy rules then changed the compiled policy version. The exact-tuple guard
correctly refused the stale artifact, so the deploy that was meant to activate
`formation@9` silently ran deterministic formation until the evidence was
regenerated on the deployed tree. Nothing in CI had observed the mismatch: the
bundle test deliberately allowed an interregnum between a policy change and the
republished artifact, and that allowance is exactly what turned a merge into a
production regression nobody was told about.

The owner authorized both repairs on 2026-09-03.

## Decision

1. **`formation@10` is the repaired provider-assisted policy**, implemented by
   `provider-assisted-v3` in the same extractor class, selected by the
   `formation_policy_version` it is constructed with. It keeps one
   schema-constrained call, the closed claim vocabulary, local rendering, and
   every service gate. It changes only what starved `formation@8`: the input
   ceiling is the model's context window less the output reserve; the output
   ceiling is 16,384 tokens, the same as distillation; the timeout is 120
   seconds; cost is recorded in the audit and bounded only by a USD 10 sanity
   ceiling two orders of magnitude above a real consolidation, never used to
   shrink or skip the call; and the compact belief view is the fifty beliefs
   sharing the most content with the batch, drawn from the five hundred most
   recent, restricted to public and internal sensitivity, with store order
   breaking ties.
2. **`formation@8` stays frozen and its artifact is withdrawn.** What it forms
   is byte-identical and still gated by `gate.memory.distill_versions_frozen`;
   its compact belief view now applies the same public-or-internal egress
   filter as every other provider policy, because a sensitivity leak is a
   safety floor rather than a formation semantic.
   Its bundled evidence was bound to a policy version the tree no longer
   compiles, could not activate anywhere, and would have been the first thing
   the new guard rejected. Regenerating it would certify a starved policy, so
   it is not regenerated.
3. **Automatic selection prefers `formation@9`, then `formation@10`, then
   `formation@8`**, the first artifact that matches the exact tuple. The
   operator pin gains `formation@10`; a pin narrows the search to exactly one
   policy, and a pin whose evidence is missing falls back deterministically
   with the existing `pinned_policy_unevidenced` audit. Startup validation in
   `required` mode judges the pinned policy's own artifact, not merely the
   artifact type. The selection audit records the formation policy it chose.
4. **`formation@10` evidence comes from a populated store.** The 25-case
   formation corpus declares a thirty-belief seed pool whose subjects never
   touch a case's expectation, at least eighty percent of positive cases name
   it, and the evidence records the seeded case count; a `formation@10`
   artifact with no seeded case is rejected at the schema. `agent eval memory-formation` gains
   `--formation-policy`, resolves its build reference from a clean committed
   checkout the way distillation does, and publishes only a passing artifact.
5. **Bundled evidence is bound to the policy the tree compiles.** The bundle
   test compares every artifact's `policy_version` against the shipped policy
   documents and fails with the file name, both versions, and the remedy. A
   pull request that changes the policy must therefore regenerate the
   evidence on that tree or withdraw the artifact and record the interregnum
   in project state; the silent production fallback of 2026-09-03 cannot recur.

## Consequences

- Provider-assisted formation works for any evidenced tuple and for a rollback
  from `formation@9`, on the stores production actually has.
- Every policy-touching change now pays for its evidence before it merges,
  which is the point. The cost is about one US dollar per policy per tuple.
- The frozen `formation@8` arm remains the honest control in the comparative
  evaluation; `formation@10` is deliberately not a fourth arm, so the
  `formation@9` comparative artifact and its gates are unchanged.
- The activation tuple still binds the whole compiled policy. Narrowing it to
  formation-relevant inputs would weaken a conservative guard for convenience
  and is not decided here.

## Alternatives considered

- **Regenerate `formation@8` evidence on the current tree.** Rejected: it
  would certify a policy the audit proved cannot complete a call on a real
  store.
- **Add `formation@10` as a fourth comparative arm.** Deferred: it changes the
  comparative artifact schema and gates for information the single-policy
  evidence already provides.
- **Bind the activation tuple to a subset of the policy.** Rejected for now:
  the guard in decision 5 makes the full binding cheap to honor, and a
  narrower binding would need its own design.
