# ADR-0268: Jobs the agent writes, run once or on a schedule

- Status: Proposed. The owner set the direction on 2026-10-27 and 2026-10-29:
  - the platform owns scheduling;
  - the Veetbot agent writes the code a job runs, for one-time and scheduled
    jobs;
  - that code may mix API calls, computation and model calls;
  - for now all of it lives in the public repository.

  The owner has not yet accepted this decision or authorized the work.
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
- Scope: a job package on a public branch, a new schedule task kind, one
  deferred builtin tool, a broker that a sandboxed job calls, and one
  owner-approved credential. This becomes a new workstream once the owner
  authorizes it in project state.
- Detailed design, once accepted: `docs/plan/scheduling.md`,
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
     revert, archive and restore, and the import of human commits. Human edits
     first enter a private working copy and pass the same privacy screen before
     publication; direct human pushes to the public jobs branch are disabled.
     The importer rechecks published revisions as defense in depth, not as the
     first screen. The owner and coding agents can edit jobs through this path.
   - Each revision is a commit. The platform keeps its own copy of every
     revision a schedule or run is pinned to, so a GitHub outage delays edits
     but never stops a run.
3. **Public code, private values.** A package must contain no personal data,
   as widgets must not.
   - The privacy screen that checks widgets checks job packages too. Its
     comparison set includes server-supplied names, private settings, commit
     messages, grant input values, and all personal values supplied by the owner
     in the authoring context, including values supplied before a grant exists.
     Missing comparison context refuses publication; it is not an empty set.
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
   - The job uses a small preinstalled library, `veetbot_job`. Package
     installation stays denied.

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
     tool result. A run that needs no host, credential or model asks no
     approval. A run that needs any of them asks once for the exact grant and
     pinned revision together. Later one-time runs under that same approved
     pair do not ask again. A different revision requires a new execution
     approval, even if its publication was allowed without a prompt or its
     earlier development was influenced by untrusted content.
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
     approval. The approval rule is an exact-name allow for the publication
     side effect, the rule widgets use.
   - A commit from a turn influenced by untrusted content asks.
   - A change that widens a grant always asks again. That includes a new host,
     a credential, a feed or a higher limit.
   - A schedule stays pinned to its revision until the owner approves moving
     it, as schedules stay pinned to agent versions today.
7. **Who chooses egress.** The broker enforces both the operator-configured
   allowlist and an approved `SANDBOX_NETWORK` resolution, as ADR-0029 requires,
   as well as the grant's exact host list. There is no default all-public-host
   allowance. Public HTTPS on port 443, hostname and address-denylist checks
   further restrict admitted destinations. The model can propose a host, but
   owner approval cannot bypass the operator allowlist or the policy resolution.
   A missing or expired resolution refuses the request. This preserves sandbox
   rule 6: a destination is never model-settable.
8. **Credentials stay with the platform.** A credential is named in the grant
   and bound to one host. The broker adds it to a request, and the job's code
   never sees it. Every credential-bearing request requires HTTPS, even if an
   operator otherwise permits HTTP. Redirects must preserve HTTPS and the exact
   credential-bound host and pass the same destination checks; a downgrade or
   different host is refused before forwarding any credential. The first
   credential is a read-only GitHub App:
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
   action runs a revision once with no grant. That gives it no host, no
   credential and no model, and its feed publishes go to a preview. The agent
   sees the result and the log, and a trial asks no approval. `job.run`,
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
  five minutes for USD 0 in model cost and 24 queries an hour per repository,
  well inside the 5,000 an hour an authenticated App gets.
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
  - a host outside the grant or the operator allowlist;
  - a missing, expired or mismatched `SANDBOX_NETWORK` approval;
  - a private or metadata address;
  - a second resolution.
- HTTP credential requests and redirect downgrades are refused, including
  when the operator's general egress configuration permits HTTP. Redirects
  outside the credential's exact host are refused before credential forwarding.
- Reusing an approved grant with a different one-time job revision requires
  a fresh execution approval; publication approval never substitutes for it.
- A credential appears only in the broker's outbound request. It never
  appears in the sandbox environment, the job's result, the log, an event, a
  feed or the repository. The test uses a canary credential.
- Before any public commit, the privacy screen rejects a job package that
  contains a personal value. Required adversarial fixtures cover a repository
  slug supplied before a grant exists and copied into code, a grant input copied
  into code, missing comparison context, and a human edit that would otherwise
  publish before import. Each rejection leaves no public commit or Git history.
  These are implementation gates for this proposed ADR, not executed fixtures.
- `model.complete` charges the run's budget, stops at the grant's per-run cap,
  and offers no tools.
- `feed.publish` refuses a feed the grant does not allow.
- A trial reaches no host, no credential and no model.
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

The owner accepts this decision when both of these have happened:

- The GitHub widget runs as a scheduled job in production for a day, with:
  - every snapshot at most 1.5 times its interval old;
  - no model cost recorded for that schedule;
  - no unexpected broker refusal.
- One one-time job written by the agent in a chat runs under an approved
  grant, and runs again without asking.

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
