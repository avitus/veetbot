# ADR-0317: Gather evidence and stage attachments before drafting email replies

- Status: Accepted — owner approved implementation and automatic staging on 2026-11-11
- Date: 2026-11-11
- Related: ADR-0092, ADR-0104, ADR-0279, ADR-0296, ADR-0305
- Detailed design: `docs/plan/email-experience.md`, `docs/plan/email-integration.md`,
  `docs/plan/calendar-integration.md`, `docs/plan/policy-and-approvals.md`

## Context and explicit divergence

The owner requested substantially better email replies: use the relevant meeting
context, find requested documents in prior correspondence, and attach them to the
reply. Inspection of the production implementation shows that generation makes
one structured model call without tools. It supplies the selected conversation,
style examples and bounded relationship evidence. It does not search other mail
or Calendar. The draft and exact-send contracts carry text but no attachments.
The existence of read tools in Chat therefore does not make them available to
the email drafter.

The engineering plan's Milestone 26 exclusions include attachments and Calendar;
the email design tells the drafter to abstain when an essential attachment is
unread. ADR-0305 authorizes Calendar reads independently, and ADR-0296 authorizes
an explicitly requested attachment save, but neither authorizes automatic draft
attachment staging. This decision explicitly extends those boundaries. It does
not silently reinterpret an approved save as permission for automatic saves,
change the existing save tool's approval floor, or mark a gate complete.

Private correspondence, filenames, participants and production diagnostics are
not included in this decision or repository test fixtures.

## Behavior

Before asking a correspondent for information, gather information that the owner
already has permission to read. For a document request tied to a meeting, resolve
the meeting, find the matching source mail, select the requested documents and
prepare a concise reply with actual files. A question belongs in the draft only
when a consequential ambiguity remains after the bounded search. Provider failure
or exhausted search limits are preparation problems shown to the owner, not a
reason to ask the correspondent to repeat information the assistant has not read.

Use the same preparation service for explicit AI drafting, refinement and eligible
automatic drafts. Preserve manual composition, owner edits, the top-three automatic
draft limit and all current automatic-email cost ceilings. A draft never sends
itself. Research completion must not be represented as a completed external action.

## Mechanism

1. **A bounded research stage in the existing durable task.** A typed planning
   result identifies missing facts, search queries and optional event windows.
   Execute only an allowlist of governed mail reads, relevant memory reads and
   selected-calendar reads. No generic write-capable agent or second queue is
   introduced. Use the ordinary tool pipeline, account identity checks, audit,
   lease, cancellation, deadline and budget accounting. Prepare only servers
   actually needed, including when the selected thread is already cached.
   Respect both existing per-tool/run limits and a new tighter preparation cap:
   two planning rounds, three mail queries, five source threads, two event windows
   and ten tool reads total. Pagination consumes those limits. Incomplete evidence
   remains explicitly incomplete; it is never silently treated as an empty result.
   Model and tool reservations are charged to the originating draft/refresh slice.
2. **Time and relevance are explicit evidence.** Supply the current instant and
   the owner's configured IANA zone. Resolve meetings against source dates,
   organization, participants and the requested period; do not choose an old deck
   just because its title matches. Calendar-disabled or unavailable deployments
   can still establish the meeting from source mail. Search the originating
   account first; another account requires its existing read authority and a
   specific relevance reason. Another account never changes the reply's send
   identity. Unrelated private memory and calendar entries stay out of the reply.
3. **Sources, not generated filenames.** Research returns typed, account-qualified
   source/message identities, dated facts, completeness, and attachment candidates.
   The model selects only candidates supplied by successful reads. Exclude inline
   signature images and unrelated files. Match the requested document set, handle
   superseding versions and duplicate bytes, and preserve unresolved distinctions
   instead of claiming completeness. External text remains untrusted data and
   cannot change recipients, authorize actions or enlarge the research allowlist.
4. **Private draft staging, distinct from Save to Chat.** Add an application-only
   attachment staging operation using the existing bounded first-party Gmail
   fetch. Limit the draft to ten files and 15 MiB aggregate decoded bytes, and
   reject an encoded outbound message above 24 MiB. Download outside model/event
   payloads, validate the source position/name and inspected type, and create
   immutable draft-owned blobs with SHA-256 digests. Reuse the artifact store
   through a typed service; do not expose arbitrary filesystem paths or provider
   credentials. Staging creates neither a Chat message nor knowledge ingestion.
   The owner approved this automatic staging permission on 2026-11-11; ADR-0296's existing Save actions keep their behavior and approval floor.
