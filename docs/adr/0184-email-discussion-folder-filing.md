# ADR-0184: Manually file email discussions from Chat

- Status: Proposed — repair requested by the owner on 2026-10-11
- Date: 2026-10-11
- Related: ADR-0092, ADR-0102, ADR-0113
- Design: [Chat thread folders](../plan/thread-folders.md),
  [Email experience](../plan/email-experience.md)

## Context

The owner reported that an email discussed in Chat cannot be added to a
folder. ADR-0102 excludes every session carrying `email_thread_id`, while
the Email design exposes that same ordinary session in Chat after Discuss
in Chat or an owner message. The native conversation row already offers
manual filing, but the server rejects it with `session_not_chat`.

## Proposed amendment

Amend ADR-0102's manual-filing exclusion for an email-linked session that
contains a principal-authored `email.discussion.opened` or
`user.message.created` event belonging to its owner. Preserve the email
metadata, draft, source attribution and session identity. Existing events
qualify, so previously discussed emails need no migration or repeated action.
Draft-only email sessions remain ineligible. Presence of `email_operational`,
`schedule_id` or `run_kind` still excludes a session, even if it has owner
interaction. Ownership checks and session scopes remain unchanged.

This is a manual-filing repair. Automatic grouping inputs and proposal
eligibility keep ADR-0102's original exclusion of email-linked sessions.
No gate, milestone, schema, dependency, mailbox action or authority changes.

## Verification

Exercise Discuss in Chat through the HTTP boundary, then file, replay, move
and unfile its existing session while retaining the draft and thread context.
Cover legacy owner messages, draft-only and application-authored events,
foreign authors and owners, missing scopes, and the reserved exclusions.
Check the same filing lifecycle against PostgreSQL as well as memory.

## Alternatives

Removing `email_thread_id` would break the retained source association.
Allowing every email-linked session would expose draft-only state to filing.
Hiding the move menu would preserve the reported inability to organize a
conversation the owner is already discussing in Chat.
