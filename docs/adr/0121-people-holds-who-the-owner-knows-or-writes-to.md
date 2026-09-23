# ADR-0121: People holds who the owner knows or writes to

- Status: Accepted (authorized by the repository owner, 2026-09-23)
- Date: 2026-09-23
- Related: ADR-0100, ADR-0101, ADR-0113, ADR-0116, ADR-0117; Sections 1, 5,
  7 and 9 of `docs/plan/people-and-relationships.md`
- Amends: ADR-0100 (who becomes a person) and ADR-0116 decision 3 (mail keeps
  forming attributed facts, but no longer identities)
- Detailed design: `docs/plan/people-and-relationships.md`

## Context

On 2026-09-23 production People held 130 provisional people and three the
owner had created. 128 of the provisional people came from names inside
email bodies. They included strangers and public figures, pronouns such as
"I", "you" and "we", the owner's own address and handle, other people's
relatives ("Mom" three times), and duplicates of the owner's family. Only two
came from the owner's own Chat statements, and both were tied to the owner.

- Nothing ever removed a provisional person. Deleting or expiring a fact left
  its Person row behind, and Needs review listed every provisional person.
- No person had any interaction history. Correspondence projection required
  a `ready` mail account, and every refresh marks the account `syncing` before
  it registers mail. Tests used `ready` accounts and hid the gap.
- Correspondence writes one identifier copy per message. Resolution capped
  its candidates at one hundred rows, so a frequent correspondent would have
  turned ambiguous, and substring search let longer names crowd out a match.
- Chat context matched display names by word boundary, so a person named "I"
  or "you" could reach almost any conversation.

The owner set the rule on 2026-09-23: People holds the people the owner knows
and the people the owner interacts with.

## Decisions

1. **A person joins People only from evidence the owner produced.** There
   are three paths:
   - an owner action: create, confirm, pin, rename or correct;
   - an owner Chat claim that ties them to the owner: a relationship or
     commitment whose other endpoint is the owner, or a reported meeting whose
     text establishes owner participation with "I", "me", "we" or "us";
   - mail the owner sent: a named To or Cc recipient who is not a role
     mailbox, a pronoun, or one of the owner's own names or addresses.
2. **Every other mention links to someone already in People or stays
   unresolved.** Its fact still forms. A stranger named in mail keeps the
   attributed fact, which expires after thirty days, and gets no People entry.
   "My sister Maya ... her partner Jules" adds Maya. The partner claim keeps
   its evidence with Jules unresolved.
3. **Pronouns and the owner's own identities never name a person.** They
   never create a person, match one, or select one for Chat context. The
   owner's identities are every mail account's address and verified addresses
   in any sync state, their local parts, and the From name on the owner's own
   mail.
4. **Correspondence records who the owner writes to.**
   - It needs only the account's address, not a `ready` status.
   - Mail from an unknown sender records one unattached address endpoint and
     no person or history.
   - The owner's first reply adds the person and adopts that earlier mail as
     received history, a bounded number per message.
   - Older mail processed after the reply attaches to the same person when
     one person has ever held the address and the owner never ended that
     assignment.
   - A person is created only when no assignment of the address is live at or
     after the message, so a gap between two holders creates no duplicate.
   - Ending an alias ends every observed copy of that assignment, and a late
     copy of mail inside an ended assignment ends with it.
   - An erased row is skipped instead of failing the message's registration.
5. **The directory follows the rule.** People lists active and provisional
   people. Needs review lists provisional, unpinned people with no
   owner-confirmed or channel-observed identifier: people known only by a
   name or a role, such as "My brother". `GET /v1/people` accepts a repeated
   `state` and `review=true`, and both bind the cursor.
6. **Resolution reads each assignment once.** Lookup is exact on the
   normalized value and returns one row per distinct assignment, so message
   copies and longer names cannot crowd out a match. An address or number the
   owner stated in Chat identifies that person for mail and texts. A person
   the owner creates gets an owner-confirmed name alias, so Chat mentions of
   that name link to them.
7. **A one-time repair aligns existing data.** The command is
   `agent people repair-directory --owner TENANT/PRINCIPAL`, and it previews
   by default. With `--confirm` it does three things:
   - projects the headers of retained mail from the last ninety days again,
     without model calls, skipping bulk, excluded, suppressed and
     unverifiable mail;
   - adds owner-confirmed name aliases to active people without one;
   - removes provisional, unpinned people that nothing the owner did ties to
     them.

   Pronoun and self-reference people are removed whatever else was recorded.
   Removal deletes facts that mail formed only about removed people through
   the governed delete of ADR-0117. It unlinks facts the owner stated, detaches
   mentions and address endpoints, and suppresses no source. It appends one
   content-free audit event per removal. The repair refuses to run beside a
   running People import.
8. **No new hard gate.** As with ADR-0113 and ADR-0116, the evidence is unit,
   contract and PostgreSQL tests. The People gate count stays at 36.

## Consequences

- Someone who only emails the owner is not in People until the owner replies.
  Their earlier mail then appears as history. A text to an unknown number adds
  no one.
- A fact formed before its subject joins People stays unlinked, because a
  stored mention with no person wins over re-extraction. Relinking is
  follow-up work.
- Deleting a mail-formed fact also resets the generated summary of the thread
  it came from. It excludes that retained passage from further formation.
  That is the governed delete's defined behaviour, and the repair preview
  counts both before anything changes.
- The `people.v1` and `email-people.v1` corpora label mail-body and untied
  Chat mentions as resolvable, so recall reads lower against them. That gates
  nothing under ADR-0101; a v2 corpus is follow-up work.
- Group mail stays excluded by the list rule of ADR-0116, which is still an
  open decision.
- A database restore does not replay the repair; rerun it after a restore.
- The change alters the People implementation digest, so an open People
  import job stops and must be started again.

## Alternatives considered

- **Age provisional people out after a period.** Rejected. The directory
  would still fill between sweeps, and age says nothing about whether the
  owner knows someone.
- **Admit anyone who emails the owner.** Rejected by the owner. Mailing lists,
  services and strangers write to the owner too.
- **Keep creating people from mail and hide them in Needs review.** Rejected.
  That is the flood the owner reported, and resolution and Chat context would
  still see those people.
- **Delete the facts along with the removed people's source threads.**
  Rejected. Source exclusion also removes a thread from the mailbox view and
  blocks later learning from it.
