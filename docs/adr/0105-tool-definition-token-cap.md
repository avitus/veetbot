# ADR-0105: The tool-definition token cap rises to 9,000 so the item cap binds first

- Status: Proposed
- Date: 2026-09-17
- Related: ADR-0030, ADR-0071, ADR-0097 (calling, whose tools this restored)
- Amended by: ADR-0124 (2026-09-23), after email unsubscribe took two of the
  four discovered slots
- Detailed design: `docs/plan/context-engine.md`

## Context

`context/plan.yaml` caps tool definitions at thirty tools and 6,000 tokens.
`docs/plan/context-engine.md` states that the item cap is the primary limit,
because selection accuracy degrades with the number of candidates rather than
their token weight. The token cap is meant to bound the cost of those thirty
items.

In production it decided the roster instead. The owner's configured tools —
People, Email mode, scheduling, web, workspace, memory and the rest — number
twenty-six and cost 5,973 of the 6,000 tokens. Configured tools take their
slots first, and a discovered tool is skipped when it would exceed the token
cap (`src/agent_core/context/planner.py`). Every discovered tool was therefore
skipped while four of the thirty slots stood empty, and the skip is silent: no
event, no log line, no error to the owner.

The consequence surfaced on 2026-09-17. A caller left a message with the Bland
receptionist on 2026-09-16 at 06:14 UTC. The callback, the worker, the record
and its summary all worked. The owner asked Chat to read the call and Chat
answered that it had no such tool, because `mcp.bland_read.list_calls`,
`mcp.bland_read.get_call` and `mcp.bland_call.start_call` — 592 tokens for all
three — had never entered a plan. Every first-party Gmail tool was excluded the
same way. Milestone 27's gates never caught it: `tests/gates/test_call_runtime.py`
pinned `start_call` in a minimal roster, and the production-roster test in
`tests/gates/test_email_m18.py` runs with People disabled and requires no MCP
tool.

Enabling delegation on top of that roster fails the run outright with
`ContextOverflow`, which shows the class had no headroom left for any growth.

## Decision

Raise `classes.tool_definitions.max_tokens` from 6,000 to 9,000 and the prefix
`ceiling_tokens` from 17,000 to 20,000, exactly the 3,000 tokens the class
gained, following the precedent Milestone 22's persona row set.

The thirty-tool item cap does not change. With twenty-six configured tools it
now binds first, which is what `context-engine.md` always specified: four
discovered tools enter, in name order, and the three calling tools are among
them. A deployment that wants more than thirty candidates still needs tool
filtering or skills, not a larger allowance.

The builder version becomes `context-builder@11`, so existing sessions rebuild
their plans through the ordinary logged epoch rotation and recover the tools
without replacing their history.

## Consequences

- The Chat prefix may grow by up to 3,000 tokens per request, about 1.5% of a
  200,000-token window, charged on every request of a session. History takes the
  remainder and absorbs the change.
- The owner's calling tools reach Chat. Gmail tools take the remaining slot in
  name order; Email work is unaffected, since typed Email tasks pin their own
  servers and never consult this class (ADR-0104).
- The item cap, not the token cap, now decides which discovered tools are
  skipped. A silent skip is still possible when more than thirty tools exist,
  and that remains the documented behavior.
- `tests/gates/test_call_runtime.py` gains a production-shaped roster case, so a
  future roster that squeezes the calling tools out fails a gate instead of
  reaching the owner.

## Alternatives considered

- **Name the calling tools in `enabled_tools`.** Within the current spec, but an
  explicitly enabled tool that does not fit fails the plan at run time instead of
  being skipped, so the same crowding would break every Chat run rather than one
  capability. It also needs about 600 tokens freed from existing descriptions,
  which the next feature would consume again.
- **Move rarely used tools behind skills or a filter.** The spec's own remedy,
  and still the right answer beyond thirty tools. It is a larger change than the
  defect requires and would not have restored calling today.
- **Leave the cap and accept the loss.** Rejected: the platform would keep
  shipping capabilities the owner cannot reach, without an error.
