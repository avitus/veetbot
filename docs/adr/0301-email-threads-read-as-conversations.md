# ADR-0301: Email threads read as conversations, with a brief for complex ones

- Status: Accepted (chosen by the repository owner, 2026-11-05)
- Date: 2026-11-05
- Related: ADR-0234, ADR-0237; Milestone 26
- Amends: `email-experience.md`'s reading-view rule that only an `On … wrote:`
  chain folds
- Detailed design: `docs/plan/email-experience.md`, `docs/apple-client.md`

## Context

The owner asked for a long board thread to be made readable: "I want the
irrelevant information stripped out. I want to clearly see who is replying."
It held 19 emails over a week. About 94% of its 77,000 characters was quoted
history, signatures, disclaimers and From/Sent/To blocks. The reader folds a
quoted chain only when an `On … wrote:` line introduces it, so the
`From: / Sent: / To:` blocks most of the other writers' mail clients start
their quotes with stayed in view, and every message repeated the ones before
it. The thread was also four conversations, not one: the whole board, the
owner forwarding it to an assistant, one investor writing to the owner alone,
and an investors-only group the company's CEO could not see. Only the To and
Cc lines showed that, and the Mac reader had moved those lines into the
side panel.

Three designs were built on MagicPath: A, a two-sided conversation; B, the
thread split into lanes, one per audience; and C, a brief of who is saying
what. The owner chose A as the default and C "for when there is additional
complexity".

## Decisions

1. **A thread of two or more messages reads as a conversation.** Each message
   is a bubble. The owner's own mail sits on the right: the server marks it
   `direction: sent`, and a sender matching the account's address counts as
   the owner too unless the server marks that message `direction: received`.
   Received mail claiming the owner's address is never shown as the owner's:
   it sits on the left under the raw address with an amber warning that it
   is not in the owner's sent mail, and the brief's card for that writer
   carries the same warning. Everyone else's mail sits on the left, with an initials
   avatar and the writer's name. A writer's consecutive messages share one
   name line. Day dividers separate the days. A thread of one message keeps
   the existing prose reader, because a newsletter or a single letter reads
   better at full width.
2. **A bubble shows only the writer's own words.** Quoted history now also
   folds when it starts with an Outlook header block (a paragraph that begins
   `From:` and continues with a `Sent:` or `Date:` line and a `To:` or
   `Subject:` line), a `-----Original Message-----` line, or an unquoted
   `On … wrote:` line that names an address, or a clock time on a date, so
   "On the 10:30 call, Priya wrote:" stays the writer's own text. A Gmail chain may be followed by more
   quoted blocks, but not by an unquoted reply. As before, nothing folds unless the message has text
   of its own above it. In a conversation, trailing paragraphs that repeat in
   the same writer's other messages fold as well, which removes sign-offs and
   signatures, and so does a trailing confidentiality disclaimer and a
   `---------- Forwarded message ---------` copy below the writer's note,
   which repeats mail the thread already holds. **Show full
   email** opens a bubble's whole reading text. The thread-wide **Show
   original** switch is unchanged. The single-message reader uses the same
   fold rule.
3. **The envelope decides the audience, and changes are marked where they
   happen.** A message's audience is its sender, To, Cc and the owner. The
   thread's everyone is the union of the audiences of messages that left
   nobody out, so a person reached only through a side conversation, such as
   an assistant the owner forwarded the thread to, never joins it. When a
   message leaves out part of everyone, an amber marker says who it was
   addressed to and who it was not addressed to, and each bubble in that side
   conversation carries an "Addressed to" line. The words never claim anyone
   cannot see a message, and a caveat says Bcc recipients and mailing-list
   members are not shown. When it adds people, a marker says who
   added whom. When the thread returns to everyone, a marker says so. Bcc and
   the members of a mailing list cannot be seen, so they never count.
4. **The people are listed above the conversation.** One chip per writer with
   a message count fades every other writer's messages when pressed. The
   people who were copied but never wrote are counted in one quiet line. A meta
   line says what share of the thread's text was repeated and folded.
5. **A complex thread opens on a brief.** A thread is complex when three or
   more people wrote and either one side conversation exists or the thread has
   eight messages or more. A complex thread opens on **Brief**, and a
   Conversation | Brief switch moves between the two views. The brief shows
   Veetbot's existing summary, each writer's latest point (the first
   paragraph of their latest message that is not a greeting, verbatim), the
   side conversations with who is not included, every file sent in the thread
   with who sent it, and who was only copied. Pressing a side conversation
   opens it in Conversation, filtered to that audience.
6. **The views are presentation only.** No model call, no stored copy, no
   route or schema change. The client decodes the `direction` field the thread
   projection already carries. The design's per-person stances and "numbers in
   play" need a model and are not built: they would need their own decision
   about cost and grounding.

## Alternatives considered

- **Folding on the server.** `reader_body` stays a faithful reading text and
  the client keeps deciding what folds, as it already did for Gmail chains;
  the Mac panel, iPhone and iPad then share one rule without a schema change.
- **B's lanes as the main view.** They show audiences best but split a
  thread the owner reads front to back. The brief's side-conversation list and
  the conversation's filter keep that information one press away.
- **Model-written stances in the brief.** Deferred: every complex thread
  opened would cost a model call, and the reply-all email cost history
  (ADR-0298) argues for measuring before adding one.

## Consequences

- A reply typed inside an Outlook-quoted block now folds with the quote, and
  **Show full email** shows it. Gmail's reply-below-a-quote still stays in
  view.
- Only English attributions and header words are recognized.
- The Mac panel's thread-wide **Show original** shows the conversation with
  every message's original body, even on a complex thread, and the switch to
  **Brief** waits until it is turned off.
- The client keeps one parsed conversation per thread for at most nine
  threads. It reuses one only while its messages are unchanged and replaces
  it when the thread changes. It drops a thread's parse when the owner
  excludes the thread, and every parse when the Email connection resets, so
  no excluded mail or old connection's mail stays in the client.
- The owner's copies of earlier messages in the thread decide the owner's
  address when the account has no verified address yet.
