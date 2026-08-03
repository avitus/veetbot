# ADR-0037: Milestone 1 in-memory seam decisions

- Status: Proposed
- Date: 2026-07-31
- Related: Milestone 1, ADR-0021, ADR-0022, ADR-0023, ADR-0024
- Detailed design: `docs/plan/bootstrap-and-composition.md`

## Context

Milestone 1 deliberately places the complete model/tool loop over in-memory
adapters one milestone before durable checkpoints, model-call rows, database
transactions, and the policy engine exist. The design fixes the boundaries and
most semantics, but an implementation still has to decide what the first tier
does where a later adapter or service owns the final guarantee.

The project owner asked that implementation continue autonomously and that each
such decision be recorded for review. This ADR groups the reversible seam
decisions; it does not weaken a later milestone's requirement.

## Proposed decision

1. `SessionRepository` exposes `create` and tenant-scoped `get`;
   `ToolInvocationRepository` exposes `create`, idempotency lookup, guarded
   transition, and tenant-scoped `list_for_run`. These are the smallest
   Milestone 1 signatures that exercise the stated behavior and can be retained
   by the PostgreSQL adapters.
2. The runtime keeps one materialized checkpoint for the duration of an inline
   execution and emits `run.checkpointed` events. It does not invent a sixth
   in-memory repository when the detailed design explicitly lists five.
   Durable checkpoint storage and restart enter in Milestone 2.
3. Until the Milestone 4 policy engine exists, the pipeline authorizes only
   tools whose `SideEffectClass` is `NONE`. Every other call receives the fixed
   `policy.milestone1.non_pure` denial. This preserves the authorization stage
   without pretending that the future policy matrix exists.
4. Run transitions and their events are sequential in Milestone 1, not
   cross-repository atomic. The in-memory capability declaration continues to
   state that cross-repository transactions and crash recovery are absent.
   Milestone 2 must replace the pair with its specified database transaction.
5. Step/model-attempt identity is persisted in the in-memory event repository
   and run counters. A separate model-call repository is not introduced before
   the Milestone 2 schema that owns those rows.
6. Authored model fixtures accept the concise `kind: tool_call | final | error`
   YAML shape printed by the evaluation-harness design and translate it into the
   canonical in-code `FakeModelScript`/`ScriptedTurn` types during collection.
   Canonical serialized fixtures remain accepted directly.
7. Evaluation code is imported only when `agent eval` is invoked. The runner
   asks the composition root for fixed-clock and sequential-id adapters and then
   uses the ordinary `RunService`; normal CLI startup never imports
   `agent_core.evals`, and the harness contains no second runtime loop.
8. `agent run get`, `agent run events`, and `agent session create` use the same
   application services but inherit the tier's explicit process-lifetime
   boundary. Identifiers printed by one CLI process cannot be read by another
   until Milestone 2 supplies shared persistence.

## Consequences

- The whole Milestone 1 flow is provider-neutral, deterministic under fixtures,
  and executable without a database or network.
- No unsafe side effect can be authorized by a placeholder policy decision.
- Checkpoint, model-call, and event evidence is sufficient for this milestone's
  gates but is not restartable.
- Cross-process CLI reads are present as contracted commands yet have no useful
  prior state in the in-memory tier. This limitation is visible rather than
  hidden behind an undeclared persistence mechanism.
- Items 2, 4, 5, and 8 are deliberately superseded by Milestone 2 rather than
  carried into production persistence unchanged.

## Alternatives considered

- **Add SQLite or file persistence for CLI continuity:** rejected because it
  creates an undeclared third persistence tier and evades the PostgreSQL
  milestone.
- **Add in-memory checkpoint and model-call repositories:** rejected because
  the detailed design names exactly five in-memory repositories and assigns the
  durable records to Milestone 2.
- **Allow every builtin before policy exists:** rejected because the pipeline's
  authorization stage would become decorative precisely where side effects
  matter.
- **Make evals a second entry point with its own loop:** rejected because the
  plan requires evaluations and the CLI to exercise ordinary application
  services.
