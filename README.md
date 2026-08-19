# Modular General-Purpose AI Agent

This repository implements the provider-neutral agent platform defined by the
canonical [engineering plan](docs/plan/engineering-plan.md). Work is strictly
milestone-gated: Milestones 0 through 9 are complete, and Milestone 10 is in
progress through three separately authorized workstreams.

## Features

### Available now

- **Durable, resumable runs** — an append-only PostgreSQL event log, leased
  queue workers, checkpoints, idempotent submission, and crash recovery across
  processes.
- **Real model providers** — OpenAI Responses, Anthropic Messages, and
  OpenAI-compatible local endpoints behind declared model policies, with
  normalized streaming and durably pinned models, capabilities, and pricing.
- **Policy and approvals** — a deterministic policy gate with non-overridable
  hardline rules; approvals resolve through the CLI, the API, and the clients.
- **A complete tool lifecycle** — builtin tools with validation,
  classification, recovery boundaries, and durable invocation records.
- **HTTP API with SSE** — versioned `/v1` routes, scoped bearer authentication,
  idempotent submission, and reconnectable, replayable event streams.
- **Isolated execution and artifacts** — sandboxed tool execution behind
  Docker/gVisor with default-deny egress, resource limits, and a checksummed
  artifact store.
- **Context engineering** — budgeted assembly with a stable prefix,
  deterministic compaction, and typed, durable working state.
- **Skills and MCP** — versioned, immutable skill packages pinned at session
  open, plus MCP servers over the official SDK.
- **Long-term memory and knowledge** — governed memory formation with
  provenance and corrections, hybrid recall with an auditable trace, and
  knowledge-document ingestion with cited passage retrieval.
- **Provider-neutral web access** — independently selectable Tavily and
  Firecrawl adapters behind stable `web.search` and `web.fetch` tools, with a
  recommended Tavily-search/Firecrawl-fetch deployment.
- **Governed trajectory export** — consent-gated, redacted, and verified
  before any artifact is committed.
- **Evaluation harness** — deterministic evaluation cases, a 177-entry gate
  registry, and a credential- and cost-gated live capability lane.
- **Clients** — a dependency-free downloadable terminal client and a native
  SwiftUI client for iOS and macOS.
- **Production deployment** — CircleCI-driven atomic releases to
  `api.veetbot.com` with systemd, Nginx, gVisor, and production validation.

### In progress (Milestone 10)

Three separately authorized workstreams are underway. Optional capabilities
stay behind default-off controls:

- **Automatic memory formation** — post-run extraction and consolidation of
  durable memories from ordinary conversations, with audited caps, conflict
  handling, and provenance.
- **Self-authored skills** — the agent creating and revising its own skills
  through `skill.manage` behind diff-carrying approvals, with an optional
  confined background-review child run.
- **Public-web access** — read-only search and page extraction through
  independently configured providers; every result remains external-untrusted.

### Planned

The remaining Milestone 10 extensions are designed but deliberately deferred
and not yet authorized:

- **Scheduling** — scheduled runs that fire in the cloud regardless of which
  devices are online.
- **Model routing** — richer routing behavior across the declared model
  policies.
- **General-purpose subagents** — `delegate.run` child runs with their own
  leases, budgets, and traces.
- **Multi-device surfaces** — inbound messaging surfaces and device pairing
  over the shared core.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Docker with the Compose plugin for the development PostgreSQL service
