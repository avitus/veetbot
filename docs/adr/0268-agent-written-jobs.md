# ADR-0268: Jobs the agent writes, run once or on a schedule

- Status: Accepted for MVP implementation on 2026-10-29. The owner set the
  direction on 2026-10-27 and clarified the MVP on 2026-10-29:
  - the platform owns scheduling;
  - the Veetbot agent writes the code a job runs, for one-time and scheduled
    jobs;
  - that code may mix API calls, computation and model calls;
  - for now all of it lives in the public repository.

  The owner explicitly instructed: update this ADR and begin implementation.
  Production validation is still pending; implementation authorization does not
  authorize a pull request, deployment or credential installation.
- Date: 2026-10-29
- Amends:
  - ADR-0240 decision 8, which defers deterministic collectors;
  - ADR-0240's repository rules, which let the Cockpit App write only
    `cockpit/widgets`;
  - ADR-0254, under which every feed refresh is a model run.
- Related: ADR-0072, ADR-0073, ADR-0213, ADR-0240, ADR-0243, ADR-0254
- Answers: Cockpit open question 2 (`cockpit.md`, Open questions). For jobs
  only, it also answers sandbox open question 3 (who chooses egress
  destinations).
- Scope: a job package on a public branch, a new schedule task kind,
  deferred authoring and execution tools, a sandbox broker and one
  owner-approved credential. This is an independently authorized MVP workstream
  recorded in project state; it does not advance a milestone or gate.
- Detailed design: `docs/plan/scheduling.md`,
  `docs/plan/sandbox-isolation.md`, `docs/plan/cockpit.md`,
  `docs/plan/builtin-tools.md`, `docs/plan/policy-and-approvals.md`

## Context

A schedule has one kind of task: an agent run. When it fires, its instruction
becomes the first message of a new conversation, and a model works out what to
do. That suits work that needs judgment, such as the weekday briefing. It does
not suit work whose steps never change.

The owner's GitHub pull request widget shows the cost. Its run of 2026-10-27
at 7:10 PM Pacific had:

- five model steps, about 66,000 input tokens and USD 0.55;
- fourteen tool calls, eleven of them `web.fetch` reads of `api.github.com`;
- one `cockpit.feed.publish` of a fixed, closed schema.

The model only reshaped eleven answers into enum values and counts. The run at
8:00 PM cost USD 0.23 in three steps. Hourly refresh costs about USD 5 to 13 a
day, and refresh every ten minutes would cost USD 33 to 79. Every scheduled
run shares USD 25 a day and USD 250 a month. The reads are also
unauthenticated, so they share GitHub's limit of 60 requests an hour per
address.

The agent used a schedule because nothing else could run unattended. It cannot
leave code on the platform, so it left instructions. Instructions need a model
to read them, every time.

In chat the gap is the same. `sandbox.run_command` runs throwaway commands
with no network. The agent cannot save a program, give it approved access to a
service, and run it again later.

## MVP scope and delivery

The agent chooses the appropriate combination of code and model calls for the
owner's task. The failure being repaired is unnecessary model reasoning over
fixed steps, not the presence of model calls. Code handles structured reads,
filtering, counts and formatting; a job calls a model when interpretation,
summarization or other judgment is useful. Both paths belong in the MVP. A
model is never invoked automatically merely because a job starts or finishes.

The first complete workflow is: the owner asks Veetbot for a GitHub pull request
monitor; Veetbot writes and tests its program, runs it once, schedules it every
five minutes and publishes into the existing widget's feed. A second example
fetches review text with code, asks a model for a summary and publishes the
result. These are examples of the general job capability, not special platform
collectors. The model budget defaults to zero for a code-only grant; an approved
positive budget enables model calls in a mixed job.

