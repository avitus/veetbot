# ADR-0341: One fact in an email thread forms one memory

- Status: Accepted (the owner chose both formation rules and the merge command
  with a preview on 2026-11-20)
- Date: 2026-11-20
- Amends: ADR-0340 (decision 4, what counts as a repeat), Section 6 of
  `docs/plan/email-experience.md`
- Related: ADR-0090, ADR-0116, ADR-0121, ADR-0247
- Detailed design: `docs/plan/email-experience.md`

## Context

A read of production on 2026-11-20 found 668 live memories formed from email.
271 of them, 40%, stated a value that another memory from the same thread
already stated. In the largest thread it was 206 of 255, and one value was
stored 57 times.

Three things produce the copies, and one makes each of them count.

- **Analysis restarts when a thread changes.** A new message gives the thread a
  new fingerprint, and the assessment walks every message passage again. Each
  older message is assessed afresh.
- **The newest message is shown in every call of that walk**, so its facts are
  proposed again each time.
- **A reply quotes what it answers.** A fact's quote only has to be a substring
  of the message it names, and a quoted sentence is a substring of every reply
  that carries it.
- **A fact's identity includes the model's wording.** The key hashes the
  subject, the predicate and the quote the model chose. The model words the
  same fact differently each time, so each proposal is a new fact and a new
  memory.

157 of the 271 copies came from one message assessed again in other words. The
rest came from other messages in the same thread.

ADR-0340 keeps email memories by their thread's importance, most of them with
no expiry, so the copies no longer age out after thirty days.

The owner chose rules in code over a change to the model's instructions, which
would not be guaranteed to hold and would need the assessment's quality
evidence run again.

## Decision

Everything here applies to the People policy, `email-semantic@2`.

1. **One memory per fact per message, whatever the wording.** A proposed fact
   is the fact a message already formed when its subject, belief type and value
   match. Values are compared without case, spacing, surrounding quotation
   marks or a closing punctuation mark, which a model may or may not take with
   the value. Every other mark is part of the value: `-5 °C` is not `5 °C`, and
   `3.5%` is not `3-5%`. The value is read from the end of the stored
   statement, past the subject, so a quotation inside the subject or the value
   does not change it. A memory linked to a person is stored under that
   person's identity followed by the subject, and the subject is read from
   there. The first memory stands, in the words it was formed
   with, and the proposal changes nothing. This holds for a memory in any
   status: one that expired and one a merge superseded are not formed again.
   The retained source record remembers each fact it formed under this
   identity, beside the exact-wording key it already kept. A deletion already
   closes its source to formation, as before.
2. **A quoted sentence is not the replier's statement.** A fact forms nothing
   and renews nothing when its quote is quoted text in the message it names.
   Quoted text is either of:
   - text that appears in that message only on lines marked `>`;
   - a quote of at least six words that also appears in an earlier message of
     the same stored thread, whatever the reply's layout.

   Line markers and line wrapping are ignored in both comparisons. A shorter
   quote is judged by the line markers alone, because two people can each
   write "Approved". A forwarded message's content is in no earlier message of
   the thread, so it still forms.
3. **A later message that restates a fact renews the one memory.** ADR-0340
   renewed a memory only on an identical subject, predicate and value. The
   predicate no longer has to match: the same subject, belief type and value
   from a different message with a later date renews the existing memory. The
   rest of ADR-0340's rule is unchanged, including what a repeat settles.
4. **Existing copies are merged by an owner-run command.**
   `agent memory merge-email-duplicates --owner TENANT/PRINCIPAL` previews by
   default and applies with `--confirm`. Within one thread, live email
   memories that share a subject, a belief type and a value are one fact. The
   memory with the earliest evidence stays. Each other copy is superseded by
   it: status superseded, validity closed, pointing at the memory kept. The
   memory kept takes the latest evidence date and the longest retention among
   its copies, as rule 3 would have given it, so the fact does not expire
   while a copy's later evidence still holds. Its standing, wording and first
   evidence date do not change. The message behind that later evidence is
   linked to the memory kept, so ADR-0340's re-rating still follows that
   message's thread. The command touches only what ADR-0340's re-rating may
   touch, reports counts only, and appends one content-free
   `email.memory.merged` event per thread it changes. A second run changes
   nothing.
5. **What the merge leaves alone.** Copies stored under different subjects are
   not merged, because two people in one thread can each hold the same value.
   The preview counts them.
6. **A memory belongs to the thread that formed it.** A message of another
   thread that renewed or only found a memory lists it too. That listing is
   no membership: the listing record says its message formed the memory, or,
   on a record written before roles were kept, the memory's own record line
   names the thread. Without this a thread's earlier copy could supersede a
   memory another thread formed.
7. **A copy's later evidence is handed over only to a message that answers
   for it.** The command reads which messages list each memory before it
   takes a thread's locks. Under the locks it reads the copy and those
   messages again. The message behind the copy's latest evidence formed or
   renewed it, or is the one its record line names, and carries that evidence
   date. If a copy was renewed in between, none of the messages read answers
   for its new evidence, and the copy is left as it is; the next run reads the
   renewing message and merges it.

## Consequences

- Of the 271 copies seen, the merge can remove the 170 that share a subject.
  The other 101 sit under different subject keys and stay.
- Two different facts in one message that share a subject and an identical
  value, such as a title someone held at two companies, form one memory. The
  quote is not part of the identity because the model also varies the span it
  quotes.
- A reply that restates a short sentence word for word, with no `>` marks, is
  still read as the replier's own statement.
- Two people who each write the same sentence of six words or more, word for
  word and independently, are read as one statement and its quotation. The
  second forms and renews nothing. Rule 2 accepts this: the alternative reads
  every unmarked quotation as a fresh statement, and a wrong renewal can also
  settle a first-class memory as active.
- Values that differ only by a mark inside them, such as `Nov. 5` and `Nov 5`,
  are two values. A missed match leaves a second memory; a wrong match would
  remove a real fact.
- Quoting is detected in the stored thread. When an earlier message's text is
  no longer held, only the `>` rule applies, and rule 3 folds what it misses.
- The assessment still spends a model call each time it re-reads a passage.
  That cost is unchanged here.
- No gate moves and no gate wording changes. `memory/formation.py` and
  `memory/retrieval.py` are untouched.

## Alternatives considered

- **Tell the model to skip quoted text and facts already reported.** Rejected
  by the owner: it is not guaranteed, and the assessment prompt is bound to its
  quality evidence.
- **Stop the analysis from restarting on a new message.** Deferred. It would
  also save model calls, but it changes assessment invalidation (gate 13) and
  needs its own decision.
- **Merge copies stored under different subjects when the value is long.**
  Rejected for now: a length threshold is a guess, and a wrong merge removes a
  real fact.
- **Delete the copies.** Rejected. A governed delete also resets the thread's
  generated summary and excludes its passage from formation (ADR-0121).
  Superseding keeps the copy as history and points at the memory kept.
