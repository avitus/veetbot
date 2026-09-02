# ADR-0080: Milestone 23 SMS through the owner's iPhone

- Status: Proposed
- Date: 2026-08-26
- Related: engineering plan Section 29 and roadmap item B7; ADR-0034,
  ADR-0049, ADR-0061, ADR-0069
- Detailed design: `docs/plan/device-channel-and-sms.md`

## Context

Roadmap item B7 held the device channel and device-scoped tools behind "a
concrete use case, after Milestones 12 and 14". On 2026-08-26 the owner
supplied the use case: sending and reading SMS through the owner's own
iPhone — the agent acting as the owner, not a CPaaS number acting as the
agent. Milestone 12 is complete and delivers everything the slice stands
on; the Milestone 14 dependency in B7's entry condition covered client
attribution and the session-key resolver, and this design takes neither —
its triage session is keyed per device and channel, not through the
surface resolver. The roadmap rule requires the owner's authorization and
a specification with gates to declare. This ADR is the authorization, and
device-channel-and-sms.md is the specification, with twelve gates.

## Proposed decisions

1. **Parallel workstream.** Milestone 23 is authorized as a parallel
   workstream on the ADR-0069 terms: its gates become green independently,
   and the verified gate ceiling still advances only in numerical order,
   so nothing here moves the ceiling past 15. The milestone touches three
   shared files, named here rather than discovered in review: the devices
   domain model (the `capabilities` field lands where Milestone 14's
   surface rows also live), and the two document widenings named in
   decision 6.
2. **The device channel is push-wake with poll-back.** One adapter: a
   pending invocation row, a content-free APNs wake, authenticated fetch
   and result post, a bounded wait resolving `tool.device_offline`. No
   websocket, no suspension kind.
3. **`device.sms.send` registers from declared capabilities.** The
   device's registration declares the capability; the registry exposes the
   tool with `ToolSource.DEVICE` while the device is present and
   unrevoked. This is the third registration source the seam audit
   anticipated.
4. **The compose-sheet tap is the approval.** The tool classifies `ALLOW`
   because iOS makes the owner's Send tap non-bypassable; a second in-app
   approval would duplicate, not add, control. Hardline rules still scan
   the outbound body before any invocation is written.
5. **Ingest is best-effort and untrusted.** The Shortcuts automation and
   App Intent forward incoming texts to a device-authenticated route; the
   content is device-originated untrusted input routed into a standing
   triage session; the design says plainly that iOS can silently disable
   the automation.
6. **Two explicit widenings.** Milestone 12's closed trigger catalog
   grows a sixth content-free entry (the pending device invocation), and
   tool-system's registration closes at three sources instead of two.
   Both documents are amended by this change-set, not silently diverged
   from.
7. **Default off.** `AGENT_DEVICE_CHANNEL_ENABLED` and
   `AGENT_DEVICE_SMS_ENABLED` default off and change together at release
   validation. This follows the schedule and notification routers exactly.
8. **Twelve gates extend the existing `device` area.** Registered at
   Milestone 23 against the design's hard-gates section.

## Consequences

- The census grows from 353 to 365 registry entries; the `device` area
  grows from six gates to eighteen.
- The scope vocabulary does not grow; the new routes take `device.read`
  and `device.write`.
- The iOS client gains its first device-scoped capability, behind a
  default-off setting.
- B7's roadmap row narrows to presence-based routing and hand-off.

## Alternatives considered

- **A CPaaS number (Twilio-class):** rejected by the owner; the point is
  the agent acting as the owner, and a second number is a different
  product.
- **A websocket device transport:** rejected for v1; push-wake reuses
  Milestone 12 delivery and adds no long-lived connection infrastructure.
- **A waiting-on-device suspension kind:** deferred; the bounded wait is
  honest about an unreachable phone, and the suspension kind is a later
  ADR if expiry rates demand it.
- **Silent sends via Shortcuts automation:** rejected; it would remove
  the non-bypassable confirmation that justifies `ALLOW`, and its
  reliability is unverifiable.
- **Treating ingest as a Surface:** rejected; an incoming SMS sender is
  not a paired principal, and pairing-as-authentication cannot apply.
  The device seam, with forced untrusted output, is the honest home.
