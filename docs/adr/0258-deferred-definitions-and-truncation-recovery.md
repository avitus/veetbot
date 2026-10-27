# ADR-0258: Deferred definitions, recoverable truncation, and JSON field reads

- Status: Accepted (the owner asked for all four changes on 2026-10-26)
- Date: 2026-10-26
- Related: ADR-0054, ADR-0130, ADR-0240, ADR-0253
- Amends: ADR-0123 decision 2 (invalid arguments return only the input
  schema) and ADR-0242 (its claim that a truncated result stays reachable
  through its pointer)
- Detailed design: `docs/plan/context-engine.md`, `docs/plan/tool-system.md`,
  `docs/plan/web-access.md`
- Renumbered from ADR-0256 on 2026-10-27, when `main` took that number for
  the new-mail Email budget reserve; earlier commits and review comments on
  PR #204 call this decision ADR-0256

## Context

On 2026-10-26 the owner asked a Cockpit chat to improve a widget that lists
their GitHub branches. Two of its runs failed in ways that had nothing to do
with the widget code.

**The model probed deferred tools again and again.** A deferred tool's index
entry shows its name, its parameter names and the first sentence of its
description; for `cockpit.read` that sentence is "Read the Cockpit." An invalid
call through `tool.call` returned the input schema and nothing else, so the
rest of the description never reached the model, including "read the kit
before writing a widget" and "preview is not available yet". The research run
sent ten empty-argument probes, three of them to `cockpit.configure`, and two
of its model steps contained nothing but a `cockpit.read {}` probe. Its answers
were truncated like any other tool result, so the same probe was needed again.

**The research run repeated itself until the identical-call guard stopped
it.** To learn how far each of about sixty branches was ahead of `main`, the
model read one GitHub compare answer per branch with `web.fetch`. Each answer
was about ten kilobytes for two numbers, 111 fetches came to about 984
kilobytes, and from step 8 the oldest results were truncated to pointers. No
model-callable tool reads an event or a captured result back, the model was
never told that truncation had begun, and a result of the current run had no
event number, so its pointer read `event:pending` and a working-state fact
could not cite it. The model fetched the same pages in three cycles until
three fingerprints reached the guard's threshold and forced an answer. The run
took 29 steps and 2.31 million input tokens, cost USD 13.70, and kept nothing.

## Decision

1. **An invalid deferred call returns the whole definition.** The refusal
   carries one text: `Definition of <name>.`, the full description, and the
   input schema as before. The trust split is unchanged: a builtin's
   definition is trusted configuration, and a server's or device's definition
   is untrusted data.
2. **The newest definition answer of each deferred tool stays whole.**
   Tool-result truncation skips it and still counts it against the budget.
   The kept answers together never exceed that budget: past it, the oldest
   truncate in their turn like any other result, so probing many deferred
   tools cannot crowd out the rest. An
   answer qualifies only when it replies to `tool.call` for a tool the plan
   defers, and its content is exactly the platform outcome
   `tool.arguments_invalid` followed by that tool's pinned definition. A
   tool's own output that copies a definition, or a schema-only answer
   recorded before this decision, truncates as before.
3. **A result of the current run carries its event number.** After tools run,
   the runtime gives each new result the sequence of the event that recorded
   it, as history rebuilt from the log already does: the model's batch, a
   refusal, the runtime's own browser recovery and workflow reads, and a batch
   resumed after an approval. A replayed result recorded by an earlier attempt
   keeps none, and so does a call identifier that more than one result
   carries. A truncated result's pointer names that event, after the
   artifact when one holds the content.
4. **A platform note says when results are truncated.** While anything is
   truncated, the step context gains a platform row: older results are
   pointers that no tool reads back, and a call should not be repeated only to
   recover one. When the plan defines `context.update_working_state`, the row
   also says to keep each needed finding as a fact citing its result's event,
   and lists the events of the newest twelve results still shown whole after
   the history cut, with the pinned tool that produced each. The row is in the step context, outside the
   cached body, and only names tools the plan pins.
5. **`web.fetch_json` reads named fields of a public JSON document.** It takes
   a URL and one to thirty-two dotted paths of object keys, array indexes and
   `*` for every array element. It reads the document through `web.fetch`'s
   provider route, unwraps one Markdown code fence, and returns the selected
   values and the paths that matched nothing. A page that is not JSON fails
   with `tool.web.json_invalid`, as does NaN, Infinity, a number too large for
   a float or nesting too deep to decode; a lone surrogate in a string becomes
   U+FFFD. A selection past 64 KiB fails with
   `tool.web.json_selection_too_large`. Its classification is `web.fetch`'s:
   `NETWORK_READ`, `READ_ONLY`, `EXTERNAL_UNTRUSTED`, `web_provider` target,
   and the policy engine treats it as one of the fixed provider tools. The
   default agent enables it with `web.fetch` and defers it, so it takes no
   definition slot.

The deferred-index note still says that an invalid call returns the input
schema. That wording predates this decision but is not false: the refusal now
returns the whole definition, the description and the input schema. Changing
that text would change every plan's prefix and re-plan every chat.

## Consequences

- One probe gives a deferred tool's whole definition, and the answer survives
  later truncation, so ADR-0123's "at most one extra model step" holds
  through a long run.
- A long tool loop can keep its findings in working state. Working state is
  shown on every step and never yields.
- Each step with tool calls reads the new events of its own run once.
- With every production flag on and a website profile bound, the deferred
  index was at exactly its 2,000-token cap. `web.fetch_json` is a configured
  deferral, so it ranks ahead of discovered overflow and moves
  `mcp.gmail_write.modify_labels` (the personal account's) into
  `skipped_tool_names` in such a chat, beside `trash_thread` and
  `untrash_thread` (ADR-0253). Its entry's first sentence is kept short for
  that reason. Raising the index cap remains the owner's decision. The
  definition cap and the thirty-item roster are unchanged, and a chat without
  a website profile skips nothing.
- The default agent's version changes, so the new tool reaches chats started
  after the deploy. Schedules stay pinned.
- The builtin census gains one tool, for fifty-two.

## Verification

- `tests/gates/test_deferred_tools_adr0123.py`: invalid arguments through
  `tool.call` return the whole definition for a builtin and for an MCP tool,
  with the trust of each, and the production rosters defer `web.fetch_json`.
- `tests/unit/test_context_truncation_recovery.py`: the newest definition
  answer survives truncation while a stale copy, a direct call's copy and a
  schema-only answer truncate, and kept answers stay within the budget; an
  artifact pointer also names its event; the
  note appears only while results are
  truncated and names the events of the results still shown, after the
  history cut.
- `tests/gates/test_truncation_recovery_adr0258.py`: in a composed run, every
  truncation pointer names a recorded `tool.call.completed` event and the note
  lists events of results still shown; a browser recovery read and a batch
  resumed after an approval carry their events, and a call identifier
  recorded twice carries none.
- `tests/unit/test_browser_feed_workflow.py`: every browser workflow read in
  the final report request carries its event.
- `tests/unit/test_web_fetch_json.py`: field selection from raw and fenced
  JSON, `*` paths, the two failures including non-finite numbers and deep
  nesting, lone surrogates, URL refusal, and a deferred call through the
  default agent that policy allows.

Each test failed before its change: the refusal returned only
`Input schema: …`, the definition answer truncated with the others, no note
appeared, live pointers read `event:pending`, and the tool did not exist.