Reuse the existing execution service and bridge transport, scheduler, gateway,
feed validation, approval and budget mechanisms, and public-code publication
pipeline. Keep their existing protections. Do not redesign the widget renderer,
build a general credential UI, add advanced grant administration, transactional
feed publication, sophisticated replay recovery, shared rate-limit coordination
or a comprehensive fixture framework as prerequisites for the MVP. Later work
can strengthen these where real use demonstrates the need.

Implement in this order:

1. Execute Python through the existing sandbox bridge. Prove a code-only job
   makes no model request and a mixed job makes exactly the model requests its
   code asks for. Return the result and useful errors to the caller.
2. Add saved packages and the four broker operations using existing platform
   services, the GitHub reader credential, and private approved inputs/access.
   Expose authoring and one-time execution to the production Veetbot agent.
3. Add the job task to the existing scheduler and connect the existing widget.
4. Verify both workflows in production. Pause the old model-driven collector
   only after the job publishes a working feed; retain that schedule for manual
   fallback. Never deliberately run both collectors against that feed.

## Decision

1. **A job is a saved program the agent writes.** A job package holds:
   - a Python 3 entry module;
   - a manifest with its title, its input schema, the broker calls it uses and
     the feeds it may publish.

   A job takes typed inputs and returns a bounded JSON result. The same
   package can run once or on a schedule.
2. **Its code lives in the public repository.**
   - Packages are stored on a new orphan branch, `cockpit/jobs`, one directory
     per job, beside `cockpit/widgets`.
   - The Cockpit GitHub App's repository rules widen to let it write this
     branch too, and nothing else.
   - Jobs reuse the widget pipeline: the commit outbox, commit before activate,
     revert, archive and restore, and the import of human commits. The owner
     and Claude Code can therefore edit a job like any code.
   - Each revision is a commit. The platform keeps its own copy of every
     revision a schedule or run is pinned to, so a GitHub outage delays edits
     but never stops a run.
3. **Public code, private values.** A package must contain no personal data,
   as widgets must not.
   - The privacy screen that checks widgets checks job packages too.
   - Repository names, account ids, addresses and every other personal value
     arrive as inputs.
   - The **grant** is private and is held with the owner's approval. It
     contains:
     - the input values;
     - the hosts the job may call;
     - the credential bound to each host, if any;
     - the feeds it may publish;
     - its limits: wall-clock time (default 60 s), requests (default 50),
       response bytes, and model cost per run (default USD 0).
4. **The code runs in the sandbox and reaches the world only through a
   broker.**
   - Each run gets a fresh sandbox from the existing execution service:
     gVisor, a read-only root, the standard limits, an empty environment, no
     credentials and networking off.
   - Its only channel out is a per-run socket to a job broker. The broker runs
     outside the execution service, so the execution service still holds
     nothing worth stealing.
   - The job exports `main(inputs, job)`. The preinstalled job library supplies
     the `job` context with the four operations below. Package installation
     stays denied.

   The library has four calls, and each is a broker request:

   | Call | What the broker does |
   | --- | --- |
   | `http.request` | Checks the host against the grant and the operator superset, adds the credential the grant binds to that host, applies the address denylist with one resolution and one dial, and returns the response |
   | `model.complete` | Sends one prompt through the model gateway with no tools, charges the cost to the run's budget, and returns text that the code must treat as untrusted |
   | `feed.publish` | Publishes to a feed the grant allows, validated as `cockpit.feed.publish` validates |
   | `log` | Records a bounded line in the run's log |

   That covers the owner's three ingredients. API calls go through
   `http.request`. Code execution is the job itself. Model calls go through
   `model.complete`, made only when the code asks.