- [Pandoc](https://pandoc.org/) for the single-file documentation build

## Install

```bash
cp .env.example .env
make install
```

`make install` runs `uv sync --all-groups`. The committed `uv.lock` is the
only dependency resolution used by development and CI.

## PostgreSQL and migrations

Start the pinned PostgreSQL 16 service, wait for its healthcheck, and apply the
linear Alembic graph as separate operations:

```bash
make db-up
make migrate
```

The database, user, and development-only password are all `agent`; PostgreSQL
is exposed on `localhost:5432`. The composition root will never migrate on
startup. The first migration is intentionally empty because Milestone 0 adds no
application schema.

Port 5432 must be free before startup. Stop any separately installed PostgreSQL
service using that port; otherwise `make migrate` may connect to that server
instead of the Compose database.

To reset the development database, remove the compose service and its named
volume, then recreate it. This permanently deletes local development data:

```bash
docker compose down -v
make db-up
make migrate
```

## Checks

The pull-request gate is:

```bash
make check
```

It runs formatting validation, linting, strict type checking, the static and
contract partitions, isolated deployment-script tests, citation validation, the 177-entry gate-registry
reconciliation, and strict documentation builds. It requires neither a database
nor a provider credential.

Additional targets are explicit about their requirements:

| Command | Purpose |
| --- | --- |
| `make format` | Apply Ruff formatting and safe lint fixes |
| `make test` | Run every non-live test; integration tests need PostgreSQL once present |
| `make test-static` | Run unit tests and structural/property gates without I/O |
| `make test-contract` | Run shared port contracts against in-memory/fake adapters |
| `make test-integration` | Run PostgreSQL, resilience, security, and eval-case tests |
| `make test-live` | Explicitly enable credentialed provider tests |
| `make test-apple` | Run the Apple client's Swift Testing suite (requires full Xcode) |
| `make test-deploy` | Exercise release and Nginx installers against isolated command stubs |
| `make production-check` | Validate release identity, model credential, gVisor, sandbox image, storage, and migration head |
| `make docs` | Build the MkDocs site and standalone HTML publication |
| `make docs-check` | Validate citations, registry structure, and strict docs output |

Static and contract tests deny network egress. Integration tests may use Unix
sockets and loopback only. Live tests are the sole category that lifts the
socket block.

## Run the durable agent

After copying `.env.example` to `.env` and applying migrations, start a worker
in one terminal:

```bash
uv run agent worker --role worker
```

Then submit the calculator flow from another terminal:

```bash
uv run agent run "What is 17 multiplied by 23?"
```

Progress is written to stderr and the final answer (`391`) to stdout. Run the
checked-in deterministic cases with:

```bash
uv run agent eval run
```

To see which named problems are actually isolated, execute the canonical gate
registry. Active checks run; later-milestone checks remain visible as pending;
and an active skip is reported as a failure rather than a pass:

```bash
uv run agent eval gates
uv run agent eval gates --area policy
```

The live capability lane is credential- and cost-gated. It accepts only
scenarios linked to a checked-in redacted failed trajectory, repeats each
scenario, uses a version-pinned independent judge, enforces scenario/suite/day
ceilings, and persists the score distribution in PostgreSQL:

```bash
RUN_LIVE_MODEL_TESTS=1 uv run agent eval capability --suite research
```

No publishable scenario is checked in yet because the repository has no actual
failed redacted trajectory. The command refuses to invent one; admission and
fixture details are in [`evals/capability/README.md`](evals/capability/README.md).

Choose a declared model policy when creating a new session:

```bash
uv run agent run --model-policy balanced "Summarize this request"
uv run agent run --model-policy flagship "Solve this difficult problem"
uv run agent run --model-policy local "Answer without a hosted provider"
```

`balanced` uses the OpenAI Responses adapter, `flagship` uses Anthropic
Messages, and `local` uses the OpenAI-compatible endpoint declared by the
Ollama profile. The corresponding worker resolves and durably pins the provider,
model, capability set, profile hash, and pricing snapshot before its first
model call. Remote policies fail closed at use when their credential is absent;
fake and local workflows remain available without remote credentials.

`agent session create`, `agent run get`, and `agent run events` read the same
PostgreSQL state across processes. Run periodic lease reclamation separately
with `uv run agent worker --role maintenance` in deployments that do not use a
process supervisor to start that role.

Inspect and correct the governed memory store through the same PostgreSQL
composition:

```bash
uv run agent memory list --session <session-id> --include-inactive
uv run agent memory get <belief-id>
uv run agent memory formations --session <session-id>
uv run agent memory trace <trace-id>
uv run agent memory edit <belief-id> --statement "Corrected statement"
uv run agent memory delete <belief-id>
```

The commands emit JSON to stdout. Belief records carry their source session,
source event ids, formation-run id, policy version, lifecycle, authority, and
sensitivity. Formation records show extraction watermarks and candidate outcome
counts; trace records show the retrieval query, ranking scores and arms, safety
blocks, and budget drops. Trace ids appear in `memory.recalled` events.

Hosted checks use [CircleCI](https://circleci.com/) via
`.circleci/config.yml`. Connect the repository as a CircleCI project for the
static, contract, integration, and sandbox workflow. A successful `main`
pipeline packages the tested commit and deploys it to `api.veetbot.com`; the
versioned proxy site is reconciled after the application release. Create a
restricted context named
`live-model` for provider credentials; nightly runs and manually triggered
pipelines with `run_live: true` are the only workflows that use it.

## Clients

Two clients speak the public `/v1` API. The shared core remains authoritative
for sessions, runs, approvals, events, and artifacts in both.

### Downloadable terminal client

Build the dependency-free terminal client with:

```bash
make client-build
```

The resulting `build/veetbot-client.pyz` needs Python 3.12 or newer but does
not need the server package or its dependencies. It defaults to the loopback
API; the API and worker must be started separately as described in
[Run the durable agent](#run-the-durable-agent) and the [client guide](docs/client.md).
For a deployment, export the URL and let the client request the bearer token
through its interactive no-echo prompt:

```bash
export VEETBOT_API_URL=https://agent.example.com
python build/veetbot-client.pyz
```

The client creates or resumes sessions, submits idempotently, reconnects and
replays SSE, handles approvals and user questions, and reconciles transient
output against the durable final message. See the
[client guide](docs/client.md) for its commands and security boundary.

### Native Apple client

`clients/apple` holds a SwiftUI client for iOS 15+ and macOS 12+ with no
third-party dependencies. It restores complete durable transcripts, replays the
active run over SSE, keeps a reconciled local session history, and stores its
bearer token only in Keychain. See its
[README](clients/apple/README.md) for signing, settings, and test
instructions.

## Configuration

Environment values are limited to deployment identity, addresses, and secrets.
Tuning values live in reviewed YAML beside the package that owns them. An
optional `AGENT_CONFIG_DIR` overlays shipped YAML by top-level key; it cannot
replace `policy/hardline.yaml`. Environment values are interpolated only where
a YAML file explicitly names `${VAR}`.

Production validation refuses development authentication and the `docker` or
`fake` sandbox mechanisms. Provider credentials are represented as Pydantic
secret values and structured-log processors redact sensitive keys, provider-key
prefixes, prompts, messages, reasoning, tool results, and large content.

Set `VEETBOT_OPENAI_KEY` or `ANTHROPIC_API_KEY` only for the remote profiles you
intend to use. `OPENAI_API_KEY` remains a compatibility fallback, but the
Veetbot-specific name wins when both are present. Governed trajectory export is
disabled by default. To opt a local deployment in, set
`AGENT_TRAJECTORY_EXPORT_ENABLED=1` and choose an `AGENT_ARTIFACT_ROOT` outside
the source tree. A principal grant is still required and is prospective:

```bash
uv run agent session export-consent grant
uv run agent run "A run that may later be exported"
uv run agent run export <run-id> --json
uv run agent session export-consent withdraw
```

Withdrawal expires all prior exports for that principal. Run the maintenance
worker to remove expired metadata and bytes. Exported JSON excludes reasoning,
provider metadata, usage, prices, precise timestamps, and internal execution
identifiers; mandatory secret rules are applied and then verified before any
artifact is committed.

Provider-assisted memory formation uses safe automatic selection by default.
`AGENT_MEMORY_PROVIDER_EXTRACTION_MODE=auto` activates matching operator or
release-bundled evidence and otherwise records the reason and stays on
deterministic `formation@2`. `off` does not even resolve a formation model;
`required` refuses startup without an exact extractor, model, profile, and
compiled-policy-version match. An operator artifact can be supplied with
`AGENT_MEMORY_PROVIDER_EXTRACTION_EVIDENCE`.

Release engineers can generate that artifact instead of authoring JSON:

```bash
RUN_LIVE_MODEL_TESTS=1 uv run agent eval memory-formation \
  --model-policy balanced --policy-profile default \
  --build-ref <immutable-build-ref> \
  --output .agent/evals/provider-memory-evidence.json
```

The checked-in corpus currently makes 24 bounded provider calls (at most USD
1.20 under the extractor's per-call ceiling). The command compares isolated
provider and deterministic arms, derives the corpus hash and resolved model,
and writes a new file only after the no-fabrication, no-policy-regression, and
positive-lift gate passes. Provider extraction itself runs only during memory
maintenance, has no tools, and falls back deterministically on call failure.

Public-web access is also disabled by default. The recommended provider split
is enabled with:

```bash
WEB_SEARCH_PROVIDER=tavily
WEB_FETCH_PROVIDER=firecrawl
TAVILY_API_KEY=...
FIRECRAWL_API_KEY=...
```

Either selector may instead name `firecrawl`, `tavily`, or `disabled`. Provider
keys are resolved by the credential broker at call time and are never exposed
to the model.

## Workflow availability

Every workflow the engineering plan reserved for Milestones 0 through 9 is
implemented: durable submission and workers, the three provider profiles,
live-provider tests, normalized usage accounting, consent-gated trajectory
export, CLI approval resolution, the HTTP API, deterministic evaluation cases,
and named gate status by milestone or area. Two boundaries remain so
availability is not confused with implementation:

- The live capability harness is implemented but has no publishable scenario;
  admission requires a real checked-in redacted failed trajectory.
- Milestone 10's authoring surfaces default off, and the deferred extensions
  listed under [Planned](#planned) have no commands yet.

`agent run`, `agent run export`, `agent session create`, `agent session
export-consent`, `agent worker`, `agent api`, and `agent eval run` are available
now. The separately downloadable client and the native Apple client provide
interactive remote chat; `agent chat` itself remains unavailable.

## Documentation and governance

Start with [AGENTS.md](AGENTS.md). Markdown and YAML are canonical. Files under
`site/` and `dist/` are generated and must not be edited. Architectural changes
require ADRs, milestone status changes require evidence, and work beyond the
authorized milestone must not begin speculatively.

Canonical references are the [engineering plan](docs/plan/engineering-plan.md),
the [current-milestone pointer](docs/plan/current-milestone.md), the
[machine-readable project state](docs/status/project-state.yaml), and the
[architecture decision records](docs/adr/). The original Word document under
`archive/` is archival only.

Security boundaries and the controls established so far are documented in
[docs/security.md](docs/security.md).

Production operators should follow the evidence-based
[DigitalOcean deployment runbook](docs/deployment.md). It covers the checked-in
atomic release script, systemd, Nginx, production environment, gVisor,
CircleCI context, rollback procedure, and remaining host bootstrap work.
