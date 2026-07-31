# ADR-0001: The modular monolith and mechanically enforced boundaries

- Status: Accepted
- Date: 2026-07-25
- Related: Milestones 0, 1, Sections 1 (the vertical slice), 2.1
  (architecture), 2.6 (scope discipline), 4 (repository layout), 5
  (dependency rules), 7 (ports), 20 (the evaluation framework), 21
  (milestone gates), ADR-0002 (provider-neutral model protocol), ADR-0003
  (event log and projections), ADR-0008 (sandbox isolation), ADR-0011
  (multi-device operation and the shared core), ADR-0022 (the gate
  registry and evaluation identity)
- Detailed design: `docs/plan/engineering-plan.md` Sections 2.1, 4, and 5;
  the enforcement mechanism is specified in
  `docs/plan/evaluation-harness.md`

## Context

Section 2 of the engineering plan is titled *Fixed architectural
decisions*, and its first entry is a modular monolith with explicit
interfaces between modules, chosen over microservices and over a
multi-agent architecture. Section 5 then lists fourteen dependency rules
that give the choice its teeth, and Section 4's repository layout names
`docs/adr/0001-modular-monolith.md` as the record of the decision. The
record was never written. Twenty ADRs numbered 0002 through 0021 exist,
each recording a decision made downstream of a foundation that is asserted
in the plan and documented nowhere.

That would be a bookkeeping problem if the decision were self-enforcing. It
is not. A modular monolith and a large ball of mud are the same artifact
until something checks the boundaries, and they degrade into each other
along a path nobody chooses deliberately: a repository import in the API
layer to avoid a service method, an ORM object returned because it is
already loaded, a provider SDK type in a port signature because it carries
exactly the fields needed. Each is locally reasonable. The plan
anticipates this and closes Section 5 with a single sentence — *"Add an
import-boundary test or static rule that verifies these constraints where
practical"* — which is the right instruction and leaves the interesting
half open. Which rules are practical to check, by what mechanism, and what
happens to the ones that are not?

Two things make that question answerable now rather than in Milestone 3.
The evaluation-harness spec defines structural gates as a first-class gate
kind that needs no runtime and is buildable in Milestone 0, and it gives
gates a registry, an identifier, and a milestone. The seven specs written
since have added rules of their own that belong in the same mechanism —
the tool system's single-call-site pipeline, the policy engine's single
`PROPOSED` to `AUTHORIZED` transition, and the evaluation harness's own
rule that production code may not import the evaluation package.

The plan also gives the decision a deadline. Section 4 says the design must
make every major component replaceable without changing the central domain
model or event format, and Milestone 3 introduces a second model provider,
Milestone 8 an MCP adapter, and Milestone 9 an external memory provider.
Each is a test of a boundary that will already have been crossed if nothing
was watching.

## Decision

1. **The architecture is a modular monolith with ports and adapters, and
   the decision is recorded here as accepted rather than proposed.** It is
   Section 2.1's fixed decision; this ADR documents the reasoning and the
   enforcement rather than reopening the choice.
2. **One deployable, several entry points.** The FastAPI application, the
   CLI, and the worker are three entry points into one codebase and one
   database, differing in what they invoke rather than in what they
   contain. Scaling is by process count, not by service decomposition.
3. **Replaceability is defined as a port with a contract, not as a service
   boundary.** A component is replaceable when it is reachable only through
   a Protocol in `agent_core/ports/`, that Protocol has a contract suite
   attached to it, and a second implementation can pass the same contract.
   Distribution is not what makes a component swappable; an enforced
   interface is, and it costs a great deal less.
4. **Section 5's fourteen dependency rules each get a named mechanism, and
   "where practical" is resolved rule by rule rather than left to
   judgement.** Every rule is either a registered structural gate with an
   identifier, or is recorded here as not mechanically checkable with the
   compensating control named. No rule is left in the middle.
