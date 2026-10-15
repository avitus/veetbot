# ADR-0206: Default deliberate memory search to the trusted session

- Status: Implementation decision under the owner's recall-release repair request
- Date: 2026-10-15
- Related: ADR-0178, ADR-0198, ADR-0199, ADR-0205
- Detailed design: `docs/plan/general-memory-recall.md`

## Problem

The retained failed confirmation on `74b14815` contains a mandatory incident
failure: a formed fact reached deliberate recall, but the reader requested
clarification. Its search used a different scope from its session. That difference
changes portability attribution and source recovery. It does not by itself prove
why the reader declined to answer. The production tool currently requires the
model to supply a scope even though the session already establishes one.

The same attempt also exposed diagnostic loss: a structured provider failure can
be overwritten by the evaluation wrapper's missing-final-usage exception. Its
uncertain charge remains reserved, but the original safe error code is lost.

## Decision

The model-facing `memory.search@2.1.0` makes `scope` optional. Omitting it resolves
the authenticated session's project through the existing persistence port. The
description directs ordinary searches to omit scope; an explicit scope retains
its existing meaning for intentional cross-project lookup. Preparation and tool
execution resolve the same default, so a prepared query cannot silently differ
from its later search. Failure to resolve the session fails the operation; it
does not invent a default project. The legacy direct tool keeps its required
scope and original version. No principal, sensitivity, portability, lifecycle,
confidence, source-recovery, or erasure requirement changes.

The evaluation budget wrapper passes a failed terminal event through to the
normal stream collector and journals only its safe classification, provider code,
parameter and HTTP status. It retains the full uncertain reservation and refuses
further paid calls. It adds no retry and cannot convert a failed observation into
successful evidence. Provider messages and partial content stay out of the journal.

## Verification and release boundary

Regressions cover default scope on multiple sessions, deliberate preparation and
execution parity, retained explicit cross-project lookup, and provider failure
identity with uncertain accounting. These mechanism tests do not establish reader
quality or close the failed incident gate.

The owner requested completion of the remaining gates before merging PR #163.
The original failed attempts and their identities remain immutable. Aggregate
reported and held costs are USD 55.913546; a fresh complete confirmation may use
at most USD 240 after rechecking the ledger, within the existing USD 300 cap.
Changed evaluated inputs require new matching measurements, final CodeRabbit
review, exact-head hosted verification and signing before that paid run. All
quality, cost, latency, independent human review and publication requirements
remain unchanged. No optional channel or unattended dreaming is activated here.
