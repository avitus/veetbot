# ADR-0214: Memory corrections, evidence excerpts and review counts over HTTP

- Status: Accepted (owner, 2026-10-20: "implement all 4")
- Date: 2026-10-19
- Related: ADR-0070, ADR-0117, ADR-0125, ADR-0189; Sections 10 and 22 of the
  engineering plan
- Would amend: ADR-0117 decisions 1 and 5
- Detailed design: `docs/plan/memory-read-api-and-browser.md`,
  `docs/plan/memory-formation-and-consolidation.md`

## Context

The A2 redesign of the Apple memory browser asks for three things the server
does not offer:

1. **Corrected text.** "Fix the wording" on a flagged memory and "Edit" on any
   memory both produce a replacement statement. ADR-0117 admitted exactly two
   writes and left "was true, has changed" on the People correction path and
   the CLI (memory-read-api-and-browser.md:55-61,
   memory-read-api-and-browser.md:207-217); hard gate 6 pins that route table
   (memory-read-api-and-browser.md:514-519). `MemoryService.edit` overwrites a
   statement in place and is CLI-only, which the formation design forbids for
   every other path: "Never overwrite" (memory-formation-and-consolidation.md:218-219)
   and "Rejections are events, not edits"
   (memory-formation-and-consolidation.md:345). The correction table already
   defines the right write: "Was true, has changed" supersedes the belief with
   a new belief at user authority (memory-formation-and-consolidation.md:336),
   implemented as `reject(..., RejectionKind.CHANGED, replacement_statement=...)`.
   `POST /v1/people/{id}/corrections` reaches only person-linked beliefs.
2. **Evidence quotes.** Each memory row shows "quote — source · when". A
   `MemoryView` carries provenance as `source_session_id` and
   `source_event_ids` only; `evidence_count` is deliberately withheld
   (memory-read-api-and-browser.md:301). ADR-0189 governs recovering source
   text for model recall: restricted, owner-private, digest-bound, fenced on
   any change, no backfill, and gated on ablation evidence.
3. **Counts.** The Mac sidebar shows a number beside Needs review, All, each
   belief type, and Synthesis. No route returns a count.

The A2 client already ships what needs no server change: the sidebar groups
without numbers, "Looks right" mapped to `dismiss`, and Not true, Not relevant
here and Delete on the existing writes. Edit, Fix the wording, quotes and
counts are held for this decision.

Two defects in the existing `CHANGED` path matter to any HTTP use of it:
`_replace_rejected` in `src/agent_core/memory/formation.py` commits the
rejection and the replacement in separate units of work even when the caller
passes `existing_uow`, so a crash between them leaves a superseded belief with
no replacement; and the replacement cites the old belief's
`source_event_ids`, which never said the new text.

## Decision

Three parts; the owner can accept each separately.

### A. A `changed` review outcome that supersedes, never overwrites

`MemoryReviewOutcome` gains `changed`. `POST /v1/memories/{id}/review` accepts
`{"outcome": "changed", "replacement_statement": str, "expected_updated_at":
datetime}`; `replacement_statement` is required for `changed` and forbidden for
every other outcome, and `expected_updated_at` is optional for the existing
outcomes so current clients keep working.

- **Authority and visibility are unchanged.** `memory.write`, a bounded
  `Idempotency-Key`, and `ceiling`, with ADR-0117's `not_found` rule for
  above-ceiling, foreign and absent beliefs. A repeated key replays; a reused
  key with a different body is `conflict`.
- **Revision check.** A mismatch between `expected_updated_at` and the
  belief's current `updated_at` is `conflict`, and nothing changes. Every
  governed transition that alters what the owner saw (reinforcement,
  refinement, review, decay) must advance `updated_at`; a gate case observes it.
- **Content checks before any write.** The text is 1 to 500 characters after
  trimming, differs from the current statement after case folding, and passes
  `contains_secret_material` and `contains_memory_injection`. A refusal is
  `malformed_request` and leaves the original untouched. A replacement whose
  hash matches a tombstoned or rejected statement for the same principal is
  refused the same way, so Edit cannot resurrect what Delete or Not true removed.
