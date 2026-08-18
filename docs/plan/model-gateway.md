---
title: Model Gateway
status: design
canonical: true
---

# The model gateway and the first two adapters

This document expands Sections 2.3, 6.5, 6.6, 6.7, 6.8, 6.9, 7, 10 and all of
its subsections, 12.3, 13, 19, and 20 of the
[engineering plan](engineering-plan.md), and Milestones 1 and 3. It is recorded
as [ADR-0002](../adr/0002-provider-neutral-model-protocol.md) and constrained by
[ADR-0007](../adr/0007-provider-neutral-reasoning-state.md),
[ADR-0006](../adr/0006-no-private-reasoning-storage.md), and
[ADR-0012](../adr/0012-open-and-self-hosted-models.md).

It does not replace the requirements in those sections. Every requirement they
state still holds; this document defines the types they name, resolves the
references they leave dangling, and specifies the behaviour they assume. Where
a plan sentence and a sentence here appear to conflict, the plan wins and the
conflict is a defect in this document.

## The gateway is a translator, and the pressure is always to make it a brain

Section 5's dependency rules say the gateway may not execute tools (rule 10)
and that provider SDK objects may never cross an adapter boundary (rule 6).
Section 21's Milestone 1 acceptance criterion is blunter: *"No provider-specific
code exists in the runtime."* Those three sentences describe the same boundary
from three directions, and the whole design follows from taking them literally.

The gateway's job is to turn one neutral request into one provider call, and
one provider's stream into one neutral stream. It does not decide what to send,
it does not decide what to do with what comes back, and it does not retry
anything it has already started reporting on. Everything that looks like
judgement — which model, what context, whether to call the tool the model
asked for, whether to try again — belongs to a caller.

This is worth stating because the gateway is where every provider difference
first becomes visible, and the cheapest fix for a provider difference is
almost always a special case one layer up. Anthropic rejects a request whose
signed thinking blocks were altered; OpenAI does not. Anthropic reports cache
writes as a separate token class; OpenAI does not report them at all. Anthropic
has no equivalent of `response.incomplete`; OpenAI has no equivalent of
`stop_sequence`. Each of those has an obvious local fix in the runtime, and
each of those local fixes is the thing Milestone 1's acceptance criterion
forbids. The differences have to be absorbed here, in the adapters, behind a
protocol that does not leak them — which means the protocol has to be rich
enough to express both providers and strict enough that a caller cannot tell
which one answered.

Two consequences run through everything below. The first is that the neutral
vocabulary has to be defined before the adapters, not derived from whichever
adapter is written first — which is exactly why Section 2.3's v2.1 amendment
makes both first adapters co-equal. The second is that where a provider cannot
supply something the protocol asks for, the answer is an explicit absence
(`None`, with a documented meaning) rather than a plausible substitute. A
fabricated reasoning-token count is worse than a missing one, because the
missing one is visibly missing.

## The vocabulary the protocol is written in

Section 10.2 declares `ModelEvent` as a union and never defines a single
member. Section 6.6 declares `ConversationItem` as a union and never defines a
single member. `ModelTurn.usage: ModelUsage` names a type that appears nowhere
else in the plan. This section defines them. Every field is either named by an
existing plan sentence or required by one, and the derivation is noted where it
is not obvious.

### Conversation items

Section 6.6's union, made concrete. These are the items a `ContextBuilder`
assembles and an adapter renders into provider wire format.

```python
class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str

class ImageReferencePart(BaseModel):
    kind: Literal["image"] = "image"
    artifact_id: UUID            # Section 7 ArtifactStore; never inline
    media_type: str
    detail: str = "auto"

class FileReferencePart(BaseModel):
    kind: Literal["file"] = "file"
    artifact_id: UUID
    media_type: str
    filename: str | None

ContentPart = TextPart | ImageReferencePart | FileReferencePart
```

Images and files are references, never inline bytes. Section 6.8's event
envelope is persisted and Section 22 forbids large or sensitive blobs in
events; an artifact id keeps the conversation item small enough to live in the
log and keeps the bytes under the artifact store's retention and redaction
rules. The adapter resolves the reference at render time and the resolved bytes
never re-enter the conversation.

```python
class SystemMessage(BaseModel):
    kind: Literal["system"] = "system"
    content: list[ContentPart]
    trust: TrustLevel = TrustLevel.PLATFORM

class UserMessage(BaseModel):
    kind: Literal["user"] = "user"
    content: list[ContentPart]
    trust: TrustLevel = TrustLevel.USER
    principal_id: str | None = None

class AssistantMessage(BaseModel):
    kind: Literal["assistant"] = "assistant"
    content: list[ContentPart]
    item_index: int              # position in the provider's output
    trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED

class ToolCallItem(BaseModel):
    kind: Literal["tool_call"] = "tool_call"
    call_id: str                 # provider's id, preserved verbatim
    item_index: int
    name: str
    arguments: dict[str, Any]    # parsed; see stream assembly below
    raw_arguments: str           # exactly what the provider emitted

class ToolResultItem(BaseModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: str                 # must match a ToolCallItem.call_id
    content: list[ContentPart]
    is_error: bool = False
    trust: TrustLevel = TrustLevel.INTERNAL_TOOL
```

`AssistantMessage.trust` defaults to `EXTERNAL_UNTRUSTED` because Section 11.2's
label describes where content came from, not how much we like it, and model
output is not a platform statement. Section 30.3 already treats model-authored
skills as untrusted until reviewed; this is the same rule one level down.

`ToolCallItem` carries both parsed `arguments` and `raw_arguments`. The parsed
form is what the tool system consumes; the raw form is what gets replayed to
the provider and what an argument-parse failure is diagnosed from. Keeping only
the parsed form makes a round-trip lossy, and Section 10.4 requires tool-call
ids and content to survive a round trip exactly.

```python
class PendingToolCall(BaseModel):
    call_id: str
    item_index: int
    name: str
    raw_arguments: str
    parse_error: str | None = None
```

Section 6.9's `RunCheckpoint.pending_tool_calls` is typed
`list["PendingToolCall"]` — a forward reference to a class the plan never
declares. This is it. It is deliberately not `ToolCallItem`: a pending call may
have arguments that failed to parse, which Section 10.3 requires the fake
provider to simulate, and a checkpoint must be able to hold that state rather
than refuse to serialize it.

### Model events

Section 10.2's union, made concrete. Every event carries the correlation
fields, because a consumer reading a merged stream from a fan-out
(Section 26) has no other way to attribute one.

```python
class ModelEventBase(BaseModel):
    attempt_id: UUID             # Section 12.3's "unique attempt ID"
    run_id: UUID
    step_number: int
    sequence: int                # monotonic within one attempt, from 0

class TextDeltaEvent(ModelEventBase):
    kind: Literal["text_delta"] = "text_delta"
    item_index: int
    text: str

class ReasoningDeltaEvent(ModelEventBase):
    kind: Literal["reasoning_delta"] = "reasoning_delta"
    item_index: int
    text: str                    # display only; never persisted
    is_summary: bool             # OpenAI emits a summary, not raw CoT

class ToolCallDeltaEvent(ModelEventBase):
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    item_index: int              # assembly key, per Section 10.2
    call_id: str | None          # known at item start on both providers
    name: str | None             # known at item start
    arguments_delta: str         # raw JSON fragment, appended in order

class UsageEvent(ModelEventBase):
    kind: Literal["usage"] = "usage"
    usage: ModelUsage
    is_final: Literal[False] = False   # provisional, always superseded

class ModelCompletedEvent(ModelEventBase):
    kind: Literal["completed"] = "completed"
    turn: ModelTurn              # carries the authoritative usage
    stop_reason: StopReason
    stop_sequence: str | None = None

class ModelFailedEvent(ModelEventBase):
    kind: Literal["failed"] = "failed"
    error: ModelError            # typed, per Section 13
    partial_turn: ModelTurn | None   # what arrived before the failure
```

`ReasoningDeltaEvent.is_summary` exists because the two providers are not
emitting the same thing. Anthropic streams thinking text; OpenAI streams a
summary of reasoning it does not disclose. A UI that labels both "the model's
reasoning" is making a claim about OpenAI that is not true, and ADR-0006's
first argument — that reasoning is an unendorsed draft — applies with more
force to a summary presented as a transcript.

`ModelFailedEvent.partial_turn` carries whatever was assembled before the
stream died. Section 10.3 requires the fake provider to simulate truncated
streams, which is only testable if a truncated stream has somewhere to put the
part that arrived. It is diagnostic, not resumable: nothing may treat a partial
turn as a turn.

### `ModelUsage`, and how it relates to `RunUsage`

`ModelTurn.usage: ModelUsage` (Section 10.2) is per call. `RunUsage`
(Section 6.5) is per run. The plan defines the second and not the first, and
states no accumulation rule between them.

```python
class ModelUsage(BaseModel):
    input_tokens: int
    cached_input_tokens: int         # read from cache; billed lower
    cache_write_input_tokens: int    # 10.2's "fifth class"
    output_tokens: int
    reasoning_tokens: int | None     # None = not separately reported
    cost: Decimal
    cost_source: CostSource
    provider: str
    model: str
```

Three things about this shape are decisions rather than transcription.

`cache_write_input_tokens` is the fifth token class Section 10.2 asks for when
it says of `cache_creation_input_tokens` that we should "track it as a fifth
class". Section 6.5's `RunUsage` gains the same field, because a class that is
tracked per call and dropped per run is not tracked. This is the one place this
document adds a field to a type the plan defines, and it is adding the field
the plan asks for in a different section.

