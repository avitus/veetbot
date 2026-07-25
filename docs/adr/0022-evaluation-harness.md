# ADR-0022: The gate registry, evaluation identity, and the capability track

- Status: Proposed
- Date: 2026-07-25
- Related: Milestones 0, 1, 2, 3, 4, 5, 6, Sections 3 (definition of done,
  item 18), 4 (repository layout, `evals/`, `tests/`), 10.3 (the fake
  provider and `FakeModelScript`), 12.4 (parallel tool calls), 15
  (`tool_invocations`), 18 (`agent eval run`), 19 (spans and metrics),
  20 (the evaluation framework), 21 (milestone gates), 22 (security
  baseline), 31 (trajectory capture and export), ADR-0001 (modular
  monolith and enforced boundaries), ADR-0002 (provider-neutral model
  protocol), ADR-0005 (deterministic policy engine), ADR-0006 (no private
  reasoning storage), ADR-0016 (trajectory capture and export), ADR-0020
  (context engine), ADR-0021 (tool execution pipeline and effect
  watermarking)
- Detailed design: `docs/plan/evaluation-harness.md`

## Context

Six specs written for this plan end in a section called hard gates, and
between them they declare roughly forty-nine. Each says the same thing in
its own words: failing one blocks the milestone, and it is not a warning.
The policy spec is the most explicit about who enforces this — *"Section
20's harness gates Milestone 4 on these"* — and that sentence is the
problem, because Section 20's harness cannot run most of them.

Section 20 specifies a YAML case format with an input, a model fixture, and
seven expectation fields, plus sixteen assertion types. Sorted against that
list, the declared gates do not fit. "The import-boundary walk passes" is
not a run outcome. "No API key appears in any log line, event payload, span
attribute or persisted row" is not a tool call count. "`build()` invoked
twice on the same checkpoint produces byte-identical output" does not
involve a run at all. "A canary string placed in `EXTERNAL_UNTRUSTED` tool
output never appears outside an envelope" is a statement about every case in
the suite rather than about any one of them. Roughly a third of the declared
gates are not expressible as eval cases, and a harness that runs only cases
reports a green build with a third of the plan's stated invariants
unchecked.

Four smaller gaps each block work on the first day of Milestone 1.

`model_fixture: calculator_then_answer` resolves to nothing. Section 20.1
names a fixture with a bare string; Section 10.3 defines the fake provider's
input as a Python object. Nothing states where a fixture named by a YAML
string lives, what format it is in, whether two cases may share one, or what
happens when the name does not resolve.

"Deterministic" is asserted and never defined. The runtime as specified
reads a clock in at least four places, generates identifiers in six,
executes tool batches concurrently, and stores its events in a database
whose row order is not its insertion order. Two of the sixteen assertion
types are sensitive to all of that.

"No unauthorized side effects" is the last assertion type in Section 20.2
and it is not decidable as written, because a test cannot observe an HTTP
request made to a system it does not control.

And the suite runs the agent, so the suite is a privileged caller. Case 12
requests an approval, case 13 grants one, case 25 proposes external writes.
Nothing says what tenant an evaluation executes as, under which policy
profile, or whether the mechanism that lets a test approve its own external
write is reachable from production. A deterministic policy engine with a
test-only bypass has a bypass.

## Decision

1. **A hard gate is a named, executable, milestone-attached condition that
   fails the build.** Named, so "ten hard gates on Milestone 3" reconciles
   against a test run. Executable, so a gate is a function rather than a
   paragraph. Milestone-attached, so the full list is written at design time
   and switches on as its subject arrives.
2. **Gates are declared in a checked-in registry, one YAML file per spec
   area,** carrying the identifier, milestone, kind, a link back to the
   declaring document, the prose statement, and the check that implements
   it. A docs check compares each spec's declared gates against the registry
   and fails the docs build on a mismatch. It is a weak check — it compares
   identifiers and counts, not meanings — and it catches the failure that
   actually happens, which is a gate silently dropped during an edit.
3. **Gates come in four kinds, because they run in different places.** Case
   gates are what Section 20 already describes. Property gates generate
   their own inputs and declare a generator, a predicate, and a minimum
   trial count, because the point of them is the quantifier. Corpus gates
   run one procedure over a checked-in set and assert an outcome for every
   member, so growing the corpus strengthens the gate without touching code.
   Structural gates never run the agent at all. The last two need no runtime
   and are buildable in Milestone 0.
4. **A gate before its milestone is `pending` and prints as pending. A gate
   at or past its milestone may not skip.** A gate that cannot run is a
   failure; skips are how suites decay into decoration.
5. **A corpus gate declares `minimum_members`,** so it cannot pass vacuously
   after somebody empties a directory.
6. **"Deterministic" is defined by naming its seven sources and their
   treatments.** The wall clock, identifier generation, and model output are
   *pinned* behind ports. Batch concurrency and database row order are
   *ordered* by explicit sort keys and a deterministic scheduler. Retry
   timing is *bounded* rather than pinned. Hash iteration order is
   *accepted* and never asserted on. What determinism does not mean is
   stated as plainly: the runtime is not deterministic, payloads are not
   byte-identical across runs, and the deterministic scheduler proves the
   parallel path produces the right result — not that it is race-free.
