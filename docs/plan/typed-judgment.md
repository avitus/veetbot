---
title: Typed Judgment
status: design
canonical: true
---

# The typed-judgment port and the TypeSafe adapter

This document specifies the typed-judgment port admitted under Milestone 29.
The engineering plan states the requirement; this document states the
mechanism. It is subordinate to [engineering-plan.md](engineering-plan.md),
and [ADR-0110](../adr/0110-typed-judgment-port-and-typesafe-jev.md) records
the architectural decisions, the vendor, the data the vendor may receive, and
the owner's authorization.

Several places in the platform make a narrow judgment — which of these
folders does this conversation belong in, does this text carry personal data
— and today each one either compares strings or spends a generative model
call and then parses, grounds, and second-guesses the prose that comes back.
A typed-judgment provider answers such a question directly: the caller sends
structured state and a closed set of typed questions, and receives a
probability, a choice from the offered options, or a position on an ordered
scale. It generates no text. The port makes that capability available behind
one provider-neutral interface, off by default, with a deterministic fallback
named by every consumer.

The port is not the model gateway. `ModelProvider` streams generative output
and tool calls under a routed model policy; a judgment provider returns typed
distributions and resolves no policy. Nothing here registers a model profile,
and dynamic model routing stays on the roadmap as item B2.

## Scope

In scope: the port and its value types, one vendor adapter, one scripted fake,
the selector that composes them, and the rules every consumer follows. The
first consumer is the add-to-folder half of chat thread grouping
([thread-folders.md](thread-folders.md)). ADR-0110 also admits an offline,
non-activating email-importance evaluation as an evaluation-only consumer.

Out of scope, each for a reason:

1. **Text generation.** A judgment provider names nothing and summarizes
   nothing. A consumer that needs a folder name, a summary, or a timestamp
   keeps its existing source for it.
2. **A second vendor.** One adapter ships. The port is neutral so that a
   second costs an adapter and a contract-suite subject, not a redesign.
3. **Autonomous action.** No answer from the port authorizes, files, sends, or
   deletes anything. Consumers turn answers into proposals or into a more
   restrictive decision, never into a less restrictive one.
4. **Any further consumer.** Each new consumer needs its own authorization and
   names its data classes and its fallback. The policy advisory layer in
   particular is roadmap item B8 and enters only through its own policy ADR.

## The port

```python
class JudgmentProvider(Protocol):
    name: str

    async def judge(self, request: JudgmentRequest) -> JudgmentResult: ...

    async def close(self) -> None: ...
```

The port lives in `agent_core.ports.judgment` and exposes only domain types.
Its contract suite is `tests/contract/test_judgment_provider_contract.py`,
run against every shipped provider and the fake through one census tuple, the
pattern `tests/contract/README.md` prescribes.

## Value types

All are frozen and live in `agent_core.domain.judgment`.

- **`NoulQuestion`** — `instructions` and optional `true_when` and `false_when`
  criteria, given together or not at all. The answer is the probability, from
  0 to 1, that the statement holds. A value near one half means the provider
  finds yes and no similarly likely; it is not a medium intensity.
- **`ChoiceQuestion`** — `instructions` and between two and sixteen
  `ChoiceOption`s, each an opaque key, a description, and optional examples.
  The answer is the chosen key, the probability of every key, and a
  confidence. A consumer that may have no match offers an explicit no-match
  option rather than thresholding a forced choice.
- **`ScoreQuestion`** — `instructions` and between two and eight ordered level
  descriptions. The answer is a position from 0, the first level, to the index
  of the last, the probability of each level in order, and a confidence. A
  score is a threshold signal; consumers do not interpolate magnitudes from it.
- **`JudgmentRequest`** — JSON-serializable `state` and between one and eight
  questions keyed by identifiers matching `^[a-z][a-z0-9_]{0,31}$`. Questions
  in one request are answered independently over the same state, so a
  consumer asks everything it may need about one state in one request.
- **`JudgmentResult`** — the answers by question identifier and one
  `ModelUsage`. A pure `validate_result` checks that every question has an
  answer of its own type, every chosen key was offered, and every probability
  is in range; the adapter and the fake both run it.

Question identifiers and option keys are for code. They are never sent as
meaning: the instructions and descriptions carry the whole question.

## Failures

One error type leaves the port: `JudgmentProviderError(reason, retryable)`.
Its message is the reason code and nothing else.

| Reason | Meaning | Retryable |
| --- | --- | --- |
| `judgment.auth_failed` | The credential is missing, or the vendor refused it | no |
| `judgment.request_rejected` | The vendor refused the request as malformed | no |
| `judgment.payment_required` | The vendor refused the request for billing: no credits, or no active plan | no |
| `judgment.rate_limited` | The vendor's rate limit held through every attempt | yes |
| `judgment.provider_unavailable` | Timeout, transport failure, or a server error through every attempt | yes |
| `judgment.output_invalid` | Oversize, non-JSON, or schema-invalid response, an unknown choice, or an out-of-range probability | no |
| `judgment.input_too_large` | The encoded request exceeds the request ceiling; nothing was dialed | no |