`reasoning_tokens` is `int | None`, and `None` is load-bearing. OpenAI reports
`output_tokens_details.reasoning_tokens` separately. Anthropic includes thinking
tokens inside `output_tokens` and reports no separate figure, which Section 10.2
states outright. Section 6.5 requires reasoning to be priced as output tokens —
which, for Anthropic, it already is, because they are inside the output count.
So `None` means *this provider bills reasoning inside `output_tokens`, and the
cost calculator must not add it again*, and a test asserts that no Anthropic
call ever produces a non-`None` value. An integer zero would say something
different and false: that the model did not think.

`cost` and `cost_source` are on the per-call record because Section 6.5's
precedence — provider cost API, then generation usage, then model catalog, then
docs snapshot, then config override — resolves per call, not per run. Two calls
in one run can legitimately have different cost sources, and a run-level source
would have to lie about one of them.

Accumulation is plain summation, with one rule: `reasoning_tokens` accumulates
as `None + n = n` and stays `None` only if every call in the run reported
`None`. Section 6.5's Milestone 10 note that fan-out usage is additive to the
parent run uses the same summation, applied across runs.

```python
class CostSource(str, Enum):
    # Section 6.5's precedence order, highest authority first.
    PROVIDER_COST_API = "provider_cost_api"
    GENERATION_USAGE = "generation_usage"
    MODEL_CATALOG = "model_catalog"
    DOCS_SNAPSHOT = "docs_snapshot"
    CONFIG_OVERRIDE = "config_override"
```

Section 6.5 requires "a typed `cost_source`" and gives the five values as a
precedence list. The enum is that list. Ordering is by declaration and a
comparison helper returns the more authoritative of two; nothing else may
define an ordering over it.

### `StopReason`

Section 10.2 types `ModelTurn.stop_reason` as `str` and Section 19 records it as
a telemetry attribute, so it is a value that leaves the system and gets grouped
on. An unconstrained string means Anthropic runs and OpenAI runs never group
together, and Section 12.5's termination logic ends up matching on provider
spellings — provider-specific code in the runtime, which Milestone 1 forbids.

```python
class StopReason(str, Enum):
    END_TURN = "end_turn"            # model finished normally
    TOOL_USE = "tool_use"            # model requested tool calls
    MAX_TOKENS = "max_tokens"        # output cap reached
    STOP_SEQUENCE = "stop_sequence"  # a configured sequence matched
    CONTENT_FILTER = "content_filter"  # provider refused or filtered
    INCOMPLETE = "incomplete"        # provider ended without finishing
    CANCELLED = "cancelled"          # we cancelled; deadline or user
```

Because it subclasses `str`, Section 10.2's declared field type is unchanged.

The mapping is exhaustive in both directions, and the two holes are stated
rather than papered over. Anthropic has no `response.incomplete`: an
Anthropic turn that runs out of room arrives as `max_tokens`, which maps to
`MAX_TOKENS` and never to `INCOMPLETE`. OpenAI has no stop-sequence concept in
the Responses API: `STOP_SEQUENCE` is unreachable on that adapter, and a
contract test asserts it stays unreachable rather than being approximated. A
value that one provider can never emit is honest; a value that means different
things per provider is not.

### Errors

Section 13 names three error types in a prose taxonomy and defines none of
them. Section 13's retry matrix and the event-log spec's step-level retry both
dispatch on them, so they need shapes.

```python
class ModelError(BaseModel):
    provider: str
    model: str
    attempt_id: UUID
    message: str                 # redacted; never the raw provider body
    provider_code: str | None    # provider's own code, for diagnosis
    http_status: int | None
    provider_parameter: str | None  # provider field path after closed-character validation

class ModelTransientError(ModelError):
    kind: Literal["transient"] = "transient"
    retry_after: timedelta | None   # from Retry-After when present
    stream_had_output: bool         # decides adapter vs caller retry

class ModelPermanentError(ModelError):
    kind: Literal["permanent"] = "permanent"

class ModelProtocolError(ModelError):
    kind: Literal["protocol"] = "protocol"
    detail: str                  # which invariant the stream broke
```

`message` is redacted before it is constructed. Milestone 3's acceptance
criterion is that no API keys or raw authorization headers enter logs or
events, and a provider error body is the most common way one does: it echoes
the request. The adapter builds the message from a fixed template plus the
provider's own error code, never from the response body.

`provider_code` and `provider_parameter` are accepted only through closed
character grammars before they enter an event. The runtime copies those values
and `http_status` into `RunFailure.details` for model failures so event-stream
clients can diagnose a rejected field without receiving the provider's raw
response body. The public `RunView` continues to omit `details` as required by
the HTTP boundary.

`stream_had_output` is what resolves the retry-ownership question below.

`ModelProtocolError` is the gateway accusing the provider, not the model. It is
raised when the stream violates the contract in the next section — a delta for
an item index that never started, two terminal events, a completed event with
no usage — and it is permanent by nature: replaying a broken protocol produces
the same broken protocol.

### Attempts

Section 12.3 requires "a unique attempt ID" and does not say what carries it.

```python
class ModelAttempt(BaseModel):
    attempt_id: UUID             # unique per provider call, never reused
    run_id: UUID
    step_number: int
    attempt_number: int          # 1-based; Section 13's retry counter
    started_at: datetime
```

The attempt id is a correlation key, not an idempotency key. Section 12.3 is
explicit that a repeated model call after a crash may incur duplicate provider
cost; what it must not do is produce duplicate external side effects, and that
is guaranteed by the tool layer's idempotency keys (Section 8.4), not here.
Where a provider offers request-level idempotency the adapter may pass the
attempt id through, but no platform behaviour may depend on the provider
honouring it, because two of the three first adapters do not offer it.

## The stream contract

Section 10.2 fixes the field mappings for both providers. It does not say what
a well-formed normalized stream looks like, and without that statement the
adapters have nothing to be tested against. This section states it.

A single attempt produces a sequence of `ModelEvent` values obeying six
invariants. The adapter is responsible for all six. A stream that violates any
of them is a bug in the adapter, not a condition the caller must tolerate, and
the gateway's own validator raises `ModelProtocolError` when it sees one.

1. `sequence` starts at 0 and increases by exactly 1 for every event in the
   attempt, including the terminal event. A caller that sees a gap knows it
   dropped an event rather than that the provider sent nothing.
2. Exactly one terminal event ends the stream: either `ModelCompletedEvent` or
   `ModelFailedEvent`, never both and never neither. Section 10.4 already
   states this for the completed case; this extends it to the failure case so
   that a caller has one place to look for the end.
3. Delta events for a given `item_index` are contiguous and ordered. The
   adapter must not interleave two items' text deltas. Both providers emit
   items sequentially, so this costs nothing to honour and it lets the
   assembler use a single open buffer rather than a map of buffers.
4. Every `ToolCallDeltaEvent` for an `item_index` carries the same `call_id`
   and `name` once those are known, and both are known at item start on both
   providers. The first delta for an item may therefore be relied upon to
   name the tool.
5. `UsageEvent` is advisory. It may appear zero or more times and its values
   are provisional. The authoritative usage is `ModelCompletedEvent.turn.usage`
   and it always supersedes anything a `UsageEvent` reported. This resolves
   the ordering contradiction noted below: `UsageEvent` is not a terminal
   event and does not compete with invariant 2.
6. No event carries provider-raw error text, authorization headers, or API
   keys. `ModelError.message` is redacted at the adapter boundary, which is
   the only place that has seen the raw body. Milestone 3's acceptance
   criterion "No API keys or raw authorization headers enter logs or events"
   is satisfied here or nowhere.

### Assembling a turn from a stream

The gateway ships one assembler, shared by every adapter, that folds an event
sequence into a `ModelTurn`. Adapters emit events; they do not build turns.
This is what makes the contract suite meaningful: the same assembler runs over
OpenAI, Anthropic, and `chat_completions` event sequences, so a difference in
the resulting turn is a difference in the events, which is exactly the thing
under test.

The assembler keeps one open item at a time, keyed by `item_index`. Text
deltas append to a string. Tool-call argument deltas append to
`raw_arguments`. When an item closes, a tool call is parsed:

```python
def close_tool_call(pending: PendingToolCall) -> ConversationItem:
    try:
        arguments = json.loads(pending.raw_arguments)
    except json.JSONDecodeError as exc:
        return _malformed(pending, str(exc))
    if not isinstance(arguments, dict):
        return _malformed(pending, "arguments were not an object")
    return ToolCallItem(
        call_id=pending.call_id,
        item_index=pending.item_index,
        name=pending.name,
        arguments=arguments,
        raw_arguments=pending.raw_arguments,
    )
```

Malformed arguments are not a stream error. The model produced bad JSON, which
is a modelling failure, and the runtime's answer to a modelling failure is to
tell the model. `_malformed` returns a `ToolCallItem` whose `arguments` is
empty and records the parse error, and the runtime pairs it with a
`ToolResultItem` carrying `is_error=True` and the parser message. The model
gets a chance to correct itself on the next step. Raising here would convert a
recoverable turn into a failed run, and Section 13 reserves failure for
conditions the model cannot fix.

`raw_arguments` is retained on every tool call, including successful ones.
Section 12.3's replay requirement and the trajectory export in Section 20 both
need the bytes the provider actually emitted, not a re-serialization of the
parsed object, because the two differ in key order and whitespace and a replay
that differs from the recording is not a replay.

## Closing the gaps in the Section 10.2 mapping table

Section 10.2's table is the copy-ready field mapping for both providers, and
it is close to complete. Nine cells are missing or ambiguous. Each is resolved
below. None of these resolutions changes a mapping the table already states;
they only fill cells the table left blank.

**Anthropic `server_tool_use`.** The table has no row for it because we never
request server-side tools. The adapter therefore treats a `server_tool_use`
content block as a `ModelProtocolError` with
`detail="unexpected server_tool_use block"`. This is deliberate and
conservative: a server tool executed by the provider bypasses the policy
engine entirely, so silently mapping it onto `ToolCallItem` would let a class
of side effects through without a policy decision. If we ever want server-side
tools, they need a policy story first, and the loud failure is what forces
that conversation rather than deferring it.

