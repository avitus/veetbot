# ADR-0068: Retryable memory formation and governed replay

- **Status:** Accepted
- **Date:** 2026-08-22
- **Related:** Milestone 10, ADR-0018, ADR-0045, ADR-0051, ADR-0057
- **User authorization:** implement the recommended memory-formation repair,
  diagnostics, and replay path

## Context

A completed production run contained three durable user facts in one ordinary
message: a newly started soprano-saxophone activity, many years of tenor-
saxophone experience, and recurring right-thumb pain after playing. Formation
was requested and maintenance ran, but the provider attempt failed. The
deterministic fallback recognized none of the phrasing, the consolidation still
advanced its watermark, and no belief was persisted. The provider audit retained
only the exception class, so it could not distinguish a retryable transport
failure from a permanent request error.

ADR-0057 correctly kept provider failure off the interactive path and preserved
deterministic fallback, but it treated every failed provider attempt as a terminal
formation outcome. Manually inserting a belief would erase source-event and
formation-run provenance, and replaying the original session through an ad hoc
script would create an ungoverned second write path.

## Decision

1. The deterministic extractor recognizes this ordinary experience narrative as
   three independently correctable candidates with semantic subjects. A recurring
   physical symptom is sensitive and flagged for review.
2. A provider-backed extraction returns list-compatible deterministic proposals
   plus optional content-free failure metadata. The normalized model failure is
   preserved across stream collection so audits can record failure kind, safe
   provider code, HTTP status, provider parameter, whether the stream produced
   output, and retryability. Exception messages, prompts, and responses are not
   stored.
3. Transient and protocol failures are retryable; permanent failures are not.
   Retryable failure commits valid deterministic candidates but does not advance
   the source watermark. It appends an idempotent formation request after 60
   seconds and then 300 seconds. Three total attempts are allowed. Exhaustion
   advances the watermark and appends a content-free terminal process event.
4. Maintenance eligibility is controlled by the latest formation request after
   the watermark. An older ready request cannot bypass a newer retry's
   `not_before`. The event log and existing consolidation watermark remain the
   durable retry state; no parallel retry table is introduced.
5. `agent memory diagnose --session` returns the owning principal's formation
   requests, watermark, provider selection and attempts, consolidation runs, and
   beliefs. `agent memory replay --session --confirm` reprocesses the original
   session prefix through `GovernedMemoryService`; source event ids and ordinary
   conflict, safety, and audit rules remain authoritative.
6. The semantic deterministic change is `formation@5`; provider-assisted
   formation with the new failure lifecycle is `formation@6`. Historical
   `formation@4` evidence remains an audit artifact but cannot activate the new
   tuple. `auto` therefore selects the deterministic path until reviewed
   `formation@6` evidence passes.

This decision extends ADR-0057 decisions 5, 6, and 8. Its provider placement,
budget, source boundary, activation mode, and evaluation gates remain unchanged.

## Consequences

- A provider outage no longer permanently consumes an otherwise retryable source
  prefix, and deterministic memories remain available during the retry window.
- Retry state survives process restart and remains inspectable without persisting
  user text in process events.
- Operators can diagnose and repair formation from original evidence without
  directly editing persistence or inventing provenance.
- Provider assistance temporarily falls back to deterministic formation after
  this change. A new live evaluation is required before `formation@6` can
  activate for the production tuple; tenant activation is still not a Milestone
  10 completion condition under ADR-0061.
- The repair does not itself deploy code or replay any production session. Those
  remain explicit operational actions after the repaired release is installed.

## Alternatives considered

- **Advance the watermark and rely on deterministic fallback:** rejected because
  an outage can silently make rich formation loss permanent.
- **Retry inline before returning maintenance:** rejected because it holds worker
  resources, hides retry timing, and weakens the durable background boundary.
- **Insert the missing belief manually:** rejected because it destroys the
  source-event and formation-run evidence needed for correction and re-derivation.
- **Add a retry table:** rejected because the append-only formation request and
  consolidation watermark already provide the required durable state.
- **Continue using `formation@4` evidence:** rejected because its deterministic
  baseline and failure lifecycle are not the implementation now being activated.
