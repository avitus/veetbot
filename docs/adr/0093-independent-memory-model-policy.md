# ADR-0093: Keep an independently selected, evidenced memory model

- Status: Accepted
- Date: 2026-09-11
- Related: ADR-0024, ADR-0057, ADR-0077, ADR-0086
- User authorization: use GPT-6 Astra and Claude Fable 5.1 for chat while
  temporarily retaining GPT-5.6 Sol for memory formation

## Context

Memory extraction inherited the interactive agent's model policy. Changing
the chat default therefore changed the extraction model and invalidated its
activation evidence. The Astra evaluations failed existing gates; inspection
found plausible wording mismatches as well as omissions, duplicate memories,
and classification differences. These results do not establish an intrinsic
memory advantage for Sol. They do leave Sol as the evidenced deployment choice.

## Decision

1. The production chat default becomes `astra`; `flagship` and `fable` select
   Claude Fable 5.1. Retain `balanced` as an explicit GPT-5.6 Sol policy so the
   existing memory evidence retains its exact model-policy identity.
2. `formation.model_policy` in `memory/profiles.yaml` independently selects
   the maintenance model through the existing static router. It defaults to
   `balanced` and is operator-overlayable. It is a policy-name reference
   included in the configuration inventory alongside the other maintenance
   controls. Invalid names fail profile validation; unresolved or unevidenced
   selections keep the existing `auto` fallback and `required` refusal.
3. Non-routed fake compositions retain their deterministic model policy.
   Explicit memory evaluation modes use the requested evaluation model policy,
   allowing Astra to be evaluated without changing the deployed memory choice.
   `off` still performs no extraction-model resolution or provider call.
4. Activation, extraction requests, cost accounting, and selection audits use
   the actual memory model policy. Audits also retain the chat policy. No
   evidence artifact, formation prompt, scorer, threshold, or trust gate changes.
5. The existing content-addressed agent versioning preserves the immutable
   previous agent when the default model policy changes. New sessions use Astra;
   existing sessions retain their agent version and provider pins. Deployment continues
   to require draining runs whose registry versions are being replaced.
6. Memory formation remains active Milestone 21 work. Revisit scorer semantics
   and compare Sol/Astra under controlled reasoning settings before attempting
   a memory-model change; activate a different tuple only after valid evidence.

## Consequences

Chat upgrades no longer silently disable evaluated memory. The separate model
selection remains visible in configuration and audit events. This is a fixed
maintenance-model selection within the existing gateway, not authorization for
dynamic routing, learned policies, or any other deferred roadmap item.
