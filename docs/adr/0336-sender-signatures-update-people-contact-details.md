# ADR-0336: A sender's own signature updates their contact details in People

- Status: Accepted (owner, 2026-11-20)
- Date: 2026-11-20
- Related: ADR-0100, ADR-0116, ADR-0121, ADR-0125, ADR-0126, ADR-0165; Sections
  4, 5, 7 and 10 of `docs/plan/people-and-relationships.md`
- Amends: ADR-0121 decision 4 (what correspondence records about a sender)
- Detailed design: `docs/plan/people-and-relationships.md`,
  `docs/plan/email-experience.md`

## Context

On 2026-11-20 the owner set the rule: "Signature files should be used to
update contact information in People but should not show up in the email
content." The display half shipped the same day: the Email reader leaves a
signature out of the reading text and the retained body keeps it.

The People half did not exist. That day Rory O'Driscoll's production profile
held a name alias and the `rory@scalevp.com` address, while his retained mail
ended with a signature giving a title, "Partner, Scale", and a number written
as `650.378.6038`.

Four existing rules bound the design:

- Mail is external untrusted content. A signature is the sender's own claim
  about themselves, never verified identity.
- Names, titles and domains cannot merge people, and a shared phone stays a
  contact endpoint (ADR-0121, ADR-0125).
- A phone is normalized to E.164 only when a country context is known.
- The email assessment prompt tells the model to infer no kinship or
  affiliation from signatures or domains.

## Decisions

1. **Two details are taken: phone numbers and the line under the name.**
   - *Phone numbers.* Each becomes a `phone` identifier on the sender. A
     number written after a fax label is skipped, and at most four are kept.
   - *The line directly under the sender's name.* It becomes a `role`
     identifier on the sender, kept exactly as written, such as
     "Partner, Scale". It is refused when it holds a digit, an address or a
     link, or begins "Sent from".
   - *Organisation.* It is not parsed out of that line, and no organisation,
     employment relationship or belief is created. The line is shown as what
     the sender wrote, so nothing is inferred about who employs whom.
   - *Links, postal addresses and everything else are not taken.* A profile
     link is a handle in all but name, and a handle could later be asked to
     identify someone. A link in mail is also the usual lure. Both wait for
     a decision of their own.
2. **Trust: the sender's claim, shown as a claim, never as identity.**
   - The rows carry the existing weakest verification, `contextual`, in a
     reserved namespace, `signature`, with the context "email signature".
     They are never `channel_observed`, because the channel carried the
     address and not the number.
   - A message contributes only when all of these hold:
     - it is received mail, not the owner's own and not a draft;
     - its `sender_check` is `pass`, Gmail's own DMARC verdict for the From
       domain (ADR-0165). `fail` and `none` contribute nothing, so mail read
       before the check existed contributes nothing until it is read again;
     - the From address resolves, at the time the mail was sent, to exactly
       one person already in People, by the same rule correspondence uses,
       and the owner has not ended that assignment;
     - the address is not a role mailbox;
     - the thread page holds the complete body.
   - The detail goes to the person the From address resolves to. The passed
     check vouches for the sending domain under its DMARC policy, not for the
     mailbox: someone able to send as another mailbox of the same domain can
     put a known person's address in the From line. Gmail reports no control
     that binds the mailbox; ADR-0340's consequences state the same limit and
     what bounds it. A display name alone never selects a person, so mail from
     another address that borrows a known person's name updates nobody.
   - Only the sender's own signature counts. The block must sit under a line
     that is exactly the From display name, of two or more words, and above
     the first quoted line, reply header or forward header. A signature
     inside quoted history or a forward is never read, whoever wrote it.
   - Mail from someone not yet in People writes nothing. Their next message
     after the owner first replies supplies the details.
3. **A signature detail never identifies or merges anyone.**
   - Identity resolution refuses the `signature` namespace outright.
   - The duplicate pass ignores it, so two people who sign with one office
     number are neither merged nor suggested, and a number the owner gave one
     person never pulls in a correspondent who signs with it.
   - It never selects Chat context and never matches a text or a call.
   - The namespace is reserved: a model-proposed mention or an owner alias
     naming it is refused, so nothing else can write a row that reads as a
     signature detail.
   - The owner makes a number an identifier by adding it in the profile or
     stating it in Chat, as before.
