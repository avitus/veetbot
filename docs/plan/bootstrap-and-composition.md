---
title: Bootstrap and Composition
status: design
canonical: true
---

# Bootstrap and composition

Nine specifications now describe nine mechanisms. Not one of them describes
the process that constructs them.

That gap has a precise shape. Milestone 1's acceptance is a single command —
`agent run "What is 17 multiplied by 23?"` — producing six lines of flow, and
every document in this corpus describes something that command passes through:
the context engine builds its request, the model gateway streams the response,
the tool system executes `math.calculate`, the policy engine authorizes it, the
runtime loop drives the turn, and the event log records it. What no document
describes is the twenty seconds before any of that: a process starting, reading
configuration, deciding which adapter satisfies which port, constructing them
in an order that respects seventeen separate startup constraints scattered
across those nine specs, and handing the result to a CLI command that must not
contain a second runtime loop.

Section 4 of the plan draws the repository tree. Section 5 gives fourteen
dependency rules, the fourteenth of which is `bootstrap.py` by name. Section 17
lists twelve CLI commands. Between them they name every file. They specify the
contents of none.

This document specifies that layer: the layout, the configuration object, the
composition root, the in-memory tier Milestone 1 runs on, the run dispatcher,
the minimal context builder, the CLI, and the four Milestone 0 checks that run
against an almost-empty repository. It is the last document written before
coding starts, and it is deliberately the least inventive of the nine — almost
everything here is a name the plan already chose, given a body.

## What this document is responsible for

The rule that governs every choice below: **the composition root is the only
module that knows both a port and its adapter.** Every other module knows one
or the other. That is dependency rule 14 restated as a property rather than a
prohibition, and it is what makes the rest of the corpus's substitutions —
in-memory for Postgres, fake provider for OpenAI, inline dispatcher for the
queue — a configuration change rather than a code change.

Three things follow, and they are the spine of this document:

1.  If construction lives in one module, that module must be able to construct
    everything. So it needs a settings object rich enough to select every
    adapter, and the settings object is specified here.
2.  If construction lives in one module, the order of construction is a
    property of that module, not of the components. So the seventeen
    startup-order constraints the specs state separately are collected here
    into one sequence.
3.  If construction lives in one module, every entry point shares it. The API,
    the CLI, and the worker are three `main` functions over one graph, and the
    difference between them is which surface they attach, not what they build.

## The repository layout

### What the plan's tree already fixes

Section 4 names 135 paths. They are not a suggestion — Milestone 0's last
acceptance criterion is "No application code exists outside the documented
module boundaries", and Milestone 0's import-boundary walk is what turns that
sentence into a test. The tree is therefore load-bearing, and this document
adds to it rather than redrawing it.

The eight top-level packages under `src/agent_core/` and their meanings are
unchanged: `domain` (value objects, Pydantic, no I/O), `ports` (Protocols over
`domain`), `application` (services the API and CLI call), `runtime` (the loop
and its supervisors), `context`, `models`, `tools`, `policy`, `adapters`,
`api`, `cli`, `evals`, plus `config.py` and `bootstrap.py` at the package root.

### Three places the tree and the specs disagree

Writing this document surfaced three conflicts between the Section 4 tree and
the nine specs. All three are resolved here, additively, in the same way the
superseded `RunRepository` methods were: the tree is annotated, not rewritten.

**One. `runtime/engine.py` becomes `runtime/loop.py` and
`runtime/executor.py`.** The tree names one module; ADR-0023 requires two,
because the whole point of that decision is that the code which computes an
outcome and the code which performs a terminal action are separately
reachable — and the structural gate names `runtime/executor.py` as the sole
permitted caller of `RunRepository.transition` and `RunQueue.release`. A gate
that names a file the tree does not contain is a gate that cannot run. The
split is the resolution:

```text
runtime/
  loop.py          -- run_loop; computes a RunOutcome, ends nothing
  executor.py      -- finalize; the only terminal writer
  state_machine.py -- unchanged
  budgets.py       -- unchanged
  retries.py       -- unchanged
  checkpoints.py   -- unchanged
  worker.py        -- unchanged; the claim/lease/heartbeat process
  supervisor.py    -- ADDED; the heartbeat task of ADR-0023
```

`engine.py` is not renamed to one of them. It is replaced by both, and the
name is retired so that no module can be written against the old boundary
without someone noticing.

**Two. There is no Anthropic adapter file.** `adapters/models/` contains
`fake.py` and `openai_responses.py`. Anthropic and OpenAI are co-equal first
adapters — that is a decision on the record — and the contract suite in
`evaluation-harness.md` runs against five adapters. The tree gains:

```text
adapters/models/
  fake.py               -- unchanged; the deterministic provider
  openai_responses.py   -- unchanged
  anthropic_messages.py -- ADDED
  openai_chat.py        -- ADDED; the chat-completions shape
  local_openai.py       -- ADDED; self-hosted, OpenAI-compatible
```

The last two are named because ADR-0012 requires open and self-hosted models
and the contract suite counts five adapters; naming them now costs nothing and
prevents the fifth from being written inside the fourth.

**Three. The CLI package has no `sessions.py`.** `agent session create` is a
command; `cli/` has `main.py`, `chat.py`, `runs.py`, `approvals.py`,
`evals.py`. The tree gains `cli/sessions.py`. This is the smallest of the
three and the most likely to have been an oversight rather than a decision.

### Where the ports live

`ports/` has nine modules and the corpus declares thirty-nine ports. The
arithmetic is forty-seven `Protocol` blocks naming forty-three distinct
types: four of the blocks re-declare a type an earlier document already
declared, and four of the types are the application services of
[http-api-and-streaming.md](http-api-and-streaming.md), which belong under
`application/` rather than here. Without an assignment rule, the first
implementer invents one, and the second invents a different one. The rule
is: **a port lives in the module named for the capability it abstracts, not
for the component that calls it.** The table below assigns all thirty-nine
and names fourteen modules where the tree has nine.

