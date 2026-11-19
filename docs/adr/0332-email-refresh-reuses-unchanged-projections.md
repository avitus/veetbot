# ADR-0332: Email refresh reuses unchanged projections

- Status: Accepted — owner authorized the performance plan on 2026-11-18
- Date: 2026-11-18
- Related: ADR-0318, ADR-0234, ADR-0301; Milestone 26
- Detailed design: `docs/plan/email-experience.md`

## Context

Each foreground status poll reloads every loaded mail-list page and the selected
conversation. Opening a conversation also regenerates its reading text even
when its source and draft are unchanged. The owner requested a performance
plan and directed implementation. Production timing remains to be measured.

## Decision

1. List responses carry an additive opaque `snapshot_version` covering the
   entire ordered, authorized, filtered result and its collection counts, not
   just the returned page. Page size and cursor do not affect it. Feedback,
   attention expiry, mailbox changes and source exclusion remain observable.
   Reading a conversation's access timestamp alone does not change this value.
2. An in-place native refresh checks the first page. When its version matches
   the version of the loaded rows, it retains the rows and pagination without
   reading later pages. A changed or absent version uses ordinary pagination.
   A multi-page load keeps a reusable version only if every page agrees.
   Query, connection and local mutation invalidation also discard the version;
   obsolete requests cannot restore it. Existing bounded list snapshots may
   retain their version alongside their rows. Returning to an unchanged list
   still resumes tracking its pending archive operations.
3. Thread reads support an explicit `If-None-Match` validator. Authorization,
   retention, current draft and operation state are checked before matching.
   The validator covers retained source content and presentation inputs,
   excluding only access time, and includes a reader-format version. A match
   returns 304 without regenerating reading text. Matching honors wildcard and
   weak validators, preserving commas inside quoted tags and rejecting malformed
   separators ([RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.2)).
   Both responses remain
   `private, no-store`; this introduces no HTTP disk cache or offline authority.
4. The native client uses a validator only for its currently displayed thread
   and only during refresh. Selection, connection and list-invalidating owner
   actions discard it. Server draft edits change the validator; unsaved edits
   do not enter it. A 304 keeps the displayed content and unsaved edits. Older
   clients and servers retain their existing behavior.
5. Reading-text preparation runs outside the API event loop and owner lock.
   It performs no network fetch and preserves the existing sanitization and
   explicit image-loading rules.
6. Native message and conversation preparation runs in a detached worker. The view shows
   progress until the parse for its exact messages, revision and account owner
   is available. A nine-entry least-recently-used cache retains one parse per
   thread. Message changes (including retention), exclusion and connection reset
   invalidate it; cancelled or invalidated workers cannot repopulate the cache.
   Each cancelled reader resumes promptly; the worker is cancelled only when
   its final reader leaves. Reader controls belong to the thread outside the
   preparation gate, so an updated account address or message source preserves
   the selected mode, focus, side conversation and expanded content.
7. The first authorized inbox page is published independently of account
   metadata. Account freshness continues loading concurrently; its failures
   remain visible and permission loss still removes inaccessible content.
8. Thread access bookkeeping reuses the record already read under the owner
   lock, avoiding a second full source fetch. If archive recovery writes first,
   it rereads the new revision before updating access. Compare-and-swap writes,
   exact access timestamps and expired-body removal retain their semantics.

## Verification and scope

Use regression-first native request-count tests and server boundary tests for
unchanged results, later-page changes, feedback, retention, draft changes,
authorization, failures and stale reads. Run them in local test sandboxes.
Measure large-mailbox and long-thread workloads before claiming latency targets.
This change introduces no schema, retention-window, milestone or gate changes.
A persisted indexed list projection and separate access-time column are not
part of this decision: the synthetic cold-list profile is dominated by the
in-memory store's copying and scans, and does not establish PostgreSQL query
cost. Such schema work requires production-shaped database measurements first.
The existing summary projection and cache remain authoritative.

## Local evidence (2026-11-18)

- The new server regressions first failed for the absent list version and
  thread validator, and for reader work executing on the API event-loop thread.
  The native regressions first observed three page reads instead of one and
  no conditional thread header. Setup failures were excluded from red evidence.
