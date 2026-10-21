# ADR-0235: Independently verify known accessibility contrast findings

- Status: Accepted by the owner on 2026-10-21
- Date: 2026-10-21
- Scope: Native People directory accessibility verification

## Context

The People directory/profile gate rejects every accessibility finding inside
its surface, as specified in `docs/plan/people-and-relationships.md`.
The iPad iOS 27 audit in release PR 181 reports contrast failures for the
Everyone filter value and the Import history and Add person action labels.
The same unchanged audit reproduces locally. A diagnostic build replacing
those foregrounds with literal black still reports all three failures; its
failure screenshots show black text on white or near-white paper. These
findings therefore cannot be corrected by increasing the foreground contrast.
The diagnostic color changes are not part of the release.

## Decision

Keep the existing audit and its default rejection of findings. Permit a
contrast-only finding for these three exact controls to be independently
verified against the rendered element screenshot, instead of accepting an
unconditional exception or changing the application's layout to influence the
auditor. Verification must:

1. Match the contrast audit category and one of the three explicit controls.
2. Obtain the element screenshot and identify sufficient pixels of its expected
   semantic foreground and background colors for the current appearance.
3. Require a WCAG relative-luminance contrast ratio of at least 4.5:1, even at
   accessibility text sizes where a lower large-text threshold may apply.
4. Reject unknown colors, insufficient evidence, missing elements or images,
   low contrast, other controls, and every other audit category.
5. Retain the original finding and the independent measurement as test
   attachments, so the exception remains visible in CI evidence.

Add regression cases for low contrast and missing or mismatched evidence as
well as the valid light/dark palette cases. Run the directory and
profile audit on iPhone, iPad, and macOS; action and filter interaction coverage
must continue to pass. This does not authorize a general contrast-audit skip.

## Consequences

This revises the existing reject-every-finding rule while preserving the
underlying text-contrast requirement through explicit rendered evidence.
The owner explicitly approved this narrow correction. The release remains
subject to the measured audit, regression cases, review, and hosted CI. A future
palette change requires fresh evidence rather than silently broadening the
accepted colors.
