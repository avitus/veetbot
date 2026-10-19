# ADR-0217: Bounded recovery of reconciled evaluation interruptions

- Status: Accepted by the owner; implementation and paid admission remain separately gated
- Date: 2026-10-18
- Related: ADR-0194, ADR-0207, ADR-0212
- Historical identity: ADR-0213 in the frozen evaluation branch; renumbered for integration without changing retained originals.
- Scope: Two retained operational interruptions in the isolated M33 evaluation

## Problem

Checkpointing preserved 411 of the required 1,600 observations. The replacement
preparation for `dev-decisions-03` was cancelled before an answer and could not seal
its formation bank. Another worker completed a planning turn and `memory.search`
for `dev-decisions-04` deliberate/hybrid, then the shared accounting halt refused
its next provider admission. The latter request has a runtime start event but no
provider reservation or send. Neither interruption produced a final reader answer.

Offline reconciliation verified the original archives, event chain, pending bank,
provider journal and terminal runtime events. The formation bank contains two
completed replies and an unresolved third reservation. Its audited fallback formed
no memories and scheduled a transient recovery; this is not a complete paired bank.
The reader has one completed provider turn with known usage, no outstanding reader
reservation and a terminal failed checkpoint. These records cannot reconstruct a
missing final answer or its completed-answer latency. A local formation timeout is
consistent with the cancellation, but its precise cause remains unproven.

ADR-0212 permits one preparation replacement and prohibits selective reader retries.
Its existing permissions therefore do not cover another preparation or restarting
this terminal reader. Repeatedly invoking the current collector cannot resolve that
boundary. Retained progress must not be discarded or relabeled to bypass it.

## Decision

Authorize one bounded recovery of these two identified operational failures:

1. Permit **one final replacement preparation** for `dev-decisions-03`: the third
   preparation overall, counting the failed parent and failed first replacement.
   It uses the identical frozen runtime, model, request policy and timeout, a new
   bank and fresh disposable databases. No formation answer exists for this case.
   Its four required arms must share the newly completed bank. Both failed banks
   remain failed and cannot supply a successful replay.
2. Permit **one replacement reader execution** for `dev-decisions-04`, deliberate,
   formation, hybrid. Eligibility requires the exact reconciled evidence that no
   final answer completed and the follow-up was refused before provider admission.
   The original planning turn, search, failure, timings and cost remain retained.
   This is a separately identified operational retry, not a continuation of the
   terminal runtime or a reconstructed answer. Its unchanged complete formation
   bank and the other three arms retain their original identities.
3. Do not retry a completed answer, a quality failure, a security/protocol rejection
   or an unknown dispatched reader request. Eligibility is determined from process,
   event and journal evidence before inspecting or scoring answer content. The
   permission is limited to these exact two interruptions; it does not create an
   unlimited retry policy for later failures.
4. If either recovery fails, stop further paid admissions and retain the failure.
   Do not automatically request another exception, extend a timeout, switch models,
   discard a scenario or start a fresh full run. Report the bounded recovery as
   unsuccessful with its remaining budget and evidence.

Keep all 411 completed observations unchanged and collect only the 1,189 remaining
positions. Neither failed attempt counts as a successful observation. Publication
must retain both the failed attempts and the separately identified replacements,
including their selection rule and all wasted or uncertain work. This amendment
relaxes the prohibition on these two operational retries, not any numeric gate.
It makes no promise that the next preparation will finish or that quality will pass.

## Unchanged limits and admission

The 400 scenarios, both paths, sparse/hybrid comparison, 1,600 complete answer
observations, all numerical quality/security/per-answer limits, original runtime
and effective inputs remain required. The original absolute deadline remains
2026-10-25T00:27:32.501898Z. Independent human review must cover the additional
attempts and uncertain costs as well as difficult answer failures.

All nine existing ledgers remain accounted for. Reported and uncertain exposure is
USD 138.14496425 against the USD 300 aggregate cap. The existing USD 150 continuation
allocation has USD 147.76386 unused; this decision creates no new allocation and
does not release any uncertain reservation. Final admission must freshly reconcile
all predecessors and retain the smaller remaining allowance. Failed attempts do not
become zero-cost or instantaneous observations.

Implementation must use append-only recovery authorizations bound to the exact
parent/segment/result/journal digests. It must not edit the sealed plan, previous
protocol, deadline, event history, banks or database backups. Any revised collector
protocol must explicitly link its predecessor and preserve the original deadline
and budget anchor. Fresh workers cannot reuse failed scratch databases. Source and
runtime identities remain separate; deployment equivalence is still unestablished.

Before any paid send, verify the negative admission paths, refusal of a second use
of either exception, unchanged completed-row bytes, accounting of all predecessor
charges, exclusive ownership and crash-safe recovery. Complete required source
verification and freeze/retain the new operator inputs. Existing unchanged runtime,
native and CI evidence stays attached to its original revision.

## Implementation

The append-only `recall-bounded-recovery@1` manifest binds the retained owner
response, offline assessment, all original state artifacts, exact failed event
positions, journals and segment reports. `bounded_replacement` events consume each
exception once. Reopening can complete interrupted authorization publication but
cannot resend a started or failed replacement. Completed rows remain byte-identical.

The separately pinned `recall-checkpoint-operator@2` links its original protocol;
only collector identity and explicit recovery inputs may differ. The immutable
protocol transition binds the original deadline and first successor segment, so
historical worker admissions retain their own protocol. The raw lineage compiler
includes the authorization, failed work and new attempts in the required human
review. These checks grant no quality pass or deployment authority.

## Alternatives assessed

- Reopening the current state does not authorize these retries and cannot create
  the absent answer or third formation completion.
- Promoting the pending bank or using its fallback as a complete measurement lacks
  the missing provider usage and does not establish the existing per-case cost gate.
- Counting missing answers as completed or excluding their positions changes the
  approved coverage and measurement contract.
- Changing the timeout, model or evaluated runtime invalidates the current input
  equivalence and is a separate repair proposal, not this recovery.
- Keeping the current protocol leaves collection stopped at 411 complete rows.

## Release boundary

The owner explicitly approved the two bounded retries. This decision is not
launch clearance, a quality pass or activation authority.
No new pull request, production hold or change to another development branch is
authorized. The operator implements and verifies this narrow recovery on the isolated memory
branch. Paid work remains stopped until its source and operational admission pass.
