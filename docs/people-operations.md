---
title: People memory operations
---

# People memory operations

This runbook covers the People schema, controlled import, evaluation, and
application rollback. It does not authorize production changes or source access.
The governing contract is [People and relationships](plan/people-and-relationships.md).

## Installation and rollback

The schema revision is `c28d52ea7301`. Apply it using the existing deployment
migration step during a maintenance window. Its four GIN indexes on existing
event, invocation, recall-trace, and email tables use ordinary transactional
`CREATE INDEX`, which blocks writes while each index is built. Quiesce application
writers before starting the release and keep them stopped through migration;
the deployment script does not stop the old processes before `alembic upgrade`.
Allow for table-size-dependent index build time. Resume service with the release's
normal restart and readiness checks after migration succeeds. Do not substitute
`CREATE INDEX CONCURRENTLY` inside this transactional migration.

People is enabled by default under ADR-0101. Leave
`AGENT_MEMORY_FORMATION_POLICY_PIN` empty (or select `formation@11`) and use
`AGENT_MEMORY_PROVIDER_EXTRACTION_MODE=auto` or `required` with the configured
memory provider. No People or Email semantic evidence artifact is required.
Existing Email setup and exact source scopes still apply. Add `people.read` and
`people.write` to the owner's authenticated scopes for the browser and tools.
Open a new conversation to receive the current tool catalog.

Automatic Chat formation, attributed Email formation, contextual recall, native
browsing/correction/forgetting and explicitly scoped history imports are available
together. Set `AGENT_PEOPLE_ENABLED=0` only for an operational shutdown; an older
explicit formation pin intentionally selects that legacy implementation.

Disabling People hides its routes, tools, and contextual recall and stops new
capture. Existing derived data and source provenance remain stored. Erasure
maintenance continues with the switch off. Do not select `formation@11` while
the switch is off; configuration deliberately rejects that combination.

For an application rollback:

1. Stop new imports and cancel active imports through their original job IDs.
   Wait for in-flight calls to settle; unresolved reservations remain held.
