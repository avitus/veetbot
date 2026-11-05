# ADR-0298: A new-mail slice assesses only arrivals

- Status: Accepted (the owner chose this repair on 2026-11-04)
- Date: 2026-11-04
- Related: ADR-0096, ADR-0173, ADR-0286
- Amends: ADR-0256 decision 2, what a new-mail slice assesses and drafts
- Detailed design: `docs/plan/email-experience.md`

## Context

With Email open, a client refreshes about every two minutes: a 60-second wait after
each refresh of about 90 seconds. Production measured on 2026-11-04:

- 504 refreshes cost USD 122.64 in seven days, and only 37 of the 493 that completed
  made no model call.
- From 1:09 to 1:31 PM PST, 11 refreshes cost USD 4.11, about USD 11 an hour, while
  rolling spending was above the 75% share. Every completed one made four model calls.
- Of 6,227 Inbox conversations, about 15 counted as up to date by their stored fields,
  and about 3,000 of the rest had activity in the last thirty days. The email profile
  moved from revision 195 to 222 in eight days, and each move marks the whole Inbox
  out of date.
- In that sample, 14 conversations took about 44 calls. A long conversation takes one
  call per passage and holds its place at the top of the newest-first order until it
  finishes.

ADR-0256 limited a new-mail slice to Inbox mail, which is nearly all of this queue, so
its four places were always full. Above the share, the money reserved for new mail was
paying for the backlog, whether or not anything had arrived. ADR-0286 already defined
what a slice owes arriving mail, for server checks.

## Decision

1. **A new-mail slice follows the arrival rule.** It assesses at most four arrivals as
   ADR-0286 defines them, newest first, after any retained window a change is waiting
   on, one pass each.
2. **Drafts follow the same rule.** It prepares automatic drafts only for conversations
   it assessed, under the existing eligibility rules. A rejected draft is not retried
   by later new-mail slices.
3. **Its other work is unchanged.** It reads Gmail changes with all eight full-thread
   reads, the first page of an Inbox pass, expired Inbox bodies and unsubscribe evidence,
   as ADR-0256 says. None of that calls a model.
4. **A slice that found nothing is an empty look.** With no Gmail change, read, arrival
   or unsubscribe read, it makes no model call and requests no memory formation. After
   seven days its session, events and task are removed through ADR-0286's path.
5. **Full slices and every limit are unchanged.** So are the 75% share, the ceilings,
   the client's timer and server checks.

## Consequences

- With Email open above the share, a refresh with no new mail costs nothing. The cost
  follows arriving mail; the estimate is USD 1 to 2 an hour, not yet measured.
- Above the share, older mail keeps its rating after a profile change, and mail older
  than seven days that was never assessed stays unranked.
- Above the share, a long conversation is ranked on its newest passage only, and its
  automatic draft waits, because eligibility needs a finished analysis. This can delay
  a draft for an important long thread until a full slice runs.
- Below the share, full slices spend on the backlog as before, until spending returns
  to the share. This bounds spending at about the share rather than reducing it
  further. Holding it lower would need a separate decision.
- A replayed idempotency key whose task was removed after seven days is refused as
  erased. The client clears its key once a refresh is admitted, so only a request whose
  answer was lost and that is retried over a week later could meet this. That client
  would report a refresh failure until it is reopened.
- No milestone or gate moves.

## Alternatives considered

- **A slower client timer.** At five minutes the same refresh costs about USD 4 an
  hour, still spends with no new mail, delays new mail and needs an app release.
- **A cheaper model.** Model routing is not authorized, and a new model revision marks
  every assessment out of date.
- **Keeping long conversations' later passes in new-mail slices.** A 42-message thread
  would keep filling the four places, which is the cost this decision removes.