```text
ports/models.py
  ModelProvider          model-gateway.md
  ModelRouter            model-gateway.md

ports/repositories.py
  RunRepository          engineering-plan.md, runtime-loop.md
  CheckpointRepository   event-log-and-persistence.md
  ApprovalRepository     policy-and-approvals.md
  UsageRepository        model-gateway.md
  AgentRepository        runtime-loop.md
  BudgetLedger           runtime-loop.md
  PrincipalResolver      runtime-loop.md
  SkillRepository        skills.md

ports/events.py
  EventRepository        engineering-plan.md
  Projection             event-log-and-persistence.md
  Upcaster               event-log-and-persistence.md

ports/dispatch.py
  RunQueue               event-log-and-persistence.md
  RunDispatcher          this document
  CancellationToken      runtime-loop.md

ports/context.py         -- ADDED
  ContextBuilder         engineering-plan.md, context-engine.md
  ContextPlanner         context-engine.md
  TokenEstimator         context-engine.md
  Compactor              context-engine.md

ports/memory.py          -- ADDED
  MemoryRetriever        memory-retrieval-and-ranking.md
  QueryFormer            memory-retrieval-and-ranking.md
  Ranker                 memory-retrieval-and-ranking.md
  EpisodeSearch          memory-retrieval-and-ranking.md
  TraceStore             memory-retrieval-and-ranking.md
  the formation ports of memory-formation-and-consolidation.md

ports/knowledge.py       -- ADDED
  Extractor              knowledge-documents.md
  Chunker                knowledge-documents.md
  KnowledgeStore         knowledge-documents.md

ports/determinism.py     -- ADDED
  Clock                  runtime-loop.md
  IdFactory              runtime-loop.md

ports/credentials.py     -- ADDED
  CredentialResolver     sandbox-isolation.md

ports/tools.py
  Tool                   engineering-plan.md, tool-system.md
  ToolRegistry           tool-system.md
  the MCP ports of tool-system.md

ports/policies.py
  PolicyEngine           engineering-plan.md

ports/artifacts.py
  ArtifactStore          engineering-plan.md
  ArtifactWriter         sandbox-isolation.md
  SkillPackageStore      skills.md

ports/execution.py
  ExecutionEnvironment   engineering-plan.md
  WorkspaceHandle        sandbox-isolation.md

ports/telemetry.py
  the telemetry port
```

`ports/determinism.py` is the one grouping that is not obvious, and it is
deliberate. `Clock` and `IdFactory` have nothing in common as capabilities;
they have everything in common as a purpose. They exist so that a run is
reproducible, and grouping them by that purpose is what makes the negative
rule checkable: **no module outside `ports/determinism.py` and its adapters
may read ambient time or generate a random identifier.** That is a module-scope
static check of the same family as dependency rule 13, and it has somewhere to
point only because the two ports share a module.

`ports/knowledge.py` and `ports/credentials.py` are the other two groupings
that are not obvious, and both are new modules rather than rows in an
existing one because nothing already in `ports/` is named for what they
abstract. Knowledge is memory's sibling and would land in `repositories.py`
under a misreading of the rule as "anything that stores goes with the
stores" — `KnowledgeStore` does store, but extraction and chunking are the
capability and the store is downstream of them. `CredentialResolver` is not
policy, because policy decides whether a tool may run and the resolver
hands it the secret afterwards, and it is deliberately not `execution.py`:
the one structural thing [sandbox-isolation.md](sandbox-isolation.md) says
about it is that a tool whose target is the sandbox is handed a resolver
that raises for every reference, and a module boundary is the cheapest
place for that asymmetry to live.

The rest is the rule applied without argument. `SkillRepository` joins the
repositories for the reason
[multi-device-and-surfaces.md](multi-device-and-surfaces.md) puts the
device registry there. `SkillPackageStore` and `ArtifactWriter` both take
bytes and hand them back under a key, which is what `artifacts.py` is named
for; `ArtifactWriter` says as much itself, calling itself deliberately
narrower than `ArtifactStore`. `WorkspaceHandle` is what
`ExecutionEnvironment` returns.

Three rows still name no type: `ports/telemetry.py`, which is in the
Section 5 tree while no document declares a telemetry Protocol; the
formation half of the memory row, which is prose in
[memory-formation-and-consolidation.md](memory-formation-and-consolidation.md)
rather than a `Protocol` block; and the MCP row, whose adapter imports
`ports` and `domain` and may well implement `Tool` and `ToolRegistry`
rather than add a third. This is worth naming here and not only there,
because [evaluation-harness.md](evaluation-harness.md) gates on a walk of
`agent_core/ports/` that demands one contract module per Protocol, and a
port that exists as a sentence has neither a Protocol nor a contract.

### The layout additions in full

```text
src/agent_core/
  runtime/loop.py            replaces engine.py, with executor.py
  runtime/executor.py        replaces engine.py, with loop.py
  runtime/supervisor.py      heartbeat, deadline, cancellation watch
  ports/context.py           context engine ports
  ports/memory.py            memory formation and retrieval ports
  ports/determinism.py       Clock, IdFactory
  ports/knowledge.py         extraction, chunking, the store
  ports/credentials.py       CredentialResolver
  adapters/models/anthropic_messages.py
  adapters/models/openai_chat.py
  adapters/models/local_openai.py
  adapters/memory/           in-memory and Postgres memory stores
  cli/sessions.py            agent session create
tests/
  gates/                     ADDED; the structural gates of Milestone 0
  contract/                  named by the harness; not in the plan tree
```

`tests/gates/` and `tests/contract/` are named by `evaluation-harness.md`'s
import table and are absent from the Section 4 tree, which lists `unit`,
`contract`, `integration`, `resilience`, `security`, `live`, and `eval_cases`.
`contract/` is present after all; `gates/` is the only genuine addition.

## Configuration

### The problem the settings object actually has

The corpus declares **106 configuration knobs** across the nine specs, and the
plan names **three environment variables**: `AUTH_MODE`, `OPENAI_MODEL`, and
`RUN_LIVE_MODEL_TESTS`. Those two facts are not in tension by accident. Read
the 106 and the pattern is obvious: they are almost all tuning values —
`MAX_COMPACTIONS_PER_STEP = 2`, the RRF constant `k = 60`, the 15,000-token
prefix ceiling, the three approval expiry windows, the 8-way parallel-batch
cap. Not one of them differs between two deployments of the same revision.
They differ between *revisions*, which is another way of saying they belong in
files that get reviewed and committed, not in an environment that gets edited
at three in the morning.

The corpus already relies on that. `policy_version` is
`{profile_name}@{profile_sha256[:12]}+h{hardline_sha256[:8]}` — a policy
version is *a hash of the file its rules came from*, recorded on every
decision the engine makes. An environment variable that changed an effective
rule would leave the hash untouched and the audit trail lying. The plan says
the same thing in prose at Section 15: "Policy rules themselves are
version-controlled files, not rows." Generalize it and the rule that sorts all
106 falls out.

**A value belongs in the environment if and only if it differs between two
deployments of the same revision and cannot be committed.** Everything else is
a checked-in file. The test is mechanical, and it puts credentials, the
database address, and the deployment's identity in the environment, and all
106 tuning knobs in YAML.

### The three layers, and why only one of them is a precedence chain

Configuration is assembled in three layers, and the interesting property is
that **the environment never overrides a file**.

1.  **Shipped defaults.** YAML committed inside the package, next to the
    module that owns it. This is where all 106 knobs live, at the values the
    specs state.
2.  **The operator overlay.** An optional directory, named by
    `AGENT_CONFIG_DIR`, whose files are merged over the shipped defaults by
    top-level key. Section 10.7 requires this — a provider profile is "a
    plugin the registry loads and the user can override without editing
    core" — and file-over-file merging keeps the result hashable, diffable,
    and reviewable, which an environment override would not.
