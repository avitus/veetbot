# ADR-0215: Retained formation terminal outcomes and collector cleanup

- Status: Owner-authorized repair verified; checkpointed collection in progress
- Date: 2026-10-18
- Related: ADR-0194, ADR-0212, ADR-0214

## Evidence and scope

The owner authorized formation repairs, including runtime changes, preservation
of completed observations and an aggregate cap up to USD 500 if needed. The first
deadline-repaired segment retained five additional observations. All 418 rows,
including the original 413, remain immutable under their original identities.

The formerly cancelled provider call completed and its cost was recorded. Its
terminal response was not retained in the pending formation bank. The exact
rejection cause is therefore unknown; token exhaustion is not established by
the cost alone. A reproduced collector defect kept a ContextVar token across an
async-generator yield. Closing the generator in another context raised ValueError
and left an active-stream count behind. Ledger sealing then failed before the
collector retained its attempt report. The stopped segment remains stopped and
its unsealed journal, pending bank and scratch databases remain original evidence.

## Decision

Scope slot attribution to each provider iteration and close operation, never
across a yield to the consumer. Drain and settle the provider stream before
delivering its terminal event. Invalid events after a terminal still fail.
Early cancellation retains uncertain reservations. Record content-free terminal
facts before downstream validation: request and slot identity, stop reason,
usage, retry count, tool count and content-part kinds. Never retain reasoning,
opaque continuation, credentials or assistant content in these diagnostic files.

Persist admitted attempt facts even when a ledger cannot seal, without claiming a
sealed ledger or granting retries. Cleanup must preserve the primary failure;
new cancellation still propagates after safe retention. These changes do not
alter provider requests, reservations, formation decisions or reader behavior.

The production formation contract already uses an audited deterministic fallback
for unusable terminal output. Version 2 paired banks can retain the specific
MAX_TOKENS refusal that the bank itself raises before passing output to that
runtime. The refusal records its usage and terminal metadata, not a fabricated
completion or the partial response text. Replay raises the identical ValueError
at the identical request position, so the runtime executes the same fallback.
The complete ordered requests and resulting formed state must still match.
Actual reader answers and every quality gate remain required; a fallback is
neither a forced answer nor a passing result.

This exception requires a completed provider event with known usage, matching
MAX_TOKENS reasons, no internal retry, no tool calls and ordinary text parts only.
Other unsupported terminals, transport failures, incomplete recordings, changed
requests and changed formed state remain ineligible. Legacy version 1 banks
retain their original contract and bytes. No failed historical bank is converted
to version 2, and no missing historical response or latency is reconstructed.

## Continuation and release

No automatic successor or additional send is authorized by a diagnostic file.
Before a separately admitted successor, preserve every original artifact,
reconcile all eleven ledgers and prove the disposition of each unfinished slot.
The five completed rows lacking a terminal segment report require explicitly
derived provenance from their durable events and original row/journal bytes;
missing wall timings must remain missing. A known unsent reader and a failed
pre-answer preparation cannot be confused with an unknown sent reader.

The recovery binds the exact owner instruction, stopped-process assessment,
849-event history and all original artifacts. It appends one recovery event for
each proved unfinished slot and a derived report for the five completed rows.
It adds a separately derived seal for the settled journal without modifying any
original bytes or claiming the original process sealed successfully. A failed or
unknown successor cannot consume either recovery again. The new bank uses a
distinct path; all failed pending banks remain evidence.

Pin the changed collector and bank implementation separately from the original
evaluated runtime and deadline repair. Existing observations keep their original
measurement and bank identities. Remaining positions retain the full 1,600-row
matrix, original deadline, per-answer limits and genuine independent human review.
Keep all charges and uncertain reservations. This change allocates no money,
grants no activation authority and does not authorize a new pull request.
