# ADR-0118: Files attached to chat messages

- Status: Accepted (authorized by the repository owner, 2026-09-23)
- Date: 2026-09-23
- Related: ADR-0028, ADR-0030, ADR-0050, ADR-0117; Sections 15, 16, 18, and
  22 of the engineering plan
- Detailed design: `docs/plan/http-api-and-streaming.md`,
  `docs/plan/sandbox-isolation.md`, `docs/plan/model-gateway.md`,
  `docs/plan/context-engine.md`, `docs/plan/knowledge-documents.md`

## Context

The owner asked to drag files into the chat. The wire format has carried
`image` and `file` content blocks since Milestone 5, but nothing could put a
file behind one: the Milestone 5 baseline stated that an artifact is not
uploaded through the API in 0.1, the submit handler never checked the
artifact a block names, and every model adapter raised on an artifact
reference. The last point was also a latent failure: the tool executor puts
a file reference into a truncated tool result, and the next model call
refused the whole history as invalid.

## Decisions

1. **One upload route, behind a flag.** `POST /v1/sessions/{session_id}/artifacts`
   takes the file as the raw request body, its media type as `Content-Type`,
   its percent-encoded UTF-8 name as `X-Filename`, and a required bounded
   `Idempotency-Key`. It requires the existing exact scope `artifact.write`,
   so the scope vocabulary does not grow. It is mounted only when
   `AGENT_ATTACHMENT_UPLOADS_ENABLED` is set, so the historical route census
   is unchanged while the flag is off. The body limit on this route alone is
   32 MiB; every other route keeps 1 MiB, and the reverse proxy raises its
   limit for this path only. A new upload is `201` with the `ArtifactView`; a
   replayed key with the same body is `200` with the same view, and a reused
   key with a different body is `conflict`.
2. **An upload belongs to the session before any run exists.** Its artifact
   has origin `upload`, trust `EXTERNAL_UNTRUSTED`, the session's id, no run
   id, and an expiry 24 hours out. `artifacts.run_id` becomes nullable, and a
   check constraint allows a null run only for `upload` and
   `knowledge_source` artifacts. The server detects the stored type from the
   bytes: an image signature (PNG, JPEG, GIF, WebP) is an image, `%PDF-` is a
   PDF with its page count, valid UTF-8 declared as text is text, and
   everything else is kept as a reference. A file that cannot be parsed is
   stored as a reference rather than refused.
3. **Sending the message claims the upload.** Step 5 of the submit handler
   validates each image or file block: the artifact must be an upload of the
   caller, in the same session, and not expired, or the block is
   `not_found`. An `image` block on something that is not an image, an
   invalid `detail`, or more than ten attachments is `malformed_request`. The
   stored parts are rebuilt from the artifact row, so the recorded media
   type, filename, size, and page count are the server's, never the
   client's. After the run is enqueued, the upload takes that run's id and
   loses its expiry: a sent attachment lives as long as the conversation, and
   deleting the conversation deletes it. An upload that is never sent is
   swept after 24 hours.
4. **The model sees what the owner attached, within a budget.** A new
   `AttachmentResolver` port, built in the composition root, lets the model
   adapters read attached bytes after checking owner, session, and expiry.
   Only references in owner-written user messages are resolved. Each
   attachment is preceded by a one-line label naming the file, its type, its
   size, and its artifact id. Images go to the provider as images, PDFs as
   native documents, and text inline inside the same untrusted-content
   envelope the context engine uses for retrieved text. Any other type, a
   provider without the capability, an attachment over its per-item limit or
   past the per-request budget (newest first), an expired upload, and any
   reference in a tool result, assistant message, or system message becomes
   a one-line text marker. A reference never fails a model call again. The
   token estimator counts the attachments the adapter will send, from the
   same selection, so budgets stay honest.
5. **Owner-sent documents are also added to knowledge.** A text, Markdown, or
   PDF attachment on a message the owner wrote is marked for ingestion when
   the owner holds `knowledge.write`. The maintenance worker ingests it
   through the existing knowledge service with `USER` origin trust: the
   owner sending the file is the human admission the knowledge design
   requires. The file itself stays `EXTERNAL_UNTRUSTED`, and ingestion keeps
   the secret scan. The document takes the file name as its title, principal
   visibility, and an id derived from the content hash, so the same file sent
   twice becomes one document. A refused or failed ingestion is recorded on
   the artifact with a reason code and never fails the conversation. PDF text
   comes from a new PDF extractor adapter using `pypdf`, the one new
   dependency.
6. **The native client attaches by drag, by file picker, and from Photos.**
   macOS and iPad accept drops on the chat and its composer; every device has
   a paperclip for Files and, on iOS, the photo library. Each file uploads as
   soon as it is added, shows its progress, and can be removed or retried. A
   message may be attachments alone. Images are downscaled to a 2000-pixel
   long edge and re-encoded as JPEG before upload, which also removes their
   location metadata.

## Consequences

- Production must set `AGENT_ATTACHMENT_UPLOADS_ENABLED`, grant
  `artifact.write` and `knowledge.write` to the owner principal's
  `AUTH_SCOPES`, and deploy the proxy change before uploads over 1 MiB work.
- The Milestone 5 route baseline, the scope count, and the CLI's twelve
  commands are unchanged; the upload has no CLI command.
- `pypdf` is a runtime dependency used only by adapters.
- A document the owner sends becomes retrievable from every later chat until
  the chat it came from is deleted.
- The data-model deviation from Section 15 (a nullable `run_id`) is limited
  to unclaimed uploads and knowledge sources and is enforced by a constraint.

## Alternatives considered

- **Multipart form uploads:** rejected; a raw body needs no parser and streams
  straight to the artifact store.
- **Staging uploads outside the artifacts table:** rejected; the orphan sweep
  and session deletion already govern artifact rows, and a second store would
  need both again.
- **A synthetic run per upload:** rejected; it breaks the one-active-run rule,
  cost accounting, and the erasure fences.
- **Ingesting at upload time:** rejected; a file removed from the composer
  before sending would still reach knowledge.
- **Provider file APIs:** deferred; requests are sent with `store: false` and
  a provider-held copy would need its own retention and deletion.
- **A new `artifact.upload` scope:** rejected; `artifact.write` already names
  writing an artifact.
