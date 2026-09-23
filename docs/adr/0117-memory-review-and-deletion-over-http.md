# ADR-0117: Memory review and deletion over HTTP

- Status: Accepted (authorized by the repository owner, 2026-09-22);
  supersedes ADR-0070 decision 3
- Date: 2026-09-22
- Related: ADR-0045, ADR-0069, ADR-0070, ADR-0079, ADR-0101, ADR-0116;
  Sections 10 and 22 of the engineering plan
- Detailed design: `docs/plan/memory-read-api-and-browser.md`,
  `docs/plan/memory-formation-and-consolidation.md`

## Context

Formation is fully autonomous. Every belief committed without an explicit
owner statement at sensitive or restricted sensitivity is marked
`flagged_for_review`, and the design's safety model is after-the-fact review:
the owner corrects or deletes what formation got wrong. Every email-derived
memory is sensitive by construction, so by 2026-09-22 sixty-eight of the
newest two hundred production memories carried the flag.

ADR-0070 decision 3 kept the memory API read-only and left corrections on the
`agent memory` CLI. The native memory browser therefore shows a "Flagged for
review" label with nothing to do about it, nothing in the codebase ever clears
the flag, and the only in-app removal path is the People correction menu,
which reaches only person-linked facts. The owner asked for review and
deletion from the clients.

## Decisions

1. **Two write routes join the memory router.** `DELETE /v1/memories/{id}`
   removes a belief through the governed delete, which tombstones the
   statement, erases People copies, and blocks re-formation of the same
   statement. `POST /v1/memories/{id}/review` takes one outcome: `dismiss`
   clears the flag; `untrue` retires the belief as never true; `not_here`
   lowers its portability so it stops travelling. The last two are the
   existing rejection kinds; a "was true, has changed" outcome needs
   replacement text and stays on the People correction path and the CLI.
2. **A new exact scope, `memory.write`.** Both routes require it, a bounded
   `Idempotency-Key`, and the caller's ceiling. A belief above the ceiling, in
   another principal's store, or absent is `not_found`, exactly as a read. A
   repeated key replays the recorded result; a reused key with a different
   request is `conflict`.
3. **Dismissal is a governed transition.** It is the only operation that sets
   `flagged_for_review` back to false. It takes a fresh store position, so the
   next recall delta shows the belief as reviewed, and it appends a
   `memory.reviewed` event. It never changes status, confidence, or authority.
4. **The browser can ask for the review queue.** `GET /v1/memories` gains a
   `flagged` filter that composes with every other filter, implemented alike
   in both store adapters and covered by the existing filter and parity gates.
5. **Hard gate 6 is rewritten, not removed.** It now asserts the exact route
   table: two reads with `memory.read` and exactly these two writes with
   `memory.write`, each write declaring the idempotency header. Any other
   route, method, or scope still fails the build. Hard gate 10's vocabulary
   gains `conflict` for the write routes only.
6. **The native client acts on what it shows.** A "Needs review" filter, a
   review menu with Mark reviewed, Not true, Not relevant here, and Delete,
   and swipe-to-delete with confirmation. A server without the routes is
   presented as not supporting memory changes yet, the way browsing already
   degrades. The `agent memory review` CLI command offers the same outcomes.

## Consequences

- The owner principal must be granted `memory.write` in the deployment
  configuration alongside `people.write`; a token without it is refused with
  `authorization_error`.
- The read routes are untouched: same parameters, same projection, same
  error vocabulary. Milestone 17's remaining gates do not change.
- Dismissal leaves an audit trail but no rejection record, so a dismissed
  belief can still be reinforced, superseded, or decayed like any other.
- No Python gate observes the Swift work; Swift model, view-model, transport,
  and fixture tests are its evidence under ADR-0049.
- The historical milestone gate counts do not change.

## Alternatives considered

- **Keep corrections on the CLI:** rejected because the review queue is
  visible only in the clients and the owner cannot act on it there.
- **Route every memory write through the People correction API:** rejected
  because most flagged beliefs are not person-linked.
- **Clear the flag automatically after a period:** rejected because the flag
  records that nobody has looked; time passing is not review.
