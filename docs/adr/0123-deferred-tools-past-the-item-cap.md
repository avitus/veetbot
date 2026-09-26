# ADR-0123: Chat defers tools past its item cap instead of dropping them

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: ADR-0020, ADR-0030, ADR-0079, ADR-0092, ADR-0094, ADR-0097, ADR-0104,
  ADR-0112
- Amends: ADR-0105 (its consequence that a silent skip remains the documented
  behavior), ADR-0124 (its consequence that no slot is spare) and
  `tool-system.md` decision 25 (the control-tool set of four)
- Amended by: ADR-0130 (2026-09-25), which ranks `browser.navigate`,
  `browser.observe` and `browser.act` first and never defers them in a chat
  bound to a website profile
- Detailed design: `docs/plan/context-engine.md`, `docs/plan/tool-system.md`,
  `docs/plan/builtin-tools.md`

## Context

Chat sends the model at most thirty tool definitions. Configured tools take
their slots first, and discovered MCP tools fill the rest in name order. Tools
that fit in neither were dropped, and nothing recorded the cut.

With production's flags the roster has forty-six candidates: twenty-seven
configured tools, three calling tools and sixteen Gmail tools across the
owner's two accounts. ADR-0124 dropped `workspace.list_files` so that the
three calling tools fit, which left no slot spare. Turning on device SMS,
delegation, the browser, or any new built-in tool would cut
`mcp.bland_read.list_calls` again, and every Gmail tool already stayed out of
Chat.

For a roster beyond thirty, `context-engine.md` prescribes "tool filtering or
skills", not a bigger allowance, and ADR-0105 named moving rarely used tools
behind a filter as the right answer beyond thirty. Skills cannot carry a tool:
a skill that names an unpinned tool still loads, and the pipeline still denies
the call. Name order was also arbitrary. It is why Bland beat Gmail, and why
one account's tools beat the other's.

## Decisions

1. **A tool can be deferred: pinned and listed, but not defined.**
   - A deferred tool is resolved, authorized and pinned at session open exactly
     like a defined one. The plan records it in `deferred_tool_names` and
     `deferred_tool_specs`, and a run's tool pins include it.
   - Instead of its definition, the prompt carries a one-line index entry: the
     name, its parameter names with optional ones marked, and the first
     sentence of its description, capped at 200 characters.
   - The entries form a new Region A class, the deferred tool index, capped at
     forty entries and 2,000 tokens. The prefix ceiling rises by exactly 2,000,
     to 22,000, so it remains the sum of the Region A class caps (ADR-0079).
   - The calling convention and builtin entries render as trusted
     configuration. Entries for discovered or device tools carry text from a
     server or device, so they render in an `EXTERNAL_UNTRUSTED` envelope.
   - A tool is advertised by its definition or by its index entry.
2. **One control tool reaches a deferred tool and adds no authority.**
   - `tool.call` takes `{name, arguments}`. The tool pipeline unwraps it
     before resolution, so the call continues as the named tool with the
     model's call id: argument validation, classification, trust overlay,
     policy, hardline rules, approval, idempotency, invocation rows and
     events all carry the deferred tool's own name. The result answers the
     `tool.call` the provider replays.
   - The named tool must be in the run's pinned set and must not be a control
     tool. Otherwise the call is denied with `tool.not_found.not_offered`. A
     malformed wrapper fails with `tool.arguments_invalid`.
   - Invalid arguments for the named tool return its input schema, because
     its definition never reached the provider. A schema from a server or
     device is returned as untrusted data.
   - `tool.call` is advertised only while the plan defers something. It is
     classified `NONE`, so every policy decision uses the deferred tool's own
     classification, and the policy files do not change.
   - The control-tool set becomes five and stays closed at build time. This
     amends `tool-system.md` decision 25.
3. **Discovered tools are ranked reads first.** Tools whose side effect is
   `NONE`, `WORKSPACE_READ` or `NETWORK_READ` get definitions before tools that
   change the world. Ties break by server or device, then by name. A deferred
   tool costs at most one extra model step, and a read is usually a request's
   first step, so reads keep their definitions. Configured tools keep their
   first-occurrence order ahead of discovered ones.
4. **Overflow is deferred, and anything that cannot be deferred is recorded.**
   - When anything is deferred, `tool.call` takes a definition slot, and every
     candidate without a definition becomes an index entry: the configured
     deferrals first, then the overflow in rank order. This includes
     configured tools past slot thirty, which were previously cut silently.
   - A candidate that fits in neither is skipped, and its name is recorded on
     the plan event as `skipped_tool_names`. An agent without `tool.call`
     defers nothing and records its skips the same way.
   - Nothing leaves the roster silently. This amends ADR-0105's consequence
     that a silent skip remains the documented behavior.
