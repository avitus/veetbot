# ADR-0024: The composition root and the configuration layers

- Status: Accepted
- Date: 2026-07-25
- Related: Milestones 0, 1, 2, 3, Sections 4 (repository layout), 5
  (dependency rules), 7 (port interfaces), 10.5 (model routing), 10.7
  (provider profiles), 11.1 (the initial context builder), 15 (policy
  configuration), 16 (authentication, health, and readiness), 17 (the CLI
  contract), 25 (local development), 26 (the first assignment), 28
  (sandbox isolation), ADR-0001 (modular monolith), ADR-0007
  (provider-neutral reasoning state), ADR-0008 (sandbox isolation),
  ADR-0020 (context engine), ADR-0021 (tool execution pipeline),
  ADR-0022 (the gate registry), ADR-0023 (the run loop)
- Detailed design: `docs/plan/bootstrap-and-composition.md`

## Context

Nine detailed-design specifications now describe nine mechanisms. None of
them describes the process that constructs those mechanisms, and the plan
names that process in exactly two places: `config.py` and `bootstrap.py`,
two filenames in the Section 4 tree, one of which is also the subject of
dependency rule 14.

The gap is not stylistic. Milestone 1's acceptance is a single command,
`agent run "What is 17 multiplied by 23?"`, producing six lines of flow.
Every specification in the corpus describes something that command passes
through. What no specification describes is the interval before any of it
runs: a process reading configuration, deciding which adapter satisfies
which port, constructing them in an order that respects seventeen
separate startup constraints stated across nine documents, and handing
the result to a CLI command that Section 17 forbids from containing a
second runtime loop.

Four specific things are undecided, and each one blocks work rather than
merely leaving it underspecified.

**Configuration has no shape.** The corpus declares 106 configuration
knobs and the plan names three environment variables. Read literally, the
definition-of-done item "New configuration appears in `.env.example`"
turns all 106 into environment variables — which contradicts
`policy_version`, defined as `{profile_name}@{profile_sha256[:12]}
+h{hardline_sha256[:8]}`, a hash of the file the rules came from and
recorded on every policy decision. An environment variable that changed
an effective rule would leave that hash untouched. No document says which
reading is intended, and there is no name for the settings class, no list
of keys, no precedence, and no environment variable for reaching the
PostgreSQL database the plan makes the source of truth.

**Startup order is stated seventeen times and assembled zero.** Hardline
rules frozen at load. Policy profiles compiled once and frozen.
Determinism ports before anything reads ambient time. Reserved tool
domains as a startup error. Trust labels forced at registration.
Production refusing the development sandbox and refusing any `eval.`
identity. Authentication configured or startup fails. Readiness that must
not call a provider. Each is correct and local; nothing states the
sequence they compose into, and three more constraints — migrations,
provider pinning, contract-module coverage — read like startup
constraints and are not.

**Milestone 1 rests on three bullets with no bodies.** "In-memory
repositories" does not say which ports, in which module, with which
guarantees, or whether the result is production code. "Inline run
dispatcher" names a port that Section 7 lists without a Protocol body —
no method, no signature, no statement of whether it runs the loop or
enqueues. "Minimal context builder" points at a specification whose hard
gates are all on Milestone 7 and whose build sequence carries no
milestone tags at all.

**Two milestone assignments conflict outright.** Milestone 1's acceptance
criteria require that every state transition be represented by an event,
while append-only event storage is a Milestone 2 implement item. The
transaction-hygiene check is placed in Milestone 0 by the plan and its
gate is tagged Milestone 2 by the runtime specification, and Milestone 0
has no database code to walk.

## Decision

1. **The composition root is the only module that knows both a port and
   its adapter.** Every other module knows one or the other. This is
   dependency rule 14 restated as a property rather than a prohibition,
   and it is what makes the corpus's substitutions — in-memory for
   PostgreSQL, fake provider for OpenAI, inline dispatcher for the queue
   — configuration changes rather than code changes.
