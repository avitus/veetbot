# ADR-0072: Milestone 19 conversational schedule creation

- Status: Proposed
- Date: 2026-08-24
- Related: Sections 8, 9, 21, 22, and 29 of the engineering plan;
  ADR-0021, ADR-0024, ADR-0026, ADR-0059, ADR-0062
- Detailed design: `docs/plan/scheduling.md`

## Context

Milestone 11 delivered a complete authenticated schedule control plane and
explicitly kept schedule creation out of the model-callable tool registry.
Milestone 12 then delivered schedule-outcome notifications. The combination
works when an HTTP client first creates a schedule, but the native chat has no
schedule client and the model has no tool that can call the schedule service.
Consequently a direct request such as "remind me at 7pm" cannot reach either
implemented subsystem.

The owner authorized the narrow conversational bridge on 2026-08-24. This is
a parallel Milestone 19 workstream and does not advance the verified gate
ceiling past unfinished Milestones 13 through 15.

## Proposed decisions

1. **Add one model-callable capability, `schedule.create`.** It creates only a
   one-time schedule. Listing, updating, pausing, resuming, cancelling, daily,
   and weekly creation remain on the authenticated application surface.
2. **Use the existing application service directly.** The tool receives the
   principal and invocation idempotency key from `ToolExecutionContext` and
   calls `ScheduleService.create`; it does not call the HTTP API, hold a bearer
   token, or add a second persistence path.
3. **Keep the call consequential and governed.** The tool is
   `EXTERNAL_WRITE`/`HIGH`/`CONDITIONALLY_IDEMPOTENT`, requires exactly
   `schedule.write`, is non-parallel, and therefore follows the ordinary
   approval and effect-watermark pipeline.
4. **Accept an exact instant, not natural-language time.** The schema accepts
   `title`, `instruction`, and one timezone-aware ISO 8601 `at` value. The
   model uses `system.current_time` and `conversation.ask_user` to resolve an
   ambiguous date or timezone before proposing the call. Past and naive
   instants fail without state.
5. **Delegate no scopes.** The resulting schedule requests the empty scope
   set. It pins the active agent version and policy profile and derives finite
   limits no wider than that agent and the existing schedule ceilings. A
   future tool that schedules privileged work needs separate design.
6. **Reuse the schedule feature flags.** The tool is registered and advertised
   only when both `AGENT_SCHEDULE_API_ENABLED` and
   `AGENT_SCHEDULE_WORKER_ENABLED` are enabled. No third activation flag is
   added.
7. **Keep notification semantics unchanged.** A scheduled run still produces
   the existing generic, content-free "Scheduled run finished" notification
   after accounting. No reminder text enters APNs and no new notification
   trigger or payload field is added.
8. **Five gates own the new boundary.** They cover feature-gated
   classification, the approval-backed happy path, exact-scope denial,
   validation without residue, and retry idempotency.

## Consequences

- A direct chat request can create a one-time reminder after the user approves
  the concrete title, instruction, and instant.
- The schedule worker, occurrence ledger, run path, authority refresh, and
  notification outbox are reused unchanged.
- The push occurs after the scheduled run reaches a terminal state, not at the
  nominal instant itself, and its lock-screen text remains generic.
- Existing sessions keep their pinned tool set; `schedule.create` appears in a
  session opened after activation.
- `schedule` becomes a build-time builtin tool domain.

## Alternatives considered

- **Have the model call `POST /v1/schedules`:** rejected because tools do not
  receive bearer credentials and internal HTTP would duplicate authorization
  and failure translation.
- **Create arbitrary notification rows:** rejected because Milestone 12's
  closed trigger catalog and content-free payload are security boundaries, not
  a missing convenience API.
- **Expose the complete schedule lifecycle at once:** deferred; creation solves
  the reported use case, while mutation and recurring natural-language rules
  need their own schemas and concurrency semantics.
- **Allow requested tool scopes:** rejected for this slice. A reminder needs no
  delegated capability, and the empty set makes the future run least-privilege
  by construction.