Every consumer catches this error and takes its named deterministic fallback.
No consumer may fail a run, a pass, or a startup because the port failed.

## The TypeSafe adapter

`agent_core.adapters.judgment.typesafe.TypeSafeJudgmentProvider` speaks raw
HTTP through `httpx`; no vendor SDK is admitted, the rule the web providers
already follow.

- **Fixed egress.** The adapter dials exactly one HTTPS endpoint, the vendor's
  documented System One endpoint, held as a module constant, and follows no
  redirect. No setting, request field, or response changes the destination.
  This is the fixed-endpoint arrangement web-access.md:162-167 describes: the
  composition root selects the adapter and the adapter hard-codes its host.
  The egress policy governs sandbox traffic and tenant-supplied URLs
  (sandbox-isolation.md:805-816) and gains no entry.
- **Call-time credential.** `TYPESAFE_API_KEY` enters the existing credential
  broker as the reference `typesafe`, as web-access.md:140-146 describes for
  the web providers. The adapter resolves it on every call, sends it only as
  the bearer authorization header of that one request, and holds it on no
  object. A resolver refusal is `judgment.auth_failed` with nothing dialed.
- **Bounds.** The encoded request is at most 64 KiB, checked before dialing.
  The response is streamed and refused past 256 KiB. One attempt has four
  seconds; a call makes at most three attempts inside ten seconds overall.
- **Retries.** Only a timeout, a transport failure, and statuses 408, 425,
  429, and 500 and above — which includes the vendor's 529 overload — are
  retried, after fixed delays of 0.25 and 0.5 seconds taken from the injected
  clock. There is no jitter: ambient randomness is not available to adapters.
- **Sanitization.** A non-success response is classified by status alone and
  its body is never read. Status 402 is `judgment.payment_required`, apart
  from the malformed-request code, because its remedy is the owner's billing
  page and not a code change, and it is not retried. Every failure is raised
  without an exception chain, because validation errors quote their input.
  The model name returned by the
  vendor is kept only when it matches `^[A-Za-z0-9._-]{1,64}$`; otherwise the
  requested alias stands in, because that string reaches audits. Nothing logs
  state, criteria, a body, or a header.

On the wire the adapter follows the vendor's documented shapes: a Noul's
criteria are its `true` and `false` entries, a Choice option with examples is
sent as a structured description of `what` and `examples`, and a Score's
probabilities arrive keyed by level index and are returned in level order. An
answer whose type, keys, or level indexes do not match the question asked is
`judgment.output_invalid`.

`FakeJudgmentProvider` is scripted, records every request it receives, and
runs the same `validate_result`, so a consumer test can assert exactly what
would have left the process.

## Usage and cost

The vendor returns token counts and no cost. The adapter reports a
`ModelUsage` whose cost is the input-token count at the pinned price — USD
0.042 per million input tokens, output free, as published on 2026-09-19 —
with `docs_snapshot` as its cost source, and the provider and model it used.
Judgment calls are not model-gateway calls: they write no `model_calls` row
and draw on no run budget. Each consumer owns a ceiling and records its own
audit.

## Configuration

`JUDGMENT_PROVIDER` selects the provider and accepts `disabled` and
`typesafe`; it defaults to `disabled` and an unknown value is a configuration
error at load. It is an environment value by the corpus's own test
(bootstrap-and-composition.md:338-340): whether a deployment holds a vendor
account differs between two deployments of one revision. It appears in
`.env.example` and the production template with `TYPESAFE_API_KEY` left empty,
and in neither the schedule nor the notification worker's template, because
those roles refuse to start with a provider key in their environment.

- **A credential enables nothing.** With the selector unset or `disabled`, no
  provider is composed, no consumer dials, and every consumer behaves exactly
  as it does in a build without the port.
- **A selector enables no consumer.** Each consumer has its own switch in its
  own checked-in profile, default off.
- **A missing key degrades and never refuses startup.** With the selector on
  and no credential, composition logs one warning that names variables only,
  and every call fails `judgment.auth_failed` without dialing, so each
  consumer falls back.

## What may be sent

ADR-0110 is the authority and records the owner's acceptance of the vendor's
terms. The classes admitted are: chat conversation titles, first-message
snippets of at most 400 characters, folder names, and member titles, for
folder matching; and, for the offline email evaluation only, the owner's
frozen evaluation corpus with the evidence the production assessor sees.

Never sent, by any consumer: credentials or credential-shaped values; memory
statements above `INTERNAL` sensitivity, the limit the memory providers
already observe; hardline rules and policy profile contents; drafts and style
examples; session metadata; and principal or tenant identifiers. Hazard
scans run before egress, and a consumer drops rather than masks a hazardous
value, because a placeholder is noise to a classifier.

A judgment provider can be steered by instructions planted in the state it
judges. Consumers therefore keep instructions and criteria platform-authored,
confine untrusted text to named state fields, and use answers only where a
steered answer costs a bad proposal or an unnecessary escalation.

## Hard gates

