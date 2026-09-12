# ADR-0091: Schedule identity in outcome notifications

- Status: Proposed
- Date: 2026-09-11
- Related: ADR-0059, ADR-0062, ADR-0072
- User authorization: identify the actual schedule in scheduled-run
  notifications, then push the completed changes to `dev`.

## Context

The owner found generic schedule outcomes unhelpful even after their status
and next action became explicit. The previous notification design excluded
schedule titles. The owner's follow-up expressly authorizes showing which
schedule ran. This is a narrow exception to ADR-0062 decision 4 and ADR-0072
decision 7, not authorization to include instructions or run results.

## Proposed decision

1. Both scheduled-run outcomes and skipped-occurrence alerts carry a snapshot
   of the occurrence's schedule title and nominal firing time. Production uses
   the immutable revision named by the occurrence, never the current revision
   or a lookup at dispatch time. A rename while a run is executing therefore
   cannot change the identity in its notification; retries preserve the same
   snapshot.
2. A closed optional `schedule_context` value on the persisted notification
   payload contains only `title` and `scheduled_for`. It is legal only on the
   two schedule kinds. Old rows without it retain their generic display. New
   producers receive the already loaded revision and verify its schedule and
   revision identifiers against the occurrence.
3. The title is normalized to a single line, stripped of invisible formatting
   controls, and bounded to 160 characters with an ellipsis. Credential
   detection checks the entire original and normalized title before truncation;
   a credential-bearing or empty legacy title becomes `Scheduled task`.
   The nominal instant is rendered in the pinned recurring timezone, or UTC for
   a one-time schedule, retaining its numeric UTC offset in the snapshot.
4. APNs displays the title as the alert subtitle and the scheduled time in the
   body beside the outcome and next action. It omits `schedule_context` from
   the version-1 `veetbot` tap dictionary, so existing Apple clients still
   validate and open every notification. The complete wire payload stays below
   APNs's 4-KiB limit, including non-ASCII titles.
5. Schedule instructions, conversation messages, results, questions, approval
   summaries, recipients, credentials, and failure details remain excluded.
   Process events, logs, and delivery records gain no title. The optional
   snapshot uses the existing outbox JSON and authenticated inbox, with no
   schema migration, new trigger, new transport, or new milestone.

## Verification

Extend the existing `gate.notify.content_free` coverage for this explicit
exception while retaining its identifier and milestone. Tests cover pinned
revision identity after rename, all terminal outcomes and skipped dispositions,
time offsets, replay, legacy payloads, credential filtering before truncation,
forbidden kinds and fields, Unicode size bounds, persistence, and the unchanged
Apple tap dictionary. Existing notification atomicity and isolation gates remain
required.

## Consequences

Schedule names can now appear on the lock screen and pass through APNs, as the
owner requested. OS preview settings still govern their visibility. The new
snapshot is persisted before dispatch and is available in the authenticated
inbox. A historical row created before this change cannot recover its title
from the notification alone and retains its previous generic presentation.

## Alternatives considered

- Look up the latest schedule title during dispatch: rejected because renames
  would mislabel old occurrences and retries could change their content.
- Add the snapshot to the version-1 tap dictionary: rejected because installed
  Apple clients reject unknown keys.
- Include an instruction or result preview: outside the authorized exception.
