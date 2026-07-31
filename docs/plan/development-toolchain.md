---
title: Development Toolchain
status: design
canonical: true
---

# Development toolchain

Milestone 0 asks for eleven things and describes two of them. "Makefile"
is one word; the eight commands it must provide are listed in a fence
three subsections later, and not one of them says what it runs. "Docker
Compose with PostgreSQL" names no version, port, volume, or database.
"CI pipeline" names no workflow file, and the only CI shape in the corpus
is four jobs in a specification the milestone does not cite. "Structured
logging bootstrap" appears once in the entire documentation set.

None of this is design work in the sense the nine mechanism
specifications are. It is the part of Milestone 0 that a competent
implementer would guess at, and the cost of guessing is not a wrong
guess — it is nine different guesses across nine sessions, and a
`make check` whose contents drift from what CI runs until the two stop
agreeing about whether the build is green.

This document fixes the toolchain so the guessing stops. It adds no
requirement to any milestone. Every command, target, and job below is
already required somewhere in the corpus; what is new is what each one
contains.

## What this document is responsible for

The governing rule is that **`make check` and CI must be the same
thing**. Section 21 states one Milestone 0 acceptance criterion about
continuous integration — "CI executes `make check`" — and
[evaluation-harness.md](evaluation-harness.md) specifies four jobs with
different needs, one of which requires a database and one of which is
skipped without credentials. Both are satisfiable at once only if
`make check` is defined as the union of the jobs that need neither, and
CI is defined as running that target plus the ones that do.

Three consequences follow.

1.  A developer who runs `make check` and sees it pass has run exactly
    the two CI jobs that gate a pull request without a database. They
    have not run the integration job; the Makefile says so by giving it
    a separate target rather than folding it in silently.
2.  Adding a gate means adding it to one place. A gate registered under
    `static` is picked up by `make check` and by CI job 1 without
    either being edited, because both select by marker rather than by
    listing files.
3.  CI never runs a command that does not exist in the Makefile. The
    workflow file is a schedule and an environment; it is not a second
    definition of what the project checks.

## The project file

`pyproject.toml` is the one place a version is pinned. Section 2.2 names
the stack; this fixes the parts of it that a lockfile alone does not
settle.

```text
requires-python      >=3.12
build backend        hatchling, src layout
console script       agent = agent_core.cli.main:app
dependency groups    dev, test, docs
lockfile             uv.lock, committed
```

The console script is what makes `agent run`, `agent worker`, and
`agent api` the commands Section 17 writes rather than
`python -m agent_core.cli`. Section 25's `uv run agent chat` depends on
it existing from the first commit.

Tool configuration lives in `pyproject.toml` rather than in per-tool
files, so that a reader who wants to know what lint means opens one
file:

```text
[tool.ruff]              line length 100, target py312
[tool.ruff.lint]         E, F, I, N, UP, B, A, C4, SIM, TID, RUF
[tool.mypy]              strict, no implicit Optional, warn unused
                         ignores, disallow untyped defs
[tool.pytest.ini_options] asyncio_mode = auto, markers declared,
                         --strict-markers, --strict-config
```

`--strict-markers` is what turns a typo in `@pytest.mark.liv` from a
silently-skipped test into an error. It matters more here than in most
projects, because Section 20.4's live tests are defined by a marker and
a missing marker means a test that quietly never runs.

Three dependency groups, because Section 25 has three audiences: `dev`
for someone editing the code, `test` for CI, and `docs` for the
documentation build. `uv sync` installs the default set; `uv sync
--group test` is what job 1 installs.

## Structured logging

Section 19 gives eight structured-log fields and one sentence of
context. It never says what emits them, in what format, or when the
bootstrap runs. Milestone 0 lists "Structured logging bootstrap" as a
deliverable with no elaboration anywhere in the corpus.

**`structlog` over the standard library's `logging`, configured once in
`agent_core/observability/logging.py`, called from phase 1 of the
composition root.** Phase 1 is where it belongs because a settings
validation failure is the first thing that can go wrong and the first
thing worth a structured log line. Nothing before that point may log.

The output format follows the deployment mode, which is one of the eight
fields in `Settings`:

```text
development   console renderer, colours, one line per event
production    JSON lines on stdout, one object per event
```