**`content_block_stop` and `message_stop`.** These are structural, not
semantic. `content_block_stop` closes the open item in the assembler and emits
no normalized event. `message_stop` triggers the terminal
`ModelCompletedEvent`. The neutral protocol has no "item ended" event because
no consumer needs one: text is streamed for display and the item boundary is
carried by `item_index` changing.

**No OpenAI source for `UsageEvent`.** Correct, and it does not matter,
because invariant 5 makes `UsageEvent` advisory. The OpenAI adapter emits no
`UsageEvent` at all and reports usage once, on the completed event, from
`response.completed.response.usage`. The Anthropic adapter emits a provisional
`UsageEvent` from `message_start.message.usage` (which carries input and cache
token counts before any output exists) and the authoritative figures on
completion from the accumulated `message_delta.usage`. Callers that display a
live cost meter use the provisional values; callers that bill use the terminal
one. Nothing reconciles them because nothing needs to.

**No OpenAI analog of Anthropic's thinking signature.** The signature is
carried in `ProviderReasoningItem.provider_payload`, which is provider-opaque
by construction, so the absence of an OpenAI field is not a gap in the neutral
protocol. OpenAI's continuation mechanism is the `response.id` plus encrypted
reasoning items, which live in the same opaque payload. Both providers'
continuation state has exactly one requirement on us: round-trip it unchanged.

**No Anthropic analog of `response.incomplete`.** Anthropic signals the same
condition through `stop_reason` on `message_delta`, principally `max_tokens`.
The neutral `StopReason.INCOMPLETE` is therefore produced only by the OpenAI
adapter, from `response.incomplete`, and only when the incomplete reason is
not one the neutral vocabulary already names. An OpenAI response that is
incomplete because of the output cap maps to `MAX_TOKENS`, not `INCOMPLETE`,
so that the two providers agree on the common case.

**In-band `<think>` text.** ADR-0012 calls this "the third representation" and
Section 10 gives it no mapping row. Here it is:

| In-band form | Neutral event | Notes |
| --- | --- | --- |
| Text before `<think>` | `TextDeltaEvent` | ordinary output |
| Text inside `<think>` | `ReasoningDeltaEvent` | `is_summary=False` |
| Text after `</think>` | `TextDeltaEvent` | ordinary output |
| Unclosed `<think>` | `ReasoningDeltaEvent` | to end of stream |
| `<think>` mid-token | buffered | see below |

The scrubber is a streaming state machine in the `chat_completions` adapter,
not a post-hoc regex, because the tag can be split across two deltas. It holds
back at most `len("</think>")` characters of lookahead, which bounds the
display latency it introduces to one token. Nested tags are not supported and
a second `<think>` inside an open one is treated as literal text. The tag set
is configurable per provider profile, defaulting to `<think>` and
`</think>`, because open models do not agree on the delimiter.

**`cache_creation_input_tokens` has no `RunUsage` field.** Section 10.2 says
to "track it as a fifth class" and stops. `ModelUsage.cache_write_input_tokens`
is that field, and `RunUsage` gains a matching accumulator. It is tracked
separately rather than folded into `input_tokens` because it is priced
differently on Anthropic (a premium over the base input rate) and does not
exist on OpenAI, and a single summed field would make the two providers'
numbers incomparable in exactly the place where comparison matters.

**`reasoning_tokens` has no Anthropic source.** Section 10.2 states that
Anthropic includes thinking tokens in `output_tokens`, and Section 6.5 prices
reasoning separately. The resolution is that `ModelUsage.reasoning_tokens` is
`None` on Anthropic, meaning "not separately reported", and the cost
calculation for Anthropic prices `output_tokens` at the single output rate
that already includes thinking. The pricing table therefore carries a
`reasoning_priced_separately` flag per model rather than assuming every
provider itemizes. `None` and `0` mean different things and the type reflects
that: `0` means the model did no reasoning, `None` means we cannot tell.

**OpenAI lifecycle events absent from the table.** `response.created`,
`response.in_progress`, `response.content_part.added`,
`response.content_part.done` and `response.output_item.done` are structural.
They drive the assembler's item bookkeeping and emit no normalized events, for
the same reason `content_block_stop` does not. `response.output_item.added` is
the exception already in the table: it carries the tool call's `call_id` and
`name`, which is what makes invariant 4 satisfiable on OpenAI.

## Caching, and who is responsible for it

Prompt caching is the single largest cost lever in the system and it is also
the one most easily broken by an innocent-looking change. Section 10.1 makes
prefix stability an invariant rather than an optimization for that reason. The
gateway's role in it is narrow and worth stating precisely, because the
temptation is for the gateway to start making caching decisions and it must
not.

The context engine decides where the cache boundaries are. It has the only
complete view of what is stable and what is volatile, it computes
`prefix_sha256`, and it populates `CacheHints` on the `ContextPlan`
(`context-engine.md:816-818`). The gateway translates those hints into
provider syntax and nothing more. It does not add breakpoints, it does not
move them, and it does not decide that a request would cache better a
different way.

The translation differs sharply between the two first providers:

| Aspect | Anthropic | OpenAI |
| --- | --- | --- |
| Mechanism | explicit `cache_control` | automatic prefix |
| Breakpoints | at most 4 | hints ignored |
| Minimum | model-dependent, ~1024 | provider-managed |
| TTL control | `default` or `1h` | none |
| Reported as | `cache_creation`/`cache_read` | `cached_tokens` |

Because OpenAI may ignore breakpoints entirely, the OpenAI adapter drops
`CacheHints` after recording that it did so. This is not a failure and must
not be logged as one. What it does mean is that on OpenAI the prefix-stability
invariant is the whole mechanism: there is no explicit marker to fall back on,
so a prefix that changes byte-for-byte simply stops caching with no signal
other than the ratio falling.

The breakpoint budget is where the two providers force a real decision. Four
breakpoints, three natural boundaries (`after_system`, `after_tools`,
`after_history_prefix`), and a fourth that the context engine may place at a
compaction boundary. When the context engine supplies more hints than the
provider allows, the adapter keeps the earliest ones and drops the rest, on
the reasoning that an earlier breakpoint protects a larger prefix. The adapter
records the drop on the attempt so that a persistent shortfall is visible
rather than silent.

TTL selection follows Section 10.1's guidance: a long agentic loop that will
issue many calls against the same prefix requests the one-hour TTL, an
interactive session takes the default. The `ContextPlan` carries the choice
because the context engine knows the session shape; the gateway does not.

### Measuring it

The cached-prefix ratio is defined in `context-engine.md:794-796` and the
gateway supplies its numerator and denominator, not its interpretation.
Every completed attempt records `input_tokens`, `cached_input_tokens` and
`cache_write_input_tokens` on the `model_calls` row and on the
`model.response.completed` event. The context engine's metric reads those.
Below roughly 90 per cent on a session that should be stable, the invariant is
leaking, and the diagnosis is a prefix diff, which is why `prefix_sha256` is
recorded on `model.request.started` (`context-engine.md:133-141`). Two
consecutive requests in one session with different prefix hashes and no
intervening epoch bump is the signature of the bug.

Section 6.8 never defined those two event payloads. They are defined below in
the events section, because the gateway is what emits them.

## Routing and capability resolution

`ModelRequest.model_policy` is a bare string in the plan (Section 10.1) and
several documents need things that a string cannot answer: whether the model
supports images, what its context window is, what it costs, whether it does
native tool calling, how much output to reserve. `context-engine.md:221`
wants "8,192 or the model's default" and has no carrier for the second half.
Section 10.5's YAML defines only a `balanced` policy. There is no port that
turns a policy name into any of this.

The gateway owns that resolution, through one port:

```python
class ModelRouter(Protocol):
    async def resolve(
        self,
        model_policy: str,
        *,
        tenant_id: str,
        required: CapabilitySet | None = None,
    ) -> ResolvedModel: ...

    async def resolve_pinned(
        self, pin: ProviderPin
    ) -> ResolvedModel: ...
```

```python
class ResolvedModel(BaseModel):
    provider: str                # adapter key: openai, anthropic, ...
    model: str                   # provider's own model identifier
    capabilities: ModelCapabilities
    limits: ModelLimits
    pricing: ModelPricing
    credential_ref: str          # names a secret; never the secret
    policy_name: str             # what was asked for, for the record
    resolved_at: datetime
```

```python
class ModelCapabilities(BaseModel):
    native_tool_calling: bool    # false routes via ADR-0012's parser
    parallel_tool_calls: bool
    images: bool
    audio: bool = False
    files: bool
    reasoning: ReasoningSupport  # none | native | in_band
    provider_managed_state: bool
    explicit_cache_control: bool
    structured_output: bool
    streaming: bool = True
```

This is not the `ModelCapabilities` of `engineering-plan.md:599`. Two
fields are renamed, one moves into `ModelLimits` below, and three are
added, so the two declarations are a divergence rather than an extension.
They are reconciled field by field further down, under "The two
`ModelCapabilities` declarations", and that table rather than this fence is
what an implementer holding the plan open should read.

```python
class ModelLimits(BaseModel):
    context_window_tokens: int
    max_output_tokens: int       # the model's own cap
    default_output_reserve: int  # context-engine.md:189's second half
    max_cache_breakpoints: int   # 4 on Anthropic, 0 on OpenAI
    max_tool_count: int | None
```

```python
class ModelPricing(BaseModel):
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    cache_write_per_mtok: Decimal | None
    output_per_mtok: Decimal
    reasoning_per_mtok: Decimal | None
    reasoning_priced_separately: bool
    source: CostSource
    effective_at: datetime       # prices change; rows are not updated
```