3.  **Named interpolation.** Inside a YAML value, `${VAR}` resolves against
    `Settings.interpolation`. The plan already writes this at Section 10.5:
    `model: ${OPENAI_MODEL}`. Interpolation happens only where a file
    explicitly asks for it.

So there is no precedence chain to reason about at a given key. Either the
overlay supplies that key or the shipped default does, and either the value
contains a `${VAR}` or it does not. The merged document — after overlay,
before interpolation — is what gets hashed, so two deployments running the
same overlay produce the same `policy_version` and two running different
overlays produce different ones. That is the property the audit trail needs.

The cost is real and is accepted deliberately: **changing a knob for one
deployment requires either committing a file or adding an interpolation
point.** There is no escape hatch. That is the point of the design; an escape
hatch is exactly the thing that makes a `policy_version` unfalsifiable.

### Where the files live

The plan set one precedent — `src/agent_core/policy/hardline.yaml` sits inside
the policy package — and this document follows it rather than inventing a
top-level `config/` directory, which would in any case collide with the
`config.py` module Section 4 already names.

```text
src/agent_core/
  policy/hardline.yaml     the frozen never-bypassable set
  policy/default.yaml      the v0.1 policy profile
  models/policies.yaml     model_policies and provider profiles
  models/catalog.yaml      aliases, limits, context windows, prices
  context/plan.yaml        region caps, reserves, the 15,000 ceiling
  tools/limits.yaml        registry ceilings, breaker thresholds
  runtime/limits.yaml      leases, sweep cadences, priority classes
  memory/profiles.yaml     snapshot caps, RRF k, recall profiles
```

The two under `policy/` are already named by
[policy-and-approvals.md](policy-and-approvals.md). The other six are
additions, and each one is a home for knobs a spec has already fixed a value
for — none of them introduces a knob that does not already exist.

The count is executable rather than prose. `SHIPPED_KNOB_PATHS` in
`agent_core.config` names every operator-reviewable dotted path, and a static
test resolves every path from its shipped YAML document, rejects null values,
and asserts the total is 106. Schema versions, profile names, rule identifiers,
model-catalog records, conditions, and frozen hardline predicates are metadata
or invariants rather than knobs and are not counted.

| File | Knobs |
| --- | ---: |
| `policy/default.yaml` | 23 |
| `models/policies.yaml` | 4 |
| `context/plan.yaml` | 26 |
| `tools/limits.yaml` | 20 |
| `runtime/limits.yaml` | 16 |
| `memory/profiles.yaml` | 17 |
| **Total** | **106** |

Five required operational defaults had no numeric value in the corpus. ADR-0036
sets the initial values: a 4 MiB global tool-output ceiling, a 30-second worker
lease, and default run caps of 32 steps, 16 model calls, and 32 tool calls. The
values are versioned alongside the 101 values already fixed by the specs, so a
later evidence-backed change is an ordinary reviewed configuration diff rather
than an environment override.

`hardline.yaml` is the one file the overlay may not touch. Section 15 requires
the hardline set to be frozen at load "so no configuration or in-process code
can disable them", and an overlay is configuration. Attempting to overlay it
is a startup error, not a silent no-op.

### The settings object

```python
@dataclass(frozen=True)
class Settings:
    """The environment layer. Nothing here is a tuning knob."""

    database_url: str
    deployment_mode: DeploymentMode      # development | production
    auth_mode: AuthMode                  # dev | token
    auth_token: SecretStr | None
    sandbox: SandboxMechanism            # microvm | gvisor | docker | fake
    config_dir: Path | None              # operator overlay directory
    credentials: Mapping[str, SecretStr]
    interpolation: Mapping[str, str]     # what ${VAR} resolves to
```

Eight fields, and the whole of `config.py` is this class plus the loader that
fills it. `credentials` is keyed by provider profile name rather than by a
fixed set of provider fields, because Section 10.7 makes providers plugins;
adding a provider must not require editing this class. `interpolation` is the
allow-list of names a YAML file may reference, so a typo in a config file
fails at load with the offending name rather than silently producing an empty
model identifier.

`sandbox` carries four values rather than three. `fake` is added by
[sandbox-isolation.md](sandbox-isolation.md) as a production adapter in the
sense the plan uses for the in-memory repositories, a real implementation of
the port that runs the contract suite unchanged
(`sandbox-isolation.md:1252`), and it is what lets the whole system be
exercised without a hypervisor. Startup check 4 below refuses it in
production beside `docker`.

`SecretStr` is the one place a Pydantic type is welcome in a value object.
Dependency rule 6 keeps Pydantic-only behaviour out of the domain, and
`Settings` is not a domain model — it is the boundary where credentials enter
the process, and a type whose `__repr__` cannot leak is worth having exactly
there. Coding standard 9 ("do not log full prompts or secrets") is then a
property of the type rather than a rule people have to remember.

### `.env.example`

The plan makes this file a definition-of-done item for every milestone: "New
configuration appears in `.env.example`." That is satisfied for the environment
layer by this file and for the 106 file-layer knobs by their
appearance in a committed default — the requirement is that no configuration
is undocumented, and both layers meet it. The alternative reading, that all
106 knobs become environment variables, contradicts the `policy_version` hash
and Section 15's "version-controlled files, not rows", so it cannot be the
intended one.

```text
# Required in every deployment.
DATABASE_URL=postgresql+asyncpg://agent:agent@localhost:5432/agent
DEPLOYMENT_MODE=development

# Authentication. Section 16: token mode requires AUTH_TOKEN, and
# startup fails outside development if it is missing.
AUTH_MODE=dev
AUTH_TOKEN=

# One per enabled provider profile. Never a real value in this file.
VEETBOT_OPENAI_KEY=
ANTHROPIC_API_KEY=

# Interpolated into models/policies.yaml as ${OPENAI_MODEL}.
OPENAI_MODEL=

# docker is refused when DEPLOYMENT_MODE=production (ADR-0008).
SANDBOX_MECHANISM=docker

# Optional overlay directory merged over the shipped YAML defaults.
# Unset means run exactly as committed.
AGENT_CONFIG_DIR=

# Test-only. 1 enables the live-provider job (Section 20.4).
RUN_LIVE_MODEL_TESTS=
```

Ten canonical names for eight fields, which is not a discrepancy.
`VEETBOT_OPENAI_KEY` and `ANTHROPIC_API_KEY` both populate the `credentials`
mapping, which is keyed by provider profile name and so grows a name per
profile without growing a field. `OPENAI_API_KEY` remains an accepted
compatibility alias for the OpenAI profile; `VEETBOT_OPENAI_KEY` wins when both
are present. `OPENAI_MODEL` populates `interpolation`. `RUN_LIVE_MODEL_TESTS`
populates no `Settings` field at all: it is read by the test harness to enable
the live-provider job, and it is listed here because the definition-of-done
rule is that no configuration is undocumented, not that every name is a field.
The remaining six names map one-to-one onto the six scalar fields.