5. **The import-boundary check walks the import graph; it does not grep.**
   It builds the module graph for `agent_core`, resolves relative and
   conditional imports, and asserts reachability rather than text. A grep
   for `from sqlalchemy` misses `import sqlalchemy as sa`, misses a
   re-export through an intermediate module, and produces false positives
   inside strings and comments. Eight of the fourteen rules are decidable
   this way:

    ```text
    1  domain imports only stdlib and Pydantic
    2  ports imports only domain
    3  runtime and application import only domain and ports
    5  api and cli reach application, never adapters or repos
    8  no fastapi module is reachable from application
    9  no tool module reaches the model gateway
    10 the model gateway reaches no tool implementation
    11 the policy engine reaches no prompt or model-gateway module
    ```

6. **Four rules need a different static mechanism, and each gets one.**
   Rule 6 (provider SDK objects must never cross adapter boundaries) and
   rule 7 (SQLAlchemy ORM objects must never be returned from
   repositories) are signature checks: the port Protocols' parameter and
   return annotations are resolved, and each must be either a type defined in
   `agent_core.domain` or a value type from the standard library or `typing` —
   `str`, `int`, `UUID`, `datetime`, `Mapping`, `Sequence` and their kind. What
   fails is a type defined in an adapter module: a provider SDK class, an ORM
   model, a driver's connection or row type. The rule exists to keep adapter
   vocabulary out of port signatures, not to forbid `str`. Rule 13 (no global singleton database
   sessions) is a module-scope check for engine or session objects assigned
   at import time. Rule 14 (explicit construction in `bootstrap.py`, no
   dependency-injection framework) is a dependency-manifest check against a
   denylist plus an assertion that construction happens in one module.
7. **Rule 12 is the secret scanner, not an import rule.** "Secrets must
   never be stored in domain events" is a statement about values rather
   than about dependencies. It is enforced by the scanner over captured
   output that the model gateway spec already requires, and it is
   registered as a structural gate in the same registry so it is counted
   with the others.
8. **Rule 4 is a registration check.** "Adapters implement ports" is
   asserted by the same mechanism ADR-0022 uses for contract coverage:
   every adapter registers against a port, an adapter registered against
   no port fails the build, and a port with no contract module fails the
   build. The permissive half of the rule — adapters may depend on
   external SDKs — needs no check, being the absence of a restriction.
9. **Two residues are recorded as not mechanically checkable, with
   compensating controls.** Rule 6's runtime half — an SDK object passed at
   run time through a parameter annotated as a domain type — is caught by
   the contract suite rather than statically, since a fake and a real
   adapter passing the same contract cannot be exchanging provider-specific
   objects. Rule 11's second half — *"must not depend on model judgment"* —
   is semantic and no static check reaches it; ADR-0005's determinism and
   totality gates carry it instead, since a policy engine that consulted a
   model could not produce identical decisions across repeated evaluations
   of the same input. Naming these two is the point: an unstated residue is
   assumed to be covered.
10. **The rule set is extended by later specs and lives in one place.** The
    boundary table is not closed at fourteen. The specs written since add
    the tool pipeline's single call site, the policy engine's single
    `PROPOSED` to `AUTHORIZED` transition, the transaction-hygiene check,
    and — from ADR-0022 — that no module under `agent_core` outside
    `agent_core.evals` may import `agent_core.evals`. That last one is the
    structural form of "there is no test mode", and it belongs to this ADR
    as much as to that one: it is a dependency rule, and it is enforced by
    the same walk.
11. **The boundary gates are Milestone 0 work.** They run against an
    almost-empty repository, cost nothing, and stay correct as it fills.
    Adding them later means adding them against violations, which is the
    situation in which they get relaxed instead of obeyed.
12. **A boundary change is an ADR, not a test edit.** Adding an allowed
    edge to the rule set requires a record; the gate identifier is the join
    between the rule, the ADR that justifies it, and the check that runs
    it. Without that rule the enforcement mechanism becomes the thing
    people edit when it is inconvenient, which is worse than not having it,
    because the green build then certifies whatever the boundaries most
    recently became.
