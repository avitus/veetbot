# ADR-0151: Browser observations remain readable within the model budget

- Status: Proposed (owner-requested X posting repair, 2026-10-05)
- Date: 2026-10-05
- Related: ADR-0058, ADR-0127, ADR-0130, ADR-0137; engineering plan Section 33
- Detailed design: `docs/plan/browser-automation.md`

## Evidence

The production browser successfully typed into the owner's composer and returned
an enabled publication button. Its roughly 39 KB observation became a 4 KB
head/tail excerpt that omitted that button and its reference. The model tried
to find the captured output in the workspace, where tool-output artifacts are
not files, then attempted another navigation and lost the usable page. No
publication action was dispatched. This record contains no private page text.

## Decision

Add `browser.observe@1.1.0` with optional nonnegative `element_offset` and
`text_offset`. It refreshes the current page through the existing provider and
returns complete JSON within the configured serialized inline byte budget.
The result carries the current revision, complete element references and states,
counts, and separate next offsets for elements and Unicode text characters.
Element names and the title may have an explicitly marked display truncation;
the provider's full observation and approval facts stay unchanged. A metadata
or element reference that cannot fit produces an explicit output error.

Offsets select from each fresh observation, not a frozen snapshot. A changing
page can move entries between calls; only the newest returned revision and
references may be used for an action. Set the element offset to the returned
element count when reading text alone. Default reads begin at zero. Reads never
navigate, mutate the website, expand origin authority, or expose profile bytes.

Keep `browser.observe@1.0.0` registered for pinned chats. New chats select 1.1.0;
an existing frozen chat requires a new profile-bound chat to use pagination.
Navigate, act and upload retain their existing versions and full observations.
The new observe description explains how to recover controls omitted by their
generic excerpts. The generic artifact/excerpt pipeline and its byte ceiling
remain unchanged; pages fit before that pipeline, without a new artifact reader.

When the runtime itself dismisses a beforeunload dialog and Chromium cancels
that navigation, report `tool.browser.navigation_cancelled` and preserve the
existing page and lease. Do not accept the dialog or retry navigation. Other
browser, transport and origin failures keep their existing classifications.
Action dispatch failures retain uncertain-write handling.

## Validation

Reproduce a large composer result with its publication control in the omitted
middle, then traverse the observation pages through real output admission and
prove every returned reference and revision is intact. Cover configured byte
limits, escaping, Unicode text continuation, invalid offsets, foreign origins,
long labels, pinned versions, and full approval facts. A real Chromium composer
with an unsaved-change dialog must retain its draft after cancelled navigation;
the hosted provider must retain the same lease. No live publication is required.

No engineering-plan requirement, approval rule or context limit changes. The
browser gate count and milestone status do not move.
