---
title: Changelog
---

# Changelog

## 2026-07-25 — Bootstrap, configuration, and the composition root

- Added `docs/plan/bootstrap-and-composition.md`, the design for the process
  that constructs everything the other nine specs describe. Recorded as
  ADR-0024. Nine specifications described nine mechanisms; none described the
  interval before any of them runs.
- Gave **configuration a shape**. The corpus declares 106 knobs and the plan
  names three environment variables. A value is an environment variable if and
  only if it differs between two deployments of the same revision and cannot be
  committed — which leaves eight fields in `Settings` and puts the rest in six
  committed YAML files, each beside the package that reads it, with an optional
  operator overlay directory merged over them.
- Fixed the reading of "New configuration appears in `.env.example`" that would
  have made all 106 knobs environment variables. That reading contradicts
  `policy_version`, which is a hash of the files the rules came from: an
  environment variable that changed an effective rule would leave the hash
  untouched, turning the audit trail from stale into false. **The environment
  never overrides a file; it is interpolated into one at named points**,
  generalizing the `model: ${OPENAI_MODEL}` form Section 10.5 already uses.
- Introduced `DATABASE_URL`, which no document named, for the PostgreSQL
  instance the plan makes the source of truth.
- Assembled **startup order**, stated seventeen times across nine documents and
  composed nowhere. Five phases, ordered by what each may touch: refuse
  (settings only), determinism (`Clock` and `IdFactory`, before anything reads
  ambient time), resources (a session factory, never a session), freeze (every
  versioned asset loaded, hashed, and made immutable, hardline first), and wire.
  All seventeen constraints land in one of the five, and the three that read
  like startup constraints and are not — migrations, provider pinning, contract
  coverage — are named as such.
- Specified `build` as one async context manager whose `Composition` exposes
  application services and nothing else. No adapter, repository, or session
  factory is reachable from an entry point, which is how ADR-0023's reservation
  of `RunRepository.transition` to `runtime/executor.py` survives a second one.
- Gave **Milestone 1's three bodiless bullets** bodies. "In-memory
  repositories" is five adapters in `adapters/persistence/memory.py`, run
  against the same contract suites as their PostgreSQL counterparts rather than
  living under `tests/` as doubles; there is no in-memory `RunQueue`, because
  that port's entire content is `FOR UPDATE SKIP LOCKED` and lease fencing.
  "Inline run dispatcher" is a one-method `RunDispatcher` whose postcondition
  both adapters satisfy. "Minimal context builder" is context-engine
  build-sequence step 1, with its stability test stated as two assertions
  rather than one.
- Resolved **two milestone conflicts** by separating two words each. An event
  *repository* is Milestone 1 and append-only event *storage* is Milestone 2 —
  one port, two implementations. The transaction-hygiene *check* is a Milestone
  0 deliverable and the *gate* is a Milestone 2 criterion, because Milestone 0
  has no database code to walk.
- Completed the **Section 17 CLI contract**: arguments, options, stdout versus
  stderr, six exit codes, and the milestone each command first works at. `get`,
  `events`, and `cancel` are reserved words after `agent run`, with `--` as the
  escape, which is what makes `agent run "get the weather"` decidable.
- Specified the **secret scanner** dependency rule 12 and Milestone 0 both name
  and neither describes: five rule families, a report that never prints what it
  matched, an allowlist whose entries require prose, and `.env.example` scanned
  rather than exempted.
- Added four static checks to the Milestone 0 import-boundary walk, each true
  of an empty repository and still true as it fills: `bootstrap` is imported
  only by the three entry points, no module outside `bootstrap.py` instantiates
  an adapter, no module outside `adapters/determinism.py` reads ambient time or
  generates an identifier, and no `AsyncSession` exists at module scope.
- Annotated the Section 4 tree rather than redrawing it: eleven files added,
  one name retired. `runtime/engine.py` becomes `loop.py`, `executor.py`, and
  `supervisor.py`; `ports/` gains `context.py`, `memory.py`, and
  `determinism.py`; `adapters/models/` gains the Anthropic, OpenAI-chat, and
  local-endpoint adapters ADR-0002 and ADR-0012 require and the tree never
  listed.
- Recorded that **`ModelProvider` was declared twice** with incompatible
  shapes, in Section 7 and in `model-gateway.md`, and made the gateway's
  version canonical. Section 7's is annotated as superseded and left in place,
  the same treatment `RunRepository.claim_next` received.
- Fixed a rendering defect in **Section 5**: the Pydantic note between rules 1
  and 2 split the ordered list, so the fourteen dependency rules rendered as
  1 and then 1 through 13 — meaning every reference to "rule 14" in the
  corpus pointed at a line the reader saw numbered 13. The note is now
  indented as continuation text under rule 1, which is the rule it is about.
  No rule text changed. A sweep of all forty-one built pages found no other
  interrupted list.
- Sixteen further judgment calls are recorded with their reasoning and reversal
  cost in [questions-for-review.md](status/questions-for-review.md).

No product implementation was performed.

## 2026-07-25 — Cross-document defect sweep before the readiness review

- Read the nine detailed-design specs against each other rather than against
  the plan, looking only for places where two documents state the same fact
  differently or where one names something no document declares. **Ten defects
  were found and all ten are fixed.** Each is recorded with its reasoning in
  [questions-for-review.md](status/questions-for-review.md).
- Declared **`RunStatus`** — seven members — which five documents referenced and
  none defined. A run blocked on a child waits in `WAITING_FOR_USER` with a
  suspension record naming the child, rather than in an eighth state that only
  the suspension record could distinguish.
- Added the four **`runs` columns** the domain model always implied and the
  schema never carried: `tenant_id`, `agent_id`, `agent_version`, and a nullable
  `deadline_at` with a partial index restricted to the three live statuses.
- Resolved **`tool_invocations.origin_trust`**, declared nullable by
  `tool-system.md` and `NOT NULL` by `policy-and-approvals.md`, in favour of
  `NOT NULL`. A call the runtime issues itself carries `PLATFORM`. A nullable
  column would let an authorization record say "policy did not compute this",
  which is the one thing it must never be able to say.
