# ADR-0310: Retain both deliberate memory searches

- Status: Proposed under the owner's 2026-11-10 recall repair instruction
- Date: 2026-11-10
- Related: ADR-0195, ADR-0208, ADR-0309
- Detailed design: `docs/plan/general-memory-recall.md`, `docs/plan/context-engine.md`

## Context

The full production-snapshot audit reproduced a remaining retention failure.
The first deliberate search delivered a required supporting fact. A second
search filled the deliberate allowance, displacing the first whole result from
the final model request. The automatic reserve did not recover that fact because
the original task ranked it below other evidence. Better query wording alone
cannot guarantee continuity across two successful searches.

## Decision

Newly pinned `memory.search@2.4.0` bounds each deliberate result to seven items
and 750 tokens. Both permitted deliberate searches can therefore retain their
whole, independently validated receipts within the existing fifteen-item,
1,500-token deliberate allocation. The five-item, 500-token automatic reserve
and combined twenty-item, 2,000-token ceiling remain unchanged. Unused space,
including the remaining item after two full seven-item results, is available to
fresh automatic recall. Assembly does not truncate evidence or invent receipts.

Focused searches reserve identity context and one relationship per focal person
before beliefs, then use spare capacity for additional relationships, commitments
and recent interactions (`+focused-people@1`). They do not reserve extra slots ahead
of requested facts: in the audit, generic received-email entries and repeated
relationships displaced a residence fact from the narrower window.
Older query versions keep their existing People reservation. Explicit owner
overview pages keep their first-person completeness and cursor rules; discovery
still considers the original identity candidate set. Recent interactions remain
eligible and are delivered whenever space remains.

Focused 2.4 queries also require a complete stored relationship label before
fallback role discovery selects a person. A query about a portfolio must not
become a query about its only recorded portfolio CEO just because the two share
the word "portfolio". The existing bounded, governed discovery first establishes
an unambiguous person; an additional bounded read checks that a visible, resolved
direct edge names a role whose whole lexemes occur in the query. Its qualifier,
or a non-generic predicate when no qualifier exists, supplies that label. No
owner-specific vocabulary or guessed role is introduced. No matching label or
an overflowing edge page abstains and leaves ordinary recall available. This
tightens ADR-0208's broad overlap fallback for the new focused query version;
explicit person selections, name matches and older query versions keep their
semantics. Discovery retains the original candidate and exclusion bounds.

A later full cohort exposed repeated searches returning identical evidence with
different observation timestamps. Counting both blocks displaced a previously
delivered automatic fact. New current-search receipts therefore also bind a
digest of the complete rendered block with only its outer `as_of` timestamp
removed. Historical queries do not produce this digest. Retained-source
validation recomputes it from the authoritative trace before assembly may use
it to keep only the newest identical block. Every other byte, including policy,
People context, corrections and provenance, must match. The original receipt and
content hashes remain unchanged; no evidence is excerpted or reconstructed.
Only validated, actually retained blocks suppress a duplicate, so an invalid
newer result cannot displace an older valid one. The reclaimed allowance goes
to fresh automatic recall under the existing shared caps.

Identity discovery still uses twenty candidates and the original hard
exclusions, independently of this presentation limit. Returned-belief exclusions,
source validation, sensitivity ceilings, mandatory corrections, phase clocks,
run-wide search quotas and erasure protections are unchanged. Preparation uses
the same versioned query as execution. All older tool versions remain registered
with their original allocations; existing chats keep their pinned version.

This explicitly amends ADR-0309's per-search allocation for new pins. The tradeoff
is fewer results from one deliberate query in exchange for retaining evidence
from both allowed queries. Reader guidance keeps searches focused on missing
facts; full runtime audits must check that the smaller window still delivers the
required facts. No milestone or activation gate changes.

## Verification

The new context regression derives result sizes from the current registered
tool, fills both deliberate results, and checks that both facts, fresh automatic
evidence and mandatory corrections survive within the unchanged shared caps.
Before implementation it failed on the earlier fact; the other 26 tests in its
static atom passed. The command was `circleci testsuite run static --run-tests=all
-t '^tests/unit/test_context_memory_budget.py$'`, with discovery and execution
routed through the local Linux sandbox. This was a behavioral red test.
The People allocation regressions separately reproduced a fifth belief being
crowded out by interaction entries and repeated relationships (two failures,
65 existing passes).
The same regression also covers commitments (one failure, 68 existing passes
before that final allocation repair).
It also checks that spare capacity still delivers history and that the identity
header survives.

The duplicate-block contract reproduced one failure with ten existing passes
using `circleci testsuite run contract --run-tests=all
-t '^tests/contract/test_retained_recall.py$'` in the same Linux sandbox. Both
successful searches returned identical facts at different times; assembly spent
both allowances. The repaired contract also covers legacy and historical
queries, changed evidence, and a forged newer digest. All sixteen cases passed.
A controlled replay of the failing production-snapshot queries retained the
previously lost automatic fact through the final model request.

The qualified-role regression first reproduced three failures with 72 existing
passes in `tests/unit/test_people_context.py`: portfolio, tennis and estate topics
incorrectly selected people through partial labels. The same shared contract
runs against PostgreSQL, alongside focused and legacy role-discovery boundary
cases. A controlled replay of the exact failed portfolio query delivered the
missing addition after the repair, without changing the model's query wording.
Final boundary review added expired and future label cases sharing a still-valid
relationship belief. Both failed before the label read explicitly supplied the
current validity instant (107 existing passes). The new read uses `as_of` when
supplied and the current clock otherwise, retaining the separate `known_at`
coordinate. Those cases run in both adapters as well.

Tool-version coverage preserves the old allocations and checks the new
identity-discovery ceiling. The preparation/execution contract includes all four
planned versions. Final audit and verification evidence is recorded in the
detailed recall design; private production data and model receipts remain
outside the repository.
