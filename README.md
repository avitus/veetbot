<p align="center">
  <img src="assets/brand/veetbot-icon.svg" width="112" alt="Veetbot">
</p>

<h1 align="center">Veetbot</h1>

<p align="center">
  <strong>An AI agent that can do useful work, remember what matters, and show its work.</strong>
</p>

## Meet Veetbot

Veetbot is a self-hostable AI agent for work that takes more than a single
prompt. Give it a goal and it can use tools, keep track of context, remember
useful information, pause when it needs your approval, and continue after a
process restart.

It is designed for people who want a capable assistant without giving up
control. Every run leaves a durable record, important actions pass through
explicit policy, and you can inspect what happened instead of trusting a black
box.

### What it can do

- **Carry work through to completion.** Runs, tool calls, checkpoints, and
  results survive crashes and can be resumed safely.
- **Use the model you prefer.** OpenAI, Anthropic, and OpenAI-compatible local
  models share one provider-neutral interface.
- **Work with tools and the web.** Built-in tools, MCP servers, skills, public
  web search, page extraction, and browser automation use the same governed
  execution path.
- **Remember with context.** Long-term memory records where information came
  from, supports corrections and deletion, and makes retrieval decisions
  inspectable.
- **Keep you in control.** Deterministic policy rules, scoped credentials,
  isolated execution, and approval prompts guard consequential actions.
- **Handle work while you are away.** Durable schedules, offline results, and
  notifications let useful work continue beyond an open chat window.
- **Meet you where you work.** Use the command line, the versioned HTTP API and
  event stream, the downloadable terminal client, or the native Apple client.
- **Make behavior testable.** Reproducible evaluations and named release gates
  turn agent quality, safety, and recovery into things a team can verify.

Veetbot is under active development. The durable agent core and the workflows
through Milestone 12 are complete, along with the memory evaluation and memory
browser workstreams; newer capabilities continue to move through explicit
release gates. See the
[current project state](docs/status/project-state.yaml) for the exact status of
each milestone.

Public product and policy pages are available at
[www.veetbot.com](https://www.veetbot.com/), with technical documentation at
[docs.veetbot.com](https://docs.veetbot.com/). The website source and its local
verification commands live under [`website/`](website/README.md).

## Developer guide

The quickest way to understand Veetbot is to run its deterministic local demo.
It exercises PostgreSQL persistence, the worker queue, a real built-in tool, and
the event log without requiring a model API key.

### Prerequisites

- Python 3.12 or newer
- Node.js 22.13 or newer
- [uv](https://docs.astral.sh/uv/)
- Docker with the Compose plugin
- `make`
- [Pandoc](https://pandoc.org/), required by `make docs` and by the
  documentation gate that `make check` runs

The commands below assume a macOS or Linux shell and start from the repository
root.

### 1. Install the project

Create your local environment file, then install the locked development
dependencies:

```bash
cp .env.example .env
make install
```

The default `.env` uses development authentication and a scripted fake model,
so the first run is local, deterministic, and free.

### 2. Start PostgreSQL

Start the pinned PostgreSQL 16 container and apply the migrations:

```bash
make db-up
make migrate
```

The development database listens on `127.0.0.1:5432`. Veetbot never applies
migrations during application startup, so run `make migrate` whenever you pull
new migrations.

### 3. Start a worker

Keep this command running in its own terminal:

```bash
uv run agent worker --role worker
```

The worker leases queued runs, executes model and tool steps, and writes their
events back to PostgreSQL.

### 4. Submit your first run

In a second terminal, run the built-in calculator demo:

```bash
uv run agent run "What is 17 multiplied by 23?"
```

You should see progress on stderr and `391` on stdout. That result travels
through the same durable run and tool lifecycle used by real model providers.

### 5. Connect a real model (optional)

Add the credential for the provider you want to use to `.env`:

```dotenv
VEETBOT_OPENAI_KEY=...
ANTHROPIC_API_KEY=...
```

Restart the worker after changing `.env`, then create a run with a declared
model policy:

```bash
uv run agent run --model-policy balanced "Summarize this repository"
uv run agent run --model-policy flagship "Review the architecture"
```

`balanced` uses OpenAI and `flagship` uses Anthropic. For a credential-free
local model, start Ollama with the configured `qwen3:8b` model and use:

```bash
uv run agent run --model-policy local "Explain the event-driven run loop"
```

Model policies, provider profiles, pricing snapshots, and capability limits
live in reviewed YAML under `src/agent_core/models/`.

### Run the API and terminal client

With the database and worker already running, start the local API in another
terminal:

```bash
uv run agent api
```

It listens on `http://127.0.0.1:8000`. Build and open the dependency-free
terminal client with:

```bash
make client-build
python build/veetbot-client.pyz
```

The [client guide](docs/client.md) covers remote URLs, authentication, session
resume, approvals, and reconnect behavior. The native iOS and macOS client has
its own [setup guide](clients/apple/README.md).

### Everyday development commands

| Command | What it does |
| --- | --- |
| `make check` | Run formatting checks, linting, strict types, fast tests, deployment-script tests, and documentation checks |
| `make format` | Apply Ruff formatting and safe lint fixes |
| `make test` | Run every non-live Python test |
| `make test-static` | Run unit and structural tests without I/O |
| `make test-contract` | Run shared contracts against in-memory and fake adapters |
| `make test-integration` | Run tests that need PostgreSQL or another local service |
| `make docs` | Build the MkDocs site and standalone HTML documentation |
| `make docs-serve` | Serve the documentation locally with live reload |
| `make test-website` | Install, build, test, and lint the public static website |

`make check` does not require a database or provider credential. Static and
contract tests block network access; only explicitly enabled live tests may
contact model providers and incur cost. The provider-assisted memory evaluator
currently makes 25 bounded provider calls (at most USD 1.25 under its per-call
ceiling).

### Configuration and project conventions

Secrets, addresses, and deployment identity belong in `.env`; reviewed tuning
values belong in the YAML file owned by the relevant package. Optional web,
browser, scheduling, notification, email, and trajectory-export features are
disabled by default. Their available environment switches are documented in
[`.env.example`](.env.example).

The initial public-web comparison keeps the incumbent provider for half of each
capability and routes the other half to Keenable:

```bash
WEB_SEARCH_PROVIDERS=tavily:50,keenable:50
WEB_FETCH_PROVIDERS=firecrawl:50,keenable:50
TAVILY_API_KEY=...
FIRECRAWL_API_KEY=...
KEENABLE_API_KEY=...
```

Weighted entries must be unique positive integer percentages summing to 100.
The backward-compatible singular selectors may instead name `firecrawl`,
`tavily`, `keenable`, or `disabled`. Provider keys are resolved by the
credential broker at call time and are never exposed to the model.

Before changing the codebase, read [AGENTS.md](AGENTS.md). It explains the
authorized milestone, required reading lane, test-driven workflow, and
completion-report requirements. The main technical references are:

- [Engineering plan](docs/plan/engineering-plan.md) — normative requirements
- [Current milestone](docs/plan/current-milestone.md) — authorized work
- [Architecture decisions](docs/adr/) — decisions and tradeoffs
- [Security model](docs/security.md) — trust boundaries and controls
- [Deployment runbook](docs/deployment.md) — production installation,
  validation, rollback, and recovery

Markdown and YAML are canonical. Do not edit generated files under `site/` or
`dist/`.
