# ADR-0307: Aggregate browser operation evidence without page content

- Status: Accepted under the owner's browser work-package continuation on 2026-11-10
- Date: 2026-11-10
- Related: ADR-0156, ADR-0158, ADR-0306
- Detailed design: `docs/plan/browser-automation.md`, `docs/plan/bootstrap-and-composition.md`

## Decision

Add `agent run browser-report --hours N [--json]` to the existing operator
reporting surface. The default window is twenty-four hours; positive windows
are bounded to ninety days. The PostgreSQL report is scoped to the configured
tenant and principal and runs in a read-only, repeatable-read transaction with
a ten-second statement timeout. It does not create runs or browser actions.

Read the newest 10,000 terminal `browser.navigate`, `browser.observe`,
`browser.act` and `browser.upload` tool events in the window. One extra row
detects selection truncation. Completed, failed and uncertain operations are
counted separately. Policy refusals before execution and unfinished operations
are outside this report's population. The query selects only closed tool and
event labels, diagnostic presence, and at most 16 KiB of diagnostic JSON per
operation. It never selects arguments, results, page content or correlation IDs.

Validate each entire diagnostics-v1 object before aggregating it. Missing,
invalid, oversized and truncated diagnostics remain explicit counts. An invalid
object contributes no phase or duration values. Report operation and phase
counts, median, 95th percentile and maximum elapsed seconds, grouped only by
closed tool, phase, placement, outcome and failure enums. Nested phases overlap;
their durations are never summed into operation wall time. No traffic is an
empty population, not a successful reliability result.

This report reuses the durable event log and its existing access, retention and
deletion rules. It introduces no new metrics service, raw trace store, egress,
retention policy, alert threshold, retry decision or browser authority. It is
bounded operational evidence, not a live-site benchmark or a complete W2 exit.

## Verification

The composed-report regression first failed because PostgreSQL composition
provided no browser report. Fifteen focused unit cases and six real-PostgreSQL
cases now cover empty and mixed populations, exact distributions, malformed
and unknown diagnostics, secret canaries, row and byte bounds, time windows,
tenant/principal isolation, and database-enforced read-only transactions.
CLI cases cover the reserved command, bounded arguments, text/JSON output and
content-free failures.

The local Linux VM static partition passes 10,008 tests with three skips.
The integration partition records 1,092 passes and twelve skips, with one
pre-existing role-policy test blocked by the disposable runner's network
topology. Aligning that runner with CI's loopback database access makes the
affected partition subset, including all six report cases, pass all 21 cases;
no test or database security configuration was changed.
The combined Apple suite passes 1,190 core tests and four evidence tests in
the local macOS VM. Lint, typing, documentation and citation checks pass.
Delivery and live-site acceptance remain separate from this construction evidence.