`DATABASE_URL` does not appear anywhere in the plan. The plan names PostgreSQL
as the source of truth in Section 2, gives the schema in Section 14, and
requires migrations that "upgrade from a clean database" — and never says how
the process finds the database. This is an omission rather than a decision,
and the variable is added here under the name every SQLAlchemy deployment
already uses.

### Validation happens before construction

`config.py` exposes one function, `load_settings()`, and it either returns a
valid `Settings` or raises. It does not warn, and it does not fall back. Six
checks run there, before any adapter exists:

1.  Every required variable is present and parses. A missing `DATABASE_URL`
    is an error, not an empty string.
2.  `auth_mode == "token"` implies `auth_token is not None`. Section 16: "In
    non-development mode, startup must fail if authentication is not
    configured."
3.  `deployment_mode == "production"` implies `auth_mode != "dev"`. The dev
    mode is documented as localhost-only, and localhost-only is not a
    production posture.
4.  `deployment_mode == "production"` implies `sandbox` is neither `docker`
    nor `fake`. ADR-0008: "Production startup must refuse to run untrusted
    code under the development fallback." `fake` is behind the same check
    because it executes nothing (`sandbox-isolation.md:1605`), and a
    mechanism that executes nothing isolates less than the fallback this
    rule was written for.
5.  `config_dir`, if set, exists and contains only files that mirror a shipped
    default name. An overlay file with no counterpart is a typo, and a typo in
    a config filename is otherwise invisible.
6.  Every `${VAR}` reachable in the merged YAML has a key in `interpolation`.

None of these is a warning, and none of them is deferred to first use. A
process that cannot serve requests correctly should fail while a deployment
system is still watching, not on the first request an hour later.

## The composition root

### One module, one function

Dependency rule 14 is "Use explicit dependency construction in `bootstrap.py`;
do not add a dependency-injection framework", and ADR-0001 turns it into a
check: "a dependency-manifest check against a denylist plus an assertion that
construction happens in one module." The assertion needs something to assert
against, and this is it.

```python
@asynccontextmanager
async def build(
    settings: Settings,
    role: DeploymentRole,
) -> AsyncIterator[Composition]:
    """The only function in the process that names an adapter."""
```

```python
@dataclass(frozen=True)
class Composition:
    settings: Settings
    ruleset: LoadedRuleset       # frozen; carries policy_version
    services: ApplicationServices
    background: Background | None  # None when role is "api"
```

```python
@dataclass(frozen=True)
class ApplicationServices:
    sessions: SessionService
    runs: RunService
    approvals: ApprovalService
    artifacts: ArtifactService
```

`build` is an async context manager rather than a plain function because the
things it constructs — a connection pool, HTTP clients, an execution-service
channel — have to be closed, and the process that owns construction is the
only one that can honestly own teardown. Every entry point is therefore a
thin `async with build(...) as composition:` around a surface.

`Composition` exposes **application services only**. No adapter, no
repository, no session factory is reachable from it. This is deliberate and
it is checkable: an entry point that could reach a `RunRepository` could
transition a run without going through `RunService`, and
[runtime-loop.md](runtime-loop.md) reserves `RunRepository.transition` and
`RunQueue.release` to `runtime/executor.py` alone. Narrowing the return type
is how that reservation survives contact with a second entry point.

### The five phases

The seventeen startup-order constraints stated across the corpus are not
seventeen independent orderings. They collapse into five phases, and the
phases are ordered by what each one is allowed to touch: phase 1 touches
nothing, phase 2 touches nothing external, phase 3 opens resources, phase 4
reads files and writes one event, phase 5 constructs.

**Phase 1 — refuse.** `load_settings()` has already run; this phase makes the
decisions that can be made from configuration alone, before anything is
built. Authentication configured or fail. Production refuses the development
sandbox. Production refuses any policy profile, principal, or tenant name
beginning with `eval.` or `tenant_eval`. The configured context plan's prefix
classes sum to at most 15,000 tokens. Nothing has been constructed yet, so
nothing has to be torn down, which is the entire reason these checks are
first rather than convenient.

**Phase 2 — determinism.** `Clock` and `IdFactory`, and nothing before them.
[evaluation-harness.md](evaluation-harness.md) places these in Milestone 1
"before anything depends on ambient time", and the composition root is the
first thing that would: the `policy.profile.loaded` event emitted in phase 4
has a timestamp and an identifier. Constructing the determinism ports first
means the negative rule — no module outside `ports/determinism.py` and its
adapters reads ambient time — holds for startup too, not only for runs.

**Phase 3 — resources.** The async engine and the `async_sessionmaker`, the
provider HTTP clients, the artifact-store handle, the execution-service
channel. The session *factory* is constructed here; a session is not.
Dependency rule 13 forbids global singleton database sessions, and the
distinction the rule turns on is exactly this one — a factory in the
composition is fine, a live `AsyncSession` in it is the bug the rule names.
Section 2.2 states the unit-of-work rule the factory exists to serve: "Each
request, worker operation, or parallel tool invocation must receive its own
unit of work and database session."

This phase also asserts the schema revision. It does not migrate. What it
compares against is specified in
[event-log-and-persistence.md](event-log-and-persistence.md): an
`EXPECTED_REVISION` constant in the persistence adapter, read against the
single row of `alembic_version`, rather than a head computed from the
migrations directory at runtime.

**Phase 4 — freeze.** Everything versioned is loaded, hashed, and made
immutable, in this order:

1.  `hardline.yaml`, first and un-overlayable, because Section 15 requires
    the never-bypassable set to be frozen "so no configuration or
    in-process code can disable them" — and anything loaded before it
    could, in principle, be what disables it.
2.  The policy profiles, compiled once and frozen, yielding `LoadedRuleset`
    and `policy_version`. `policy.profile.loaded` is appended here; this is
    the one write the composition root performs.
3.  Provider profiles and the model catalog, so that "adding a model is
    configuration, not code" is true of a running process.
4.  The tool registry. Reserved-domain enforcement (`mcp` and `device` are
    a startup error for a builtin) and forced trust labels (`MCP`,
    `DEVICE`, and `SANDBOX` sources are overwritten to
    `EXTERNAL_UNTRUSTED`) both happen at registration, which means they
    happen here or they do not happen at all.
5.  The compaction prompt, whose version is recorded on every checkpoint it
    produces and therefore has to be pinned before a checkpoint can exist.

MCP capability negotiation — declining `sampling/createMessage` and `roots` —
joins this phase in Milestone 6, when servers are first connected. Its
placement is the same: negotiation is registration, and registration is
phase 4.

**Phase 5 — wire.** Adapters are selected from `settings`, application
services are constructed over them, and the role's background machinery is
attached. `api` attaches none. `worker` attaches the claim loop, `run_loop`,
`finalize`, and post-run hook enqueue. `maintenance` attaches the four
sweeps, each behind its advisory lock. All three are the same binary with a
role flag, so the flag selects a branch in this phase and nowhere else.

