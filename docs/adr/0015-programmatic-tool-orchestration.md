# ADR-0015: Programmatic tool orchestration

- Status: Accepted
- Date: 2026-07-20
- Related: ADR-0008 (sandbox isolation), Sections 8, 12, 28

## Context

Multi-step tool pipelines cost one model round-trip per step. Hermes lets the
model write a script that calls tools via RPC in a single turn — "collapsing
multi-step pipelines into zero-context-cost turns" — and refunds iteration budget
for code-only turns. This is a large token and latency win the current design
misses.

## Decision

1. Add a **code-orchestration tool**: the model writes code that runs in the
   sandbox (ADR-0008) and invokes registered tools through an **in-sandbox RPC
   bridge** back to the tool executor.
2. Every underlying tool call still passes the **full pipeline**: schema
   validation, principal scopes, policy, approval, timeout, output limits,
   tracing, and idempotency. The bridge is the enforcement point; sandboxed code
   cannot bypass it or reach credentials directly.
3. **Refund** the step/model-call budget for orchestration-only turns so
   pipelines do not exhaust limits; cap the total number of underlying calls.
4. If an underlying call requires approval, the orchestration turn checkpoints at
   the bridge and the run pauses/resumes normally (Sections 9, 27).

## Consequences

- Large token/latency savings for pipelines and data-heavy work.
- Complexity in the RPC bridge and in enforcing per-call policy from within
  running code; approval-mid-script requires careful checkpoint/resume.

## Alternatives considered

- **One tool call per model turn**: rejected; slow and expensive for pipelines.
- **Let sandbox code call external services directly**: rejected; bypasses the
  policy engine and the credential broker (ADR-0008, Section 22).
