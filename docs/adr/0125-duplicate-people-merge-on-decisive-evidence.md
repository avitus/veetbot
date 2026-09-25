# ADR-0125: Duplicate people merge on decisive evidence and are otherwise suggested

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: ADR-0100, ADR-0121; gate P01; Sections 5 and 9 of
  `docs/plan/people-and-relationships.md`
- Amends: ADR-0121 decision 1 (who can be a person) and the merge rules of
  ADR-0100
- Detailed design: `docs/plan/people-and-relationships.md`

## Context

After the ADR-0121 repair on 2026-09-23, production People held 72 people.
Several were duplicates of each other: "Erin Vitus" beside the owner-created
"Erin", "Cheryl Vitus" beside the Chat-created "Cheryl", and two "Sabina
Smith" entries with different addresses. Four were not people at all:
"Investment Team", "Partners", "API OAuth Dev Verification", and an address
shown as its own name.

Duplicates arise by design. Correspondence adds whoever the owner writes to,
keyed by address, while the owner and Chat name people without addresses.
A name copied from a mail header never merges identities (ADR-0121). Merging
was only possible through the owner's Repair identity flow.

On 2026-09-25 the owner asked to stop non-people from joining, and to merge
duplicates automatically, asking for confirmation only when there is enough
uncertainty.

## Decisions

1. **Groups, services and addresses are never people.** A label is refused
   when it is itself an address, or when it contains a word naming a group,
   department, organization or automated service, such as team, partners,
   board, support, newsletter or verification. It never creates a person from
   sent mail or Chat, never matches one, and never selects Chat context.
   Singular roles such as "partner" or "analyst" stay person labels. So do
   words that are also common surnames, such as "Mailer" or "Bank". More role
   mailbox local parts are recognized. The directory repair of ADR-0121
   removes existing entries like these with the reason `group`, whatever
   their history.
2. **Decisive evidence merges without asking.** An address, number or handle
   the owner gave one person is decisive when a provisional, unpinned person
   created from correspondence also holds it as an observed endpoint. The
   owner gave it by an owner-confirmed alias or a Chat statement. The
   correspondent then merges into the owner's person. An endpoint that
   several owner-given people share, such as a family address, merges
   nobody. Neither do role mailboxes or the owner's own addresses.
3. **Anything weaker asks the owner.** These become a merge suggestion that
   the owner confirms or dismisses:
   - full names that agree, allowing a differing middle name, initials or a
     generational suffix;
   - a first name that equals another person's first name;
   - a nickname of at least three letters that starts another person's first
     name;
   - an address that only correspondents share.

   A first name that matches several people is suggested only for the one
   match that shares the owner's family name. The family name comes from the
   owner's address, for example "vitus" from "avitus". Names never merge
   anyone on their own, because two people can share a name (gate P01).
4. **The owner's answer is final.** A dismissed suggestion, an undone merge,
   or a split keeps that pair apart for good. A suggestion that stops
   matching is withdrawn, and reopens if the pair matches again.
5. **The weaker identity merges into the stronger.** Strength is, in order:
   confirmed by the owner, pinned, supported by the owner's own words, more
   recorded history, then the older identity. The survivor keeps its display
   name, and the merged identity's name aliases move to it.
6. **Merges stay ordinary identity operations.** An automatic merge runs
   through the revision-checked identity service and is marked `automatic`.
   The survivor's profile lists it with an undo. Accepting a suggestion
   applies an owner merge in the same way.
7. **When it runs.** The maintenance pass runs the duplicate check every 15
   minutes when its principal holds `people.write`. The command
   `agent people dedupe --owner TENANT/PRINCIPAL` previews the same pass, and
   `--confirm` applies it. `GET /v1/people/merge-suggestions` lists open
   suggestions. `POST /v1/people/merge-suggestions/{id}` merges or separates a
   pair under `people.write` with an idempotency key.
8. **Storage.** Suggestions are a new People record kind, `merge_suggestion`,
   referencing both identities, so erasing either person erases them.
   Migration `524f16dfc8f9` admits the kind.
9. **No new hard gate.** The evidence is unit, contract, PostgreSQL and native
   tests. Gate P01 is unchanged; this decision keeps names from merging anyone.

## Consequences

- Most duplicates are name matches, so the owner answers one question per
  pair in Needs review. Giving the right person an address, in the app or in
  Chat, settles a correspondent automatically.
- A merge touching more than the identity service's 1,000 assignments is
  skipped, as a manual repair would fail too. That affects only a very heavy
  correspondent.
- The family name is a heuristic from the owner's address. It only decides
  which of several first-name matches is worth asking about.
- Undoing an automatic merge works while the moved records are unchanged,
  like any identity repair.
- Rerun `agent people repair-directory` to remove group-named entries that
  already exist.

## Alternatives considered

- **Merge identical full names automatically.** Rejected. Two people can
  share a name, and gate P01 forbids that false merge.
- **Ask a model whether two people are the same.** Rejected. The People
  design keeps the private directory out of provider prompts, and a model
  score alone cannot merge people.
- **Notify the owner about each suggestion.** Deferred. Needs review is
  where the owner already looks, and a notification kind needs its own
  migration.
