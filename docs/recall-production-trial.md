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

## Application updates

With [ADR-0269](adr/0269-semantic-admission-falls-back-at-startup.md), an app
update can start normally when its semantic admission is no longer valid:
it serves ordinary sparse recall and logs
`semantic_recall_unavailable_using_sparse`. Readiness alone does not establish
semantic activation. Verify current admission separately; refresh compatibility
and evidence before restoring semantic retrieval through recomposition. A trial
still expires on its original clock, and no app update renews it automatically.

## Operator preparation

Follow [ADR-0223](adr/0223-owner-production-recall-trial.md). Before activation,
verify the final reviewed release, fresh production source/configuration/schema
compatibility, root-controlled evidence and readback, withdrawal and rollback.
Install version-three admission with the genuine owner instruction; do not fill
the old case-review export or reuse the version-one assembly helper.

Record the fixed start, seven-day end, first 24-hour deadline, admitted source
and owner, and the next check-in time in the operator checkpoint. The existing
monitor should prepare one check-in before each expiry, with enough time for an
owner response at its configured cadence,
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

## Restart after a lapse

Preparation does not authorize activation. Follow
[ADR-0246](adr/0246-bounded-owner-recall-restart.md) only after a separate explicit
owner restart decision and a fresh assessment. Do not turn a late response into
a timely renewal, infer answer-quality approval from normal-use confirmation,
or change the original start or seven-day end.

Prepare version-four evidence with the unchanged original trial and one
`production_restart` object. Retain the original evidence and withdrawal
receipt privately, recording their hashes. Hash the original trial using its
validated `ProductionRecallTrial.model_dump_json()` bytes. Bind the new owner's
response reference/hash and instruction, actual decision and restart times,
previous expiry, fresh assessment hash, current target-input digest and verified
rollback build. The decision must follow the previous expiry and precede or
coincide with restart. A recorded stop or serious incident is not cleared by
this mechanism.

The manifest keeps the original `activated_at`; its expiry is the earlier of
24 hours after restart and the original seven-day end. Inventory for the new
phase begins at restart. Preserve unfinished historical review and the inactive
interval separately. Genuine subsequent check-ins go in the restart object and
follow the same selection, provenance, privacy and timely-response rules above.
Do not replace an earlier restart phase to conceal another lapse.

Before installation, verify final-head GitHub review, hosted CI and delivery,
exact deployed source/configuration/overlay/schema compatibility, local encoder
identity and cost cap, sparse fallback and owner-only behavior, root-controlled
ACLs, expiry/withdrawal and a compatible application/docs/website rollback.
Keep the historical withdrawn manifest and evidence intact; use a new admission
location with independently verified readback. Until both these checks and the
separate owner decision are complete, semantic recall stays withdrawn.

## Carry a live trial through an equivalent release

ADR-0274 implements the proposed recall handoff. The first release containing
the verifier changes the input census and requires ordinary exact admission.
For later updates, the deployment hook runs immediately before promotion.
The independently installed controller checks final review, hosted CI, merge
identity, recovery and both environments. The issuer compares both deployed
trees with the existing admission. A target process checks its own source,
effective composition (including its actual principal and scopes), local model
and original trial evidence. The trial clock and check-in history stay in the
original files. Inspect the private `memory.recalled` event's binding digest
alongside its run and trace IDs to attribute actual semantic use.

Provision the controller once per host. Install a reviewed copy of the project in a root-owned isolated Python installation
at `/opt/veetbot-recall-verifier`. Install `deploy/recall/veetbot-recall-handoff`
at `/usr/local/sbin/veetbot-recall-handoff` and install three copies of
`deploy/recall/probe` named `release-clearance`, `environment` and `recovery`
under `/usr/local/libexec/veetbot-recall/`. Restrict write access to the
administrator throughout those paths. Grant the deploy account noninteractive
sudo only for the installed verifier with its validated release ID and phase arguments (prepare, commit and verify).
Never point the verifier interpreter at a release or editable checkout.

The root-controlled directory `/etc/veetbot/recall/handoff-controller` holds:

- `pg_service.conf` with a read-only `veetbot_recall_verify` libpq service
  against the actual application database. The reference probes use host
  PostgreSQL clients, systemd and the Docker CLI. A missing or inaccessible
  input refuses the handoff.
- `host-profile.json` conforming to `HostProfile` in
  `src/agent_core/operations/recall_handoff_observe.py`. Enroll every effective settings file,
  environment file, credential source, override directory, encoder asset and
  all seven role units. Include the retained application, interpreter and its
  resolved dependencies, public docs/site artifacts and three image tags.
  Record the recovery fingerprint and live schema digest along with the
  independent successful rollback guard receipt. Retain those artifacts and
  non-prunable image tags outside ordinary deployment pruning. Only enroll a complete
  inventory verified for this host. The reference collector accepts filesystem
  links only when their resolved bytes are also covered.
- `enrollment.json` conforming to `Enrollment` in
  `src/agent_core/operations/recall_handoff_controller.py`. At exact admission or genuine
  renewal, pin the live manifest and evidence digests, verified rollback commit
  and the result of the environment probe on the admitted release. Recheck the
  actual configuration before replacing enrollment after a renewal.
- `github-gates.json` with the enrolled release reviewers' GitHub numeric
  `approver_ids`, the `coderabbit_app_id`, the `circleci_creator_id` and the
  complete `required_contexts` for this repository. An enrolled independent
  release reviewer approves the final head after disposing all forms of the
  CodeRabbit review. The verifier reads that ordinary PR approval, the merge
  and reviewed trees, review threads, CodeRabbit result and each hosted CI
  status directly from GitHub. It requires the approved head and successful
  checks before merge. Configure a protected read-only `github-read-token`
  for private repository reads and the GitHub GraphQL review-thread query.
- For a release coordinated outside GitHub approvals, the independent release
  operator can instead publish its normal `review-clearance.json` and
  `hosted-ci-evidence.json` together in `reviewed-heads/<full PR head SHA>/`.
  Restrict writers to the existing release gate and publish the directory
  atomically. The verifier still resolves the actual merge, head and both trees
  through GitHub. An incomplete supplied record refuses the handoff.

The verifier issues a read-only receipt per deployment attempt in the active
trial root. Keep it with the original evidence. A refused or missing receipt
leaves the new process on ordinary sparse retrieval with jobs available.
Deployment removes an attempt marker on failure and retires the previous
release's marker after role verification. Before manual rollback, withdraw
the active trial overrides using the current trial helper. After delivery,
verify public identities, four strict semantic compositions and the five
expected jobs roles. If delivery verification fails after server cutover,
withdraw the current handoff and verify sparse retrieval and jobs.