5. **Two ways to run a job.**
   - **Once.** From a chat, `job.run` runs a pinned revision with inputs under
     a grant. It returns the result and the log to the agent as an untrusted
     tool result. A run that needs no host, credential, feed or model asks no
     approval. A run that needs any of them asks once, for that grant, and
     later one-time runs under the same grant do not ask again.
   - **On a schedule.** A schedule revision's task is either `agent_run`
     (today's instruction and pinned agent, unchanged) or `job` (a pinned
     revision and a grant). Every cadence applies, including `ONCE` at a
     chosen time. Overlap, misfire handling, Run-now, refresh on entry, the
     failure pause and notifications apply to both kinds. `INTERVAL` stays
     limited to feed-keeping schedules (ADR-0254), and a job counts as
     feed-keeping when its grant allows a feed. Creating a job schedule asks
     the owner, and the approval card shows the cadence and the grant in plain
     words.
6. **Code changes are governed like widget changes.**
   - A code commit from the owner's own turn ships without a per-change
     approval. The approval rule is an exact-name allow for `job.manage`
     under the publication side effect. Other publication tools keep their rules.
   - A commit from a turn influenced by untrusted content asks.
   - A change that widens a grant always asks again. That includes a new host,
     a credential, a feed or a higher limit.
   - A schedule stays pinned to its revision until the owner approves moving
     it, as schedules stay pinned to agent versions today.
7. **Who chooses egress.** The operator sets a superset. Its default is public
   HTTPS on port 443, with the existing hostname and address-denylist checks.
   The owner approves each grant's exact host list, and the broker enforces
   both. The model can propose a host, and only the owner's approval admits
   it. This keeps sandbox rule 6: a destination is never model-settable.
8. **Credentials stay with the platform.** A credential is named in the grant
   and bound to one host. The broker adds it to a request, and the job's code
   never sees it. The first credential is a read-only GitHub App:
   - permissions `metadata`, `pull_requests`, `checks`, `statuses` and
     `contents`, all read-only;
   - installed only on repositories the owner picks;
   - named `github-reader`, bound to `api.github.com`;
   - its key delivered with systemd `LoadCredential` to the broker's role
     only.

   A general credential store the owner fills from Settings is a later
   decision.
9. **The agent writes, tests and fixes jobs.** A deferred builtin,
   `job.manage`, creates, updates, archives and restores packages. A trial
   runs the same program and returns its result and useful errors for repair.
   With no grant it has no host, credential or model; with approved access it
   can exercise real HTTP and model calls. Trial feed publishes go to a preview.
   A trial never grants access itself. The agent can fix its code and retry
   without a separate testing framework. `job.run`,
   `schedule.create` and `schedule.update` gain the job task. Earlier tool
   versions stay registered unchanged for pinned runs.
10. **Outcomes.**
    - A run succeeds when the job returns. It fails when it raises an error,
      exceeds a limit or times out.
    - A failed scheduled run counts toward the failure pause and notifies as
      any scheduled run does.
    - A successful feed-keeping job sends no notification, because the widget
      is its report.
    - A successful other job sends its result's summary line under the
      schedule's notification setting.
    - The schedule browser shows the run's log and its broker requests, but
      never a credential, a response body or a model prompt.

## Consequences

- Fixed work costs no model tokens. Rewritten as a job, the GitHub widget makes
  one GraphQL query per repository and no model call. It can refresh every
  five minutes for USD 0 in model cost and twelve scheduled executions an hour.
  Pagination and refresh on entry may add queries. GitHub GraphQL limits are
  measured in points per installation; actual usage is checked in the smoke.
- Judgment stays available where it pays. A job can fetch a thread and call
  `model.complete` once to summarize it, at a cost its grant caps. Briefings
  and research stay agent runs.
- The agent gains a reusable tool kit. A one-time job it wrote for one chat
  can be run again, or scheduled, without being rewritten.
- Job code is public. Anyone can read what each job does, but not what it
  reads, for whom, or with which credential. The privacy screen is what keeps
  that line, so it now guards a second kind of package.
- Inputs make a later step cheap: a widget could call a job on demand with
  typed arguments. That is not part of this decision.
- The owner creates the GitHub reader App once and installs it on the chosen
  repositories, and widens the Cockpit App's repository rules to
  `cockpit/jobs`.
