# ADR-0256: New mail keeps a reserved share of the Email budget

- Status: Accepted (the owner asked for it on 2026-10-26)
- Date: 2026-10-26
- Related: ADR-0092, ADR-0096, ADR-0173
- Amends: ADR-0096's budget refusal, which paused every refresh at the ceiling
- Detailed design: `docs/plan/email-experience.md`

## Context

Every Email refresh slice reserves USD 1 against one aggregate allowance, a
daily and a rolling thirty-day ceiling. A slice reads new Gmail changes first,
then spends the rest of its reads and model calls on inbox catch-up, historical
learning and correspondence summaries.

On 2026-10-26 the owner reported receiving no important email. Production had
counted USD 399.25 of its USD 400 rolling ceiling. Every refresh since
2026-10-25 23:17 UTC had been refused at admission, so no new mail was read or
ranked for more than a day. The Work account's ninety-day catch-up was still
unfinished, and its slices kept spending from the same allowance that ranks new
mail. A refusal stops everything, including new-mail work that is cheap or free
when nothing has arrived.

## Decision

1. **Background learning stops at three quarters of each ceiling.** A refresh
   is admitted as a full slice only while counted spending, live reservations
   and the next reservation fit within 75% of both the daily and the rolling
   thirty-day allowance.
2. **New mail keeps the remaining quarter.** Above that share and within the
   full ceilings, a refresh is admitted as a new-mail slice. That slice reads
   new Gmail changes with all eight full-thread reads. It assesses only current
   Inbox conversations, the newest activity first. It may still re-read expired
   Inbox bodies, prepare eligible automatic drafts and verify unsubscribe
   evidence. It still lists and reads the first page of an Inbox pass, which
   holds the newest mail, so a resynchronization after an expired history
   cursor recovers new mail. It lists no later catch-up page, does not advance
   the ninety-day history pass, assesses no non-Inbox mail and writes no
   correspondence summaries. Importing a thread still records the owner's own
   Sent replies in it as learning evidence: that step makes no model call.
3. **The full ceilings are unchanged.** At either ceiling every refresh is still
   refused with ADR-0096's accounting and next check time. Owner-requested
   drafts and People imports remain bounded by the full ceilings, not the share.
4. **The admitted scope is durable.** Each refresh task records `scope` as
   `full` or `new_mail`. Tasks written before this decision read as `full`.

## Consequences

When historical learning nears its share, new mail is still read and ranked,
and catch-up resumes on its own once spending falls back under the share. New
mail can use the whole allowance, because its spending counts toward the share
as well. The share is a fixed fraction of the operator's ceilings, so raising
or lowering a ceiling moves both parts. Email mode does not yet say that
historical learning is held; the account still reports catch-up as unfinished.
No milestone or gate moves.
