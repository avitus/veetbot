# ADR-0344: One approval for a request that changes a saved job

- Status: Accepted (the owner chose one approval per request on 2026-11-22);
  production acceptance follows the check in Validation
- Date: 2026-11-22
- Amends: ADR-0268 (a revision-bound grant may come from a request approval),
  ADR-0271 (repinning a job schedule), ADR-0302 (`approve_for_request` and
  `request_grant` also serve job approvals)
- Related: ADR-0058, ADR-0129, ADR-0270, ADR-0291
- Scope: job and job-schedule authorization, one offer field on an approval,
  one echo field in `request_grant`, one event type, and the Apple approval
  card. No table, migration, milestone, gate, compiled policy or
  `policy_version` change.
- Detailed design: `docs/plan/cockpit.md`, `docs/plan/policy-and-approvals.md`,
  `docs/plan/http-api-and-streaming.md`

## Context

On 2026-11-22 the owner asked the "Fix Emerging Market Radar" chat to repair
the radar after its overnight collector run failed. That one request raised
five approval cards, every one for the same code change to the same job:

1. a live trial of the new collector against its existing grant;
2. the same trial again, after the first failed on an undeclared call;
3. saving the corrected code as a new revision;
4. running the new revision with its access;
5. repinning the daily schedule to the new revision.

Each card said "This chat has read web pages or other outside content". A
repair has to read the failing widget, its run history and its data, so every
repair turn carries untrusted content and the trust overlay escalates every
write. Exact grants (ADR-0268) then cover only the revision they were approved
for, so new code needs a card at each step. The website request approval
(ADR-0302) and the task grant (ADR-0129) are offered only on `browser.act`
cards, so none of the five offered a way to approve the request once.

The owner's decision, 2026-11-22: one approval covers all collector and
schedule changes for one job until the owner's next message. Access to new
outside services, new data feeds or larger limits still asks.

This loosens the trust overlay for these tools inside one request. The owner
approved that in the conversation that produced this record.

## Decision

1. **A request approval for a job.** A card for one of the actions decision 2
   covers, in an eligible run, offers "Allow for this request" beside Approve
   once and Deny. Accepting it approves the pending action and, in the same
   transaction, records `job.request_grant.created` with the approval id, the
   job slug and the end of its window.
   - An eligible run is a top-level interactive run of the owner's own chat
     (not scheduled, inbound, device, refresh or job occurrence), seeded by the
     owner's message, in a turn whose newest message is the owner's.
   - The offer is stored on the approval as `job_request_grant_offer`, beside
     `request_grant_offer`. It names the slug, the access ceilings (decision 3),
     `duration_seconds` (7200) and a server-authored summary. A client that
     predates it shows Approve once and Deny and nothing else changes.
   - The resolution is the existing `approve_for_request`. Its `request_grant`
     body repeats the offered slug as `job_slug` instead of a profile id. It
     requires `approval.resolve` and `jobs.run`.
   - The offer is made only when the pending action is itself covered.

2. **What it covers**, for the offered slug only:
   - `job.manage` `trial` that names a grant, and `update`, whose manifest
     declares no feed outside a ceiling;
   - `job.run` of any published revision of the job, with proposed access or a
     saved grant whose access fits a ceiling;
   - `schedule.update` that repins a schedule already running this job to a
     published revision and a grant whose access fits a ceiling, optionally
     with a new title or instruction.

   Still asking every time: creating, archiving or restoring a job, any other
   job, creating a schedule, changing a cadence or an occurrence time,
   replacing a schedule's MCP servers, and every other tool.

3. **Access ceilings.** The ceilings are fixed when the card is made: the
   access the card's own action uses, if any, and the access of each grant the
   job already holds, at most 16. Each grant was approved by the owner or
   needed no approval. Access fits a ceiling when one ceiling alone holds it:
   - every host with the same credential reference, every MCP server and every
     feed are in that ceiling;
   - its timeout, request count, response size and model cost are no larger.

   A run's private inputs may change; they reach only the hosts and servers
   the ceiling names. A trial with a grant still uses that grant's inputs
   unchanged (ADR-0270). Ceilings are never merged, so a request cannot
   combine one grant's hosts with another's limits.

4. **Revision-bound grants.** A `job.run` that the request approval authorizes
   binds its new grant to that approval as ADR-0268 binds one to a run's own
   approval: id `uuid5(approval_id, "job-revision:<revision_id>")`. A second
   run of the same revision with different access in the same request would
   reuse that id, so the authorizer does not cover it and it asks.

5. **Lifetime.** As in ADR-0302 decision 4: the grant belongs to the run that
   answers the owner's message. It ends when that run ends, after 2 hours, or
   when the owner sends a follow-up that the run reads. It never transfers to
   another run or session. The agent version and policy version must match the
   approval's.

6. **The card says what the owner allows.** The summary reads: "Allow for this
   request: change, test and run the <slug> job and repin its schedule using
   access it already has. Veetbot still asks before new websites, MCP servers,
   feeds or larger limits, and before schedule timing changes." Once allowed,
   the card reads "Allowed for this request". Each use is audited by
   `tool.call.authorized` with `authorization_kind: "job_request_grant"` and the
   approval id.

## Consequences

- The radar repair asks once after the owner's message instead of five times.
- Inside one request, code written after reading untrusted content can run
  against hosts, servers and feeds the owner already approved for that job
  without a card showing the code. That is the loosening the owner accepted.
  Data reaches only those destinations, and new access still asks.
- Cadence changes and new schedules still ask, so a request cannot add runs.
- Clients older than the update show Approve once and Deny only.

## Validation

Red tests first: the offer on `job.manage` trial and update, `job.run` and
`schedule.update` cards in an eligible run, and its absence for a scheduled or
child run, another job, new access, a cadence change and archive; resolution
appends the event, checks the slug and scope, and refuses a stale offer;
coverage of later trials, saves, runs and repins with fitting access, and
refusal for a wider host set, a new feed, a new MCP server, a larger limit,
another slug, after expiry, after a follow-up and in another run; and the
revision-bound grant created from the request approval.

Production acceptance: ask a chat to repair a widget's collector. Accept when
the request raises at most one card and a job run needing a new host still asks.