2. Under the deployment lock, stop application writers and export the database.
3. Select an artifact that explicitly supports `c28d52ea7301`, turn People off,
   and restore its compatible, evidenced memory configuration. Use the
   [deployment rollback procedure](deployment.md#manual-rollback).
4. Keep the People tables and memory revisions intact. Verify schema agreement,
   application readiness, the release identity, ordinary Chat, and erasure
   maintenance before reopening service.

An older binary expecting `b27c41d9e602` cannot run against this schema merely
because People is disabled. Roll forward with a compatible disabled build if
no compatible rollback artifact exists. Never bypass the startup schema check.

Alembic downgrade refuses any populated People or memory-revision tables.
Exporting does not itself authorize deleting those records. A destructive schema
rollback requires a separately reviewed restoration plan and an isolated restore
rehearsal; prefer application rollback with the additive tables retained.

## Explicit history imports

In People, open **Import history**, select conversations or email accounts,
choose the date range (end date excluded), record limit, and total budget, then
review the preview. Email defaults to already retained passages. **Read older
mail from selected accounts** explicitly includes mailbox discovery in the saved
scope; resume cannot silently switch the source mode or add an account.

Mailbox reads use the governed Gmail tools and verify the selected mailbox's
identity. Discovery stores original event references and bounded page cursors.
After discovery, analysis processes evidence oldest-first using its original
dates. The automatic 90-day email catch-up window remains unchanged.

Progress distinguishes mailbox messages read from analyzed sources. Counts are
unknown until discovered; a record or passage cap reports partial coverage.
Unavailable bodies, changed messages, and provider failures do not count as a
complete mailbox. Cancellation fences further source registration and analysis.
Resume a failed or paused job through its saved receipt; unresolved model charges
must be settled before a retry can spend more. Synthetic tests exercise these
paths without accessing a live mailbox.

`waiting_for_chat` means the import yielded to this owner's foreground work.
Progress and reservations remain saved; the next background slice is scheduled
30 seconds later and can defer again if Chat is still busy. No manual Resume is
needed for this state. Imports use the configured asynchronous worker class;
verify that the async worker is running if a due queued job never advances.

## Export and restore safeguards

A complete PostgreSQL custom-format export includes People heads, all immutable
revisions, source links, erasure/suppression receipts, import jobs, and ordinary
memory history. Export the whole database so foreign keys remain restorable.
A People directory JSON response is not a database backup.

Use the PostgreSQL client major version matching the server and the dedicated
backup identity. Supply the connection through libpq configuration and a private
password file; do not put credentials in command arguments or transcripts.
The following manual export streams directly into encryption. Its destination
must be new, private, outside the repository, and on the approved backup storage.
`PEOPLE_EXPORT_DIR` names that new directory; `PEOPLE_EXPORT_RECIPIENTS` names the
approved age public-recipient file. This operation does not upload anything.

```bash
set -euo pipefail
umask 077
: "${PEOPLE_EXPORT_DIR:?Choose a new private export directory}"
: "${PEOPLE_EXPORT_RECIPIENTS:?Choose the approved age recipients file}"
mkdir -- "$PEOPLE_EXPORT_DIR"
pg_dump --format=custom --no-password |
  age --recipients-file "$PEOPLE_EXPORT_RECIPIENTS" \
    --output "$PEOPLE_EXPORT_DIR/postgres.pgdump.age"
sha256sum "$PEOPLE_EXPORT_DIR/postgres.pgdump.age" > "$PEOPLE_EXPORT_DIR/SHA256SUMS"
```

Retain the backup manifest and detached signature through the
[operational-hardening contract](plan/operational-hardening.md#the-backup-set).
That full recovery set also covers artifacts and browser profiles. Encryption
alone does not establish who produced an export; a checksum alone does not
establish authenticity. Preserve the maximum 35-day encrypted-backup retention.
A failed export is incomplete even when a partial output file exists.

Rehearse restoration in an isolated database with all ingress, provider calls,
mailbox access, schedules, and device delivery disabled. Verify the manifest,
signature, hashes, schema revision, owner boundaries, and readable historical
revisions. Reapply erasure receipts newer than the restored snapshot, drain
pending cleanup, and verify that forgotten identities and source-derived copies
remain unavailable before serving restored data. If the newer receipts are not
available, keep that restored database offline. Retaining a pre-erasure snapshot
does not authorize reintroducing erased content into service.

### Reapply erasures newer than the snapshot

Each applied receipt retains opaque record, belief, and source identifiers after
cleanup. Export every applicable receipt from the authoritative newer state into
new private files before discarding that state:

```text
agent people export-erasure RECEIPT_UUID --owner TENANT/PRINCIPAL --output NEW_PRIVATE_FILE
```

The command refuses previews, incomplete pages, foreign owners, existing output
files, and archives over 64 MiB. Its output file is owner-readable only. Include
the archive and its returned SHA-256 in the verified signed recovery manifest;
the command itself does not sign files. The checksum alone is not authenticity.

After verifying that manifest's signature and restoring the older database with
all ingress and writers stopped, replay each newer receipt:

```text
agent people restore-erasure --offline --owner TENANT/PRINCIPAL \
  --receipt VERIFIED_PRIVATE_FILE --sha256 VERIFIED_MANIFEST_DIGEST \
  --audit-session RESTORED_OWNER_SESSION_UUID
```

`--offline` is the operator's assertion that the restored database is not serving;
it does not stop services. The command checks the digest, owner, complete receipt
pages, and original applied scope before mutation. It follows restored person
links to include older revisions, keeps source suppression, and returns a durable
receipt. Retry the same archive safely. A pending result remains offline until
cleanup reports completion. Maintenance retries erasures even while People is
disabled. Replaying a receipt does not prove that every newer receipt was supplied;
recover the complete authoritative set before reopening service.

## Owner-scoped management

The CLI uses the configured principal and the same scope checks, sensitivity
ceilings, revision checks, and idempotency controls as HTTP. Writes additionally
require `--owner TENANT/PRINCIPAL` to match that principal exactly.

```text
agent people list --ceiling sensitive
agent people get PERSON_UUID --ceiling sensitive
agent people history PERSON_UUID --ceiling sensitive
agent people diagnose PERSON_UUID --ceiling sensitive
agent people link-existing --owner TENANT/PRINCIPAL --limit 100
```

Continue paginated results with their returned cursor. A changed cursor binding
requires a fresh scan. `link-existing` links only already stored, unambiguous
owner evidence; it does not mine old conversations or change belief authority.

Merge, split, undo, forget, and import accept a typed JSON `--request`, an
idempotency `--key`, and an explicit ceiling. First submit the preview request;
review its exact affected records and revision; then apply the same operation
ID. A changed source or revision requires another preview. Reuse the original
key for a transport retry rather than creating another operation.

Removing one fact keeps the person and original messages. It also clears the
fact's generated copies, including frozen context and derived replies. A pending
cleanup receipt stays visible in the person's detail until those copies finish
clearing. Existing rejection records retain suppression hashes without the
deleted subject or statement.

## Directory repair

[ADR-0121](adr/0121-people-holds-who-the-owner-knows-or-writes-to.md) limits
People to the people the owner knows or writes to. Data formed before it can
hold strangers named in mail, pronouns, and the owner's own addresses, and no
correspondence history. Run the one-time repair after deploying it:

```text
agent people repair-directory --owner TENANT/PRINCIPAL
agent people repair-directory --owner TENANT/PRINCIPAL --confirm
```

The first command is a preview and writes nothing. It reports:

- each person the repair would remove, with a reason of `unconfirmed`,
  `pronoun`, or `self`;
- the active people who would get an owner-confirmed name alias;
- how many facts would be deleted or unlinked;
- how many mail threads would have their generated summaries reset.

The preview is computed before the correspondence backfill, which can only
keep more people. To keep someone it lists, confirm or pin them in the People
browser. A listed duplicate of someone you know can be confirmed and then
merged with Repair identity.

With `--confirm` the repair runs three steps:

1. It projects the headers of retained mail from the last 90 days again, one
   message per transaction and without model calls. Bulk, excluded,
   suppressed, and unverifiable mail is skipped and counted.
2. It adds the owner-confirmed name aliases, recorded as owner assertions in
   a new People management session.
3. It removes each listed person that still qualifies, in its own transaction
   under the owner's mail and People locks.

Removing a person deletes the facts that mail formed only about removed people
through the governed delete. Each deletion also resets the generated summary
of the thread the fact came from and excludes that retained passage from
further formation. Facts you stated remain, unlinked. Mentions and address
endpoints remain, detached from the person, so a later reply adopts that mail
as history. An interaction with other people keeps them. The original messages
remain, no source is suppressed, and each removal appends a content-free
`people.directory_pruned` event to the repair's session.

The repair refuses to run while a People import is queued or running. A second
run changes nothing. Rerun it after restoring a snapshot taken before it ran.

## Duplicate people

[ADR-0125](adr/0125-duplicate-people-merge-on-decisive-evidence.md) merges
duplicates only on decisive evidence and asks about the rest. The maintenance
worker runs the pass every 15 minutes when its principal holds `people.write`.
To run it now:

```text
agent people dedupe --owner TENANT/PRINCIPAL
agent people dedupe --owner TENANT/PRINCIPAL --confirm
```

The first command previews and writes nothing. It lists the merges it would
apply and the pairs it would ask about. With `--confirm`, the pass does three
things:

- merges each provisional correspondent holding an address, number or handle
  the owner gave someone else;
- records a merge suggestion for each name match or address that only
  correspondents share;
- withdraws suggestions that no longer match.

The owner answers suggestions in the People browser under Needs review. A
dismissed suggestion, an undone merge, or a split keeps that pair apart for
good.

Entries named like a group, service or address, such as “Investment Team”,
stay until the directory repair runs again. Rerun `agent people
repair-directory` to preview and remove them.

## Historical imports

Name exact source sessions/accounts, inclusive start, exclusive end, exclusions,
record ceiling, and a finite monetary cap. Imports cover retained Chat records,
verified retained Email passages, and explicitly selected mailbox discovery.
Discovery limits and unavailable messages must remain visible as partial coverage.
Automatic Email learning retains its separate 90-day boundary.

Use `agent people import-status JOB_UUID --ceiling sensitive`, or reopen the
saved import in the native People browser. Source reads and successful analysis
have separate checkpoints. A record ceiling, budget pause, incomplete analysis,
and complete source coverage are distinct states. Resume retries incomplete
analysis on its original source without counting it twice. It cannot widen the
scope, discard an unknown charge, or ignore a changed identifier assignment.
Unknown charges require accounting reconciliation before further spending.

## Synthetic evaluation and activation

Use a committed clean checkout. Untracked input files also prevent an evaluated
build from being labeled as an unchanged commit. First run:

```text
agent eval people --check-corpus
```

A bounded development smoke comparison uses a new output directory and an
explicit allowance:

```text
RUN_LIVE_MODEL_TESTS=1 agent eval people --run --model-policy POLICY \
  --build-ref FULL_COMMIT_SHA --max-cost-usd APPROVED_AMOUNT \
  --development-case DEVELOPMENT_CASE_ID --output NEW_PRIVATE_DIRECTORY
```

Omit `--development-case` for the complete paired comparison, including the
unchanged ordinary-memory development and holdout benchmark. The three People
arms share source cutoffs, model, and recall budgets. Every provider reservation
is journaled before egress; failed runs retain their journal and partial results.
The full runner reports at least three repeats, direction/attribution/time
accuracy, abstentions, identity collisions, task and retrieval scores, inherited
ordinary-memory metrics, costs, and a paired confidence interval.

Email has independent development and holdout fixtures and a separate comparison:

```text
agent eval email-people --check-corpus
RUN_LIVE_MODEL_TESTS=1 agent eval email-people --run --model-policy POLICY \
  --build-ref FULL_COMMIT_SHA --max-cost-usd APPROVED_AMOUNT \
  --development-case DEVELOPMENT_CASE_ID --output NEW_PRIVATE_DIRECTORY
```

The Email comparison uses the production assessor for email-semantic@1 and
email-semantic@2, with synthetic sources and live communication connectors
disabled. It records the automatic 90-day boundary, attribution, direction,
commitment status, draft handling, assessment calls, and provider costs. Omit
`--development-case` for both complete splits and all repeats. Offline rescoring
uses `agent eval email-people --observations PRIVATE_OBSERVATIONS_JSON`.
Reply-decision regressions are measured separately. Publication also requires
paired private labels from the full ordinary Email benchmark: identical frozen
threads and snapshots, both original accounts, ranking, draft coverage, blind
style judgments, and semantic-memory judgments. Both policies must pass the
existing M26 scorer, with no regression in an individual paired case. Synthetic
labels remain pending. A completed comparison or smoke run alone is not an
activation artifact.

```text
agent eval email-people-evidence --run-directory COMPLETE_EMAIL_RUN
agent eval email-people-evidence --run-directory COMPLETE_EMAIL_RUN \
  --people-evidence MATCHING_PEOPLE_EVIDENCE_JSON \
  --baseline-labels PRIVATE_EMAIL_SEMANTIC_1_LABELS_JSON \
  --candidate-labels PRIVATE_EMAIL_SEMANTIC_2_LABELS_JSON \
  --output NEW_EMAIL_PEOPLE_EVIDENCE_JSON
```

The offline compiler rescores every repeat, checks settled provider costs, binds
both ordinary benchmark digests and implementation versions, and requires
reviewed Chat and Email corpora on the exact clean commit. It does not obtain
private labels, run a provider, or activate a deployment.

After a complete comparison, identify its immutable input bundle:

```text
agent eval people-evidence --run-directory PRIVATE_RUN_DIRECTORY
```

The returned `run_sha256` binds run metadata, People observations, ordinary-memory
observations, and the provider cost journal. After the authorized private owner
evaluation and boundary suites, prepare a separate JSON aggregate file containing
`schema_version` (1), `build_ref`, `run_sha256`, `boundary_failures` (0),
`owner_people_count`, `owner_task_count`, `owner_useful_correct`,
`owner_harmful_mixups` (0), and `evaluated_at`. Record measured results; the
publisher never supplies judgments or treats a missing evaluation as a pass.

```text
agent eval people-evidence --run-directory PRIVATE_RUN_DIRECTORY \
  --owner-acceptance PRIVATE_AGGREGATES_JSON --output NEW_EVIDENCE_JSON
```

Publication requires a clean matching commit, reviewed frozen corpora, complete
paired repeats, no unresolved provider charges, all quality floors, and matching
owner acceptance. The publisher rescores recorded observations and refuses to
overwrite an existing output. Publication creates a reviewable artifact; it does
not change the running configuration or authorize production activation.

Synthetic corpus structure and a completed runner are not activation evidence.
The current People labels remain unreviewed. Quality certification additionally requires
independent review of the exact fact spans, organization and person endpoints,
calendar precision, and all six product question categories. Structural corpus
validation and scripted-provider tests do not establish extraction quality.
The source labels were expanded before the first provider comparison; both
corpus digests must be recorded from the actual candidate checkout.
Quality certification also requires passing quality/boundary gates, the permitted aggregate
results of the private owner evaluation, and a version-bound publication bundle.
Obtain explicit source access and spending authorization before that private
20–30-person, 50-task evaluation. Keep its source material and judgments outside
the repository. Quality artifacts do not gate runtime availability. Release follows its authorized
exact-head review/CI process. No separate hosted evaluation environment is required.

### Pending physical cleanup

A forget request immediately fences current and historical People and belief
reads. Large histories return `cleanup_pending`; maintenance and receipt polling
continue persisted pages even with People disabled. Each page purges at most 256
People revisions, 256 beliefs, and 256 payloads of each generated-copy kind in a
separate transaction. Pending Email summaries/assessments, integrated episodes,
run messages, events/history, tool invocations, checkpoints, recall traces, and
derived knowledge are hidden before their content is physically removed.
Frozen prompt snapshots are redacted and invalidated in cached plans; affected
runs are cancelled, and the next run rebuilds its snapshot from eligible memory.
Original email messages remain intact. Keep restored data offline until every
newer receipt reports `completed`. Initial dependency discovery uses indexed
opaque references, including downstream generated documents, but the full graph
and metadata fence still share one transaction. Its contention/duration gate
remains pending.

Email source removal and session deletion create the same kind of content-free
cleanup receipts for cross-session People copies. Export every newer receipt
for an offline restore, including independent source-cleanup pages. Email source
removal continues to report `cleanup_pending` while these pages remain unfinished;
retry after maintenance completes to obtain the final `erased` status.

Affected running tasks retain a durable erasure fence. Late messages are
redacted, further execution is cancelled at checkpoint/tool writes, and terminal
finalization cannot restore a checkpoint. Late generated artifacts expire
immediately and keep the receipt pending until byte deletion completes. Artifact
references are drained in pages of at most 256; receipts stay exportable while
cleanup is pending. Forgotten artifacts cannot be retained as knowledge or have
their expiry extended. Byte cleanup waits until dependent knowledge rows are gone.
