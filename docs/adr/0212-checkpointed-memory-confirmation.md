# ADR-0212: Checkpointed memory confirmation

- Status: Owner-authorized protocol; implementation and release evidence pending
- Date: 2026-10-17
- Related: ADR-0178, ADR-0185, ADR-0194, ADR-0199, ADR-0207
- Scope: Isolated M33 collection and evidence admission

## Decision and authorization

The owner approved checkpointed continuation and authorized the operator to choose
the path to release after discussing a smaller production comparison. This amends
the earlier prohibition on reusing completed observations from failed collection
jobs. It does not change a failed job into a successful one or waive a quality gate.

Use 400 independently reviewed scenarios, both automatic and deliberate recall,
and sparse-control versus final hybrid retrieval through actual formation and
answering: 1,600 required answer observations. The seeded and dense-only comparisons
remain diagnostic evidence, retained under their original identities. Selection is
by configuration only, before scoring; no scenario or answer is selected for its
outcome. All eight families, held-out implicit comparisons, incidents, privacy,
recall floors, answer lift, family regression and numerical work limits remain.
Legacy release formats continue to require their original complete matrices.

## Immutable continuation

The failed parent collection, its output directory, every original row, formation
bank, provider ledger and scratch backup remain immutable. A separately identified
continuation names the parent hashes, original evaluated inputs, collector version,
amended matrix, approval and allowance. Imported rows retain their original bytes,
timestamps and measurement identity. They are never retagged to the collector's
source commit. New observations execute the same frozen runtime and configuration.
Collector and publication changes are verified separately; they cannot establish
equivalence for a changed evaluated runtime.

Before importing an observation, verify its original identity and captured runtime
configuration, corpus membership, unique matrix position, formation bank and
mechanically reconstructed evidence and usage. Preserve unselected diagnostic rows
and their charges too. Reuse is all-or-nothing under these checks, never conditional
on a score. A changed corpus, model, authority, runtime or effective configuration
refuses continuation before provider construction.

One exclusive coordinator owns a continuation directory. It writes a durable start
record before a slot can issue work, and a durable content-addressed result before
marking that slot complete. Restart skips completed slots. A started slot with an
unknown outcome cannot be sent again automatically. A complete retained result may
be reconciled after validation; conflicting bytes, duplicate slots, changed records,
missing files or incomplete writes fail closed. Read-only supervision never starts
or resumes provider work. Before the first new answer, the coordinator durably
pins one absolute deadline of at most 604,800 seconds and the collector protocol
digest. Reopening a segment cannot extend that deadline or silently change the
collector. Every segment uses newly admitted, distinct scratch databases; no
failed parent or predecessor database may be reset for continuation.

## Failure treatment

An operational interruption does not discard other completed slots. Record its
stage, bounded error class, original timing and ledger reference without prompts,
provider payloads or credentials. Stop new admissions when accounting or source
integrity is uncertain, while letting already admitted requests settle. Distinguish
that stop from actual allowance exhaustion.

A cancelled formation attempt that produced no formation-to-answer observation may
receive one explicitly recorded replacement preparation for the entire scenario.
Its failed bank remains ineligible for replay. The replacement uses one new bank
shared by all four required arms, preserving the original request policy and limits.
This permission is based on incomplete preparation, never on answer quality. Every
completed, cancelled and uncertain provider call from both preparations remains in
the aggregate ledger and the failure review. Security/protocol rejection, changed
inputs, exhausted allowance or another failed preparation do not qualify for an
automatic replacement. Reader answers are not selectively retried. An unresolved
reader slot or repeated preparation failure remains an explicit incomplete outcome
that cannot certify release; it does not erase unrelated completed progress.

Importing a failed parent's preparation uses a distinct `parent_failed` event,
not a new continuation start. Its explicitly pinned operator assessment retains
the terminal timestamp, pending-bank digest, diagnostic digest and uncertain ledger
entry. A bank request key and a provider-journal request key use different encodings;
the operator must identify contextual linkage honestly, without claiming an exact
key match. A complete original bank or any completed formation answer disqualifies
the scenario from replacement.

