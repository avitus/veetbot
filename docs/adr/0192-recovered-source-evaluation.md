# ADR-0192: Distinguish recovered source evidence from formed facts

- Status: Accepted for implementation; release admission remains gated
- Date: 2026-10-12
- Related: ADR-0178, ADR-0185, ADR-0189
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

The initial release scorer requires rendered evidence to be a subset of ranked,
candidate and formed atoms. That is correct for atomic retrieval. It cannot
measure the separately approved source recovery experiment: a compressed fact
can retrieve its original message, which contains a required detail absent from
that fact. Crediting the detail as a formed atom would hide the formation failure.
Counting only labeled atoms also omitted unlabeled distractors from reported noise.

## Decision

Keep atomic stages unchanged. Add transient, content-free source-read observations
to `RecallResult.stages`: selected original, source session/event and exact text
digest. These describe admitted reads before excerpt budgeting. An excerpt counts
as rendered only if its exact text and provenance survive into a recall block in
the actual reader request. Stage metadata is excluded from result serialization.

The evaluator matches each source to the authored synthetic history after retrieval.
It records source witnesses separately from formed/candidate/ranked/rendered atoms.
Every source required by an authored claim must match; a partial source set does not
support a multi-source claim. A recovered source counts toward top-twenty recall only
when its actual selected original was among the first twenty ranked originals.
No label, answer or source-selection hint enters the query planner or reader.

Independent search results cannot borrow each other's best rank. The runtime
diagnostic preserves every query's candidate and ranked IDs and unions candidates,
but certifies a global ordering only when all nonempty observed orders agree.
Different orders produce an explicit uncertifiable-rank marker, no global atomic
top-twenty credit and unknown source-anchor ranks. Source visibility and rendering
remain separately measurable. A future runtime merge must record its actual
combined ordering before those observations can supply release ranking evidence;
the evaluator must not invent that ordering after seeing expected labels.

The context builder now records its existing assembly-priority order at the point
it constructs the reader request: admitted deliberate blocks from newest to oldest,
then automatic base and delta. Each whole block contributes its observed ranked
prefix; stable deduplication gives one global twenty-original prefix. This records
the current block-priority policy, not a new cross-query relevance fusion algorithm.
Only blocks retained in that request contribute. Context pressure, excerpting or
an unknown legacy receipt cannot acquire ranking credit. The transient request
receipt stays outside provider serialization; the evaluator retains it explicitly.
The compiler checks block identities, exact rendered digests, their original ranked
prefixes and the combined prefix. Without this runtime receipt, differing orders
remain uncertifiable. Candidate and rendered stages remain separately measured.
When a delta already obeys the base exclusion, assembly preserves its original
rendered block, including its timestamp and retrieval-policy stamp. Rebuilding
that block had made actual reader-visible delta evidence unidentifiable to the
observer. Partial transformations without an exact matching receipt remain
uncertifiable. Neither repair changes which originals are selected.

`general-recall-release@2` binds expected source IDs and text digests into the scenario
manifest and observed source witnesses into each measurement. It reports atomic
formation and ranking separately from source-supported ranking and rendering.
Required-evidence and answer success use the union without counting a claim twice.
Each row must supply a measured noise count including unlabeled rendered items.
This includes distinct People records and revisions. Until a People context item
has a mechanically verified mapping to an authored required claim, it counts as
unlabeled; a matching name alone earns no evidence credit.
Source witnesses remain bounded by the existing three-per-search and four-search
limits. Unknown, duplicate, conflicting or unbudgeted witnesses fail verification.

The raw-observation compiler derives labels from recorded originals and their
source-event mappings, and answers from the actual structured reader response.
It ignores diagnostic aggregate scores. Rendered statements and recovered quotes
must occur in the recorded reader block; blocked placeholders supply no evidence
and count as noise. Missing output fields remain answer failures even when their
expected value is null. Recorded provider completions and verified formation
replays must account for every attributed usage record. Sparse fallback cannot
masquerade as a successful dense or hybrid measurement, and differing per-search
rankings cannot supply a fabricated global order.

Version one rejects these new fields when populated and retains its atomic stage
rules. Both versions retain all boundary, invalid-current, quality, cost, latency,
independence review and receipt requirements. Frozen M16 and M32 artifacts are
unchanged. This decision changes measurement of the approved recovery mechanism,
not the release thresholds or source visibility rules.

## Evidence and limits

Offline composition uses the existing CLI entry’s factory exports, which delegate
concrete adapter construction to bootstrap. Evaluation clocks come from its
determinism adapters, including the
elapsed worker timer. Runtime/application imports remain restricted to their
ports and domain controls, and production modules cannot import the evaluator.

Regression tests reproduce a required detail compressed out of a stored fact,
successful recovery through the governed runtime tool result, an excerpt dropped
for budget, and an entire recall block dropped before the reader. Exact-source
and digest checks reject mismatches. The release matrix tests distinguish zero
formed recall from complete source-supported recall and count unlabeled noise.
These fabricated tests are not activation evidence.

The publisher still relies on reviewed corpus labels and trustworthy runtime
instrumentation. Digests establish identity, not semantic entailment or independent
human review. Actual production-shaped comparisons and reviewed release receipts
remain mandatory before activation.
