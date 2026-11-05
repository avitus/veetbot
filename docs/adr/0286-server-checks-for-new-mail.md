# ADR-0286: The server checks for new mail while Email is closed

- Status: Accepted — the owner accepted this decision, including both spending
  limits, and authorized its implementation on 2026-11-01. Amended 2026-11-03: see
  the amendment below. ADR-0298 applies its arrival rule to new-mail slices, so the
  work it leaves to foreground slices now waits for a full slice within the share.
- Date: 2026-11-01
- Related: ADR-0092, ADR-0096, ADR-0103, ADR-0104, ADR-0173, ADR-0244, ADR-0256
- Amends: ADR-0092 decision 3 (refresh is foreground-request-driven)
  and the exclusion of background monitoring in the engineering plan, project
  state and `docs/plan/email-experience.md`
- Detailed design: `docs/plan/email-experience.md`

## Context

Email refresh runs only while a client has Email open. ADR-0092 rejected
monitoring because it was "unnecessary for the stated active-mode requirement",
not because of a measured cost. The owner now reports the result as a poor
experience: checking starts only at the moment they want to read new mail.

Production measurements taken on 2026-11-01 show how large the gap is and why
the existing refresh cannot simply be run on a timer.

**Mail waits for hours.** Over the last seven days the time from a
conversation's latest activity to Veetbot first storing it had a median of about
5 hours on the personal account and 18 hours on the work account. The slowest
10% waited 33 hours or more and 52 hours or more. The median wait to a stored
assessment was about 10 hours. About 75 to 165 conversations a day show new
activity across both accounts.

**A refresh is never idle.** The specification says a poll with unchanged sources
does no new model work, but that case almost never occurs. In the last seven days
only 2 of 361 completed refreshes made no model call. Refreshes averaged USD 0.275
and 3.5 model calls. Of about 4,100 Inbox conversations active in the last ninety
days, about 1,100 still need model work and about 3,000 are out of date only
because the owner's learned email profile changed. That profile moved from
revision 182 to 204 between 2026-10-18 and 2026-10-31, and each change marks the
whole Inbox out of date. A refresh assesses at most four conversations, so this
queue does not empty. Rescoring can reuse earlier features without a model call
when a conversation's assessment context is unchanged, yet it still takes one of
the four places. Stored events do not show how much of today's spending that
re-assessment accounts for, because a model call does not record its conversation.

**A plain timer would exhaust the budget.** At the measured average, a refresh
every five minutes costs about USD 79 a day. That reaches the USD 40 daily ceiling
in about 12 hours and the USD 600 rolling ceiling in about 15 days. Counted spending
over the last thirty days is USD 436.65, close to the USD 450 point at which
ADR-0256 holds back learning.

**Failures already cost money.** Of 980 refreshes in twenty-one days, 19 failed
and 18 hit the 120-second deadline, or 3.8%. Twelve were charged a conservative
USD 1 under ADR-0173 because their usage could not be proven. At 288 checks a day
that rate would mean about 11 failures and about USD 3.50 in such charges daily. The
median refresh takes 75 seconds and the slowest 10% over 111 seconds.

**Each refresh leaves a permanent trace.** A refresh creates its own operational
session, and 2,518 of them have accumulated since 2026-09-11. A busy refresh writes
about 175 events, against about 31,000 events a day across the whole system today.
Every refresh also reads all 9,072 stored conversations and 6,173 assessments.

## Decision

1. **The server admits an arrival check every five minutes.** The maintenance
   worker submits a typed refresh through the existing admission path, for the
   same owner principal its email cache expiry uses. A check is due when no refresh
   of any scope was created within the current interval, read from the durable task
   records, so a restart causes no burst. It uses no schedule row, no agent prompt
   and no device presence. The interval is the reviewable default
   `email.check_interval_seconds: 300` in `src/agent_core/runtime/limits.yaml`.
2. **Foreground refresh is unchanged.** Clients still refresh on entry, on return
   and every sixty seconds while Email is visible, and stop when it is hidden. Their
   requests keep making a check unnecessary, because a recent refresh of any scope
   defers the next check. A client request made while a check is running joins it
   through existing coalescing; the next request admits an ordinary slice.