- Renamed the classification column to **`idempotency_class`**, because the same
  table already carries `idempotency_key` and crash recovery reads both — one to
  decide whether replay is safe, the other to decide whether it is the same call.
- Corrected the **step identity** claim: `step_number` is canonical on
  `model_calls`, not on `tool_invocations`, because a step that proposes no tool
  calls writes no invocation row and would therefore be skipped by any numbering
  taken from that table.
- Completed the **event catalog** at fifty-one types. It had gone stale at the
  twenty-four of Section 6.8 while five later specs added to it, including seven
  MCP and bridge events and six memory events. Stating the total makes the next
  addition visible.
- Fixed three assertions that name things which do not exist: the harness case
  `approval_granted_resumes_run` now asserts `run.queued` and
  `approval.resolved`, and the gate id standardises on the registry spelling
  `gate.policy.prompt_is_not_authorization`.
- Recorded, without deleting them, that **`RunRepository.claim_next` and
  `heartbeat` are superseded** by `RunQueue.claim` and `RunQueue.heartbeat`. The
  signatures stay in Section 7 as the record of what was replaced.
- Corrected two cross-references in `model-gateway.md` that cited Section 10.3
  for the normalized request; it is Section 10.1.
- Brought every fenced code block in the corpus within the rendered code column.
  Verified by measuring `scrollWidth` against `clientWidth` for every `pre` on
  all thirty-nine built pages rather than by counting characters: **zero blocks
  overflow.** The first-line allowance was re-measured too — the copy control in
  the current Material release sits in its own navigation bar, so a first line
  has the same budget as any other line, not a shorter one.

No product implementation was performed.

## 2026-07-25 — The runtime loop, the step, and the single terminal writer

- Added `docs/plan/runtime-loop.md`, the design for Sections 12 and 13 and the
  loop-facing halves of 6.4, 6.5, 6.9, 14.1, 14.2, 16, 19, 26, and 27. Recorded
  as ADR-0023. This is the last of the eight specs and the seam the other seven
  are called from.
- Established the finding that drives the document: **Section 12.1's forty-two
  lines name eleven callables and not one of them is a declared port.** Section
  7 declares eight port Protocols; the only one the loop touches is
  `context_builder.build`. Each of the other eleven names is a seam where two
  specifications' requirements meet, and at most of them the two disagree.
- Found three places where the loop as written **cannot do what another document
  requires of it.** It cannot resolve its own agent, because line 1408 reads
  `agents.get_version(run.agent_id, run.agent_version)` and Section 6.3 puts
  both fields on `Session`. It cannot suspend, because line 1437 returns bare
  while Section 27.2 requires the lease released, a checkpoint written, and an
  event emitted — so a run paused for approval holds its lease until expiry, at
  which point the queue hands it to a second worker. And it cannot compact,
  because Section 11.4 requires compaction, Milestone 7 gates it, and no
  pressure measurement or compactor call exists anywhere in the corpus.
- Split the runtime into a **`run_loop` that computes a typed `RunOutcome`** and
  a **`finalize` that performs every terminal action once**. The suspension
  defect is not fixed by adding a `finally` to the flat loop, which leaves the
  next `return` free to reopen it; it is fixed by moving the ending somewhere a
  `return` cannot skip. A structural gate asserts that `RunRepository.transition`
  and `RunQueue.release` are reachable from exactly one module.
- Defined **`Step`** as a runtime value object with a persisted identity — one
  additive column, `model_calls.step_number` — rather than as a counter or a
  table. Steps that produce no tool calls are currently invisible; they are the
  ones a terminal turn is made of.
- Added **nine fields to `Run`**, six of which the event-log spec already
  introduced as columns and never reflected in the domain model, and three of
  which — `agent_id`, `agent_version`, `deadline_at` — are denormalized so the
  run is self-describing and the deadline is indexable by a sweep.
- Gave **compaction a call site**: `build_with_pressure`, which measures, invokes
  the compactor if the body will not fit, adopts the checkpoint it returns, and
  measures again, capped at two per step with `ContextOverflow` permanent on the
  third. `build()` stays pure, which is what the byte-identical rebuild on the
  step-retry path depends on.
- Resolved **budget** into three scopes — run, step, attempt — and ruled that
  Section 6.5's "after" means *record*: usage is recorded and the limit is
  evaluated in one transaction, because splitting them opens a window in which
  the run is over budget and nothing knows.
- Made the **heartbeat a supervisor task** rather than a statement in the loop,
  running at a third of the lease interval and watching three things that are
  all "has the outside world changed its mind": the lease, the deadline, and a
  cancellation request. `heartbeat` returns `bool`; `False` means fenced. Every
  non-append write the loop makes is guarded by
  `WHERE lease_epoch = :lease_epoch`.
- Specified **cancellation** as one token per run with six observation points
  shared by the loop, the tool executor, and the sandbox, and one rule about
  effects: a cancellation observed after a call's `effect_sent_at` watermark is
  set completes the disposition rather than abandoning a half-sent side effect.
- Resolved the **checkpoint's two descriptions** as two types rather than a
  contradiction: Section 6.9's inline `conversation` is what the repository
  returns, and event references and deltas are what it persists. Added the six
  triggers, the `full` rule the call site can evaluate, `checkpoints.full` and
  `checkpoints.base_version`, and `seed_checkpoint` as a function with two call
  sites so a run whose checkpoints are all deleted still reaches the same
  terminal state.
- Specified the **resume ladder**: resumption is a cold process start and a warm
  pipeline entry. The worker claims a `QUEUED` run from scratch; the pipeline
  re-enters at step 6 for each pending call, so a call whose watermark was set
  is not re-executed. `run.resumed` is emitted whenever the execution did not
  start the run, which covers all four paths without enumerating them.
- Ruled that an **empty terminal model turn is a failed step**, retried and
  failing the run with `EmptyModelTurn` on exhaustion, rather than a completed
  run whose final message is empty.
- Added **`FailureReason`**, fourteen values, so a `FAILED` run distinguishes a
  provider outage from an exhausted budget from a policy denial, and named the
  owner of every retry: the adapter's transport retries, the gateway's attempt
  loop, and the runtime's step retry, split on `stream_had_output`.
