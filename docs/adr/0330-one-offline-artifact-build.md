# ADR-0330: One offline artifact build

- Status: Accepted for implementation by the owner on 2026-11-16.
- Amends: ADR-0243's separate write/render/export workflow and ADR-0260's
  guide-reading workflow. No general sandbox exemption or milestone move.
- Detailed design: `docs/plan/sandbox-isolation.md`.

## Context

Creating an infographic required repeated approvals for reading a bundled guide,
writing inputs and running a renderer. Each direct approval pause ends the worker
claim and discards its workspace, so later commands cannot find earlier inputs.
The owner explicitly requested a single approval for the offline artifact build,
approval-free guide reads and verified delivery to `dev`.

## Decision

`artifact.build` is one sandbox-targeted code-execution invocation. Its immutable
arguments contain all UTF-8 input files, an argument-vector command and an explicit
list of output paths, filenames and media types. The ordinary code-execution
policy applies once to the entire package: after untrusted input it requires one
approval; a trusted computation already allowed by policy adds no new prompt.
Denials, scope checks, exact-argument approval revalidation and non-idempotent
uncertainty handling remain unchanged. Changed packages need a new invocation.
No general standing grant or policy-profile change is introduced.

Only after authorization, a fresh sandbox stages the package, executes once and
copies its requested outputs into the chat's existing artifact store before
destroying the sandbox. It always denies network access, has no credential or
environment passthrough, and exposes no tool bridge. It uses existing sandbox
resource limits and the enclosing tool deadline. Input is bounded to 32 files and
1 MiB total, with at most 16 outputs of 32 MiB each. Paths are relative and cannot
escape the workspace. A nonzero exit, timeout or killed process attaches nothing.
Export failures report any files already attached, without claiming a complete
build. An uncertain invocation is never automatically rerun.

`artifact.read_guide` reads only the packaged visualization guide, by named
section, without executing a command or accepting a filesystem path. It is a
read-only, no-side-effect tool; outside content cannot cause a guide approval.
The sandbox image and this tool consume the same checked-in guide. Default-agent
instructions select these two deferred tools for offline artifact creation.
Existing session catalogs stay pinned; new chats receive the tools after release.

## Verification

Required evidence covers a guide read after untrusted input without approval;
one approval followed by input staging, execution and durable attachments;
denial and missing scopes without execution; altered packages; execution and
export failures; bounded paths, bytes and outputs; no bridge, network or parent
environment; cleanup and non-replay. A real Docker build renders and attaches an
image. Delivery to production and owner acceptance remain separate from `dev`.

The regression began with
`scripts/run_in_linux_container.sh uv run pytest -q tests/unit/test_offline_artifact_build.py`,
which failed because neither capability was registered. The composed tests now
exercise a web read, guide read and complete build: exactly one approval, no
staging before approval, one execution, durable bytes after cleanup, an idempotent
approval replay and a new approval for changed inputs. Denial, each missing scope,
path and UTF-8 bounds, export failure and cancellation are covered in the same file.
The focused artifact, sandbox and production-roster selection passed 73 tests.

The local Docker sandbox passed all 27 static-visual and isolation tests in
99.82 seconds, exercising the guide's actual renderer example,
downloaded PNG, CSV and JSON, and a refused cross-tenant download. The release
wheel contains the shared guide. The browser context and production-roster
selection passed 106 tests without increasing any context limit. The final
offline-build and hash-seed identity selection passed 22 tests in 19.54 seconds;
the identity assertion now includes the new tool's three scopes while preserving
every deterministic-prefix assertion. Lint and typechecking passed.
The full contract partition passed all 2,309 tests in 170.85 seconds, using
`make -o test-static test-contract` after the separate completed static run.

The full static partition completed with 10,557 passes, three skips and four
failures in 949.49 seconds. One failure was the old multi-scope roster expectation,
corrected and verified above. The other three report the same pre-existing
`runtime/browser_workflows.py` dependency on `tools.validation`. An architecture
scan of an untouched archive of `dev` at `d34e01a5` and the changed checkout
returned the identical single violation. This work neither changes that runtime
nor relaxes its boundary checks; the repository-wide static gate is not green.
The subsequent PR 255 promotion repair restores that dependency boundary by
injecting the validator through bootstrap; its focused evidence is recorded in
[ADR-0328](0328-browser-workflow-capability-preflight.md).

Documentation checks include citation repair. Reading lane A (also the
diff-derived minimum). No engineering plan requirement or policy profile is
weakened, and no milestone gate is marked complete. Production release and a
new chat remain necessary before the owner can use these tools.

PR 255 review fixes also preserve completed attachments when a later export raises
an unexpected storage exception. The result is an explicit, non-retryable
`tool.internal_error` with no storage diagnostics; cancellation still propagates
and destroys the sandbox. The regression command
`scripts/run_in_linux_container.sh uv run pytest -q tests/unit/test_offline_artifact_build.py -k export_exception`
first failed for `OSError` and `RuntimeError` escaping after the first export.
The artifact-build/export selection now passes all 40 cases in 10.09 seconds,
including cancellation during export. All 17 real Docker static-visual cases pass
in 64.35 seconds; the bundled example explicitly requires PNG, CSV and JSON media
types before validating their contents. Lint and type checking pass. This repairs
the existing export-failure contract without changing approval or replay policy.
