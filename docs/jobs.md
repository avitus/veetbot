# Agent-written jobs MVP

A job is a saved Python program. Veetbot writes it in chat, tests it, saves its
public source and runs it with private inputs. Fixed reads, filtering, counts
and formatting use code. A program may explicitly request a model completion
when the task needs interpretation or judgment. Starting or finishing a job
never adds a model call.

The implementation is behind `AGENT_JOBS_ENABLED=0` by default. ADR-0268 is the
accepted decision. Production acceptance requires the live workflows described
there; local tests do not establish production freshness or provider access.

## Enable the feature

1. Apply the normal database migration and deploy the application, sandbox and
   Cockpit builder images from the same revision. The schema revision is
   `d8a268b10301`. Rollback to an older schema refuses to discard saved jobs or
   pending job publication.
2. Give the existing Cockpit publishing App access to the `cockpit/jobs` branch
   under its repository rules. The maintenance role uses its existing
   `AGENT_COCKPIT_GITHUB_*` configuration and key to publish and import jobs.
   Keep its widget branch setting at `cockpit/widgets`; jobs select their own
   fixed branch. The App cannot publish arbitrary paths through these adapters.
3. Create a separate GitHub reader App, install it only on the chosen
   repositories, and grant read access to Metadata, Contents, Pull requests,
   Checks and Commit statuses. Set `AGENT_JOBS_GITHUB_APP_ID` and
   `AGENT_JOBS_GITHUB_INSTALLATION_ID` in the application environment.
4. Mount its private key only into run workers through a systemd drop-in:

   ```ini
   [Service]
   LoadCredential=jobs-github-reader:/etc/veetbot/secrets/jobs-github-reader
   Environment=AGENT_JOBS_GITHUB_KEY_FILE=%d/jobs-github-reader
   ```

   Apply this to each role that executes runs. The API, scheduler, maintenance
   publisher and execution service do not need the reader key. The key is read
   lazily, only for a credentialed HTTP call. Do not put it in a job, grant,
   model prompt, sandbox environment or application environment value.
5. Add `jobs.write,jobs.run` to the owner's application `AUTH_SCOPES` and
   `jobs.run` to the scheduler's authority. Feed jobs also need the existing
   `cockpit.read,cockpit.write` scopes. Set `AGENT_JOBS_ENABLED=1` on the
   application and run workers; restart the normal services, including
   maintenance. Existing chat sessions may keep their old tool roster: use a
   new chat when first enabling the tools.
6. Optionally set `AGENT_JOBS_ALLOWED_HOSTS=api.github.com,example.com` to
   restrict the operator host superset. An empty setting permits public HTTPS
   hosts that an individual grant approves. Every grant still names exact
   hosts; private addresses, redirects and arbitrary credential bindings are
   refused.

The four SDK operations are synchronous:

```python
def main(inputs, job):
    response = job.http.request("GET", inputs["url"])
    text = job.model.complete("Summarize this evidence: " + str(response["json"]),
                              max_output_tokens=400)
    job.log("Summary completed.")
    return {"summary": text.splitlines()[0]}
```

`job.http.request` returns `{status, json}`. `job.model.complete` returns text.
`job.feed.publish(feed_id, data, expected_interval_seconds=300, schema=schema)`
uses the existing feed validator. `job.log(message)` records at most 2,000 bytes
per line. Declare operations in `manifest.calls` and feeds in `manifest.feeds`.
Programs receive no tool roster, credentials or direct network access.

## Create a widget with one approval

Enable Cockpit and both scheduling roles with `AGENT_COCKPIT_ENABLED=1`,
`AGENT_SCHEDULE_API_ENABLED=1` and `AGENT_SCHEDULE_WORKER_ENABLED=1`, in addition
to jobs. The owner's scopes must include `cockpit.write,jobs.write,jobs.run`
and `schedule.write`. Both installation tools require all four scopes and all
these features, even for a widget-only proposal without a collector.

Ask Veetbot to prepare the complete widget and collector. It uses
`cockpit.prepare` to check the packages and private access, repairs any errors,
and calls `cockpit.install` only when ready. One card summarizes the public
code, private inputs, network/credential access, feeds, model budget and refresh
interval. Approve once to install everything. New access is not exercised during
preparation: trials use fixtures or an existing approved grant. Collection
starts at the next interval boundary. Pending publication resumes with the same
proposal and approval; changed proposals need a new approval. This ADR-0270
repair requires deployment before it is available in production.

## Create and run a job

In a new chat, ask Veetbot to create a generic Python job for the task. The
`job.manage` tool offers create, update, read, archive, restore and trial.
Updates name the expected live revision. Restore can select an older revision,
so it also provides revert. Publication is asynchronous: a `pending_commit`
result means the source is saved but cannot run yet. Read the job until the
revision is `live`; a repository outage leaves it pending for maintenance to
retry. Legacy human commits are screened on import and stored as new revisions;
ADR-0272 requires new edits to use the private screened publishing path.
Existing run and schedule pins retain their original source.
An imported edit or deletion supersedes older queued publications. A delayed
receipt records the old commit without reactivating it or undoing that deletion.
Imported deletions retain an archived revision at the deletion commit, so the
normal restore flow can republish saved source without repository repair.
Publication batches select the current owner's requests before applying their
limit, so another owner's pending requests cannot block this job's publication.