- The final server partition passed 209 regressions in 85.26 seconds, including
  API authorization, collection counts, archive recovery, retention, reader
  sanitization, snapshot invalidation and scan-cost coverage. The shared
  in-memory store contract passed nine cases in 6.94 seconds. Full lint and
  typing passed (1,854 source files); documentation and citation checks passed.
- The final Apple view-model, conversation, reading and inspector partition
  passed 196 tests in seven suites in 54.19 seconds in the headless Tart VM.
  Its incremental build took 35.08 seconds; the VM runner took 138.31 seconds
  including startup and transfer. Coverage includes early row publication,
  late account failures, overlapping pagination, LRU eviction, off-main work,
  cancellation, exclusion and connection reset. All regressions listed below
  pass, and no existing assertion was weakened.
- A synthetic in-process run in the Linux test VM held 10,000 threads and
  simulated 1,800 loaded rows. Across three samples, the previous request
  pattern (18 list pages and a full thread) took a median 3,479 ms; one page
  plus a validated unchanged thread took 177 ms. The unchanged thread returned
  zero body bytes. Both patterns used the current server code; these numbers
  measure request-work reduction, not a before/after production release.
  The first cold list took 1,160 ms, so the proposed cold-load target is not
  established. Native request-count cases cover both old and new servers.

Production end-to-end timing and cold-list latency acceptance remain unmeasured.
This evidence does not complete Milestone 26 or establish the proposed p95
latency targets. The local native worker tests establish UI-thread separation
and invalidation behavior, not an installed-client rendering benchmark.

Regression-first evidence for the remaining latency changes:

| Regression | Observed failure before the fix |
| --- | --- |
| `test_thread_read_fetches_source_payload_only_once` | Two full source reads instead of one |
| `testCacheEvictsOnlyTheLeastRecentlyUsedConversation` | Filling the cache discarded recently used entries |
| `testInboxRowsAppearBeforeSlowAccountMetadata` | Rows stayed hidden and the initial loader remained active |
| `testConversationPreparationRunsOffTheMainThread` | The builder observed `Thread.isMainThread == true` |
| `testLateAccountsDoNotFinishAnOverlappingPageLoad` | Account completion cleared the newer page's loading indicator |

PR review hardening also reproduced blocked cancelled readers, a reset native
mode after owner-address preparation, and missing HTTP wildcard/weak matches.
`testCancellationReleasesReaderBeforeWorkerFinishes` covers one and two readers;
`testPreparationPreservesConversationMode` exercises the actual native picker.
The HTTP boundary cases retain authorization checks for wildcard requests and
reject malformed tag separators. The final focused API partition passed 83
cases; the expanded native partition passed 198 tests in seven suites. Lint,
typing and documentation checks passed in the same local sandboxes.

The hide/reopen regression now explicitly holds the initial list response,
keeping every existing assertion. Holding account metadata alone no longer
establishes that the independently published rows arrived after hiding.

Reading lane: **A**; diff-derived minimum: **A**. No engineering-plan requirement
or acceptance gate changed. The implementation adds ADR-0332 and updates the
email design, ADR navigation and affected citation pointers. No milestone is
marked complete and no production latency claim is made.

Reproduction commands (from the checkout, in the local sandboxes):

```sh
DOCKER_CONTEXT=colima-veetbot-adr0317 scripts/run_in_linux_container.sh \
  'UV_NO_SYNC=1 PYTHONPATH=src make test-static PYTEST_ARGS="tests/unit/test_email_projection_versions.py -q"'
scripts/run_apple_ui_macos_vm.sh swift test --package-path clients/apple \
  --scratch-path /Users/admin/email-speed-build \
  --filter 'EmailViewModelTests|EmailConversation|EmailReadingTests|InspectorSurfaceStructureTests'
```

The Linux runner used its installed dependencies because the sandbox's DNS
could not reach the package index. CircleCI CLI was unavailable there, so the
repository test targets were used directly. The Apple run used a fresh build
scratch directory after the shared build database proved corrupt. Neither
setup issue was counted as a behavioral failure or as performance evidence.
A disposable PostgreSQL image could not be pulled because sandbox DNS was
unavailable. Persistence adapters and schema are unchanged; this run does not
claim new PostgreSQL integration evidence.
