# ADR-0130: Chats bound to a website profile get a browser task budget

- Status: Accepted by the owner 2026-09-25 (decision 1, the browser-task
  limits). Decisions 2 to 9 are engineering decisions made within that
  acceptance.
- Date: 2026-09-25
- Related: ADR-0058, ADR-0078, ADR-0098, ADR-0106, ADR-0115, ADR-0119,
  ADR-0123, ADR-0124, ADR-0127, ADR-0129; Sections 12.5 and 33 of the
  engineering plan
- Amends: `tool-system.md` "The circuit breakers" (the identical-call row);
  ADR-0123 decisions 3 and 4 (configured order and overflow) for sessions
  bound to a website profile
- Detailed design: `docs/plan/runtime-loop.md`, `docs/plan/context-engine.md`,
  `docs/plan/tool-system.md`, `docs/plan/browser-automation.md`,
  `docs/plan/bootstrap-and-composition.md`,
  `docs/plan/http-api-and-streaming.md`

## Context

On 2026-09-25 a Duolingo session created on the owner's network loaded
`/learn` and opened `/lesson` from the server's headless, automated Chromium.
The owner now wants Veetbot to finish a lesson in a chat bound to that
website profile.

A lesson is about fifteen exercises. Each exercise takes three to eight
actions: choose word tiles or type, then Check, then Continue. That is about
70 to 100 `browser.act` calls. Every action names the revision of the page it
acts on, so each one needs its own model call. Five things stop a lesson
today:

1. **Run limits.** Chat runs have 32 steps, 24 model calls and 64 tool calls,
   with 2 model calls and 4 tool calls kept for the final answer
   (`runtime/limits.yaml`, ADR-0115). A lesson stops after about 20 actions.
2. **Repeated observations.** The identical-call breaker counts
   `(name, arguments)` for the whole run and fails it at five. Every
   `browser.observe` has the arguments `{}`, so the fifth observation ends the
   run, even when each one saw a different exercise.
3. **Unsettled pages.** `browser.navigate` returns at `DOMContentLoaded`, and
   `browser.act` returns right after the click. The observation shows the page
   before it has finished changing, so the model usually observes again. That
   doubles the calls per action.
4. **Hidden controls take the element slots.** An observation keeps the first
   256 matching nodes and only then drops the invisible ones. On a large page,
   hidden nodes can use up the cap before the exercise's buttons are reached.
5. **The model guesses the origin.** Nothing tells it which origins the
   profile allows, so it tries `https://duolingo.com` and is refused.

The roster is not a blocker on `dev`. ADR-0123 is implemented there:
configured tools take definitions before discovered ones, and overflow moves
to the deferred index instead of being cut. A bound Chat defines all three
browser tools. The guarantee still depends only on the length of the
configured list, and nothing tests it.

Other work is out of scope here. Lease reuse and renewal for a run are
ADR-0127. Signing in from the app is ADR-0128. The task grant and the
approval card are ADR-0129. This ADR is owner decision 2 of 2026-09-25
without lease renewal, plus the tool fixes a lesson needs.

## Decisions

1. **Chats bound to a website profile run under a browser-task budget.**
   (Owner, 2026-09-25.) `runtime/limits.yaml` gains a `browser_task` overlay:
   160 steps, 120 model calls, 160 tool calls, a cost limit of USD 30 and a
   cost reserve of USD 3. The model-call reserve (2) and tool-call reserve (4)
   come from `run_defaults`. Every other chat keeps the ordinary limits and
   has no cost limit.

   The owner asked for "a cost cap". The USD 30 value is sized from the model
   pricing as follows:
   - **Assumptions:** a 12,000-token prefix; 2,000 new tokens per model call
     (one lesson observation plus the call); 800 output tokens per call at
     high effort; one 7,000-token page early in the run.
   - **On GPT-6 Astra,** the default and most expensive chat model: a
     120-call run costs about USD 23.70 when history is cached. A typical
     90-call lesson costs about USD 15.10.
   - **Result:** the count limits bind first in an ordinary lesson. The cost
     limit binds only on poor caching, on oversized pages, or on a model whose
     history is not cached.

   How the cost limit holds:
   - Every model attempt is checked against the limit before it starts, and
     usage is checked again when it is recorded (ADR-0078 decision 3). A run
     can therefore exceed USD 30 by at most one model call: the one in flight
     when the limit was crossed. Recording that call fails the run with
     `budget_exceeded`.
   - One call costs at most its model's context window at the uncached input
     price plus 8,192 output tokens. That is about USD 3.05 on Astra (a
     272,000-token window) and about USD 10.33 on Claude Fable (a
     1,000,000-token window). A lesson call costs about USD 0.10 to 0.35.
   - Once USD 3 or less remains, the run may only answer. A tool request from
     the call that reached the reserve, or from any later call, fails the run
     with `budget_exceeded` (ADR-0078).
   - A delegated child's cost limit is carved from what the parent has left,
     and the child follows the same rule.
   - A model priced at zero, such as the local model, has no effective cost
     limit. Only the counts bound it.
