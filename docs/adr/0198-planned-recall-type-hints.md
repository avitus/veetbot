# ADR-0198: Model-planned recall uses type hints, not guessed hard filters

- Status: Accepted implementation mechanism under ADR-0178
- Date: 2026-10-12
- Detailed design: `docs/plan/general-memory-recall.md`

## Evidence

The completed six-arm PostgreSQL development comparison records four remaining
reader-planned failures. The planner asked for `fact`, `preference` or
`procedure_pointer` records while normal formation had stored the required claims
under other kinds. These hard predicates excluded the correct originals before
either retrieval channel. The failures span a vehicle, a dietary constraint,
a recurring review and an entrance instruction; this is not an ownership rule.

## Decision

Production advertises `memory.search@2.0.0` with optional `type_hints`. These are
soft structured relevance anchors and never populate the query's hard
`belief_types` predicate. The model-facing schema does not advertise the old
`belief_types` argument; unsupported old arguments fail validation rather than
silently widening a requested filter. Query preparation and execution use the
same version and mapping. The major version identifies the changed input
contract. Its concise description stays within the existing tool-definition
budget; the limit is unchanged. Scope, explicit subjects and temporal arguments retain
their existing semantics in this repair.

The explicit `RecallQuery.belief_types` predicate remains a hard filter in both
stores, including dense retrieval. The legacy tool mode remains available to
frozen/direct diagnostic callers with its unchanged `memory.search@1.1.0`
contract. No provider, index, ranker, owner-specific dictionary or production
semantic channel is added or activated.

## Verification

Runtime schema coverage first reproduces the unsafe planning surface. Cross-kind
regressions must retrieve a lexically relevant original despite a wrong hint,
while an explicit legacy filter still excludes it. Unknown hint values and the
old hard-filter argument are rejected on the model-facing surface. Existing
isolation, preparation, budgets and PostgreSQL contracts remain mandatory.
The development comparison remains frozen; it cannot certify this later repair.