- **Supersession, atomically.** One unit of work: appends a
  `memory.owner_correction` event, actor the authenticated principal, to the
  old belief's source session, carrying the replacement text; runs the
  existing `CHANGED` rejection (status `superseded`, `valid_to = now`, flag
  kept, rejection record with the old statement's hash); forms the replacement
  through `remember` at `USER` authority and origin trust, `explicit=True`,
  inheriting subject, scope, type, portability and sensitivity, citing the
  new event rather than the old evidence; links `superseded_by` and the
  rejection's `replacement_id`; and records the idempotency result.
  Re-derivation replays the rejection, so the old wording cannot re-form.
- **Scope of the outcome.** Any live original belief, flagged or not. A
  derived summary (`record_kind=summary`) refuses `changed` with
  `malformed_request`; its corrections belong to `memory-reconsolidation.md`.
- **Response.** The replacement's `MemoryView`. The client navigates to it;
  the superseded belief stays readable with `status=superseded`.

`MemoryService.edit` stays CLI-only and is not exposed. "Fix the wording" and
"Edit" are the same request; the client differs only in where it offers it.

### B. A read-only evidence route

`GET /v1/memories/{id}/evidence?ceiling=` under `memory.read` returns at most
five items, newest first, one per cited source event:
`{source_kind, session_id, event_sequence, evidence_at, excerpt, withheld}`.
`source_kind` is a closed set (`owner`, `email`, `sms`, `call`, `scheduler`,
`tool`, `unknown`); `withheld` is null or one of `erased`, `ceiling`,
`not_owner_text`, `scope`, `hazard`.

- An `excerpt` (at most 280 characters, cut at a word boundary, from the
  start of the cited text) is returned only for the owner's own authenticated
  `user.message.created` or `people.owner_assertion` event, in a session the
  caller can read, when the caller also holds `session.read`, the request's
  ceiling is `restricted`, and the text passes the secret and injection scans.
- Email, SMS, call, web and tool sources return navigation identifiers only,
  exactly like `PeopleEvidenceView`, and never their text. Reading them never
  touches a mailbox and never resets an email body's retention timer.
- Erased sessions, sources covered by a People erasure receipt, and events
  past retention return `withheld` with no identifiers beyond the reference.
- Responses carry `Cache-Control: private, no-store`. Excerpts are never
  written to a trace, index, cache or context.

The route follows the People evidence pattern rather than ADR-0189. ADR-0189
exists to put source text in front of the model, so it is restricted,
digest-bound, fenced on any sibling change, has no backfill, and is gated on
recall ablation; under it almost every existing memory would show nothing,
and display would inherit an activation gate about recall quality. Showing
the owner their own words, which `session.read` already lets them open in
the conversation, needs none of that. The excerpt never reaches the model.

### C. A counts route

`GET /v1/memories/counts?ceiling=&include_derived=` under `memory.read`
returns `{live, flagged, by_status, by_type, synthesis, counted_at}`. `live`
and `by_type` count `active` and `provisional`, matching the default browse;
`flagged` counts live beliefs with `flagged_for_review`, matching
`flagged=true`; `synthesis` is null unless `include_derived=true` and the
reconsolidation surface is enabled. Every count applies the same principal,
tenant and ceiling predicates as the list, in both store adapters.

A summary route is chosen over a `total` on the page: the sidebar needs about
eight numbers, which is one request instead of eight; a total would make every
page pay a count; and a total read beside a keyset cursor drifts as beliefs
change between pages. It is mounted before `/v1/memories/{memory_id}`, as
`/v1/people/merge-suggestions` already is before the person route.

## Consequences

- Owners can correct any memory from the clients without a conversation, and
  every correction leaves the superseded belief, a rejection record and an
  owner event, so the audit trail matches a Chat correction.
- The CHANGED path becomes atomic for the CLI and People callers too.
- Counts and evidence add read load proportional to the owner's store; both
  stay inside the existing per-request timeouts.
- No security requirement is relaxed: writes keep ADR-0117's scope,
  idempotency and ceiling rules; external content stays untrusted and is never
  shown as text; the owner's own words are shown only at `restricted`.

## What changes if accepted

- Routes: `review` gains `changed` (part A); `GET /v1/memories/{id}/evidence`
  (B); `GET /v1/memories/counts` (C). No new scope.
- Gates: hard gate 6's table in `tests/gates/test_memory_read_api_m17.py`
  gains the two reads under `memory.read`; new cases for supersession
  atomicity, revision conflict, secret and tombstone refusal, evidence
  withholding by kind, ceiling and erasure, count parity with the list filters
  in both adapters, and route ordering. Hard gate 10 needs no new code.
- Specs: in the memory read design, scope item 1
  (memory-read-api-and-browser.md:55-61), the exclusions paragraph
  (memory-read-api-and-browser.md:87-90), the derived-routes sentence
  (memory-read-api-and-browser.md:179), *Writes*
  (memory-read-api-and-browser.md:207-217) and gate 6
  (memory-read-api-and-browser.md:514-519); `evidence_count` stays withheld
  (memory-read-api-and-browser.md:301). The formation correction section
  (memory-formation-and-consolidation.md:325-345) gains the HTTP door and the
  atomicity rule.
- Client: Edit and Fix the wording sheets, quote rows with withheld states,
  sidebar numbers; older servers degrade as ADR-0117 decision 6 describes.

## Alternatives considered

- **Expose `MemoryService.edit`.** Rejected: an in-place overwrite loses the
  old statement's history and lets re-derivation re-form it.
- **Route every correction through People.** Rejected in ADR-0117: most
  beliefs are not person-linked.
- **Evidence through ADR-0189 recovery.** Weighed above; its constraints serve
  model context, not owner display.
- **`total` on each list page.** Weighed above.
