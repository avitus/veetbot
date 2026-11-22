# ADR-0337: The owner's own synced reply keeps an Inbox gesture current

- Status: Accepted (owner, 2026-11-20)
- Date: 2026-11-20
- Related: ADR-0095, ADR-0165, ADR-0279
- Amends: ADR-0095 decision 6 (which local changes stale an Inbox gesture)
- Detailed design: `docs/plan/email-experience.md`

## Context

On 2026-11-20 the owner replied to a conversation and archived it 38 seconds
later. Email showed "Inbox state could not be changed. Try again." The
production record shows why. Sending the reply started a mailbox refresh. That
refresh read the conversation, found the new sent message and saved a new
content revision while the archive task was between its checks. The archive
consent named the earlier revision, so its last check before the Gmail write
refused it. Gmail was never changed.

ADR-0095 pins an Inbox gesture to the local content revision so that the owner
cannot archive a conversation on a view that lacks mail they have not seen.
The owner's own sent reply is not such mail. Refusing the gesture for it
protects nothing and fails every archive made shortly after a reply. The owner
asked for that case to stop cancelling the archive.

## Decisions

1. A conversation records the earliest revision that differs from the current
   one only by messages the owner sent. A content revision carries that floor
   forward when every added message is a sent message of the account, every
   earlier message is unchanged and completeness is unchanged. Any other
   content change, including removal of a message, clears it.
2. An Inbox gesture is current when the revision it names lies between that
   floor and the current revision. Without a floor only the current revision
   qualifies, which is the earlier rule.
3. One predicate serves admission, every consent revalidation before the Gmail
   write and the projection of a confirmed result. A Report spam or Not spam
   gesture is the same gesture and follows the same rule (ADR-0165).
4. Consent records the revision the owner was looking at. Its owner, account,
   provider thread, tool binding, label change and 120-second expiry are
   unchanged, as are approval, fencing and uncertain-effect handling.
5. The floor is internal state. No client projection carries it.

## Consequences

Archiving straight after a reply succeeds whether the refresh saves the reply
before the request arrives or while it runs. New mail from anyone else still
stales the gesture, as do edits or removals of earlier messages. A confirmed
archive then updates Inbox state on the revision that holds the owner's reply.

"Sent" is the direction the first-party Gmail read server reports for the
owner's own account. A sent message is the owner's own writing, so allowing it
shows the owner nothing they have not seen. Exact-content freshness for
sending is unchanged.

## Verification obligations

Cover the owner's reply saved before admission and during the run, a
correspondent's message at both points, a confirmed result on the newer
revision, and the unchanged refusal of a genuinely newer source.
