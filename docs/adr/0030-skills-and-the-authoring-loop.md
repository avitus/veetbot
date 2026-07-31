# ADR-0030: The skill package, the pinned catalog, and the authoring loop

- Status: Accepted
- Date: 2026-07-27
- Related: Sections 8 (tools and skills), 8.4 (skills), 12 (context
  assembly), 15 (policy and approvals), 21 (Milestones 8 and 10), 30
  (self-improving skills), ADR-0013 (self-improving skills), ADR-0017
  (layered approval), ADR-0020 (the context engine), ADR-0021 (tool
  execution pipeline and MCP), ADR-0022 (the gate registry), ADR-0023
  (the run loop), ADR-0026 (builtin tools), ADR-0027 (the milestone
  map), ADR-0029 (sandbox isolation and artifacts)
- Detailed design: `docs/plan/skills.md`

## Context

Milestone 8 is the last milestone the readiness review scored Split:
MCP integration ready, skills not. Eleven documents reference skills,
Section 30 sets two acceptance criteria for a self-improving loop, and
`AgentSpec.enabled_skills` has been a field since Section 6 — with no
document defining what a skill is, where one is stored, how one is
referenced, or what enters context when one is used.

The review's summary of that gap, "skills have no specification at
all", is not accurate and the inaccuracy matters.
`tool-system.md:1102-1149` draws the line between a skill and a tool, fixes the
metadata block at four fields, puts `required_tools` checking at load
rather than at authoring, assigns trust by author, and classifies
`skill_manage`. That is a real design and this document does not
replace it. What is missing is everything underneath: no types, no
storage, no reference grammar, no context accounting, no gates.

Three things in the corpus disagree with each other on the way there.
`tool-system.md` calls `skill_manage` a control tool, and the same
document requires a control tool to have `side_effect: NONE` and a
`READ_ONLY` or `IDEMPOTENT` idempotency class — while classifying
`skill_manage` as `NON_IDEMPOTENT` with a write scope, and while
listing exactly four control tools, none of them `skill_manage`.
`policy-and-approvals.md` justifies `ActionKind.SKILL_AUTHORING` on
the grounds that skill authoring "is not a tool invocation", which
the tool classification contradicts. And the scope is spelled
`skills:write` in a document whose sibling enumerates every scope in
the dotted form and closes the list.

Two smaller errors surfaced while reading. `readiness.md` cites
`engineering-plan.md:2684` for the self-improvement criterion, which
is at 2690. `policy-and-approvals.md` cites Section 30.4 for the
requirement that skill authoring be policy- and approval-gated, which
Section 30.3 makes; Section 30.4 is loading and lifecycle.

## Decision

1.  **A skill is a package, not a string.** A directory with
    `SKILL.md` at its root, stored as a `tar.zst` archive under
    `skills/{tenant_id}/{skill_id}/{revision}.tar.zst`, with the body
    denormalized into the revision row so that loading is a row read
    and never an archive fetch.
2.  **`revision` is a platform integer; `version` is the author's
    string.** Pinning needs a total order the author cannot forge.
    `AgentSpec` already made this choice for the same reason.
3.  **The reference grammar is `<name>` or `<name>@<revision>`.** A
    bare name floats to the newest `ACTIVE` revision and is resolved
    once, at session open; a pinned reference is exact.
    `AgentSpec.enabled_skills` holds these, which is why that field
    could exist with no design behind it and still be right.
4.  **The catalog is pinned at session open**, the tool set's own
    rule applied to skills. It is also what stops an agent from
    writing a skill and loading it in the same session.
5.  **A reference that does not resolve fails the session open.** It
    does not resolve to nothing and it does not warn. An agent
    missing a procedure it was configured with is a different agent.
6.  **Two new context classes and a ceiling that moves**: skill
    catalog metadata in Region A at 1,500 tokens for at most twenty
    entries, and loaded skill bodies in Region B at 6,000 tokens for
    at most two. The prefix ceiling goes from 13,500 to 15,000 rather
    than taking budget from tools or memory.
7.  **Metadata may be in the prefix; a body never is.** A description
    is data the model compares. A body is instructions it follows,
    and it enters only through `skill.load`, only in Region B, and
    only for the rest of the session.
8.  **Skill bodies never yield under pressure**, and a third load
    fails rather than evicting a loaded one. Eviction would have to
    pick, and the picker cannot know which procedure is finished.