3. **A check handles only arrivals.** Each refresh task's `scope` gains a third
   value, `arrivals`, alongside `full` and `new_mail`. An arrival is an Inbox
   conversation with activity in the last seven days whose current source has not had
   a pass: it was never assessed, its source changed, or its one promised retry is
   pending. A conversation still being read is not an arrival until the read ends. A
   check:
   - reads Gmail changes with the existing eight full-thread reads per account,
     most recently changed first;
   - lists and reads only the first page of an Inbox pass after a resynchronization,
     the ADR-0256 rule that recovers new mail;
   - assesses at most four arrivals, newest first, after any retained window that a
     change is waiting on, with the junk first pass as usual. Each gets one pass, on
     its newest passage, which is enough to rank it. Further passes of a long
     conversation wait for foreground slices;
   - prepares automatic drafts only for arrivals, under the existing eligibility
     rules and limit of three. Those rules need a finished analysis, so a long
     conversation gets its draft once foreground slices finish it. A check sends
     nothing and writes nothing to Gmail.

   A check does not re-assess a conversation only because the profile, model or
   prompt revision changed. It does not page further through the Inbox, advance the
   ninety-day history pass, re-read expired bodies, verify unsubscribe evidence or
   write correspondence summaries. That work stays with foreground slices.
4. **Checks have their own spending limits.** A check is admitted only while counted
   spending, live reservations and its USD 1 reservation fit within 50% of the daily
   ceiling and 90% of the rolling thirty-day ceiling: USD 20 and USD 540 at today's
   ceilings. The limits are fixed fractions, as in ADR-0256, so raising or lowering a
   ceiling moves them too. Half of the rolling ceiling would be USD 300, below what
   has already been spent, and full slices stop at 75%, so checks need room above
   that share. The remaining 10% stays available while the owner is in Email. The
   ceilings, the 75% learning share and foreground admission do not change.
5. **A refused check is silent.** It writes no owner-visible pause, because the
   client's pause and its retry time describe foreground work. It logs one content-free
   line and waits for the next interval.
6. **Failures widen the interval.** Each consecutive check that fails, hits its
   deadline or takes a conservative charge doubles the wait: 10, 20, 40, then at most
   60 minutes. Any completed refresh resets it. The count comes from durable task and
   run records. The daily limit in decision 4 still bounds what failures can charge.
7. **A check with nothing to do stays cheap.** When Gmail reports no change and no
   arrival is waiting, the check ends without loading stored message content,
   makes no model call and requests no memory formation. After seven days the session,
   events and task record of such a check are removed through the existing session
   erasure path, so they neither build up nor slow budget admission. Erasure's small
   tombstone row remains.
8. **A check is never an alert.** It sends no push, digest or other notification,
   whether it succeeds or fails. A failure reaches the owner through the existing
   Email account status, which a later success clears.
9. **It ships off, behind a flag.** `AGENT_EMAIL_BACKGROUND_CHECKS_ENABLED` defaults
   off, and the owner turns it on for the first production week. It stays on if that
   week shows:
   - a median wait from mail activity to stored conversation of 10 minutes or less;
   - check spending within the daily limit on every UTC day, with its total reported;
   - no-change checks making no model calls, with their duration, event count and
     database growth reported;
   - no material rise in queue time for scheduled runs, which share the asynchronous
     lane.

## Authority and safety

A check uses only the read servers ADR-0104 already prepares for a refresh. Mail
remains untrusted evidence, and the assessor and draft generator gain no authority
from it. Gmail writes and sending still need the owner's act.
`src/agent_core/policy/default.yaml` and `src/agent_core/policy/hardline.yaml` do
not change, so memory-formation evidence bound to the policy version stays valid.
Mail that a check assesses forms memories under the
existing bulk gate and source rules; what changes is that this can now happen with
no client open, so the owner may see email-derived memories before opening Email.

The foreground admission gate still holds: hidden or backgrounded clients admit no
new slices. A new Milestone 26 gate, `gate.email.experience_background_arrival_check`, covers
this decision. Its tests must show that a check:

- admits only when due, and defers to a recent refresh of any scope;
- selects arrivals only, and leaves profile-stale conversations alone;
- stops at both limits without writing a client pause;
- widens its interval after failures and resets after a success;
- makes no model call and requests no formation when nothing changed;
- sends no notification on success or failure.

## Consequences

Mail should be stored and ranked within minutes, and drafts for important mail
should be ready when the owner opens Email. Fewer long absences should reach the
ten-page limit that forces a resynchronization and restarts the Inbox pass.

Keeping up with new mail costs at least an estimated USD 6 to 12 a day, at about
USD 0.074 per model call and one call per conversation; long threads take more.
Most of this spending would otherwise happen when the owner next opens Email, so
the added cost is mainly mail the owner would have deleted or archived unread.
Because checks count toward the shared allowance, foreground learning may reach
ADR-0256's 75% share sooner and history catch-up may slow. That follows ADR-0256's
priority of new mail over learning.

