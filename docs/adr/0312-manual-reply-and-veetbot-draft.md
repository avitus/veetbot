# ADR-0312: Reply means owner composition; Veetbot drafting is a separate action

- Status: Accepted — owner requested the separate actions and native implementation on 2026-11-11
- Date: 2026-11-11
- Related: ADR-0092, ADR-0279

## Context

The reading footer previously scrolled to a draft. The missing-draft repair
incorrectly made Reply generate text. The owner clarified that Reply means
writing personally and approved adjacent manual and AI choices in MagicPath.

## Decision

Translate those choices into the shared SwiftUI reader. Reply creates or resumes
a server-owned manual composition, initially empty and focused. Draft with
Veetbot explicitly generates text; Use Veetbot draft opens an existing suggestion.
Manual and AI compositions use a marker on immutable draft revisions, retaining
one current proposal per reply target. Switching saves pending edits, archives
the outgoing revision and restores the latest revision of the requested kind.
This preserves both texts across devices without a second active draft store.

A typed, scoped reply-selection route verifies the observed source and draft
revisions under the existing owner lock. Blank creation shares generated drafts'
verified envelope rules and performs no model or provider operation. Retries
replay the result; concurrent changes and sends refuse replacement. Existing
drafts default to the Veetbot composition. Existing Send and Undo remain intact.
The detailed request and persistence contract lives in
[email-experience.md](../plan/email-experience.md).

## Consequences

The client and server ship together for manual composition. An older server
returns an actionable error; the client must never fall back to generation.
No new dependency, storage table, approval bypass or Gmail draft write is added.
