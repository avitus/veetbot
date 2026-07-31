# Modular General-Purpose AI Agent

This repository is implementing the provider-neutral agent platform defined by
the canonical [engineering plan](docs/plan/engineering-plan.md). Work is
strictly milestone-gated. Milestone 0 establishes the repository and engineering
foundation; it does not yet contain the agent runtime.

## Current status

Milestone 0 is in progress. The Python package, configuration boundary,
structured logging, PostgreSQL development definition, empty Alembic graph,
CI workflow, test categories, gate registry, and structural checks exist.
Agent execution begins in Milestone 1.

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
contract partitions, citation validation, the 172-entry gate-registry
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
| `make docs` | Build the MkDocs site and standalone HTML publication |
| `make docs-check` | Validate citations, registry structure, and strict docs output |

Static and contract tests deny network egress. Integration tests may use Unix
sockets and loopback only. Live tests are the sole category that lifts the
socket block.

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

## Planned operating commands

The engineering plan reserves the following workflows. They are documented
here so availability is not confused with implementation:

| Workflow | Availability |
| --- | --- |
| Use the fake provider and run `agent run` | Milestone 1 |
| Start the durable worker | Milestone 2 |
| Configure OpenAI, Anthropic, or an OpenAI-compatible endpoint | Milestone 3 |
| Run optional live-provider tests | Milestone 3 |
| Inspect model/tool traces and usage | Milestone 3 |
| Resolve an approval through the CLI | Milestone 4 |
| Start the HTTP API | Milestone 5 |
| Run deterministic evaluation cases | Milestone 1; later cases activate with their owning milestone |

Do not invoke `agent api`, `agent worker`, `agent chat`, or `agent run` yet.
The only installed CLI operation in Milestone 0 is `agent --version`; adding
placeholder runtime commands would falsely imply that their acceptance criteria
hold.

## Documentation and governance

Start with [AGENTS.md](AGENTS.md). Markdown and YAML are canonical. Files under
`site/` and `dist/` are generated and must not be edited. Architectural changes
require ADRs, milestone status changes require evidence, and work beyond the
authorized milestone must not begin speculatively.

Security boundaries and the controls established so far are documented in
[docs/security.md](docs/security.md).
