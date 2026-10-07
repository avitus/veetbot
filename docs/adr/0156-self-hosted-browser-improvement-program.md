# ADR-0156: Implement the self-hosted browser improvement program

- Status: Accepted by owner instruction on 2026-09-30
- Date: 2026-09-30
- Related: ADR-0058, ADR-0127, ADR-0128, ADR-0129, ADR-0130, ADR-0137, ADR-0141
- Detailed design: `docs/plan/browser-automation.md`, `docs/plan/development-toolchain.md`
- Program: [Self-hosted browser improvement plan](../browser-automation-improvement-proposal.md)

## Authorization and delivery

The owner requested implementation of the program after reviewing the plan.
This is an independently delivered extension to the existing Milestone 10
browser tranche. It does not move the sequential milestone ceiling or authorize
Browserbase. Deliver the work packages in dependency order and record partial
progress explicitly. Live evaluation needs selected accounts and task-specific
consent; synthetic verification proceeds independently.

Keep the current approval, exact profile/origin binding, human-only login,
secret isolation, and uncertain-write rules. Later capability contracts remain
separate ADR/design changes before implementation; the program does not replace
them with a blanket browser permission.

## First implementation decisions

1. Real Chromium checks have their own `browser` test partition. The partition
   is required by `make check`, remote sidecar validation, and hosted release
   verification. Static, contract, and PostgreSQL partitions exclude it. It
   includes the existing runtime, hostile-page, device-handoff, and hosted
   task-grant tests. Missing Chromium, skipped or filtered cases, and an empty selection
   cannot produce passing browser evidence. Installation is a separate target.
2. The browser partition emits a versioned content-free JSON report alongside
   JUnit: source revision and dirty status, library and observed browser identity,
   case identities without parameter values, outcomes,
   counts, and durations. No exception text, credentials, page content, URLs,
   screenshots, or captured output is copied into the JSON report. This is a
   runtime regression baseline, not the live task-completion benchmark. Live
   and end-to-end model quality remain visibly unmeasured.
3. Large successful builtin browser observations receive a semantic context
   projection at tool-output admission. Preserve complete revision and element
   records, disclose omissions, and retain the canonical result and its full
   artifact. Fit the existing byte budget including escaping and the artifact
   reference. Never enlarge global budgets or accept a tool-supplied alternate
   projection. If a field does not fit, omit it explicitly rather than clipping
   an opaque reference. Unsupported/invalid payloads use the existing failure or
   excerpt path. Persist the projection once so durable replay is stable.
4. Observation capture is atomic with respect to its revision and retained
   element handles. On capture, validation, or title failure, or cancellation,
   cancel and join outstanding captures, release every newly acquired handle,
   and invalidate the previous observation. Publish the new revision only once
   the complete bounded observation validates. This strengthens cleanup without
   changing which nodes are observed or granting action authority.

Decision 3 refines ADR-0137's presentation mechanism for known browser tools;
it does not change canonical observations, action schemas, trust, or authority.
It cannot make a control outside the current provider observation discoverable;
region expansion and relevance ranking are subsequent work.

## Verification

Write failing checks for required browser selection and release dependency,
and reproduce a large admitted observation whose mechanical excerpt cannot be
parsed or loses its action revision. Verify canonical preservation, complete
reference records, explicit coverage, byte ceilings, hostile strings, durable
replay, and unchanged non-browser output. Run existing real Chromium and
adversarial cases without skips. Record measured commands and outcomes with
the implementation; this ADR is not evidence that the whole program is done.
