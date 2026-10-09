# ADR-0172 — Website access and specific consent survive owner follow-up

- Status: Accepted under the owner's implementation instruction, 2026-10-09
- Amends: ADR-0094, ADR-0129, ADR-0150, ADR-0164
- Scope: Existing browser and scheduling capabilities; no milestone gate moves

## Decision

An owner can connect an existing conversation to an owned website profile.
Connection is an authenticated application operation, never arbitrary session
metadata supplied by the model. New message admission may carry that selection;
its idempotency identity includes the selection. Changes are serialized with run
admission and cannot replace the profile of running work. A new run refreshes its
context when the selected profile changes, even when its scopes have not changed.
Verified sign-in continues to resume the original bounded request through the
existing authentication wait. Website selection applies to the current chat.

An owner reply in an occurrence conversation has interactive authority. The
run's persisted seed message proves its source: authenticated owner admission
records the principal actor, while a scheduled occurrence records the scheduler.
Scopes alone never prove an owner request. A schedule ID is history, not a
permanent ban on owner task permissions. Scheduled occurrences, delegated runs,
and untrusted surface input remain ineligible.

For the initial action-specific consent contract, an explicit owner request to
follow one X handle authorizes one corresponding Follow click. A pronoun reply
such as "Follow him" resolves only against one unique X profile linked in the
immediately preceding assistant answer. Ambiguous, quoted, negated, unrelated or
untrusted text grants nothing. The application records the resolved target and
source message, and may reuse one matching owned X profile. No model argument,
memory entry, page text or scheduled instruction is consent. A consumed request
cannot authorize another invocation. Existing policy denials still win.

The isolated browser rechecks the exact X profile URL, exact Follow control for
that handle and current revision before dispatch. The result requires positive
Following evidence; an already-followed account needs no action. Lost or ambiguous
post-action evidence never licenses another click. Authentication before dispatch
retains the request, while account/profile changes require new authorization.
Other external writes retain ordinary approval. This is not a general standing
permission or an instruction to follow recommendations automatically.

Conversational schedule creation can explicitly request website access from the
current chat's trusted binding. The approval view exposes this dependency;
creation fails visibly when it cannot be satisfied. The immutable revision pins
that profile and its read scope, while subsequent owner replies remain separate.
Existing schedules are not silently rewritten.

## Verification

Regressions cover the Friday recommendation and owner reply; same-chat profile
connection and unchanged-scope context refresh; foreign, unavailable and changing
profiles; admission replay and active-run conflicts; scheduled versus owner task
permission; specific consent, ambiguous targets, repeated invocations and hostile
live controls; authenticated continuation and uncertain effects. Native tests
cover message selection and website connection. Run final repository verification
on the sidecar, plus risk-relevant persistence and native checks.
