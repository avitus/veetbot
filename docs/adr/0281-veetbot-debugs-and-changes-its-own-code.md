# ADR-0281: Veetbot debugs and proposes changes to its own code

- Status: Accepted for implementation on 2026-11-01. The owner asked for
  first-class coding and debugging power after Veetbot could not explain why its
  first Cockpit widget kept failing, and chose power "up to opening PRs".
  Production activation stays separate: it needs the owner's GitHub App change
  below and the release of the new image.
- Date: 2026-11-01
- Related: ADR-0240, ADR-0268, ADR-0273
- Scope: five deferred builtin tools, one offline sandbox image, one owner-scoped
  store and one GitHub adapter. This workstream does not advance a milestone or
  gate.

## Context

On 2026-11-01 the owner asked Veetbot why a widget's refresh schedule kept
pausing. Veetbot answered that its tools did not expose the failures and asked
the owner to paste them. The answer sat in three read-only API routes: the
schedule's occurrences, each run's status, and the run's events. A person with
the API token found the cause in minutes. Veetbot also could not read the design
documents, the sample GitHub job that already solved the problem, or its own
source, and it could not test a fix or offer one.

## Decision

1. **Evidence.** `run.inspect` reads the owner's schedule history, a run's
   failure and its timed event timeline. It is a read with no side effect,
   offered wherever the owner's scopes allow.
2. **Source.** `code.read` lists, reads and searches the source tree of the
   running release, so what Veetbot reads is what is deployed. It serves only
   allowlisted, tracked-source directories, never a dotfile, an environment file
   or a virtual environment. The repository is public, so this exposes nothing
   new.
3. **Reproduction.** `code.draft` keeps one owner-scoped set of edits against the
   running release. `code.test` runs a closed set of checks (pytest, ruff, mypy
   and the Cockpit tests) over that draft in a fresh sandbox built from the same
   release, with networking off, a read-only root, an empty environment and no
   credentials. Its output is untrusted.
4. **Proposal.** `code.propose` opens a pull request from a new `veetbot/<slug>`
   branch to `dev`. Veetbot's power stops there:
   - It never merges, approves, deploys, force-pushes, or writes to `main`,
     `dev` or any existing branch.
   - Every proposal asks the owner, showing the bounded diff. There is no
     `tool_rules` entry that would skip this.
   - Only the owner's interactive chat is offered these writes. Scheduled,
     delegated, email and device sessions are not, and the tools refuse a call
     by name there too.
   - The draft may not touch `.circleci/`, `.github/`, `deploy/` or `policy/`.
     Those paths decide what runs with credentials or what the platform allows,
     so they stay with people.
   - Added lines pass the personal-data screen and a high-confidence secret check
     before anything leaves PostgreSQL, because the repository is public.
   - Only the maintenance role holds the GitHub key. The tool records an outbox
     entry and the worker commits.
   - A branch push starts no CI on its own (`verify` needs `main` or an explicit
     `run_verify`), and Veetbot has no way to request a pipeline. CodeRabbit and
     the owner review the PR on the ordinary path.
5. **Diagnostics that explain themselves.** Each job broker call records its
   outcome and duration without its arguments. Known job failure codes carry a
   repair hint. The widget watchdog stops counting sleep and hidden pages against
   a widget (`docs/plan/cockpit.md`).
6. **Live previews.** `cockpit.check` can preview a widget with the owner's
   current feeds and settings, amending ADR-0273's synthetic-only preview. The
   values reach only the offline builder sandbox, stay out of the package, the
   screen and the public branch, and the result and its image are untrusted and
   private.

The mechanism is specified in `docs/plan/builtin-tools.md`, "Platform diagnostics
and code tools".

## Consequences

- Veetbot can establish a cause from evidence, read the code involved, test a
  repair, and hand the owner a reviewable PR.
- The trust boundary is the owner's approval and the ordinary review path. A
  proposed change has no effect until a person merges it.
- A sandboxed test run executes code the model wrote. It runs under the same
  isolation as `sandbox.run_command`, in a release-pinned image with no network.
- Releases build and retain one more image.
- Tests that need PostgreSQL, Docker or a browser engine do not run in the
  sandbox. Veetbot reports that limit, and hosted CI remains the full verification.

## Activation by the owner

1. Give the Cockpit GitHub App `Pull requests: read and write`.
2. Let the App create and update branches matching `veetbot/**`, keeping every
   other ruleset unchanged.
3. Set `AGENT_CODE_TOOLS_ENABLED=1` and, for proposals,
   `AGENT_CODE_PROPOSALS_ENABLED=1`.

## Rejected alternatives

- **Self-merge or self-deploy.** The owner stopped at opening PRs.
- **A general shell over the production host.** Evidence reads and an offline
  sandbox cover the need without giving a model a path to credentials.
- **Reading source through GitHub.** It adds a network dependency and can differ
  from what is deployed.
