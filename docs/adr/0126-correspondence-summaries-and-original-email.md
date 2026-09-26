# ADR-0126: Email correspondence carries a short summary, and Chat can read the original

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: ADR-0090, ADR-0096, ADR-0100, ADR-0101, ADR-0116, ADR-0117,
  ADR-0121, ADR-0123, ADR-0124, ADR-0125
- Amends: ADR-0121 (what a correspondence record holds);
  `docs/plan/people-and-relationships.md` (generated prose on interactions)
- Detailed design: `docs/plan/people-and-relationships.md`,
  `docs/plan/email-experience.md`

## Context

On 2026-09-25 the owner asked Chat what it knew about a portfolio company's
CEO. Chat said it could see a record of an outgoing email on 2026-08-26, with
him among the recipients, but not its substance. The run's tool results show
why:

- **The record holds only metadata.** ADR-0121's correspondence projection
  writes an interaction with a date, a direction, the recipients and the fixed
  summary "Sent email". `people.history` returned exactly that.
- **Chat cannot follow the record to the message.** `people.history` exposes
  only opaque People source ids. The People evidence route resolves such an id
  to an Email thread, but no Chat tool does. `email.context` needs that thread
  id, and it reads only the thirty-day body cache. At the time, the Gmail
  tools did not fit in Chat's thirty-item roster (ADR-0124). ADR-0123, accepted
  the same day, now defers them into reach.
- **Facts from the message would have expired anyway.** Email-derived facts
  expire thirty days after the message was sent (ADR-0090). For this message
  that was twelve minutes before the question. None existed in any state.

The owner asked for two things: a short, summarized memory of each email
exchange, and access to the original email when it is needed later.

## Decisions

1. **Each observed email exchange gets a short generated summary.**
   - The Email refresh generates summaries after it assesses mail, prepares
     drafts and verifies unsubscribe evidence, so a summary backlog never
     delays them. It handles at most four correspondence records per slice,
     newest first.
   - It stops when fewer than thirty seconds of the slice deadline remain. A
     failed call or a spent slice ends the summaries, never the refresh.
   - It summarizes the verified retained passage of the message, not the body
     cache, so mail whose cached body expired can still be summarized.
   - It uses the email model through the governed model call, within the
     slice's existing reservation and the approved automatic-email allowances.
   - It summarizes only:
     - mail inside the ninety-day boundary of ADR-0096;
     - mail that is not bulk (ADR-0116);
     - sources that are not excluded, suppressed or erased;
     - while email learning is not paused.
   - The model returns a summary of at most 240 characters and an exact
     supporting quote. The result is not stored when:
     - the quote is not found verbatim in the passage or the subject;
     - the summary is empty;
     - the summary trips the injection or secret-material checks that People
       display already applies.
   - An invalid result is retried once, in a later slice. A second invalid
     result abstains for good.
2. **The summary is dated history, stored on the interaction itself.**
   - The interaction's summary becomes `Sent email: …` or `Received email: …`.
     Every People surface already shows that field: Chat context,
     `people.history`, the People history route, and the native History row.
   - A `summary_provenance` field records:
     - the state: generated, retry, abstained or withheld;
     - the generator, `correspondence-summary@1`;
     - the model, the source id, the passage digest, and the time.
   - Like the interaction, a summary lasts until its source or the
     interaction is removed. It does not expire with the thirty-day
     communication facts.
3. **Removing the source or a related fact removes the summary.**
   - Source exclusion, source erasure, session deletion, principal erasure,
     and forgetting any participant already erase the interaction with all
     its revisions. The summary goes with it.
   - Deleting a fact formed from the same message (ADR-0117) withholds the
     summary, as that deletion already resets the Email thread summary:
     - every stored revision of the interaction is rewritten to the plain
       label;
     - a new revision records the withheld state, so the summary is never
       generated again.
4. **Chat reads the original email through People history.**
   `people.history@1.1.0` adds two things:
   - Each email item names its message: the account, the message id, the
     Gmail thread for the account's Gmail read tools and, while Email mode
     still caches the conversation, the thread id `email.context` accepts. Only
     an owner with `email.read` sees these identifiers.
   - `source_id`, one of an item's source ids, returns that message's original
     text. The result holds the sender, the recipients, the date, the subject,
     and at most 8,000 characters of the retained text, paged by `offset`.
     The text joins retained passages only where they are contiguous.
     - The text comes from the first-party read event Veetbot kept when it
       fetched the mail. It is verified against the stored passage digest
       before it is returned.
     - It requires `email.read`, as the People evidence route does.
     - It is refused for an excluded, suppressed, erased or bulk source.

   This is the "minimal authorized source view" of
   `docs/plan/people-and-relationships.md`, served to Chat. The retained event
   lasts until the owner deletes that operational session or the source
   (`docs/plan/email-experience.md`, "Privacy, retention, and controls").
   Several things are unchanged:
   - the thirty-day body cache;
   - `email.context`;
   - the Email thread view;
   - the People HTTP evidence route.

   Version 1.0.0 of `people.history` stays registered for chats pinned to it.
5. **Several things do not change.**
   - No new tool; Chat's roster count is unchanged.
   - No policy file, context builder, or schema migration.
   - The assessment revision stays `email-assessment@4`, so ninety days of
     mail are not reassessed.

## Consequences

- Chat can say what an exchange was about, and can quote the original when
  asked. Owner questions such as "what did I tell Alex last month" no longer
  end at a date and a list of recipients.
- Summaries appear as Email mode refreshes, like correspondence itself. The
  existing backlog fills four records per refresh, newest first.
- Each summary is one small model call on one passage. It is billed to the
  approved Email allowance and shown in its spend.
- Summary quality has structural checks but no private evaluation yet.
  `docs/plan/people-and-relationships.md` asks that generated prose carry its
  own evaluation. The owner accepted activation ahead of it, and
  `docs/status/milestones.md` records the evaluation as an open Milestone 28
  item.
- Chat can read an old email's text for as long as the owner keeps the
  operational session that fetched it. Deleting that session or excluding the
  source ends the access.

## Alternatives considered

- **A per-message summary inside the Email assessment.** This is one call
  instead of two, but it changes the assessment schema. That means
  `email-assessment@5` and reassessing all ninety days of mail.
- **Reuse the thread assessment summary.** It describes the thread's latest
  state rather than one message. It is also missing for threads that were
  never assessed.
- **Store summaries as memories.** More than a thousand dated summaries would
  crowd ordinary recall and the review queue. History is shown only when a
  person is in focus.
- **Refetch from Gmail on demand as the only route.** A governed refetch
  operation would need a new task kind and asynchronous waiting inside a Chat
  tool. ADR-0123 lets Chat call the Gmail read tools itself, and each history
  item now names the Gmail thread for them. A live read still costs a provider
  call and a model step, and it fails once the owner deletes the message in
  Gmail. The retained copy answers at once and is the view the People evidence
  contract describes, so both routes remain.
- **Let `email.context` read the retained copy.** That changes the Email
  cache contract that the Milestone 26 privacy gates rely on.
- **A separate People record kind for summaries.** It needs a migration and
  its own forget handling. The interaction's erasure closure already covers
  every other removal path.