Do not manufacture a zero cost, latency or passing observation for an unknown
outcome. Preparation interruptions and replacement costs are reported separately
from completed answer measurements, with original durations and unknowns preserved.
No per-answer limit, production timeout or quality threshold is raised. Independent
difficult-failure human review includes the original interruptions and replacements.

## Budget and publication

Every prior ledger counts toward the unchanged USD 300 aggregate cap. Uncertain
reservations remain held until supported reconciliation; a new process or segment
does not renew the allowance. A continuation must reconcile all parent ledgers and
its own durable reservations before admitting a send. Admission cannot exceed the
smaller of its fixed allocation and the remaining aggregate allowance.
Each new reservation and settlement also names its durably started matrix slot;
concurrent workers cannot exchange that attribution. A sealed segment retains its
original bytes, and the next segment subtracts both reported and uncertain costs.

Publication uses an explicitly versioned reduced-matrix protocol. It verifies the
complete 1,600 positions, original provenance and all provider ledgers, independently
supplied corpus/failure reviews, and every remaining gate. It must report the
amendment and the distinction between evaluated runtime and collector identity.
Neither a retained partial run nor a synthetic recovery test is a quality pass.
Deployment still requires a verified input binding and an authorized release route.
The separate `general-recall-checkpoint-release@1` format retains the original
measurement identity and names the collector, amended scorer and parent lineage.
It shares the existing numerical gate calculations. Its scoring report alone
explicitly grants no activation authority; legacy deployment binding continues to
refuse this format until the separate publication and integration checks succeed.
The raw `recall-checkpoint-collection@1` compilation verifies all parent ledgers,
sealed segment ancestry, slot-attributed charges, original import bytes and every
completed result. Unobserved original charges, uncertain reservations, interruptions
and preparation replacements remain explicit review items. The separate
`recall-checkpoint-failure-review@1` must cover every such item as well as difficult
answer failures. Its reviewer attribution must come from an actual independent
human; a schema assertion does not authenticate that authorship. Publication
recompiles the originals and rechecks source and review inputs before writing its
final evidence file. It grants no unattended activation authority.

The offline `recall-checkpoint-input-binding@1` receipt requires the independently
pinned publication digest and compares the entire original input manifest against
the clean target Git tree and checkout. Collector additions are not silently
excluded. The receipt preserves the original measurement identity and grants no
activation authority; a changed target still needs separate integration evidence.

`scripts/collect_recall_checkpoint.py` invokes one segment with the frozen runtime's
interpreter and six separately pinned collector modules. Its immutable protocol
is distinct from each fresh segment admission, so new scratch names cannot renew
the protocol or deadline. The coordinator repeats zero-provider preflight, verifies
effective inputs, imports parent rows, records the approved preparation replacement,
and pins each worker pair to its budget segment before constructing paid adapters.
The raw compiler rechecks those worker admissions and their complete inventory.
The entry point refuses dirty source trees, expired admission, changed files,
nonempty or reused scratch databases, and ambiguous store/deadline initialization.
It never automatically invokes a successor segment. Checkpoint timestamps and the
absolute deadline use an injected clock from the existing composition root; elapsed
durations use its monotonic adapter. Temporary artifacts use exclusive filesystem
creation. Tests can pin timing without rewriting retained evidence or changing the
measured runtime.

## Required verification

- Crash after start, after result retention and after completion cannot duplicate
  provider work or lose a completed observation.
- A second coordinator, changed parent bytes, foreign/duplicate slots, traversal,
  symlinks, missing evidence and conflicting result bytes refuse admission.
- Outcome-blind matrix selection preserves all scenarios and both paths; legacy
  formats still reject a reduced or incomplete matrix.
- A permitted pre-answer formation replacement is bounded to one and retains all
  failed charges; completed-answer retries and failed-bank replay remain refused.
- Reconciliation includes out-of-order settlement, unknown usage and every earlier
  ledger, and never treats a shared stop flag as proof of monetary exhaustion.
- Original runtime measurements and banks keep their identities; offline scoring
  and admission reject changed evaluated behavior and fabricated human review.