Two renderers rather than one because a JSON log in a terminal is
unreadable and a console log in a log aggregator is unparseable, and the
deployment mode is already the value that distinguishes the two
audiences.

### The context variables

Section 19's eight fields are not eight arguments passed to every log
call. Five of them are context variables bound once and inherited by
every event emitted inside that scope:

```text
bound at            variable
------------------  -------------------------------------------
request middleware  request_id
run start           run_id, session_id, tenant_id
tool invocation     tool_invocation_id
span entry          trace_id, span_id
```

`timestamp`, `level`, and `message` come from the processor chain.
`trace_id` and `span_id` are read from the active OpenTelemetry span
rather than passed, which is what makes Section 19's stated goal —
"tracing, metrics, and log correlation" — true without every call site
threading a trace id.

### Redaction is a processor, not a convention

Section 19 lists eight things that must not be recorded by default and
Section 22 requires secret redaction as a control. A processor in the
chain enforces both, because a convention that every developer must
remember is a control that fails on the first hurried commit.

The processor drops or replaces:

```text
any key matching  secret|token|password|api_?key|authorization
any value matching the provider key prefixes the profiles declare
any key named     prompt|messages|reasoning|tool_result|content
```

The third family is truncation rather than removal — the first 200
characters and a length — because a log line that says a tool returned
something is useful and a log line carrying 40 KB of tool output is the
thing Section 19 forbids. A call site that genuinely needs full content
writes it to an artifact and logs the artifact id.

The processor is covered by a security test: Section 20.4 already lists
"Secret redaction" as a required security test, and this is the code it
tests.

## The Makefile

Section 21 requires eight targets. Six of them are one command each;
the interesting ones are `check`, `test`, and `migrate`.

```text
target      runs
----------  ---------------------------------------------------
install     uv sync --all-groups
format      uv run ruff format . && uv run ruff check --fix .
lint        uv run ruff format --check . && uv run ruff check .
typecheck   uv run mypy src tests
test        uv run pytest -m "not live"
check       lint typecheck test-fast
db-up       docker compose up -d postgres && wait-for-healthy
migrate     uv run alembic upgrade head
```

Six targets exist that Section 21 does not list, because CI needs them
and rule 3 above says CI may not invent commands:

```text
target            runs
----------------  -------------------------------------------
test-static       pytest -m static
test-contract     pytest -m "not static and not integration
                  and not live"
test-fast         test-static then test-contract
test-integration  pytest -m integration
test-live         RUN_LIVE_MODEL_TESTS=1 pytest -m live
docs              mkdocs build --strict
```

`make check` is `lint typecheck test-fast`, `test-fast` is
`test-static` followed by `test-contract`, and those two are CI jobs 1
and 2 exactly — everything that needs neither a database nor a
credential, partitioned rather than overlapping. This is the whole of
the reconciliation the governing rule demands. A developer with no
Docker daemon running can still satisfy the criterion in Section 24
that says
"`make check` succeeds"; a developer with one runs `make db-up migrate
test-integration` and has run the third job as well.

`make test` and `make test-fast` differ, and the difference is
deliberate. `test` is what a developer runs when they want the whole
suite that can run locally, including integration if a database is up;
`test-fast` is what gates. Naming them apart is what stops the
integration suite from being quietly deleted from `check` the first
time a laptop has no Docker.

`db-up` waits. `docker compose up -d` returns as soon as the container
is created, which is several seconds before PostgreSQL accepts
connections, and a `make db-up migrate` that fails intermittently is
the first thing a new contributor hits. The target polls the compose
healthcheck rather than sleeping a fixed interval.

`migrate` does not start the database and `db-up` does not migrate.
Section 25's documented sequence runs them in order as two commands,
and ADR-0024 already forbids the composition root from migrating on
boot; a Makefile that chained them would make the two-process case
look like a one-command case.

### What `make install` assumes

`uv sync --all-groups` installs the `dev`, `test`, and `docs` groups.
It does not install Docker, and it does not download a browser, a
linter binary, or a schema tool at run time. Section 22 forbids silent
runtime downloads and the same discipline applies to the toolchain: if
a check needs a binary, the binary is a declared dependency or the
check fails with a message naming what to install.

## The compose file

One service at Milestone 0.