Keep repositories, account identifiers and other personal values in private
inputs. Propose a `job.run` access object with those inputs, exact hosts and
credential bindings, feeds and limits. For example, a GitHub collector uses
`{"api.github.com":"github-reader"}` as `hosts` and `"0"` as `model_cost`.
The owner approves new external access or feed publication. The tool returns a
`grant_id`, which
subsequent runs reuse without repeating that approval. An expanded grant needs
approval; existing grants are never changed in place. Pure computation can
use empty hosts and a zero model budget. A mixed job needs a positive approved
model budget and charges each explicit completion to the normal run ledger.
Failed completions retain their billed usage and stop the job; catching the
error cannot reuse the same allowance. After interruption, a job tool whose
effects may have been sent is marked uncertain using existing tool recovery.
The platform does not automatically replay those effects. This also applies to
approved trials, whose HTTP calls can have effects even though feeds use previews.

A trial without a grant has no HTTP or model access. A trial naming an existing
grant uses its exact private inputs and access, with feed writes returned as
previews. It cannot change a live feed or schema. Failure feedback lets the
agent repair the program and try again. Trial output, source read back from
storage and job results remain untrusted content.

Use `schedule.create` version 1.7.0 with `task` containing `kind: job`, `slug`,
`revision_id` and `grant_id`. The existing scheduling approval shows the cadence
and approved access. The scheduler pins both identifiers and derives its run
limits from the grant and deployment ceilings. Feed jobs support the existing
interval cadence and stay quiet on success. Failures use the existing
notification and consecutive-failure pause behavior. `schedule.update` version
1.2.0 can repin future occurrences; earlier occurrences retain their pins.
Previous tool versions and ordinary agent-run schedules remain supported.
An interrupted update through version 1.2.0 is marked uncertain after its effect
boundary; read the schedule's current revision before proposing another update.

The schedule report contains the result's summary, job log and broker operation
names. The event log records operation and call identifiers; it does not record
HTTP bodies, URLs, model prompts or credentials.

## First useful workflows

The repository contains two generic packages:

- `samples/jobs/github-prs`: Query open pull requests and check/review status,
  paginate when necessary, sort the result and publish `agent.github-prs`.
  It makes no model calls. Repository names come from private inputs.
- `samples/jobs/review-summary`: Fetch structured review data, explicitly ask
  for one model completion, then turn the response into a short result with
  Python.

For the existing GitHub widget, have Veetbot inspect its declared feed schema
and adapt the collector's final mapping and feed identifier to that schema.
Do not replace the widget renderer or silently change its live schema. Run a
preview with approved access and verify the mapping. Choose a gap when the old
collector is idle and its next run cannot overlap the trial. Publish one live
snapshot, confirm the widget updates, then pause the old collector before
enabling the five-minute job schedule. If publication fails, retain the old
schedule; do not enable both collectors against the same feed.

After authorized delivery, observe a day of snapshots: age no more than 1.5
intervals, zero model cost for the collector and no unexpected broker refusals.
Also run a one-time job twice with the same grant and run the mixed example.
Verify its recorded model calls match only the calls requested by its code.
These live checks, credential setup and deployment remain separate from local
implementation verification.

To roll back functionality, set `AGENT_JOBS_ENABLED=0` and restart application
and run workers. Job schedules fail explicitly and pause at their failure
limit; they never fall back to model-driven execution. Existing agent-run
schedules keep their previous behavior. Keep the additive database schema and
saved source unless deliberately exporting and removing the jobs.

## Repair an existing widget refresh

A legacy agent-run schedule keeps its original agent version and scopes.
Putting `job.run` in its instruction cannot grant `jobs.run` or change its tool
roster. After publishing and previewing a collector, use `schedule.update` with
an explicit job task, expected schedule revision, published job revision and
owned approved grant. A feed-keeping agent schedule can switch in place; existing
occurrences keep their pins. The ordinary update approval still applies. Match
the cadence to the collector's expected interval. A count-only job without
approved feed publication access cannot replace the feed-keeping task. Verify one completed run
and the resulting live feed. Keep the prior revision for rollback.

`samples/jobs/github-widget` preserves the original PR widget's
`observedAt`, `expectedIntervalSec` and `repositories` schema. It reads compact
GitHub GraphQL JSON through the approved `github-reader` credential, including
current-head checks and decisive reviews, and publishes to
`agent.github-pull-requests`. The grant must permit that feed and the manifest
must declare it. Use private `repositories` and `refresh_seconds` inputs. A
provider error never publishes a false zero; the previous snapshot remains
stale. The query follows [GitHub's pull request schema](https://docs.github.com/en/graphql/reference/pulls).

After ADR-0271's server and Apple release, refreshes never add Chat rows. Their
isolated execution records and failures stay in schedule history. The migration
also marks earlier refreshes, and native reconciliation prunes their cached
conversation rows without deleting server history.

## Jobs alongside semantic recall

ADR-0272 permits an assessed jobs-enabled configuration without granting jobs
implicit memory retrieval. Semantic admission failure falls back to sparse
recall and does not stop jobs or approved schedules.

Only the publishing App may create or update `cockpit/jobs`; human edits must
use Veetbot's private authoring workflow. Keep the separate history protections
and verify that no human bypass exists. Supply private authoring values in
`job.manage.inputs`; widget installation supplies its approved job inputs.
Known private values are screened before queuing and again before public
publication. A queued package refused by the second screen becomes `rejected`
and needs corrected source in a new revision.

External grants now bind to the approved code revision. Choosing different
code, or reusing an older unbound grant interactively, needs one fresh execution
approval. Existing approved schedules keep their pinned code and grant. Live
trials cannot use publication permission to bypass execution approval. The HTTP
broker refuses supported encoded credential echoes with a content-free failure;
it does not claim to recognize arbitrary encodings.
