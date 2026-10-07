# ADR-0157: Bounded browser observation expansion

- Status: Accepted under the owner's ADR-0156 implementation instruction
- Date: 2026-09-30
- Related: ADR-0058, ADR-0129, ADR-0130, ADR-0137, ADR-0156
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Extend `browser.observe` with exactly one optional continuation: `after`, an
opaque element reference in the current observation, or `cursor`, an opaque
provider-issued continuation for a candidate window. An empty request still
refreshes the page. No selector, script, offset, profile, or URL is accepted.
Adapters without expansion support refuse explicitly; they never silently
restart the page. Hosted expansion uses the existing authenticated observe
route, current lease, profile binding, lock, and error handling.

Each observation captures at most 256 visible controls from a window of at
most 4,096 candidates. A trusted isolated-world selector acquires at most
4,097 handles, including one lookahead, before visibility and metadata work.
Traversal includes open shadow roots, in depth-first order with shadow children
before light children; closed roots and frame documents are outside coverage.
Candidate offsets are bounded to 65,536. Coverage reports offset, scanned count,
continuation, and whether that terminal scan limit was reached. Handles outside
the retained observation are released. The selector is registered before pages
are created, using Playwright's isolated `content_script` world; it is not an
agent-facing JavaScript capability.

An `after` continuation validates the retained node at its candidate position,
then resumes after it in the live traversal,
not after the provider's 256th control. This matters when context admission
shows only a prefix. Detached or moved anchors, obsolete references, other-session
cursors, navigation, and failed capture cannot authorize continuation. Expansion
produces a new revision and replaces all action references. A cursor resumes
the next candidate window on the current document; every successful observation
replaces it. Pagination is a sequence of live snapshots, not a frozen DOM or a
claim of complete coverage of a changing page. Reordering before a cursor may
repeat or omit controls; the agent can refresh. No action is retried or dispatched
by expansion.

The semantic model projection includes continuation after the last contiguous
control it actually exposes, or the provider cursor when the whole window is
shown or contains no visible controls. A gap caused by an individually oversized
record is disclosed and must not silently advance past that record. The complete
artifact remains available. All metadata and continuation bytes count against
the existing inline budget. Legacy observations without coverage retain their
existing projection.

Control names now consult `aria-labelledby` and native HTML labels as well as
existing name sources. Input values remain excluded. The full independent label
facts and live task-grant classification continue to apply; a readable name
does not grant permission. This is bounded label support, not a claim to implement
the entire accessible-name algorithm.

Add a deterministic synthetic component task suite with independently checked
website state, including dense and hidden controls, multilingual text, shadow
roots, forms, and changing pages. Drive discovery through model-sized projections.
Report only scenario identifiers, outcomes, operation counts, durations, and
expected/observed synthetic effects. This is scripted browser component evidence;
model calls and live task quality remain unmeasured. Existing policy, hostile-page,
and profile isolation tests remain mandatory and are not replaced by this suite.

## Verification and limits

Use red-green checks for continuation through the tool and hosted boundary,
stale/foreign continuation refusal, bounded acquisition, no skipped projected
controls, empty candidate windows, and stable action handles. Run the mandatory
Chromium partition and final sidecar aggregate. This delivers part of W1 and W3;
relevance ranking, region/frame expansion, live task quality, and W2/W4–W10 remain
separate work. No engineering-plan security or approval requirement changes.

Playwright's [selector API](https://playwright.dev/python/docs/api/class-selectors)
and [custom selector documentation](https://playwright.dev/python/docs/extensibility)
define registration timing and isolated-world selector execution.
