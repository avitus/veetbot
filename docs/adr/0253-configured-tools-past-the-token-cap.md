# ADR-0253: A configured tool past the token cap is deferred, not fatal

- Status: Proposed (a production repair the owner asked for on 2026-10-25; the
  owner has not yet reviewed the decision)
- Date: 2026-10-25
- Related: ADR-0105, ADR-0130, ADR-0239, ADR-0240
- Amends: ADR-0123 decision 4, which deferred configured tools past the item
  cap but not past the token cap
- Detailed design: `docs/plan/context-engine.md`

## Context

ADR-0123 decision 4 deferred configured tools past the item cap, but a
configured tool was never checked against the 9,000-token cap: the plan took
every one and failed with `ContextOverflow` when they did not fit. On 2026-10-25 the
records tools joined the default roster, and in a chat bound to a website
profile, whose four browser definitions are required, the configured
definitions passed the cap. Every new bound chat then failed before its first
step with "context prefix class tool_definitions exceeds its cap". The
production-shaped roster gate had not gained the hosted browser, media
generation or records when production activated them, so it never planned
that roster.

## Decision

When the agent enables `tool.call`, a configured tool that would take the
definitions past the token cap is now deferred like one past the item cap, in
the same rank order, and the next configured tool that fits still takes a
definition. A required tool is never deferred, and an agent without
`tool.call` still fails at plan time. A control tool past either cap is
recorded in `skipped_tool_names` and never listed, because `tool.call` refuses
control targets, and control overflow alone reserves no `tool.call` slot. A
plan that fit before, with every control tool defined, is selected exactly as
before, because the check only moves a tool the old plan could not have held.
The default agent's control tools rank ahead of slot thirty, so no chat is
re-planned and the builder version does not change.

## Consequences

With every production flag on, a bound chat now defines thirty tools and
moves `records.append` to the index. The index then reaches its own
2,000-token cap and skips the personal account's `trash_thread` and
`untrash_thread`, which are recorded in `skipped_tool_names`. The index cap is
unchanged and remains the owner's decision. The gate in
`tests/gates/test_deferred_tools_adr0123.py` now plans a bound chat with the
hosted browser, media generation, records and the Cockpit on.

## Alternatives considered

- **Raise the token cap again.** ADR-0105 did this once. It buys headroom
  until the next enabled tool, costs tokens on every request, and is the
  owner's decision alone.
- **Defer the records reads by configuration.** The default agent defers
  only the two rare series changes and keeps `records.list`, `records.query`
  and `records.append` direct, and the next configured tool would fail every
  bound chat the same way.
- **Fail at plan time, as before.** One more enabled tool then fails every
  chat of that shape before its first step, which is what happened.