- Rolling back is setting `AGENT_JOBS_ENABLED=0`. Job runs are then refused
  with a reason, and a job schedule pauses after its failure limit. A feed's
  widget shows stale, never wrong. Agent-run schedules are untouched.
- Moving job code to a private repository later changes only where packages
  are stored. The grant already keeps personal values out of the code, so
  nothing else changes.

## Hard gates

- A job sandbox has networking off. A direct connection from job code fails at
  the network namespace.
- The broker refuses:
  - a host outside the grant or the operator superset;
  - a private or metadata address;
  - a second resolution.
- A credential appears only in the broker's outbound request. It never
  appears in the sandbox environment, the job's result, the log, an event, a
  feed or the repository. The test uses a canary credential.
- The privacy screen rejects a job package that contains a personal value,
  using the widget screen's adversarial fixtures.
- `model.complete` charges the run's budget, stops at the grant's per-run cap,
  and offers no tools.
- `feed.publish` refuses a feed the grant does not allow.
- A trial without a grant reaches no host, credential or model; an approved
  trial uses only its approved access and publishes only to its preview.
- Widening a grant asks for approval, and so does a commit from a turn
  influenced by untrusted content. A code-only commit from the owner's own
  turn does not.
- A pinned run executes the platform's stored copy of its revision with
  GitHub unreachable.
- Exceeding any limit fails the run, and a scheduled failure counts toward the
  failure pause.
- With the flag off, no job runs, and agent-run schedules behave exactly as
  before.

## Validation

This decision is accepted for implementation. MVP production acceptance remains
pending until all of these have happened:

- The GitHub widget runs as a scheduled job in production for a day, with:
  - every snapshot at most 1.5 times its interval old;
  - no model cost recorded for that schedule;
  - no unexpected broker refusal.
- One one-time job written by the Veetbot agent in a chat runs under an approved
  grant, and runs again without asking.
- A mixed job fetches structured data with code, explicitly calls a model for
  useful judgment, then processes and returns or publishes its answer. Its
  recorded model calls match those requested by the code; there is no automatic
  model planning or synthesis around execution.
- The owner sees the actual widget update, and the agent receives useful errors
  from an unsuccessful trial so it can repair and rerun the job.

The production-day check evaluates the feature's usefulness. It is not a
prerequisite for starting implementation, and it does not pull deferred
hardening into the MVP.

## Implementation checkpoint — 2026-10-29

The MVP is implemented on `agent-jobs`, behind the default-off flag. It now
includes saved revisions and private grants in memory and PostgreSQL, a
separate job publication namespace, commit-before-activate and human import,
archive/restore/revert, the four broker operations, explicit model budget
accounting, agent tools and pinned job schedules. The maintenance role owns
publication; run workers own the lazy GitHub reader credential. No new runtime
dependency was introduced.

`job.manage` and `job.run` are deferred tools. The exact-name publication rule
admits generic code authored for an owner turn, retaining approval escalation
when the turn consumes untrusted content. This rule uses a separate jobs policy
identity; unrelated model and memory evidence keeps its original policy binding.
Existing grants can be reused; new external access and expansions ask for
approval. Schedule creation version
1.7.0 and update version 1.2.0 support job pins while earlier versions keep
working. A job run bypasses model planning and final synthesis; only explicit
SDK completions use the model gateway and ordinary run ledger.
Failed completions stop the job after recording billed usage, so caught errors
cannot reuse the grant allowance. Job tools use existing non-idempotent recovery:
an interruption after the effect watermark yields an uncertain outcome instead
of replaying effects, including approved trial HTTP calls. No new recovery
subsystem is introduced.