```text
service     postgres
image       postgres:16-alpine
port        5432 published on 5432
database    agent
user        agent
password    agent (development only, in .env.example)
volume      named volume, agent-pgdata:/var/lib/postgresql/data
healthcheck pg_isready -U agent -d agent, 2s interval, 30 retries
```

PostgreSQL 16 is pinned rather than floating on `postgres:latest`
because the plan's persistence layer uses `FOR UPDATE SKIP LOCKED` and
generated columns, and a lock-ordering or planner change between major
versions is exactly the kind of thing that turns a green suite red on
an unrelated Tuesday. The version is a versioned asset in the sense
ADR-0024 uses the term: it is pinned, it is committed, and changing it
is a reviewed change.

The credentials are `agent/agent` and they are in `.env.example`,
which the secret scanner scans rather than exempts. A scanner that
skips the one file everyone copies is not a scanner. These credentials
pass because they are matched by the allowlist with the prose reason
"local compose default, not reachable from outside the host network",
which is the mechanism ADR-0024 already specifies for a match that is
deliberate.

A second service is not added at Milestone 0. The artifact store is
the filesystem until Section 2.2's S3 step, the queue is PostgreSQL,
and there is nothing else to run. When the sandbox milestone needs a
container runtime it brings its own arrangement; adding a placeholder
now would be a service nobody starts and nobody maintains.

## The CI workflow

One file, `.github/workflows/ci.yml`, and four jobs matching
[evaluation-harness.md](evaluation-harness.md) exactly.

```text
job           target invoked         needs     runs on
------------  ---------------------  --------  ----------------
1 static      make lint typecheck    nothing   every push, PR
              test-static docs
2 contract    make test-contract     nothing   every push, PR
3 integration make test-integration  postgres  every push, PR
4 live        make test-live         secrets   schedule, manual
```

Jobs 1 and 2 partition `make check`, split so the cheap one fails
first. This is the only place where CI's shape differs from the
Makefile's, and it differs in scheduling rather than in content: the
union of the two jobs is exactly `make check`, no check appears in
both, and a developer who runs `make check` locally has run both
jobs' contents.

Job 3 uses a GitHub Actions service container rather than the compose
file, because the compose file publishes a port on the developer's
host and a service container does not need to. The database name,
user, and password are the same three values, which keeps
`DATABASE_URL` construction identical in both places.

Job 4 does not run on a pull request. Live tests cost money and
require a credential that a fork's pull request cannot have, so the
job runs on a schedule and on manual dispatch, and its absence from
the pull-request path is a fact rather than an omission — Section 20.4
already gates live tests behind `RUN_LIVE_MODEL_TESTS=1` and the
workflow is the place that variable is set.

Three workflow-level facts complete the definition:

1.  **Triggers.** Push to the default branch, pull request against it,
    and `workflow_dispatch`. Jobs 1 through 3 run on all three; job 4
    runs on `workflow_dispatch` and a nightly `schedule`.
2.  **Python version.** A single version, 3.12, not a matrix. The
    project pins `requires-python >=3.12` and runs one deployment; a
    matrix here would test a configuration nothing runs.
3.  **Caching.** The `uv` cache is keyed on `uv.lock`. The lockfile is
    committed, so a cache miss is a dependency change and never a
    coincidence.

A fifth job is not added for the documentation build. `make docs`
exists and `mkdocs build --strict` runs inside job 1, because the docs
check the gate registry requires is a static check with no database
and no fixtures, and giving it a job of its own would put a second
`uv sync` on the critical path to catch a broken link.

## The test tree

Section 4's tree names six directories plus `eval_cases/`. Section 20.4
names five categories, and
[evaluation-harness.md](evaluation-harness.md) already named the sixth
— `resilience` — and gave it the same treatment as the others. What is
still missing is the mapping from directory to selector: which marker
each carries, and therefore which of the four CI jobs runs it.

```text
directory           category        marker      first milestone
------------------  --------------  ----------  ---------------
tests/unit          unit            static      M0
tests/contract      contract        (none)      M1
tests/integration   integration     integration M2
tests/resilience    interventions   integration M2
tests/security      security        integration M2
tests/live          live            live        M3
tests/eval_cases    case fixtures   n/a         M1
```

Three of the six carry the `integration` marker because all three need
a database, and the marker exists to answer "can this run without
Docker" rather than to restate the directory name. Structural gates —
the import-boundary walk, transaction hygiene, the secret scanner,
contract-module coverage — live in `tests/unit` and carry `static`,
because they are pure functions over the source tree.