4. **A number is stored as written.** `650.378.6038` is stored as
   `650.378.6038`. No country is guessed and nothing is converted to E.164,
   including a number written with a `+`. Inside the namespace two spellings
   of one number compare equal by their digits. A number is recognised when
   it is written with a `+`, or in the ten-digit North American shape; other
   national shapes are not recognised without a `+`.
5. **Details persist, and the newest signature replaces the older one.**
   - They do not take the 30-day expiry of mail-formed facts. They are
     assignments with an interval, not beliefs, and they last while their
     source mail supports them.
   - Each message writes its own copy, with that message as its only source,
     valid from the time it was sent. The profile lists each distinct detail
     once. Placing a message reads every copy of the titles, or of the
     numbers, the sender's signatures have stated, ended or not, at most 2,000
     of each detail; past that the message adds nothing. Erasing or excluding a message removes its copies. A detail other
     retained messages also stated remains on theirs.
   - Titles and phone numbers are replaced separately. When a later message
     states a different title, the earlier title ends at that message's sent
     time. When a later message states phone numbers, an earlier number it
     omits ends the same way. A signature with no title, or no numbers, says
     nothing about them and ends nothing.
   - The rule reads sent times, not arrival order. A copy holds from its
     message's sent time to the earliest later signature that omits it. A
     message read late takes its place in that sequence: it ends an earlier
     copy that still held at its sent time, even one a later signature had
     already ended, and its own copy ends at the next signature that omits
     it. The recorded intervals are therefore the same whatever order mail is
     read in. Until the review of 2026-11-20, three or more signatures read
     out of order could leave two titles holding together. A message that
     left no copy, because it was erased or stated only a detail the owner
     had ended, is not part of the sequence.
   - A detail the owner ended stays ended. A later signature repeating it
     adds nothing, and an older signature read late does not move the end the
     owner set.
6. **Extraction is deterministic and no model is called.**
   - The block is found by the reader's third sign: the sender's name line
     and the short lines under it. One function now serves both the reader
     and People, so what the owner no longer sees and what People reads are
     the same block.
   - People reads the retained plain body, never the presentation HTML, so a
     mailer's HTML mark is not evidence here and every stored value is an
     exact substring of the retained body.
   - The assessment prompt is unchanged. The model is still told to infer no
     kinship or affiliation from signatures or domains.
7. **No flag and no migration.** The rule is the owner's instruction and uses
   existing record kinds, fields and verification values, so a build that
   predates it can still read the rows, as contextual aliases. Its identity
   resolution never asks for the namespace, and its duplicate pass skips a
   number it cannot normalize, which is every signature number except one
   written as bare `+` digits. See the rollback note under Consequences.
8. **No new hard gate.** As with ADR-0121 and ADR-0125, the evidence is unit
   tests. The People gate count stays at 36.

## Consequences

- A sender who signs with only a first name, or on one line with their title,
  contributes nothing, and neither does a signature below quoted text.
- A body with no line structure, as when Gmail supplied only flattened HTML,
  contributes nothing.
- The sender's name inside a list is an entry of that list, never a signature:
  a bulleted line, or any line under one that introduces a list (a line ending
  in a colon, a short label such as `Cc:` before the first entry, a bold line
  or a heading), across blank lines, until a finished sentence, a closing such
  as "Best,", a `--` rule or a quoted line ends the list. A signature written
  straight under a list that nothing ends is therefore left in the reading text
  and contributes nothing. That is chosen on purpose: reading the next entry as
  the sender's title, or removing real names from an email, is the worse error.
  A list with no line that introduces it is not recognised.
