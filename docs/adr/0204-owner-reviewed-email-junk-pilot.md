# ADR-0204: A time-limited owner-reviewed email junk pilot

- Status: Accepted — owner explicitly approved implementation and production activation on 2026-10-14
- Date: 2026-10-14
- Amends: ADR-0203 for this bounded pilot only

## Decision

The owner reviewed 43 classifications from 50 live work-Inbox conversations:
30 correct junk nominations, six missed junk conversations and seven correct
keep choices. Seven conversations were protected before classification. This
is a small, selected sample, not the independent 100-case general-rollout gate.

Admit a separate, default-off pilot only with a private owner-reviewed packet
bound to the exact questions, pinned Jev model, owner and `work` account. It
requires at least 43 unique owner-reviewed classifications, at least 20 junk
nominations, at least seven legitimate keep cases and no false-positive junk
nominations. A packet is valid for at most seven days. The pilot may bypass
normal assessment for at most 50 new conversations per UTC day; all other mail
retains normal assessment. Expiry or invalid evidence stops new pilot calls and
restores normal assessment of bypassed mail through bounded refresh.

Use the same closed question and model the owner reviewed. A valid spam or bulk
choice qualifies without ADR-0203's 0.99 score cutoff or broad topic-probability
veto. Preserve every pre-egress protection, including known correspondents,
relationship memory, direct Important/Needs reply feedback, Not spam, kept
subscriptions, incomplete sources, attachments, secret and injection detection.
Keep and uncertain provider outcomes always retain normal assessment. The six
missed junk cases are deliberately left on the normal path in this pilot.

Mail remains readable in Other/All with Important restoring priority. No Gmail
labels change, and no email is archived, deleted, unsubscribed, sent, or used to
create a draft or memory by the bypass. Existing cost reservations, recovery,
audit events and budgets remain intact. Turning the mode off is the kill switch.

The 100-case calibrated filter and its thresholds remain unchanged. This pilot
does not establish general rollout readiness or complete a milestone. A wider
rollout or renewal requires further owner-reviewed evidence and authorization.
Release still requires focused regressions, sidecar checks, exact-head hosted
CI and CodeRabbit, successful deployment, and live release-identity verification.

## Mechanism

`pilot` is a separate `AGENT_EMAIL_JUNK_FILTER_MODE` value. The private packet
uses `JUNK_PILOT_POLICY` (binding ADR-0203's unchanged questions/model plus
this pilot's choice rule, lifetime and cap), an owner/tenant identity, the
literal `work` account, aware issue/expiry timestamps, selected-sample provenance
and independent labels. It is loaded on every email slice. Missing or invalid
pilot evidence disables calls rather than falling back to shadow. Scope and
expiry are checked before dispatch, on receipt use and at assessment commit.

The existing principal lock and email transaction atomically save a content-free
`junk_pilot_day` counter, a `junk_pilot_admission` keyed by a hash of account and
provider conversation identity, and the bypass assessment. Repeated refresh or
recovery of an admitted conversation consumes no further slot; changing evidence
cannot reset the counter. New UTC days grant at most fifty new conversations.
The count is checked before a paid request and again at save; losing a concurrent
last-slot race resumes ordinary assessment. These records contain no mail content
and require no schema migration. The general filter's calibration is unchanged.
