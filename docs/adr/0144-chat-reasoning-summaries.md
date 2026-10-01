# ADR-0144: Chat runs ask for reasoning summaries, and the app shows their headings

- Status: Accepted (authorized by the repository owner, 2026-10-01)
- Date: 2026-10-01
- Related: ADR-0006, ADR-0010, ADR-0119, ADR-0131
- Detailed design: `docs/plan/model-gateway.md`, `docs/apple-client.md`

## Context

ADR-0131 and the cache repairs removed almost all of a Chat turn's setup. On
2026-10-01, `agent run latency --days 4` measured 20 production Chat turns:
setup before the first model request took 0.75 s at the median, and the model
took the rest. Each call to gpt-6-astra at high effort took 8.5 s at the
median (25 s at p90), and its first text arrived after 7.8 s (17.6 s at p90).
Turns made 3 model calls at the median and up to 11, so the median turn took
37.6 s. Through all of that, the app said only "Working…".

The pieces for showing progress already existed. The OpenAI Responses adapter
translates `response.reasoning_summary_text.delta` into a
`ReasoningDeltaEvent` with `is_summary=True`. The API publishes reasoning
deltas as transient `reasoning.delta` frames, and the Apple client switches its
activity line to "Reasoning…" when one arrives. But the adapter sent only
`reasoning.effort`, and OpenAI streams no summary unless one is requested. An
OpenAI chat therefore never produced a reasoning frame, and the label never
changed.

On 2026-10-01 a live probe sent `reasoning.summary: "auto"` to gpt-6-astra at
high effort, with the development key. The request was accepted. The summary
streamed in parts, and each part opened with a bold title on a line of its
own, `**Title**`, followed by a blank line. On a long prompt, the first summary
text arrived about 18 s before the first answer text. A follow-up request that
replayed the reasoning item with an empty `summary` array was accepted too.

OpenAI generates reasoning summaries only for verified organizations, and it
rejects the request otherwise. The probe used the development key, so it says
nothing about the production account.

## Decision

1. **`ModelRequest.reasoning_summary` asks for a displayable summary.** It
   defaults to false. The run loop sets it on the runs that receive the owner's
   chat settings: every run that is not a typed task, when model settings are
   configured. These are the same runs the chat effort applies to (ADR-0119).
   Typed tasks, memory formation and other direct callers never set it.
2. **The OpenAI Responses adapter sends `reasoning.summary: "auto"`.** It does
   so when the flag is set and the resolved model has native reasoning, with
   or without an effort. `"auto"` lets the provider choose the most detailed
   summarizer the model supports. The Anthropic Messages and chat-completions
   adapters ignore the flag. Anthropic's adaptive thinking already streams
   thinking text, marked `is_summary=False`.
3. **Summary text is display text.** It follows the rule for all reasoning
   text: it streams as transient `reasoning.delta` frames with
   `is_summary: true`, and is never persisted or replayed. The reasoning item
   kept for continuation still carries an empty `summary` array.
4. **A refused summary never fails an answer.** OpenAI may reject the
   `reasoning.summary` parameter before any output. The adapter then sends the
   same request again without it, as one of its internal retries. It also stops
   asking for that model for the rest of the process, and logs
   `openai_reasoning_summary_refused` once.
5. **The Apple client shows the heading of the latest summary part.** The
   activity line reads "Thinking: <heading>". The reducer takes a heading only
   from frames marked `is_summary`. A heading must be a bold span that ends a
   line, does not follow a space, and is at most 80 characters long. The
   reducer keeps only the end of the line in progress, never the summary.
   It clears the heading when the answer starts, when a model response
   completes, and when the run ends. Raw reasoning (`is_summary: false`)
   still shows "Reasoning…".
6. **The reducer publishes only changes.** Before this change, it reassigned
   the run id on every frame and the reasoning flag on every reasoning frame.
   Each assignment re-rendered the chat, even when nothing had changed. Now
   the run id, the reasoning flag and the heading are assigned only when their
   values change.

## Consequences

- While gpt-6-astra reasons, a chat shows the step it is working on, and the
  label changes with each summary part.
- Tool calls still show "Working…" between model responses, beside the tool
  rows the timeline already shows.
- A long reasoning response streams hundreds of small summary deltas through
  `pg_notify` and the event stream. Like text deltas, they are best effort and
  carry no sequence.
- The per-session reasoning display filter in `model-gateway.md` is still not
  built. Summary deltas reach every subscriber, as reasoning deltas already
  did.
- If the production organization cannot summarize, each process pays one
  rejected request per model. Then chats show "Working…" as before.
- Whatever OpenAI bills for summaries appears in `model_calls` usage as
  before. The adapter's usage accounting is unchanged.

## Alternatives considered

- **Extract headings on the server and publish only those.** This would send
  fewer frames. But the gateway publishes normalized events unchanged, and a
  new frame type would need its own API contract. The client can find the
  heading in the frames it already receives.
- **Show the whole summary as an expandable transcript.** `model-gateway.md`
  warns that a summary presented as a transcript claims something about
  OpenAI's reasoning that is not true. A one-line status is the smallest
  surface that shows progress.
- **Ask for summaries only with an explicit effort.** A chat on a model's
  default effort would then show none.
- **Ask for summaries on every run.** Typed Email runs and memory formation
  have no live reader, so the deltas would go nowhere.