The router is a port with one implementation in 0.1, reading the model
registry described at `engineering-plan.md:1288-1295`. The registry is
configuration, not code: a YAML file per provider profile, validated at load,
hashed the way the policy profile is hashed so that a run records which
registry it resolved against. That document's schema, its validation, and the
format of `registry_version` are below. Making it a port rather than a module is what
lets Milestone 10's availability-aware routing arrive later without touching
call sites.

### Pinning, and the contradiction with availability routing

Section 10 (`engineering-plan.md:1305`) requires a run to be pinned to one
provider. Milestone 10 (`engineering-plan.md:2929`) wants routing to move work
between providers on availability. These are in tension and the resolution is
temporal, not architectural.

Selection happens once, at run start. From then until the run reaches a
terminal state, `ProviderPin` is absolute: every attempt in the run goes to
the pinned provider and model, and a provider outage fails the run rather than
silently switching. Milestone 10's routing chooses which provider a run is
pinned to when the run begins, and may consider availability, latency, queue
depth and price when doing so. It does not re-route a live run.

```python
class ProviderPin(BaseModel):
    run_id: UUID
    provider: str
    model: str
    registry_version: str        # what the pin was resolved against
    pinned_at: datetime
```

The reason mid-run switching is refused is reasoning continuation. Both
providers carry opaque continuation state that is meaningless to the other,
and a switch mid-run either discards it, which silently degrades the model's
reasoning without telling anyone, or attempts to translate it, which cannot be
done. Section 10 already made this call; this document only explains it so
that a future reader does not undo it as an obvious improvement.

A resumed run keeps its pin. `ProviderPin` is persisted on the run rather than
held in memory, because `event-log-and-persistence.md:618` shows
`ProviderContinuation` being lost across a worker restart and a lost pin would
compound that into a provider switch on resume.

## The provider profile document

`engineering-plan.md:1288` calls a provider profile "a plugin the registry
loads and the user can override without editing core" and
`engineering-plan.md:1290-1293` says what it declares: an API mode, aliases
and capabilities and limits and prices, credential pools, and a
model-catalog import. ADR-0012 decision 2 requires that a new
OpenAI-compatible provider be addable without writing code. Neither
statement gives the declaring document a schema, and "addable without code"
is not a property anyone can act on until the thing being added has a shape.
This section is that shape.

### Where a profile lives, and the two files it is not

The routing section above says the registry is a YAML file per provider
profile. `bootstrap-and-composition.md:360-361` places `models/policies.yaml`
("model_policies and provider profiles") and `models/catalog.yaml`
("aliases, limits, context windows, prices") inside the package. Read
together those describe two layouts, and the difference is not cosmetic: one
of them makes adding a provider a diff against a file every other provider
also lives in, which is the thing ADR-0012 decision 2 exists to prevent.

The reconciliation keeps every statement and adds one directory.

```text
src/agent_core/models/
  policies.yaml             model_policies, and the enabled list
  catalog.yaml              shared entries a profile may import
  providers/openai.yaml     one profile, one file
  providers/anthropic.yaml
  providers/ollama.yaml
```

`policies.yaml` keeps `model_policies` unchanged and satisfies its "and
provider profiles" half with the list of profile names this deployment
loads; a profile's body is a file of its own. `catalog.yaml` keeps exactly
the four things `bootstrap-and-composition.md:361` names it for and becomes
the target of Section 10.5's fourth declaration, the model-catalog import,
rather than a second place models are defined. A profile either declares a
model inline or imports a catalog entry for it, never both.

The overlay rules are unchanged. `AGENT_CONFIG_DIR` merges file over file by
top-level key, so an operator adds a provider by dropping one file into the
overlay's `models/providers/` and adding its name to `policies.yaml`: two
files touched, neither of them another provider's.

### A profile, worked

```yaml
schema_version: 1
profile: anthropic
adapter: anthropic
api: messages
base_url: https://api.anthropic.com
credential_ref: ANTHROPIC_API_KEY
enabled: true

capabilities:
  native_tool_calling: true
  parallel_tool_calls: true
  images: true
  audio: false
  files: true
  reasoning: native
  provider_managed_state: true
  explicit_cache_control: true
  structured_output: true
  streaming: true

limits:
  max_cache_breakpoints: 4
  default_output_reserve: 8192
  max_tool_count: null

models:
  - id: claude-sonnet-4-5
    aliases: [sonnet, default]
    limits:
      context_window_tokens: 200000
      max_output_tokens: 64000
    pricing:
      input_per_mtok: "3.00"
      cached_input_per_mtok: "0.30"
      cache_write_per_mtok: "3.75"
      output_per_mtok: "15.00"
      reasoning_per_mtok: null
      reasoning_priced_separately: false
      source: published
      effective_at: "2026-06-01T00:00:00Z"
```

An OpenAI-compatible endpoint is the same document with different values and
no new field except the one Section 10.6 already requires:

```yaml
schema_version: 1
profile: ollama
adapter: chat_completions
api: chat_completions
base_url: http://127.0.0.1:11434/v1
credential_ref: null
enabled: true

in_band_reasoning:
  open: "<think>"
  close: "</think>"

capabilities:
  native_tool_calling: false
  reasoning: in_band
  explicit_cache_control: false
  provider_managed_state: false
  parallel_tool_calls: false
  images: false
  audio: false
  files: false
  structured_output: false
  streaming: true

limits:
  max_cache_breakpoints: 0
  default_output_reserve: 2048
  max_tool_count: 32

models:
  - id: qwen3-8b
    aliases: [local-small]
    catalog: open-local-8b
```

`in_band_reasoning` is where decision 6's configurable tag pair lives.
`catalog: open-local-8b` names an entry in `catalog.yaml` and stands in for
that model's `limits` and `pricing` blocks. `credential_ref: null` is how a
profile says it needs no credential, and it is not the same as omitting the
key, which is rejected.

### What the loader rejects

Loading is total: the process either has a registry or exits non-zero naming
the file, the field, and the rule. There is no partial registry and no
profile that loads with a warning, for the reason
`gate.event.revision_pinned` gives about the schema revision. A process that
starts on configuration it could not fully parse is a process whose
telemetry describes something other than what it is running.

```text
field              rule                                     on failure
-----------------  ---------------------------------------  ----------
schema_version     equals 1                                 reject
profile            [a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?       reject
profile            equals the file stem                     reject
profile            unique across the merged registry        reject
adapter            names a registered adapter class         reject
api                responses|messages|chat_completions      reject
api                permitted by that adapter                reject
base_url           absolute https, or http on a loopback    reject
credential_ref     present; a name or null, never a value   reject
capabilities       all ten fields present, none extra       reject
capabilities       within the adapter's ceiling             reject
limits             three fields present                     reject
in_band_reasoning  present iff reasoning is in_band         reject
models             1 to 200 entries                         reject
models[].id        non-empty, unique within the profile     reject
models[].aliases   unique across the merged registry        reject
models[].catalog   resolves to an entry in catalog.yaml     reject
models[]           declares pricing or catalog, not both    reject
pricing amounts    decimal strings, never YAML floats       reject
effective_at       RFC 3339 with an explicit offset         reject
any level          an unknown key                           reject
```

Three of those rows are worth defending.

**Prices are strings.** A YAML float for `0.30` is a binary approximation,
`ModelPricing` types every amount as `Decimal`, and a registry that loses a
fraction of a cent per million tokens at parse time makes `model_prices`
unreconcilable against an invoice for a reason nobody will find. Quoting
them is the whole fix.

**`credential_ref` is a name, never a value.** The field is validated
against the shape of an environment variable name, and a value matching any
family of the secret scanner at `bootstrap-and-composition.md:1078-1115` is
rejected at load with the match not printed. This is the one field where a
mistake gets committed to a repository, and
`gate.structure.no_committed_secrets` catches it a second time.

**Unknown keys are rejected at every level.** The opposite choice, ignoring
what you do not recognize, turns a typo in `parallel_tool_calls` into a
capability silently false, which produces a deployment that quietly stops
using parallel tool calls and emits nothing that says why.

### A capability a profile claims and an adapter cannot satisfy

Every adapter class carries a `CAPABILITY_CEILING: ModelCapabilities`
constant describing what its code can do at all. The `chat_completions`
adapter's ceiling has `native_tool_calling`, `explicit_cache_control`, and
`provider_managed_state` false, because it parses tool calls out of text,
sends no cache breakpoints, and has no continuation slot to put a payload
in.

A profile may narrow the ceiling and may never widen it. A profile setting
`explicit_cache_control: true` under an adapter whose ceiling is false fails
the load, naming both. Intersecting silently was rejected because the
narrowing would be invisible: the operator who wrote `true` believes prompt
caching is on, the bill says otherwise a month later, and nothing in between
emits a line about it. A refused start is loud on the day the mistake is
made, which is the day it is cheap.

Narrowing is allowed because it is how an operator disables something a
deployment cannot afford or a vendor contract does not include, and it is
recorded rather than assumed: `ResolvedModel.capabilities` is the narrowed
set, and the narrowing is inside the profile hash, so a run's
`registry_version` resolves to the exact capability set it ran under.

### `registry_version`

`ProviderPin.registry_version` and the `model_calls` column of the same name
are declared as strings above with no format. The format mirrors
`policy_version` at `policy-and-approvals.md:632` because it answers the
same question about a different ruleset.

```text
{profile_name}@{profile_sha256[:12]}+r{registry_sha256[:8]}

anthropic@8c41f0b2e7d9+r5a3c1e04
```

`profile_sha256` is over the merged profile document, after overlay and
before interpolation, which is the point `policy_version` is taken at and
for the same reason. `registry_sha256` is over the sorted list of every
enabled profile's hash together with the hash of `policies.yaml`. The left
half says what this attempt resolved to; the right half says what the router
was choosing among when it resolved.

Both halves are needed. A pin identified only by its profile cannot tell you
that a second provider was added between two runs, and that is the change
most likely to explain why two runs a week apart resolved differently.

