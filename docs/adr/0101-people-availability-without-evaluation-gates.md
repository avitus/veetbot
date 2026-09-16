# ADR-0101: People availability without evaluation gates

- Status: Accepted — owner explicitly requested ungated People functionality
- Date: 2026-09-16
- Supersedes: ADR-0100 evaluation-dependent activation and default-off rollout
- Related: ADR-0077, ADR-0092, ADR-0093, ADR-0096

## Context

The sole owner wants to use the complete People experience on the existing
application. They explicitly rejected a limited manual-only release and requested
that no People functionality be gated. The previous design required reviewed
corpora, comparative provider runs and private acceptance before enabling the
new source policies. Those requirements prevented useful operation despite the
implemented correction, erasure and source-governance boundaries.

## Decision

People is enabled by default. With the configured memory provider available,
automatic Chat formation selects `formation@11` without a release evidence
artifact. Email uses `email-semantic@2`, including attributed person facts,
without a separate semantic evaluation artifact. Profiles, relationships,
history, commitments, recall, correction, forgetting and explicitly scoped
imports are available together. No pilot allowlist or separate evaluation
installation is introduced.

Keep `AGENT_PEOPLE_ENABLED=0` as an explicit operational shutdown switch, and
retain explicit legacy formation pins for rollback and comparative evaluation.
These are operator choices, not prerequisites for normal feature access.
Missing providers or credentials are reported as operational unavailability;
`required` provider mode still refuses unavailable providers. Runtime audits
record default selection honestly and never invent passing evaluation evidence.

Benchmarks, strict evidence validation, corpus review and owner feedback remain
quality measurement tools and milestone evidence. They do not decide runtime
availability or block release of the implemented People functionality. Legacy
source policies retain their own evaluated contracts when explicitly selected.

Existing authorization, source admission, sensitivity, egress, identity ambiguity,
correction and erasure controls remain enforced. Imports still require chosen
sources, dates and finite record/cost limits. Automatic Email retains its 90-day
window. New providers, rich SMS and call-derived semantics remain outside scope.
No permission, budget or production credential is granted by default enablement.

## Delivery and verification

The owner also authorized final implementation changes, delivery to `dev`, and a
`dev` to `main` pull request. Exact-head CI and CodeRabbit review still apply;
this decision does not authorize merging or claim production delivery.

Regression coverage must show default API/tool availability, automatic Chat and
Email formation without artifacts, usable scoped imports, explicit shutdown and
legacy-pin behavior, and unchanged authorization/source/erasure/budget boundaries.
No quality gate is marked passed merely because its runtime prerequisite is removed.
