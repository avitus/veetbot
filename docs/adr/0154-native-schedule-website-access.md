# ADR-0154: The native schedule detail chooses a schedule's website access

- Status: Accepted (authorized by the repository owner, 2026-10-06; amends
  ADR-0075 decision 4)
- Date: 2026-10-06
- Related: Sections 16 and 29 of the engineering plan; ADR-0049, ADR-0075,
  ADR-0150
- Detailed design: `docs/plan/scheduling.md`

## Context

ADR-0150 lets a schedule pin an owned, ready browser profile so that its
occurrences can read signed-in pages, and requires an existing schedule to be
bound through the authenticated full-definition update. Chat cannot bind one,
and the native schedule browser is read-only (ADR-0075 decision 4), so the only
way to bind a schedule was a hand-built request carrying its complete
definition. After ADR-0150 reached production nobody made that request, and
the owner's daily X briefing kept answering that it could not reach the
signed-in feed. The owner asked to choose the binding in the app.

## Decisions

1. **One management control joins the read-only browser.** The schedule detail
   gains a Website access picker. ADR-0075 decision 4 otherwise stands: the
   device creates, pauses, resumes, cancels and edits nothing else.
2. **The choices are the owner's ready sign-ins.** The picker offers None and
   each `ready` browser profile, labelled by the host names of its allowed
   origins. A bound profile that is no longer ready, or no longer listed,
   stays visible with its state, so the owner can see why a run would fail.
   ACTIVE and PAUSED schedules accept a change; terminal and unknown states
   show the binding read-only.
3. **The full-definition update is the only server surface.** No route, scope,
   flag or migration is added. Saving performs a fresh point read, then sends
   `PATCH /v1/schedules/{schedule_id}` with that read's `current_revision` and
   the revision's own JSON less its five revision-only fields (`schedule_id`,
   `revision`, `timezone`, `created_by_principal_id`, `created_at`). Only
   `browser_profile_id` and the `browser.profile.read` requested scope change:
   binding adds the scope and unbinding removes it. A definition field the
   client does not model is sent back unchanged, and a future revision-only
   field makes the server refuse the update instead of resetting a setting.
4. **The device now uses write authority.** Saving needs `schedule.write`, and
   binding needs `browser.profile.read`; ADR-0150's validation of an owned,
   ready profile and a requested, held scope is unchanged. Listing the choices
   reads profiles under `browser.profile.read`.
5. **Server truth stays displayed.** The picker shows the record the server
   returns. A `schedule.revision_conflict` is read again and retried once. A
   second conflict, a validation refusal or any other failure is reported
   beside the picker, which keeps the server's binding. Choosing the binding
   the schedule already has sends no update.
6. **Conversation still cannot bind.** ADR-0150's rule that neither the model
   nor a prompt selects a profile is unchanged; the owner's explicit choice in
   an authenticated client is the authorization.

## Consequences

- The owner can bind the existing briefing to the X sign-in, and later rebind
  or unbind it, without a hand-built request.
- Binding grants only reads. `browser.act`, scrolling included, keeps its
  approval rules, so an unattended run reads what the first page shows.
- The evidence is Swift transport and view-model tests and an iOS UI journey
  under ADR-0049; no gate is registered and no milestone gate count changes.

## Alternatives considered

- **A dedicated binding route:** rejected. ADR-0150 already designates the
  full-definition update, and a second route would duplicate its validation.
- **Re-encoding the typed revision:** rejected, because a definition field the
  client does not decode would be dropped and the server would reset it.
- **Binding from chat behind an approval:** not chosen. ADR-0150 keeps profile
  selection out of conversation, and the request was for an app control.
