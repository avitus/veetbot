# ADR-0124: Chat's default roster drops workspace.list_files so the calling tools fit

- Status: Accepted (authorized by the repository owner, 2026-09-23)
- Date: 2026-09-23
- Related: ADR-0097, ADR-0104, ADR-0105, ADR-0112; Section 11.1 of the
  engineering plan
- Amends: ADR-0105 (the gate it promised, and the default agent's configured
  roster)
- Detailed design: `docs/plan/context-engine.md`, `docs/plan/builtin-tools.md`

## Context

ADR-0105 raised the tool-definition token cap so that the thirty-item cap
binds first. With twenty-six configured tools, four discovered tools entered
Chat's roster in name order: the three calling tools and
`mcp.gmail_read.get_thread`.

Milestone 31's `email.subscriptions` and `email.unsubscribe` are configured
tools. With `AGENT_EMAIL_UNSUBSCRIBE_ENABLED` on, production enables
twenty-eight:

- `math.calculate`, `conversation.ask_user` and `system.current_time`;
- three workspace tools, `sandbox.run_command` and `artifact.export`;
- working state, three memory tools and three People tools;
- four email tools and `knowledge.search`;
- `web.search` and `web.fetch`;
- `schedule.create` and five lifecycle tools.

`skill.load` is dropped because production has no skills. That left two
slots. Name order gave them to `mcp.bland_call.start_call` and
`mcp.bland_read.get_call`. `mcp.bland_read.list_calls` and all sixteen Gmail
tools never reached Chat. The roster cost 7,068 of the 9,000 tokens, so the
item cap decided. The owner could place a call but not list calls, and
nothing recorded the cut.

ADR-0105 promised that `tests/gates/test_call_runtime.py` would fail any
roster that squeezes the calling tools out. Its production-shaped case never
enabled unsubscribe, so it passed while production failed.

Two statements in ADR-0105 need correcting:

- Its first rejected alternative says an explicitly enabled tool that does
  not fit "fails the plan at run time". That holds for the token cap only.
  Configured tools past slot thirty are cut silently
  (`src/agent_core/context/planner.py`), as the planner contract test for
  first-occurrence priority shows.
- Its context says the `tests/gates/test_email_m18.py` roster case requires
  no MCP tool. It now requires Gmail tools.

Two facts decide which configured tool gives up its slot:

- The run workspace lives for one worker claim. It is erased when a run parks
  for an approval or a question, and a Docker environment expires after 300
  seconds (`docs/plan/sandbox-isolation.md`). A model never inherits a
  workspace to explore; it knows what it wrote during the current claim.
- No gate, evaluation case, skill, client or prompt depends on
  `workspace.list_files` being advertised. Its three Milestone 4 gates
  construct the tool directly.

## Decisions

1. **The default agent no longer enables `workspace.list_files`.** This is
   bootstrap curation of the fallback roster, like the omission of
   `demo.external_write` and `knowledge.ingest` that `docs/plan/web-access.md`
   describes. The tool stays registered: an explicit `enabled_tools` list may
   name it, and sessions pinned to an earlier agent version keep it. Under
   production's flags the roster holds twenty-seven configured tools and all
   three calling tools: thirty items at 7,058 tokens, estimated the way
   `tests/gates/test_email_m18.py` estimates them.
2. **`workspace.read_text` no longer names it.** A directory path returns
   "That path is a directory." A message naming a tool the model was not
   offered costs a refused call.
3. **The production-shaped roster gate enables every flag that production
   enables and that changes the default roster**, email unsubscribe included,
   and it requires all three calling tools. Activating another such flag in
   production adds it to that gate first. Device SMS is the next known one.
   The gate then fails until a slot is found.
4. **Nothing else changes.** The item cap stays at thirty and the token cap at
   9,000. The policy files and the context builder version do not change.

## Consequences

- New chats reach `mcp.bland_read.list_calls`. A session pins the agent
  version it was created with, and the plan prefix does not depend on
  `enabled_tools`, so existing chats keep their roster until the owner starts
  a new one. A builder-version bump would not change that, because it rebuilds
  a plan from the session's pinned agent.
- Paired Telegram and WhatsApp threads follow the latest default agent
  version. Each starts a fresh session on its next message after the deploy,
  as it does after any change to the default agent.
- No slot is spare. The next configured tool cuts `list_calls` again, and the
  gate in decision 3 fails before production does.
- Gmail tools stay out of Chat, which has carried at most one since
  ADR-0105. Email work is unaffected: typed Email runs prepare their own
  servers (ADR-0104).
- `sandbox.run_command` is not a drop-in replacement for listing. Every
  command needs approval, and the approval pause discards the workspace, so a
  command that creates files should print its own listing.
- Two test pins move. The single-account roster in
  `tests/gates/test_email_m18.py` holds twenty-eight tools instead of
  twenty-nine. Its two-mailbox case still pins `workspace.read_text` and
  `workspace.write_text` but no longer pins `workspace.list_files`.

## Alternatives considered

- **Deferred tools.** Tools that do not fit would stay pinned without being
  advertised and would be reached through one governed control tool. This is
  the "tool filtering or skills" remedy `docs/plan/context-engine.md` names,
  and it would bring every Gmail tool back to Chat. It needs a new control
  tool, a new prefix class and a builder-version bump. It remains the answer
  at the next squeeze.
- **Raise the item cap.** Thirty-one restores `list_calls`.
  `docs/plan/context-engine.md` argues against it because selection accuracy
  degrades with the number of candidates, and only the owner changes the cap.
- **Rebuild existing chats.** A plan-time filter plus a builder-version bump
  would reach existing chats at their next message. A run parked on an
  approval or a question at deploy time would then resume against a
  different tool set and fail with `tool_pin_mismatch`.
- **Drop or merge schedule, People or unsubscribe tools.** Milestones 23 and
  31 name those tools in their scope, and Milestone 28's governed Chat recall
  runs through the People tools.
- **Name the calling tools in `enabled_tools`.** Configured tools only reorder
  among themselves, so this frees no slot.