`tests/eval_cases/` holds data, not tests. The case files the harness
loads live there; the runner that loads them lives in
`src/agent_core/evals/`, which is where Section 4 puts it.

### Deterministic tests and cases 1 through 11 are one deliverable

Milestone 1 lists "Deterministic tests" and the harness's build order
lists "Case schema, loader, and runner. Milestone 1, with cases 1
through 11." These are the same work under two names, and reading them
as two deliverables is how a milestone acquires a second, informal
test framework beside the one that was specified.

The Milestone 1 test surface is therefore three things, and the third
is the one the plan names:

1.  Unit tests for the domain — state transitions, budget accounting,
    tool schema validation, and the rest of Section 20.4's unit list,
    for the parts that exist at Milestone 1.
2.  The contract suite, run against the in-memory adapters and the
    fake provider, which is what ADR-0024 means when it says the
    in-memory tier is adapters rather than doubles.
3.  Cases 1 through 11, loaded and executed by the harness runner.
    "Deterministic tests" is this, and it is deterministic because the
    `Clock` and `IdFactory` ports are pinned, not because the cases
    avoid randomness by convention.

Milestone 1's acceptance criterion "Direct-answer and calculator
scenarios pass" is satisfied by two of those eleven cases. It is not a
separate script.

### Naming

`test_<subject>.py` inside the directory that matches the category,
one test class per behaviour under test where classes help and plain
functions where they do not. `asyncio_mode = auto` means an `async
def` test needs no decorator, which removes the most common cause of a
test that passes by never awaiting anything.

## The initial ADRs

Milestone 0 implements "Initial ADRs" and Section 26 lists them
seventeenth. Section 4's tree names six files, and a note says version
2.0 "adds these ADRs (create the files as the milestones reach them)"
for ADR-0007 through ADR-0017. Meanwhile this design corpus already
contains twenty-five accepted ADRs, five of which Milestones 0 and 1
cite directly.

The deliverable is satisfied by carrying the accepted set forward, not
by authoring a fresh one. An ADR written new in the agent repository
for a decision this corpus already recorded would be a second and
divergent record of the same decision, and the failure mode is not
hypothetical: the two would be edited independently and the reader
would have no way to tell which one the code follows.

So Milestone 0 copies `docs/adr/` in full, keeps the numbering, keeps
`docs/adr/index.md`, and adds nothing. The tree's six filenames are a
subset of what arrives, and the "create the files as the milestones
reach them" note is already satisfied for ADR-0007 through ADR-0017
because those files exist.

New ADRs are authored in the agent repository from the first decision
the implementation makes that this corpus did not. The definition of
done's "Relevant ADRs are added or updated" is what governs from that
point, and the numbering continues from the highest number carried
over rather than restarting.

## Egress at Milestone 0

The harness's gate 7 requires that the deterministic suite run without
an API key, and observes that "without requiring an API key" is
usually implemented as "we did not configure one" — true until a
fixture falls through to a real client. Blocking egress turns the
claim into a test.

The Milestone 0 form of that block is a pytest fixture, not
infrastructure:

```text
scope       session, autouse, for the static and contract markers
mechanism   socket.socket patched to raise on connect
exempt      AF_UNIX, and 127.0.0.1 for the integration marker
failure     the test that attempted the connection fails, naming
            the host it tried to reach
```

This is what "a small amount of infrastructure work in Milestone 0"
amounts to: about thirty lines in `tests/conftest.py`, no firewall, no
container network policy, and no CI-runner configuration. It costs
nothing at Milestone 0, when nothing makes an outbound call, and that
is precisely why it belongs there — it is installed before the first
adapter that could violate it, so the first violation is a failing
test rather than a surprising invoice.

The `live` marker lifts the block. That is the marker's entire
purpose, and it is why live tests are selected by marker rather than
by an environment variable alone.

## What gets published

Two repositories publish two different things and the distinction has
been implicit.

**The agent repository** publishes a README and `docs/security.md`.
The README's twelve required explanations are listed in Section 25 and
the security document's trust-boundary table and control list are in
Section 22. Neither Milestone 0's implement list nor its acceptance
criteria name `docs/security.md`, but the definition of done requires
"Security implications are documented" for every milestone, and this
is the file that requirement writes to. It exists from the first
milestone that has security implications, which is Milestone 0 — the
secret scanner and the egress block are both security controls.