Readiness is wired here too, and wired to the database and to
`Composition.settings` — never to a provider client. Section 16: "Readiness
should verify the database and critical configuration, but it should not call
a model provider on every probe." Placing the probe in the composition root
rather than in the API module is what makes that easy to keep true, because
the probe is built from a `Composition` that has no provider client on it.

### Where each stated constraint lands

| Constraint | Source | Phase |
| --- | --- | --- |
| Ports defined before adapters | plan §7 | authoring, not runtime |
| Explicit construction, no DI framework | rule 14 | the shape of all five |
| No global singleton DB sessions | rule 13 | 3 |
| Hardline rules frozen at load | plan §15 | 4, first |
| Policy profiles compiled once, frozen | policy spec | 4 |
| Reserved domains are a startup error | tool spec | 4 |
| Trust labels forced at registration | tool spec | 4 |
| Sampling and roots declined | tool spec | 4 (M6) |
| Determinism before ambient time | eval spec | 2 |
| Authentication configured or fail | plan §16 | 1 |
| Production refuses the dev sandbox | ADR-0008 | 1 |
| Production cannot load an eval identity | eval spec | 1 and 4 |
| Readiness must not call a provider | plan §16 | 5 |
| Prefix classes fit the 15,000 ceiling | context spec | 1 and per session |
| Role-conditional composition | runtime spec | 5 |
| Migrations upgrade cleanly | plan §25 | not startup |
| A port with no contract module fails | eval spec | build gate |

Three of the seventeen are listed as *not* startup constraints, and saying so
is part of the specification rather than an omission from it.

Migrations are the important one. **The composition root never runs
migrations.** It asserts that the database is at the revision the code
expects and refuses to start otherwise. Migrating on boot means N processes
racing to migrate the same database during a rolling deploy, and it means a
process that failed to start has already changed the schema. Section 25
already treats migration as a step ("Run migrations") separate from starting
anything. The mechanism this leaves open — where the expected revision comes
from, and why it is a constant rather than a computed head — is specified in
[event-log-and-persistence.md](event-log-and-persistence.md) under "The
revision the code expects is a constant", along with the migration-authoring
conventions the assertion is only as good as.

Provider pinning is per run, not per process: ADR-0007 fixes routing "once at
run start", which is `RunService`'s concern. And contract-module coverage — "a
port with no contract module is a build failure" — is checked when the suite
is collected, not when a process starts.

### What the composition root must not do

Five prohibitions, each of which is a static check rather than a convention:

1.  **It is imported only by entry points.** `bootstrap` may be imported by
    `api/main.py`, `cli/main.py`, and the worker entry, and by nothing else.
    A service that imports `bootstrap` has found a way to construct its own
    dependencies, which is rule 14 defeated from the inside. This is one
    edge in the import-graph walk Milestone 0 already builds.
2.  **It constructs no `AsyncSession`.** Rule 13, checked at module scope.
3.  **It imports nothing from `agent_core.evals`.** The general rule — no
    production module may import the evals package — has no exception for
    the composition root, and the composition root is the module most
    tempted to make one.
4.  **It calls no model provider.** Constructing a client is phase 3;
    calling one is a run.
5.  **It holds no module-scope state.** `build` is a function. There is no
    module-level container, no `get_container()`, and no import-time side
    effect — which is what "do not add a dependency-injection framework"
    forbids in practice, whatever the framework is called.

### The three entry points

```text
api/main.py        role=api            FastAPI app over services
cli/main.py        role=api            twelve commands over services
runtime/worker.py  role=worker         claim loop; or role=maintenance
```

The CLI builds the same graph as the API and calls the same application
services. Section 17 is unambiguous about why: "The CLI must call the same
application services as the API. Do not implement a second runtime loop
inside the CLI." A CLI that built its own graph would be one refactor away
from having its own loop, so it does not build its own graph.

The worker entry takes the role flag and attaches either the claim loop or
the sweeps. One binary, three roles, one `build`.

## Milestone 1: the in-memory tier

Milestone 1 names "In-memory repositories" in one bullet and never returns to
it. The bullet has to answer four questions before anyone can write the file:
which ports, in which module, with which guarantees, and whether the result is
production code or a test double.

### Which ports, and where

Five, in a new `adapters/persistence/memory.py`:

```text
AgentRepository            the one default agent from configuration
SessionRepository          create, get
RunRepository              create, get, transition
EventRepository            append, list_after
ToolInvocationRepository   record, list_for_run
```

`RunQueue` is deliberately absent. Milestone 1 uses the inline dispatcher, so
nothing claims and nothing leases, and an in-memory `RunQueue` would be a
simulation of the one mechanism — `FOR UPDATE SKIP LOCKED` with `lease_epoch`
fencing — that exists precisely because it cannot be simulated. Checkpoints
and approvals are absent for the same reason in reverse: nothing at Milestone
1 suspends, so there is nothing to store.

The module grows as ports arrive, but it never grows to cover a port whose
whole point is a database guarantee.

### They are adapters, not test doubles

They live under `adapters/`, they implement the same Protocols, and they are
exercised by the same contract suites as the SQLAlchemy implementations.
ADR-0001 defines replaceability as "a port with a contract suite attached to
it", and an in-memory implementation that ran against a different suite would
not be replaceable — it would be a second, unverified definition of what the
port means.

Some contract assertions cannot hold in memory. Crash recovery, transaction
rollback across repositories, and advisory-lock behaviour all require a real
database. The suite is therefore parameterized over adapters, and **each
adapter declares the capability groups it satisfies against a checked-in
table**. A group that an adapter does not declare is reported as
not-applicable, and a group it declares but fails is a failure. The declared
set is itself asserted against the table, so "skipped" is never something an
adapter can quietly award itself.

### What is guaranteed in memory

The invariants that are observable inside one process are held exactly:
per-session event `sequence` is monotonic with no gaps, appends are
append-only with no update path, and `transition` rejects a transition the
state machine does not allow. Each repository serializes its mutations behind
one `asyncio.Lock`, which is sufficient because Milestone 1 is one process and
the inline dispatcher runs one run at a time.

The invariants that are not observable in one process are not faked. There is
no durability, no cross-repository transaction, and no recovery. A Milestone 1
process that exits loses everything, which is correct for a vertical slice and
is why Milestone 2 exists.

### The event store at Milestone 1

Milestone 1's acceptance criteria include "Every state transition is
represented by an event", and Milestone 2's implement list includes
"Append-only event storage". Read as a pair, they look like a contradiction:
the criterion needs an event store one milestone before the store is built.