Up to 288 checks a day run in the asynchronous lane, each starting a read server
per account. Duration, process cost and event growth for a check that finds nothing
are unmeasured, which is why decision 9 reports them before the flag stays on.

This does not clear the existing queue of out-of-date Inbox conversations or the
unfinished work-account Inbox pass. "Last synced" still updates only when an account
is fully caught up, so it can stay old while new mail flows. The specification also
says existing mail is reranked immediately after learning changes, while the code
processes at most four conversations per slice; that needs its own investigation.

## Documents changed by this decision

- `docs/plan/email-experience.md`: "Active use, not a mailbox monitor", the budget
  section, the milestone exclusions and the hard gates.
- `docs/plan/engineering-plan.md`: narrow the background monitoring exclusion.
- `docs/status/project-state.yaml`: narrow the deferred scope and authorize the work.
- `evals/gates/email.yaml` and `src/agent_core/runtime/limits.yaml`.

Gmail push, digests, alerts and scheduled-run interval scheduling stay excluded.

## Amendment — 2026-11-03

The first evening in production showed that counting unfinished analysis as an
arrival was wrong. Analysis moves one passage per pass, and a check takes the newest
conversations first, so a long thread came back every five minutes. On 2026-11-02,
27 checks between 5:09 PM and 7:26 PM PST cost USD 8.25 and none was empty. Four
conversations of 20 to 43 messages took about 90 of the 109 model calls. With USD
10.81 of foreground spending on the same UTC day, the daily limit then refused checks
until the next UTC day, which begins at 4 PM PST. Overnight mail would have waited
for the owner again.

The owner approved the fix on 2026-11-03. A conversation now gets one pass per source
from a check. A first pass already saves the conversation's ranking, so new mail
still ranks within minutes, and foreground slices finish the rest. A long conversation
gets its automatic draft only after they do. The exception is a full retained window
that a Gmail change is waiting on. It keeps its passes, because the mail behind it
cannot be read until the window is analyzed.

Review of that fix found the same fault one step earlier. A conversation of more than
one page takes several checks to read, each read changes the stored source without any
new mail, and a pass after each read would start again from the newest passage read so
far. A conversation now waits until its read ends, and then gets its one pass, when the
newest message is there to rank. The cost is delay: about one page of ten messages per
check, so a conversation of 43 messages ranks after about five checks, or 25 minutes.
Most conversations fit one page and are unaffected. A new conversation shows as pending
until then, and one already ranked keeps its ranking. A label change on such a
conversation re-reads it and still costs one pass at the end, because the source
comparison cannot match a partial read to the full one it replaces.

The same evening showed a refused check retrying at every 60-second sweep, 63 log
lines by 8:56 PM, against decision 5. It now waits one interval. A refusal writes no
task, so that wait is held in memory, and a restart costs one more attempt.

Estimates, not yet measured: the conversations touched in the last seven days held at
most 388 that were never assessed or had changed, which is about USD 29 once at USD
0.074 a pass, and less because that count includes mail no longer in the Inbox. The
estimate of USD 6 to 12 a day to keep up then applies. Decision 9's first week is
measured from this fix reaching production.

## Alternatives considered

- **Run the existing refresh on a timer:** rejected on the measured cost above. Its
  scope also keeps spending on the older queue and re-reads expired bodies, which
  would keep renewing their retention with nobody using Email.
- **Run the `new_mail` scope on a timer:** rejected for the same reason. It averages
  USD 0.26 because it assesses any out-of-date Inbox conversation, newest first.
- **Gmail push through Pub/Sub:** deferred. It needs a public webhook and a Google
  Cloud subscription and still needs the same arrival scope and limits. Its lower
  latency matters only if the owner later wants instant alerts.
- **iOS background refresh:** rejected. The system decides when it runs, it does not
  cover the Mac, and it keeps the work tied to a client.
- **Fix the out-of-date queue first and keep client-only refresh:** rejected as the
  answer to this problem. It could lower spending, but mail would still wait until
  the owner opens Email.
- **Limit rolling spending to 50%:** rejected. Checks would be refused from the first
  day, since counted spending is already above USD 300.
- **Raise the daily limit instead of the 2026-11-03 fix:** rejected. It would spend more
  on the same long threads without reaching new mail any sooner.
