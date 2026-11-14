# ADR-0324: Every mail list scrolls, collections page newest first, and catch-up continues above the share

- Status: Accepted (the owner asked for these repairs on 2026-11-14)
- Date: 2026-11-14
- Related: ADR-0231, ADR-0234, ADR-0256, ADR-0298, ADR-0303; Milestone 26
- Amends: ADR-0303 decision 3 (touch paging), ADR-0234's rank selection for
  Inbox and its collections, and ADR-0256 decision 2 (later catch-up pages)
- Detailed design: `docs/plan/email-experience.md`

## Context

On 2026-11-14 the owner reported three Email problems:

1. On iPhone and iPad, Important showed only five conversations. ADR-0303
   kept touch at five rows per page behind a More button.
2. On iPhone and iPad, Inbox showed the same five conversations as Important.
   The thread-list route ordered every view by rank before recency, so the
   first five Inbox rows were the five highest-ranked conversations, which
   are also the first five of Important. The client then reorders the loaded
   rows by recency (ADR-0234), but it never receives newer ordinary mail. A
   Mac Inbox had the same flaw behind its 100-row page.
3. The Work account said it was checking Inbox mail back to June 16 but had
   reached only November 7. Production showed `inbox_complete` false, an open
   `inbox_cursor` and `inbox_reached_at` 2026-11-07. ADR-0256 lets a new-mail
   slice read only the first page of an Inbox pass. Since ADR-0298, spending
   normally sits above the 75% share, so almost every refresh is a new-mail
   slice. The first page reached back about a week, and no later page was
   ever listed.

## Decision

1. **Every list scrolls on every device.** iPhone and iPad load 100 rows a
   page, like the Mac. The touch list keeps its List rows and fetches the
   next page when its end scrolls into view. A More button appears only to
   retry a page that failed to load.
2. **Inbox and its collections page newest first.** The thread-list route
   orders Inbox, Needs you, Handled and Suspected spam by latest activity,
   with rank as the tie-break. Important, Other mail and search results keep
   rank selection. Rows still display newest first (ADR-0234).
3. **New-mail slices continue the Inbox catch-up.** After new Gmail changes
   take their reads, the reads left in a new-mail slice list and read later
   catch-up pages, as a full slice does. Listing and reading call no model.
   Imported mail older than the seven-day arrival window is not an arrival,
   so it is not assessed there. Server checks are unchanged: they read only
   the first page of a pass. The ninety-day history pass, re-assessment,
   correspondence summaries and the other work ADR-0256 defers still wait for
   a full slice.

## Consequences

- A touch list's first read asks for up to 100 thread summaries instead of
  five, as the Mac already does.
- Catch-up mail imported above the share appears in Inbox unranked until a
  full slice assesses it. It does not enter Important until then.
- A new-mail slice that reads catch-up mail is not an empty look, so it
  requests memory formation like any slice that read mail. That cost follows
  the catch-up and ends when the account reaches its floor.
- New mail still comes first: changes keep all eight full-thread reads, and
  catch-up gets only what they leave.
- No milestone or gate moves.

## Alternatives considered

- **Raise touch to a larger fixed page with a More button.** It still stops
  at an arbitrary count, which is the reported problem.
- **Say that catch-up is held instead of advancing it.** It reports the stall
  honestly but leaves the Work account without five months of Inbox mail,
  although reading it costs no model call.
- **Let server checks advance catch-up too.** It would finish sooner with
  Email closed, but server checks are bounded to stay small (ADR-0286), and
  foreground refreshes are enough to finish the pass.