### `CapabilitySet` and `ReasoningSupport`

Both are referenced above and neither is declared anywhere in the corpus.

```python
class ReasoningSupport(str, Enum):
    NONE = "none"
    NATIVE = "native"
    IN_BAND = "in_band"


class Capability(str, Enum):
    NATIVE_TOOL_CALLING = "native_tool_calling"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    IMAGES = "images"
    AUDIO = "audio"
    FILES = "files"
    REASONING = "reasoning"
    PROVIDER_MANAGED_STATE = "provider_managed_state"
    EXPLICIT_CACHE_CONTROL = "explicit_cache_control"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"


CapabilitySet = frozenset[Capability]
```

One member per `ModelCapabilities` field, so `ModelRouter.resolve`'s
`required` argument is satisfied by projecting the resolved capabilities
into a set and testing containment, with no field-name string anywhere.
`Capability.REASONING` is satisfied by `NATIVE` and by `IN_BAND` and not by
`NONE`: a caller that needs the model to think does not care how the
thinking arrives, and a caller that needs it structured asks for a model
policy that guarantees it rather than for a capability.

A resolution that cannot satisfy `required` is the failure table's "Model
policy unresolvable", a permanent error, and never a downgrade to the
nearest model that fits. A silent downgrade is the same failure as a silent
capability intersection, one layer up.

### The two `ModelCapabilities` declarations

`engineering-plan.md:599` declares `ModelCapabilities` with eight fields and
this document declares it with ten. That is a divergence rather than an
addition, and this is where it gets reconciled instead of being left for an
implementer to discover.

```text
engineering-plan.md:599   here                     what changed
------------------------  -----------------------  ------------
tool_calling              native_tool_calling      narrowed
parallel_tool_calls       parallel_tool_calls      unchanged
structured_output         structured_output        unchanged
streaming                 streaming                unchanged
vision                    images                   renamed
audio                     audio                    unchanged
provider_managed_state    provider_managed_state   unchanged
max_context_tokens        ModelLimits              moved
--                        files                    added
--                        reasoning                added
--                        explicit_cache_control   added
```

`tool_calling` is the one row that changes a meaning. ADR-0012's XML parser
makes a model that cannot call tools natively still able to call tools, so
the plan's single boolean answers two different questions.
`native_tool_calling` answers the one an adapter branches on. The plan's
question, whether this model can call tools at all, is
`native_tool_calling or adapter.parses_in_band_tool_calls`, and nothing is
lost.

`max_context_tokens` moved to `ModelLimits.context_window_tokens` because
limits and prices can arrive from a catalog import while capabilities are
bounded by the adapter's ceiling, and a field subject to both rules has no
correct home. The three additions are things the gateway branches on that
the plan had no reason to name before adapters existed.

Where the row for a field and `engineering-plan.md:599` conflict the plan
wins, per this document's preamble, and the conflict is a defect here. The
renaming is recorded as an open question rather than fixed by editing a plan
sentence.

## Usage, cost, and where the numbers live

Section 6.5 fixes the precedence order for cost figures and Section 15 has no
table to put them in. `runs.usage JSONB` at `engineering-plan.md:1680` is the
only persistence the plan gives usage, and a JSONB blob on the run cannot
answer the questions the budget enforcement in Section 6.5 needs to ask: what
did this step cost, which attempt burned the tokens, and what were we charged
before the retry that produced no output.

Two tables close that.

```text
model_calls                          -- one row per attempt
  attempt_id       UUID PRIMARY KEY  -- Section 12.3's attempt ID
  run_id           UUID NOT NULL
  session_id       UUID NOT NULL
  tenant_id        TEXT NOT NULL
  step_number      INTEGER NOT NULL
  attempt_number   INTEGER NOT NULL  -- 1-based within the step
  provider         TEXT NOT NULL
  model            TEXT NOT NULL
  model_policy     TEXT NOT NULL     -- what was asked for
  registry_version TEXT NOT NULL     -- what it resolved against
  prefix_sha256    TEXT NOT NULL     -- from the ContextPlan; never absent,
                                     -- because the stability gate asserts
                                     -- exactly one distinct value per session
                                     -- and a NULL cannot participate
                                     -- (`context-engine.md:133-141`)
  input_tokens          INTEGER NOT NULL
  cached_input_tokens   INTEGER NOT NULL
  cache_write_tokens    INTEGER NOT NULL
  output_tokens         INTEGER NOT NULL
  reasoning_tokens      INTEGER NULL -- NULL: not separately reported
  cost             NUMERIC(20,10) NOT NULL
  cost_source      TEXT NOT NULL     -- CostSource
  price_id         TEXT NULL         -- FK to model_prices
  stop_reason      TEXT NULL         -- NULL if the attempt failed
  error_kind       TEXT NULL         -- transient|permanent|protocol
  started_at       TIMESTAMPTZ NOT NULL
  finished_at      TIMESTAMPTZ NULL
  INDEX (run_id, step_number, attempt_number)
  INDEX (tenant_id, started_at)      -- billing and quota rollups
  INDEX (session_id)

model_prices                         -- append-only price history
  price_id         TEXT PRIMARY KEY  -- provider:model@effective_at
  provider         TEXT NOT NULL
  model            TEXT NOT NULL
  input_per_mtok        NUMERIC(20,10) NOT NULL
  cached_input_per_mtok NUMERIC(20,10) NOT NULL
  cache_write_per_mtok  NUMERIC(20,10) NULL
  output_per_mtok       NUMERIC(20,10) NOT NULL
  reasoning_per_mtok    NUMERIC(20,10) NULL
  reasoning_priced_separately BOOLEAN NOT NULL
  source           TEXT NOT NULL     -- CostSource
  effective_at     TIMESTAMPTZ NOT NULL
  recorded_at      TIMESTAMPTZ NOT NULL
  UNIQUE (provider, model, effective_at)
```

`model_prices` is append-only and rows are never updated. A cost recorded
against a `price_id` stays reproducible when the vendor changes prices, which
is what makes a three-month-old invoice reconcilable. `model_calls.cost` is
denormalized rather than computed on read for the same reason.

`runs.usage` stays exactly as Section 15 defines it. It becomes a rollup of
`model_calls` for that run rather than the source of truth, and it is
maintained in the same transaction that writes the attempt row so that the two
never disagree. The usage repository port named at `engineering-plan.md:810`
and never typed is:

```python
class UsageRepository(Protocol):
    async def record_attempt(
        self, call: ModelCallRecord
    ) -> None: ...

    async def run_usage(self, run_id: UUID) -> RunUsage: ...

    async def tenant_usage(
        self,
        tenant_id: str,
        *,
        since: datetime,
        until: datetime,
    ) -> UsageRollup: ...
```

`RunUsage` accumulates the same five token classes plus cost, which resolves
the `ModelUsage` versus `RunUsage` question: `ModelUsage` is one attempt,
`RunUsage` is the sum over a run's attempts including the ones that failed.
Failed attempts count. A transient failure after 3,000 output tokens cost real
money and a budget that ignores it will overspend on exactly the runs that are
going worst.

### Duplicate cost after a crash

Section 12.3 accepts duplicate provider cost on crash recovery; Section 6.5
enforces `max_cost`. These meet in an unpleasant place: a run that crashes
repeatedly can exceed its budget through work it never got to keep. The
gateway's answer is that budget is checked before an attempt, not after, using
`RunUsage` that includes prior attempts, so the duplicate cost is visible to
the check even though the tokens produced nothing. A run that crashes its way
to its budget ceiling stops, and stops for the right stated reason
(`BUDGET_EXCEEDED`, with the attempt count in the event), rather than being
mysteriously expensive.

There is no model-call idempotency key because neither first provider offers
one that would let us reclaim the cost. This is recorded as an accepted cost,
not an oversight.

## Provider metadata, and why the key set is closed

`ModelTurn.provider_metadata` is declared at `engineering-plan.md:1205` as
`dict[str, Any]` and given exactly one rule at `engineering-plan.md:1207`:
it "may include response IDs and cache information, but application logic
must not rely on provider-specific fields." That is a constraint on readers.
It says nothing about writers, and an adapter is a writer.

The corpus carries twenty-two `dict[str, Any]` field declarations and
exactly one of them is bounded: `ProviderReasoningItem.provider_payload`,
which is safe because the four properties above leave it no consumer.
`provider_metadata` cannot be made safe that way, because it exists to be
read. It gets the other treatment, which is a closed set of keys, each with
a declared type, a declared writer, and a declared destination.

### The seven keys

The dictionary's contents come from a frozen model and are serialized into
it. The plan's declared type does not change — the field stays
`dict[str, Any]` on the wire, because that is what Section 10.1 says it is —
and what goes in comes from exactly one place.

```python
class ProviderMetadata(BaseModel, frozen=True, extra="forbid"):
    provider_api: str            # responses|messages|chat_completions
    response_id: str | None      # the provider's id for this response
    request_id: str | None       # the vendor's request id, support only
    resolved_model: str | None   # what the provider says it ran
    previous_response_id: str | None
    cache_breakpoints_sent: int = 0
    cache_breakpoints_dropped: int = 0
```

`ModelTurn.provider_metadata` is `ProviderMetadata(...).model_dump()` with
null values dropped, and it is built nowhere else. `extra="forbid"` is what
makes "closed" a property the type system holds rather than a convention,
and the gate below is what makes it hold across adapters nobody has written
yet.

| key | source | why it earns a key |
| --- | --- | --- |
| `provider_api` | the profile | one adapter fronts three APIs, and a row that does not say which is a row that cannot be compared |
| `response_id` | the response body | `engineering-plan.md:1250` requires the OpenAI adapter to capture it |
| `request_id` | a response header | the only identifier a vendor support ticket can be opened against |
| `resolved_model` | the response body | an alias resolves to a dated model, and reproducibility needs the dated one |
| `previous_response_id` | the request | which continuation this attempt resumed, which is the first thing to check when a reasoning chain breaks |
| `cache_breakpoints_sent` | the adapter | how many hints reached the provider |
| `cache_breakpoints_dropped` | the adapter | decision 10's drop, recorded where the attempt can be found |