7. **`model_fixture: NAME` resolves to `evals/fixtures/models/NAME.yaml`,**
   one file per script, validated at collection against the current
   `FakeModelScript` shape rather than at run time. `call_id` values are
   authored, an exhausted script is an error rather than a silent stop, and
   failure turns are first-class with an error class and a byte offset.
8. **Model scripts are authored source. Recording is an explicit command**
   (`agent eval record`) and never a side effect of running the suite,
   because a suite that silently re-records is a suite that asserts whatever
   happened last.
9. **`interventions` is added to the case schema.** Approve, deny, cancel,
   kill the worker, answer, and disconnect are the six. Without them cases
   12 through 18 and 22 are unwritable, which is most of what distinguishes
   this platform from a chat loop.
10. **"No unauthorized side effects" is asserted against
    `tool_invocations.effect_sent_at`.** ADR-0021 introduced the watermark
    for crash recovery and it answers this question too: an empty
    `expected.effects` list means no invocation in the run set a watermark,
    and it is the default for every case that does not opt in. The
    undecidable assertion becomes a database read.
11. **`event_order` is a subsequence assertion, not equality,** so adding an
    event type does not fail every case in the suite at once.
12. **There is no test mode.** No environment variable, no configuration
    flag, no `if settings.testing` branch in the policy engine, the approval
    service, or the tool executor. Everything special about an evaluation is
    data: a `tenant_eval` tenant created by an ordinary migration under
    ordinary row-level security, named principals with real scopes, and
    policy profiles that are files loaded by the same loader and subject to
    the same totality gate.
13. **An `approve` intervention calls the approval application service as a
    second principal** — `eval.approver`, holding approval-resolution
    authority and nothing else — through the same authorization check the
    CLI uses. It does not set a status.
14. **Eval runs are ordinary runs in the ordinary event log,** in a tenant
    nobody queries. The suite truncates that tenant's tables between cases;
    it does not turn the log off.
15. **Production configuration cannot load an evaluation identity.** The
    production loader does not read `evals/`, a startup check asserts that
    no loaded profile name begins with `eval.` outside development, and a
    test asserts that the check fires. The structural form of the same rule
    is that no module under `agent_core` outside `agent_core.evals` may
    import `agent_core.evals`, enforced by ADR-0001's import-graph walk.
16. **Contract suites are attached to ports, not to implementations.** Every
    port has exactly one contract module, parameterized over
    implementations. A port with no contract module fails the build, and so
    does an implementation not registered against its port's contract. This
    is what makes the model gateway's second gate — the same contract
    passing against fake, recorded, OpenAI, Anthropic, and
    `chat_completions` — one configuration line per adapter instead of five
    files that drift.
17. **`resilience` is named as the sixth test category.** Section 20.4 lists
    five; the repository layout in Section 4 has six directories and two
    specs already place tests in the sixth. It is the only category
    permitted to be nondeterministic, on the condition that a failure prints
    its seed and a failing seed is promoted to a checked-in case in the same
    change. Eval cases are not a seventh category: a case is an integration
    test with a declarative front end.
18. **Every case declares its earliest milestone.** Ten of Section 20.3's
    twenty-five are writable in Milestone 1, cases 16 through 18 need
    Milestone 2, most of the policy cases need Milestone 4, and case 22
    needs Milestone 5. A Milestone 1 checkout that fails case 22 is not a
    failing checkout.
19. **Case 18 splits into 18a and 18b.** Killing the worker before the
    watermark asserts re-execution; killing it after asserts `UNCERTAIN`, a
    human-review row, and that the model is told the outcome is unknown
    rather than that the call failed. Only the second has a safety
    consequence, and it was untestable while "ambiguous" was undefined.
20. **A capability scenario has no assertions and defaults to five
    repeats.** A single run of a stochastic system against a rubric produces
    a number of unknown variance, and comparing two such numbers across a
    release is the most common way an evaluation programme generates false
    alarms until people stop reading it.
21. **A judge is a model, a prompt, and a rubric versioned as one unit,**
    pinned to a provider version, replaced only alongside a bridge run that
    publishes a calibration offset, never sharing a provider family with the
    subject unless the scenario records that it does, never shown the
    rubric's weights, and never reusing an identifier after deprecation.
    Comparison across judge versions is refused by the tooling rather than
    footnoted.
22. **A regression is a distribution change, not a score change.** Floor
    drops and policy failures block a release; mean drops within a measured
    noise band are investigated. The noise band is recomputed each release
    from repeats on an unchanged build rather than picked in advance. **A
    capability improvement that increases policy failures is a regression**,
    and no score outranks a policy failure.
23. **Cost ceilings exist at scenario, suite, and day, and a scenario that
    hits one is excluded from the score distribution rather than scored
    zero.** A scenario that ran out of budget has not been shown to be bad
    at the task, and scoring it zero corrupts the distribution the whole
    track rests on. The rate of ceiling hits is itself a tracked signal.
