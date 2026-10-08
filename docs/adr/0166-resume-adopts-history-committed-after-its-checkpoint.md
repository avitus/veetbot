# ADR-0166: A resumed run adopts the history committed after its checkpoint

- Status: Proposed (runtime repair requested by the owner on 2026-10-08)
- Date: 2026-10-08
- Related: ADR-0003 decision 12 (made mechanical), ADR-0004 (recovery),
  ADR-0006 (continuation loss), Section 14.2
- Detailed design: `docs/plan/event-log-and-persistence.md`,
  `docs/plan/runtime-loop.md`

## Evidence

On 2026-10-07 production run b642e4d7 failed with `INTERNAL_ERROR`. Its
second attempt wrote checkpoint 12 (`tool_pending`, one `web.fetch` call), the
call failed, checkpoint 13 (`tool_call`) followed, and the next model turn
committed an assistant message with a `conversation.ask_user` call. A
maintenance prune race then deleted checkpoint 13. The third attempt resumed
from checkpoint 12 and re-dispatched the read-only fetch, which ran again under
a new invocation row and appended a second result for the same call. The
fourth attempt read session history holding two results for that call and an
unanswered question call, and context assembly raised
`assembled context contains duplicate tool pair identifiers`.

The prune race is fixed separately. Two defects made any lost checkpoint
dangerous, whether lost to a prune, a crash between two transactions, or a
lease expiry:

1. A checkpoint stores its conversation as session history through its
   `last_event_sequence`, so the next checkpoint's reference covers everything
   an earlier attempt committed after it. The resumed attempt's in-memory
   conversation stopped at the restored checkpoint, so it redid that work, and
   the stored history held both copies.
2. Every invocation's idempotency key includes the step that proposed it.
   The resumed batch was dispatched at the run's current step counter, which
   the earlier attempt had already advanced, so the pipeline did not find the
   original invocation and executed the call again.

## Decision

1. **The log, not the checkpoint, says where the run is.** At resume the
   executor appends to the restored conversation every session-history item
   committed after the checkpoint's `last_event_sequence`, keeping the
   in-memory conversation equal to the history its next checkpoint will
   reference.
2. **The checkpoint's own batch keeps its step.** When the adopted items
   contain no model output, the batch the checkpoint recorded (pending calls,
   or a `model_response` checkpoint's unanswered calls) re-enters the pipeline
   at the step its `budget_state` recorded. Recorded outcomes are returned by
   idempotency key and their context updates are reapplied. A result the log
   already holds is not appended again, and the checkpoint's tool-usage
   watermark decides whether the batch was counted, as before.
3. **A turn the checkpoint never saw supersedes its batch.** When the adopted
   items contain an assistant message or a tool call, the restored batch had
   returned before that turn. The provider continuation is dropped, because it
   belongs to an older turn and the newer turn's was never checkpointed. Only
   the newest turn's unanswered calls are dispatched, at the current step,
   because the loop starts a step only after the previous batch returns. That
   batch was never counted, so its full size is recorded.
4. **A committed final reply completes the run.** If the adopted history ends
   with the final assistant message and no call is unanswered, the run has
   answered and only its finalization was lost. The executor finalizes with
   that message instead of asking the model again.

The four-step recovery procedure of Section 14.2 is unchanged: this decision
defines its first step, "load the latest checkpoint", as the checkpoint plus
the log after it.

## Alternatives rejected

- **Excluding the abandoned suffix from history.** A marker event could hide
  what a superseded lease epoch committed after the restored checkpoint. The
  model would then propose that work again under new call identifiers that no
  idempotency key matches, so a non-idempotent effect the abandoned attempt
  completed would happen twice. It also needs a new event type, a projection
  rule, and a way to keep owner answers and child-run results that arrive in
  the same window.
- **Deduplicating in context assembly.** Choosing one of two results, or
  dropping an orphaned call, hides corruption instead of preventing it, and it
  has to guess which result the model acted on.
- **Writing checkpoints in the same transaction as every conversation event.**
  It would close the crash windows but not deletion, and it puts checkpoint
  serialization on every tool and model commit.

## Consequences

- Deleting any suffix of a run's non-terminal checkpoints and resuming now
  reaches the uninterrupted run's terminal state, with every tool executed
  once. `gate.event.checkpoint_dispensable` checks every restore point of a
  two-tool run.
- A turn adopted from the log loses its provider continuation, so the next
  request omits that turn's opaque reasoning. ADR-0006 already accepts this.
- Checkpoint-only state that steps after the checkpoint changed is not rebuilt
  for adopted steps: loop counters, tool evidence, and context updates such as
  loaded skills. The run repeats that work if it needs it. This is time, not
  information.
- If an adopted turn's whole batch returned but the attempt died before
  recording its usage, the batch is treated as recorded, so the tool-call
  counter can undercount by that batch.
- Resume reads the session history once more. A long session pays for one
  extra projection read per resumed execution.

## Verification

- `tests/integration/test_event_runtime_m2.py::test_lost_checkpoints_cost_time_not_information`:
  a two-tool run interrupted after its second model turn, resumed with the last
  one to five checkpoints deleted, plus a run interrupted while its second
  model call was in flight. Before the fix, four restore points failed: either
  the second call never ran or the first ran twice, and the stored history
  failed `validate_tool_pairs`. Deleting every checkpoint already passed,
  because reseeding reads the whole history.
- `test_a_question_adopted_after_lost_checkpoints_survives_the_next_resume`:
  run b642e4d7's shape through the owner's answer and the resume after it.
  Before the fix, the resumed run re-ran the lookup and never asked the question.
- `test_a_committed_final_reply_completes_the_resumed_run`: before the fix,
  the resumed run asked the model again and failed when the script was
  exhausted.
