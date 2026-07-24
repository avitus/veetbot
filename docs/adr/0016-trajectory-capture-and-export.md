# ADR-0016: Trajectory capture and export

- Status: Accepted
- Date: 2026-07-20
- Related: ADR-0003 (event log), ADR-0006/0007 (no raw reasoning storage), Sections 20, 31

## Context

The event log already captures every run in full. Hermes exports ShareGPT-format
trajectories (both successful and failed) to train next-generation tool-calling
models. Our plan has a capability-evaluation track (Section 20) but no path from
real runs to eval fixtures or fine-tuning data.

## Decision

1. Add a **trajectory-export projection** over the event log that emits portable
   conversation + tool calls/results + outcomes in a standard format (e.g.
   ShareGPT / messages) for (a) generating deterministic **eval fixtures** from
   real runs and (b) **distillation/fine-tuning** data, especially for
   self-hosted open models (ADR-0012).
2. **Exclude** secrets, raw reasoning (ADR-0006/0007), and policy-restricted PII;
   exports are **tenant-scoped and consent-gated**; capture both successful and
   failed trajectories (failures are the most valuable for training).

## Consequences

- Real runs become regression fixtures and training data — a virtuous loop,
  particularly valuable once open-model support lands.
- Redaction and consent must be enforced rigorously; a leak here is a data-
  governance incident.

## Alternatives considered

- **Synthetic-only evaluations**: rejected; misses the real-world distribution.
- **No export**: rejected; wastes the event log's latent value.
