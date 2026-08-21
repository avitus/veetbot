# ADR-0061: Milestone 10 completion versus tenant activation, and the roadmap authorization

- Status: Accepted (directed by the repository owner, 2026-08-20)
- Date: 2026-08-20
- Related: Sections 21, 24, 27.6, 29, and 30.5 of the engineering plan;
  ADR-0027 (milestone map), ADR-0034 (the Section 29 seam), ADR-0052,
  ADR-0057, ADR-0059, ADR-0060
- Detailed design: `docs/plan/engineering-plan.md` (Section 21, Milestones 12
  through 15 and the roadmap subsection), `docs/plan/milestone-map.md` (the
  census), `docs/status/project-state.yaml`

## Context

Milestones 0 through 9 are complete and verified. Milestones 10 and 11 are
implemented locally with every registered gate passing; both still need the
hosted CI lanes and the final CodeRabbit review on their final head. Milestone
10's completion rule additionally required the self-authored form of evaluation
case 27 to clear the rollout threshold in `skills.md#rollout-evidence` — at
least thirty paired samples per model-policy, policy-profile, and authoring
version, with a five-point completion lift, a positive Clopper-Pearson lower
bound, and no added policy failure. That evidence needs real usage. Holding a
construction milestone open on usage that cannot exist until the platform is in
daily use inverted the dependency: activation evidence is produced by using the
system the milestone builds.

Separately, the engineering plan ended at Milestone 11 with no roadmap
section, although Section 24 requires deferred work to become "documented
issues or a roadmap section", and the repository tracks no issues. The
deferrals lived in a dozen places: Section 2.6's exclusion list, Section 21.1's
"keep late" rows, the design-only routing and subagent subsections, Section
29.8, Milestone 11's exclusion sentence, the readiness review's partial items,
and the seam audit's five named gaps. No document ranked them or said which
would become a milestone.

The owner reviewed an assessment of every milestone and every deferral on
2026-08-20, chose a direction — a personal daily-driver used from the native
Apple client, the CLI, and soon a messaging channel — and authorized four
milestones in order.

## Decisions

1. **Milestone 10 completes on construction.** Milestone 10 is complete when
   every registered Milestone 10 gate and the cumulative registry pass, the
   hosted CI lanes pass on the final head, and the final CodeRabbit review has
   no finding or unresolved conversation. The self-authored-skills tranche's
   rollout threshold and the provider-assisted extractor's version-bound
   evidence are tenant-activation conditions, not completion conditions.
2. **Activation stays evidence-gated and default-off.** Nothing in this ADR
   enables authoring or provider-assisted extraction for a tenant. The
   acceptance criterion "authoring remains disabled by default and is not
   enabled for a tenant until the rollout evidence rule passes" stands verbatim,
   as does ADR-0057's activation rule. The pending evidence is tracked as
   roadmap item B1.
3. **Milestones 12 through 15 are authorized, in order.** Milestone 12 is
   notifications and device identity, delivering Apple push notifications to
   the existing native client and the `Device` registry Section 29.6 names;
   Milestone 13 is general-purpose subagents through `delegate.run`, honouring
   the gate for multi-agent work by requiring evaluation evidence before tenant
   activation; Milestone 14 is inbound Surfaces and pairing, first through a
   Telegram bot; Milestone 15 is operational hardening of the single-Droplet
   deployment. Each is implemented only after its detailed-design document and
   ADR exist and declare its gates.
4. **Zero-gate rows are reported, not filled.** The milestone map's census
   carries a zero row for each authorized milestone until its specification
   lands, under the map's Decision 9. The registry's milestone bound moves from
   11 to 15 and is named once, as `MAX_MILESTONE`.
5. **The verified gate ceiling still advances in order.** Milestone 12 may be
   developed while Milestones 10 and 11 await hosted review, exactly as
   Milestone 11 was developed alongside Milestone 10, but the ceiling cannot
   pass 11 until 10 closes, or 12 until 11 closes.
6. **The roadmap is a plan section.** Every remaining deferral is a ranked
   item in Section 21's "Roadmap beyond Milestone 15", with its entry
   condition. An item becomes a milestone only by owner authorization plus a
   specification with gates to declare. Model routing remains unauthorized.
7. **A child run always gets a dedicated child session.** Section 27.6's "or
   a dedicated child session per policy" branch is removed in favour of the
   only branch the one-active-run-per-session index admits, which is the
   branch ADR-0052 and ADR-0059 already took.

## Consequences

- `docs/plan/current-milestone.md` and `docs/status/project-state.yaml` drop
  the rollout threshold from Milestone 10's remaining work and record
  Milestones 12 through 15 as authorized.
- Four detailed-design documents and four ADRs are owed before the
  corresponding code: `notifications-and-devices.md`,
  `subagents-and-delegation.md`, `inbound-surfaces.md`, and
  `operational-hardening.md`.
- `scripts/gate_registry.py` and `scripts/check_docs.py` admit milestone keys,
  plan headings, and registry entries through 15; the census reports four zero
  rows until the specifications land.
- Section 21.1's sequencing table and the subagent subsection's framing change
  from "deferred" to the milestone that owns them; Section 29's opening
  paragraph points at Milestones 12 and 14.
- ADR-0046 is amended in place to record that ADR-0048 already superseded its
  Caddy choice with Nginx.

## Alternatives considered

- **Keep the rollout threshold as a completion condition:** rejected because
  the evidence requires production usage, and the verified ceiling would stall
  on data the platform cannot yet generate.
- **Declare authoring activation abandoned:** rejected; the owner wants the
  capability, and default-off with an evidence gate already bounds the risk.
- **Authorize only Milestone 12 and decide the rest later:** rejected by the
  owner in favour of a standing authorization that removes a decision between
  milestones; the plan's one-milestone-at-a-time rule is preserved by the
  ordered ceiling.
- **Track deferred work as GitHub issues:** rejected because the repository's
  planning already lives in the plan, the ADRs, and the state file, and the
  plan's own Section 24 offers a roadmap section as the alternative.
- **Put operational hardening in the backlog:** rejected by the owner; data
  loss on a single host is a live risk, so it gets full milestone treatment.