- Listed and resolved **twenty cross-document contradictions** by number, naming
  the losing side in each. Three needed argument: `RunUsage` has five token
  classes and the event log's "unchanged in shape" is about the rollup
  relationship; Section 6.8's `tool.call.*` names are canonical and the
  evaluation-harness case asserting `tool.proposed` / `tool.authorized` /
  `tool.succeeded` is corrected in that document; and Section 27.5's "reject or
  queue" resolves to reject with HTTP 409, because ADR-0004's partial unique
  index makes queueing impossible at the database level.
- Added fourteen hard gates, twenty-five decisions, the events consolidation
  that introduces no new event type and assigns owners to `run.claimed` and the
  two `run.waiting_*` events, the three-role deployment shape with the sweeps
  under advisory locks, and the transaction boundaries of every write the loop
  makes.
- Wired the nav, the ADR index, and additive cross-references from Sections 12,
  13, and 27 and from Milestones 1, 2, 4, 5, and 7. No requirement was
  rewritten: Section 13's retry table keeps every row and its retryability, the
  Section 8.3 pipeline steps are not reordered, Section 7's `build()` signature
  is unchanged, and 27.1's definitions — including that a turn has no domain
  object — stand.
- Recorded further decisions in `docs/status/questions-for-review.md` and six
  open questions in the spec, of which the two with the highest reversal cost
  are whether cancellation ships in three milestone slices and whether a
  child-run wait deserves its own run status.

No product implementation was performed.

## 2026-07-25 — The evaluation harness, the gate mechanism, and ADR-0001

- Added `docs/plan/evaluation-harness.md`, the design for Section 20 and for the
  gate sections the seven sibling specs close with, plus the parts of Sections
  3, 4, 10.3, 19, 21, 22, and 31 they depend on. Recorded as ADR-0022, with the
  boundary-enforcement half recorded as ADR-0001.
- Added `docs/adr/0001-modular-monolith.md`, closing the numbering gap the ADR
  index had been apologizing for. Section 4's repository layout names the file
  and twenty ADRs referred to a decision documented nowhere. It records the
  modular monolith as accepted, defines replaceability as a port with a contract
  suite rather than as a service boundary, and resolves Section 5's *"where
  practical"* rule by rule: eight of the fourteen dependency rules by import
  graph, four by other static checks, one by the secret scanner, one by
  adapter registration, and two residues named as not mechanically checkable
  with their compensating controls.
- Established that **Section 20's harness cannot run the gates six specs assign
  to it.** The specs declare roughly forty-nine hard gates and the policy spec
  names Section 20 as their enforcer; Section 20's sixteen assertion types can
  express perhaps eight. Sorting the declared gates by kind gives the finding
  that drives the document: **roughly a third of them are not case gates**, so a
  case-only harness reports a green build with a third of the plan's stated
  invariants unchecked.
- Defined a **hard gate** as a named, executable, milestone-attached condition
  that fails the build, and gave gates a checked-in **registry** — one YAML file
  per spec area, carrying the identifier, milestone, kind, a verified link back
  to the declaring document, the prose statement, and the check that implements
  it. A gate declared in a spec but absent from the registry fails the docs
  build; a gate at or past its milestone may not skip.
- Added the three gate kinds the case format cannot express: **property** gates
  with a generator, a predicate, a minimum trial count, and a recorded failing
  seed; **corpus** gates with a `minimum_members` floor so they cannot pass
  vacuously; and **structural** gates that never run the agent. The last two
  need no runtime and are Milestone 0 work.
- Defined **"deterministic"**, which the plan asserts and never defines, by
  naming its seven sources and their treatments: the clock, identifiers, and
  model output are pinned behind ports; batch concurrency and database row
  order are ordered by explicit keys; retry timing is bounded; hash iteration
  order is accepted and never asserted on. Stated the limits as plainly — the
  runtime is not deterministic, payloads are not byte-identical, and the
  deterministic scheduler proves the parallel path produces the right result,
  not that it is race-free.
- Resolved `model_fixture`, which Section 20.1 names with a bare string and
  nothing defines: it resolves to `evals/fixtures/models/NAME.yaml`, validated
  at collection against the current `FakeModelScript` shape rather than at run
  time. Recording is an explicit command and never a side effect of running the
  suite.
- Added **`interventions`** to the case schema — approve, deny, cancel, kill the
  worker, answer, disconnect — without which the eight Section 20.3 cases that
  involve approval, restart, cancellation, or disconnect are unwritable. Every
  field Section 20.1 already had is unchanged.
- Made **"no unauthorized side effects"** decidable. It was the last assertion
  type in Section 20.2 and it asked the harness to prove a negative about a
  system it does not control. It is now asserted against ADR-0021's
  `tool_invocations.effect_sent_at`: an empty `expected.effects` list means no
  invocation in the run set a watermark, and it is the default.
- Ruled that **there is no test mode.** No environment variable, no flag, no
  `if settings.testing` branch in the policy engine, the approval service, or
  the tool executor. Evaluation identity is data — a `tenant_eval` tenant under
  ordinary row-level security, named principals with real scopes, and policy
  profile files loaded by the same loader and subject to the same totality gate.
  An `approve` intervention calls the real approval service as a second
  principal rather than setting a status, and the structural form of the rule is
  that no module under `agent_core` outside `agent_core.evals` may import
  `agent_core.evals`.
- Bound **contract suites to ports rather than to implementations**, so the
  model gateway's second gate — the same contract passing against fake,
  recorded, OpenAI, Anthropic, and `chat_completions` — is one configuration
  line per adapter instead of five files that drift. A port with no contract
  module, or an implementation not registered against its port's contract, fails
  the build.
- Named **`resilience`** as the sixth test category. Section 20.4 lists five;
  Section 4's layout has six directories and two specs already place tests in
  the sixth. Added the routing rule that keeps "integration" from becoming the
  drawer everything slow ends up in, and stated that eval cases are not a
  seventh category.
- Gave every one of Section 20.3's twenty-five cases an **earliest milestone**
  and a statement of what only that case proves. Ten are writable in Milestone
  1, which is what makes "build evaluations before advanced features" a schedule
  rather than an aspiration. Split case 18 into **18a** and **18b** around the
  effect watermark, since only the second — asserting that the model is told the
  outcome is unknown rather than that the call failed — has a safety
  consequence.