1. **Egress is fixed and the credential is resolved at call time.** The
   TypeSafe adapter dials exactly one hard-coded HTTPS endpoint and follows
   no redirect; it resolves the `typesafe` credential through the credential
   resolver on every call, sends it only as that request's authorization
   header, and holds it on no object; and no setting, request field, or
   response changes the destination. Registered as
   `gate.judgment.fixed_egress`, case. **M29.**
2. **The port is off until selected, and a credential alone enables
   nothing.** With `JUDGMENT_PROVIDER` unset or `disabled` no provider is
   composed and no consumer dials; an unknown selector value fails at load;
   and with the selector on and the credential missing, startup succeeds,
   one warning names variables only, and every call fails
   `judgment.auth_failed` without dialing. Registered as
   `gate.judgment.default_off`, case. **M29.**
3. **Failures are typed, bounded, and content-free.** Every adapter failure
   is a `JudgmentProviderError` carrying one of seven reason codes and nothing
   else; an oversize request fails without dialing; an oversize, non-JSON, or
   schema-invalid response, an unknown choice, and an out-of-range
   probability are `judgment.output_invalid`; retries are bounded in count
   and total time and limited to transient failures; and no exception, log
   line, or event carries request state, a response body, or the credential.
   Registered as `gate.judgment.typed_failure`, case. **M29.**
4. **Usage is priced locally and one contract binds every provider.** Cost is
   the returned input-token count at the pinned, dated price with
   `docs_snapshot` as its source; the shipped adapter and the fake pass one
   contract suite through the provider census; and `validate_result` rejects
   an answer of the wrong type, a choice that was not offered, and a
   probability out of range on both. Registered as
   `gate.judgment.priced_contract`, property. **M29.**

## Tracked metrics

- **Fallback share by consumer** — calls that ended in the consumer's
  deterministic fallback, by reason code. A rising share means the vendor or
  the credential is failing.
- **Tokens and cost by consumer** — against each consumer's ceiling.
- **Acceptance of what the port proposed** — for folder matching, the share
  of judgment-derived proposals the owner accepts, against the lexical and
  generative shares. It is the number that says whether the port earns its
  place.

## Build sequence

1. The domain types, the port, the fake, the census, and the contract suite
   over importable skeletons. The first red test is the Choice contract case
   against the vendor adapter.
2. The TypeSafe adapter: wire shape, bounds, retries, sanitization, pricing.
   Gates 1, 3, and 4.
3. The selector, composition, the templates, and the degrade-on-missing-key
   behavior. Gate 2.
4. The first consumer, specified in [thread-folders.md](thread-folders.md).

## Decisions

1. **A new port, not a model-gateway adapter.** The gateway's provider
   protocol is a stream of generative events under a routed policy, and its
   wire shapes are a closed set. A typed distribution is a different contract,
   and forcing it through the gateway would make model routing — which is
   not authorized — the mechanism of a feature that needs none.
2. **Raw HTTP, not the vendor SDK.** The house rule admits two vendor SDKs,
   both confined to the model adapters. One POST does not justify a third.
3. **All three question types ship together.** The folder consumer needs
   Choice; the admitted evaluation needs Score and Noul. One contract suite
   over all three is cheaper than reopening it twice.
4. **No jitter.** Determinism rules forbid ambient randomness, the retry
   count is three, and the callers are background passes; fixed delays are
   enough.
5. **Judgment calls stay out of the model ledger.** They are not model calls,
   their cost is orders of magnitude smaller, and consumers already own the
   audit events that carry their usage.

## Implementation checkpoint: 2026-09-20

Build steps 1 through 3 landed, and the offline email-importance replay that
ADR-0110 admits as an evaluation-only consumer landed the same day in
`agent_core.evals.email_importance_replay`; it obtains its provider from the
composition and no production module imports it. The domain types, the port, the TypeSafe
adapter, the scripted fake, and the production census bind under
`tests/contract/test_judgment_provider_contract.py`; the selector and the
composition bind under `tests/unit/test_judgment_composition.py`; and
`tests/gates/test_judgment_m29.py` gives each of the four gates one check. No
consumer exists yet, so no judgment request is made in any deployment. A live
round trip against the vendor is an opt-in test and is not gate evidence.
Registration and local checks are not release evidence: exact-head hosted CI,
review, and production delivery remain open items in project state.

## Implementation checkpoint: 2026-09-21

The first production day showed a rejected call could not be told from a
malformed one: a key whose account had no credits returned status 402, which
read `judgment.request_rejected`. It now reads `judgment.payment_required`,
the seventh reason code, bound by the status table of
`tests/contract/test_judgment_provider_contract.py`. Each consumer already
records the reason code of a failed call — the folder pass in
`judgment_error_class`, and the advisory composite as its abstention cause,
which previously held only the error's class name.

## Open questions

1. Whether the vendor accepts a pinned model version as well as the floating
   alias. The adapter requests the alias and records the version returned;
   pinning can follow a verified answer.
2. Whether a verdict cache is worth its invalidation rules. Folder matching
   re-judges an unfiled conversation on each pass while proposal slots are
   free; the cost is small and the cache is deferred until a metric asks.
