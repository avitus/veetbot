# ADR-0009: Run, turn, and session model

- Status: Accepted
- Date: 2026-07-17
- Related: Sections 6.3, 6.4, 12, 16, 27

## Context

The domain left several load-bearing questions implicit: what a run is relative
to a conversational turn and a session; where a new run's prior conversation
comes from (checkpoints are per-run); how an agent can ask the user a question
mid-run; and whether a session may have concurrent runs (which would contend on
the per-session event sequence).

## Decision

1. **run == turn.** Submitting a user message creates exactly one run; that run
   ends when the agent produces its final response, or fails, is cancelled, or
   times out. A session is a sequence of runs. Subagents are child runs, not
   turns.
2. The **session** owns the authoritative event log and the per-session
   sequence. A run's checkpoint holds only that run's working conversation and
   is not the session's system of record.
3. A new run **seeds its conversation from a session-history projection** built
   from events, plus the new user message, subject to context budget and
   compaction. Provider-opaque reasoning items never cross a run boundary.
4. Add `WAITING_FOR_USER`, entered by a deterministic `conversation.ask_user`
   control tool (not by parsing model prose). `POST /v1/runs/{id}/input`
   delivers the answer and resumes the **same** run; this is distinct from
   `POST /v1/sessions/{id}/messages`, which starts a new turn/run.
5. Routing user text to a waiting run versus a new run is a **deterministic API
   decision** from run state, never a model decision.
6. Default to **at most one active (non-terminal) run per session**, enforced by
   a partial unique constraint. Allocate the per-session sequence inside the same
   short transaction that appends an event; `UNIQUE(session_id, sequence)` is the
   backstop.

## Consequences

- The run stays the unit of scheduling, leasing, checkpointing, and recovery;
  the session stays the unit of memory and cross-device continuity.
- Provider switching between turns is safe by construction (only portable items
  carry forward).
- Adds an input endpoint, a control tool, and a database constraint.
- Parallel branches, if ever needed, are modeled as separate sessions or child
  runs, not concurrent same-sequence writers.

## Alternatives considered

- **run == whole session**: rejected; makes leasing, budgeting, and recovery of
  a single response hard to reason about.
- **Infer clarifying questions from assistant prose**: rejected;
  nondeterministic and a prompt-injection surface.
- **Allow concurrent runs per session by default**: rejected; sequence
  contention and ambiguous ordering.
