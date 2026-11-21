# ADR-0340: Email memories are kept by the thread's importance, and mail from people the owner knows forms first-class memories

- Status: Accepted (the owner set the direction and confirmed the tiers and the
  sender rule on 2026-11-20, and the same day chose the readings in decisions
  2, 3, 6 and 7 when asked)
- Date: 2026-11-20
- Amends: ADR-0090 (decision 2, for the `email-semantic@2` policy only),
  ADR-0121 (decision 2, the thirty-day expiry of a stranger's fact), ADR-0247
  (one lifecycle class added), Section 6 of `docs/plan/email-experience.md`
- Related: ADR-0101, ADR-0116, ADR-0117, ADR-0165, ADR-0249, ADR-0290, ADR-0338,
  ADR-0339
- Detailed design: `docs/plan/email-experience.md`,
  `docs/plan/memory-evaluation-and-lifecycle.md`

## Context

On 2026-11-20 production held 661 live memories formed from email, 49% of the
1,361 live memories. They had been cited twice in total. About 800 more had
already expired unused, and over the previous ten days about one recall slot in
six went to an email memory that was never cited.

The construction explains it. Every email memory expired thirty days after the
email's date, whatever the email was. Its confidence was capped at 0.4. It was
stored as a tentative, provisional hypothesis, flagged for review, and ranked
under the attention penalty and the one-third cap of ADR-0249. Its statement
was a record line that carried the mailbox, thread and message identifiers.

The owner's direction, in their words: "the more important the email is, the
longer the memory should be retained. I still want email to form first class
memories." The owner then confirmed the tiers and the sender rule below, and
chose the sender rule over first-class standing for every sender because
anyone can email them a "fact".

## Decision

Everything here applies to the production policy, `email-semantic@2`
(`EmailPeopleFormationService`). The older `email-semantic@1` policy keeps its
evaluated construction unchanged.

### 1. Three inputs, all read from stored state

- **The thread's importance** is the 0 to 1 rating the email assessment saves
  on the thread, the same number that orders the Inbox, after the owner's
  surviving importance feedback: Important sets 1.0 and Less important sets
  0.0, for a thread, a person or a topic. The dated-attention rule that takes a
  finished event out of the Inbox does not lower it. A thread with no stored
  rating counts as 0.
- **The owner replied** when the thread holds a message the owner sent.
- **A known sender** is one of:
  - the owner, on a message the owner sent;
  - on a received message whose sender check passed (ADR-0165), an address
    that resolves to exactly one active or provisional person in People,
    through an owner-confirmed, channel-observed or owner-stated identifier.
    Under ADR-0121 that is someone the owner created, confirmed or named, or
    someone the owner has written to.

  Anyone can write a familiar address in a From line, so the address alone is
  not enough. Gmail's passed check is what ties a received message to it, as
  ADR-0336 requires before it reads a signature. A message whose check failed,
  or gave no verdict, never has a known sender. An address that is unresolved,
  ambiguous, or one of the owner's own on received mail is unknown.

A stored thread is not always a full, current reading, and the first read of
production on 2026-11-20 showed how much that matters. The 667 live email
memories sat in 239 threads. Eighteen of those threads, holding 316 of the
memories, had a rating of zero that was not a rating. Three, holding 257,
stored only part of their messages, and 262 memories came from a message
outside the part held. One long thread accounted for most of each figure.

- **A reset rating is not a rating.** A new message resets the thread's stored
  rating to zero until the thread is assessed again. A zero with no assessment
  version is read as unknown, and the thread's last saved assessment answers
  instead. The assessment record now keeps the rating it produced. A record
  saved before this change holds only the model's features, so the content
  share of the same formula stands in as a floor. With no assessment and no
  owner feedback, the importance is unknown.
- **A floor and an abstention are not ratings either.** The content share of
  an older record leaves out the relationship share, so the true rating may
  have been higher. A rejected or ungrounded result, and a junk first pass,
  save zero features and with them a zero rating that reads no part of the
  thread; the assessment record says it was not grounded. Each of these may
  lengthen a memory and never shortens or demotes one. A known rating is one a
  grounded assessment saved, or the owner's own feedback.
- **A long thread stores a window of its messages.** The owner's reply, or the
  message a memory came from, may be outside it. The thread index lists every
  message ever observed, and re-rating reaches a thread's memories through
  that list.

### 2. Retention follows importance

The expiry is measured from the memory's last evidence date: the date of the
email that formed it, or of a later email that renewed it.

| Thread | Kept for |
| --- | --- |
| importance below 0.3 | 30 days |
| 0.3 up to 0.6 | 90 days |
| 0.6 up to 0.85 | 1 year |
| 0.85 and above, or the owner marked it Important, or the owner replied | no expiry |

The table applies to every sender. A guess from an unknown sender in an
important thread is kept longer and stays a guess. The confirmed proposal also
said mail from unknown senders "stays as today's short-lived guesses"; asked
which was meant, the owner chose retention by importance for every sender on
2026-11-20, so important mail from a sender the owner never writes to, such as
a bank or a tax office, is kept by its rating.

A memory under this table carries the lifecycle label `email-retention@1` and
is outside the automatic decay sweep, as kinship beliefs are
(`people-lifecycle@1`). Decay would otherwise retire an uncited email memory
about five weeks after its email, whatever the table said.

### 3. First-class standing

A memory is first-class when the sender is known **and** the thread is kept for
a year or more (importance 0.6 or above, or marked Important, or the owner
replied). Both conditions are required. Every other email memory keeps today's
construction. The confirmed proposal named known senders and important threads
without saying whether either alone was enough; asked on 2026-11-20, the owner
chose both, so a known sender's unimportant mail stays a guess.

| Field | First-class | Otherwise (unchanged) |
| --- | --- | --- |
| Statement | `<sender> said by email on <date> that <subject> <predicate>: "<value>".` | `Email from <sender> on <date> (mailbox, thread, message) reports <subject> <predicate>: "<value>".` |
| Confidence | the extraction's, up to 0.65, the ceiling every automatically captured chat memory has | the extraction's, capped at 0.4 |
| Derivation | direct | hypothesis |
| Longevity | durable | tentative |
| Review flag | not set for coming from email | set |
| Sensitivity | sensitive | sensitive |
| Status | provisional, as a new chat memory is | provisional |
| Authority | inferred | inferred |
| Portability | contextual | contextual |

The mailbox, thread and message identifiers of a first-class memory stay where
inspection already reads them: the retained source record that lists the
memory, the People source row, and the memory's source session and event.

In recall a first-class memory takes no attention penalty, is outside the
one-third cap, and is not held below owner beliefs by rule. Its score is
computed as for an automatically captured chat memory of the same confidence.
No ranking code changes, so the `retrieval@7` label stays.

### 4. A memory follows its thread, and a repeat renews it

- **Re-rating.** A memory's retention and standing are recomputed from the
  current stored state: after every saved assessment of its thread, whichever
  path saved it, and at once when the owner gives or undoes feedback on that
  thread. The feedback commits first. A retry of the same request, or a
  repeated undo, writes nothing and re-rates again, so a re-rating that did not
  run is made up by the retry or by the thread's next assessment. A reply
  reaches the memory when the sent message is synced.
- **Person and topic feedback, and the other threads it matches.** Importance
  feedback on a person or a topic rates every thread it matches, not only the
  one it was given on. That thread is re-rated in full. When the feedback is
  saved or undone, the other matching threads are visited at once in the
  lengthening direction only: a memory there gains a longer retention, or
  first-class standing, and nothing is shortened or demoted. Lengthening
  needs only what is visible, so it is safe at once. What the feedback takes
  away in those threads, by Less important or by an Important since undone,
  waits for each thread's next assessment or for the owner's command, which
  read the thread whole. Until then a memory raised by a mark the owner has
  undone keeps what it gained. A single Less important on a person therefore
  cannot expire memories across that person's threads in one request.

  The pass runs inside the request, after the feedback commits. It takes up
  to 250 other threads, newest first, fifty to a transaction, each under its
  own locks with feedback, threads and senders read again, and it starts no
  new thread after three quarters of a second. A thread it does not reach
  waits for its next assessment or the command. A pass that fails never fails
  the feedback, and the retry that resumes the thread's own re-rating runs the
  pass again.
- **Raising needs what is visible; lowering needs proof.** A re-rating
  lengthens a memory's retention, or promotes it, on whatever the stored
  thread shows. It shortens retention only on a reading whose importance is
  known and whose thread is held whole, so that "no owner reply" is a fact and
  not a gap. It changes standing only when the memory's message is among those
  held, and it demotes only when that sender is no longer known or such a
  reading rates the thread lower. A reset rating, a floor, an abstention, a
  partial thread, or a message that left the window lowers nothing. Neither
  does a sender check that merely reads no verdict: the stored message may
  have been cached before the check existed, so it earns no standing and takes
  none from a memory that is already first-class. A failed check demotes.
- **Older memories wait for the command.** The two hooks, and the pass over
  other threads, follow only a memory
  already under the table, one that carries `email-retention@1`. A memory
  formed before this change is brought under it only by the owner's command
  in decision 8, or by a later email that renews it.
- **What re-rating touches.** Only a live memory that is still inferred, still
  under `email-semantic@2`, still worded as one of the two email statements,
  and not yet past its expiry. A memory the owner confirmed, corrected or
  replaced is never touched. Neither is one the owner's own chat statement took
  over in place: that row keeps the email policy label but holds the owner's
  words, and it is the owner's memory from then on. An expired memory is never
  revived; "Still true" restores one.
- **What re-rating changes.** The expiry always. When the standing changes, the
  statement is reworded in place, and the derivation, longevity and review flag
  follow. A promoted guess keeps the confidence it was stored with, because the
  extraction's own figure was not kept; a returned guess is capped at 0.4
  again. A memory with a conflict link keeps its flag. A reworded memory takes
  a fresh store position, so the next turn sees the change; an expiry change
  alone does not. Each applied re-rating that changes something appends one
  content-free `email.memory.rerated` event naming the memories.
- **A later email.** When a later message states the same subject, predicate
  and value, the existing memory is renewed instead of a second one forming:
  its evidence date moves to the later email's date, its evidence count grows,
  and its expiry is recomputed and never shortened. The later message must be
  a different message with a strictly later date. Older or same-dated mail
  that is processed afterwards finds the memory and changes nothing.
- **What a repeat settles.** A first-class memory becomes active when the
  later message is from a known sender too, as a repeat makes a chat memory
  active. A guess stays provisional however often it is repeated, and a
  stranger's repeat of a first-class memory moves its dates and settles
  nothing. Nobody can write a claim into the active set by sending it twice.
- **Which message governs a renewed memory.** The message that formed it sets
  its standing and wording. The message that supplied its last evidence sets
  its retention. For a memory nothing renewed these are the same message. The
  retained source record says, for each memory it lists, whether its message
  formed it, renewed it or only found it. A message that only found a memory
  governs nothing, even when it carries the same timestamp as the one that
  formed it. Dates order the evidence; they identify a message only on a
  record written before this was kept.
- **A later chat.** The owner's own statement about the same subject takes the
  memory over under the ordinary formation rules, which this change leaves as
  they are: it promotes a guess in place with the owner's words, and it
  supersedes a first-class memory with the owner's.
- **Confirmation.** "Still true" (ADR-0247) replaces any live email memory with
  the same words at owner authority and no expiry.

Re-reading a message, importing older mail, and citing a memory in a reply
renew nothing. A re-read is the same message. A historical import keeps the
thirty-day horizon from the email's own date whatever its thread is rated, so
an import never makes an old claim current, and a later re-rating does not
revive what it formed. A citation never touches evidence (ADR-0247).

### 5. What does not change

- **A correspondent's claim never overrides the owner.** Every email memory
  keeps inferred authority. A claim from email that contradicts a belief formed
  from anything else is stored beside it and both are flagged for the owner; it
  never supersedes that belief. Before this change an email claim could
  supersede an automatically captured owner statement of the same subject; a
  test now reproduces that and holds the repair. A later email may supersede
  only a belief that is still a correspondent's attributed claim. A claim from
  email also never takes over a guess formed elsewhere by promotion: only the
  owner's own statement promotes a guess. A claim that repeats a stored guess
  word for word counts as evidence for it and leaves it a tentative guess.
  These rules sit in the shared formation code, so they hold for
  `email-semantic@1` as well.
- **Bulk mail forms nothing** (ADR-0116), and suspected spam forms nothing
  (ADR-0165).
- **Attribution is always shown.** Both statement forms name the sender, the
  channel and the date, and recall marks the line `inferred`.
- Sensitivity, portability, the source checks, source erasure and the
  ninety-day formation window are unchanged.

### 6. The session-start summary: by the chat rule

The confirmed proposal said "included in the session-start summary". The
summary admits active memories only, and a new chat memory is provisional
until something repeats it. A first-class email memory follows that same rule:
it enters the summary once a later email from a known sender, a chat statement
or the owner's confirmation makes it active. This needs no change to what the
summary admits, and an unconfirmed claim never appears there.

The alternative was to store a first-class memory as active at once. It would
then be in the summary from the day it forms and would outrank a fresh
automatic capture of the owner's own words. Asked on 2026-11-20, the owner
chose to keep the chat rule.

### 7. Dreaming: not included

The confirmed proposal said "included in dreaming". Dreaming sends each
selected memory's subject and statement, and excerpts of up to 2,048 bytes of
its source event, to the dreaming model provider. Today it admits only
memories whose speaker is the owner and whose text is not sensitive. For an
email memory the source event is the Gmail read result: the headers, addresses
and body text of the messages on that page. Including email memories would
send a correspondent's words and the original email text to that provider for
the first time. Asked on 2026-11-20 with that stated, the owner chose to keep
email memories out. They stay sensitive and attributed to their sender, and
the existing rules keep them out. Including them later needs its own decision
record.

### 8. Existing memories are re-rated by an owner-run command

`agent memory rerate-email --owner TENANT/PRINCIPAL` previews by default and
applies with `--confirm`. It walks the retained email sources two hundred to a
transaction, recomputes each live email memory's retention and standing by
decisions 1 to 4, and reports counts only: examined, changed, promoted,
returned, first-class, guesses, and memories by retention. Each transaction
reads feedback, threads and senders again, so a mark the owner gives while the
command runs is not undone by what an earlier transaction read. The preview
reports what applying would leave. Nothing is backfilled at deploy, and no
later assessment or feedback changes a memory the command has not yet brought
under the table. A memory that already expired is not revived.

### 9. Gates and evidence

- **Gate 25, semantic memory authority, holds as written.** Email evidence
  stays attributed, inferred and sensitive, cannot overwrite an owner
  assertion, and the evaluated `email-semantic@1` policy is unchanged. Its
  wording and its test do not change. Decision 5 strengthens the "cannot
  overwrite" clause.
- **Gate 26, semantic memory lifecycle, holds as written.** The source date
  still governs: every horizon is measured from it, and import, repeated reads
  and self-citation renew nothing. Its wording and its test do not change. What
  changes is the length of the horizon, which Section 6 now states.
- **Gates 24 and 27** are untouched.
- **The `email-semantic@1` evidence digest changes.**
  `semantic_implementation_sha256()` covers `memory/formation.py`, which this
  change edits, so any stored `email-semantic@1` activation artifact no longer
  matches and that policy stays off until re-evaluated. Production runs
  `email-semantic@2` with People on and reads no artifact, so nothing running
  changes, and no paid evidence was regenerated.
- **The policy label stays `email-semantic@2`.** The label is part of each
  fact's identity; moving it would form every retained fact a second time.
- No hard gate is added. Unit, contract and PostgreSQL tests are the evidence.

## Consequences

- An important thread's memories now outlive the thirty-day body cache, so
  more of them answer from the memory alone.
- The store grows. Formation runs at 100 to 400 email memories a week, and the
  upper tiers no longer expire them. The owner's question set (ADR-0339) is the
  measure, run before and after.
- A first-class memory needs a repeat or a confirmation before the
  session-start summary shows it, and dreaming never sees email memories.
- Requiring a passed sender check has a cost. A sender whose domain publishes
  no DMARC policy never passes, so that sender's mail forms attributed guesses
  however well the owner knows them. Mail cached before the check existed
  reads no verdict and forms guesses until Gmail returns it again. Both are
  still kept by the thread's importance.
- A first-class statement no longer carries identifiers, so two emails that say
  the same thing read alike apart from sender and date. The renewal rule folds
  the exact repeats.
- A reply that quotes the earlier message can renew a memory through the quoted
  text. The assessment already reads the newest message first; no separate
  quote filter is added here.

## Alternatives considered

- **First-class standing for every sender.** Rejected by the owner: anyone can
  email them a "fact".
- **First-class standing for any thread rated important, whoever sent it.**
  Rejected. The rating is a model's reading of the sender's own text, so a
  stranger could write their way to it.
- **Accept a familiar address whose sender check gave no verdict.** Rejected
  in review of the change. An unauthenticated From line is what a spoofed
  claim would arrive under, and two such messages could have made the claim
  active. ADR-0336 rejects it for signatures for the same reason.
- **Lower the sensitivity so the review flag and dreaming follow by rule.**
  Rejected. It would widen which surfaces and providers see correspondence.
- **A new policy label, `email-semantic@3`.** Rejected: see decision 9.
- **Backfill at deploy.** Rejected. The owner runs the re-rating and sees the
  preview first.