- Specified the **capability track's** governance: scenarios have no assertions
  and default to five repeats; a judge is a model, prompt, and rubric versioned
  as one unit and pinned to a provider version, replaced only alongside a bridge
  run, never shown the rubric's weights, and never reusing an identifier after
  deprecation; cross-version score comparison is refused by the tooling. A
  regression is a distribution change — floor drops and policy failures block a
  release, mean drops inside a measured noise band do not — and **a capability
  improvement that increases policy failures is a regression.**
- Ruled that a scenario hitting a cost ceiling is **excluded from the score
  distribution rather than scored zero**, since it conflates "we stopped paying"
  with "the agent failed" in the direction that corrupts the distribution most.
- Designed the **trajectory-to-case conversion** that Section 31.3 asserts and
  nothing specified, and stated its lossy boundary field by field. Recorded tool
  results are replayed rather than re-executed; timestamps, identifiers, usage,
  and cost are discarded; redaction happens at export so there is one place the
  rule lives; and a converted case is marked `source: trajectory` and stays out
  of the blocking suite until a person writes its assertions.
- Added a **flake policy with an expiry**: retry once with the retry rate
  reported even while green, quarantine on a second failure within thirty days,
  and automatic release after fourteen days whether or not the test was fixed.
  Gates may never be quarantined.
- Added ten hard gates of the harness's own, ten build-order steps with the
  first two in Milestone 0, six metrics, four event families, two tables for the
  capability track and none for the deterministic suite, an eight-row
  import-boundary table, and ten failure modes with their mitigations. Gate 7
  asserts the definition of done's eighteenth item by running CI with egress
  blocked rather than by not configuring an API key.
- Wired the nav, the ADR index, and additive cross-references from Sections 2.1,
  5, and 20 and from Milestones 0 and 1. No requirement was rewritten: the
  twenty-five cases stay twenty-five, the sixteen assertion types stay and gain
  four, the capability track stays non-blocking, and Section 5's fourteen
  dependency rules are unchanged.
- Recorded seventeen further decisions in `docs/status/questions-for-review.md`,
  including the ADR renumbering that moves the runtime-loop record to ADR-0023,
  and five open questions in the spec.

No product implementation was performed.

## 2026-07-25 — The tool system, the execution pipeline, and MCP

- Added `docs/plan/tool-system.md`, the design for Sections 7, 8, 12.4, 12.5,
  15's `tool_invocations`, and Milestones 1, 4, 6, and 8: the execution
  pipeline, idempotency and recovery, output limits, control tools, the
  orchestration bridge, and the MCP adapter. Recorded as ADR-0021, constrained
  by ADR-0015, ADR-0008, ADR-0005, ADR-0006, ADR-0002, ADR-0013, and ADR-0020.
- Defined the two types the Section 7 `Tool` port names and nothing in the plan,
  the six sibling specs, or the twenty ADRs declared: **`ToolResult`** with
  `ToolFailure` and `ToolFailureKind`, and **`ToolExecutionContext`** with its
  sixteen fields. A tool returns `ok` and content, never a status, because
  status is the pipeline's judgement and a tool able to claim `denied` could
  launder a denial.
- Ruled that `ToolExecutionContext` carries no database session, no repository,
  no policy engine, and no registry, so Section 12.2's "no transaction across
  tool I/O" is enforced by construction and a tool cannot call a tool.
- Completed `ToolSpec` with `kind`, `target_kind`, `output_trust`, `source`,
  `server_id`, and `deprecated`, and added `ToolKind`, `ToolSource`, and
  `ToolOutcomeStatus`. `output_trust` is **forced** to `EXTERNAL_UNTRUSTED` at
  registration for every MCP, device, and sandbox source, which also resolves
  the plan's two conflicting defaults for `ToolResultItem.trust`.
- Gave the registry a **name grammar and a reserved-domain partition**, and
  named MCP tools `mcp.{server_id}.{normalized_remote_name}` by a deterministic
  five-step normalization. This is the join Milestone 8 needed and lacked:
  without it every discovered MCP tool is an unknown tool and Section 9.2's
  `Unknown tool -> Deny` row makes the milestone non-functional.
- Made the pipeline **fourteen steps, one function, one call site**, preserving
  Section 8.3's ten in order and inserting the four persistence points Section
  12.2 requires. Nothing before step 8 has touched the world; nothing after
  step 10 can undo step 10. The bridge, the device channel, and the MCP adapter
  re-enter this function rather than implementing variants, which is what makes
  ADR-0015's "the bridge is the enforcement point" true rather than aspirational.
- Added **`effect_sent_at`**, a nullable timestamp written immediately before a
  tool's first outbound operation, and turned the event-log spec's undefined
  word *ambiguous* into a fact a recovering worker can read. Section 8.4's rule
  against auto-retrying non-idempotent calls is preserved exactly for calls that
  may have escaped, while a worker that died during argument marshalling is
  retried instead of escalated. The honest limits — that a set watermark proves
  the effect *may* have happened, and that the false-positive case costs one
  human review — are stated in the document rather than left to be discovered.
- Derived the idempotency key from Section 8.4's five inputs plus a version tag
  and `tool_version`, and **excluded `attempt_number`** so a bounded retry
  reuses the key and the external service can deduplicate it.
- Defined where `argument_trust` and `origin_trust` come from, which the policy
  spec consumed and nothing produced. `origin_trust` is the minimum label over
  the request's context items; `argument_trust` defaults to
  `EXTERNAL_UNTRUSTED` and is only ever **raised**, on a verbatim
  sixteen-character match against `USER`-labelled context — a direction chosen
  so the failure mode is an unnecessary approval rather than an injection
  passing as trusted.
- Ruled that large output is **excerpted head-and-tail and artifactized, never
  summarized**, since a summarizer over untrusted tool output is the laundering
  ADR-0020 forbids, and that an excerpt never splits a trust envelope.
- Gave all four non-success outcomes **one six-field shape** with a stable
  `reason_code` and a fixed message, and ruled that external text is data and
  never narration: a remote system's error string is enveloped untrusted
  content, never the outcome `message`.
