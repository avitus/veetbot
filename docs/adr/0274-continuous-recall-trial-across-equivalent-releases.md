# ADR-0274: Continue an active recall trial across equivalent releases

- Status: Accepted for implementation
- Date: 2026-10-31
- Related: ADR-0199, ADR-0218, ADR-0220, ADR-0223, ADR-0246, ADR-0269, ADR-0272

The proposal circulated as ADR-0273 before the Cockpit decision occupied that number on dev.

## Problem

The bounded recall validator compares the running build revision with the approved
deployment revision before checking the input census. Even a release that changes
only operator documentation can therefore send owner chats back to sparse recall.
The owner wants ordinary production updates to preserve a valid trial and jobs.

## Decision

Add a release-specific handoff at deployment. Begin with releases whose entire
existing ADR-0199 census is identical. A small diff, an unchanged directory name
or a developer label is insufficient. Compare the actual staged artifacts and
effective settings, schema, overlays, encoder assets, role permissions, and
reader, formation and indexing configuration. Compare the release identifier
separately. Unknown differences take the existing sparse path. Future exceptions
for changed runtime inputs require a separately justified compatibility rule.

A privileged verifier independent of the candidate deployment validates the
currently active admission and prepares a receipt for the exact reviewed target.
Bind it to the prior evidence digest, target revision, inventories, effective
dependencies, verified rollback, and final-head review and CI. Keep the original
trial, owner, eligible roles, review history, budget, withdrawal state, expiry
and seven-day limit. Deployment may never renew a trial.

Stage the receipt before replacing processes. Serialize the handoff with owner
renewal and withdrawal, recheck the prior admission immediately before cutover,
and atomically publish the target-specific binding. Each new role validates its
own installed inputs. A stopped, expired or superseded admission cannot be
revived by deployment or rollback. During a rolling deployment, attribute each
eligible run to the binding used by its process. Record actual cutover times.

Verify delivery, public identities, strict semantic composition in the four
eligible roles and jobs in their five configured roles. If equivalence or
delivery cannot be verified, serve sparse recall, keep jobs available, and
record the specific cause. Preserve all earlier receipts.

## Acceptance before release

Exercise the deployment entry point with a documentation-only revision change
and verify semantic retrieval before and after cutover with the original expiry.
Exercise changed runtime bytes, schema, assets, access settings, incomplete
receipts, concurrent stop or renewal, partial role cutover and rollback. Verify
sparse operation and jobs in failure cases. Retain the existing exact-binding
refusal tests. Production adoption requires a reviewed release and readback.


## Implementation evidence

The isolated release rehearsal uses the deployment controller, real bounded
restart validation, application composition, local test encoder, run-bound
query preparation, memory recall traces and stored job execution. A documentation
update keeps semantic retrieval and the original trial bytes. A staged permission
change and a process whose actual scopes differ both use sparse recall and
complete the saved job. The Linux deployment fixture covers ordering and
fallback at the shell entry point. Per-attempt receipts, source mutation,
withdrawal, renewal during cutover, original-build retirement, and expiry have
regressions. Public production readback follows the first authorized release.
