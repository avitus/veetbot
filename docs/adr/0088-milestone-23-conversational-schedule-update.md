# ADR-0088: Milestone 23 conversational schedule update

- Status: Proposed (authorized by the repository owner, 2026-09-04)
- Date: 2026-09-04
- Related: Section 21 of the engineering plan; ADR-0021, ADR-0059,
  ADR-0072, ADR-0073, ADR-0080
- Detailed design: `docs/plan/scheduling.md`, Milestone 23 in
  `docs/plan/engineering-plan.md`

## Context

Milestone 23 exposed bounded schedule discovery and approval-gated pause,
resume, and cancellation to the conversational agent, but deliberately left
content and cadence updates on the HTTP surface. The owner has now reported
that this makes schedule management through chat incomplete and explicitly
authorized conversational modification on 2026-09-04.

The existing HTTP update accepts a complete `ScheduleDefinition`, including
agent, policy, scopes, limits, and failure settings that the summary-only model
tool must not read or control. A chat tool therefore cannot safely mirror the
HTTP body or reconstruct hidden fields in model arguments.

## Decisions

1. **Milestone 23 gains `schedule.update`.** The existing parallel workstream
   expands by five gates without changing the verified sequential ceiling.
2. **The tool accepts a closed content-and-cadence patch.** A stable
   `schedule_id` and positive `expected_revision` are mandatory. At least one
   of title, instruction, one-time `at`, or one recurring `cadence` is present;
   `at` and `cadence` are mutually exclusive.
3. **Hidden execution fields are preserved by the application service.** The
   service loads the guarded current revision and carries forward its agent ID
   and version, policy profile, requested scopes, run limits, timeout, misfire
   grace, and failure limit. The model cannot supply or widen them.
4. **The existing immutable-revision path remains authoritative.** A valid
   edit writes revision N+1 through `ScheduleService`, leaves revision N
   unchanged, affects only future occurrences, preserves PAUSED state, and
   recomputes an ACTIVE schedule's first strictly future firing.
5. **The edit is consequential and governed.** `schedule.update` is
   `EXTERNAL_WRITE`/`HIGH`/`CONDITIONALLY_IDEMPOTENT`, requires exactly
   `schedule.write`, is non-parallel, and waits for ordinary approval showing
   the stable identity, expected revision, and complete supplied patch.
6. **Recovery uses the existing schedule request ledger.** The tool passes its
   invocation idempotency key to the service. The key, a canonical patch hash,
   and resulting schedule ID are committed with the revision; an identical
   recovery returns the schedule without writing another revision, while key
   reuse with different content conflicts.
7. **The existing deployment pair gates the tool.** It registers and enters
   the default-agent roster only while both schedule flags are enabled.

## Consequences

- An owner can change a schedule's name, instruction, time, or calendar rule
  through chat after identifying and approving the exact target.
- Summary-only discovery remains summary-only; the tool does not need
  `schedule.read` to load hidden fields, because the application service
  performs its write-authorized guarded merge.
- A PAUSED schedule stays paused with no next firing after an edit. An ACTIVE
  schedule receives the first occurrence strictly after update time. COMPLETED
  and CANCELLED schedules remain terminal.
- The schedule request-idempotency table now guards conversational update as
  well as creation; it stores hashes and identifiers, never patch content.
- Occurrence and run history, hard deletion, delegated scopes, new cadence
  kinds, native mutation, and model control of execution authority or limits
  remain outside the milestone.

## Alternatives considered

- **Expose the complete HTTP update body to the model:** rejected because it
  would disclose and let the model alter authority, policy, and finite bounds.
- **Require `schedule.read` and merge in the tool:** rejected because update is
  already authorized by `schedule.write`, and a read followed by a write would
  add a needless authority requirement and a race outside the service
  transaction.
- **Classify update as non-idempotent:** rejected because the existing request
  ledger can bind the patch to the tool invocation and make crash recovery
  deterministic.
- **Mutate the current revision in place:** rejected because occurrence
  reproducibility depends on immutable revision history.