- Unified the policy spec's repeated-denial breaker and Section 12.5's loop
  detector into **one counter at three thresholds**, adding the rule that an
  invocation which resolved `UNCERTAIN` is never proposed again in the run.
- Defined a **step** as one model call plus the complete disposition of every
  tool call it produced, so a batch shares a `step_number` and a crash-retry
  derives the same keys. Parallelism admits or rejects the whole batch; a mixed
  batch runs sequentially rather than being split, because splitting requires
  the independence inference Section 12.4 warns against.
- Made control tools a declared kind that runs the full pipeline and suspends
  through a nullable marker rather than a new status, so no consumer of
  `tool_invocations.status` learns about suspension and the lease reaper
  excludes them by predicate.
- Specified the MCP adapter to one principle: **a server does not classify
  itself**. `side_effect`, `risk`, `idempotency`, and `required_scopes` come
  from operator configuration, an unclassified server is maximally restricted,
  tenant-configured servers are HTTP-only through the egress allowlist, and
  stdio servers — child processes in the worker's trust zone — are
  operator-configured only.
- Mapped MCP's other surfaces deliberately rather than naturally: resources
  become one synthetic per-server read tool instead of an automatic context
  source, prompts become read-only untrusted skills that never enter the
  cacheable prefix, and sampling and roots are declined at capability
  negotiation so a server never gets to spend a tenant's model budget.
- Extended ADR-0020's pinned-advertisement rule to MCP catalog changes, so
  `tools/list_changed` is recorded and not applied and an external server cannot
  invalidate a tenant's prompt cache at will.
- Gave the orchestration bridge a synthesized `call_id` from the script hash and
  a bridge-counted ordinal so a replayed script deduplicates, with the
  determinism caveat stated plainly, plus a bounded approval hold and a
  per-turn cap on underlying calls.
- Added fourteen columns to `tool_invocations` and two new tables,
  `mcp_servers` and `mcp_tool_catalog`, the second a history rather than a
  cache. Declined to add a `tool_registry_snapshots` table, since the context
  plan's pin already answers what a session advertised.
- Added seven event families, two span children, five metrics, an import
  boundary table, ten hard gates, and a nine-step build order that places most
  of the work in Milestone 1 rather than Milestone 8.
- Cross-referenced the new document from Section 8, Milestone 1, and Milestone
  8's MCP subsection, additively. No requirement in the plan was rewritten,
  weakened, or reordered.

No product implementation was performed.

## 2026-07-25 — Model gateway and the provider-neutral protocol

- Added `docs/plan/model-gateway.md`, the translation design for Section 10 and
  Milestones 1 and 3: the normalized stream, the two first adapters, routing,
  usage and cost, retries, and reasoning. Recorded as ADR-0002, constrained by
  ADR-0006, ADR-0007, ADR-0010, and ADR-0012.
- Defined the roughly twenty types the plan used at call sites and never
  declared: the five `ConversationItem` members, `ContentPart` with its text,
  image and file variants, `PendingToolCall`, the six streaming event classes,
  `ModelUsage`, `CostSource`, `StopReason`, `ModelAttempt`, the three error
  classes, `FakeModelScript` with `ScriptedTurn` and `ScriptedToolCall`, the
  `UsageRepository` port, and `ResolvedModel` with `ModelCapabilities`,
  `ModelLimits`, and `ModelPricing`.
- Stated the **six invariants of the normalized stream** — contiguous sequence,
  exactly one terminal event, contiguous ordered deltas per item, `call_id` and
  `name` known at tool-item start, advisory usage, and no raw provider error
  text or credentials on any event — and gave them a shared validator, so a
  violation is an adapter defect rather than a caller concern.
- Made **one shared assembler** fold events into turns for every adapter, which
  is what makes the contract suite a controlled comparison: the same code
  produces the turn on every provider, so a difference in the turn is a
  difference in the events.
- Resolved the **nine unfilled cells** of the Section 10.2 mapping table without
  changing any mapping it already states: `server_tool_use` becomes a protocol
  error, `content_block_stop` and `message_stop` are structural, `UsageEvent` is
  advisory so its missing OpenAI source stops mattering, `response.incomplete`
  maps to `MAX_TOKENS` where the cap is the cause, and the five OpenAI lifecycle
  events drive item bookkeeping only.
- Gave in-band `<think>` the mapping table ADR-0012 assumed and Section 10 never
  wrote, plus a streaming scrubber with one-token lookahead and a per-profile
  configurable tag pair, since open models do not agree on the delimiter.
- Gave `cache_creation_input_tokens` the home Section 10.2 said to find for it:
  `cache_write_input_tokens`, a **fifth tracked token class** on both
  `ModelUsage` and `RunUsage`. Made `reasoning_tokens` `None` rather than `0`
  where a provider does not itemize it, and moved the itemization question into
  pricing as `reasoning_priced_separately`.
- Added the **`ModelRouter` port**, turning `model_policy` from a bare string
  into a `ResolvedModel` carrying provider, model, capabilities, limits,
  pricing, and a credential reference. This gives `ModelCapabilities` a
  resolution path and gives the context engine's "8,192 or the model's default"
  output reserve its missing second half.
- Resolved **provider pinning against availability routing** temporally rather
  than architecturally: selection happens once at run start, the pin is absolute
  and persisted for the life of the run, and Milestone 10 routes selection and
  never live runs.
- Split **retry ownership on `stream_had_output`**: the adapter retries only
  before the first event reaches the caller, at most three times; after any
  output it fails and the caller decides. `max_attempts = 3` lives in
  application code, matching the worker's existing figure.
- Added the **model-call timeouts** no document carried — `timeout_seconds` and
  the load-bearing `stream_idle_seconds` — and made cancellation produce
  `StopReason.CANCELLED` on a partial turn rather than an error.
- Bounded the **`PLATFORM` trust default** on `ProviderReasoningItem` with four
  properties that leave the label no consumer able to act on it: the payload is
  never parsed, never rendered as prompt text, never reaches the policy engine,
  and never enters memory or a user-facing renderer. The name is still wrong and
  is raised for review rather than edited.
- Made the gateway enforce **tool call and tool result pairing before sending**,
  which the policy spec's denial-as-tool-result and the context engine's
  compaction atomicity both depended on and neither owned.
