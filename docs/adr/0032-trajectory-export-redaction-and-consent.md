# ADR-0032: Trajectory export, redaction, and consent

- Status: Accepted
- Date: 2026-07-28
- Related: Milestone 3, Sections 20 (evaluation), 22 (security
  controls), 31 (trajectory capture and export), ADR-0003 (event log
  and projections), ADR-0006 (no raw reasoning storage), ADR-0016
  (trajectory capture and export), ADR-0022 (the gate registry),
  ADR-0028 (the HTTP API surface), ADR-0029 (the artifact store)
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Context

ADR-0016 decided that the event log should produce portable
trajectories, that they should exclude secrets, raw reasoning, and
policy-restricted PII, and that exports should be tenant-scoped and
consent-gated. Section 31.3 turned that into four acceptance criteria.

The readiness review found Milestone 3 blocked on it. Three things were
missing and each was load-bearing.

**No format.** "ShareGPT / messages" names two shapes without choosing
one, and says nothing about which fields survive.

**No redaction procedure.** "Exclude secrets" is a goal, not a
mechanism. The corpus already contains two pattern engines — the
committed-secret scanner and the log-redaction processor — and neither
had been pointed at conversation content.

**No consent mechanism at all.** Four places in the corpus assert that
export is consent-gated. Nothing anywhere defines what a consent record
is, who holds one, when it is evaluated, or what a withdrawal does. It
was the only requirement in the plan with no design surface whatsoever.

Meanwhile the consuming half was already built. The evaluation harness
specifies conversion from an export to a case, states that "redaction
happens at export, not at conversion", and asserts that its converter
has no access to the raw log. That is a promise this side had not yet
made.

## Decision

1. **An export is an artifact, not a table.** The projection maintains
   the cheap incremental state; the document is materialized once, on
   demand, into the artifact store under a new
   `ArtifactOrigin.TRAJECTORY_EXPORT`. The store already supplies
   content addressing, a platform-derived key, an authorized read path,
   and `expires_at` with a sweeper behind it, all four of which a
   governed export needs and a new table would have to grow.
2. **The format is one versioned JSON document in the `messages`
   shape**, carrying outcome, a failure classification without its
   error text, and the name and schema hash of every tool the run
   touched. ShareGPT is a rename of the role vocabulary and therefore a
   consumer's transformation.
3. **The export carries no per-message timestamps**, and a date rather
   than a timestamp at the top. Timing is the most correlatable field an
   export could carry and no stated consumer keeps it.
4. **Redaction is three stages and fails closed.** Structural exclusion
   by the builder, pattern replacement reusing the scanner's five rule
   families and the log processor's key-name families, then a
   verification scan over the finished document. A verification hit
   raises, writes no artifact, and names the rule without printing the
   match. It does not redact a second time, because a second pass hides
   the gap in the first and ships the artifact anyway.
5. **Consent is stamped forward and withdrawn backward.** A grant is
   evaluated at run start and stamped on the run; export reads the
   stamp. A withdrawal blocks every run, stamped or not, and expires
   every artifact already produced from that principal's runs. A grant
   is a statement about data the principal has not produced yet; a
   withdrawal is a statement about data they have.
6. **Withdrawal deletes through `expires_at` and the existing artifact
   sweeper**, never through a bespoke deletion path, so the rarest
   governance operation runs on the most exercised code.
7. **An export never descends into child runs.** Each run exports
   separately, keeping the redaction surface flat, the consent question
   single-principal, and the document bounded.
8. **Promotion is the durable step and the export is perishable.**
   Exports expire on the ordinary artifact schedule; a reviewed eval
   case in source control is what lasts.
9. **The entry point is `agent run export <run-id>` and `POST
   /v1/runs/{run_id}/export`**, idempotent per run at the schema.
   `export` becomes a fourth reserved word after `agent run`, which is
   cheaper than a thirteenth top-level command and follows the
   precedent the evaluation harness set with four `agent eval`
   subcommands.
10. **Two gates**, both Milestone 3: `gate.event.export_redacted` and
    `gate.event.export_consent`.

## Consequences

- The event log's third projection becomes implementable, and Section
  31.3's four acceptance criteria each map to something a test can
  evaluate.
- The evaluation harness's promise — that the converter consumes an
  already-redacted artifact and cannot reach the raw log — is now true
  by construction, because production and consumption are separate
  commands and only one of them touches the log.
- Consent acquires a table, a stamped column, and a withdrawal sweep,
  which is new machinery at Milestone 3 for a Milestone 3 requirement.
- The stamp is a Milestone 2 column for a Milestone 3 feature. That is
  deliberate: a run that started before the column existed has no
  honest value to backfill, so the column has to precede the first run
  anyone might later want to export.
- Redaction failing closed means a shape the replacement stage does not
  cover surfaces as a failed export rather than as a leaked artifact.
  It also means an export can fail for reasons the caller cannot fix,
  which is the correct trade and should be said out loud in whatever
  the operator reads.

## Alternatives considered

- **Exports as rows in a table**: rejected. It duplicates four
  properties the artifact store already has, and the one it would most
  likely omit is the tested deletion path.
- **Redact-and-continue on a verification hit**: rejected. It converts
  a detectable defect into an undetectable one and ships the artifact
  regardless.
- **Consent evaluated at export time only**: rejected. A grant would
  then retroactively authorize export of every conversation the
  principal had before anyone asked them, which is not what a grant
  means.
- **Consent stamped only, with no withdrawal sweep**: rejected for the
  mirror-image reason. A withdrawal that does not reach existing
  artifacts is a preference, not a withdrawal.
- **Per-message timestamps retained "in case a consumer wants them"**:
  rejected. Both named consumers discard them, and the field
  re-identifies a user.
- **A thirteenth top-level CLI command**: rejected. Section 17 states
  twelve and the composition spec's own heading repeats it; a
  subcommand under an existing command changes neither.
