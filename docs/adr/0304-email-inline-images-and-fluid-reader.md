# ADR-0304: Click-to-load email images and a fluid Mac reader

- Status: Accepted (owner request and image-loading choice, 2026-11-06)
- Date: 2026-11-06
- Amends: Email experience's external-image prohibition
- Related: ADR-0237, ADR-0276, ADR-0301; Milestone 26

## Decision

The owner requested inline images and a Mac reading panel that uses large
windows. External images load only after the owner presses **Load images**
for that message. There is no automatic load or remembered sender permission.

Gmail retains a bounded image-bearing HTML part separately from the existing
plain-text body. It is presentation source only: it expires and is erased with
the message, and is excluded from content identity and model evidence. The
thread API sanitizes it into an additional reading projection. It never sends
raw HTML to a WebView. Older retained messages without this part keep their
existing reader until the mailbox source is read again. When the HTML alternative
contains only images, the projection keeps the plain-text body before those images.

Only explicit public HTTPS image references survive sanitization. Hidden images,
images declared at two pixels or smaller, active content, inline data and CID
references are omitted. A visible image can still be a tracker; the loading
control explains that loading can disclose that the message was opened.

The scoped image endpoint accepts a retained message identity and image index,
never a caller-provided URL. It rechecks source retention and uses the existing
credential-free, DNS-pinned public image reader with its byte, pixel, redirect
and time limits. The process-wide email service admits at most four concurrent
image fetches, releasing the slot on success, failure or cancellation. Images are returned as bounded JPEG renditions, with no-store
responses, and are not saved as artifacts or used for learning. Failures leave
the message readable and offer an explicit retry. There is no automatic retry.

The Mac reader uses the width left by its neighboring columns with fixed edge
padding, without the old 680-point ceiling. iPhone and iPad retain their current
reading widths. Conversation alignment, folding and the brief remain intact.

## Limits

This first increment renders externally hosted JPEG, PNG and WebP images.
Embedded attachment/CID images and GIFs are not supported. No milestone or
acceptance gate is moved. Production use requires server and Mac app delivery.

## Verification (2026-11-06)

Reading lane A; the diff-derived minimum is A (API, domain/runtime and plan
changes). No milestone gate is advanced. This ADR records the owner's explicit
amendment to external-image loading; there is no other plan deviation.

The implementation spans the Gmail sync presentation field, retained message
and expiry rules, the scoped image route and service, the existing public image
adapter, and the Mac image renderer, message models, conversation and reader
layout. The email, HTTP and Apple design documents and citation ledger are
updated with it. No dependency or database migration is added.

Regression evidence:

- `scripts/run_in_linux_container.sh 'uv run pytest tests/unit/test_email_reader.py tests/unit/test_gmail_client_transport.py -q'`
  initially failed on the missing image projection and retained HTML, with
  54 existing tests passing. Its expanded final cases passed (60 tests).
- `scripts/run_apple_ui_macos_vm.sh swift test --package-path clients/apple --filter EmailSurfaceStructureTests/testMacReaderHasNoFixedWidthCeiling`
  initially failed on the 680-point cap. The final rendered AppKit test also
  verifies an editor wider than 900 points at a 2200-point window and its
  contraction when that window narrows to 1200 points.
- The native image-position test initially failed because the conversation
  omitted the image marker. The final Email partition exercised 235 tests;
  234 passed, and one new request test exposed an empty test fixture. After
  adding its synthetic message, that focused test passed, including no
  automatic image request, no automatic failure retry, and explicit recovery.
  The rendered image-bearing view also makes zero requests before a press.
- The server/API/Gmail/image partition passed 184 tests in 41.52 seconds.
  Source import, learning and runtime contracts passed 45 tests in 12.30
  seconds; import-boundary and reading-lane checks passed 27 in 29.20 seconds.
  Image adapter tests passed 10 in 8.09 seconds. These cover owner/source
  checks, expiry, hostile references, bounds, failure and retry, and unchanged
  source identity when image presentation becomes available.
- Repository lint, typechecking and documentation checks passed. Citation
  updates were reviewed and contain only line-offset adjustments. The Linux
  sandbox lacked the CircleCI CLI, so repository pytest commands ran there.
  A damaged Apple VM build cache was cleared inside the guest before rerunning.

Timing before submission: the final lint/typecheck/image-adapter stage took
17.05 seconds wall time; source checks took 16.71 seconds and architecture
checks 32.72 seconds including container startup. The Mac partition's test
execution took 44.80 seconds. Estimated final verification wall time was eight
minutes, with Linux checks overlapping the Mac VM; Mac startup and compilation
were the slowest stage (approximately three minutes). These are local sandbox
results, not hosted CI or production delivery evidence.

Before the release PR, `main`'s Mac scrolling changes were merged and this ADR
renumbered to 0304. The combined Email partition passed all 238 tests in 48.423
seconds, including the corrected image-request fixture. Documentation checks
passed after resolving citation offsets. Estimated merge-verification wall time
was 142 seconds for the Apple VM and 15 seconds for documentation, overlapping;
total approximately 142 seconds, with Apple startup/build the slowest stage.

PR review follow-up preserves plain MIME text when the HTML alternative contains
only images and limits the shared email service to four concurrent image fetches.
The two new regressions first failed on missing text and 12 simultaneous fetches:
`scripts/run_in_linux_container.sh 'uv run pytest tests/unit/test_email_reader.py -q -k "image_only_html or image_fetch_concurrency"'`.
The reader, Gmail transport, image adapter and email service partition then passed
112 tests in 17.86 seconds, including slot release after failure. Lint, typecheck
(1787 source files), citation repair and documentation checks passed. Estimated
follow-up wall time was three minutes, with approximately one minute of container
startup, 18 seconds of tests and 20 seconds of static/documentation checks;
these stages ran serially with no overlap. Container startup was the slowest stage.