- Added `model_calls` and `model_prices` to the schema, one row per attempt and
  an append-only price history, so a three-month-old invoice stays reconcilable
  and per-attempt cost is a query rather than log archaeology. `runs.usage` is
  unchanged in shape and becomes a rollup maintained in the same transaction.
- Made **failed attempts count against budget**, checked before each attempt, so
  a crash-looping run stops for a stated reason instead of being mysteriously
  expensive.
- Added event payloads for `model.request.started` and `model.response.completed`
  that the context engine already consumed and Section 6.8 never specified, plus
  three telemetry attributes for the cached, cache-write, and reasoning token
  classes and a `model.attempt` span.
- Specified ten hard gates, six tracked metrics, and a fourteen-step build order
  in which the stream validator and the contract suite are written **before the
  first real adapter**.
- Wrote ADR-0002 with twenty decisions and seventeen rejected alternatives, and
  wired it and the spec into the navigation, the ADR index, and Section 10,
  Section 15, and Milestones 1 and 3 of the engineering plan.
- Recorded twelve judgment calls in `docs/status/questions-for-review.md`,
  flagging the `PLATFORM` trust default as the one worth reading first.
- No product implementation was performed.

## 2026-07-25 — Policy engine and approval lifecycle spec

- Added `docs/plan/policy-and-approvals.md`, the authorization design for
  Sections 8.3, 8.4, 9, 11.2, 13, and 22 and Milestone 4: classification, the
  deterministic matrix, the hardline layer, and the approval lifecycle. Recorded
  as ADR-0005 and ADR-0006, constrained by ADR-0017.
- Defined the five types the plan named as field types but never declared —
  `SideEffectClass`, `RiskLevel`, `IdempotencyClass`, `ProposedAction`, and
  `ApprovalStatus` — deriving each value from an existing plan statement rather
  than inventing a taxonomy: fifteen side-effect classes against Section 9.2's
  fifteen action categories, four idempotency classes against Section 8.4's four
  crash-recovery bullets.
- Rekeyed Section 9.2's matrix on `SideEffectClass` instead of a prose "action
  category" with no referent, and added a **Condition** column so the three cells
  holding non-enum decision strings resolve: "Allow with restrictions" and "Allow
  only in sandbox" become a guarded `ALLOW`, "Deny initially" becomes `DENY` in
  the `default` profile. **No outcome in the matrix changed.**
- Made the evaluator a **pure function** — `evaluate_deterministic(action,
  principal, run, ruleset)` — with time passed in as `evaluated_at` and never
  read from a clock, so the same inputs produce the same decision on replay.
- Established restrictiveness as a **total order combined by `max`**: `ALLOW` <
  `ALLOW_WITH_MODIFICATIONS` < `REQUIRE_APPROVAL` < `DENY`. Section 9.1's "more
  restrictive wins" was undefined across four decision types; it is now a
  computation. Hardline is not a rank in that order but a short-circuit.
- Located the hardline rules that Section 9.3 required but never placed:
  `src/agent_core/policy/hardline.yaml`, packaged, frozen at import, and
  deliberately **not** behind a port — a port implies substitution, and these
  are the rules that must not be substitutable. Every rule carries a mandatory
  `near_miss` it must permit, so an over-broad pattern fails its own test.
- Defined `policy_version`, which `ContextPlan` already consumed and nothing
  produced, as a **content hash** of the profile plus the hardline file
  (`default@3f2a1c9d4e5b+h7c1e0a92`), making rules version-controlled files
  frozen per process rather than rows in a table.
- Generalized approval beyond tool calls with `ActionKind` (tool call, memory
  write, skill authoring, artifact export), since `approvals.tool_invocation_id`
  being `NOT NULL` made the other three structurally unapprovable.
- Gave `approvals` the `tenant_id` and `principal_id` that Milestone 4's
  cross-tenant rejection criterion requires, three indexes, a unique index for
  one open approval per action, and a **guarded resolution** that is
  first-writer-wins: a second caller agreeing gets 200, a second caller
  disagreeing gets 409, and a cross-tenant caller gets not-found rather than
  forbidden.
- Specified the shape of "denial becomes a structured tool result" as a **field
  allowlist** enforced by test, with a repeated-denial circuit breaker at three,
  so a denial teaches the model to stop without teaching it what to evade.
- Mapped Section 22's three trust tiers onto Section 11.2's seven trust labels,
  with only `PLATFORM`, `TRUSTED_CONFIGURATION`, and `USER` able to authorize.
- Added `GET /v1/approvals` and `GET /v1/approvals/{id}`, without which Section
  17's `agent approval list` had no endpoint to call.
- Added ten hard gates, six tracked metrics, and a twelve-step build order to
  Milestone 4, keeping the advisory layer sequenced after Milestone 6.
- Wrote ADR-0006 as **already amended by ADR-0007**, so the distinction between
  "never persist reasoning" and "may hold provider-opaque continuation in the
  checkpoint for the life of a tool loop" lives in the record rather than in one
  trailing sentence of Section 11.4.
- No product implementation was performed.

## 2026-07-24 — Event log and persistence spec

- Added `docs/plan/event-log-and-persistence.md`, the persistence design for
  Sections 6.8, 6.9, 12.2, 14, and 15 and Milestone 2: the append transaction,
  projections, checkpoints, and the run queue. Recorded as ADR-0003 (amended for
  payload versioning) and ADR-0004.
- Stated the layer's contract as **observation, not durability** — a committed
  event no projection ever observed is, to every consumer, an event that did not
  happen — and wrote the hard gates against that definition.
- Identified a **silent missing-write hazard**: two appends take sequences 5 and
  6, the transaction holding 6 commits first, and a projection polling in that
  window advances past 5 and never sees it. The log stays consistent, `UNIQUE` is
  satisfied, and every rebuild reproduces the loss identically.
- Resolved it by establishing that Section 27.5's **one active run per session is
  load-bearing for projection correctness**, not only for contention, enforced by
  a partial unique index, with snapshot-aware watermarking
  (`pg_snapshot_xmin`) specified as the companion change required if that default
  is ever relaxed.
- Made **sequence gaps legal**: a rolled-back append burns its number, so
  consumers read after a watermark and never wait for a specific next sequence.