**This documentation repository** publishes two artifacts from the
same canonical Markdown: the MkDocs site, which is the complete
corpus, and a single self-contained HTML file listed in
`docs-manifest.yaml`. The manifest names four sources — the index, the
current milestone, the engineering plan, and the changelog — and that
is deliberate rather than an oversight. Those four are the documents
that are read start to finish. The sixteen specifications, the
milestone map, the readiness review, and the thirty-three ADRs are
reference material reached by cross-reference, and the site is where
cross-references work.

Widening the manifest is not free, and the reason is worth recording
because it will be proposed again. Concatenating fifty
documents into one file collapses their heading namespaces. The
headings `Decisions`, `Context`, and `Open questions for review`
appear in almost every one of them, and the anchor generator would
silently resolve every link to the first occurrence. Publishing the
full corpus as one file therefore requires per-document anchor
prefixing in
`scripts/build_docs.py`, which is real tooling work with a silent
failure mode. It is recorded below as an open question rather than
done badly.

## Conflicts this document resolves

1.  **One CI criterion, four CI jobs.** Section 21's Milestone 0
    acceptance criterion is "CI executes `make check`."
    [evaluation-harness.md](evaluation-harness.md) specifies four jobs,
    one needing PostgreSQL and one needing credentials. Resolved by
    partition: `make check` is jobs 1 and 2, CI runs `make check`'s
    two halves as separate jobs and then the two that need resources.
    Both statements are true afterward and neither was weakened.
2.  **`make test` cannot be inside `make check`.** Section 21 requires
    both targets, and Section 24 requires `make check` to succeed as a
    definition-of-done item. If `test` includes the integration suite,
    `check` needs a database and stops being runnable on a fresh
    checkout with no Docker. Resolved by making `check` depend on
    `test-fast` rather than `test`, and by keeping `test` as the
    broader local target it reads as.
3.  **Six test directories, no selectors.** Section 20.4 names five
    categories, [evaluation-harness.md](evaluation-harness.md) names
    `resilience` as the sixth, and nothing says how a category is
    selected at the command line. Resolved by assigning each directory
    a marker, so that "can this run without Docker" is answered by the
    marker rather than by remembering which directory needs a
    database. No category was added or removed.
4.  **"Deterministic tests" and "cases 1 through 11".** Milestone 1
    lists the first; the harness's build order lists the second at the
    same milestone. Resolved as one deliverable under two names. The
    alternative reading produces a second test framework beside the
    specified one.
5.  **Three readings of "Initial ADRs".** Section 4's tree names six
    files, a note defers ADR-0007 through ADR-0017 to their
    milestones, and Milestones 0 and 1 cite five ADRs of which three
    are outside the six. Resolved by carrying the accepted set forward
    whole, which satisfies all three readings at once and creates no
    second record of a decision already made.

## Decisions

1.  **`make check` and CI are the same set of checks.** CI runs no
    command that is not a Makefile target, and `make check` is the
    exact union of the two CI jobs that need neither a database nor a
    credential. A gate added to one is added to both, because both
    select by pytest marker rather than by listing files.
2.  **Six targets are added to Section 21's eight.** `test-static`,
    `test-contract`, `test-fast`, `test-integration`, `test-live`, and
    `docs`. Each exists because a CI job invokes it; none exists
    because it seemed useful.
3.  **`test-static` and `test-contract` partition `test-fast`.** The
    contract selector is a negation — not static, not integration, not
    live — so a test with no marker runs in job 2 rather than nowhere.
    A new unmarked test is visible by default, which is the safe
    direction for the mistake to fall.
4.  **`--strict-markers` and `--strict-config` are on.** A mistyped
    marker is an error rather than a test that silently never runs,
    which matters most for `live`, the one marker whose tests cost
    money when they do run and prove nothing when they do not.
5.  **`make db-up` polls the compose healthcheck.** `docker compose up
    -d` returns before PostgreSQL accepts connections, and a
    `db-up`-then-`migrate` sequence that fails intermittently is the
    first thing a new contributor meets.