They are not in conflict once the two words are separated. Milestone 1 needs
an event *repository* — something that assigns a sequence and records the
thirteen-event trace the plan prints at Section 26. Milestone 2 builds
append-only event *storage* — the durable table, the schema versioning, the
upcasting read path. The `EventRepository` port is declared at Milestone 1
because the acceptance criterion requires it; the in-memory implementation
satisfies it at Milestone 1; the PostgreSQL implementation replaces it at
Milestone 2 and adds durability the port never claimed to provide.

The per-session monotonic `sequence` invariant applies to both. It is the one
guarantee the trace depends on, and it is cheap in memory.

## The run dispatcher

`ports/dispatch.py` and `adapters/dispatch/inline.py` and
`adapters/dispatch/postgres.py` are all named in the tree. Section 7 lists
"Run dispatcher" among the ports that get a bullet and no Protocol body. This
is the body.

```python
class RunDispatcher(Protocol):
    async def dispatch(self, run_id: UUID) -> None:
        """Guarantee that run_id will be executed exactly once.

        Called after the unit of work that created the run has
        committed. Returning does not mean the run has finished.
        """
```

One method, and the whole design is in the docstring's second paragraph.

### Both adapters satisfy the same postcondition

The inline adapter runs the loop to completion and then returns. The
PostgreSQL adapter inserts the queue row, notifies, and returns immediately.
These look like different contracts and are not: both guarantee that after
`dispatch` returns, the run will be executed exactly once. They differ in when
that has already happened, which is latency, not semantics.

That framing decides the one thing callers get wrong. **A caller must never
assume the run has finished when `dispatch` returns.** `agent run` at
Milestone 1 will in fact find a completed run, because the inline adapter
finished it; code written against that observation breaks the day the
PostgreSQL adapter is swapped in. So `RunService` returns a run identifier and
the CLI polls the run's status, at Milestone 1, when polling is redundant.

### Dispatch happens after commit

If `dispatch` is called inside the transaction that created the run, a worker
can claim a row that has not committed yet, and the failure is a
`RunNotFound` in a process that has no idea why. The rule is therefore
absolute: `dispatch` is called after the unit of work commits, never within
it.

Milestone 1 cannot observe a violation of this rule — the inline adapter runs
in the same task and would work either way — which is exactly why the rule
needs a test rather than a convention. **The inline adapter asserts that no
unit of work is open when it is called.** A Milestone 2 race becomes a
Milestone 1 assertion failure, in the milestone where the code is being
written rather than the one where it is being scaled.

## The Milestone 1 context builder

[context-engine.md](context-engine.md) specifies the finished builder and
places its hard gates on Milestone 7. Its build sequence has seven steps and
no milestone tags. **Step 1, "deterministic assembly", is the Milestone 1
builder** — the two regions, the fixed order, trust labels on every item, and
`prefix_sha256` recorded from the first commit. The spec is explicit about why
that step cannot wait: the hash "is not retrofittable onto traffic that has
already been served unstably."

### Region assignment for the seven Section 11.1 inputs

| Section 11.1 input | Region | Why |
| --- | --- | --- |
| Platform policy | A | Fixed for the deployment |
| Agent instructions | A | Fixed for the session |
| Available tool definitions | A | Fixed once filtered |
| Relevant conversation items | B | Grows every turn |
| Current goal and constraints | B | Editable within a session |
| Current user message | B | Volatile by definition |
| Runtime metadata (date, scope) | B | The canonical volatile item |

Region A is exactly the three classes Section 10.1 names as the byte-stable
prefix. The memory snapshot is a fourth Region A class in the finished design
and is empty at Milestone 1, because there is no memory yet — its absence
changes no bytes, which is the property that lets it be added later without
breaking a cached prefix.

### The invariant, and its test

Region A is built once per session, serialized to bytes with a fixed
separator, and hashed. `prefix_sha256` is recorded on every `model_calls` row
the session produces. The test is two builds:

1.  Build a request for a session. Record `prefix_sha256`.
2.  Advance the injected `Clock`, add a conversation item, build again.
3.  Assert the two `prefix_sha256` values are identical.
4.  Assert the two serialized requests are not, and that every differing
    byte falls in Region B.

Step 4 is the half that matters. Without it the test passes for a builder that
puts everything in the prefix and never changes anything, which is
prompt-stable and useless. This is the Milestone 1 form of the cache-boundary
enforcement test the context spec places at Milestone 7, and it needs the
`Clock` port from the determinism harness, which is why both land in the same
milestone.

### Tool filtering

Section 11.1 names four filters — agent configuration, principal
authorization, policy profile, runtime environment — and all four exist as
stages from the first commit. Two of them do nothing at Milestone 1: there are
no policy profiles until Milestone 4 and one runtime environment until
Milestone 8. They are written as identity stages rather than omitted, because
the plan's own rule for this class of decision is that builder shapes later
code hardens around are cheap now and expensive to retrofit.

The output is capped at 30 tools before it reaches the prefix. Milestone 1
registers two.

## The CLI contract

Section 17 lists twelve commands. Nine of them have no behaviour stated
anywhere in the corpus, `agent chat` has seven numbered steps, and `agent run`
has a required demonstration. The section closes with the one rule that
matters: "The CLI must call the same application services as the API. Do not
implement a second runtime loop inside the CLI."

This is the twelve commands with arguments, output, and exit codes. Nothing
here changes a command's spelling.

### The twelve

| Command | Arguments | Stdout | First milestone |
| --- | --- | --- | --- |
| `agent session create` | — | the session id, one line | 1 |
| `agent chat` | — | the transcript | 3 |
| `agent run` | prompt | the final message | 1 |
| `agent run get` | run id | the run record | 1 |
| `agent run events` | run id | the event trace | 1 |
| `agent run cancel` | run id | the resulting status | 5 |
| `agent approval list` | — | pending approvals | 4 |
| `agent approval approve` | approval id | the resolution | 4 |
| `agent approval deny` | approval id | the resolution | 4 |
| `agent eval run` | suite, optional | the suite result | 1 |
| `agent worker` | — | nothing; runs until signalled | 2 |
| `agent api` | — | nothing; runs until signalled | 1 |

`agent session create` prints the session identifier and nothing else, so
`SESSION=$(agent session create)` is the intended usage rather than a lucky
accident. Every other read command follows the same discipline: **the result
goes to stdout, and progress goes to stderr.** Milestone 1's required
demonstration prints six lines of flow — `run created`, `model requests
math.calculate`, and so on — and those six lines are stderr. The final
assistant message is stdout. `agent run "..." > answer.txt` then does the
obvious thing.

### Reserved words after `agent run`

`agent run "Calculate 12 times 9"` and `agent run get <run-id>` are the same
command with different first arguments, which is a genuine ambiguity in the
spelling the plan chose. It is resolved by reserving words: `get`,
`events`, and `cancel` are subcommands of `run`, and any other first argument
is a prompt.

The residual collision — a prompt that is exactly the word `get` — is
accepted rather than designed away, because the alternative is renaming a
command the plan fixed. `agent run -- get` passes the literal string.