5. **The default agent defers its six management tools.** Its configuration
   names `schedule.update`, `schedule.pause`, `schedule.resume`,
   `schedule.cancel`, `email.subscriptions` and `email.unsubscribe` as
   deferred. They change or clean up existing state and are never the first
   step of an ordinary request. Each keeps its classification, scopes and
   approval, and `schedule.create` and `schedule.list` keep their
   definitions. The owner chose this set and the reads-first order on
   2026-09-25.
6. **The caps do not change, and no chat is re-planned.** Thirty definitions
   and 9,000 tokens stand. The builder becomes `context-builder@12`, and it
   treats a `context-builder@11` plan as current because such a plan renders
   the same bytes. Re-planning existing chats could move the selection under
   a run parked on an approval and fail it with `tool_pin_mismatch`. Chats
   pin their agent version, so the new roster reaches chats started after the
   deploy.

## Consequences

- With production's flags and both Gmail accounts, Chat defines twenty-one
  configured tools, `tool.call`, the two Bland reads and the six Gmail reads:
  thirty definitions at 5,908 estimated tokens. The index holds seventeen
  entries at 1,013 tokens: the six management tools, `start_call`, and the
  ten Gmail writes and sends. Nothing is skipped. Before this decision the
  thirty definitions cost 7,058 tokens, and the sixteen Gmail tools were not
  offered at all.
- `mcp.bland_call.start_call` moves to the index. Starting a call can take one
  more model step, and still waits for the same approval.
- A configured tool added later no longer evicts a capability. It moves the
  lowest-ranked definition into the index. The production-shaped roster test
  enables every flag production enables that changes the default roster, and
  it gains each new one when production activates it.
- ADR-0124 needed `workspace.list_files` out of the definitions. It stays out
  of the default roster: the workspace lives for one worker claim, so the
  tool has little to offer even as an index entry.
- Milestone 23 hard gate 35 says the schedule flag pair "advertises exactly"
  the lifecycle tools. An index entry advertises a tool, so the gate holds;
  its check asserts configuration and classification rather than the prompt.
- Delegation briefs may name deferred tools, because they are pinned. The
  child receives them as definitions.
- A client first shows a deferred call's activity as `tool.call` from the
  model's response. The pipeline's events rename it to the deferred tool.
- The configuration census gains two knobs, for 184 in all, and the builtin
  census gains `tool.call`, for thirty-seven model-callable builtins.
- No hard gate is registered and the milestone gate counts do not change, as
  for ADR-0113, ADR-0116 and ADR-0121. The tests are
  `tests/gates/test_deferred_tools_adr0123.py`, the restated calling roster
  test, `tests/unit/test_deferred_tool_index.py`, and two planner contract
  cases.

## Alternatives considered

- **Raise the item cap.** Defining all forty-six candidates would cost 9,262
  estimated tokens on every request, 2,204 more than the thirty definitions
  before this decision and past the 9,000-token cap. The cost grows with each integration, it contradicts
  `context-engine.md`, and it is the owner's decision alone.
- **Rank discovered tools by use.** Tools the owner used most in the last
  thirty days would get definitions. It adapts to the owner, but it needs a
  usage lookup at session open and moves the ranking over time. The owner
  chose reads first.
- **Keep the calling tools first.** Hard-codes one integration's priority,
  and every later integration would ask for the same.
- **Withhold or merge configured tools.** Merging the schedule lifecycle
  tools, the People tools or the calling reads conflicts with the gates of
  Milestones 23, 27, 28 and 31, and withholding removes a capability.
- **Offer different tools by client mode.** Modes are presentation over one
  assistant with shared capabilities (ADR-0092).
- **Provider-side tool search.** Provider-executed tool use bypasses the
  policy engine and is a protocol error (ADR-0002).
- **Load a deferred definition when the model asks for it.** Definitions stay
  byte-stable within a run (engineering plan Section 11.1), and loading at the
  next run would make the owner repeat the request.
- **A `tool.call` that executes and re-enters the pipeline.** That writes two
  invocation rows and two event streams for one call, evaluates policy twice,
  and leaves the inner call's approval suspended under a running outer
  invocation. Unwrapping before resolution keeps one call, one row and one
  result.
