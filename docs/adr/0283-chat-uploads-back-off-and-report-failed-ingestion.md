# ADR-0283: Chat uploads back off between ingest attempts and report a failed ingestion

- Status: Accepted (owner direction on 2026-11-01)
- Date: 2026-11-01
- Related: ADR-0120, ADR-0281, ADR-0282
- Detailed design: `docs/plan/knowledge-documents.md`

## Context

On 2026-10-23 the owner sent his car's 424-page owner's manual in a chat and
asked that it be kept for future questions. The upload's record says its
automatic ingestion failed after three attempts with the reason `transient`.
The three attempts ran 15 and 17 seconds apart, within 32 seconds of the
upload: a cause that was transient had no time to clear, and a cause that was
not got no diagnosis, because the warning recorded the attempt number and
nothing about the error. Postgres logged no error in that window, and the
same bytes ingest in three seconds against a scratch database with the same
code, so the cause is specific to the production sequence or environment and
the log cannot say which.

The chat meanwhile told the owner the manual was "available and searchable".
The attachment marker knows two knowledge states, pending and ingested; a
failed or refused ingestion rendered no note at all, so the model had no way
to tell the owner the file was not indexed, and nothing let the owner ask for
another attempt short of sending the file again.

## Decision

1. **Attempts back off.** A failed attempt records when the next may run: one
   minute after the first failure, fifteen minutes after the second. The
   sweep skips an upload whose backoff has not elapsed. The third failure
   still marks the upload failed; the `auto_ingest_not_before` key is cleared
   with the other attempt bookkeeping when ingestion succeeds.
2. **The warning names the error class.** `upload_knowledge_ingest_failed`
   carries `error_class` beside the artifact id and attempt number. The
   message stays out of the log, since it can carry document text.
3. **The model is told.** The knowledge states gain `failed` and `refused`,
   and the attachment marker renders a note for each: the file could not be
   added to knowledge, `knowledge.search` will not find it, tell the owner it
   was not indexed, and read the attachment itself when a question needs it.
4. **The owner can retry.** `agent knowledge retry <artifact id>` returns a
   failed or refused upload to pending with a fresh attempt count; the
   maintenance sweep takes it from there and records the outcome as before.

## Consequences

- The manual's next attempt in production will log the class of what fails,
  which is the diagnosis this incident lacked; if it succeeds, the manual is
  back.
- A chat can no longer claim an unindexed file is searchable: the marker says
  the opposite.
- Uploads keep their one-document-per-content identity and every other
  ingestion rule of ADR-0120.

## Verification

- `tests/unit/test_upload_auto_ingest_failures.py`: the retries wait one and
  fifteen minutes, the warning carries the error class and not the message,
  and success clears the backoff.
- `tests/unit/test_model_attachments_adr0120.py`: failed and refused files
  render the new notes.
- `tests/unit/test_memory_review_cli.py`: the retry command queues the upload
  again.