2. **A value is an environment variable if and only if it differs
   between two deployments of the same revision and cannot be
   committed.** That test sorts all 106 declared knobs into committed
   YAML and leaves eight fields in `Settings`: the database URL, the
   deployment mode, the authentication mode and token, the sandbox
   mechanism, an optional overlay directory, a credential mapping, and
   the interpolation allow-list.
3. **The environment never overrides a file. It is interpolated into one
   at named points.** Section 10.5 already writes `model: ${OPENAI_MODEL}`;
   this generalizes that form and forbids the other. A blanket override
   would let a deployment change an effective policy rule without
   changing the hash the rule is recorded under, which makes the audit
   trail lie rather than merely go stale.
4. **Configuration YAML lives beside the package that owns it**,
   following the single precedent the plan set with
   `policy/hardline.yaml`, with an operator overlay directory merged
   file-by-file over the shipped defaults. The merged document is what
   gets hashed, so two deployments running the same overlay produce the
   same `policy_version`. `hardline.yaml` is the one file the overlay may
   not touch; attempting it is a startup error, because an overlay is
   configuration and the hardline set is defined as what configuration
   cannot disable.
5. **Startup is five phases, ordered by what each may touch**: refuse
   (settings only, nothing constructed), determinism (`Clock` and
   `IdFactory`, before anything reads ambient time), resources (engine,
   session factory, clients — a factory, never a session), freeze (every
   versioned asset loaded, hashed, and made immutable, hardline first),
   and wire (adapters, services, and the role's background machinery).
   All seventeen stated constraints land in one of the five, and the
   three that are not startup constraints are named as such.
6. **The composition root never runs migrations.** It asserts the schema
   revision and refuses to start on a mismatch. Migrating on boot means
   N processes racing during a rolling deploy, and a process that failed
   to start having already changed the schema.
7. **`build` is one async context manager returning application services
   only.** No adapter, repository, or session factory is reachable from
   a `Composition`. Narrowing the return type is how ADR-0023's
   reservation of `RunRepository.transition` to `runtime/executor.py`
   survives the addition of a second entry point.
8. **The in-memory tier is five adapters, not test doubles**, exercised
   by the same contract suites as their PostgreSQL counterparts, with
   each adapter declaring which capability groups it satisfies against a
   checked-in table so a skip is a reviewed fact rather than a runtime
   accident. There is no in-memory `RunQueue`: its entire content is
   `FOR UPDATE SKIP LOCKED` and lease fencing, which is the one thing a
   simulation cannot tell the truth about.
9. **`RunDispatcher` has one method, and both adapters satisfy the same
   postcondition** — after `dispatch` returns, the run will be executed
   exactly once. Inline and PostgreSQL differ in latency, not semantics,
   so a caller may never treat a returned `dispatch` as a finished run.
   `dispatch` is called after the creating unit of work commits, and the
   inline adapter asserts that no unit of work is open, which turns a
   Milestone 2 race into a Milestone 1 assertion failure.
10. **The Milestone 1 context builder is build-sequence step 1** —
    deterministic assembly, the two regions, and `prefix_sha256` from the
    first commit. Its test asserts both halves: the prefix hash is stable
    *and* the request bytes differ, in Region B only. The first half
    alone passes for a builder that puts everything in the prefix and
    never changes anything.
11. **The two milestone conflicts are resolved by separating two words
    each.** An event *repository* is Milestone 1; append-only event
    *storage* is Milestone 2 — one port, two implementations. The
    transaction-hygiene *check* is a Milestone 0 deliverable; the *gate*
    is a Milestone 2 acceptance criterion.
12. **The CLI's twelve commands get arguments, output streams, and exit
    codes.** Results go to stdout and progress to stderr; `get`, `events`,
    and `cancel` are reserved words after `agent run`; four options are
    added where the plan names none, each because a command is otherwise
    unusable rather than merely less convenient.
13. **The secret scanner is specified.** Five rule families, a report
    that never prints what it matched, an allowlist whose entries require
    prose, and `.env.example` scanned rather than exempted.

## Consequences

- Milestone 0 and Milestone 1 become implementable from the corpus alone.
  The twelve gaps a readiness pass found in those two milestones that
  concerned construction, configuration, or the vertical slice's missing
  bodies are closed here; what remains for those milestones is tooling
  detail — Makefile bodies, the compose file, the CI workflow — rather
  than design.
- Four static checks join the Milestone 0 import-boundary walk:
  `bootstrap` is imported only by entry points, no module outside
  `bootstrap.py` instantiates an adapter class, no module outside
  `adapters/determinism.py` reads ambient time or generates an
  identifier, and no `AsyncSession` exists at module scope. All four are
  true of an empty repository and stay true as it fills, which is the
  property Milestone 0's cross-reference paragraph asks for.
- The Section 4 tree gains eleven files and retires one name.
  `runtime/engine.py` becomes `loop.py`, `executor.py`, and
  `supervisor.py`; `ports/` gains `context.py`, `memory.py`, and
  `determinism.py`; `adapters/models/` gains the Anthropic, OpenAI-chat,
  and local-endpoint adapters that ADR-0002 and ADR-0012 require and the
  tree never listed; `adapters/persistence/memory.py` and `cli/sessions.py`
  are added. The tree is annotated, not redrawn.
- Six configuration files are added, and not one of them introduces a
  knob that does not already exist somewhere in the corpus. The 106
  declared knobs acquire homes and the environment layer stays at eight
  fields.
- Changing a knob for one deployment now requires committing a file or
  adding an interpolation point. This is a real operational cost,
  accepted deliberately: the escape hatch it removes is exactly what
  would make a `policy_version` unfalsifiable.
- Five open questions are recorded. The two with the highest reversal
  cost are the credential mapping's shape — flat today, a pool at
  Milestone 3 — and whether the plan's silence on CLI flags was
  minimalism rather than omission.

## Alternatives considered

- **A dependency-injection framework**: rejected by dependency rule 14
  before this document existed, and the specification here is what gives
  that rule something to check against. A container that resolves
  adapters by type would make "the only module that knows both a port and
  its adapter" unenforceable, because the answer would be "the container,
  and whoever asks it".
- **Environment variables overriding file configuration**, the
  conventional twelve-factor arrangement: rejected because
  `policy_version` is a hash of a file. Any override path that can change
  an effective rule without changing that hash converts the audit trail
  from stale to false, and the policy engine's whole claim is that a
  decision can be reproduced from its recorded version.
- **A top-level `config/` directory**: rejected for two reasons. It
  collides with the `config.py` module Section 4 already names, and it
  separates a knob from the code that reads it, which is the arrangement
  in which defaults drift from behaviour.
- **Running migrations at startup**: rejected. It is convenient in
  development and unsafe in every deployment with more than one process,
  and Section 25 already treats migration as a step separate from
  starting anything.
- **In-memory repositories as test doubles under `tests/`**: rejected.
  ADR-0001 defines replaceability as a port with a contract suite
  attached, and a double that ran against a different suite would be a
  second, unverified definition of what the port means. Milestone 1's
  entire value is that the slice it builds is the real thing with two
  adapters swapped.
- **An in-memory `RunQueue` so Milestone 1 exercises the worker path**:
  rejected. The queue's content is a PostgreSQL locking discipline; an
  in-memory version would pass its own tests and teach the wrong lesson
  about what the port guarantees.
- **Deferring the composition root until Milestone 2**, when there is
  more to compose: rejected for the same reason Milestone 0 places the
  structural gates against an almost-empty repository. A composition root
  written after the fact is written against whatever construction has
  already spread through the codebase, which is the situation in which
  rule 14 gets relaxed rather than obeyed.