The generic packages under `samples/jobs/` demonstrate a code-only GitHub PR
collector and mixed review summarization. The collector's final mapping must
match the existing widget's feed schema at activation. [The operator guide](../jobs.md)
covers credentials, branch rules, private inputs, publication state, trials,
scheduling, rollout and rollback. Credential installation, delivery and the
production-day validation remain unperformed and require owner authorization.
No milestone or gate moves, and the deferred hardening remains deferred.

Verification covers process execution and the sandbox contract, publication and
lost-response reconciliation, privacy rejection, local revision execution,
HTTP resolution/credential boundaries, explicit model accounting, feed previews,
grant reuse, approval escalation, typed schedule materialization, grant repins,
zero-model execution and flag-off refusal. PostgreSQL verification runs the
same job store contract and checks namespace isolation, upgrade/downgrade and
data-loss refusal. The red tests first failed on missing execution and services,
wrong namespace, missing schedule fields, redundant approval, a swallowed
bridge limit and a repin retaining the old zero-model limit. No existing test
was weakened to obtain passing evidence.

Final verification results are recorded in project state. These are local
synthetic checks, not live GitHub, provider, gVisor or production acceptance
claims. The earlier unrelated static failure,
`test_family_probe_checks_the_names_the_overview_delivers`, occurred on the
original development base; the isolated main-based PR passes the full static
partition, as recorded in project state.

## Integration with the recall release

PR 210 delivered the separately accepted MVP while PR 211 was reviewing the
older proposal. The accepted MVP and its implementation checkpoint above
supersede that proposal's unaccepted scope and activation claims. This recall
release does not enable jobs, install its credential, or perform its production
acceptance. The jobs flag must remain off for this release's recall admission.

The following security requirements identified in review of the earlier
proposal remain open for the jobs activation assessment; the existing MVP tests
do not establish them:

- Screen all owner-supplied private authoring values and grant inputs before
  public publication, including human edits; a post-publication import screen
  cannot remove a prior public disclosure.
- Bind reuse of external execution authority to the approved code revision,
  and retain the operator destination restriction and applicable sandbox policy
  authorization. Publication approval alone must not grant execution authority.
- Refuse credential echoes, including supported encoded forms, before any
  response enters results, logs, events or feeds. Direct JSON-string redaction
  is not evidence for arbitrary encoded echoes. Credential hosts remain trusted
  security principals; HTTP and redirects remain refused.

These are retained requirements, not completed fixes or permission to activate
jobs. A subsequent jobs activation must resolve them with evidence or obtain
an explicit owner decision on any proposed change to the security contract.
The current recall release preserves the accepted MVP behind its disabled flag.

Tool versions introduced by jobs are retained for pinned runs. ADR-0266 retires
only the audited obsolete contracts it names; it does not retire the new jobs
schedule contracts. An unfinished run keeps its exact registered contracts
across the builder bump under the planner's existing identity checks.

## Alternatives considered

- **A GitHub feed built into the platform.** This fixes one widget and leaves
  every future widget on paid model runs.
- **Keep the model, with a leaner instruction or a cheaper model.** The cost
  still grows with the cadence, and model routing is not authorized.
- **Store job code privately.** This could be in Veetbot's database or a
  private repository. It hides what each job does. The owner chose the public
  repository for now. The grant keeps personal values private either way, so
  the choice can be revisited without redesign. Pierre's Code Storage is an
  option if Veetbot ever hosts many users.
- **Give the sandbox network access through the egress proxy.** Credentials
  would then have to live in the sandbox, where agent code can read and leak
  them. The proxy also cannot attribute or cap model calls.
- **Let jobs call any tool through the tool pipeline.** That would make a job
  an unattended agent with no model to judge approvals. The four broker calls
  are the whole surface on purpose.
- **Give each widget its own server code.** That would be a second place for
  agent code to run, with its own approvals. A widget that needs server work
  uses a job.
- **TypeScript instead of Python.** Widgets are TypeScript. But the sandbox
  image, the chart tool (ADR-0243) and the agent's existing sandbox code are
  Python, and a job renders nothing.