- Established `LISTEN`/`NOTIFY` as a **latency hint, never a delivery
  guarantee** — it is transactional, so no outbox is needed, and at-most-once, so
  every consumer polls from a watermark first.
- Added `events.payload_schema_version`, required by Section 6.8 and Milestone 2
  but absent from Section 15, together with **pure, total upcasters** that may
  never invent a value, and made an unknown higher version a hard error.
- Gave projections four properties — deterministic, watermarked, rebuildable,
  never authoritative — with state and cursor written in one transaction, and
  gave **derived events deterministic derivation keys** so rebuilds converge
  instead of multiplying their own output.
- Made checkpoints **deltas against periodic full snapshots** with the
  conversation stored as event references, and made *losing checkpoints costs
  time, not information* an executable test.
- Added claim **priority classes** (interactive, async, maintenance) with
  capacity reserved per class rather than aging, which would make latency depend
  on queue history.
- Made every worker write **fenced by `lease_epoch`**, since lease expiry is a
  guess: a zero-row update means stop, not retry, and `heartbeat` returns `False`
  when fenced rather than raising.
- Specified queue-level retry that the plan lacked entirely — only lease expiry
  requeues, `max_attempts` is 3, `runs.failure` is the dead letter — and added
  the `idempotency_keys` table that Section 16 and Milestone 2 both assume.
- Added seven hard gates and five tracked metrics to Milestone 2, four ports
  (`CheckpointRepository`, `ProjectionCursor`, `Projection`, `RunQueue`), and
  four event types.
- Created `docs/status/questions-for-review.md` recording every decision taken
  without review during the plan-completion run, with its reasoning, alternative,
  and reversal cost.
- No product implementation was performed.

## 2026-07-24 — Context engine spec

- Added `docs/plan/context-engine.md`, the assembly design for Section 11 and
  Milestone 7: the cache boundary, the budget allocator, compaction, trust
  rendering, and the working-state lifecycle. Recorded as ADR-0020.
- Split context into **two regions with one membership rule** — if a value *can*
  differ between two requests in the same session, it is not in the prefix.
  Membership is a property of item type, declared in code and asserted at assembly,
  so the current date cannot reach the prefix by looking like configuration.
- Made prompt stability **enforced rather than assumed**: `prefix_sha256` is
  recorded on every request, and a scripted fifty-turn session crossing midnight
  with a revoked tool, corrected memory, and a forced compaction must yield exactly
  one hash. Added **prefix epochs** for changes that cannot be absorbed, with
  epochs-per-session tracked against a target of 1.0.
- Pinned the tool set at session open and moved revocation to call-time policy
  denial, so a permission change does not rewrite the prefix or leak into cache
  timing.
- Gave `ContextBudget` a sizing rule: **only history scales with the context
  window**; every other class is capped absolutely, because prefix content is
  attention paid on every request. The prefix never yields — a class over its
  ceiling fails the session at open rather than truncating the system prompt.
- Fixed the yield order under pressure as in-turn recall, then tool-result
  truncation to typed pointers, then compaction, and made tool call/result pairs
  **atomic budget units**.
- Separated purity from compression: **`build()` is a pure function and compaction
  is a checkpoint write**, which is what makes retries safe and the byte-stability
  gate meaningful.
- Established that **untrusted content is elided, never paraphrased** — summarization
  is a trust-label laundering vector — with typed pointers retaining label, size,
  and reference, and a summary-depth cap of 2.
- Added the nonced trust envelope with delimiter escaping, and the typed
  `context.update_working_state` control tool with per-field carry rules across turn
  boundaries, bounded lists, and constraints that never evict.
- Handed `established_facts` to memory formation as candidates subject to every
  eligibility gate, giving the write path a second input rather than a bypass.
- Added five hard gates (determinism, prefix stability, budget conformance,
  tool-pair integrity, trust preservation) and four tracked metrics to Milestone 7.
- No product implementation was performed.

## 2026-07-24 — Session snapshot budget decided

- Closed the last retrieval open question. The session-open snapshot is capped by
  **item count first and tokens second**, never by a pure percentage of the context
  window: dilution tracks the absolute number of irrelevant items, so a larger window
  is not a reason for a larger snapshot. The percentage survives only as a ceiling.
- Set the starting `core` budgets: **40 items / 1,500 tokens** for interactive
  sessions, 80 / 3,000 for long-running async runs that amortize one block over many
  requests, and 15 / 500 for child runs — each bounded by 2% of the model's window.
- Reserved roughly two-thirds of the item budget for durable user-model and preference
  beliefs and the remainder for the opening-goal priming set, so project-specific
  beliefs cannot evict the "who am I talking to" layer the snapshot exists to carry.
- Made the number self-correcting rather than fixed: snapshot size should be
  **inversely proportional to retrieval quality** and is expected to shrink as the
  query former and ranker improve. Tuning is driven by two signals already present in
  `RecallTrace` — **snapshot utilization** (shrink below about a quarter) and
  **snapshot misses** (grow when in-turn recall keeps re-fetching snapshot-eligible
  beliefs) — which pull in opposite directions by design.
- Added the `Sizing the snapshot` section to the retrieval spec, recorded as ADR-0019
  decision 17, and rejected three alternatives: percentage-of-window sizing, one budget
  for every session type, and growing the snapshot as memory accumulates.
- Both memory specs now carry no open questions; the temporal entity graph remains
  unspecified.
- No product implementation was performed.

## 2026-07-24 — The recall trace becomes a user-inspectable surface

- Resolved the second retrieval open question: **the `RecallTrace` has two
  consumers** — the operator tuning ranking and the user asking why the agent said
  what it said — and both read the **same record**. Two logs would drift, and the
  one shown to the user is the one that must not be wrong.
- Specified that the trace is **recorded in the render pass, never reconstructed**,
  and bound to the exact rendered bytes by `rendered_sha256`. Re-running retrieval
  later returns a different set; a plausible reconstruction of a turn that never
  happened is worse than no answer.
- Defined what a trace may honestly claim: what was **in context**, with cited
  beliefs marked *used* and the rest *available*. It never claims what the model
  attended to.
