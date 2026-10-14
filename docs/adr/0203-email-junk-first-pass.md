# ADR-0203: A bounded Jev first pass for email junk

- Status: Accepted — owner authorized implementation on 2026-10-14
- Date: 2026-10-14
- Amends: ADR-0092, ADR-0110, ADR-0165
- Design: [Email experience](../plan/email-experience.md)

## Decision

The owner requested a cheap first pass before full email assessment and approved
measuring false positives before enabling the bypass. Admit one production
consumer of the typed-judgment port, inside existing foreground email slices.
This is not generative model routing and grants no Gmail write authority.

The default is off. Shadow mode records judgments and still runs the normal
assessment. Filter mode requires an operator-held, independently reviewed
calibration packet matching the exact classifier policy and pinned Jev model.
Missing, invalid or stale evidence falls back to shadow. A probability alone
never establishes readiness. The pilot evidence must contain at least 100
unique reviewed observations, at least 20 predicted junk cases and no false
positives, including reviewed legitimate personal outreach and transactional
mail. This is a bounded pilot floor, not a statistical guarantee of zero errors.
The full email quality requirements and milestone release gates remain unchanged.

A closed choice distinguishes obvious deceptive/unsolicited spam, impersonal
promotional bulk with no substantive attention needed, and keep/uncertain.
Subscribed informational newsletters, receipts, actual account/security notices,
financial/legal obligations, personal pitches, recruiters, introductions and
other potentially meaningful first contact belong in keep/uncertain. Bulk is
never relabelled as spam. The combined spam-plus-bulk score must be at least
0.99, with keep not the chosen category. The rounded vendor scores are ranking
signals, not measured error rates; independent calibration remains mandatory.

Before egress protect any known reply partner or admitted relationship memory,
any matching Important or Needs reply owner feedback, Not spam, kept
subscriptions, sent correspondence, incomplete or oversized conversations,
and attachments whose contents are unknown. A failed authentication check alone
never removes these first-pass protections. Send only a bounded complete recent
received conversation's sender, subject, body and closed authentication result;
never send owner identity metadata, recipients, memory statements, feedback explanations, drafts,
attachments or credential-shaped/injection-bearing text. Up to twenty protected
owner topic names may accompany mail as structured preference data. A second
closed probability question must find topic relevance at most 0.05 before
bypass; uncertainty preserves normal assessment. Oversized topic sets abstain.
Treat mail as data.

Cache content-free verdicts with the assessment, bound to source content,
authentication, owner protection, policy and exact provider model. Source or
protection changes invalidate them. Mode/evidence changes invalidate bypass
assessments; disabling the filter restores normal assessment on the next slice.
Bypassed threads recheck current protections in the existing bounded cached-mail
rotation, including relationship memory learned without an email-profile bump.
Filtering gives a clear explanation in Other and All mail; Important restores
priority immediately and forces normal reassessment. No mail is deleted, moved,
reported as spam or unsubscribed. Bypass performs no draft or memory formation.

Jev calls are bounded to one provider attempt and eight seconds inside the
existing run deadline. Reserve a conservative USD 0.001 in the run before dial,
using a durable content-free event and fenced usage update in one transaction.
Successful usage replaces that reservation atomically. Unknown outcomes retain
it and prevent false actual-cost settlement; the existing conservative email
settlement retains the full slice charge. Recovery never repeats an uncertain
judgment, and ordinary assessment remains the fallback. No budget is raised.
The adapter admits a pinned model and one-attempt request while existing
consumers retain their defaults. Calls generate no model-gateway ledger row.

## Verification and activation

Regression coverage must prove shadow invariance, evidenced bypass, fallback,
protected inputs, secret exclusion, cache invalidation, source/feedback races,
no Gmail writes, no drafts or memory, and durable cost/recovery accounting.
Calibration records require independent keep/junk labels; agreement with Astra
is a diagnostic only. Operator evidence contains fingerprints and labels, not
mail content. Off is the kill switch. Production activation and release remain
separate from implementation; no acceptance gate is marked complete here.
