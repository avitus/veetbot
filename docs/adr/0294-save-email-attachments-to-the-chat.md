# ADR-0294: Save an email attachment into the email's chat

- Status: Accepted (authorized by the repository owner, 2026-11-03)
- Date: 2026-11-03
- Related: ADR-0071, ADR-0092, ADR-0104, ADR-0120, ADR-0122, ADR-0284, ADR-0291;
  Milestone 26
- Detailed design: `docs/plan/email-integration.md`, `docs/plan/email-experience.md`,
  `docs/plan/knowledge-documents.md`, `docs/plan/http-api-and-streaming.md`,
  `docs/plan/policy-and-approvals.md`, `docs/apple-client.md`

## Context

The owner asked to download email attachments and add them to Veetbot's file
storage. Email names an attachment and says it stays in Gmail;
`email-integration.md` listed attachment download as out of scope.

Veetbot has no account-wide file store. Every artifact belongs to one
conversation, and the Artifacts tab is derived from that conversation's messages.
Offered that against a new account-wide store, the owner chose on 2026-11-03:

1. the file lives in the email's chat;
2. both a Save button in Email and a Chat tool that asks for approval;
3. a saved text, Markdown, or PDF file also becomes searchable knowledge on every
   save.

Bytes cannot take the ordinary tool path. A tool result is recorded in the event
log and the invocation row and is capped at 4 MiB, so a base64 attachment would
either be truncated or written into append-only history.

## Decisions

1. **Two tools on the read server, one implementation.** `fetch_attachment` is
   application-only and serves the Save button. `save_attachment` is model-visible
   and serves Chat. Both take `message_id`, `attachment_index`, and `filename`. The
   index is the attachment's position in the message's listed attachments, so
   threads cached before this change work without a migration. Gmail issues a new
   attachment id on each read, so the id is never stored. A name that does not
   match the indexed part is `gmail.attachment_not_found`, which also lets the
   approver read the file's real name in the call.
2. **32 MiB, the upload limit.** A declared or decoded size above it is
   `gmail.attachment_too_large`. The larger upstream bound applies to this one
   request; every other Gmail read keeps 8 MiB.
3. **The bytes stop at the runtime.** Both tools return the file only in
   structured content, never duplicated as text. The MCP runtime hands those two
   calls to an `EmailAttachmentSaver`, which stores the bytes and returns a small
   receipt, so no event, invocation, or checkpoint sees them. Without a saver the
   call is refused. A job's MCP grant reaches neither: `fetch_attachment` is not
   model-visible and `save_attachment` needs approval, and the job broker refuses
   both kinds.
4. **A new artifact origin, `email_attachment`, on the ordinary run path.** The
   artifact has the run's session and run id and trust `EXTERNAL_UNTRUSTED`. Its type
   is detected from the bytes by the ADR-0120 inspector, not taken from the sender.
   It joins the ADR-0122 reply origins, so the run's final reply carries the file, it
   appears in Artifacts, and it lives as long as the conversation. A failed run's
   file keeps the 30-day expiry. No schema change is needed.
5. **The Save button is a typed email task, kind `attachment`.** `POST
   /v1/email/threads/{id}/attachments/save` requires `email.write` and
   `artifact.write`, and a key replays. The task runs in the thread's own session
   with no model and no email-budget charge, at the owner-waiting priority. It
   records `email.discussion.opened`, so the chat appears in Chat, and ends with one
   server-written reply carrying the file. The sender's file name stays out of the
   reply's text and only labels the file chip. A session with an active run is the
   existing conflict, and the owner tries again.
6. **Saving through Chat always needs approval.** The engine raises
   `save_attachment` to `REQUIRE_APPROVAL`, the floor mailbox mutations have, so a
   policy profile cannot turn it into a standing allow and a scheduled run cannot
   save unattended. `policy/default.yaml` is unchanged, which keeps the memory
   evidence bound to it. `fetch_attachment` needs none: the model cannot reach it
   and the owner's press is the consent.
7. **Every save offers knowledge, as an owner-sent file does.** A readable text,
   Markdown, or PDF file saved by a principal holding `knowledge.write` is marked
   pending, and the ADR-0120 sweep ingests it with `USER` origin trust. The press or
   the approval is the human admission. The stored file stays `EXTERNAL_UNTRUSTED`,
   and the secret scan and backoff of ADR-0284 apply unchanged.
8. **Erasure follows the source; expiry does not.** The receipt carries the
   provider thread and message ids, so owner-authorized source erasure finds the run
   and expires the file and its knowledge document. Gmail deleting the message and
   the email body cache expiring leave the saved copy alone, because keeping a copy
   is what the owner asked for. The file name is source content, so erasure also removes
   it from the call's recorded arguments, its events, its approval and the task record,
   in any chat that saved or tried to save the file, while keeping the call and the
   owner's decision. A file deleted or erased while its knowledge intake is pending never
   reaches knowledge.

9. **`save_attachment` is an unindexed deferral.** It has one name per mailbox, and
   as a read it would have taken a definition slot from a tool used on a request's
   first step, or spent the index's reserved entries (ADR-0267). The default
   instructions name it instead, as they name `artifact.delete`, and the model reaches
   it through `tool.call`. No cap or headroom moves.

## Consequences

- `gmail_read`'s roster grows by two, so its catalog hash changes on deploy. The
  default agent's instructions gain one sentence, so its content-addressed version
  changes too. No
  scope, migration, or environment change is required beyond `artifact.write`, which
  the owner already holds for chat uploads.
- A saved image or other non-document file downloads and previews, but a later
  model turn sees only a one-line marker, because ADR-0120 resolves bytes only for
  owner-written messages. Chat reads a saved document through knowledge search after
  the maintenance sweep, not at once.
- Pressing Save twice stores two copies in the chat, one document in knowledge.
- Text from outside senders can surface in later chats' knowledge results with
  `principal_supplied` authority, the owner's accepted cost of decision 7.
- Deleting the chat deletes the file, and `artifact.delete` can remove it as it can
  any file on a reply, until it enters knowledge and becomes a knowledge source. There
  is still no list of files across chats.

## Alternatives considered

- **An account-wide file store:** the owner declined it for now. It needs artifacts
  without a session, new erasure and retention rules, and new screens.
- **Paging the file through ordinary tool results:** rejected; every page lands in
  the event log, and each page would refetch the whole attachment from Gmail.
- **MCP embedded blob content:** rejected; the transport result already carries
  structured content, and intercepting named tools needs no adapter change.
- **One tool for both paths:** rejected; the floor would then also stop the button,
  which would need a consent mechanism for a read the owner just asked for.
- **A builtin tool in `agent_core`:** rejected; the Gmail credential lives only in
  the server process, and `agent_core` holds no Gmail types.
- **Skipping knowledge:** offered and declined by the owner.