The set is open to a subject spec that needs one, on the same terms
[evaluation-harness.md](evaluation-harness.md) already uses when it adds
four subcommands under `agent eval` without changing the twelve: a
subcommand under an existing command is not a new command.
[event-log-and-persistence.md](event-log-and-persistence.md) adds `export`
on that basis, making the reserved set four words. A spec adding one pays
exactly two costs — a line here and one more prompt that needs `--` — and
both are cheaper than a thirteenth top-level noun, because the twelve is a
number Section 17 states and this document's own heading repeats.

### Options

The plan names no flags, and this document adds four, each because a command
is otherwise unusable rather than merely less convenient:

```text
--json           machine-readable result on stdout, on read commands
--session <id>   reuse a session instead of creating one
--role <role>    worker | maintenance, on agent worker
--follow         stream events as they arrive, on agent run events
```

`--json` is what makes the CLI scriptable and is what `agent eval run`
consumes when a harness invokes it. `--role` is how the maintenance role gets
an entry point at all — the plan names `agent worker` and the runtime spec
names three roles, and one of the three would otherwise be unreachable.

### Exit codes

```text
0  the command did what it says
1  the run reached a terminal failure, or an approval was denied
2  usage error - bad arguments, unknown subcommand
3  the run suspended on something the CLI could not supply
4  configuration refused at startup - phase 1 failed
5  the platform is unreachable - database or API down
```

Codes 4 and 5 are separated because the distinction is the only one a calling
script actually needs: 4 will fail identically on retry, and 5 may not. Code 3
covers the case where `agent run` produces a run that suspends waiting for an
approval and the invocation was not interactive; the run identifier is printed
so the caller can resolve it with `agent approval approve` and the run
continues where it stopped.

### `agent chat` and the seven steps

The seven numbered steps in Section 17 are a specification of a loop, and the
loop calls application services for every one of them:

1.  Create or resume a session — `SessionService`.
2.  Submit a user message — `RunService`, which returns a run id.
3.  Stream events — the event broadcaster, filtered to that run.
4.  Display tool activity concisely — one line per invocation, from
    `tool.call.started` and `tool.call.completed`, never from model
    output.
5.  Prompt for approvals — on `approval.requested`, read from the terminal
    and call `ApprovalService`.
6.  Render the final assistant message — from the terminal event, not from
    accumulated stream deltas.
7.  Display artifact paths or ids — `ArtifactService`.

Step 4 and step 6 both carry a rule worth stating because both are easy to get
wrong in the direction that produces a second runtime loop. Tool activity is
rendered from events, so the CLI never inspects a model response to decide
what a tool did. The final message is read from the run's terminal event
rather than assembled from deltas, so a dropped or reordered stream chunk
cannot make the CLI's rendering disagree with what the platform recorded.
Those two rules are the whole of "do not implement a second runtime loop
inside the CLI", made concrete.

`agent chat` arrives at Milestone 3, when there is a real provider to stream
from. Milestone 1 has `agent run`.

## The Milestone 0 checks

Milestone 0's implement list has eleven bullets and its cross-reference
paragraph adds six deliverables that are not in the list: the gate registry,
the docs check, the import-boundary walk, the transaction-hygiene check, the
secret scanner, and contract-module coverage. The paragraph also states why
they land in a milestone with no runtime: "Both run against an almost-empty
repository and stay correct as it fills; added later, they are added against
existing violations, which is the situation in which they get relaxed rather
than obeyed."

Five of the six are specified elsewhere. The secret scanner is named nine
times across the corpus and specified nowhere, and the transaction-hygiene
check is placed in two milestones at once. Both are settled here.

### The static checks this document adds

The import-boundary walk already exists as a mechanism. These are the edges
and module-scope assertions the composition root contributes to it:

1.  `agent_core.bootstrap` is imported by `api/main.py`, `cli/main.py`, and
    `runtime/worker.py`, and by nothing else. A service that can import
    `bootstrap` can construct its own dependencies.
2.  No module outside `bootstrap.py` instantiates a class imported from
    `agent_core.adapters`. This is ADR-0001's "assertion that construction
    happens in one module" as an AST rule rather than a hope.
3.  No module outside `adapters/determinism.py` calls `datetime.now`,
    `time.time`, `uuid.uuid4`, or anything in `random`. This is the
    checkable negative that justifies `Clock` and `IdFactory` sharing a
    port module.
4.  No `AsyncSession` is constructed at module scope, anywhere. Dependency
    rule 13.

All four are true of an empty repository and stay true as it fills, which is
the property the milestone paragraph asks for.

### The secret scanner

Gate id `gate.structure.no_committed_secrets`, registered at Milestone 0.
The engineering plan's Milestone 0 acceptance criteria declare it, in the
same breath as the import-boundary walk; this section specifies it. The
area is `structure` rather than `security` because the identifier grammar
in [milestone-map.md](milestone-map.md) defines no `security` area, and a
repository-wide scan owned by no subject spec is what `structure` is for.

It scans committed text —
everything under `src/`, `tests/`, `evals/`, `migrations/`, `docs/`, plus
`.env.example` and any committed fixture — and fails the build on a match.

Five rule families, named so a failure report can say which one fired:

```text
provider_key     sk-*, sk-ant-*, and the profile-declared prefixes
private_key      PEM BEGIN blocks of any type
bearer_literal   a literal Authorization: Bearer with a value
dsn_password     a connection string with an inline password
assigned_secret  a name matching secret|token|password|api_?key
                 assigned a literal longer than twelve characters
```

Three properties matter more than the patterns:

1.  **The report never prints the match.** It prints the path, the line
    number, and the rule name. A scanner that echoes the secret into a CI
    log has moved the secret somewhere worse.
2.  **The allowlist requires a reason.** An entry is a `path:line:rule`
    triple plus prose; an entry without prose is itself a failure. The cost
    of suppressing a finding should be having to write down why.
3.  **`.env.example` is scanned, not exempted.** It is the file most likely
    to acquire a real value by accident, and the definition of done already
    says "No secrets appear in fixtures, logs, or committed files."

The scanner runs in `make check` and therefore in CI, and its findings are
build failures rather than warnings.

### Transaction hygiene at Milestone 0

The plan places the transaction-hygiene check in Milestone 0.
[runtime-loop.md](runtime-loop.md) tags the corresponding hard gate Milestone
2, and the acceptance criterion it enforces — "Database transactions are never
held across provider or tool I/O" — is a Milestone 2 criterion. Milestone 0
has no database code to walk.

Both are right, because they are talking about two different things. **The
check is a Milestone 0 deliverable; the gate is a Milestone 2 criterion.** At
Milestone 0 the AST walk is written, registered, and run, and it finds zero
violations because there is nothing to violate. At Milestone 2 the same walk
becomes an acceptance criterion for the milestone that first makes violating
it possible.

