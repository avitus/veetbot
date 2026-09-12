# ADR-0094: Refresh context authority at run boundaries

- Status: Proposed (repair authorized by the repository owner, 2026-09-12)
- Date: 2026-09-12
- Related: ADR-0020, ADR-0043, ADR-0059, ADR-0088
- Detailed design: `docs/plan/context-engine.md`

## Context

A scheduled briefing builds its session's first context plan with the
schedule revision's restricted scopes. An authenticated owner reply receives
the owner's scopes, but reuses the restricted plan and cannot see
`schedule.list` or `schedule.update`. A new conversation exposes those tools.
The owner authorized repairing this transition while keeping the briefing
restricted and preserving the conversation.

## Decisions

1. **New run scope grants start an explicit prefix epoch.** Before a run
   initializes its tool pins, compare its effective scopes with those used
   to select the current plan. A new grant rebuilds through the existing
   filtered, bounded planner and records `run_authority_changed`. This narrowly
   amends ADR-0020's session-long authorization pinning; it does not refresh on
   every model request or on live permission changes during a run.
2. **Selection scopes are durable and content-free.** The plan stores a sorted
   tuple of SHA-256 hashes of the scopes used for selection, within its existing
   principal-scoped session event. These are metadata, never prompt content or
   authority grants. An absent tuple in an older plan is unknown, not empty authority;
   the next unpinned run rebuilds it once through the same epoch mechanism.
3. **Checkpoint pins prevent mid-run refresh.** The runtime requests authority
   refresh only before any tool pins or pending tool calls exist. This includes
   a scheduler's preseeded checkpoint. An initialized empty tool set is still
   pinned. Approval resumes, retries, and restored legacy nonempty pins retain
   their tools and ordinary call-time policy enforcement.
4. **Revocation alone preserves the existing prefix contract.** A subset of the
   selection scopes reuses the plan; revoked tools are denied at call time even
   across run boundaries. A new grant rebuilds using only the new run's effective
   permissions and the pinned agent's enabled tools. It changes
   neither agent versions nor schedule revisions, requested scopes, or limits.
   Discovery remains read-scoped; schedule mutation still requires its exact
   write scope and ordinary approval.

## Verification

The regression starts a restricted daily briefing, then replies in its occurrence
conversation to replace daily 09:00 Pacific with Friday 08:55 Pacific. It must
retain briefing history, advertise discovery and update only to the owner run,
wait for approval, and write one revision with unchanged execution authority.
Planner contracts cover new grants and stable revocation, restart reconstruction,
unchanged-authority reuse, legacy plans, and no refresh while pins are retained.

## Consequences

- New scope grants incur one logged re-cache; unchanged or reduced scopes keep
  the existing prefix. Unrelated sessions and running checkpoints do not rotate.
- Existing plan events remain readable without a database migration.
- Old agent versions that never enabled scheduling remain pinned; this repair
  does not silently upgrade agent configuration.
