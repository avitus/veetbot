# ADR-0011: Multi-device operation and the shared core

- Status: Accepted
- Date: 2026-07-17
- Related: Sections 9, 16, 27, 29; Milestone 9 (memory)

## Context

The agent must be usable from many devices - phone, laptop, desktop, web - while
behaving as one continuous assistant. Memory is the obvious component that must
be shared across devices, but it is not the only one, and some capabilities
(reading a file on a specific laptop, driving a browser on a specific desktop)
are inherently local to one machine. We need an explicit split.

## Decision

1. **One shared cloud core, many thin clients.** There is exactly one
   authoritative instance of the user's state, in the cloud (PostgreSQL is the
   source of truth). Devices hold no authoritative state; they render, capture
   input, stream events, and optionally expose device-local capabilities. Writes
   from any device go to the core; there is no device-to-device sync.
2. **Cloud-shared components** (authoritative server-side, identical on every
   device): identity/principals; sessions, runs, events, checkpoints; long-term
   memory and knowledge; artifacts; approvals; versioned agent configuration;
   policy rules and the policy engine; credentials and the secret broker; the
   model gateway; tool execution and the sandbox; usage, cost, and budgets;
   scheduling; observability and audit.
3. **Device-local** (never centralized): the client UI and input (screen,
   keyboard, mic, camera); device-local capability providers (local filesystem,
   a browser on that machine, local apps, locally-running MCP servers); the
   transient stream connection; optional non-authoritative read caches.
4. **Device-scoped capabilities (hybrid).** Introduce a `Device` concept
   (registered per principal, with declared capabilities, granted scopes, and
   presence) and device-scoped tools routed to a connected device. Such calls
   still pass through the full pipeline - schema validation, principal scopes,
   policy, approval, timeout, output limits, tracing. The device is an execution
   target, not a policy or credential authority. Device output is untrusted.
   Offline devices make their tools unavailable and yield structured failures.
5. **Cross-device flows**: any device attaches to a session and resynchronizes
   via SSE replay (`Last-Event-ID`); approvals can be resolved from any
   authorized device (idempotent, first resolution wins); the single-active-run-
   per-session rule prevents two devices racing one session.
6. **Scope**: the shared core is already multi-device by construction. Defer the
   `Device` concept, presence, device-scoped routing, and notifications to a
   milestone with concrete use cases; for 0.1, ensure every read/write is
   principal-scoped and served from the core, and confirm a second client can
   attach and replay.

## Consequences

- A coherent multi-device experience with central control and security and
  nothing to reconcile between devices.
- New domain (`Device`) and ports (`DeviceRegistry` / `DevicePresence`,
  `DeviceChannel`, `NotificationService`); the tool registry and context builder
  gain presence-based filtering and routing.
- Compromise of one device is limited: no local secrets, and scopes are central
  and per-device, revocable server-side.

## Alternatives considered

- **Per-device local state with sync**: rejected; reconciliation complexity,
  security exposure, and divergence.
- **Device-local policy or credentials**: rejected; the security boundary must be
  central.
- **Treat every capability as cloud-only**: rejected; the agent could not reach a
  specific machine's files or browser, which is a core use case.