5. **A versioned attachment manifest.** Extend draft persistence, API and native
   models with attachment id, source identity, filename, inspected media type,
   size and digest. Old drafts deserialize with an empty manifest. Display file
   chips with preview/download and removal before Send. Changing text, recipients
   or attachments creates a new immutable revision and invalidates send consent.
   Preparation commits only if the originating source and draft revisions still
   match; concurrent edits, refreshes and cancellation preserve the owner's work.
   Refinement includes the current draft and preserves its verified files when no
   replacement set is selected; the owner can remove files in the draft editor.
   The generated body may say 'attached' only after every selected blob is staged.
   One-press Send includes the displayed file IDs as a capability acknowledgement;
   an older client that omits them cannot consent to files it does not display.
6. **Send exactly the reviewed bytes.** Bind the existing exact-action approval
   and Undo consent to the complete manifest as well as the envelope and body.
   The application resolves owner-scoped immutable blobs through a protected
   transport boundary; bytes never enter ordinary tool arguments, results,
   checkpoints or approval text. The first-party Gmail sender builds multipart
   MIME from those verified bytes and retains reply headers. Revalidate ownership,
   retention, size and digests before the dispatch claim. Never refetch a possibly
   changed attachment after approval. Missing or changed files refuse dispatch;
   uncertain dispatch remains non-idempotent and cannot be retried automatically.
7. **Retention and erasure follow the draft and source.** Unsent staged files live
   with the retained unsent draft; sent/discarded revisions expire after thirty
   days. Cancelled or failed unreferenced staging is collected after thirty days.
   Source exclusion/erasure removes matching bytes and derived evidence, revokes
   affected proposals and consent, and preserves unrelated attachments. Principal
   erasure includes staging. Atomic reference checks prevent an erasure racing
   staging or dispatch. Filenames receive the same privacy protection as bodies.
8. **Useful failure states.** Preparation reports whether it is finding context,
   finding documents, staging files or ready for review. A missing source,
   conflicting version, permission failure or size limit appears beside the draft
   with the specific unresolved issue. Preserve existing edits and never label a
   text-only partial result as an attachment-complete reply.

## Implementation and acceptance sequence

1. Amend the owning plan/specification and project authorization to record the
   accepted extension, with no milestone completion claim. Settle the protected
   attachment transport and staging permission before changing execution code.
2. Begin with fabricated regression cases: a request omits a meeting date, Calendar
   identifies the current meeting, and another email contains its materials.
   Demonstrate the current missing research and absent attachment manifest before
   implementing the smallest passing research/staging path.
3. Add shared adapter contracts and boundary tests for account isolation, source
   freshness, old-draft compatibility, draft revisions, MIME bytes, authorization,
   cancellation, retry, byte limits and source erasure. Add adversarial cases for
   old versus current decks, misleading filenames, inline images, duplicated files,
   conflicting meetings, inaccessible calendars, partial pages and malicious mail.
4. Exercise automatic and explicitly requested drafts, native attachment review,
   cross-device edits, Undo, stale source rejection, frozen-file integrity and
   uncertain-send behavior. Prove that draft preparation causes no Gmail write and
   does not bypass the existing attachment Save approval or ingest knowledge.
5. Run red/green tests, affected partitions, lint, typing and documentation checks
   in the prescribed local sandboxes. Evaluate representative private drafting
   examples separately from fabricated correctness tests; report retrieval success,
   correct file selection, needless clarification rate, latency and cost.
6. Follow the repository's authorized PR, CodeRabbit, final-head CI and deployment
   process. Verify a real draft's visible attachments against its source before
   calling the feature delivered. A live send requires its own exact-message consent.

## Alternatives

- Prompt wording alone cannot supply unavailable tools or represent file bytes.
- Passing the whole mailbox into the prompt wastes budget, exposes unrelated data
  and provides no freshness or file-integrity guarantee.
- Reusing approval-gated Save to Chat for every automatic candidate introduces
  interruptions and permanent knowledge writes unrelated to preparing a proposal.
- Fetching source attachments only during Send could transmit different bytes from
  the ones reviewed and is therefore rejected.

## Current status

Implemented and locally verified for development delivery. Sending remains under the owner's
exact-message consent. The owner subsequently authorized PR 250, review fixes and
merge after verification. Production delivery and regeneration of the owner's real
reply remain pending. No milestone or production acceptance is claimed.

## Local verification

Verification uses fabricated correspondence and isolated local sandboxes, not a
live send. Linux checks run in the repository's container image; native checks
run in the headless Tart macOS VM.

- Red/green: `circleci testsuite run static --run-tests=all -t
  '^tests/unit/test_email_researched_replies.py$'` first exposed missing research
  and staging capabilities. Later regressions reproduced absent client file
  acknowledgement, dispatch after lease expiry, and treating existing files as
  missing during refinement.
  The final focused run selected and ran five atoms: 186 tests passed.