`request_id` is the only header-derived value in the set, and it is copied
by name rather than by sweeping headers into a dictionary. ADR-0002's sixth
invariant forbids raw provider headers on any event, which is why this key
is persisted and never emitted: it reaches the `model_calls` row and a span
attribute, and never an event payload or an SSE frame. A support ticket is
written from the database.

### What is not in the set

Three families are excluded, and the exclusion is the design rather than an
oversight.

Anything header-derived beyond `request_id`, including rate-limit headers.
An adapter that needs `Retry-After` uses it inside its own three attempts
and does not report it upward, because a caller cannot act on a number that
was already stale when the attempt it describes finished.

Anything carrying model or user content. A refusal, a safety
classification, and a truncated completion are `StopReason` values or
errors, and both of those are typed. A key holding provider prose is a key
that puts provider prose on an event, which the sixth invariant forbids and
which the secret-leak gate would catch only in the case where the prose
happened to contain a credential.

Anything an adapter would add "just in case". Adding a key is a schema
change with a migration behind it, deliberately, because the alternative is
a dictionary that grows into a second schema nobody migrates and nobody can
remove a field from.

### Where it is persisted, and where it is not

`provider_metadata` is never persisted as a blob. `model_calls` gains six
scalar columns and the table gains no JSONB.

```text
model_calls                          -- added to the table above
  provider_api     TEXT NOT NULL     -- responses|messages|chat_c.
  response_id      TEXT NULL
  request_id       TEXT NULL         -- support correlation only
  resolved_model   TEXT NULL
  cache_breakpoints_sent    SMALLINT NOT NULL DEFAULT 0
  cache_breakpoints_dropped SMALLINT NOT NULL DEFAULT 0
  INDEX (tenant_id, response_id)     -- the support lookup
```

`previous_response_id` gets no column. On every attempt after the first it
equals the previous attempt's `response_id` within the same run, which the
table already holds and which `INDEX (run_id, step_number, attempt_number)`
already orders. A column would be a denormalization maintained by hand.

There is no JSONB column for the same reason the key set is closed. A JSONB
column is a place to put keys nobody agreed on, its shape changes without a
migration, and a query against it cannot be planned. Every key here worth
keeping is worth a column, and a key not worth a column is a key that should
not have existed.

### `ModelCallRecord`

`UsageRepository.record_attempt` above takes a `ModelCallRecord` and no
document declares one. It is the `model_calls` row before it is a row.

```python
class ModelCallRecord(BaseModel):
    attempt: ModelAttempt        # attempt_id, run_id, step, number
    session_id: UUID
    tenant_id: str
    resolved: ResolvedModel      # provider, model, policy_name
    registry_version: str        # from the run's ProviderPin
    prefix_sha256: str | None
    usage: ModelUsage | None     # None when the attempt produced none
    cost: Decimal
    cost_source: CostSource
    price_id: str | None
    stop_reason: StopReason | None
    error_kind: Literal["transient", "permanent", "protocol"] | None
    metadata: ProviderMetadata
    finished_at: datetime | None
```

`registry_version` comes from the run's pin rather than from
`ResolvedModel`, because the pin is what a resumed run carries across a
worker restart and the resolution is what would be recomputed.

Flattening `metadata` into columns happens in the persistence adapter and is
the first of exactly two places in the system that read `ProviderMetadata`
at all. The second is the span builder in the telemetry section below.
Nothing in the runtime, the policy engine, the context engine, or any tool
reads it, which is what `engineering-plan.md:1207`'s "application logic must
not rely on provider-specific fields" means once it is a rule a test can
evaluate.

## Retries, and who owns them

`engineering-plan.md:1253` puts retries in the adapter.
`engineering-plan.md:1580` says "Keep retry decisions in application code, not
in provider adapters alone." The word "alone" is doing the work, and the split
it implies is the right one.

The dividing line is whether the caller has observed any output.

```python
class ModelTransientError(ModelError):
    kind: Literal["transient"] = "transient"
    retry_after: timedelta | None
    stream_had_output: bool      # decides who owns the retry
```

Before the first event reaches the caller, the adapter may retry internally:
connection resets, TLS handshake failures, HTTP 429 and 5xx received before
the stream opened, and provider overload responses. These are invisible to the
caller by construction, since nothing was emitted, so retrying them in the
adapter costs the caller nothing and keeps a large class of noise out of the
runtime. The adapter honours `Retry-After` when present, uses exponential
backoff with jitter otherwise, and caps at three internal attempts.

Once any event has been emitted, the adapter never retries. It emits
`ModelFailedEvent` with `stream_had_output=True` and stops. The caller decides,
because only the caller knows whether partial output was already streamed to a
user, whether the step is safe to repeat, and whether the run's budget and
deadline permit another attempt. Each caller-level retry is a new attempt with
a new `attempt_id` and its own `model_calls` row.

`max_attempts` is 3 and lives in application code, matching
`event-log-and-persistence.md:726`, which already sets 3 for the worker.
Section 13 states neither number, so this document states both and notes that
they are the same number for the same reason rather than by coincidence: three
attempts is where the marginal recovery rate stops justifying the marginal
cost.

Every internal adapter retry is recorded on the attempt as
`internal_retry_count` so that a provider having a bad day is visible in
telemetry rather than hidden behind eventual success.

### Timeouts

No document defines a model-call timeout. `ToolSpec.timeout_seconds` exists at
`engineering-plan.md:837` and has no counterpart here, which means today a
hung provider connection stalls a run until the worker's own deadline fires,
if it has one. Two timeouts close that:

```python
class ModelRequest(BaseModel):
    # ... fields from Section 10.1 ...
    timeout_seconds: float = 600.0     # whole call, first byte to last
    stream_idle_seconds: float = 60.0  # gap between two events
```

The idle timeout is the one that matters. A total timeout large enough for a
long reasoning turn is also large enough to sit on a dead socket for ten
minutes; a 60-second gap between events, by contrast, is abnormal on both
providers even during extended thinking. Exceeding either produces
`ModelTransientError` with `stream_had_output` set according to whether
anything was emitted, which routes it through the ownership split above
without a special case.

A cancelled call, whether from the deadline, a user cancellation, or worker
shutdown, produces `StopReason.CANCELLED` on a partial turn rather than an
error. Cancellation is not failure and the distinction matters for the
retry-eligibility question the caller is about to ask.

## Reasoning, and the trust problem inside it

ADR-0012 names three representations of model reasoning and the gateway has to
handle all three without letting any of them become privileged text.

Native structured reasoning is Anthropic's thinking blocks and OpenAI's
reasoning items. Both arrive as first-class stream items, both carry opaque
continuation state, and both map to `ReasoningDeltaEvent` for display plus a
`ProviderReasoningItem` for continuation. In-band reasoning is ADR-0012's
`<think>` text from open models, handled by the scrubber above. The third case
is a model that does neither, where `ReasoningSupport.NONE` in the capability
set tells the context engine not to request thinking parameters at all.

Section 12.3's replay requirement and Section 20's trajectory export both
distinguish "the reasoning we display" from "the reasoning we persist". The
rule is that raw reasoning text is never persisted to the event log. It
streams to the transport for display, it may be shown in a UI, and it is
dropped. What persists is `ProviderReasoningItem.provider_payload`, which is
opaque bytes we round-trip and never read.

```python
class ProviderReasoningItem(BaseModel):
    kind: Literal["provider_reasoning"] = "provider_reasoning"
    item_index: int
    provider: str                # payload is only valid for this one
    provider_payload: dict[str, Any]  # opaque; never parsed by us
    token_count: int | None
    trust_level: TrustLevel = TrustLevel.PLATFORM
```

This supersedes the `ProviderReasoningItem` declared in Section 6.6, which
predates the adapters. `opaque_payload` becomes `provider_payload`, which
is the same bytes under a name that says whose they are. `kind` is added
because Section 6.6 names this item in a union whose five other members it
never declares, this document declares those five above with a `kind`
discriminator each, and a union member without one does not deserialize
alongside members that have one. `item_index` is added for the reason
`AssistantMessage` and `ToolCallItem` above carry one: provider output is
ordered and the order has to survive a round trip. `token_count` is added
because `ModelPricing.reasoning_priced_separately` exists and Section 6.5's
cost precedence has nothing to attribute reasoning tokens to otherwise.
`provider` and `trust_level` are unchanged, and the plan's rules for
provider-opaque items at `engineering-plan.md:594` — store verbatim, never
log or summarize or place in long-term memory, carry only for the life of
the active tool loop, drop on a provider switch — govern the renamed field
without change. The rename is recorded as an open question rather than
fixed by editing a plan sentence, the same way the `ModelCapabilities`
renames are.

### The PLATFORM default is a privilege inversion, and it is bounded here

`engineering-plan.md:592` defaults `ProviderReasoningItem.trust_level` to
`TrustLevel.PLATFORM`. That is the highest trust tier in the system, and
`policy-and-approvals.md:827-856` maps trust tiers to policy restrictiveness,
so on its face this hands model-generated content the same standing as
platform configuration. That is backwards: reasoning is model output, and
`AssistantMessage` correctly defaults to `TrustLevel.EXTERNAL_UNTRUSTED`.

Changing the plan's default is not this document's call. What this document
can do is make the label unable to cause harm, by fixing four properties of
the payload:

1. `provider_payload` is never parsed. The gateway treats it as bytes to
   round-trip. No field of it is read, matched, or branched on.
2. It is never rendered as prompt text. It is attached to the provider
   request in the provider's own continuation slot, not concatenated into any
   message body.