9.  **Catalog truncation is by configured order, never by a score.**
    A ranking underneath the model's own selection is a second
    selector nobody can see, and the drop count is a tracked metric
    because a silently halved catalog is experienced as skills that
    stopped working.
10. **`required_tools` is checked at load and the failure is a note**,
    not a refusal — `tool-system.md`'s rule, given a mechanism. The
    skill still loads, with the missing tools named in the body's
    header, because a procedure that mentions a tool in step four is
    still worth its other seven steps.
11. **A skill that ships a script does not ship a tool.** Package
    files extract to `<workspace>/.skills/<name>/`, which is
    read-only to the run; running one is a `sandbox.run_command` call
    that passes the whole pipeline like any other.
12. **MCP prompts are skills with `source = MCP`**, discovered at
    session open, never persisted, never editable, and labelled
    `EXTERNAL_UNTRUSTED`.
13. **`skill_manage` is a capability tool, not a control tool.** It
    acts on durable tenant state that outlives the run, which is the
    definition `tool-system.md` itself gives; the control-tool table
    never listed it; and this leaves the registration rule for
    control tools untouched rather than weakening it.
14. **`CONDITIONALLY_IDEMPOTENT` with `expected_revision` as the
    key**, rather than `NON_IDEMPOTENT`. It satisfies the classifier
    and it makes a crashed skill write recoverable instead of
    permanently `UNCERTAIN`.
15. **The scope is `skill.write`, singular and enumerated**, joining
    the closed list in `http-api-and-streaming.md`. No `skill.read`:
    nothing reads skills over the API in 0.1, and an uncheckable
    scope is worse than a missing one.
16. **`ActionKind.SKILL_AUTHORING` stays, with a narrower reason.**
    The action kind selects the approval's payload — a diff, not an
    argument blob — which earns the enum value without requiring the
    claim that authoring is not a tool call.
17. **Authoring is confined to trusted turns**, denied below `USER`
    origin trust, with the background review as the path for the case
    that blocks. The review is a child run whose input is enveloped
    data rather than instruction.
18. **The background review is a child run with four restrictions**:
    `COMPLETED` parents only, logged and never fatal, no user-visible
    output, and the same approval requirement as any other write.
19. **Rollback is an `AgentSpec` edit or an archive**, and there is no
    rollback operation. Both mechanisms exist and both are audited.
20. **Skill packages are not artifacts.** Artifacts expire in thirty
    days and belong to a run; skills are configuration that outlives
    every run that used them. Same object store, no shared policy,
    and nothing sweeps it — an old pin has to keep resolving.
21. **Sixteen gates in a new thirteenth area, `skill`.** Ten at
    Milestone 8, which had none, and six at Milestone 10, which had
    none.
22. **The self-improvement proof becomes harness case 27**, in the
    capability category at Milestone 8, without renumbering the
    twenty-six.

## Consequences

- Milestone 8 becomes implementable and the readiness table has no
  Split verdict left. The types exist, the storage has a shape, the
  reference grammar has a parser, the context cost has a number, and
  the milestone has gates.
- Sixteen hard gates are added. The registry gains a thirteenth area
  and goes from one hundred and eighteen entries to one hundred and
  thirty-four. Milestone 10 acquires a census row, which it did not
  have, and the map's sentence that Milestones 10 and 11 add none is
  corrected. ADR-0027 and ADR-0029 are not edited, because each is a
  record of what was true when it was decided.
- The map's open question 3 — whether Milestone 8 should acquire
  gates — is answered yes, by this document.
- `tool-system.md` is edited in three lines: `skill_manage` is no
  longer called a control tool, its idempotency class becomes
  `CONDITIONALLY_IDEMPOTENT`, and its scope becomes `skill.write`.
  Nothing else in Section 8.4's design changes, and the control-tool
  table stays at four entries.
- `policy-and-approvals.md` keeps `ActionKind.SKILL_AUTHORING` and
  loses one clause of its rationale. Its Section 30.4 citation
  becomes 30.3.
- `context-engine.md` gains two rows in the assembly-order table, two
  in the budget table, and a field on `ContextPlan`. The renumbering
  is contained to the assembly order, which no gate anchors to.
- The harness gains one case, numbered 27, and one row in the per-spec
  table.