- The full static sweep selected all 599 atoms and ran 10,286 cases: 10,228 passed
  and three existing root-permission cases skipped. Fifty-four Git-dependent cases
  failed or errored because the new container mounted worktree metadata at the
  wrong path; one Gmail roster assertion still described the pre-ADR-0317 tools.
  After correcting the mount and that expectation, all 170 tests in the ten
  affected atoms passed. The rerun covers all 55 original findings, with none
  unresolved. No production change was needed for the sandbox failures.
- The complete contract partition passed 2,284 tests (200 atoms); the affected
  PostgreSQL integration file passed three tests. These include atomic retention
  scoped to the owner, originating run, target draft and unexpired artifact.
- The full native suite passed 1,204 tests in 103 suites, plus four contrast tests.
  Coverage includes manifest decoding, file removal/autosave and exact file-ID
  acknowledgement on Send.
- Real first-party MCP handler coverage verifies multipart MIME, multiple files,
  Unicode filenames, reply headers and unchanged bytes. Preparation, source
  erasure, concurrent edits, refinement, account authority and expired dispatch
  are exercised without sending mail.
- Lint, full typing and strict documentation checks passed. Citation repairs were
  reviewed; no gate or milestone was marked complete.

Production delivery and representative private drafting evaluation are pending.
Retrieval success, correct document selection, unnecessary-clarification rate,
latency and cost have not yet been measured against the owner's live mailbox.

## Review hardening

PR 250 adds regression coverage for retained files across replacement sessions,
lease expiry during file reads, safe pre-dispatch failure recovery, stale text
edits after attachment removal, legacy consent digests and committed-proposal replay.
Planning receives the configured `AGENT_EMAIL_OWNER_TIMEZONE` before its first call,
or reads selected-calendar metadata first when no explicit zone is configured.
The native client blocks composition switching during Send preparation, binds the
press to the settled revision and displays filenames in recovered approvals.
Further review preserves claimed send states during source erasure, retries retention
cleanup after durably recording a send, and follows source pages within the same
read budget. MIME expansion is checked before staging and consent against the
unchanged 24 MiB encoded-message cap. Exact artifact reuse alone exempts a staging
retry from the existing count/byte limits. Calendar fallback selects only the
remaining allowed windows and records omitted calendars as incomplete evidence.
Native approval matching uses stable attachment identity and content metadata;
a redacted presentation filename does not invalidate the matching draft. The
server still validates the full approved arguments before dispatch. Retention
maintenance defers individual attachment-cleanup failures without blocking other
cleanup records or body expiry. Cached source-page replay reuses its existing
read admission even when the allowance is full; new reads remain bounded.

Review regressions first reproduced ten email failures, five browser visibility/editor
failures, three native issues and the missing timezone setting. The corrected focused
runs passed 403 static tests, 185 contract tests and 200 native tests. The browser
sweep passed 86 of 87 cases; its only failure was an incorrectly added assertion
classifying a remembered-account prompt as a security interstitial. After removing
that incorrect assertion, all 32 tests in its file passed; the real security challenge
retains the explicit evidence assertion. Lint, typing (1,828 sources) and strict
documentation checks passed. These are isolated sandbox results, not live-send evidence.

The second review pass reproduced inactive-reply, source-erasure, cleanup,
pagination, encoded-size admission, staging-count and native selection failures.
The corrected run passed 278 focused static tests, 185 contract tests, 201 native
tests, 61 real-browser tests and two macOS UI journeys (missing draft and Undo
after closing the composer). Lint, full typing and strict documentation checks
passed. Source erasure retains unresolved dispatch ownership; injected retention
failure leaves a successful send recorded and is completed by maintenance.

The third pass reproduced three native regressions (four failed assertions) and
two shared-calendar overflow cases. After repair, all 204 focused native tests
and 45 researched-reply tests passed. The full native sweep then passed 1,209
tests in 103 suites plus four contrast tests. The attachment tests also reject changed
file IDs and digests despite allowing a redacted filename. Lint, typing and
strict documentation checks passed in the isolated sandboxes.

## Protected transport

Two application-only Gmail capabilities keep file bytes outside recorded tool
payloads. `stage_attachment` returns bytes only to the platform interceptor,
which validates the typed email task and source and persists a private draft
artifact. `send_attached_message` accepts only a metadata manifest in the platform
schema. After approval, the interceptor resolves that exact owner-scoped manifest
and supplies base64 bytes only to the private stdio request. The Gmail adapter
checks the sizes and digests and constructs MIME. Neither capability is advertised
to Chat or available to a saved job. Text-only sends keep `send_message` unchanged.
