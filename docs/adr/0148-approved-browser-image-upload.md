# ADR-0148: Approved rich-text input and image upload

- Status: Proposed
- Date: 2026-10-02
- Related: ADR-0058, ADR-0120, ADR-0129, ADR-0140; engineering plan Section 33
- Detailed design: `docs/plan/browser-automation.md`

## Context

The owner reported that an approved typing action on an editable post composer
failed with `tool.browser.action_not_allowed`, and explicitly requested image
posting too. The runtime accepts only native text fields, despite the approval
view describing editable fields. Browser file transfer has no classified
contract and is deliberately unavailable until one exists.

## Decisions

1. An individually approved `browser.act` type action may fill a live
   contenteditable element. Password and one-time-code restrictions, exact
   revision and element binding, origin confinement and uncertain-write handling
   still apply. Standing and task grants still cannot type into editable regions.
2. Add `browser.upload` as a serial builtin browser capability, classified
   `EXTERNAL_WRITE`, `HIGH`, `NON_IDEMPOTENT`, requiring `artifact.read`. Each
   invocation needs ordinary approval; no standing or task grant covers it.
   It takes an image artifact UUID, the current page revision and an opaque
   element reference. It takes no path, URL, bytes, script or selector.
3. Resolve one PNG, JPEG or WebP of at most 5 MiB from the calling conversation,
   using the existing principal-, session-, origin-, expiry- and checksum-bound
   image resolver. Only claimed chat uploads and generated/exported artifacts
   qualify. Validate the image signature before dispatch. Use a platform-owned
   filename derived from its artifact UUID and media type; never a host path.
4. The target is either an observed native file input or an observed visible
   control whose approved click opens a native file chooser. Only a file input
   in the current main document may receive bytes. Supply an in-memory file
   payload to Playwright, never a filesystem path. A chooser timeout or any
   failure after the click or file-setting operation is uncertain and is not
   retried automatically. Upload does not click a publish control afterward.
5. Approval identifies the image UUID and the observed destination and control.
   Image bytes are resolved only after approval and stay outside tool arguments,
   events, results and logs. A site may send the file as soon as it is selected;
   upload approval authorizes that transfer. Publication remains a separate
   action with its existing approval requirement.
6. Extend the hosted session service with an authenticated, sequence-bound
   upload route. Its JSON envelope alone accepts up to 7 MiB, sufficient for
   one base64-encoded 5 MiB image and bounded metadata. Authentication precedes
   buffering. Decode and validate the payload inside the isolated service.
   Other control-plane routes keep their 64 KiB ceiling. Lease scope, expiry,
   revocation, exact sequence and uncertainty protections apply to upload.
7. Register upload only with a browser provider that implements it. Profile-bound
   chats advertise it with the other browser tools. Existing pinned sessions
   keep their old roster and require a new chat to discover the capability.

## Validation

Real Chromium regressions cover approved rich-text input, input events and no
implicit publication, stale references, credential refusals, and unchanged grant
exclusions. Upload coverage includes native inputs and hidden inputs reached by
an observed chooser control, exact bytes, stale references, invalid images,
scope and size failures, approval classification, hosted authentication and
body limits, sequence replay and uncertain dispatch. The existing browser
isolation and grant suites and the complete repository check remain required.

## Limits

This is image transfer only: no arbitrary files, video, downloads, clipboard,
server paths, external image URLs or credential material. A website can reject
the selected format or size. Tests use controlled pages and do not publish to
an external account. No engineering-plan acceptance criterion or grant exclusion
is weakened; this supplies the separate file-transfer contract Section 33
reserves.