- The eleven documents that reference skills now have something to
  reference. `readiness.md`'s Milestone 8 and Section 30 verdicts are
  rewritten, and its two citation errors are corrected.
- Milestone 8 was the last milestone with a Split verdict and Section
  30 was the largest undesigned area in the corpus. Neither is now.

## Alternatives considered

- **Keeping `skill_manage` a control tool and relaxing the
  control-tool constraints**: rejected. The constraint that a control
  tool has `side_effect: NONE` is what makes the closed set safe to
  exempt from approval reasoning, and relaxing it for one tool
  relaxes it for the category. The classification was the error, not
  the constraint.
- **`NON_IDEMPOTENT`, as written**: rejected on two grounds. It fails
  the classifier against a scope that writes, and it means a crash
  between the write and the commit leaves the invocation permanently
  `UNCERTAIN` with no safe recovery — for an operation that has a
  natural idempotency key sitting in its arguments.
- **A `skill.read` scope**: rejected. No route and no tool would
  check it in 0.1. A scope nobody checks is a scope that gets granted
  by default and audited as though it meant something.
- **A `GET /v1/skills` route or an `agent skill` CLI command**:
  rejected for 0.1 and recorded as an open question. The route table
  is thirteen routes and the CLI is twelve commands, both closed, and
  the substrate needs neither. An operator who has to read PostgreSQL
  to find out what an agent knows will not audit it, so this is
  likely a CLI command at Milestone 10.
- **Injecting skill bodies into the prefix**: rejected. It is the
  cheapest possible implementation and it puts author-supplied
  instructions in the position of platform instructions, which is the
  prompt-injection surface Section 30.3 exists to close.
- **Taking the catalog's 1,500 tokens from the tool or memory
  budgets**: rejected. The ceiling is a sum, not a constant, and
  reusing capacity sized for one subsystem is how two subsystems end
  up degrading each other with no single owner of the regression.
- **Evicting the oldest loaded skill on a third load**: rejected. The
  evictor cannot know which procedure is finished, and a procedure
  that vanishes mid-execution produces confident wrong behaviour that
  nobody diagnoses.
- **Ranking catalog entries by relevance before truncating**:
  rejected. The model's selection is already the ranking; a hidden
  pre-rank makes selection failures unattributable.
- **Refusing to load a skill whose `required_tools` are absent**:
  rejected, and `tool-system.md` had already rejected it. Partial
  applicability is the common case.
- **Letting a skill package register a tool**: rejected. The registry
  accepts entries from exactly two sources, and a third source that
  an agent can write to is a tool-authoring loop wearing a skill's
  clothes.
- **Recomputing `trust` from `source` at read time**: rejected. A
  recomputed label can be recomputed differently after a refactor,
  and every event that referenced the old one silently becomes wrong.
- **Letting `skill_manage` write a revision of a platform or MCP
  skill**: rejected. `source` lives on the identity row precisely so
  that a skill cannot change hands.
- **Storing skill packages as artifacts**: rejected. Artifacts expire
  after thirty days by ADR-0029's decision 18, and an expired package
  breaks every pin that referenced it.
- **Reference-counting or sweeping the package store**: rejected for
  the same reason. The audit story requires that an old pin still
  resolve, indefinitely.
- **Per-agent skill ownership via `authored_by_agent_id`**: a real
  alternative reading of Section 30.2's "may edit only skills it
  created", narrower and implementable, and recorded as an open
  question. Not chosen because nothing in the plan asks for it and it
  would make skills the only tenant-scoped resource with a sub-tenant
  owner.
- **Allowing authoring from untrusted turns with approval as the only
  gate**: rejected. An approval prompt generated from injected
  content is an approval prompt an operator cannot evaluate, and the
  background review already covers the legitimate case.
- **A rollback operation on `skill_manage`**: rejected. Pinning a
  revision in `AgentSpec` and archiving a bad one are both existing,
  audited mechanisms, and a third would be a second way to spell the
  same state change.
- **Splitting the gates across `context`, `tool`, and `policy`**:
  rejected. One document owns all sixteen, and
  `gate.skill.metadata_only` and `gate.skill.authoring_trust` would
  land in different areas despite being two halves of one governance
  story. `memory` and `sandbox` are the precedent.
- **Renumbering the harness cases**: rejected, as in ADR-0029. A case
  added later takes the next integer and no case is ever renumbered.
