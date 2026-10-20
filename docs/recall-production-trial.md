# Reviewing recall in real conversations

The owner has requested a limited production trial with daily review of actual
use. This replaces filling in the synthetic case packet for this trial.
The activation change still needs its own authorized release and production
checks; this guide does not mean the trial is running.

## What you will do

Use Veetbot normally in your own private interactive chats. Before each daily
deadline, the operator will bring up to five recent conversations with semantic
recall to the review chat. Read your real questions and the answers in their
original conversations, then reply in ordinary language:

- Did it remember anything incorrectly or use an outdated fact?
- Did it miss something you expected it to remember?
- Should the trial continue, or should recall be paused?

You can flag a problem at any time, including in a conversation outside the
sample. You do not need to determine retrieval scores, inspect JSON, label
synthetic cases, or fill a separate form. Your actual response supplies the
check-in. If there was no eligible use, the check-in will say so.

## What happens to your feedback

The operator records your words and decision, investigates reported mistakes,
and uses the existing correction or deletion path when authorized. Feedback
does not automatically rewrite memories. Serious problems stop the trial.
Private conversation text and source memories stay in their authorized surfaces;
the original private review chat holds your findings and the admission record
holds references and hashes.

The first window lasts 24 hours. A timely continue decision allows another
24 hours, up to seven days from the original start. If a review is missed,
semantic recall expires and ordinary sparse recall remains available. You can
stop sooner. Continuing past the first week needs a new decision.

## Operator preparation

Follow [ADR-0223](adr/0223-owner-production-recall-trial.md). Before activation,
verify the final reviewed release, fresh production source/configuration/schema
compatibility, root-controlled evidence and readback, withdrawal and rollback.
Install version-three admission with the genuine owner instruction; do not fill
the old case-review export or reuse the version-one assembly helper.

Record the fixed start, seven-day end, first 24-hour deadline, admitted source
and owner, and the next check-in time in the operator checkpoint. The existing
15-minute monitor should prepare one check-in about one hour before each expiry,
with a final trial assessment before the seventh day ends. Deduplicate notices.
Never remind on every monitor tick or renew because the owner has not replied.

For each period, inventory completed private interactive runs for this exact
tenant/owner and admitted semantic configuration. Use existing authenticated
session/run reads and recorded recall events and traces. Select the most recent
five eligible runs (all if fewer), with stable timestamp/ID ordering. Record the
window and its cutoff, exact run/trace references, eligible count and successful inventory status
in a private selection receipt; any failed or incomplete inventory is unknown,
never zero use. Do not run new retrieval, replay model calls, scan other owners,
or copy raw logs and conversation contents into the receipt. Include separately
any incident the owner reports.

Verify current visibility and deletion/correction state before presenting a
reference or supporting memory. Do not expose an operator trace as a new public
API. Explain only what memory was available; do not claim it caused an answer.
The owner can review answers even when causal attribution is unavailable.

Keep the exact human response in its original private review chat and record its
reference/hash, time, selected run IDs and selection/provenance receipt hashes
in the versioned check-in. Retain negative decisions unchanged.
The response must be before expiry. Refresh the inventory each day and preserve
all earlier observations. The next inventory starts at the previous cutoff, so runs
completed while awaiting the owner are not silently omitted.
Only a timely, explicit continue response with complete evidence can renew the
admission. Revalidate source/configuration, install the new evidence atomically
with restricted ownership, re-compose and verify expiry/readback. The original
trial start stays fixed. If selection, authorship, source or lifecycle checks
fail, leave semantic recall withdrawn and report the concrete problem.

This process establishes supervised owner feedback, not benchmark success or
permission for scheduled/delegated recall or unattended reconsolidation.