3. It never reaches the policy engine. `ProposedAction` is built from
   `ToolCallItem` only, and a reasoning item cannot become a proposed action.
4. It is never surfaced to a user-facing renderer as trusted content, and
   never written to memory. `memory-formation-and-consolidation.md`'s intake
   takes conversation items, and reasoning items are excluded from that set.

With those four properties, the trust label on a reasoning item has no
consumer that could act on it, which reduces the inversion from a live
privilege-escalation path to a naming defect. The naming defect should still
be fixed. That is recorded as an open question for review rather than a
unilateral edit to a plan sentence, per this document's constraints.

Note the asymmetry that makes this safe: the display text is untrusted and
non-persisted, and the persisted payload is opaque and never interpreted.
Neither half is both readable and privileged.

## Conversation invariants the gateway enforces

Section 10.4 specifies the turn shape and does not say what the gateway
rejects. Several other documents depend on it rejecting things.
`policy-and-approvals.md`'s denial-as-tool-result requires that every tool call
be answerable by a tool result; `context-engine.md:388-392` requires that a
call and its result never be separated by compaction. Both assume a pairing
invariant that no document states. The gateway states and enforces it, because
it is the last thing to touch the message list before it becomes a provider
request:

1. Every `ToolCallItem` in the history is followed, before the next
   `AssistantMessage`, by a `ToolResultItem` with a matching `call_id`. A
   dangling call is a `ModelProtocolError` raised before the request is sent,
   not a provider error discovered after we have paid for it.
2. Every `ToolResultItem.call_id` matches a preceding `ToolCallItem`. An
   orphan result is the same error.
3. `call_id` values are preserved exactly as the provider emitted them, never
   regenerated. Milestone 3's "Tool-call IDs are preserved correctly" is
   tested here.
4. A denial result (`policy-and-approvals.md`'s field-allowlisted denial
   payload) is an ordinary `ToolResultItem` with `is_error=True`. The gateway
   does not know it is a denial and must not: that is what keeps the denial
   path from acquiring a special case in the provider adapters.
5. Trust labels on history items are preserved through translation. A provider
   request has no field for them, so they are carried on the neutral items and
   used by the context engine and the policy engine, not sent.

Checking pairing before the request rather than after is the whole point. A
malformed history is cheap to reject and expensive to send.

## Events and telemetry

Two event payloads are consumed by `context-engine.md` and defined nowhere.
Both are emitted by the gateway and specified here.

```python
class ModelRequestStarted(BaseModel):
    attempt_id: UUID
    run_id: UUID
    session_id: UUID
    step_number: int
    attempt_number: int
    provider: str
    model: str
    model_policy: str
    registry_version: str
    prefix_sha256: str | None    # context-engine.md:127-135
    prefix_epoch: int            # context-engine.md:159-173
    input_token_estimate: int    # the plan's estimate, pre-call
    cache_breakpoints_sent: int
    cache_breakpoints_dropped: int
```

```python
class ModelResponseCompleted(BaseModel):
    attempt_id: UUID
    run_id: UUID
    step_number: int
    usage: ModelUsage            # authoritative; the terminal figures
    stop_reason: StopReason
    internal_retry_count: int
    duration_ms: int
    time_to_first_event_ms: int | None
```

A failed attempt emits `model.response.failed` carrying the same identifiers
plus the `ModelError` and whatever partial usage the provider reported. It is
a separate event rather than a status field on the completed event so that
subscribers counting successful attempts do not have to filter.

Section 19's telemetry attributes (`engineering-plan.md:2127-2136`) omit the
cached and reasoning token classes. The gateway's spans add
`gen_ai.usage.cached_input_tokens`, `gen_ai.usage.cache_write_tokens` and
`gen_ai.usage.reasoning_tokens` alongside the attributes already listed, plus
`veetbot.model.attempt_number` and `veetbot.model.internal_retries`. The span
name is `model.attempt`, one span per attempt, nested under the step span, so
that a step with three attempts reads as three spans rather than one long one
with a confusing duration.

Six metrics:

| Metric | Type | Purpose |
| --- | --- | --- |
| `model.attempts` | counter | by provider, model, stop_reason |
| `model.failures` | counter | by provider, error_kind |
| `model.ttfe_ms` | histogram | time to first event |
| `model.cached_ratio` | histogram | cached over input tokens |
| `model.cost` | counter | by tenant, provider, model |
| `model.internal_retries` | counter | adapter-level retries |

`model.cached_ratio` is the gateway's contribution to the context engine's
cached-prefix metric. The gateway reports the ratio per attempt; the context
engine interprets it per session, which is the level at which the
prefix-stability invariant is actually stated.

LISTEN/NOTIFY carries token deltas, reasoning deltas and provisional usage
(ADR-0010 at `0010-live-event-transport.md:24-25`). The gateway publishes
normalized events to that transport unchanged apart from one filter:
`ReasoningDeltaEvent` is published only when the subscribing session has
reasoning display enabled, because reasoning text is the largest volume and
the least often wanted.

## Ports, adapters, and what may import what

The gateway is one module with one outbound port and one inbound surface.

```python
class ModelProvider(Protocol):
    name: str                    # adapter key, matches ResolvedModel

    def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]: ...

    async def close(self) -> None: ...
```

This supersedes the `ModelProvider` declared in Section 7, which predates
the router. `provider_name` becomes `name` and matches the adapter key on
`ResolvedModel`; `stream` gains the `ResolvedModel` and `ModelAttempt` the
router has already produced, so no adapter resolves a model twice; `close`
is added because a pooled HTTP client needs an owner; and `capabilities`
moves to `ModelRouter`, because two models behind one provider can differ
in context window and in tool support — a capability is a property of the
resolved model, not of the adapter. Nothing else in Section 7 changes, and
the rule that the iterator ends with exactly one completed or failed event
is carried below as a contract-suite assertion.

Streaming is the only method. A non-streaming call is a streaming call whose
events are collected, and offering both would give adapters two code paths to
keep in agreement, which is exactly the divergence the contract suite exists
to prevent. The convenience wrapper lives in the gateway:

```python
class ModelGateway:
    async def complete(
        self, request: ModelRequest
    ) -> ModelTurn: ...

    def stream(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelEvent]: ...
```

`complete` runs `stream` through the shared assembler. Both resolve the model,
apply the pin, enforce the conversation invariants, record the attempt, emit
the two events, and translate errors. Adapters do none of that.

Import boundaries, enforced by the boundary tests Section 5 requires:

| Module | May import | Must not import |
| --- | --- | --- |
| `agent_core.model` | domain types only | any provider SDK |
| `adapters.openai` | `openai`, core types | `anthropic`, runtime |
| `adapters.anthropic` | `anthropic`, core types | `openai`, runtime |
| `adapters.chat_completions` | `httpx`, core types | either vendor SDK |
| runtime | `agent_core.model` | any adapter package |

Milestone 1's "No provider-specific code exists in the runtime" and Milestone
3's "The OpenAI SDK is imported only in the OpenAI adapter" are both this
table, tested rather than asserted. The test walks the import graph rather
than grepping, because a transitive import through a shared helper is exactly
the failure that grep misses.

### The four adapters of Milestone 3

Section 2.3's provider list at `engineering-plan.md:157-161` is controlling
where the later list disagrees: OpenAI, Anthropic, and an OpenAI-compatible
`chat_completions` endpoint, plus the fake. Milestone 3
(`engineering-plan.md:2551`) requires "the same contract suite against OpenAI,
Anthropic, and a chat_completions endpoint", while `engineering-plan.md:2295`
names only OpenAI fixtures. The suite runs against all three plus the fake and
the recorded adapter; that fixture asymmetry is an incomplete enumeration, not
a narrower requirement, and this document resolves it in favour of the
acceptance criterion.

The `chat_completions` adapter is where the awkward cases live. It carries the
in-band `<think>` scrubber, ADR-0012's XML `<tool_call>` parser for models
without native tool calling, and no cache control. It exists because it makes
a local Ollama endpoint a first-class test target, which is the no-cost live
test path Milestone 3 asks for.

The OpenAI Responses adapter owns three wire-only compatibility translations.
Canonical tool names retain the platform's dotted namespace everywhere inside
the system, but the adapter replaces a name that violates OpenAI's function-name
grammar with a deterministic, collision-checked alias containing a readable
stem and a SHA-256 suffix; returned calls are mapped back before leaving the
adapter. Function schemas are sent with provider strict mode disabled because
the platform permits optional arguments and applies its own JSON Schema
validation before policy or execution. The adapter also omits `temperature`:
the neutral request keeps the field for providers that accept it, while the
configured OpenAI reasoning model rejects it. These translations change no
tool identity, authorization, argument-validation, or replay semantics outside
the provider boundary.

### The fake and the recorded adapters

`engineering-plan.md:1216-1224` uses `FakeModelScript`, `ToolCallTurn` and
`FinalTurn` at a call site and never defines them.

```python
class ScriptedTurn(BaseModel):
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ScriptedToolCall] = []
    stop_reason: StopReason = StopReason.END_TURN
    usage: ModelUsage | None = None    # None: synthesized from text
    fail_with: ModelError | None = None
    delay_ms: int = 0                  # to exercise idle timeouts

class ScriptedToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] | str    # str emits raw, possibly bad
    call_id: str | None = None         # None: deterministic generation

class FakeModelScript(BaseModel):
    turns: list[ScriptedTurn]
    on_exhausted: Literal["error", "repeat_last"] = "error"
```

`ToolCallTurn` and `FinalTurn` in the plan are constructor helpers over
`ScriptedTurn`, retained as functions so the plan's example code still reads
correctly. `arguments` accepting a raw string is what lets the suite test the
malformed-JSON path deterministically.

