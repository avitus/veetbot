# ADR-0273: Inspect one public image in chat

- Status: Accepted by the repository owner on 2026-10-31
- Date: 2026-10-31
- Extends: ADR-0054 and ADR-0120

## Context

A SmugMug photo permalink led the web reader to a JPEG. The reader returned a
caption, and the offline sandbox could not download the file. The owner wants
the assistant to inspect the pixels from a public link. ADR-0120 decision 4
currently requires every image reference in a tool result to render as a
text marker.

## Decision

Add a read-only tool named `web.inspect_image`. For a SmugMug photo permalink, read the page’s unique
Open Graph image. For other gallery pages use the existing web reader to identify
the selected image first. If several
images are listed and the selection cannot be established, ask which one the
owner means. The tool also takes a direct HTTPS image URL.

Fetch through a credential-free broker. Validate the hostname and every DNS
answer, dial a checked public address, and verify TLS against the requested
hostname. Reapply those checks to each redirect. Bound redirects, elapsed
time, transferred bytes and decoded pixels. Validate the decoded image before
using Pillow to decode JPEG, PNG or WebP and
storing a bounded JPEG rendition as an external-untrusted artifact.

Permit a model adapter to read that rendition only when the platform can tie
it to the successful inspection invocation, caller, session and run. Apply
the existing vision capability and attachment budgets. Preserve tool-result
authorship when sending the image. A tool-supplied artifact identifier alone
confers no read authority. Return an explicit failure when the image cannot
be inspected.

## Acceptance

Exercise the gallery-to-JPEG flow through a model request and assert that it
contains the selected pixels. Cover ambiguous selection, invalid image bytes,
private DNS answers and redirects, oversized images, unsupported models,
cross-session artifact substitution and replay of the same invocation.
