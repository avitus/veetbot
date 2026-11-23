# ADR-0342: Code mode runs a model-written script over the sandbox bridge

- Status: Accepted. The owner asked on 2026-11-21 for this decision with the
  existing Docker sandbox and authorized its implementation the same day, and
  accepted the ADR on 2026-11-22. The tool, its budget accounting and its
  approval rule are implemented behind a default-off setting (see
  Implementation status), as are Chat's roster, instructions and evaluation
  case. A local Docker run passed on 2026-11-22. On 2026-11-23 the owner
  decided that one approval covers every script in a request (decision 11).
  Production latency evidence and activation are open.
- Date: 2026-11-21
- Amends: ADR-0015 (names the tool of decision 1, defines the
  "orchestration-only turn" of decision 3, counts bridged calls against the
  run's tool-call limit), ADR-0021 (the bridge refuses two kinds of tool).
  Decision 9 adds one exact-name raise of argument trust beside ADR-0268's.
  Decision 11 amends ADR-0302 and ADR-0344: `approve_for_request` and
  `request_grant` also serve a script approval.
- Related: ADR-0008 and ADR-0029 (sandbox isolation), ADR-0123 (deferred tool
  index), ADR-0268 (agent-written jobs, the same transport), ADR-0277 (trust of
  earlier bridge results)
- Detailed design: `docs/plan/tool-system.md`, `docs/plan/builtin-tools.md`,
  `docs/plan/sandbox-isolation.md`

## Context

ADR-0015 decided that the model may write code that calls tools in one turn.
Milestone 6 built the enforcement half: the bridge, its per-turn socket and
token, synthesized call ids, the approval hold and the 64-call cap. Every
bridged call re-enters the ordinary tool pipeline.

The model-facing half was never built.

- No library in the sandbox exposes the run's tools. A script would have to
  speak the bridge's JSON frames by hand, and nothing tells the model it can.
- `refund_orchestration_turn` exists on the budget ledger and has no caller.
- Chat's instructions tell the model to use `sandbox.run_command` "only when
  arbitrary code execution is necessary".

The cost is the failure ADR-0015 set out to remove. Asked which items appear
in two long lists, the model fetches one page of each, one model step per
page, and answers from partial data. The deferred tool index (ADR-0123) keeps
definitions out of the prompt but still costs one model step per call.

A colleague recommended an in-process Python interpreter (Monty) for this.
The owner prefers no new external dependency unless one is necessary.

## Decisions

1. **One builtin, `sandbox.run_code`, runs a Python script in the run's
   existing sandbox.**
   - Arguments are `code` (Python source, 1 byte to 64 KiB) and an optional
     `timeout_seconds` (at most 300).
   - The source reaches `python -m agent_core.execution.code_runner` on
     standard input through `SandboxManager`. Nothing is executed on the
     worker, and no interpreter is added to the worker process.
   - It is classified exactly as `sandbox.run_command`: `CODE_EXECUTION`,
     `HIGH`, `NON_IDEMPOTENT`, scope `sandbox.execute`, target `sandbox`,
     output `EXTERNAL_UNTRUSTED`. The policy files do not change.
   - No dependency is added. The runner and its library use the standard
     library only.
2. **Inside the script, `tools` exposes the run's tools as functions.**
   - `tools.email.search(query="...")` and `tools.call("email.search",
     {"query": "..."})` send the same bridge request. A name is an attribute
     path, so no catalog is generated or copied into the sandbox.
   - The library grants nothing. It holds no tool list and no credential; the
     bridge decides whether a name exists and whether the call may run.
   - A successful call returns the text the model would have read. When that
     text is a JSON object or array, the call returns the parsed value.
   - Any other outcome raises `ToolCallError` carrying `status`,
     `reason_code` and `retryable`. An uncaught error ends the script with a
     traceback on standard error and a nonzero exit code.
3. **A script reaches the run's pinned tools and nothing else.**
   - Each call passes validation, scopes, policy, hardline rules, approval,
     timeout, output limits, tracing and idempotency, as ADR-0015 requires.
   - A pinned tool is reachable by its own name whether the prompt defines it
     or the deferred index lists it. The script needs no `tool.call` wrapper.
   - The bridge refuses a control tool and a tool whose target is the sandbox,
     with `bridge.tool_not_bridgeable`. A script therefore cannot start
     another script, ask the user a question or edit working state. This
     applies to every bridge caller, `sandbox.run_command` included.
4. **Call identity follows the invocation, not the source alone.** The script
   hash covers the source and the outer call id, as `sandbox.run_command`
   already does. A replay of one invocation derives the same call ids and
   reads recorded outcomes. The same source submitted again later is a new
   invocation and executes for real. ADR-0015's determinism caveat stands.
5. **Only what the script prints returns to the model.** The result carries
   standard output, standard error, the exit code and the number of bridged
   calls. Bridged results never enter the conversation. The result is
   `EXTERNAL_UNTRUSTED`, and ADR-0277 still carries the trust of earlier
   bridged results into later bridged calls.
6. **An orchestration-only turn is a step whose every tool call is
   `sandbox.run_code` and whose scripts dispatched at least one bridged
   call.** Such a step refunds one step and one model call through the
   existing ledger method. Bridged calls count against the run's tool-call
   limit, so the refund cannot unbound tool work. The 64-call cap per script,
   the cost budget and the run deadline are unchanged.
   - The count applies to every bridge caller in the model loop,
     `sandbox.run_command` included. The bridge refuses a call the limit has
     no room for with `tool.budget_exhausted`, so the count never passes the
     limit and the run still owes its answer.
   - A refunded step keeps its number. The ledger lowers the run's step
     count, and the loop numbered steps from that count. The step number is
     now the step count plus the steps refunded, recorded in the checkpoint.
     Idempotency keys, per-step limits and the recall phase read the number,
     so none of them sees two steps alike.
   - The refund and the bridged count are written after the checkpoint that
     ends the step. A crash in between loses one refund; it cannot refund a
     step twice.
7. **Latency is measured before any interpreter is considered.** The sandbox
   is already created on the first sandbox call and held across steps within
   a claim (`sandbox-isolation.md`), so a second script reuses the container.
   An in-process interpreter would move untrusted code into the worker and
   needs its own ADR.
8. **Activation is a default-off setting, `AGENT_CODE_MODE_ENABLED`.** Off,
   the tool is not registered and rosters, prompts and pins are unchanged.
   On, the tool takes the place of `sandbox.run_command` in Chat's configured
   tools and Chat's instructions gain one paragraph (decision 10). Production
   activation is the owner's decision after the evaluation and latency
   evidence.
9. **A script written for the owner's request runs without an approval.**
   Under the default profile every script asked, because source the model
   wrote is untrusted input, and a second script asked again. The owner
   decided on 2026-11-22 that a script should ask less often.
   - The source of `sandbox.run_code` is labelled Veetbot's own work
     (`INTERNAL_TOOL`) when the newest user message is the owner's and the
     turn has read no outside content. ADR-0268 gave `job.manage` and
     `job.run` the same kind of rule.
   - Recalled memory in the context does not make a script ask. Production
     Chat always carries it, so a rule that required a turn without memory
     would never apply.
   - A turn that has read a web page, a message, a knowledge passage or an
     earlier script's output still asks before the next script. That is the
     case in which the source may carry out someone else's instruction.
   - The policy profile, the trust overlay and `policy_version` do not
     change. Each call a script makes over the bridge is still judged under
     its own trust, so a write, a send or a delete asks as before.
   - `sandbox.run_command` is unchanged.
10. **In Chat the script tool replaces the command tool, at no roster cost.**
    The roster sits at its caps by design (ADR-0253), and raising a cap is
    the owner's decision.
    - Keeping both tools defined moves one read into the index in an
      ordinary chat and two in a chat bound to a website. Listing the command
      tool in the index leaves the fullest chat nine spare entries, one under
      ADR-0267's ten. Replacing it moves nothing: the definitions grow by
      about 42 tokens and the index is untouched.
    - A script runs a command with `subprocess` in the same sandbox. The
      command tool stays registered, so a chat that pinned it keeps it.
    - Chat's instructions gain one paragraph only while the agent has the
      tool. It names loops, paging and comparing two lists as the cases for
      a script, keeps a single call and every change as a direct call, and
      points the base instructions' mentions of the command tool at a script.
11. **One approval covers every script in a request.** After decision 9 a
    request that read outside content still asked before each script. The
    owner decided on 2026-11-23 that one approval should cover them all. This
    loosens the trust overlay for this one tool inside one request, and the
    owner approved that in the conversation that produced this decision.
    - A script card raised only because the turn read outside content offers
      "Allow for this request" beside Approve once and Deny, in an eligible
      run: the eligibility ADR-0344 decision 1 set for jobs. The offer is
      stored on the approval as `script_request_grant_offer` with the tool
      name, `duration_seconds` (7200) and a server-authored summary.
    - The resolution is the existing `approve_for_request`. Its
      `request_grant` body repeats the offered tool as `tool_name`. It
      requires `approval.resolve` only: it allows nothing a script card does
      not already allow one script at a time. Accepting approves the pending
      script and records `script.request_grant.created` in the same
      transaction. The approval is the grant. No table, migration, compiled
      policy or `policy_version` changes.
    - The grant covers starting a later `sandbox.run_code` call of the same
      run whose only reason to ask is outside content. It covers nothing a
      script does: every call over the bridge is still judged under its own
      trust, so a write, a send or a delete asks. A profile that asks before
      any code execution keeps asking, and its card makes no offer.
      `sandbox.run_command` is not covered.
    - Lifetime follows ADR-0302 decision 4: the grant ends when the run
      ends, after two hours, or when the run reads a follow-up from the
      owner. It never transfers to another run or session, and the agent
      version and policy version must match the approval's.
    - Each use is audited by `tool.call.authorized` with
      `authorization_kind: "script_request_grant"` and the approval id.
    - The authorizer, the offer and the resolution are composed only while
      the setting of decision 8 is on. Off, a script card cannot exist and
      `approve_for_request` naming the tool is refused as unavailable.
    - A client that predates the offer shows Approve once and Deny.

## Implementation status

The work is cut into four slices.

1. **Tool, library, runner, bridge refusal and setting.** Implemented offline
   on 2026-11-21. The sandbox image copies the two new modules.
2. **Budget.** The refund under decision 6, the bridged-call count and the
   refusal at the limit. Implemented offline on 2026-11-22, in the model loop
   and in the path that resumes a suspended batch.
3. **Chat.** The approval rule (decision 9), the roster and the instructions
   (decision 10), and evaluation case 36 for the two-list intersection.
   Implemented offline on 2026-11-22. The case runs a real script through
   the bridge and the pipeline against a scripted model, so it proves the
   mechanism. It does not show that a live model chooses a script when it
   should; slice 4 observes that. One approval for all the scripts of a
   request (decision 11) was implemented on 2026-11-23, on the server and on
   the Apple approval card.
4. **Evidence and activation.** Started on 2026-11-22 with a local run.
   - **Done, local Docker (runc, a development laptop).**
     `tests/security/test_code_mode_runtime_adr0342.py` runs the real tool
     over the Docker adapter with the rebuilt image. A script paged two lists
     through the in-sandbox relay. A script ran a command with `subprocess`
     and a later script in the same run read the file it wrote. A script saw
     no token or key in its environment, had no network, and read a refusal
     as `ToolCallError`. An uncaught error ended with its traceback.
   - **Measured there, three runs.** The first script of a run took 1.6 s,
     which includes creating the container. Each later script took 0.84 s, of
     which 0.3 s was inside the sandbox. A script with no tool call took the
     same 0.84 s, so four bridged calls to an in-process dispatcher added
     nothing measurable. The cost is fixed per script: starting the relay,
     waiting for its socket, and starting Python.
   - **Not done.** The same timings in the production sandbox, whose runtime
     and host differ; real tools behind the bridge, which add their own time;
     a live model's choice of a script; then the owner's activation.

## Consequences

- A pipeline costs one model step instead of one per call, and only the
  answer enters the conversation.
- The bridge frame stays at 64 KiB. A single tool result larger than that is
  refused with `bridge.response_too_large`, which bounds what one call can
  hand a script. Slice 4 measures whether real tools reach it.
- An approval inside a script holds the sandbox for up to 300 seconds, then
  the run suspends and the script re-executes from the start on resume.
- The sandbox image must be rebuilt before the setting is turned on.
- With the setting on, a new chat no longer lists `sandbox.run_command`. A
  command then runs from a script, which the local Docker run confirmed.
- Turning the setting on changes the default agent's tools and instructions,
  so it registers a new agent version. New chats use it; existing chats and
  schedules keep the version they pinned.
- The refusal in decision 3 also changes `sandbox.run_command`: a command can
  no longer reach a control tool or another sandbox tool over the bridge.
- Decision 6 also changes `sandbox.run_command`: the tool calls a command
  makes over the bridge now count against the run's tool-call limit. They
  were not counted before. Its steps are never refunded.
- Calls bridged by an attempt that suspended or crashed before its step ended
  are not counted. Each such attempt is still bounded by the 64-call cap.
- A script written after the turn read outside content shows an approval
  card. With decision 11 the owner can allow the rest of the request from
  that card, so a request that reads mail and then runs three scripts asks
  once. Approve once still leaves the next script asking.
- Inside a request the owner allowed, a script written after reading outside
  content starts without a card showing its source. That is the loosening
  the owner accepted. The script still runs in the sandbox without network
  or credentials, and each call it makes is decided by policy as before.
- Decision 9 removes a card that guarded code execution inside the sandbox.
  After it, the isolation of the sandbox and the policy on each bridged call
  are the controls on a script written in a clean turn. Origin trust resets
  at each owner message, as `policy-and-approvals.md` already states, so
  outside content read in an earlier turn does not make a later script ask.

## Alternatives considered

- **An in-process interpreter (Monty).** Not adopted. It is a new external
  dependency and runs model-written code in the worker process, outside the
  boundary ADR-0008 draws. It stays open as a later decision if slice 4 shows
  the sandbox is too slow.
- **`exec` with restricted builtins in the worker.** Rejected. In-process
  restriction of Python is not a security boundary.
- **A fixed pipeline tool** (paginate, filter, intersect) interpreted by the
  worker. Not adopted. It needs no sandbox but cannot express open-ended
  processing. It remains a fallback.
- **Teaching the model to use `sandbox.run_command` for this.** Rejected.
  Python inside a JSON argument vector is fragile to quote, the approval card
  shows a shell command, and an orchestration-only turn could not be told
  apart from an ordinary command.