6.  **`db-up` and `migrate` stay separate.** Section 25 documents them
    as two commands and ADR-0024 forbids migrating from the
    composition root. Chaining them in the Makefile would make a
    two-process deployment look like a one-command one.
7.  **PostgreSQL 16 is pinned, not floated.** The persistence layer
    depends on `FOR UPDATE SKIP LOCKED` semantics, and a major-version
    change is a reviewed change rather than whatever `latest` resolved
    to that morning.
8.  **The compose credentials are in `.env.example` and the scanner
    scans it.** They pass by allowlist entry with a prose reason, not
    by exemption. A scanner that skips the file everyone copies is not
    a scanner.
9.  **One compose service at Milestone 0.** PostgreSQL and nothing
    else. Artifacts are the filesystem, the queue is PostgreSQL, and a
    placeholder service is one nobody starts and nobody maintains.
10. **One workflow file, four jobs, one Python version.** No matrix:
    the project pins `>=3.12` and runs one deployment, so a matrix
    would test a configuration nothing runs. The `uv` cache keys on
    `uv.lock`, so a cache miss means a dependency changed.
11. **Job 4 does not run on pull requests.** Live tests need a
    credential a fork cannot have and cost money per run. Schedule and
    manual dispatch only, which is where `RUN_LIVE_MODEL_TESTS=1` is
    set.
12. **`mkdocs build --strict` runs inside job 1.** The docs check is a
    static check with no database and no fixtures; a fifth job would
    put a second dependency install on the critical path to catch a
    broken link.
13. **Structured logging is structlog, configured in phase 1 of the
    composition root**, with two renderers keyed on deployment mode
    and a redaction processor rather than a redaction convention.
    Section 19's eight fields are bound by context variable at four
    named points, and `trace_id` is read from the active span rather
    than threaded through call sites.
14. **Redaction truncates content keys instead of dropping them.**
    First 200 characters and a length. A log line saying a tool
    returned something is useful; one carrying 40 KB of tool output is
    what Section 19 forbids.
15. **Egress is blocked by an autouse pytest fixture at Milestone 0.**
    Roughly thirty lines in `conftest.py`, exempting `AF_UNIX` and
    loopback for the integration marker, lifted by the `live` marker.
    It is installed before the first adapter that could violate it.
16. **The ADR set is carried into the agent repository, not
    reauthored.** Numbering continues from the highest number carried
    over. New ADRs begin at the first decision this corpus did not
    already record.
17. **`docs-manifest.yaml` stays at four sources.** The single-file
    HTML is the plan-of-record bundle; the site is the complete
    corpus. Widening the manifest requires per-document anchor
    prefixing first, and is recorded as an open question rather than
    done with silently colliding anchors.

## Open questions for review

1.  **Should the combined HTML publication include the specifications
    and ADRs?** Doing it correctly needs per-document anchor prefixing
    in `scripts/build_docs.py`, because thirty-seven documents share
    heading names like "Decisions" and the generator resolves
    duplicates to the first occurrence. Cost is perhaps an afternoon;
    reversal cost is near zero, since the manifest and the script are
    both generated-output tooling and no canonical source changes.
2.  **Is a single Python version right, or should CI run a matrix?**
    One version matches one deployment and keeps the pull-request path
    under twelve minutes. A matrix would catch the day a dependency
    drops 3.12 support, at roughly double the CI minutes. Reversal
    cost is low — a matrix is three lines — but the choice sets an
    expectation about what "supported" means.
3.  **Should the nightly live job exist before Milestone 3?** No live
    adapter exists until then, so the job would run an empty
    selection. Defining it at Milestone 0 keeps the workflow file
    complete and costs one skipped job per night; deferring it means
    editing the workflow later. Chosen: define it, skipped.
4.  **Is `postgres:16-alpine` the right pin?** Alpine images are
    smaller and occasionally differ in locale and collation
    behaviour, which is exactly the surface a sort-order-dependent
    query would hit. The non-Alpine tag is larger and closer to what a
    managed provider runs.
5.  **Where does the eleventh Milestone 0 deliverable live?**
    "Structured logging bootstrap" is specified here, but its code
    sits in `agent_core/observability/logging.py` and is called from
    the composition root, which belongs to
    [bootstrap-and-composition.md](bootstrap-and-composition.md).
    The split is deliberate — configuration shape here, call site
    there — but it is the one place where two specifications describe
    one file.