2. **The overlay is chosen when the session is created and pinned by the
   agent version.**
   - The composition root stores the overlaid limits in the default agent's
     metadata under `browser_task_limits`, as ADR-0123 did for deferred tools.
     The agent's content-addressed version therefore includes them.
   - The composition root adds this key only to the default agent, and only
     when browser tools are enabled.
   - A run takes these limits instead of `agent.limits` when two conditions
     hold: its session has the trusted `browser_profile_id` binding, and the
     session's pinned agent version carries the key.
   - Both conditions are fixed when the session is created. Bound and unbound
     chats share one agent version, so the exact `agent_version` match of a
     standing grant still applies to bound chats. An ADR-0119 chat-model
     variant copies the metadata, so a bound chat on another model gets the
     same budget.
   - A chat created before this change pinned a version without the key. It
     keeps the ordinary limits. Only new bound chats get the budget.
3. **The existing run-limit rules still hold.**
   - Each reserve is strictly below its total (2 < 120, 4 < 160, USD 3 <
     USD 30). `RunLimits` enforces this, and startup builds the overlay so a
     bad value fails there.
   - Startup also rejects an overlay count below the matching `run_defaults`
     count.
   - The ADR-0078 and ADR-0115 rules are unchanged: a batch is fitted to the
     remaining budget, a run that runs out of tool calls still gets to
     answer, and the final-answer reserves still apply. A model call that
     reaches the model-call or cost reserve and asks for a tool ends the run
     with `budget_exceeded`. In a lesson almost every call asks for an
     action, so at most 117 calls can act, and a lesson that outlasts its
     budget ends failed rather than with an answer. The owner continues it in
     a new run.
   - Schedules, delegated child runs, surfaces, device-ingested turns and
     typed email work keep their own limits.
4. **A bound Chat always defines `browser.navigate`, `browser.observe` and
   `browser.act`.**
   - In a bound session the planner treats the three browser tools as
     required. They take definitions before every other candidate, and neither
     the agent's deferral list nor overflow can move them to the index.
   - A plan that cannot define them fails when the plan is built.
   - In the production-shaped roster, a bound chat moves three discovered
     reads from definitions to the deferred index:
     `mcp.gmail_work_read.get_thread`, `list_labels` and `search_threads`. They
     can still be called through `tool.call`, and nothing is skipped. Unbound
     chats do not change.
   - A new gate test enforces this, beside the ADR-0123 roster gate.
5. **Pages settle before they are observed.**
   - After navigation, the provider waits until the network has been idle for
     500 ms, then until the document has had no DOM mutation for 300 ms.
   - After an action, it waits for DOM quiet. If the action started a
     main-frame navigation, it first waits for the new document to load.
   - Each wait ends after 2 seconds at most.
   - Settling never fails a call. A page that is still changing at the limit
     is observed as it is.
6. **`navigate` and `act` return the settled page.** The contract already
   returns a `BrowserObservation` from `act`. That observation is now taken
   after the page settles, so its revision and element references are current.
   The tool descriptions tell the model it may act again on the returned
   revision without calling `observe`. No schema or version changes.
