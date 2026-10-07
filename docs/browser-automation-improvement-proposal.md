---
title: Self-hosted Browser Automation Improvement Plan
status: implementation
canonical: false
---

# Self-hosted browser automation improvement plan

**Implementation authorized by the owner, 2026-09-30.** Improve Veetbot's own browser platform;
Browserbase is not a dependency, migration target, or fallback. Retain Python,
Playwright, Chromium, the provider boundary, and Veetbot's approval system.
Compete on verified task completion, minimal user involvement, privacy,
and operating cost on the websites the owner actually uses.

**Owner priority update, 2026-10-06:** autonomous operation is the default.
Once the user supplies intent and the necessary account and permission scope,
the agent should handle observation, verification, routine recovery and task
continuation. Ask the user only for a decision, authorization or authentication
step that the system cannot complete within its existing authority. This is a
planning update; the proposed automatic resumption and site verification below
are not claims about shipped behavior.

The owner instructed implementation after reviewing this plan; authorization
and the initial decisions are recorded in [ADR-0156](adr/0156-self-hosted-browser-improvement-program.md).
This document is not release evidence. Existing requirements remain
governed by [engineering plan Section 33](plan/engineering-plan.md#33-authenticated-browser-automation)
and the [browser design](plan/browser-automation.md). Changes to those contracts
need the decisions listed below before implementation. No milestone is opened
or completed by this proposal. Roadmap labels P0–P5 are delivery phases, not
repository milestone numbers.

The baseline is checkout `f52ee215cf138cad2064862ba1f4de508b4dbd68`.
Code and documentation were inspected; production configuration, live task
success rates, capacity, and operating cost were not measured for this plan.
All numerical targets and effort estimates below are proposals, not results.

## Implementation progress — 2026-10-06

The first construction slice adds the required Chromium delivery partition and
content-free runtime report, semantic model-context projection, and atomic
cleanup of failed observation captures. The implementation decisions and tests
are recorded in [ADR-0156](adr/0156-self-hosted-browser-improvement-program.md).

The second slice adds bounded candidate acquisition and observation continuation
through local and hosted providers, model-budget-aware continuation, native and
ARIA label support, and a twelve-scenario synthetic component task manifest with
server-side effect assertions. [ADR-0157](adr/0157-bounded-browser-observation-expansion.md)
records the contract and limits. Its content-free task evidence is part of the
mandatory browser baseline; it uses no model calls.

The third slice adds typed, bounded phase diagnostics through the hosted service
and existing terminal tool events, with safe native activity details
([ADR-0158](adr/0158-browser-operation-diagnostics.md)). Navigation and actions
report bounded DOM readiness without waiting for background network silence.
Optional visible-control postconditions poll reads within finite limits and
preserve uncertainty after dispatch, including an outer tool deadline
([ADR-0159](adr/0159-browser-readiness-and-postconditions.md)). Focused regression
evidence covers secret-free metadata, collector isolation, hosted refusals,
small-budget condition preservation, delayed completion and a single server
effect. All 620 native package tests pass on a macOS 13 validation target; the repository's
unchanged macOS 12 package target currently encounters an availability error in
`MemoryDetailView`, so the normal full Apple gate is not claimed green.

The fourth slice adds bounded visible headings, forms, status messages, alerts
and dialogs under [ADR-0160](adr/0160-bounded-browser-page-structure.md). Whole
semantic records survive hosted transport, output bounding and model projection
with explicit scan and omission coverage. Evidence references cannot become
action targets. Real-Chromium fixtures cover hidden/editable canaries, priority,
Unicode, traversal bounds and changed evidence; replay retains external-untrusted
framing. This is page structure, not schema-based table/form/list extraction.

The fifth slice adds deterministic table/list extraction and form metadata
under [ADR-0161](adr/0161-deterministic-browser-extraction.md). Typed columns
retain explicit missing/invalid/truncated evidence, bounded traversal and whole
row omissions. The optional read capability uses local and hosted providers,
revision and lease checks, with no action dispatch. Real Chromium coverage
includes native and ARIA collections, hidden/editable canaries, schema conversion,
scan limits, stale revisions and failed-capture cleanup; model projection and
artifact replay retain whole rows and external-untrusted framing. This is
main-document extraction by current visible index, not stable collection
identity, merged-grid inference or model-based interpretation.

The sixth slice uses Stagehand's snapshot-before-inference and content-pruning
approach as a source reference, recorded at an exact revision in
[ADR-0162](adr/0162-bounded-readable-browser-snapshots.md). A bounded readable
text collector replaces whole-body reads, includes open shadow roots and omits
editable/hidden content. Explicit capture coverage survives canonical and model
budgets, hosted transport and replay. No Stagehand dependency or hosted service
is introduced. The owner deferred comparative benchmarks on 2026-10-01; continue
implementation with regression checks, leaving the proposed scorecard unmeasured.

The seventh slice adds current-region focus, model-sized text continuation,
active-dialog control priority, and private stable region handles. Recovery uses
separately audited reads and bounded owner sign-in suspension; no action replay
or automatic credential entry is introduced. See
[ADR-0163](adr/0163-focused-browser-observations-and-recovery.md).

W1–W5 remain partial: live task quality, production-image validation, operational
aggregates, in-flight phase streaming, frame coverage, reusable workflows and
site-specific positive sign-in verification still need evidence. W6–W10 remain
outstanding. Live site contracts use the websites, tasks and account/action
scope the owner has already selected; ask only for genuinely missing scope.
Technical predicate and workflow maintenance belongs to the implementation,
not to the user.
Keep the release scorecard unmeasured until its own evidence exists; local work
is not a push, review, deployment or a completed program.

## 1. Product outcome and priorities

A user should be able to connect a website, describe a task, understand the
permission being granted, and receive a result supported by website evidence
without supervising routine steps. Progress remains available when wanted;
intervention is exceptional and actionable. Repeated tasks should
become faster without acquiring broader authority. A failed task should say
whether nothing happened, something happened, or the outcome is uncertain.

Prioritize in this order:

1. Make the current browser diagnosable; comparative task benchmarks are deferred
   by the owner's 2026-10-01 instruction.
2. Verify task progress and outcomes automatically; recover within existing
   authority before considering a user interruption.
3. Reuse valid sessions, verify authentication and resume automatically after
   any necessary owner sign-in.
4. Reduce repeated reasoning and setup through guarded, reusable workflows.
5. Add useful capabilities: structured extraction, files, bounded frames and
   pages, and human takeover.
6. Expand supported workloads through isolated execution and a dedicated
   device-local provider when the evidence justifies them.

Fleet size and a long feature checklist are secondary. Ten reliably supported
workflows with clear recovery are a stronger first release than hundreds of
sites that sometimes load. Do not equate page-load success, a completed tool
call, or the model saying "done" with task completion.

### Autonomous default and intervention rules

For a supported routine task with a valid session, sufficient instructions and
current authority, the target is **zero additional user prompts** after task
submission. If authority is missing, reuse the existing scoped approval flow;
one valid task grant should cover its eligible routine steps. Do not ask again
for permission already granted, but do not extend its lifetime, action count,
profile generation or consequence coverage. Existing sensitive-action and
authentication requirements remain unchanged.

| Situation | Automatic response | When the user is needed |
| --- | --- | --- |
| Missing page detail, stale target or transient read failure | Expand, wait, re-observe and make a fresh agent decision within fixed limits; recheck every proposed action against current authority. | Only if a material choice remains ambiguous after bounded inspection, or the next action requires approval. |
| Valid saved session | Reuse it and verify protected state in the isolated service; retain normal site-managed renewal within the existing session lifecycle. | No fresh sign-in solely because a new task starts. |
| Expired session or authentication challenge | Check whether the authorized session can recover without credentials, then preserve progress and offer the existing sign-in surface once. | Owner-controlled credentials, MFA, CAPTCHA, passkeys or required consent; never request these in chat. |
| Sign-in completed | Consume a trusted verification result, recheck the intended account and current authority, acquire fresh browser state and resume unfinished work. | Only a remaining account choice or newly required approval; no obligatory "Done" message. |
| Action response lost | Inspect bounded read-only completion evidence; continue from a verified result or preserve an explicit uncertain outcome. | Only if the user has information or authority needed to resolve the next decision. Never resend an uncertain write. |
| Outage, unsupported capability or no useful progress | Stop or defer within the run's existing limits and report completed work and the blocker. | Ask only when the user can take a concrete useful action; do not ask them to troubleshoot ordinary automation. |

Deduplicate interruptions by the blocking episode: retries, worker restarts and
reconnecting clients must not create repeated questions or notifications for
the same unresolved condition. Record recovery progress in the existing activity
view without requiring acknowledgement. Notify on a useful completed result,
a material failure, or a required user action; transient recoverable failures
need no separate notification. Bundle related missing choices into one concise
request, reuse already supplied answers, and retain task intent and verified
progress across the interruption.

The user should not author selectors, login markers or workflows, manually
refresh pages, repeat the task description, confirm every routine click, or
judge whether an ordinary action succeeded. The implementation maintains those
mechanics. Do not invent account identity, consequential preferences or broader
permission to avoid a question. If the run's deadline or budget expires, finish
with the verified partial result; automatic resumption cannot reset limits,
revive cancelled work or renew an expired grant.

These are correctness and interaction requirements for the next regression
fixtures. Comparative benchmarks and the scorecard below remain deferred.

## 2. What we already have, and what actually needs work

| Area | Verified implementation or documented limit | Planning implication |
| --- | --- | --- |
| Runtime | `PythonPlaywrightRuntime` launches Chromium; hosted orchestration uses `BrowserProvider` and a separate browser service. | Evolve the adapter; avoid a framework rewrite. |
| Authority | Per-action approval, standing grants, task grants, profile generations, live dispatch constraints, and uncertainty handling exist. | Preserve these through every optimization. |
| Task setup | Device sign-in, one-run lease reuse and renewal, browser-task budgets, and client-managed task scopes already exist under ADRs 0127–0130 and 0141. | Verify delivery and UX rather than reimplementing them. |
| Authentication | Remote readiness uses challenge/input evidence; device handoff compares authenticated and empty sessions. Neither has a declared site-specific positive login predicate. | Add evidence-based authentication contracts and an explicit inconclusive state. |
| Storage | Hosted `storage_state(indexed_db=True)` already preserves IndexedDB. Native handoff currently transfers cookies and localStorage only. | Treat broader native handoff as a specific compatibility project, not a missing hosted capability. |
| Observation | Root-page text plus up to 256 visible controls from 4,096 inspected candidates; the runtime first obtains the matching handles, then slices them. | Improve relevance, structured bounds, and work done before truncation. |
| Model presentation | Browser tools allow large results; the shared context path can mechanically excerpt tool content at a much smaller inline budget. | Test the final model-visible observation, not just provider output. Potential control loss is a hypothesis to reproduce, not a diagnosed production incident. |
| Stability | Navigation/actions already settle, bounded at two seconds; stable handles and revisions reject stale actions. | Add target-specific readiness and completion evidence, not another unconditional sleep. |
| Features | Downloads, uploads, new windows, clipboard, device permissions, and service workers are currently denied. Embedded resource frames may load but do not gain agent action authority. | Add separate classified contracts, not a blanket permissive browser mode. |
| Debugging | Existing events, action approval views, stable reason codes, and direct sign-in frames provide foundations. The inspected agent result schema has no screenshot field. | Add safe diagnostic events first; screenshots need a separate capture and exposure contract. |
| Isolation | The production service has a read-only root, unprivileged uid, bounded resources, private secrets, and a dedicated proxy. Chromium children share the service container that mounts profile material and key files. | Assess browser-escape impact and separate execution from the profile vault before scaling to many simultaneous profiles. |
| Verification | Contract/gate suites and a real-Chromium harness exist. Real-browser tests can skip when Chromium is missing; the inspected CI configuration does not explicitly require that harness. | Require actual Chromium execution in the delivery lane; do not infer it from the gate count. |

Current task grants are thirty minutes, two hundred actions, and 4,096 total
typed characters. They do not cover named sensitive consequences. Current
browser-task run limits are 160 steps, 120 model calls, 160 tool calls, and a
USD 30 cap with a USD 3 reserve. These are ceilings to respect, not performance
targets or a reason to increase budgets before improving efficiency.

The project-state list contains older production acceptance items, including
environment-based task-scope setup. ADR-0141 now makes persisted client-managed
scopes authoritative. P0 must reconcile this history with actual deployed
versions and configuration; do not ask the owner to redo completed setup.

## 3. Definition of competitive performance

This proposed benchmark program is deferred under the owner's 2026-10-01
instruction. Existing mandatory correctness checks continue to run.

Create a versioned benchmark with three explicitly separate populations:

- **Controlled workflows:** synthetic, deterministic sites with a ground-truth
  server and injected failures. Used for blocking correctness checks.
- **Representative live workflows:** owner-approved accounts and actions on
  real sites. Used for compatibility and task-quality evidence, with no secrets
  in reports and no repeated external writes merely to increase sample size.
- **Adversarial workflows:** malicious page text, changed targets, hostile
  frames, redirects, popup races, and secret-leak canaries. Used for boundaries.

Start with 40 controlled task scenarios across at least eight families, 30
adversarial scenarios, and 12 live task definitions across at least six sites.
Existing tests may satisfy scenarios; map them instead of duplicating them.
Families should include dashboards and tables, search/filtering, multi-step
forms, lesson-like interaction, document retrieval, uploads to a test account,
authentication interruption, and long-running scheduled work. Transfer and
multi-page cases are initially marked unsupported and become eligible only
when their contracts ship. Always report unsupported coverage separately.

### Proposed release scorecard

| Measure | Initial release target | Measurement rule |
| --- | --- | --- |
| Controlled task completion | At least 95% overall; no eligible family below 90% | Ten independent repetitions per scenario; exact expected state/result, within the same budgets. |
| Live task completion | At least 90% in the declared supported set | At least 60 consented task attempts across multiple days and six sites; report sample sizes and confidence intervals. Small samples are provisional. |
| Boundary regressions | Zero observed unauthorized dispatches, cross-profile reads, or secret canary leaks | All adversarial cases pass; any failure blocks release regardless of task success. This is test evidence, not proof of universal safety. |
| Duplicate effects | Zero in the injected lost-response suite | The synthetic server counts writes after timeout, cancellation, reconnect, and retry attempts. |
| Wrong success claims | Zero in the controlled ambiguous-outcome suite | No terminal success without the scenario's postcondition evidence. |
| Authentication verification | Zero false-ready outcomes in negative fixtures; at least 95% success on supported positive fixtures | Include public landing pages, analytics cookies, wrong accounts, failed submissions, and delayed responses. |
| Efficiency | At least 40% fewer model calls and 30% lower median total cost on recurring tasks | Paired baseline/candidate runs, fixed model and pricing snapshot, equal success criteria, failures included. |
| Runtime overhead | p95 observe below 1 second and browser-side act-to-observation below 2 seconds on settled controlled pages | Exclude model and human time; report network/site wait separately rather than hiding it. |
| Human interruption | Zero additional prompts with valid session and authority; otherwise at most one task approval for eligible routine steps | Count every approval, authentication request, clarification and continuation acknowledgement, separating necessary from avoidable interruptions. Count repeated notices as avoidable interruptions; report completions without intervention separately. |
| Cancellation | No new action dispatch after cancellation is acknowledged; p95 cleanup below 5 seconds | Already-sent website effects may finish and must be reported truthfully. |
| Resource stability | No leaked processes after repeated lease cycles; no sustained RSS growth across a 24-hour soak | Define the concurrency and hardware first; measure service and child processes. |

Baseline before committing to absolute latency targets. Where a proposed target
proves unrealistic, record the measured constraint and a revised target openly.
Do not relabel failing workflows as unsupported after seeing results. Pin the
task set, model, browser build, agent version, policy version, and site-contract
versions in each comparison. Randomize paired run order and reset fixture state.

Track completion both with and without human intervention. Count timeouts,
unexpected authentication, policy refusals, provider errors, and budget exits
in the overall denominator. Break them out by cause to distinguish intended
boundaries from product defects. Track browser minutes, model tokens, storage,
host cost, and maintenance time per successful task. Targeting lower token cost
alone can make the total system worse.

## 4. Target architecture

Keep the existing tool/policy/run pipeline as the authority for execution.
Add narrow components only when a work package needs them:

```text
Chat / scheduled task / owner controls
              |
Existing run, policy, approval and grant pipeline
              |
Browser task executor -- bounded plans and verified postconditions
              |
BrowserProvider -- versioned capabilities, observations and actions
              |
Session coordinator -- profile generation, lease, sequence, cancellation
        |                                      |
Profile vault                            Execution placement
encrypted material                       hosted isolated worker
no model access                          or dedicated owner device
        |                                      |
        +-- scoped material transfer ----------+
                                               |
                                 Chromium + enforced network boundary
                                               |
                                Semantic observation and safe telemetry
```

The task executor is a proposed deterministic component inside the existing
runtime, not a second autonomous agent that can bypass policy. Site contracts
describe known page structure and success conditions; they do not grant access.
The profile vault/execution split is a later architectural change. The current
service can host early improvements without pretending that split already exists.

Prefer internal components before new public tools. Version any changed tool
schema and provider protocol; pinned plans and old clients must either continue
working or receive a deliberate unsupported-version response. A capability
declaration reports support; it never authorizes an operation.

## 5. Work packages

### W1 — Establish a task benchmark and mandatory browser verification

**Priority:** P0. **Estimated effort:** 1–2 engineer-weeks. **Dependencies:** none.

Build on `tests/real_browser_support.py`, the existing browser contracts, and
the Milestone 10 gates. Add a benchmark manifest, reusable fixture tasks, and a
report that records final website state, intervention, reason codes, timing,
cost, and version identifiers. Give every scenario an explicit completion
predicate and allowed effects. Include mixed-script labels, right-to-left text,
slow SPAs, more than 256 controls, open shadow roots, and changing content.

Require `VEETBOT_REQUIRE_REAL_BROWSER=1` in the browser delivery lane, install
the lockfile-matched Chromium build, and test headed and headless modes where
applicable. Test the actual production image and network restrictions as well
as the local harness: a harness that relaxes TLS for synthetic certificates
does not prove production TLS and proxy enforcement.

**Exit evidence:** versioned baseline report; no silent browser skips; a missing
binary fails the required lane; controlled website counters verify intended
effects; failures reproduce without real accounts. Keep live-site evaluation
out of deterministic CI and run it only within explicit account/action consent.

### W2 — Add safe diagnostics and a useful activity view

**Priority:** P0–P1. **Estimated effort:** 1–2 weeks. **Dependencies:** W1.

Add typed events for session acquisition, browser launch, navigation, readiness,
observation projection, authorization, dispatch, postcondition, and cleanup.
Record phase timings, bounded counts, reason enums, browser build, execution
placement, and existing authorized correlation IDs. Use low-cardinality
aggregate metrics. Page paths, titles, labels, typed values, network headers,
request bodies, and raw exception strings are not safe metric labels.

Separate failure stages: policy refusal; login needed; target absent; target
ambiguous; page still changing; profile busy; browser lost; site refused access;
unsupported feature; possible effect with unknown outcome. Preserve the existing
stable external vocabulary until a versioned extension is defined. Safe detail
must explain an action the user can take without dumping provider diagnostics.

Build a browser activity timeline in the existing run view: current website,
step, elapsed time, approval source, remaining grant, last verified progress,
and why it stopped. Reuse the existing approval presentation and grant banner.
Routine waiting, re-observation and successful recovery update this passive
view without prompting the user. A blocker offers one direct action with enough
context to act; opening diagnostics or acknowledging progress is never required
to continue the task.

Default production diagnostics remain content-free metadata plus the existing
bounded observations under their current access controls. A new sanitized
timeline can reconstruct step order, but is not pixel-perfect session replay.
Propose seven-day retention for new detailed operational events and thirty-day
aggregate metrics, with deletion integration and no change to existing audit
retention without its owning design.

Do not enable raw Playwright tracing or HAR collection on authenticated
production sessions. Trace Viewer can include DOM snapshots, screenshots,
headers, and request/response bodies. Use it on synthetic fixtures; a production
capture mode needs the separate W7 exposure and retention contract.
[Playwright tracing reference](https://playwright.dev/python/docs/trace-viewer).

**Exit evidence:** a failure can be assigned to a phase from its run ID;
metadata covers every browser operation; secret canaries never enter logs,
events, metrics, exports, or exceptions; diagnostics-off behavior is unchanged.

### W3 — Make observations compact, complete enough, and actionable

**Priority:** P1. **Estimated effort:** 2–3 weeks. **Dependencies:** W1; W2 helps measurement.

Introduce a versioned semantic observation with page title and safe location,
active dialog, relevant headings, form groups, visible status/error messages,
and bounded controls. Preserve roles, accessible names, checked/selected state,
disabled state, opaque references, and exact revision binding. Add explicit
coverage metadata: omitted controls, omitted text, observed region, and whether
the page was still changing. Never silently imply that a partial page is complete.

Rank the active dialog and task-relevant region before global navigation and
footer controls. Keep a deterministic baseline ranking; evaluate model-assisted
ranking only if it materially helps and remains advisory. Provide bounded
region expansion or pagination under a new observation revision, so a relevant
control beyond the first 256 is reachable. Limit work before acquiring thousands
of handles; always release discarded handles and partial captures on failure.

Make the final model projection a first-class contract. It must retain a valid
revision and complete reference records for every advertised action. Fit a
useful observation into the admitted context budget, with explicit expansion
for missing evidence. Avoid both a global context-limit increase and a generic
head/tail excerpt that cuts an action record in half. Preserve canonical source
evidence and external-untrusted framing. Initial design candidates are a
browser-specific semantic projection or a small structured tool result plus a
separately retrievable bounded detail view; choose through an ADR and paired tests.

Add schema-based extraction for tables, forms, and lists with row limits,
missing-value reporting, and references to observed evidence. Validate extracted
values against the supplied schema, but do not confuse schema validity with
truth. Start with deterministic extraction; use the existing model gateway only
for interpretation. No raw DOM, arbitrary evaluation tool, or secret storage API.

**Exit evidence:** the actual model request retains required controls on large
pages and small budgets; malformed JSON excerpts cannot become action plans;
100% of synthetic target controls are discoverable through bounded expansion;
unchanged regions do not repeatedly consume full observation tokens; hidden
values remain absent. Measure token savings without sacrificing task success.

### W4 — Improve readiness, action completion, and bounded recovery

**Priority:** P1. **Estimated effort:** 2–3 weeks. **Dependencies:** W1 and W3.

Keep Playwright's actionability checks and the existing live grant guards.
Introduce target-specific readiness: visible/enabled target, stable geometry,
no blocking dialog, expected navigation completion, and a declared page-state
condition when available. Global network silence is only one signal; polling
and streaming pages must not stall every action. Report when a bound expires.
[Playwright actionability reference](https://playwright.dev/python/docs/actionability).

Represent progress with proposed typed states: prepared, authorized,
dispatch-started, observed-complete, observed-not-complete, and outcome-unknown.
Map these into the existing effect-sent/event system instead of adding a second
inconsistent effect ledger. A DOM state change is not necessarily the intended
effect; each workflow supplies a bounded postcondition such as a result row,
validation message, or stable completion marker.

On `page_changed`, re-observe and let the agent make a new decision against a
fresh target, without asking the user to locate it. Resolve from bounded region,
role, label and surrounding evidence, requiring a unique match. Remaining
ambiguity permits further bounded inspection, not invented user intent.
Never silently resolve an old approval to a different element using a fuzzy
selector. A fresh proposed action still passes current policy and grant checks;
only a genuinely uncovered action needs a new approval. Retry bounded reads
when safe. A lost response after dispatch remains
uncertain until positive evidence resolves it; absence of a confirmation is not
proof that a write was unsent. The existing provider retires ambiguous leases,
so recovery may need a fresh read-only lease, not reuse of the failed browser.
Reconcile automatically from task-specific result evidence, such as a durable
confirmation identifier or the expected result row. Distinguish verified
completion, an observed failure and inconclusive evidence. Verification reads
must not silently become a second submission, and lack of evidence must not
become a claim that the first submission failed. Ask for help only if a specific
user decision can resolve what the bounded checks could not.

Add a progress detector using semantic page evidence and workflow state, while
preserving the existing circuit breakers and synthesis reserve. Unrelated DOM
changes, timestamps or advertising must not reset the no-progress budget. Stop
with a useful explanation when repeated actions make no relevant progress. Put finite
attempt, time, token, and action budgets around every recovery branch.

**Exit evidence:** delayed SPAs and background polling complete without fixed
sleep inflation; changed targets never inherit approval; injected response loss
produces no duplicate server effect; false completion fixtures stay incomplete;
zero-progress tasks stop before exhausting the whole browser budget; routine
recoverable cases finish with zero extra questions or approvals when already
covered by current authority.

### W5 — Make authentication verifiable and recoverable

**Priority:** P1–P2. **Estimated effort:** 2–3 weeks. **Dependencies:** W1 and W4.

Add an optional, versioned site contract reviewed through the trusted
implementation and release process, with a positive signed-in marker,
negative markers, a safe verification page, and optional account confirmation.
The user supplies or confirms account intent and permission scope only when
missing; they do not inspect predicates or approve technical contract revisions.
Use declarative predicates over bounded visible structure; no model-authored
JavaScript and no cookie-name heuristics as proof. The service owns verification;
neither the model nor the client's "I'm signed in" action can assert readiness.

Use at least two independent signals for known sites, including a protected
view or authenticated-versus-empty comparison where practical. A public page
that looks identical with and without the session is inconclusive. Bind any
account confirmation to the profile generation and show only a user-approved
masked account label. Site markers improve reliability, not protection against
a malicious site deliberately lying about its own state.

Expose profile health: ready and last verified, needs sign-in, verification
inconclusive, expired, revoked, or unsupported authentication. Proposed new
statuses require API and native-client compatibility work. Check health before
an expensive run and when evidence indicates expiration; avoid frequent
background probes that create unnecessary traffic or rotate sessions.
Reuse a healthy saved session automatically. Prefer bounded service-side
verification and ordinary session renewal over a new sign-in ceremony; do not
introduce model-visible credentials or an automatic credential-entry path.
An inconclusive check receives bounded read-only investigation before a user
request, and must not demand sign-in repeatedly when another attempt cannot
resolve the underlying unsupported or unavailable condition.

Replace the mandatory chat reply after sign-in with durable, event-driven
resumption. A trusted successful verification event must match the pending
run, principal, profile and expected authentication ceremony/generation; a
page message, client claim or notification delivery is not proof. Resume once
despite duplicate events, reconnects or worker restarts. Release suspended
browser resources and reacquire a fresh lease and observation. Recheck the
account, run status, deadline, budget and current permissions before advancing
from the last verified workflow state. A cancelled, expired or superseded run
must not resume, and completed or uncertain actions must not be replayed.

Sign-in still invalidates the old generation's grants. Automatically continue
read-only verification where permitted; if the next mutation lacks current
authority, present its necessary scoped approval once. This plan does not
transfer an old grant to a new generation. Older clients may retain a manual
continuation fallback, but supported clients must not require a "Done" reply.
The runtime/API/native event contract and cancellation races need their own
design amendment before implementation; ADR-0163 currently uses a user reply.

Preserve native device sign-in as the preferred path. Support explicit
authentication-only origin sets for identity providers and an exact return
origin under a new contract, distinct from task/navigation grants. Redirects
must not silently add origins. Test cancellation, account switching, a client
changing server connection, and the old generation's grants becoming invalid.

Broader handoff is a separate feasibility study. Hosted IndexedDB works already;
WKWebView export parity, partitioned cookies, sessionStorage, passkeys, and
device-bound credentials do not follow automatically. Verify platform support
and handling limits before promising them. For non-transferable sessions,
prefer a dedicated local execution option in W10 or a clearly explained limit.

Remote ceremony compatibility experiments must preserve truthful browser
identity and the popup prohibition. Test browser mode, input events, network
placement, and transport independently. Do not remove the CSP transport guard
to improve TLS compatibility; any replacement must prevent the first unwanted
request and pass the existing concurrent-popup suite.

**Exit evidence:** false-ready fixtures fail; supported valid sessions verify;
wrong-account and expired-session cases become visible before ordinary task
actions; reauthentication invalidates all old grants; sign-in payloads never
reach API logs or model context; existing profile material survives failed setup;
valid saved sessions need no new ceremony; verified sign-in resumes eligible
work exactly once without a chat reply; repeated challenge checks emit one
actionable interruption per unresolved episode.

### W6 — Execute repeated tasks with guarded reusable workflows

**Priority:** P2. **Estimated effort:** 3–4 weeks. **Dependencies:** W3–W5.

Introduce a small typed workflow representation: observe, find a unique visible
target, act, wait for a bounded predicate, extract, branch on a closed result,
and stop. Start with hand-authored, reviewed recipes for repeated tasks. Do not
begin with an unrestricted script generator or a general workflow editor.
Select applicable trusted recipes automatically from task intent and current
site evidence. Users should not choose a recipe, approve technical steps or
re-enter answers already available in the task. Keep agent decisions for genuine
ambiguity and novel page states rather than routing ordinary branching to the user.

Every step resolves a fresh observation, checks current policy and grant scope,
consumes its own action use, dispatches serially, and verifies its postcondition.
A workflow run is not atomic: record partial completion and require explicit
handling of irreversible effects. Cancellation is checked between steps and
before dispatch. Limit loops, steps, elapsed time, output, and model fallbacks.

A learned recipe may propose semantic target descriptions and expected states,
but stays a draft until reviewed through the existing skill-authoring boundary
or a separately designed recipe approval surface. It must not learn cookies,
account-specific values, or new permissions from page content. Cache recipes
by site-contract version, task shape, and relevant browser/schema version;
bind execution to current policy, agent, and profile generation. Never cache a
live element handle, previous revision, approval decision, or completed write
as if it authorized the next invocation.
Trusted bundled recipe updates follow engineering review and deployment, not
per-task user approval. Keep any user-authored or learned-skill activation checks
required by the existing skill contract; fewer prompts does not authorize
automatic promotion of page-derived instructions into trusted workflows.

Begin with deterministic steps between model decisions. A bounded multi-step
tool can later reduce round trips, but it must still run the full authorization
and fresh-revision process per step. This explicitly requires an amendment to
the current one-action contract. Initially, encountering a named sensitive
consequence returns to ordinary approval; no recipe bypasses that boundary.

**Exit evidence:** recurring tasks meet the efficiency target at equal or better
success; changing labels/layouts causes a safe stop or fresh model decision;
revoking a grant between two steps prevents the second; a mid-workflow crash
resumes from verified state without replaying previous writes.

### W7 — Add visual understanding and direct human takeover

**Priority:** P2–P3, selective. **Estimated effort:** 2–4 weeks. **Dependencies:** W2–W5.

Add opt-in, bounded screenshots for pages whose visible content cannot be
represented semantically. Capture in the isolated browser service, mask known
sensitive regions before export, and bind every image to a page revision,
viewport, and capture time. Set proposed limits of one viewport and 1 MiB per
capture, with an explicit error or downscaling path. Sensitive/authentication
pages are not eligible for model capture. Redaction is not a guarantee that
every secret can be recognized: default off for unknown authenticated layouts,
and allow only approved capture surfaces until evidence supports expansion.

Use vision initially to understand charts and identify candidate controls that
can then be bound to real semantic elements. Coordinate-only actions on canvas
or inaccessible controls are a later contract: screenshot grounding alone
cannot supply live consequence checks. Such actions must not inherit a routine
task grant; retain human control when no safe target binding exists.

Add a "Take over" action in the client. Transfer control exclusively: stop new
agent dispatch, settle or mark the in-flight action uncertain, revoke stale
element references, and open a direct short-lived user channel. Authentication
input bypasses the model and ordinary event payloads. Resume only after the user
returns control, the service verifies page/profile state, and the agent obtains
a fresh observation. An account change advances the generation and ends grants.

Offer an optional redacted screenshot timeline for approved non-authentication
surfaces. Proposed retention is at most 24 hours, owner-readable only, excluded
from memory formation, with immediate deletion and access audit. Raw auth frames,
DOM snapshots, cookies, and network payloads remain outside durable diagnostics.
Pixel-perfect replay is not required for this phase.

**Exit evidence:** simultaneous human/agent input is impossible; takeover during
an in-flight submission reports uncertainty correctly; canary secrets remain
absent from captures; stale screenshots cannot authorize a new action; every
capture has enforced ownership, expiry, and deletion.

### W8 — Add files, frames, and multiple pages through explicit contracts

**Priority:** P3. **Estimated effort:** 4–6 weeks, split into independent slices.
**Dependencies:** W3–W5; W9 isolation before broad production expansion.

**Downloads first.** Add an exact approved download action and transfer from
the isolated runtime into the existing artifact system. Bind the approval to
the initiating control and website; enforce limits across redirects and the
actual response, not only a filename. Proposed initial ceiling: 25 MiB per file,
100 MiB per run, at most five files, with a streaming abort. Start with PDF,
plain text, and CSV. Inspect MIME and signatures, quarantine before release,
sanitize names, and never execute content. Malware scanning is defense in depth,
not a proof that a document is safe. Handle CSV formula content safely in previews.

**Uploads second.** Accept only a principal-owned artifact selected through a
trusted surface; verify its digest, size, and permitted destination immediately
before dispatch. The user sees what file goes to which website. No host paths,
automatic directory access, or profile-volume access. File selection may itself
upload data; mark the effect before setting the input. File transfers remain
outside standing/task grants unless a separately approved contract changes that.

**Frames third.** Add scoped frame identities and origins to observations and
actions. Resource-frame loading must not imply action permission. Observe and
act inside only explicitly authorized frame origins; exclude authentication
frames from agent control. Preserve frame/navigation epoch and target revision.
[Playwright frame support](https://playwright.dev/python/docs/frames) supplies
mechanics, not Veetbot's authority model.

**Multiple pages fourth.** Start with at most three explicitly created pages
inside one profile lease, one active page, and serial dispatch. Keep website-created
popups denied. Add explicit page identity and generation, independent revisions,
page close/switch semantics, and bounded resource use. A new page must have its
network guards installed before any website content executes. Controlled popup
adoption is a later research item, not an implication of multi-page support.

**Exit evidence:** transfer cancellation leaves no accessible partial artifact;
size and redirect attacks fail before exceeding bounds; uploads cannot select
another principal's file; frame origins cannot gain authority from CDNs; stale
page/frame references fail; concurrent popup regression tests continue to pass.

### W9 — Improve execution isolation, lifecycle, and capacity

**Priority:** P1 assessment; P3 delivery before broader concurrency.
**Estimated effort:** 3–5 weeks. **Dependencies:** W1–W2.

First measure the actual process tree, Chromium sandbox status, direct-egress
possibilities, key/profile-volume reachability, and resource pressure under the
current service limits. Current process/container hardening is valuable, but
does not prove that a compromised browser process cannot reach secrets mounted
for its parent service. Do not describe an unverified isolation property as a
production guarantee.

Propose separating the profile vault from browser execution workers. Only the
vault reads the encryption key and all-profile storage. Each worker receives
the minimum material for one profile/lease through an authenticated private
channel and has no vault filesystem mount, database credential, Docker socket,
or general orchestration credential. Its own session state is necessarily
available to the browser; other profiles and the encryption key are not.
Use per-worker process/network isolation and enforce public-HTTPS egress below
browser request interception. Verify Chromium's own sandbox in the chosen host
configuration. Container recipes for testing do not automatically establish
safe operation against untrusted sites.
[Playwright container guidance](https://playwright.dev/python/docs/docker).

Add bounded admission and fair queueing based on measured CPU/RSS costs, with
reserved capacity for owner takeover and authentication verification. Preserve
exclusive mutation per profile. Start with one host; select its concurrency
limit using measured peak memory plus at least 30% headroom, not an arbitrary
"hundreds of browsers" objective. Consider a pool of clean, never-authenticated
workers only after launch latency is proven significant. Do not share warmed
authenticated contexts across profiles.

If multiple service instances are introduced, use durable fenced ownership and
explicit lease epochs; process-local locks are insufficient. On restart,
invalidate old handles and in-flight ownership. Recover from a verified workflow
checkpoint, not a claim that the old DOM survived. Profile-state writes must be
generation-checked so late shutdown cannot overwrite a newer sign-in.

Pin the browser build with the application image; add dependency/security update
canaries, image identity in health, graceful drain for deploys, key-rotation and
restore rehearsal, and deletion tests covering traces and backups under the
existing operational retention policy. Propose critical-browser-patch triage
within 24 hours and qualified rollout within 72 hours, subject to test evidence.

**Exit evidence:** a worker cannot read another profile or the vault key;
direct network escape is blocked; queue overload is bounded and visible;
24-hour soak leaves no orphan Chromium/Xvfb processes; service restart,
revocation, and concurrent sign-in never produce stale state writes; rollback
restores a known browser/app pair without reviving old authority.

### W10 — Add a dedicated owner-device browser provider

**Priority:** P4, conditional. **Estimated effort:** 4–6 weeks.
**Dependencies:** W1, W4, W5, W9 and a separate device-execution design.

Device sign-in exists; full device-local agent execution is a different feature.
Prototype a dedicated Chromium profile on the owner's Mac, controlled through a
small paired local service and the existing device-channel identity. Reuse the
provider contract, policy/grant checks, sequence fences, observations, and effect
accounting. Do not attach to the owner's ordinary browser, expose a CDP port to
the model, or read unrelated browser profiles.

Choose placement through trusted user settings per website and task. Device
absence yields a visible offline outcome. Do not silently move a task to the
cloud or move cookies between placements. A new placement binding requires
explicit setup and invalidates assumptions tied to the old profile generation.
Enforce the same public-network boundary locally; access to the owner's LAN is
not inherited from running on the owner's machine.

The value is support for legitimate sessions that cannot be transferred or used
from the server, plus direct human intervention. It is not a promise that every
site permits automation. Mac is the initial target; do not promise unattended
iPhone background execution. Sleeping devices, app exits, network changes, and
an already-running task need explicit lifecycle outcomes.

**Exit evidence:** at least two owner-relevant workflows materially benefit;
all applicable provider contracts pass; no ordinary browser profile is accessed;
disconnect after dispatch remains uncertain; re-pairing or revocation blocks
the next action; hosted behavior remains unchanged.

## 6. Site contracts and reusable knowledge

A site contract is proposed declarative configuration with these bounded fields:

| Field | Purpose |
| --- | --- |
| Identity and version | Exact origins, contract version, supported browser builds, maintainer, last validation. |
| Authentication predicates | Positive/negative visible signals, safe verification location, optional masked account check. |
| Page families | Main content region, form/table structure, routine loading and error states. |
| Target descriptions | Semantic roles, names, nearby context, uniqueness requirements; no live handles. |
| Postconditions | Evidence that each supported task step succeeded or failed. |
| Capability needs | Frames, files, capture, or device placement required; unsupported requirements cause a stop. |
| Sensitive surfaces | Capture exclusions and consequence escalation hints; never permission exemptions. |
| Evaluation fixtures | Tasks and regression cases proving the contract works. |

Contracts do not carry credentials, cookie names as authority, general URL
wildcards, grants, raw scripts, or instructions overriding Veetbot policy.
Site-specific improvements must fit general contracts rather than adding a
privileged Duolingo tool. Keep a generic path for unknown sites, with lower
confidence and explicit unsupported features. The implementation team authors,
tests and versions the contracts; the user is not their technical maintainer.
Start with three curated contracts selected from already authorized actual use,
then expand from demonstrated compatibility gaps and regression evidence.
Comparative benchmark work remains deferred and is not a prerequisite for
repairing a supported workflow.

Learn from failures by proposing contract or recipe changes with a test case.
Review and version them before activation. Page-supplied instructions cannot
edit a contract, create memory, or train a workflow automatically. Reuse the
existing skill governance where applicable; do not create a second invisible
self-modification system.

## 7. Delivery sequence and staffing assumptions

Estimates are engineer-weeks including tests and documentation, assuming one
engineer familiar with the backend and access to Apple-client expertise. They
exclude owner waiting time, external-site outages, and final review queues.
The ranges overlap where one implementation serves multiple packages; do not
add every row mechanically or treat them as calendar commitments.

| Phase | Deliverable | Approximate cumulative elapsed time with one engineer | Exit condition |
| --- | --- | --- | --- |
| P0 | W1 baseline and the first W2 diagnostics; deployment acceptance inventory | Weeks 1–2 | Failures are reproducible and real Chromium is required. |
| P1 | W3 observations, W4 readiness/recovery, W5 authentication core; W9 isolation assessment | Weeks 3–8 | Existing workflows measurably improve; no boundary regressions. |
| P2 | W6 guarded workflows and selected W7 takeover/capture work | Weeks 9–14 | Recurring tasks meet efficiency goals; interruptions recover clearly. |
| P3 | W8 files/frames/pages plus W9 isolated execution and capacity | Weeks 15–24 | Useful capability expansion passes boundary and soak tests. |
| P4 | W10 device provider if server placement blocks valuable workflows | Additional 4–6 weeks | Demonstrated benefit and provider parity. |
| P5 | Broader site coverage, accessibility/vision improvements, operational tuning | Ongoing after P3 | Prioritized by measured failure frequency and owner value. |

An initial competitive release should target P0–P2, roughly 10–14 weeks, with
file downloads pulled forward only if a top workflow needs them. Full expansion
is a multi-month program. Two engineers could divide browser/backend and
Apple/diagnostics work after contracts stabilize; that is a staffing option,
not a claim that all work parallelizes. Do not accelerate schema and authority
changes by implementing incompatible halves simultaneously.

### Next implementation tranche — minimal user involvement

1. **Automatic verification and reconciliation:** richer completion predicates,
   unique fresh-target resolution and read-only handling of ambiguous outcomes.
   Prove zero extra prompts for covered routine recovery and zero duplicate writes.
2. **Session reuse and positive sign-in verification:** implement trusted site
   predicates for already selected tasks; make missing account/scope choices
   explicit without asking users to maintain technical definitions.
3. **Automatic resumption:** connect verified authentication to durable pending
   runs and native UI, preserving generation invalidation and current approval
   requirements. One necessary sign-in ceremony must not require a second chat
   acknowledgement. Test duplicate events, concurrent runs and cancellation.
4. **Meaningful progress and consolidated interruptions:** bound recovery by
   relevant workflow progress, retain task context, and show one actionable
   blocker instead of repeated questions or transient failure notifications.
5. **Guarded recurring workflows:** automatically select reviewed recipes and
   resume from verified checkpoints while checking authority on every action.

The first integrated fixture is: an authorized task encounters session expiry;
the service exhausts safe session recovery; the user completes one sign-in;
the service verifies the intended account and resumes the same run without a
"Done" message. Reads continue where permitted; any newly required mutation
approval is explicit and not repeated. The result is verified, and the test
server records no duplicate completed effect. A companion fixture starts with
a valid session and completes with no additional user interaction.

These are regression and product-acceptance checks, not a benchmark project.
Keep production rollout subject to the existing review and release gates.

### Construction status — ADR-0164

The ADR-0164 construction slice implements the next-tranche mechanisms: richer
completion evidence, configured account verification before session reuse,
durable verified resumption, meaningful wait progress, consolidated sign-in
questions and finite reviewed workflow execution. Focused regressions prove
bounded reads, positive and negative account cases, native question clearing,
one dispatch after response loss and no extra prompts for an existing standing
grant. Real Chromium completion fixtures count effects at the server.
Catalogs remain empty pending reviewed definitions for selected live sites;
this is not a claim of live-site coverage or production activation. The integrated
release gate and site acceptance remain required before rollout.

### Original kickoff sequence — historical

The initial sequence below predates the completed construction slices and the
owner's benchmark deferral. The next tranche above now determines the order;
do not restart baseline collection or repeat completed setup.

1. **Days 1–2:** reconcile deployed browser image, flags, client builds, task
   scopes, and outstanding acceptance evidence read-only. Select the twelve
   live task definitions and forty controlled scenarios. No private transcripts
   or credentials enter the repository.
2. **Days 3–4:** require real Chromium in the verification lane; add synthetic
   server-side postcondition counters and missing baseline metrics.
3. **Days 5–6:** reproduce large-observation projection behavior through the
   actual model-request boundary. Add a regression before any repair. Ship the
   smallest contract-compatible fix if one is established.
4. **Days 7–8:** deliver the first safe failure-stage timeline; benchmark existing
   settling, stale references, and ambiguous writes.
5. **Days 9–10:** publish baseline results and the ranked failure list; write the
   concrete observation/authentication ADRs and detailed slices supported by
   that evidence. Begin only work within the resulting authorized scope.

The first release should make current tasks visibly more reliable. Do not spend
the first month on a Chromium fork, a browser fleet control panel, or a generic
workflow designer.

## 8. Decision record and scope boundaries

The plan recommends the following future ADR topics. These are not assigned
ADR numbers and are not accepted decisions. Before implementing a changed
contract, write its proposed ADR, update the owning design and engineering plan
where needed, and obtain explicit approval for any weakened security boundary.
Planning these changes does not itself amend the current contract.

| Decision | Recommended proposal | Governing contract affected |
| --- | --- | --- |
| Observation/model projection | Versioned semantic observation, explicit coverage, budget-aware presentation and expansion | Browser, tools, context; ADR-0137 integration. |
| Authentication evidence | Declarative verification predicates and explicit authentication-only origins | Browser profile/authentication lifecycle; ADRs 0058, 0106, 0128. |
| Authentication resumption and interruptions | Trusted verification events resume the matching pending run once, with fresh state, current authority and one actionable notice per blocker | Runtime checkpoints, browser profile generations, HTTP/events and native clients; amend ADR-0163's reply-based suspension. |
| Workflow execution | Bounded recipe steps with fresh observation and per-step authorization | Browser one-action contract, tool execution, skills; ADR-0130 deferred batching alternative. |
| Visual capture and takeover | Separate owner and model channels; no auth capture; short retention; exclusive control | Browser trust boundary, artifact access, HTTP and Apple surfaces. |
| File transfer | Explicit artifact-based upload/download operations and per-transfer approval | Section 33, browser exclusions, artifact ownership, policy. |
| Frame/page authority | Explicit page/frame scope, bounded explicit pages, website popups still blocked | Browser revision/origin contracts; ADRs 0098, 0138. |
| Vault/execution separation | One-profile execution workers, vault-only key access, fenced lifecycle | Browser service, deployment and operational hardening. |
| Local execution | Dedicated device profile, trusted placement, identical policy enforcement | Section 29 device seam and Section 33; separate from existing device sign-in. |

Preserve human-only CAPTCHA/MFA and truthful browser identity. No stealth
fingerprint patches, automatic CAPTCHA solving, blanket grant expansion,
arbitrary browser JavaScript tool, general host control, or model-selected
credential/profile access is proposed. Existing API integrations remain useful
for tasks where the product already provides them, but they are measured
separately and cannot inflate the browser benchmark.

Do not expand into dynamic model routing, fine-tuning from private trajectories,
general standing grants, an enterprise multi-tenant product, or a new browser
engine under this program. A model switch would require its own authorized
evaluation; efficiency work here keeps the configured model fixed.

Service-worker support, arbitrary extensions, audio/microphone access, and
unrestricted clipboard use remain deferred until a selected workflow proves
their value and a separate contract defines containment. Native browser
rendering and richer page support can improve competitiveness without enabling
all browser features at once.

## 9. Test, rollout, and rollback discipline

For each implementation slice, derive the behavior from its governing design,
write the smallest meaningful failing regression or shared adapter contract,
record the expected red failure, implement, then run focused and relevant
partition checks. Public routes require validation, authorization, ownership,
failure, cancellation, and replay coverage. New browser behavior requires a
real-browser test, not only a mock of Playwright calls.

Maintain a test matrix across hosted/ephemeral and later device placements,
headed sign-in/headless task modes, profile generations, and old/new schema
versions. Include delayed responses, process kill, queue contention, expired
grants, multi-client cancellation, site structure drift, and reconnect after an
effect. Verify denial at the test server or network boundary when "nothing was
sent" is the claim.

Assert interaction counts as correctness outcomes: zero extra prompts with a
valid session and current authority; zero prompts for routine stale-target or
transient-read recovery; one actionable sign-in interruption for an unresolved
challenge; no "Done" reply after trusted verification; no duplicate prompt on
event replay or client reconnect. Necessary fresh authorization after a profile
generation change remains visible and separately counted. Cover wrong-account
verification, duplicate completion events, cancelled/expired runs, shared-profile
contention and a response lost after dispatch. Assert eventual completion or an
explicit bounded stop, so silent abandonment cannot satisfy the zero-prompt test.

Roll out one capability at a time: synthetic fixtures; consenting test account;
owner canary; then broader supported tasks. Feature flags should disable the
new capability while preserving the last stable provider path, but never turn
off enforcement. Shadow evaluation may inspect already-authorized observations;
it must not issue duplicate real writes.

Stop or roll back on any secret leak, unauthorized dispatch, duplicate effect,
incorrect account binding, or failure to honor revocation. Also pause expansion
if paired task success falls by more than five percentage points or p95 cost
or latency rises by more than 25% without a documented benefit. Small samples
require investigation rather than automatic statistical certainty.

Rollback stops new admissions, drains or cancels active work explicitly,
invalidates incompatible leases/references, restores the known app/browser pair,
and preserves uncertain-effect records. Migrations must be additive or have a
documented compatibility window. Never revive old grants or overwrite newer
profile generations while rolling back.

Follow repository delivery rules: measure verification phases and report them
before commits; run the prescribed final `make check` through `chunk validate`
on the sidecar; repair citations when cited documents move; bind review and CI
to the exact final head. A main PR, merge, and production deployment are separate
authorized stages. Verify the live application and documentation identities
after a production release. Plan-only documentation uses `make docs-check`.

## 10. Risks, tradeoffs, and decisions to revisit

| Risk | Response and decision trigger |
| --- | --- |
| Expanding too many capabilities at once | Deliver P0–P2 first; pull a later feature forward only for a named high-value task. |
| Cosmetic metrics conceal low task success | Ground truth and fixed task populations; publish unsupported and intervention rates. |
| Semantic ranking omits the important control | Explicit coverage and bounded expansion; evaluate at the model-request boundary. |
| Recipes become brittle or accumulate authority | Fresh targets, per-step checks, versioning, uniqueness requirements, safe stop on drift. |
| Better diagnostics leak private page data | Metadata by default; synthetic traces; separate opt-in capture and deletion design. |
| Generic login verification remains inconclusive | Curated positive predicates and clear UI; do not guess from cookies. |
| Reducing prompts hides failure or silently broadens authority | Require verified completion or an explicit bounded stop; deduplicate blockers while preserving account, consent, generation and approval checks. |
| A site binds authentication to device or network | Dedicated local provider if justified; no automatic credential migration or disguise. |
| Screenshot grounding bypasses consequence checks | Semantic binding first; coordinate-only control remains a separately approved capability. |
| Browser process compromise reaches profile secrets | Vault/worker separation, OS enforcement, patch cadence, and intrusion-boundary tests. |
| Capacity work consumes months without benefit | Measure owner workload first; one host and bounded admission before a fleet. |
| Existing classifier misses non-English or image-only sensitive labels | Expand hostile/localized fixtures; use richer evidence only to escalate; never advertise complete semantic enforcement. |
| Website scripts cause effects outside visible control semantics | Retain documented limits of task grants, exact scope and network fences; do not call UI classification transactional isolation. |

## 11. Implementation map and evidence sources

The paths below identify current integration points, not a mandate to refactor
all of them. Prefer small components extracted only when their work package
requires them.

| Work | Current integration points |
| --- | --- |
| Browser actions and page capture | `src/agent_core/adapters/browser/playwright.py`, `src/agent_core/domain/browser.py`, `src/agent_core/ports/browser.py` |
| Model observation presentation | `src/agent_core/tools/browser_results.py`, `src/agent_core/domain/tool_output.py`, `src/agent_core/context/builder.py` |
| Session lifecycle and profile material | `src/agent_core/browser_control_plane/sessions.py`, `runtime.py`, `filesystem.py`, `handoff.py`; `src/agent_core/adapters/browser/hosted_provider.py` |
| Policy, grants and approvals | `src/agent_core/application/browser_grants.py`, `browser_task_grants.py`; `src/agent_core/domain/browser_classification.py`, `browser_act_views.py` |
| Native experience | `clients/apple/Veetbot/Views/WebsiteAccessActions.swift`, `BrowserActionApprovalCard.swift`; existing settings, run, and authentication views |
| Verification | `tests/real_browser_support.py`, `tests/unit/test_browser_runtime_real_chromium.py`, `tests/integration/test_browser_task_grant_live_runtime.py`, browser contract suites, `tests/gates/test_browser_m10.py` |
| Runtime isolation | `deploy/docker-compose.production.yml`, `deploy/browser-profile-service.Dockerfile`, browser service configuration and proxy modules |

Primary repository evidence:

- [Normative capability and rollout contract](plan/engineering-plan.md#33-authenticated-browser-automation).
- [Detailed browser design](plan/browser-automation.md), including its explicit
  authentication and compatibility limitations.
- [ADR-0058: provider-neutral browser architecture](adr/0058-authenticated-browser-automation.md).
- [ADR-0098: automatic website resources](adr/0098-automatic-browser-site-resources.md).
- [ADR-0106: truthful headed authentication](adr/0106-headed-authentication-ceremony.md).
- [ADR-0127: renewable leases](adr/0127-browser-leases-renew-while-the-run-needs-them.md).
- [ADR-0128: native sign-in handoff](adr/0128-device-sign-in-session-handoff.md).
- [ADR-0129: scoped task approval](adr/0129-time-boxed-task-grant-from-the-approval-card.md).
- [ADR-0130: task budgets and observation settling](adr/0130-browser-task-budget.md).
- [ADR-0138: popup prevention and transport tradeoffs](adr/0138-browser-popup-denial-before-creation.md).
- [ADR-0141: client-managed task scopes](adr/0141-client-managed-task-approval-websites.md).
- [Project state](status/project-state.yaml), [milestone mapping](plan/milestone-map.md),
  and [readiness](plan/readiness.md). Their existing completion claims do not
  establish the proposed task-quality targets.

Glen history helped identify the authentication and lifecycle decisions to
verify: *Fix browser authentication readiness* (2026-09-17,
`observation:cmu5wv15n000zgm0afv2p36su`), *Veetbot Duolingo Access*
(2026-09-25, `observation:cmuhagasy0012gm0aw3nlwaag`), and *fix(browser): clarify
task approval and preserve popup interception* (2026-09-28,
`observation:cmulpe18j001agm0air04z6qz`). These are historical retrieval sources,
not fresh production evidence; the linked ADRs and inspected code govern the
baseline. Unverified old operational failures were not adopted as current bugs.

External references are limited to underlying open-source mechanics:
[Playwright authentication](https://playwright.dev/python/docs/auth),
[storage-state API](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-storage-state),
[actionability](https://playwright.dev/python/docs/actionability),
[frames](https://playwright.dev/python/docs/frames),
[tracing](https://playwright.dev/python/docs/trace-viewer), and
[container guidance](https://playwright.dev/python/docs/docker).
The current code's IndexedDB support was verified directly. Broader device
handoff and local execution remain feasibility work. No hosted browser vendor
is required by this proposal.
