# ADR-0176 — Reviewed X feed briefing

- Status: Accepted under the owner's implementation instruction
- Date: 2026-10-10
- Amends: ADR-0164 reviewed workflow selection and collection steps

## Decision

Ship the first reviewed recipe for the owner's morning personalized X home-feed
briefing behind `BROWSER_FEED_WORKFLOW_ENABLED`, default off. Match a complete
normalized owner request, including a SHA-256 digest
of the reviewed long schedule instruction; never match a title, substring, page
instruction or model classification. Digests avoid publishing private prompt text.
Changed instructions and ambiguous matches retain ordinary execution. A selected
READY profile and the current tool contract remain prerequisites. Production
activation pairs the recipe with the privately qualified account definition in
ADR-0175; the recipe itself is not account-identity evidence.

Add read-only article extraction to browser.observe 1.8.0; retain pinned 1.7.0.
An articles collection selects one visible main region. Scan at most 8,192 nodes
for the main region and 4,096 descendants for non-nested, viewport-intersecting articles. Each
row contains a bounded rendered-text excerpt (256 characters), a unique visible
timestamp link on the current origin.
Text and link discovery each inspect at most 256 nodes per article. Hidden,
editable, nested-article and frame content is excluded. Only public HTTPS links
without credentials, query or fragment qualify; incomplete or ambiguous link
discovery supplies no link. Excerpts retain explicit truncation. These are page
claims, not authenticated statements, publication dates or action references.

A finite collection step reads up to four windows of three articles, seeking
six unique linked excerpts, with at most six empty/changing-page retries spaced
one second apart. It requires the exact home URL and X post-link structure.
The feed shell alone never supplies evidence. Deduplicate by post ID, retain
at most twelve IDs in durable state and stop on repeated content. Up to three
fresh-target bounded scrolls use the ordinary policy/effect pipeline and the
existing account-menu control as a scroll anchor, never a click. A possibly sent
scroll is not replayed: read once, collect any evidence, and stop explicitly.
Recoverable stale reads may refresh within the same bounds. Authentication uses
the existing verified continuation; a new generation clears collection progress
and returns to a fresh home navigation, never reusing old action references.
The scroll/retry ceilings persist across generations; final reporting excludes
old-generation extraction calls and rows without qualifying post IDs.

Every call counts against the existing workflow/run budgets, cancellation and
deadline, reserving final synthesis. The final turn has no tools and summarizes
only observed feed evidence, two or three recurring topics when supported, with
observed representative links and an explicit sample/insufficient-evidence limit.
Article excerpts stay EXTERNAL_UNTRUSTED. The recipe cannot post, like, follow,
open arbitrary links, or supply authentication. No benchmark or new dependency.

## Verification

Begin with failing tests for selecting the shipped recipe and collecting article
evidence. Exercise delayed/empty feeds, repeated and malformed links, truncated
and hidden content, stale reads, ambiguous scroll targets, uncertain scrolls,
authenticated resumption, wrong accounts, cancellation, deadline/budget exhaustion
and tool-free final reporting. Use synthetic site fixtures and the real Chromium
runtime; production activation and live scheduled acceptance remain separate.