13. **The monolith is not a permanent commitment, and the exit is
    identified.** ADR-0008 already carves out sandbox execution as a
    separately deployable execution service, because it isolates untrusted
    code and has a genuinely different security boundary. That is the
    shape any future extraction takes: a component already behind a port,
    with a contract suite, extracted because of an isolation or scaling
    requirement that is measured rather than anticipated. Section 2.6's
    prohibition on microservices is a prohibition on starting there, not a
    vow.

## Consequences

- The plan's foundational decision has a record, and the ADR sequence has
  no gap. Twenty existing ADRs stop referring implicitly to a decision that
  is documented nowhere.
- Section 5's "where practical" becomes a resolved question: eight rules by
  import graph, four by other static checks, one by the secret scanner, one
  by registration, and two residues named with their compensating controls.
  The instruction is unchanged; the discretion in it is now spent.
- The import-boundary walk is a Milestone 0 deliverable and appears in the
  gate registry alongside the other structural gates, which is what lets
  the definition of done's boundary claim be reconciled against a test run
  rather than asserted.
- Every future port acquires a contract obligation before it can merge,
  which is where most of the ongoing cost of this decision lands. It is
  paid at the moment a port is added and it is what makes the Milestone 3,
  8, and 9 second implementations cheap.
- Development stays simple in the ways a monolith is simple: one
  transaction boundary, one deployment, one place to set a breakpoint, no
  network between the policy engine and the thing it authorizes. The
  transaction discipline in Section 12.2 exists precisely because that
  simplicity is easy to abuse.
- The cost is that horizontal scaling is coarse. Every process carries
  every module, so the worker's memory footprint includes the API layer it
  never serves. This is accepted; the plan's scaling axis is worker count
  against a PostgreSQL queue, not per-component elasticity.
- A team large enough to want independent deploy cadences per component
  would feel this decision as friction. That is not the current situation
  and designing for it now would trade a real cost against a speculative
  one, which Section 2.6 forbids by name.

## Alternatives considered

- **Microservices from the start**: rejected by Section 2.6 and rejected
  again here on the merits. The platform's hardest correctness properties —
  an append-only log with projections, a run that pauses durably for an
  approval and resumes, exactly one function transitioning `PROPOSED` to
  `AUTHORIZED` — are all easier inside one transaction boundary. Splitting
  them across services converts every one into a distributed-consistency
  problem before there is a scaling reason to have one.
- **A multi-agent architecture as the base unit**: rejected; Section 1 asks
  for a single-agent vertical slice first, and subagents arrive in
  Milestone 6 as a feature of a working runtime rather than as its
  substrate. An architecture where the unit of composition is an agent
  makes the policy engine's job harder for no gain at this stage.
- **Enforcing boundaries by convention and code review**: rejected. It is
  the default outcome and it fails slowly and invisibly, which is the worst
  failure profile available. Reviewers approve locally reasonable changes;
  the boundary erodes across many of them and no single review is wrong.
- **A grep-based or text-based boundary check**: rejected as false comfort.
  It misses aliased imports, re-exports through intermediate modules, and
  conditional imports, and it produces false positives on strings and
  comments, which is how a check gets disabled.
- **A dependency-injection framework to manage the wiring**: rejected by
  Section 5's fourteenth rule and consistent with it here. Explicit
  construction in one module is itself a readable statement of the
  dependency graph, and a framework would make rule 14 unenforceable by
  moving the graph into configuration.
- **Making every rule a gate, including the semantic ones**: rejected. A
  gate for *"must not depend on model judgment"* would have to be either a
  proxy check or a check that passes vacuously, and a vacuous gate reports
  green, which ADR-0022 identifies as worse than an absent one.
- **Deferring this record until the evaluation harness is implemented**:
  rejected; it inverts the order. The enforcement mechanism is a Milestone
  0 deliverable specifically so the boundary is checkable from the first
  commit, and the decision it enforces should be recorded before the
  mechanism is built rather than after.
