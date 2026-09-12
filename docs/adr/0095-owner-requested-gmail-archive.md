# ADR-0095: Owner-requested Gmail archive from Email mode

- Status: Proposed — owner authorized the checkbox's Gmail archive behavior on 2026-09-12
- Date: 2026-09-12
- Related: ADR-0017, ADR-0071, ADR-0085, ADR-0092
- Detailed design: `docs/plan/email-experience.md`

## Context

The Email checkbox originally changed only Veetbot's handled state. The owner
requested that checking it archive the conversation in its originating Gmail
account. A checkbox must not silently change the meaning of an older client's
local dismissal request. The existing Gmail write server already supports the
required operation: removing `INBOX` from one thread. Restoring the checkbox
means an explicit move back to that account's Inbox, rather than restoring an
unknown earlier set of Gmail labels.

## Decisions

1. Add a distinct, authenticated archive command. The client supplies the
   application thread ID, expected source revision, desired archived state,
   and an idempotency key. The server determines the account, provider thread,
   and sole permitted label delta. Keep `/dismiss` local-only for old clients.
   Advertise archive support and the stable write-server identity per account;
   clients must not fall back to local dismissal when archive is unavailable.
2. Treat the clearly labelled owner gesture as consent to exactly one action.
   Persist its owner, account, thread, source revision, desired state, immutable
   tool binding, and bounded expiry with the ordinary durable task. This is
   neither a standing grant nor permission for model or refresh work to archive.
   Require the existing email, run, session, approval-resolution, and exact
   account read/write scopes before admission and revalidate at execution.
3. Use a typed, model-free task and the ordinary tool pipeline. Gmail writes
   retain `REQUIRE_APPROVAL`. A narrowly scoped consent consumer may satisfy
   the pending approval only when its exact tool and arguments match the
   durable owner request and current authority, source, policy, and expiry
   still permit it. Record `approval.requested` before ordinary
   `APPROVE_ONCE` resolution and reuse the same frozen invocation, including
   its normal post-approval revalidation. Respect distinct-resolver policy.
   Do not emit a second notification asking for the consent already supplied.
   Recheck current consent and authority at the tool's pre-effect dispatch
   boundary, so an intervening revocation cannot reach Gmail. This extra guard
   does not authorize a tool or change policy for other runs.
4. Keep ordinary worker leases, checkpoints, idempotency, and uncertain-effect
   handling. A lost response replays the same operation, not a new Gmail
   write. A potentially dispatched non-idempotent invocation is not retried
   automatically. A fresh authorized read can reconcile observed Inbox state;
   absence of confirmation remains visible rather than being called success.
5. Update the visible archive state only after confirmed provider success or a
   fresh read establishing the requested state. Pending or failed operations
   do not hide the row. Restore adds only `INBOX`; archive removes only
   `INBOX`. Neither changes unread state, other labels, importance feedback,
   or draft contents. New correspondence remains eligible for attention.
6. Separate mailbox-label changes from source-content changes. Label-only
   synchronization updates Inbox state without incrementing content revision,
   invalidating drafts, or re-learning the same content. Existing fingerprints
   remain usable when the only observed difference is labels.
   A bounded page verifies remote account/thread identity without downloading
   all bodies or requiring cached Gmail history equality. History changes on
   label writes, so equality would incorrectly block immediate restoration.
   The gesture applies to the entire conversation, including messages not yet
   loaded by the client. Local content revision still guards stale requests.
7. Archive/restore uses no model and reserves no automatic-email dollars. It
   remains bounded by ordinary tool, time, concurrency, and authority limits.
   Exhausted automatic learning allowance does not block this explicit
   mailbox-management action. Account routing remains pinned through retries
   and cannot be redirected by a new manifest default.

## Consequences

The checkbox becomes a confirmed Gmail action on supported servers. Older
clients retain their local handled semantics. Archive state and pending or
uncertain operation state are durable projections that another device can
read without inferring Gmail state from legacy dismissal records.

No new Gmail roster, OAuth permission, policy exemption, model routing, or
database table is required. The UI gesture supplies per-action approval; it
does not authorize autonomous mailbox organization or change send approval.
Gmail does not offer an atomic content-revision precondition on thread label
changes. The owner action covers the whole provider conversation, including
unseen mail; this is mailbox management rather than content-derived sending.
Local new-content state must not be overwritten by a stale completion, and
the existing exact-content checks for sending remain unchanged.

## Verification obligations

Cover both accounts and default changes, absent or revoked scopes, stale
source revisions, mismatched or expired consent, exact approval audit, zero
model calls, replay and concurrent clicks, crash/dispatch uncertainty, confirmed
archive and restore, unchanged labels/read state/draft edits, label-only sync,
new correspondence, old-server refusal, and native pending/failure recovery.
