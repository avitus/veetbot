# ADR-0223: Owner trial with production recall review

- Status: Owner-directed scope; implementation and release verification pending
- Date: 2026-10-20
- Related: ADR-0218, ADR-0220, ADR-0222

## Decision

The owner requested rollout with periodic review of real production conversations
because synthetic cases were difficult to assess. For this retained comparison,
that direction replaces the pre-activation case-by-case human review with a
supervised production trial. It supplies rollout intent, not fabricated findings
on the 307-case or 37-case packets, proof of quality, or permission to open a new
pull request. Those packets and their version-one/two contracts remain unchanged.

Version-three bounded evidence separates the owner's trial authorization from
human findings. It binds the original complete comparison and interruption/cost
identities, the exact owner instruction and its provenance, one tenant/owner,
and a fixed trial start. It retains the comparative-benefit, operational,
compatibility, access, erasure, local-encoder, interactive-only and release gates.
The failed quality goals, all 1,600 observations and all charges remain unchanged.
No new benchmark, embedding-provider call, or unattended use is authorized.

## Daily check-in and expiry

The first admission expires 24 hours after the fixed trial start.
The existing monitor prepares a daily check-in in the owner's chat before that
deadline. The owner reviews up to five recent real conversations with semantic
recall, plus any additional incidents they report. The operator selects the most
recent eligible completed runs in the period, never the most favorable answers.
The check-in asks what was wrong, outdated, or missing and whether to continue.
There are no synthetic questions or per-case grading forms.

Each check-in retains the owner's actual words in the original private review
chat. Admission carries that response's reference and hash, decision and time,
the selected run identifiers, eligible-run count and a hash of the operator's
selection and provenance receipt, without copying private prose. It binds this
trial and covers the period from the previous inventory cutoff (or activation)
through the new cutoff, which is no earlier than the previous owner response.
The owner responds before the current expiry; runs completed after the cutoff
belong to the next window.
A period with no eligible
runs is recorded as zero use, not successful quality evidence. An unavailable
inventory cannot be represented as zero. Hashes do not authenticate authorship;
the operator verifies the genuine owner response and eligible-run inventory.

A timely explicit continue decision sets expiry to the earlier of 24 hours after
that check-in and seven days after the original start. An operator can withdraw
sooner at any time. Missing, late, future-dated,
incomplete, duplicate or mismatched check-ins refuse renewal. A stop decision or
a serious incident refuses continuation. Evidence changes still withdraw the
old composed handle; renewal requires installation of newly verified evidence
and re-composition, never silent extension or changing the original start.
The operator must withdraw immediately on an owner stop, confirmed access or
lifecycle violation, or unacceptable obsolete-fact incident. Absence of a reply
is not consent. Expiry falls back to sparse recall even if the monitor is offline.
Restart after a lapse requires a separate explicit owner decision and assessment;
this contract does not authorize resetting the seven-day clock.

## Review surface and privacy

Use the owner's existing private conversations for the question and answer.
Show source memories only through an authorized existing read path after current
owner, visibility, correction and erasure checks. A trace proves what was made
available to the model, not that it caused the answer. Do not reconstruct past
context by running a fresh search. Missing or expired trace evidence is unknown.

The check-in itself needs only conversation references and the owner's findings.
Do not copy private messages, raw logs, memory statements or source excerpts into
Git, automation prompts or public artifacts. Feedback is not an automatic memory
edit: corrections and deletion use the existing governed paths. No new HTTP
trace route or independent archive of deleted conversations is introduced.
The workflow is described in [the production trial guide](../recall-production-trial.md).
The small production sample informs an owner decision; it is not a recall-rate
estimate, representative certification, or a replacement score for the benchmark.

## Delivery and validation

The source must receive its own authorized CodeRabbit GitHub review, final-head
CI and verified production delivery before activation. Actual production
configuration, schema, exact source composition, evidence ACLs and compatible
rollback/withdrawal must be refreshed and verified. The separate unpublished
development repairs receive no clearance from this change.

Red-first tests cover initial admission without invented synthetic findings,
24-hour and seven-day limits, timely renewal, missed review, rejection,
wrong-owner and wrong-trial receipts, missing sample/provenance, version mixing,
and all preserved source/configuration and comparison refusals. Test
authorizations and check-ins are synthetic and never operator evidence.