7. **New evidence restarts the identical-call count, a bounded number of
   times.** Engineering plan Section 12.5 says to fail a repeated call that
   brings "without new evidence". This decision implements that rule.
   - A tool may attach an evidence key to a successful result. The platform
     computes it, and the model never sees it. It is never part of a stored
     result.
   - If an identical call returns a different key from the one recorded for
     that call, its count restarts at one.
   - The browser tools compute the key from the page they return: the URL,
     title, text, and each element's role, name and state. The key leaves out
     the revision and element references, which are random for every
     observation.
   - Re-observing a page that changed is therefore not a loop. Five identical
     calls that keep returning the same page still fail the run.
   - **Restarts are capped at 32 in a run,** counting every call together. A
     restart counts only when it lowers a count. After the cap the plain
     count applies, so a page that changes on every observation allows at
     most 36 identical observations in a run before the breaker fails it. The
     cap is a code constant, not a configuration knob, because raising it
     weakens loop detection.
   - Tools without an evidence key keep the plain count, so the breaker's
     threshold and evaluation case 11 do not change.
8. **Hidden elements never take an element slot.** The observation checks the
   visibility of up to 4,096 candidate nodes in one browser round trip. It then
   keeps the first 256 visible ones in document order, as the limit in
   `browser-automation.md` specifies. It releases every handle it does not
   keep, and the handles of the observation it replaces. The existing
   visibility check for each element stays.
9. **The request names the origins that `browser.navigate` accepts.**
   - When a plan offers `browser.navigate`, the plan records those origins.
     In a bound session they are the profile's allowed origins. With a
     deployment-wide profile they are the configured origins.
   - Each request's runtime metadata row, which has `PLATFORM` trust, then
     carries `browser_origins=`. At most 16 origins are rendered.
   - The row is outside the cached prefix, so no prefix hash or builder
     version changes, and existing plans render as before.
   - The origins are normalized profile configuration. They never include the
     profile id, a name or a credential.

Decisions 1 to 4 are Part 1 and decisions 5 to 9 are Part 2. Each part builds
and lands alone on the development branch. Nothing goes to `main` until the
whole lesson path is built and verified (see Validation).

## Consequences

- **Budget:** a bound chat can run a whole lesson. About 100 actions use
  about 110 of the 117 model calls that may still act. A lesson that needs
  more ends with `budget_exceeded` and continues in a new run. A run on a
  model whose price is known has a cost limit for the first time.
- **Grants:** the default agent's version changes, because its metadata
  changes, wherever the browser is enabled. Standing browser grants pinned to
  the previous version stop matching, as they do after any agent change, and
  must be created again. Deployments with the browser disabled keep their
  agent version.
- **Configuration census:** ADR-0130 adds five knobs to the census `dev`
  declares when it lands, and five to `runtime/limits.yaml`. On `dev` at
  `882889ba`, after ADR-0131, that is 185 to 190 in total and 60 to 65 in
  `runtime/limits.yaml`. Whichever branch lands later recounts.
- **Run view:** `GET /v1/runs/{id}` shows `limits.max_cost_usd` of `"30"` for
  bound runs.
- **Deployment:** the browser-profile service runs `PythonPlaywrightRuntime`,
  so decisions 5 and 8 take effect in hosted mode only after its image is
  rebuilt. The application release already rebuilds it.
- **Anthropic models:** the planner also asks for the rolling history cache
  window (`context-engine.md`, "The history cache window"), so a lesson on
  Claude Fable caches its history too and costs about the same as the
  estimate above: about USD 12 for a full 120-call run.
- **Wall-clock time:** a 120-call lesson takes about 12 to 20 minutes,
  including up to 2 seconds of settling per action. The lease's hour
  (ADR-0127) does not bound the run: past it the next browser call gets a
  fresh browser and the run continues. The count and cost limits bound the
  run. No run deadline is added.
- **Loop detection:** the breaker now follows the plan's "without new
  evidence" rule. A page that keeps changing can extend an observation loop by
  at most 32 restarts in a run, and every run is still bounded by its counts
  and its cost limit.
- **Gates and tests:** no hard gate is registered and the milestone gate
  counts do not change. The tests are listed under Validation.

## Alternatives considered

- **Raise `run_defaults` for every chat.** The owner kept other chats as they
  are, and every chat would be exposed to lesson-sized costs.
- **Bind a limits variant of the agent to each bound session, as ADR-0119
  does for models.** A variant has its own id and version, so a standing
  grant, which matches `agent_version` exactly, would never apply to a bound
  chat.
- **Store the limits in session metadata, or read them from the
  configuration when a run is submitted.** Reserved metadata holds trusted
  bindings, not budgets. Reading the configuration at submission would change
  the limits of existing chats whenever it changed, and the agent version
  would no longer pin them.
