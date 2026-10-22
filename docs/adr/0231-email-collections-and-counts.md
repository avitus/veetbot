# ADR-0231: Email collections and scoped counts

- Status: Accepted — owner requested completion of the approved A2 designs on 2026-10-20
- Date: 2026-10-20
- Related: ADR-0092, ADR-0095
- Detailed design: `docs/plan/email-experience.md`

## Context

The approved Mac A2 Email design (`clearly-village-1475`) shows Inbox,
Needs you, and Handled collections with counts. Its Inbox includes handled
mail until separately archived. The existing priority view excludes handled
and low-ranked mail, so relabelling that page Inbox or counting its five rows
would not implement the design. Reversible handled state already exists.

## Decision

Add read-only `inbox`, `needs_you`, and `handled` views to the existing scoped
thread-list API. Count all retained, authorized cached Inbox threads after
account/search filters and feedback, before pagination. Needs you is the
reply-needed subset without a current-revision dismissal; Handled is the
current-revision-dismissed subset. These are cached counts, not a claim about
mailbox completeness. Preserve freshness and coverage warnings.

A2 clients initially request Inbox and retain five-row initial pagination;
this extends ADR-0092's priority-only initial presentation. The existing
priority, other, and all views remain supported, and the API's default stays
priority for older clients. Missing counts remain unknown on the client.

Preserve ADR-0095's explicitly labelled Archive in Gmail gesture. A separate
Mark handled/Undo handled control changes only the current source revision's
attention state through `/dismiss`; it does not alter Gmail labels, priority
feedback, reply judgments, or draft content. Changing collections preserves
open content and unsaved draft edits. New source content reopens handled state.

The pinned Settings design explicitly withholds per-device mute switches;
quiet-morning controls are absent. This delivery adds neither. Inline
subscription actions reuse the existing confirmation and durable operation
contract without widening consent.

## Consequences

No new storage, migration, scope, provider call, or notification policy is
needed. Existing authorization and retained-summary traversal govern counts
and rows together. No milestone, release gate, or production activation moves.
