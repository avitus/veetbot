# ADR-0132: Scheduled runs cache their prefix for an hour

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: Sections 10.1 and 11 of the engineering plan; ADR-0109, ADR-0119
- Detailed design: `docs/plan/model-gateway.md`, `docs/plan/context-engine.md`

## Context

Section 10.1 gives `CacheBreakpoint` a `ttl` of `"default"` or `"1h"`, and
`model-gateway.md` says a long agentic loop that issues many calls against the
same prefix requests the one-hour TTL, an interactive session takes the default,
and the `ContextPlan` carries the choice. None of it was built. The planner
never set a TTL, the Anthropic adapter wrote `{"type": "ephemeral"}` whatever
the hint said, and the registry priced only the five-minute write. The document
never said which session shapes count as long agentic loops.

Anthropic's caching rules decide what the choice is worth:

- An entry lives five minutes, or an hour when asked, from the **start** of the
  request that writes or reads it. A read refreshes the timer at no cost, so
  requests that start less than five minutes apart keep a five-minute entry warm
  indefinitely.
- A five-minute write costs 1.25 times the base input price and a one-hour write
  twice it. A read costs a tenth, and a fortieth (0.025) on Claude Fable 5.1.
- The one-hour TTL pays only across a gap of five to sixty minutes between
  requests sharing the prefix. Its premium is 0.75 of the prefix, paid once. Each
  such gap on the default costs a rewrite instead of a read: 1.225 of the prefix
  on Fable 5.1 and 1.15 elsewhere. One gap pays for it.
- The request renders tools, then system, then messages. An entry with a longer
  TTL must come before any entry with a shorter one, or the request fails.
- `usage.cache_creation` splits the written tokens into
  `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`.

## Decision

1. **A scheduled occurrence is the long agentic loop.** The planner gives both
   prefix breakpoints the one-hour TTL when the session carries `schedule_id`
   and is not a delegated child. Every other session takes the default:
   interactive Chat, inbound surfaces, skill review, operational Email, and
   delegated children. An occurrence is a dedicated session with exactly one
   autonomous run under the largest limits the platform grants (up to 64 model
   calls, 256 tool calls, and a day of run time). Its gaps come from machinery,
   not from a person typing. It waits on delegated children for up to
   `delegation.child_wall_seconds` (900 s) and on device invocations for up to
   300 s. It queues at async priority behind interactive work, and parks on owner
   approvals. A Fable 5.1 generation of several minutes also counts against the
   five minutes, because the timer starts when the request does.
2. **A delegated child takes the default.** It is a bounded loop of at most
   twelve model calls and fifteen minutes. It runs in a session that ends with
   it, and at `max_depth: 1` it never waits on a child of its own. The parent
   is the one that waits, and the parent's own shape decides its TTL.
3. **Run length is not a criterion.** The plan is fixed at the session's first
   model request, before anyone knows how long the run will be. And a loop whose
   calls start less than five minutes apart keeps the default entry warm for
   free, however long it runs.
4. **One TTL across the frozen prefix, and the default after it.** Both prefix
   breakpoints take the same value. The history window's markers keep the
   default in every session, because they move every step and a one-hour write
   there would pay the premium on each request. A shorter entry after a longer
   one is valid, so the plan's order holds by construction.
5. **The adapter translates, and keeps the request valid.** A one-hour hint
   becomes `{"type": "ephemeral", "ttl": "1h"}`. Walking the wire order from the
   end, each placed breakpoint takes the longest TTL placed at or after it. An
   earlier breakpoint caches a prefix of every later one, so reuse expected of
   the larger prefix is reuse of the smaller; no requested TTL is ever
   shortened. The adapter sends the one-hour TTL only when the resolved pricing
   carries a one-hour write price, and otherwise sends the default, so every
   write it causes is priced.
6. **One-hour writes are priced exactly.** The registry gains one optional
   pricing key, `cache_write_1h_per_mtok`: USD 10.00 for Claude Opus 5 and
   USD 20.00 for Claude Fable 5.1, twice their base input price. `ModelUsage`
   gains `cache_write_1h_input_tokens`, the one-hour subset of
   `cache_write_input_tokens`, read from `ephemeral_1h_input_tokens`.
   `price_usage` charges that subset at the one-hour rate and the rest at the
   five-minute rate. It refuses one-hour tokens that have no price. `RunUsage`
   is unchanged: its cost carries the price, and `model.response.completed`
   carries the split.
7. **Reservations use the dearest token the request can incur.** The People
   import reservation prices input at the one-hour write rate when a breakpoint
   asks for it. The run budget needs no change. It charges each attempt's priced
   cost after the attempt, and the delegation and synthesis reserves are fixed
   amounts rather than token estimates. Memory extraction's cost ceiling is
   unchanged, because its requests carry no cache hints.
8. **The builder version stays `context-builder@12`.** The TTL rides on the
   plan's breakpoints and is not prefix identity. A version bump would rotate
   every Chat plan and re-cache every prefix for nothing. A plan built before
   this change keeps the default until it rotates, and only a scheduled
   occurrence in flight at deployment has one.

## Alternatives considered

- **A keep-alive instead of the one-hour write.** While a run waits, re-send
  its last request with `max_tokens: 0` and streaming off just before the entry
  expires. At Fable 5.1's 0.025 read price this is cheaper for most waits: a
  fifteen-minute child wait costs about three reads instead of the 0.75
  premium. But the runtime does not hold a parked run's last request. It would
  need a timer over parked runs, a non-streaming send path, and attempt and
  budget accounting for the pings. That is new machinery, deferred until
  measurement shows the premium matters.
- **The one-hour TTL for delegated children.** Rejected: nothing in a child's
  shape produces a gap of five minutes or more, so the premium is pure cost.
- **The one-hour TTL for every session that can delegate.** Chat can delegate
  and then waits up to fifteen minutes, but `model-gateway.md` keeps interactive
  sessions on the default. On Fable 5.1 a keep-alive is also the better answer
  to human-paced gaps.
- **Shortening the later TTL to satisfy the ordering.** Rejected: it discards
  the reuse the context engine asked for on the larger prefix, whereas lengthening
  the earlier one costs only that prefix segment's premium.
- **A required pricing key.** Rejected: it would fail a reviewed operator
  overlay that predates the key. Optional per-model keys follow ADR-0119.

## Consequences

- A scheduled occurrence pays 0.75 of its frozen prefix once per epoch. On
  Fable 5.1, a 15,000 to 20,000 token prefix costs USD 0.11 to 0.15 more per
  occurrence. That is inside ADR-0109's USD 5 default and is recorded exactly.
- No measured distribution of gaps within scheduled runs exists yet.
  `model_calls.started_at` measures it directly. If most occurrences have no
  gap of five minutes or more, their TTL should return to the default.
- The Anthropic profile changes, so every `registry_version` changes. As with
  any registry change, runs pinned to the previous registry must drain before
  deployment.
- OpenAI and OpenAI-compatible profiles are unaffected. Their adapters drop
  cache hints, and they price no one-hour write.
