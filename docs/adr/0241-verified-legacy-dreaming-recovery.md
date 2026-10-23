# ADR-0241: Verified legacy attribution recovery for dreaming

- Status: Accepted — owner authorized implementation on 2026-10-22
- Date: 2026-10-22
- Amends: ADR-0202
- Related: ADR-0121, ADR-0169, ADR-0174
- Governing design: `docs/plan/memory-reconsolidation.md`

## Decision

The owner wants retained original memories to receive the same supervised
reconsolidation treatment as newer memories. A metadata-only investigation found
that deterministic record identities and retained cleanup receipts can establish
missing attribution without restoring deleted content. Directory cleanup and
source deletion must not be mistaken for individual owner forgetting decisions.

Add an owner-bound operator recovery command with a preview and explicit confirm.
A bounded, content-free manifest must cover every unknown erased head and match
the existing cutover digest. Never overwrite known attribution, admit an unknown
live head, or infer an empty footprint for an unsupported record kind.

For a revision-one memory link, recompute its deterministic identifier from an
owner-owned person identity, belief identity and role. The belief must survive
or occur in a retained applied erasure receipt. For a revision-one source,
recompute its owner/session/event identifier and require a matching applied
source-cleanup receipt and a deleted-session tombstone at that receipt's creation
time. A revision-one mention must match its source and exact span identifiers;
its source must be one of these proved erased sources. Later revisions of these
attribution-bearing records refuse recovery because their original identifiers
cannot prove subsequent attribution changes. Existing record kinds that never
carry an attribution footprint receive the schema-defined empty footprint.

Under the existing owner/People locks, verify all proofs before writing any
metadata. Refuse an active reconsolidation lease. Atomically retain the original
cutover fields with a recovery timestamp and manifest digest, fill only missing
opaque attribution, and restart full inventory from zero. Keep change cursors,
spend, retries, groups, decisions, daily schedule and original memories unchanged.
The full-bank path still applies every existing source, visibility, sensitivity,
attribution, correction and erasure check. New uncertainty blocks it again.
Exact retries are idempotent; changed manifests and incomplete recovery refuse.

The command makes no provider calls and restores no revision payload, source
text or deleted belief. Normal daily supervised previews resume on eligible
originals; application still requires individual owner review. Unattended gates,
evaluation data, numeric floors and historical observations remain unchanged.

## Verification

Require red-green coverage of complete recovery, immutable history, old-source
admission and matching erased-source refusal, rollback, owner isolation, forged
and incomplete proofs, later revisions, live unknown heads, active leases,
preview, retries, inventory restart and preserved budgets/scheduling. Exercise
both repository adapters and the CLI boundary before production recovery.

The read-only production metadata snapshot validated all 469 uncertain erased
heads, including all 142 attribution-bearing heads, against the recovery
validator. No content was restored and no production write was made. Private
identifiers and the operator manifest remain outside the repository. Regression
coverage includes original evidence reaching proposal and verifier review; the
new recovery command does not itself run a provider or enable unattended use.

## Delivery

After the reviewed release is deployed, use `agent memory reconsolidations
recover-legacy --owner TENANT/PRINCIPAL --manifest PATH` to revalidate the private
manifest against current production metadata. Repeat with `--confirm` only when
the complete preview succeeds. Verify that dreaming status has no active cutoff
and no attribution blocker, then run a supervised preview and inspect its result.
A refusal leaves the cutover and attribution unchanged; investigate the changed
metadata rather than bypassing validation. Retain the manifest and result in the
private operational evidence directory.
