# ADR-0137: Stable tool-output admission before context pressure

- Status: Proposed (owner-requested repair, 2026-09-27)
- Date: 2026-09-27
- Related: ADR-0020, ADR-0021, ADR-0135; engineering plan Sections 8.3, 10.1, 11, 18.4
- Detailed design: `docs/plan/tool-system.md`, `docs/plan/context-engine.md`

## Evidence and decision

The post-deployment Chat run inspected on September 27 retained about 53,218
estimated tokens of old tool results. Its tool-result allowance was 60,512.
Six fetched pages increased the total from 57,030 to 134,343 estimated tokens;
the largest page had 139,764 characters. Assembly replaced thirteen old results,
including all eight carried results. Their carried history shrank from 54,433
to 1,883 estimated tokens. The next OpenAI cache read fell from 45,848 tokens to
4,180 despite an unchanged prefix hash and epoch. A read-only reconstruction
using the deployed builder reproduced these changes. The events contained no
pressure report because the runtime returned early when the trimmed request fit.
No private page text or credentials are retained in this record.

The per-tool output acquisition limits were incorrectly doing double duty as
inline prompt allowances. A schema-valid one-megabyte page could enter the log
whole and later force the builder to rewrite the oldest tool results.

1. Add `output.inline_maximum_bytes` to the tool limits: 4,096 bytes by default,
   at least 1,024. The existing pipeline captures large results as artifacts
   before persisting their bounded head/tail excerpts. The smaller of the inline
   and per-tool limits bounds serialized model-visible content, including JSON
   escaping and references. Full capture still uses the original per-tool hard
   ceiling. Oversize partial captures remain explicitly labelled as partial.
2. Persist the excerpt as optional `ToolResultItem.context_content`. Keep
   canonical content intact within the original per-tool limit for provenance
   checks and machine callers. Both context builders select a copy of the
   excerpt before estimation/rendering, without duplicating canonical text in
   the request. Older items with no alternate content remain readable.
   Replay the excerpt without recalculating it against growing history.
   For carried legacy results, select the same deterministic excerpt on each
   assembly and link the original event; do not rewrite stored events or
   checkpoints. Record that normalization as `legacy_tool_excerpts`.
3. Emit `context.budget.pressure` for every yield, including successful yielding.
   Record `fits`; compact only an overflowing, compactable request.
4. Keep context class caps, suffix selection, trust labels, owner artifact access,
   and the provider cache protocol unchanged. Full artifacts remain available
   through the existing owner-scoped download API; there is no new agent artifact
   reader or tool authorization surface. Structured control data remains intact.

This refines the existing excerpt mechanism rather than enlarging context
budgets. No schema migration or dependency is needed. Legacy requests incur a
one-time prefix change after deployment. Long runs can still exceed aggregate
budgets and legitimately rewrite history; cache hits are not guaranteed. Runs
resumed mid-flight with old active results still obey the aggregate pressure
policy. The 4 KiB excerpts trade inline detail for stable context and can omit
relevant middle passages; the full captured output remains downloadable.

## Verification

Regression tests fail on the prior implementation for the large web batch,
legacy history normalization, and missing pressure event. The synthetic workload
uses eight carried results and subsequent batches including a 139,764-character
page; it uses the real web tool, invocation pipeline, durable event projection,
context builder, and OpenAI payload translator without production content or
provider calls. It verifies byte-identical prior wire history and replay.

Additional coverage checks complete artifact contents and ownership, a composed
web run with an operator-overridden limit, Unicode and JSON escaping, exact
threshold boundaries, missing artifact storage, the unchanged partial-capture
hard ceiling, and fitting/overflowing pressure events. The full suite also guards email
body continuation and header provenance across refresh sessions. These are deterministic
repair checks; they do not measure production cache-read percentages. The small
calculator probe in ADR-0135 did not exercise this failure mode. Production
confirmation must use a new qualifying Chat run after this repair is deployed.

The pre-fix batch reproduction was
`uv run pytest -q tests/unit/test_tool_history_admission.py -k batches --tb=short`:
it failed because `yield_steps` contained `tool_results`. The runtime regression
also failed because a fitting request emitted zero pressure events after yielding.
After repair, the focused context, cache, web, artifact, runtime and configuration
suites passed 205 cases in 8.67 seconds (9.48 seconds command wall time). The full
repository gate and production follow-up are reported with the submitted revision.

The first full gate caught two email continuation regressions: replacing the
canonical result invalidated source receipts that parse its JSON. Separating the
persisted context excerpt from canonical content fixed both without changing
email validation or weakening its tests. The expanded compatibility and repair partition passed 254 tests in 17.78
seconds (18.76 seconds command wall time). Complete stdout/stderr structures
are explicitly protected from the smaller model-context allowance.