- Added the user-safe projection (`RecallTraceView` / `TracedBelief`), which carries
  the statement, when and where it was learned, authority and source episode,
  confidence band, and citation, and excludes arm latencies, scores, candidate ids,
  and policy internals — dropped and blocked items are reported as counts only.
- Sensitivity is filtered by the **minimum of the recall surface's and the viewing
  surface's ceiling**, and retention is **two-tier over one record**: operator fields
  expire on the tuning window, user-safe fields live and die with their session.
- Added a `TraceStore` port, three failure modes (trace disagrees with what the model
  saw, trace as a disclosure path, a rejected belief returning after re-derivation),
  and two hard-gate evals: **trace faithfulness** and **correction durability**.
- Made rejection from a trace a **typed, first-class formation input**: not true
  (retire), was true and has changed (supersede), true elsewhere but not here (lower
  portability and record a negative scope override), and unspecified (flag and
  down-weight, never retire). Added `BeliefRejection` and the `MemoryStore` methods
  `reject` and `outstanding_rejections`.
- Established that **rejections are events that re-derivation replays**, matched by
  content rather than belief id since re-derivation mints new ids, and that
  **rejecting is not deleting** — a deletion keeps only a content-hash tombstone.
- Recorded as ADR-0019 decisions 15 and 16 and ADR-0018 decision 15, with build
  sequences updated in both specs so the trace is written faithfully from the first
  commit and rejections exist before re-derivation can violate them.
- No product implementation was performed.

## 2026-07-24 — Beliefs carry across projects

- Resolved the cross-project open question: **beliefs carry from project to project**
  so the agent learns from every project and environment it works in. Scope is split
  into **isolation boundaries** (tenant, principal, sensitivity — hard SQL predicates,
  unchanged) and **relevance boundaries** (project — a ranking and rendering input).
- Added a **portability** class per belief (`portable` / `contextual` / `local`),
  bounded by `belief_type` at formation and lowerable but never raisable by the
  extractor; carried beliefs render with their origin project and at a reduced
  confidence band, and explicit local overrides outrank them.
- Added **promotion by cross-project corroboration** to the formation spec: a belief
  independently observed in two or more project scopes promotes to `user` scope,
  retains every contributing origin, and emits `memory.promoted`. Recorded as
  ADR-0018 decision 14 and ADR-0019 decision 5.
- Added the **false transfer** failure mode with its defenses, and paired
  **transfer-precision / transfer-lift** evaluation metrics.
- Expanded the two remaining retrieval open questions with their tradeoffs: the
  session snapshot budget is attention-bound rather than cost-bound and should be an
  absolute token cap rather than a pure percentage; user-visible retrieval traces are
  restated in terms of the commitments they impose now (a user-safe projection,
  retention, the sensitivity ceiling, and a user-rejection input into formation).
- No product implementation was performed.

## 2026-07-24 — Memory retrieval & ranking spec

- Added `docs/plan/memory-retrieval-and-ranking.md`, the read-path design for
  long-term memory: the three retrieval moments forced by the prompt-stability
  invariant (frozen session snapshot, in-turn recall, child-run recall), query
  formation from working state, the hard scope filter, multi-arm recall fused by
  reciprocal rank, the explicit ranking function, supersession collapse, the safety
  pass, byte-stable rendering, retrieval traces, and the usage-feedback loop back
  into formation. Recorded as ADR-0019.
- Wired the spec and ADR-0019 into the MkDocs navigation and the ADR index, and
  added read-path pointers from Milestone 9 and the formation spec.
- Three open questions are recorded for decision: the session snapshot token
  budget, whether project-scoped beliefs may surface cross-project, and whether
  retrieval traces become a user-facing surface.
- No product implementation was performed.

## 2026-07-24 — Memory formation & consolidation spec

- Added `docs/plan/memory-formation-and-consolidation.md`, the detailed write-path
  design for long-term memory (formation pipeline, conflict resolution with
  bi-temporal supersession, data model, governance, evaluation, and build
  sequence). Recorded as ADR-0018.
- Wired the spec into the MkDocs navigation and the ADR index, and added a pointer from Milestone 9 in the engineering plan.
- Resolved two design decisions in the spec and ADR-0018: memory formation is **fully autonomous from the start** (safety via deterministic eligibility gates, the untrusted-content write ban, and after-the-fact review), and the **builtin consolidation path is built to parity before any external provider**.
- Resolved the remaining formation questions: a **tiered memory model** (a continuous confidence lifecycle plus an explicit working/episodic/semantic/archival hierarchy), the **user model is a projection** over user-scoped beliefs, and **re-derivation is opt-in** per principal.
- No product implementation was performed.

## 2026-07-20 — Documentation system established

- Archived the source Word document to
  `archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx` and recorded
  its SHA-256 checksum in `archive/README.md`. Preserved the prior Word revisions
  (v1.0 through v2.3) under `archive/versions/`; the canonical archived document is
  a copy of v2.3.
- Converted the complete engineering plan to canonical Markdown at
  `docs/plan/engineering-plan.md` (Pandoc `docx` → `gfm`, then deterministic
  cleanup: single level-one title, fenced code blocks with language hints,
  normalized tables, and removal of the static Word table of contents and
  title-page artifacts).
- Relocated three security controls — non-bypassable hardline rules, tiered
  credential scrubbing with fail-closed passthrough, and default-deny pairing for
  untrusted inbound surfaces — from the "Revision summary" list to their correct
  home in Section 22, "Security baseline". No requirement text was changed; only
  placement was corrected. The archived `.docx` retains the original placement.
- Created coding-agent instruction files: `AGENTS.md`, `CLAUDE.md`, and
  `.github/copilot-instructions.md`.
- Created machine-readable project state at `docs/status/project-state.yaml`
  (current milestone: 0) and a concise `docs/plan/current-milestone.md`.
- Wired the existing `docs/adr/` records (ADR-0007 through ADR-0017) into the
  documentation site.
- Created the documentation build system: the MkDocs site (`mkdocs.yml`), a
  single-file HTML build (`docs-manifest.yaml`, `scripts/build_docs.py`),
  documentation validation (`scripts/check_docs.py`), `Makefile` targets, and a
  CI workflow (`.github/workflows/docs.yml`).

No product implementation was performed. Milestone 0 has not been started.