This is the whole point of the milestone paragraph's argument. A check written
against an empty repository is written against the rule; the same check
written at Milestone 2 is written against whatever the code already does.

### Contract-module coverage

"A port with no contract module is a build failure." At Milestone 0 there are
few ports and no adapters, and the check still runs: it enumerates the
Protocols under `agent_core.ports` and asserts a corresponding module under
`tests/contract/`. It is one of the two reasons `tests/contract/` must exist
at Milestone 0 rather than at the first adapter.

## Conflicts this document resolves

Five, all resolved additively — no requirement is rewritten, and in each case
the plan's text stands with an annotation rather than a replacement.

1.  **`runtime/engine.py` versus `loop.py` and `executor.py`.** The Section 4
    tree names one module; [runtime-loop.md](runtime-loop.md) splits it in
    two and restricts `RunRepository.transition` to one of them. The split
    wins, `engine.py` is retired, and `supervisor.py` joins them.
2.  **`.env.example` versus 106 file-layer knobs.** The definition of done
    says new configuration appears in `.env.example`. Read as "no
    configuration is undocumented", both layers satisfy it. Read as "every
    knob is an environment variable", it contradicts the `policy_version`
    hash. The first reading holds.
3.  **The Milestone 1 event criterion versus Milestone 2 event storage.** An
    event *repository* is Milestone 1; append-only event *storage* is
    Milestone 2. The port is declared once and implemented twice.
4.  **Transaction hygiene in Milestone 0 versus Milestone 2.** The check is a
    Milestone 0 deliverable; the gate is a Milestone 2 acceptance criterion.
5.  **`agent run <prompt>` versus `agent run get <id>`.** Three reserved
    subcommand words, and `--` for the literal.

## Decisions

1. **The composition root is the only module that knows both a port and its
   adapter.** Every other module knows one or the other. That is dependency
   rule 14 stated as a property, and it is what makes in-memory-for-Postgres
   and fake-for-OpenAI configuration changes rather than code changes.
2. **A value is an environment variable if and only if it differs between
   two deployments of the same revision and cannot be committed.** That
   sorts all 106 declared knobs into files and leaves eight fields in
   `Settings`.
3. **The environment never overrides a file; it is interpolated into one at
   named points.** A blanket override would let a deployment change an
   effective policy rule without changing the `policy_version` hash the rule
   is recorded under, which makes the audit trail lie.
4. **Configuration YAML lives beside the package that owns it**, following
   the one precedent the plan set with `policy/hardline.yaml`, and an
   operator overlay directory merges over it file-by-file so the merged
   document is still hashable.
5. **`hardline.yaml` cannot be overlaid.** Attempting it is a startup error.
   An overlay is configuration, and the hardline set is defined as the thing
   configuration cannot disable.
6. **Startup has five phases, ordered by what each may touch**: refuse,
   determinism, resources, freeze, wire. Every startup constraint in the
   corpus lands in one of them, and three constraints that are commonly
   mistaken for startup constraints are named as not being any of them.
7. **The composition root never runs migrations.** It asserts the schema
   revision and refuses to start on a mismatch. Migrating on boot means N
   processes racing during a rolling deploy and a failed start that has
   already changed the schema.
8. **`build` returns application services only.** No adapter, repository, or
   session factory is reachable from a `Composition`, which is how the
   runtime spec's single-caller reservation on `RunRepository.transition`
   survives a second entry point.
9. **The in-memory tier is five adapters, not test doubles**, run against
   the same contract suites, with each adapter declaring the capability
   groups it satisfies against a checked-in table so a skip is a reviewed
   fact rather than a runtime accident.
10. **There is no in-memory `RunQueue`.** Its whole content is `FOR UPDATE
    SKIP LOCKED` and lease fencing, which is the one thing a simulation
    cannot tell the truth about.
11. **`RunDispatcher` has one method and both adapters satisfy the same
    postcondition**: after `dispatch` returns, the run will be executed
    exactly once. Inline and PostgreSQL differ in latency, not semantics, so
    a caller may never treat a returned `dispatch` as a finished run.
12. **The inline dispatcher asserts that no unit of work is open when it is
    called.** Dispatching inside the creating transaction is a Milestone 2
    race that Milestone 1 cannot otherwise observe; the assertion moves the
    failure into the milestone where the code is written.
13. **The Milestone 1 builder is build-sequence step 1**, and its
    prompt-stability test asserts both halves — the prefix hash is stable
    *and* the request bytes differ, in Region B only. The first half alone
    passes for a builder that never changes anything.
14. **All four tool filters exist from the first commit**, two of them as
    identity stages until their milestones arrive, because the plan's own
    rule for builder shapes is that they are cheap now and expensive to
    retrofit.
15. **CLI results go to stdout and progress goes to stderr**, so the
    Milestone 1 demonstration's six flow lines and its final answer are
    separable without a flag.
16. **`get`, `events`, `cancel`, and `export` are reserved words after
    `agent run`.** The residual collision with a prompt that is exactly one
    of them is accepted, with `--` as the escape, rather than renaming a
    command the plan fixed. The set is open to a subject spec that needs a
    subcommand, which is not the same thing as a new command.
17. **The secret scanner never prints what it matched**, requires prose on
    every allowlist entry, and scans `.env.example` rather than exempting
    it.
18. **The transaction-hygiene check is a Milestone 0 deliverable and a
    Milestone 2 gate.** Writing the check against an empty repository writes
    it against the rule; writing it at Milestone 2 writes it against
    whatever the code already does.

## Open questions for review

1.  **`DATABASE_URL` is invented here.** The plan names PostgreSQL as the
    source of truth, gives its schema, and requires migrations, but names no
    variable for reaching it. The name chosen is the SQLAlchemy convention.
    If the deployment target supplies a database address under a different
    name, this is a one-line change in `config.py`.
2.  **Credentials are a flat mapping keyed by provider profile.** Section
    10.7 requires credential *pools* with round-robin selection, failover,
    and cooldowns at Milestone 3. A pool needs more than one value per
    profile, so `Mapping[str, SecretStr]` becomes `Mapping[str,
    Sequence[SecretStr]]` then. Whether that arrives as a list in one
    variable or as indexed variables is a Milestone 3 decision, not one this
    document should make.
3.  **The overlay merges by top-level key, not deeply.** Replacing an entire
    `model_policies` block to change one model is blunt, and deep merging
    makes the effective document harder to predict from the files. The blunt
    version is chosen for reviewability; if operators find it painful in
    practice, per-key merging is a compatible change.
4.  **Four CLI options are added where the plan names none.** `--json`,
    `--session`, `--role`, and `--follow`. Each is defended above, but the
    plan's silence on flags may have been deliberate minimalism rather than
    an omission.
5.  **The capability-group table for contract suites is described, not
    enumerated.** Which groups exist — durability, isolation, concurrency,
    recovery — should be fixed when the first contract suite is written,
    against a real port rather than in the abstract.
