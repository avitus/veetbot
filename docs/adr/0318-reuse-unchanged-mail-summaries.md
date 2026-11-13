# ADR-0318: A mail list re-reads only the threads that changed

- Status: Accepted — owner accepted on 2026-11-12
- Date: 2026-11-12
- Related: ADR-0103, ADR-0303; Milestone 26
- Detailed design: `docs/plan/email-experience.md`

## Context

On 2026-11-12 the owner reported that Inbox "still takes forever to load"
after Important had been made quick. Both lists use the same route. Every
`GET /v1/email/threads` request read the summary of every cached thread,
validated each one, and only then filtered, counted, sorted and paged.

Measured on production that day: 9,962 cached threads, 106 MB of thread
payloads, 10 MB of summaries. One list request held its database transaction
for about 3.4 seconds and answered after about 5. The validation runs on the
API's event loop, so list requests queue behind each other, and the client's
background refresh issues one every few seconds. Important feels quick only
because the Mac shows its cached rows while the request runs; the first Inbox
read of a visit has nothing to show and waits for one or two full scans.

## Decision

1. **The email service keeps each owner's validated thread summaries in
   process memory and re-reads only the ones that changed.** Each list first
   reads a mark for every thread: its key with a value built from the record's
   revision, update time and pending-erasure flag, and no payload. A summary
   whose mark is unchanged is reused. Changed and new threads are read by key;
   threads no longer present are dropped.
2. **A list is as current as the scan it replaces.** Marks are compared on
   every request and nothing is served on a timer. Authorization, account and
   text filters, owner feedback, attention expiry, pending-archive
   reconciliation, counts, order and paging run on every request as before.
3. **What is kept is no more than the list already read.** Summaries carry no
   message content. They are held per tenant and principal, for at most four
   owners per process, and vanish with the process. A thread fenced for People
   erasure changes its mark and is re-read in its erased form.
4. **The store port gains two reads**, `list_thread_marks` and
   `get_thread_summaries`, covered by the shared contract on the in-memory and
   PostgreSQL adapters. No schema or API changes.

## Alternatives considered

- **Filter, count and page in SQL.** Owner feedback and attention expiry
  adjust priority per thread in application code, and the counts come from
  that adjusted snapshot, so the query would have to reimplement both.
- **Serve a cached response for some seconds.** It shows stale mail after an
  archive or a new arrival, and the first Inbox read still pays a full scan.
- **Store summaries as their own rows.** It removes the cost of skipping
  message content but still transfers and validates every thread per request,
  and needs a migration and dual writes.

## Consequences

- With an unchanged mailbox a list costs one key-ordered index read of marks
  plus the per-request filtering. At ten thousand threads on the in-memory
  store a repeat list request measured about 90 ms locally, against about
  550 ms before. The production figure is to be measured after deployment.
- Excluding a source drops that thread's kept summary in the process that
  handled the exclusion as soon as the erasure commits, waiting for any list of
  that owner already in flight. Any other process drops its copy at that
  owner's next list, or when the process restarts.
- The first list after an API restart still reads every summary once.
- Each API process holds one owner's summaries in memory, about 10 MB of
  source JSON at the current mailbox size.
- Cockpit feeds and worker selection keep the paged summary scan.