- A signature-shaped example in a code block is the message: the reader
  leaves it in the reading text and People reads nothing from it. One function
  decides which lines are code, from the text as it was written, and each
  caller carries its answer with the lines, so that no stripping or reflow can
  hide a block from a later pass.
    - *Fenced.* A block opened by three or more backticks or tildes, closed or
      not, by the same definition of a fence the reader's placement uses.
    - *Indented.* Lines set four columns or a tab in from the text's own
      margin, starting under a blank line or under a line that ends in a
      colon, and running across blank lines while the indentation holds. So
      `Format:` followed by an indented `-- ` line and three indented lines is
      an example, with or without a blank line between.
    - *Not code,* because mailers indent ordinary text there: an indented line
      that continues a paragraph; anything under a list item, until a
      paragraph of its own ends the list; a quoted line; anything under a
      reply or forward header; and a message set in as a whole, because
      indentation is measured from its own margin. A signature in any of
      these is removed and read as before. Only spaces and tabs are
      indentation. A header is `On … wrote:`, a `From:` line,
      `Begin forwarded message:`, or a line of dashes around words such as
      `-----Original Message-----`; a line that only starts with dashes, such
      as the comment `-- get users`, is not.
    - In a code block a `-- ` delimiter, the sender's name and a block that
      repeats an earlier one start no signature and are not removed. Code or a
      table row under the name makes that block the message as well, and so
      does code under a delimiter. A signature after a code block is removed
      and read as before, and a fence elsewhere in a message no longer keeps
      a `-- ` signature in view: code is decided line by line.
    - *Limits.* A real signature that its writer sets four columns in under a
      blank line reads as an indented block, so it stays and gives nothing.
      An indented example of mail headers that begins `From:` reads as a
      header, so nothing under it is code. Extracted HTML has no indentation,
      so there only a fence is code. `<pre>` and `<code>` are not treated as
      code: HTML mail carries whole converted plain-text bodies in `<pre>`,
      signature included, and treating that as code would leave those
      signatures in view. People reads the plain part, so an example that only
      the HTML marks as code reaches People without that mark.
- A motto under the name is stored as that person's line. It is shown as the
  sender's own words, and the owner can end it.
- Someone who alternates between two signatures with different numbers will
  see the omitted number end and return.
- People already in the directory gain details from new mail. Retained mail
  from the last ninety days is read again by `agent people repair-directory`.
- Erasing a message removes its copies but does not replay the sequence. An
  end that message had set on an earlier copy stays as recorded, so an earlier
  title can read as ended with no current one, until the sender's next signed
  message. Recomputing those ends at erasure means rewriting surviving records
  in both People stores and is left as follow-up work.
- `memory/people_correspondence.py` changes, so the email People
  implementation digest changes and an open People import job must be started
  again.
- The Apple client shows the rows without a change, as "From context, email
  signature". A dedicated label is follow-up work.
- Rolling back to a build that predates this decision leaves the rows in
  place. That build's duplicate pass would treat a signature number written
  as bare `+` digits as an observed endpoint, so it could merge a provisional
  correspondent into a person the owner gave the same number. No command
  removes the rows yet; one is needed before such a rollback is routine.

## Alternatives considered

- **Have the assessment model extract the details.** Rejected. It would
  weaken the prompt's rule against reading signatures, cost a model call's
  attention on every message, and return values that need grounding anyway.
- **A fourth verification state, `sender_stated`.** Rejected for now. A build
  that predates it cannot read such a row, so a rollback would fail People
  reads for that owner. The namespace carries the same distinction.
- **Store the title as a mail-formed belief.** Rejected. It would expire in
  thirty days, would not be replaced by the next signature, and would put a
  deterministic write through a path built for model proposals.
- **Use the mailer's HTML signature mark.** Rejected for People. The HTML is
  presentation only, and Gmail keeps the mark on a forwarded signature.
- **Accept `sender_check` of `none`.** Rejected. An unauthenticated From line
  is exactly what a spoofed signature would arrive under.
- **Guess the country from the owner's own numbers or the sender's domain.**
  Rejected. The spec allows E.164 only with a known country, and the number
  is never used for matching.
- **Record details for unknown senders and attach them on the first reply.**
  Deferred. It would store contact details for strangers before the owner
  has shown any interest in them.
