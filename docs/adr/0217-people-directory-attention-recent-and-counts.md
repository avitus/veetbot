# ADR-0217: People directory attention, last contact and section counts

- Status: Accepted (owner, 2026-10-20: "implement all 4")
- Date: 2026-10-19
- Related: ADR-0121, ADR-0125, ADR-0214; Section 28 of the engineering plan
- Detailed design: `docs/plan/people-and-relationships.md`

## Context

The A2 People sidebar has a Directory with **Needs you** (a count; rows read
"Needs you · Reply by Friday about the lease"), **Recent** (rows show the last
contact time) and **Everyone** (a count), and a Maintenance group with
**Possible duplicates** (a count).

`GET /v1/people` already supports `sort=recent`, which orders by the newest
visible interaction's `occurred_at` but does not return it; `review=true`
(provisional, unpinned, no owner-confirmed or channel-observed identifier,
ADR-0121); `pinned`, `relationship` and `state`. `PersonView` in
`src/agent_core/domain/people_public.py` has no last-interaction time,
attention reason or count. `GET /v1/people/merge-suggestions` is paged with no
count (people-and-relationships.md:641).

The design names list filters, pins and recent-interaction sorting, and a
**Needs review** collection holding review people and possible duplicates
(people-and-relationships.md:588-598). Open threads are commitments and
unresolved topics with "no automatic sends or reminders"
(people-and-relationships.md:608-609). Commitments are `proposed`, `open`,
`completed`, `cancelled` or `uncertain`, with debtor, beneficiary and any
supported deadline (people-and-relationships.md:394-398); `CommitmentView`
carries `due_at` and `due_precision`. ADR-0125 deferred notifying about merge
suggestions.

Without a server change the A2 client ships Needs you as the designed Needs
review set (`review=true`), Possible duplicates on its own page, Recent as
`sort=recent`, and Everyone as active plus provisional, with no counts and no
last-contact time.

## Decision

Three parts; the owner can accept each separately.

### A. `last_interaction_at` on `PersonView`

`PersonView` gains `last_interaction_at: AwareDatetime | None`, read-only and
computed by exactly the predicate `sort=recent` uses today: the newest
`occurred_at` among interactions that reference the person, are visible at the
request's ceiling and `known_at`, are not superseded, have a known time, and
are not after `as_of`. Null means no visible dated interaction. It is
populated on every response that returns a `PersonView`, whatever the sort,
so a null never means "not computed". The Postgres adapter computes it with
the same join the sort uses; both adapters share a contract case that the
field equals the sort key.

The field reveals only the time of an interaction the caller can already list
in the person's history at that ceiling, so it adds no disclosure.

### B. A counts route

`GET /v1/people/counts?ceiling=` under `people.read` returns
`{everyone, needs_review, possible_duplicates, open_threads, counted_at}`:

- `everyone`: active and provisional people, matching the default list;
- `needs_review`: matching `review=true`;
- `possible_duplicates`: open merge suggestions whose two people are both
  visible at the ceiling, matching the suggestion list;
- `open_threads`: matching part C's filter, or null when C is not accepted.

Each count applies the list's principal, tenant, ceiling and suppression
predicates, so a suppressed or above-ceiling person is never counted. The
route is mounted before `/v1/people/{person_id}`, as
`/v1/people/merge-suggestions` already is. A summary route is chosen over a
`total` on each page for the reason ADR-0214 part C gives: one request fills
the sidebar, pages do not pay a count, and keyset pages do not carry a total
that drifts.

### C. Owner-owed open threads may appear under Needs you

`GET /v1/people` gains `open_threads=true`: people who are the beneficiary of
an `open` commitment whose debtor is the owner, with a supported `due_at`
(precision `instant` or `day`) that is overdue or within the next fourteen
days. `PersonView` gains `next_thread: CommitmentView | null`, the earliest
such commitment with its revision and belief id, under the same
visibility rules as the person's Open threads section; `proposed` and
`uncertain` commitments never qualify, because they may come from unsent
drafts or ambiguous mail.

The client's Needs you becomes the union of `review=true` and
`open_threads=true`; a review row reads "Needs you · Who is this?", a thread
row reads "Needs you · Reply by Friday about the lease". Each thread row
offers the existing commitment correction through
`POST /v1/people/{id}/corrections` (a `CommitmentReplacement` marking it
completed or cancelled, or a rejection), so a stale thread can be cleared
where it is shown; the owner's correction is the attributed report that
completion requires.

**Why this does not breach "no automatic sends or reminders".** That rule
forbids the system acting or prompting on its own: sending, scheduling a
nudge, or notifying. Part C adds none of those. It is a passive list the owner
opens, showing what the Open threads section of each person already shows,
ordered by due date. It adds no notification kind, no push, no app-icon badge
and no Chat message. If the owner reads the rule as forbidding any due-date
prominence, C is declined and Needs you stays the review set.

## As built (2026-10-20)

Accepted parts were built with these choices, each no weaker than the text
above:

- **A.** The service keeps returning plain domain records, which callers may
  write back; the HTTP routes and the CLI add the two fields when they build a
  response, and `PersonView` declares both without a default, so a response
  that skipped that step fails instead of reporting "never". A token without
  `people.read` (write-only) gets both as null, because they read history.
- **B.** `everyone` counts `state=active&state=provisional`, the A2 Everyone
  list; a list with no state also returns merged identities. Counts page
  through the lists themselves, so suppression and the display-name hazard
  check apply exactly as on the list. A sixth number, `needs_you`, counts the
  union of the review and open-thread sets once per person, because the native
  Needs you shows both and their sum would count a person in both twice.
- **C.** `open_threads=true` stands alone: combined with search, review,
  relationship, pin, state, sort or `as_of` it is a validation error. Its
  cursor is keyed on due date and person. A thread is shown exactly as the
  person's Open threads section shows it: resolved, safe, and backed by a live
  belief at the ceiling.

## Consequences

- Recent rows can show "Last contact 3 days ago"; the sidebar shows numbers.
- With C, the owner sees promised replies in one place; the cost is that
  extraction mistakes become more visible, mitigated by excluding `proposed`
  and `uncertain` and by in-row corrections.
- No write authority, scope, notification kind or retention rule changes.

## What changes if accepted

- Routes: `PersonView.last_interaction_at` (A); `GET /v1/people/counts` (B);
  `open_threads` filter and `PersonView.next_thread` (C). No new scope.
- Gates and tests: People route-table and visibility matrices gain the counts
  route; adapter parity cases for the new field, each count against its list,
  suppression and ceiling exclusion; C's commitment-state, debtor and window
  rules; route ordering.
- Specs: people-and-relationships.md *Native experience*
  (people-and-relationships.md:588-609) and the route table
  (people-and-relationships.md:630-648); C records the reading of "no
  automatic sends or reminders" stated above.
- Client: counts in the sidebar, last-contact text on Recent rows, Needs you
  subtitles and in-row thread corrections; older servers keep today's
  count-free sidebar.

## Alternatives considered

- **Return the sort key only under `sort=recent`.** Rejected: a field that is
  sometimes null for "not computed" and sometimes for "never contacted" is
  ambiguous.
- **Notify about due threads or new duplicates.** Rejected: that is the
  automatic reminder the design excludes, and ADR-0125 deferred it.
- **Count on the client by paging every list.** Rejected: hundreds of
  requests on a large directory, and wrong while pages change.
- **Include `proposed` commitments.** Rejected: an unsent draft can create
  one, and drafts never prove a promise.