- **A daily cost cap for bound sessions**, on the pattern of
  `scheduling.daily_cost`. Not adopted now. The owner asked for a cost cap per
  run; every bound run starts from an owner message, and ADR-0129 caps each
  task grant at thirty minutes and two hundred actions. It remains an
  optional later defence in depth.
- **Size the cost reserve for two worst-case calls** (about USD 6.10 on
  Astra). The final answer would then start at about USD 23.90 and the cost
  limit would bind before the counts in an ordinary lesson. A worst-case call
  needs a full, uncached 272,000-token context, which a lesson does not
  produce.
- **Raise `identical_call_threshold`, or exempt `browser.observe` from the
  breaker.** A higher threshold weakens loop detection for every tool. An
  exemption lets a loop on an unchanged page run until the budget is spent.
- **Cap restarts per call rather than per run.** A per-run cap is stricter
  and needs one counter.
- **Add the page revision to the fingerprint.** The revision is random for
  each observation, so the breaker would never trip on `observe`.
- **Derive the revision from the page content.** Two identical-looking pages
  would share a revision. That weakens the stale-reference defense that the
  revision exists for.
- **Wait a fixed time after every action.** Every action would pay the full
  delay. A settle condition returns as soon as the page is quiet.
- **A batched `act` that takes several actions on one revision.** It would
  halve the model calls for word-tile exercises. It also changes the revision
  contract and the per-action grant check, so it is left for later.
- **Give each session its own `browser.navigate` description, or put the
  origins in the prefix.** Plans pin copies of tool specs, and the prefix hash
  and builder version would change. The runtime row gives the model the same
  fact at no cost to the prefix.
- **Keep the browser tools' place through list order alone.** It holds
  today, but any configured tool added ahead of them would move them.
  Treating them as required states the guarantee and lets a test check it.

## Validation

- **Delivery.** Production deploys only from `main`: `deploy-app`,
  `deploy-nginx` and `apple-testflight` run only there. The owner chose to land
  this work on a development branch only, and to open the `main` pull request
  once the whole lesson path (ADR-0127 to ADR-0130) is built and verified. Until
  then agents verify with automated tests.
- **Red tests first.** Every behaviour change starts with a failing test.
  The required coverage:
  - Configuration: the overlay's values, the census grows by five, and
    startup refuses a reserve at or above its total and an overlay below the
    defaults.
  - Limit selection: a bound chat gets the overlay; an unbound chat, a chat
    pinned before the overlay, and a custom agent do not; a bound chat on
    another chat model keeps it; a delegated child of a bound chat gets child
    limits; the run view shows `"30"`.
  - Cost limit: a bound run whose calls cross USD 30 stops after that one
    call with `budget_exceeded`.
  - Roster: the required browser definitions under a small item cap, and the
    production-shaped roster gate.
  - Breaker: a changing page is not a loop; an unchanged page is; restarts
    stop after 32; a restart also happens on the batch completed after an
    approval; the evidence key never appears in a stored result or event.
  - Provider: settling after navigation and after an action, a page that
    never settles, a navigation that destroys the page context, hidden nodes,
    released handles, and an act that follows an act on its returned
    revision, on both the Playwright runtime and the hosted provider.
  - Origins: a bound request names the profile's origins; an unbound one
    names none.
- **Real browser, locally.** A test drives the real `PythonPlaywrightRuntime`
  in Chromium against synthetic lesson pages served inside the test: a page
  that loads its exercise after `DOMContentLoaded`, a page with 300 hidden
  controls ahead of 5 visible ones, a click whose result appears after 250 ms,
  a page that never stops changing, and a link that loads a new document. It
  is skipped where Chromium is not installed, which includes CI, so the
  completion report records its local run.
- **No new public surface.** No route, request field or scope is added. The
  run view's existing `limits.max_cost_usd` field gains a value, and a gate
  test reads it.
- **Owner acceptance in production** happens after the `main` pull request
  merges and the macOS TestFlight build ships. In a new bound chat, one
  Duolingo lesson runs with one approval. The run is then read through the
  API: model calls, tool calls, input, cached input and output tokens, and
  cost, compared with the estimates above. If model calls exceed 100, or the
  cost on Astra exceeds USD 20, the overlay is revisited with the owner.