The recorded adapter replays captured provider streams from fixture files. A
fixture is the raw provider event sequence, captured once against the live
API, with credentials and identifiers redacted at capture time. Replaying raw
provider events rather than normalized ones is deliberate: the thing under
test is the adapter's translation, and replaying normalized events would test
nothing.

## Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Provider 429 before stream | HTTP status | adapter retries, backoff |
| Provider 5xx mid-stream | stream error | fail, caller decides |
| Stream stalls | idle timeout | transient, caller decides |
| Malformed tool JSON | assembler parse | error result to the model |
| Dangling tool call | pre-send check | protocol error, no request |
| Unknown stream event | adapter default | log once, ignore, continue |
| `server_tool_use` block | adapter | protocol error, fail attempt |
| Sequence gap | validator | protocol error; adapter bug |
| Two terminal events | validator | protocol error; adapter bug |
| Model policy unresolvable | router | permanent error, fail run |
| Credential missing | router | permanent error, no retry |
| Budget exhausted pre-call | gateway | fail run, BUDGET_EXCEEDED |
| Context over window | pre-send check | fail; engine should have cut |
| Provider down, run pinned | adapter | fail run; no mid-run switch |

Unknown stream events are ignored rather than fatal, and that is the one
asymmetry in the table worth defending. Providers add event types without
warning and a strict adapter would break on a vendor deploy we did not do. The
"log once" is per process per event type, so a new event type is visible in
telemetry the day it appears without producing one log line per token.

## Hard gates

Milestone 3 does not pass until every one of these holds.

1.  The import-boundary test passes: no provider SDK is reachable from the
    runtime, and neither vendor SDK is reachable from the other's adapter.
    **M3.**
2.  The contract suite passes identically against fake, recorded, OpenAI,
    Anthropic and `chat_completions`. Identical means the assembled
    `ModelTurn` matches on content, tool calls, `call_id` values and
    `stop_reason`; usage and cost may differ. **M3.**
3.  Tool-call ids round-trip byte-for-byte through a two-step tool loop on
    every adapter. **M3.**
4.  A stream that violates any of the six invariants is rejected by the
    validator, with a test per invariant. **M3.**
5.  No API key, authorization header, or raw provider error body appears
    in any log line, event payload, span attribute or persisted row.
    Tested by a scanner over captured output, not by inspection. **M3.**
6.  Malformed tool arguments produce an error tool result and the loop
    continues, on every adapter. **M3.**
7.  A killed and resumed run keeps its provider pin and its continuation
    payload, and does not switch providers. **M3.**
8.  Cost is recorded for failed attempts, and a run whose retries exhaust
    its budget stops with `BUDGET_EXCEEDED`. **M3.**
9.  The Ollama calculator scenario passes with no network cost. **M3.**
10. A live one-call smoke test against each vendor passes when
    credentials are present and skips cleanly when they are not. **M3.**
11. Every key any adapter writes into `provider_metadata` is a declared
    field of `ProviderMetadata`, and `provider_metadata` is read at
    exactly two call sites: the persistence adapter's flattening function
    and the span builder. An adapter that writes an undeclared key fails
    the check. Registered as `gate.model.metadata_closed`. **M3.**
12. Every provider profile in the repository loads, and every member of a
    corpus of intentionally invalid profiles is rejected naming the rule
    it broke. The corpus carries one member per row of the loader's rule
    table. Registered as `gate.model.profile_valid`. **M3.**

## Build order

1. Neutral types: content parts, conversation items, events, usage, errors.
2. The stream validator and the six invariants, with tests, before any
   adapter exists.
3. The assembler, tested against hand-written event sequences.
4. The fake adapter and `FakeModelScript`.
5. The contract suite, written against the fake, before real adapters.
6. `ModelRouter`, the registry file format, `ResolvedModel` and pinning.
7. The OpenAI adapter, to the contract suite.
8. The Anthropic adapter, to the same suite unchanged.
9. Cache-hint translation and the breakpoint budget.
10. `model_calls`, `model_prices`, `UsageRepository`, cost computation.
11. The recorded adapter and captured fixtures for both vendors.
12. The `chat_completions` adapter, the `<think>` scrubber and the XML
    tool-call parser.
13. Events, spans and metrics.
14. The twelve hard gates as CI checks.

Steps 2 and 5 before step 7 is the whole discipline of this module. Writing
the contract suite against the fake first is what stops it from being
accidentally shaped around the first real provider's behaviour, which is the
usual way a provider-neutral protocol quietly becomes an OpenAI protocol with
extra steps.

## Decisions

1. The normalized stream obeys six invariants, enforced by a shared validator,
   and a violation is an adapter bug rather than a caller concern.
2. `UsageEvent` is advisory and provisional; `ModelCompletedEvent.turn.usage`
   is authoritative and always supersedes it.
3. One shared assembler folds events into turns for every adapter. Adapters
   emit events only.
4. Malformed tool arguments become an error tool result, not a failed run.
   `raw_arguments` is always retained.
5. `server_tool_use` is a protocol error in 0.1, because a provider-executed
   tool bypasses the policy engine.
6. In-band `<think>` is scrubbed by a streaming state machine in the
   `chat_completions` adapter, with a per-profile configurable tag pair.
7. `cache_creation_input_tokens` becomes a fifth tracked token class,
   `cache_write_input_tokens`, on both `ModelUsage` and `RunUsage`.
8. `reasoning_tokens` is `None` when a provider does not report it
   separately, and pricing carries `reasoning_priced_separately` per model.
9. The context engine owns cache-boundary decisions; the gateway only
   translates hints and reports the resulting token counts.
10. When hints exceed the provider's breakpoint budget the earliest are kept
    and the drop is recorded on the attempt.
11. A `ModelRouter` port turns `model_policy` into a `ResolvedModel` carrying
    capabilities, limits, pricing and a credential reference.
12. Provider selection happens once at run start; the pin is absolute and
    persisted for the life of the run. Milestone 10 routes selection, never
    live runs.
13. Adapters retry only before the first event is emitted, at most three
    times. After any output, the caller owns the retry. `max_attempts = 3`
    lives in application code.
14. `ModelRequest` gains `timeout_seconds` and `stream_idle_seconds`.
    Cancellation produces `StopReason.CANCELLED`, not an error.
15. Raw reasoning text is never persisted; opaque continuation payload is
    persisted and never parsed.
16. The `PLATFORM` default on `ProviderReasoningItem.trust_level` is bounded
    by four properties that leave it no consumer able to act on it.
17. The gateway enforces tool call and tool result pairing before sending a
    request, which is what the denial-as-tool-result and compaction-atomicity
    rules elsewhere depend on.
18. `model_calls` and `model_prices` are added to the schema; `runs.usage`
    becomes a rollup maintained in the same transaction.
19. Failed attempts count against budget, and budget is checked before an
    attempt using usage that includes them.
20. The contract suite runs against fake, recorded, OpenAI, Anthropic and
    `chat_completions`, and is written against the fake first.
21. A provider profile is one YAML document per provider under
    `models/providers/`. `policies.yaml` keeps model policies and the
    enabled list, and `catalog.yaml` becomes the import target rather
    than a second place models are declared.
22. Every adapter carries a capability ceiling. A profile may narrow it
    and may never widen it; widening fails the load rather than being
    intersected away.
23. `registry_version` is
    `{profile}@{profile_sha256[:12]}+r{registry_sha256[:8]}`, mirroring
    `policy_version` so that a pin names both what it resolved to and
    what the router was choosing among.
24. `provider_metadata` carries a closed set of seven keys produced by a
    frozen `ProviderMetadata`. Each persisted key is a column on
    `model_calls`, the table gains no JSONB, and exactly two call sites
    read the metadata at all.
25. `Capability`, `CapabilitySet`, `ReasoningSupport`, and
    `ModelCallRecord` are declared here; each was referenced by this
    document or by the plan and defined nowhere.

## Open questions for review

These are decisions taken to keep the plan moving. Each is recorded in
`docs/status/questions-for-review.md` and each is cheap to reverse.

1. Should `ProviderReasoningItem.trust_level` default to
   `EXTERNAL_UNTRUSTED` rather than `PLATFORM`? This document bounds the
   inversion but does not fix it, because fixing it means editing a plan
   sentence. The bounding makes it safe; the name is still misleading.
2. Is refusing `server_tool_use` outright the right call, or should there be
   a configuration flag that permits it for a specific tool with a policy
   mapping? Refusing is safe and cheap to relax later.
3. Should the model registry be a database table rather than a hashed
   configuration file? A file matches the policy profile treatment and keeps
   0.1 simple; a table would allow per-tenant model catalogues sooner.
4. Is a 60-second stream idle timeout right for extended-thinking turns on
   both vendors? It is comfortable today and worth revisiting once real
   traces exist.
5. Should reasoning display be per-session as specified here, or per-tenant
   policy? Per-session is more flexible and slightly more work.
6. Should `ModelCapabilities.tool_calling` be renamed in the plan itself to
   `native_tool_calling`, and `vision` to `images`? This document reconciles
   the two declarations and cannot edit the plan's. The reconciliation table
   makes the divergence readable; it does not make it go away.
7. Is one file per provider profile right, given that
   `bootstrap-and-composition.md:360` describes a single `models/policies.yaml`
   holding both policies and profiles? One file per profile is what ADR-0012's
   "without editing core" requires of an overlay, and merging the two back is
   a compatible change in the other direction.
8. Should `request_id` be dropped from `model_calls` after a retention
   window? It is the one column whose only consumer is a vendor support
   ticket, and those have a shelf life measured in weeks.
9. Should `ProviderReasoningItem.opaque_payload` be renamed in the plan
   itself to `provider_payload`? This is question 6 again for a different
   type, and the same answer should cover both. The field is declared once
   in the plan and named nowhere else in the corpus, so the edit is one
   line, and until it happens the divergence is reconciled only here.