24. **Trajectory conversion replays recorded tool results rather than
    re-executing tools,** discards timestamps, identifiers, usage, and cost,
    and consumes the already-redacted export rather than the raw event log.
    A converted case is marked `source: trajectory` and does not enter the
    blocking suite until a person writes its assertions, because
    auto-generated assertions pin whatever the system did on the day it was
    recorded, including its defects.
25. **Flaky tests are retried once with the retry rate reported even while
    green, quarantined on a second failure within thirty days, and
    un-quarantined automatically after fourteen days whether or not they
    were fixed. Gates may never be quarantined.** A quarantine without an
    expiry is a delete with extra steps, and the tests that end up there are
    disproportionately the ones covering concurrency and recovery.
26. **Tracked metrics never block CI.** A metric with a threshold is a gate
    with a worse name, and should be registered as one.
27. **The deterministic suite adds no tables.** The capability track adds
    two, keyed by scenario, build, judge version, and repeat.

## Consequences

- Forty-nine invariants declared in prose across six specs become
  executable and reconcilable. The registry is buildable in Milestone 0,
  before there is an agent, which is what makes "build evaluations before
  advanced features" an instruction rather than an aspiration.
- The count itself is a finding: roughly a third of the declared gates are
  not case gates. A harness scoped to Section 20's case format would have
  shipped green with them unchecked, and nobody would have noticed until an
  invariant was violated in production.
- Section 20 is completed rather than changed. The twenty-five cases stay
  twenty-five, the sixteen assertion types stay and gain four, the
  capability track stays non-blocking, and the deterministic suite still
  runs in CI with no API key — now asserted by running that job with egress
  blocked instead of by not configuring a key.
- The policy engine, the approval service, and the tool executor gain no
  test-aware code path at all. The cost is a migration that creates an
  evaluation tenant in every deployment and a startup check that must be
  maintained; the benefit is that the deterministic policy engine's
  behaviour genuinely does not depend on where it is running.
- Every port acquires an obligation to have a contract module before it can
  merge, which will be experienced as friction the first few times and is
  the mechanism that keeps the second implementation of any port honest.
- The capability track cannot publish a number until judge governance
  exists, which delays the first score and prevents the failure mode where
  a track produces a moving number and an argument about whether it means
  anything.
- `tests/` gains a sixth category, and the routing rule that comes with it
  keeps "integration" from becoming the drawer everything slow ends up in.
- Trajectory export gains a consumer, which is the point of landing it in
  Milestone 3. The gate requiring at least one converted case from that
  milestone onward is what stops the conversion path from being written
  once, never run, and discovered broken when it matters.

## Alternatives considered

- **Treating the specs' hard-gate sections as prose and checking them by
  review**: rejected. It is the status quo, and it produces a green build
  that asserts nothing about a third of the plan's invariants.
- **Expressing every gate as an eval case**: rejected because it is not
  possible. Property, corpus, and structural gates each have a shape the
  case format cannot express, and forcing them in is what made Section 20's
  list look sufficient.
- **A test-mode flag that relaxes policy for evaluations**: rejected. It is
  a code path in the shipped binary and therefore reachable from
  production, and it contradicts ADR-0005's third gate, which says exactly
  one function performs the `PROPOSED` to `AUTHORIZED` transition.
- **Letting the harness call the policy engine's internals directly to set
  up state**: rejected for the same reason with an extra one — a case that
  bypasses the approval service stops being a test of the approval service,
  which is the thing cases 12 through 14 exist to test.
- **A separate event log or a suppressed one for evaluations**: rejected;
  it means the code path the suite exercises is not the code path
  production uses, and it would require a parallel projection
  implementation for cases to assert against.
- **Asserting the absence of side effects by inspecting logs or mocking the
  network**: rejected; both are proxies. The watermark is a fact the tool
  system already records for a different reason and it is directly
  observable.
- **Making `event_order` an equality assertion**: rejected; adding an event
  type is a routine change and it would fail every case in the suite at
  once, which trains people to update expectations without reading them.
- **Recording model fixtures automatically on a live run**: rejected; a
  suite that re-records itself asserts whatever happened last, and the
  first provider hiccup silently becomes the expectation.
- **Duplicating contract tests per implementation**: rejected; five
  parallel files drift, and the drift is invisible because all five pass.
- **Scoring a ceiling-hit scenario as zero**: rejected; it conflates "we
  stopped paying" with "the agent failed", and it does so in the direction
  that corrupts the distribution most.
- **Comparing scores across judge versions with a footnote**: rejected in
  favour of the tooling refusing. A footnote is not read at the moment the
  comparison is made.
- **A judge that sees the rubric weights**: rejected; it optimizes the
  total, and the failure is silent because the number looks fine.
- **Quarantine without an expiry**: rejected; it is a delete with extra
  steps and it disproportionately deletes the concurrency and recovery
  tests.
- **Auto-generating assertions from a converted trajectory**: rejected;
  there is no worse outcome for a regression suite than pinning a defect as
  expected behaviour.
- **A self-hosted judge model**: deferred rather than rejected. It removes
  the deprecation problem entirely and ADR-0012 makes it a real option;
  recorded as an open question to decide before the first published score.
