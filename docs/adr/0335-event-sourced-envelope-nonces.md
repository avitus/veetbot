# ADR-0335: An item read from the event log keeps its envelope nonce at any position

- Status: Proposed (owner-requested, 2026-11-20)
- Date: 2026-11-20
- Amends: ADR-0139's note that within-run reuse ends where envelope nonces move
- Related: ADR-0007, ADR-0119, ADR-0135, ADR-0137, ADR-0139; engineering plan
  Sections 10.1 and 11
- Detailed design: `docs/plan/context-engine.md`, `docs/plan/model-gateway.md`

## Context

On 2026-11-20 the owner asked for GPT-6.2 Astra as a chat model choice and for
the integration to be prefix aware. `gpt-6.2-astra` joins the shipped OpenAI
profile as the selectable `astra-6-2` policy under ADR-0119.

OpenAI's reasoning guide, checked that day, states a rule that is new with
GPT-6.2: the Responses API removes a replayed reasoning item from the input
when content before it was inserted or edited since the request that produced
it. Deleting whole earlier items, or appending after the item, does not remove
it. The API raises no error; the model continues without that reasoning.

The envelope nonce was a digest of an item's canonical bytes and its position
in the rendered body. Two positions move between steps of a tool loop:

- The run's user message is rendered after the fresh rows while unanswered and
  before them once the model has called tools (ADR-0139).
- The runtime keeps only the latest turn's reasoning items (ADR-0007). Removing
  the spent ones shifts every later item up.

Either move changed the nonce, and so the bytes, of an item that precedes the
reasoning being replayed. A regression through the real planner, budgeted
builder and Responses translator showed it on every step: the user message at
the first, the previous tool result afterwards. On GPT-6.2 each tool-loop step
would have lost the previous step's reasoning. Earlier models have no such
rule; for them the cost was only the cache boundary ADR-0139 recorded.

## Decision

An item that carries `source_event_sequence` derives its nonce from that
sequence, the part's index and the item's canonical bytes. It renders the same
bytes wherever it is placed. An item without one, which is rendered for a
single request (planned prefix rows, recall, working state, runtime metadata,
runtime control text), keeps the positional derivation and its existing bytes.

The rule lives in the provider-neutral renderer and applies to every model.
The adapter is unchanged and no behaviour is keyed on a model name.

The trust requirement is unchanged. Each event-sourced item still has its own
nonce per part: the sequence and the canonical bytes, which include the call
id and content, separate it from every other item. The nonce remains a digest
over the content it closes, so content cannot contain it, and delimiter syntax
inside content is still escaped. The position was never secret and added no
protection.

## Consequences

- Between two steps the only changes before the replayed reasoning are whole
  items leaving. GPT-6.2 keeps the reasoning across tool-loop steps.
- Every event-sourced body item renders a new nonce once. The first request of
  a session after deployment can miss the provider cache for its history, as
  after ADR-0139. Planned prefix bytes, prefix hashes and plan epochs do not
  change; no migration is needed.
- A history cut, a tool-result truncation and compaction still rewrite earlier
  content. OpenAI names these as prefix changes; such a step can lose its
  reasoning. Stable tool-result admission (ADR-0137) already bounds a result
  once, when it is admitted, so later steps do not rewrite it.
- The history-window markers do not move. The run marker still stops before
  the latest opaque continuation, because the next step removes it.
- The registry version changes with the new model; runs pinned to the previous
  registry drain before deployment, as for any registry change.

## Verification

`tests/unit/test_active_history_cache.py` drives the real planner, builder and
Responses translator with the shipped `astra-6-2` profile over five tool
steps, with and without carried history, while recall, working state, the
date or a skill body changes. At each step every row before the replayed
reasoning item must appear, byte for byte and in order, in the request that
produced it. It failed on the first step before the change.
`tests/unit/test_envelope_nonce_stability.py` covers the renderer: equal bytes
at any position for an event-sourced item, distinct nonces per event and part,
unchanged positional nonces for items without an event, and a hostile closing
tag that stays escaped.

Not verified: no live GPT-6.2 request was made. The removal rule is taken from
OpenAI's documentation, and whether a moved cache breakpoint counts as an edit
is inferred from cache behaviour on GPT-6 Astra. OpenAI's page for
`gpt-6.2-astra` states only that reasoning effort `none` is rejected; the
profile lists the five efforts `gpt-6-astra` accepts.

## Follow-up

Read `input_transformations` from a completed response and count removed
reasoning items on the attempt, once a live response shows its shape. A
removal is otherwise invisible. A live dev probe of a tool loop on
`astra-6-2` should confirm that none is reported.

## Alternatives considered

- **Leave rendering alone and accept the removal:** rejected. The model would
  lose its reasoning at every tool step with nothing reporting it.
- **Derive every nonce from content alone:** rejected because planned prefix
  rows would change their bytes, hash and epoch for no gain, and identical
  rows rendered for one request would share a nonce.
- **Key the behaviour on the model:** rejected. A position-independent
  rendering is correct for every provider and keeps the adapter generic.
