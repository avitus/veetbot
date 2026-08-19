# ADR-0056: Failure-triggered reflection review

- Status: Proposed
- Date: 2026-08-18
- Related: Section 30, Milestone 10, ADR-0013, ADR-0030, ADR-0051, ADR-0052
- Detailed design: `docs/plan/skills.md`, `docs/plan/runtime-loop.md`
- Prior art: Reflexion (Shinn et al., 2023, arXiv:2303.11366)

## Context

The evaluation harness recognizes exactly two inter-run learning carriers,
memory and skills, "because they are the only two subjects whose entire claim
is that something learned in one run changes the next"; a third carrier is a
design change rather than a configuration. Both carriers today learn only from
success or from user content. The skill background review hook is enqueued on
`COMPLETED` only. Automatic memory formation is flagged on every terminal run,
but its automatic-source integrity gate restricts direct sources to events
authored by the owning principal — model, tool, and foreign-principal content
cannot become a direct source — so the agent's own analysis of what went wrong
never persists. A `FAILED` run therefore contributes nothing to future
behavior: the queue dead-letters it, and of the four post-terminal hooks only
the trajectory-export marker touches its transcript.

External evidence says this gap is expensive. Reflexion showed that distilling
a failed trajectory into one short natural-language lesson, carried into later
attempts, is the load-bearing component of large task-completion gains
(+22 points on ALFWorld, +20 on HotPotQA, +11 on HumanEval pass@1); its
ablations showed that replaying the raw failed trajectory without the
distilled lesson recovers only a fraction of the gain, and that self-generated
unit tests without the reflection step add nothing over baseline. The parts of
Reflexion that transfer to this platform are exactly the parts it already has
governed machinery for — a post-run child that writes to a persistent carrier.
The parts that do not transfer (an in-run retry-until-pass loop, a
sliding-window reflection buffer, an in-run evaluator) conflict with settled
decisions and are named as non-goals below.

## Proposed decisions

1. **A failure reflection review is the background review's trigger,
   extended — not a new mechanism.** A post-terminal hook enqueued on `FAILED`
   only, at most once per parent run, and only for runs that made at least one
   tool call. Like the existing hooks it is enqueued after the terminal
   transition commits, and its failure is logged and never fatal. `CANCELLED`
   does not trigger it: cancellation records the user's decision, not a
   defect.
2. **The review inherits ADR-0052's confinement unchanged.** A dedicated child
   session recording `parent_run_id`; never joins the parent; receives the
   parent transcript as enveloped data; bounded deadline and budget; tool set
   limited to `memory.*`, `skill.load`, and the `create`, `edit`, and `patch`
   operations of `skill.manage`; edit and patch require having loaded the
   current revision; edits are denied for any skill whose `source` is not
   `AGENT`; `archive` is never available.
3. **The lesson rides the existing carriers only.** The review's task is to
   decide whether the failure teaches something worth writing down and, if so,
   to persist it as a revision to an agent-authored skill (or the creation of
   one), or as an explicit memory write whose provenance names the review run.
   Nothing enters the automatic extraction path; the automatic-source
   integrity gate is unchanged.
4. **At most one lesson per review.** A failed run yields one short statement
   of what went wrong and what to do differently, or nothing. A failure must
   not fan out into many low-quality writes; repeated failures of the same
   shape converge on one revised skill, not an accumulation of beliefs.
5. **Construction and rollout are separate, as in ADR-0052.** The hook is
   default off and independent of the success review's setting. Where the
   policy profile requires approval for `SKILL_AUTHORING` or memory writes,
   the review's proposal waits in the existing approval queue. Tenant
   activation requires the same quantitative evidence structure as ADR-0052:
   paired samples on scenarios that first fail, task-completion lift with a
   positive confidence lower bound, and zero additional policy failures.
6. **Acceptance uses the existing two-arm case mechanism.** The gate case for
   this feature declares `arms`: the first arm fails a scripted scenario and
   runs the review; the second arm, with `carry: [skills]` (or
   `carry: [memory]` for the explicit-memory variant), succeeds. The `delta`
   block requires the outcome to improve with policy failures unchanged. No
   third carrier is introduced.
7. **Non-goals.** No within-run reflect-and-retry loop — step retries remain
   byte-identical and permanently failed runs remain dead-lettered; a
   re-attempt is an ordinary new run that recalls the stored lesson through
   ordinary retrieval. No in-run evaluator or self-scoring step — the
   LLM-as-judge stays in the capability track. No dedicated reflection store —
   the memory hierarchy already subsumes Reflexion's bounded episodic buffer.

## Consequences

- A failed run stops being pure waste: its transcript can produce one governed,
  auditable artifact on a carrier the harness already knows how to measure.
- Run volume grows by at most one child run per failed run that did work,
  bounded exactly as the success review is bounded, and never contending with
  the user's session.
- A wrong lesson has the same blast radius as a wrong success-review proposal:
  it is a pending approval or a disabled candidate skill, with provenance,
  and an operator activation step in front of agent configuration.
- A failure caused by injected content is an adversarial reflection input; the
  mitigations are the ones the success review already relies on — enveloped
  transcript data, the tool whitelist, the `AGENT`-source boundary, and the
  approval queue — so this ADR adds no new trust surface, only a new trigger.
- The feature can be fully implemented and tested while remaining unavailable
  in ordinary deployments.
- This ADR proposes; it does not authorize. Work begins only when the owner
  lists the tranche in `docs/status/project-state.yaml`, and the detailed
  design lands in `skills.md` and `runtime-loop.md` alongside that change.

## Alternatives considered

- **Adopt Reflexion's retry loop verbatim:** rejected. It assumes a crisp
  per-task success signal and re-attempts inside one task lifetime, which
  conflicts with byte-identical step retries, the prefix-cache invariant, and
  dead-letter semantics. The platform equivalent is a new run plus recall.
- **A dedicated reflection memory (sliding window of lessons):** rejected. The
  harness names a third inter-run carrier a design change, and the existing
  belief lifecycle, decay, and recall trace are strictly stronger than a
  bounded buffer.
- **Relax automatic-source integrity so failure analyses become automatic
  beliefs:** rejected. The gate is the platform's defense against
  self-reinforcing model content and injected instructions becoming durable
  belief; the explicit, provenance-labeled channels already exist and are
  auditable.
- **In-run self-evaluation (LLM judge or self-generated unit tests):**
  rejected. Judge governance deliberately keeps grading offline; Reflexion's
  own ablation shows self-generated tests without the reflection step add
  nothing; and real test execution is already available to the agent as an
  ordinary sandboxed tool.
- **Trigger on `CANCELLED` as well:** rejected. Reflecting on a cancellation
  second-guesses a user decision and would routinely produce lessons about
  work the user chose to stop for reasons outside the transcript.
