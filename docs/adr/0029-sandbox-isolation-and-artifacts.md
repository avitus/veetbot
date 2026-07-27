# ADR-0029: Isolated execution, the egress boundary, and the artifact store

- Status: Accepted
- Date: 2026-07-27
- Related: Sections 7 (ports), 8.2 (builtin tools), 8.5 (programmatic
  orchestration), 13 (error taxonomy), 18 (sandbox and artifacts), 21
  (Milestone 6), 22 (security baseline), 28 (sandbox isolation),
  ADR-0008 (sandbox isolation), ADR-0015 (programmatic tool
  orchestration), ADR-0017 (layered approval), ADR-0021 (tool
  execution pipeline and MCP), ADR-0022 (the gate registry),
  ADR-0024 (composition root), ADR-0026 (builtin tools), ADR-0027
  (the milestone map), ADR-0028 (the HTTP API surface)
- Detailed design: `docs/plan/sandbox-isolation.md`

## Context

The readiness review gave Milestone 6 the only verdict of its kind in
the corpus: not ready, zero gates, no specification. Eleven implement
bullets, the sentence "completion of this milestone defines Version
0.1", and nothing between the two.

That verdict is narrower than "the sandbox is undocumented", which is
not true. Section 28 and ADR-0008 settle the hard part — six threats,
the rejection of the Docker socket, a kernel-isolating runtime as the
production default with shared-kernel containers demoted to a
development fallback, an execution service that owns lifecycle and
holds no secrets, and the runtime restrictions by category. Section 18
adds the tool: an argument vector rather than a shell string, a result
shape with `files_changed`, and four rules for artifact storage.

What the corpus does not contain is the layer underneath. Section 7's
`ExecutionEnvironment` port has three methods over four types —
`EnvironmentSpec`, `EnvironmentHandle`, `ExecutionCommand`,
`ExecutionResult` — that appear nowhere else in fifty documents.
`ArtifactStore` has two methods over `ArtifactMetadata` and
`ArtifactRef`, which ADR-0028 identified as the last two
referenced-and-undeclared types in the corpus. `WorkspaceHandle`,
`ArtifactWriter`, and `CredentialResolver` sit on
`ToolExecutionContext` in the same condition. Eight types named and
never defined.

One mechanism is worse than undefined: it is already depended on.
`tool-system.md` says tenant-configured MCP server URLs "go through
the same egress allowlist the sandbox spec establishes", and that
"the proxy is what makes a tenant-supplied URL safe to dial; without
it, server configuration is an SSRF surface pointed at the worker's
network." Section 28.5 says egress is denied by default and that
enabled egress is routed through an allowlisting proxy. That is the
right shape and it is not a grammar, an evaluation order, or an
owner.

Two further problems became visible while writing the design.
`builtin-tools.md` places `sandbox.run_command` at Milestone 5 and
says twice that Milestone 5 is the sandbox milestone; Section 21 names
Milestone 5 "HTTP API and SSE" and Milestone 6 "Isolated execution and
artifacts", and Milestone 6's implement list names the tool. And the
readiness review's red-team test — a container escape that reaches no
secret and no other tenant — had no harness case behind it.

## Decision

1.  **The workspace is a cache, not state.** It exists for a worker's
    lease on a run, not for the run's logical lifetime. It is created
    lazily at the first sandbox-targeted call, held across steps and
    across a hold shorter than `approval_hold_seconds`, and destroyed
    with the sandbox when the lease ends. A run that resumes gets a
    fresh, empty workspace. Anything that must survive is an artifact.
2.  **A durable per-run workspace is rejected.** It needs shared
    storage between execution hosts, which reintroduces the
    cross-tenant blast radius the topology removes; it makes the
    workspace state whose consistency with the event log nobody owns;
    and it turns crash-resume into recovery. With the workspace as a
    cache, resume needs no recovery at all.
3.  **The environment a sandbox sees is built, not filtered**, in
    three tiers. Tier 0 is platform and provider credentials, never
    present and not configurable. Tier 1 is operator-named passthrough.
    Tier 2 is synthesized by the platform. Fail-closed means tier 2
    alone — never the parent environment, and never a failed run.
4.  **Egress is one policy with two enforcement points**: the sandbox
    proxy, and a guard on the worker for any URL the platform dials on
    a tenant's behalf. This is the mechanism `tool-system.md` already
    names. A second implementation is how the two drift, and the drift
    is only ever discovered by the request that should have been
    refused.
5.  **The allowlist grammar has no open mode and no IP destination
    form**, a wildcard is one leftmost label, and ports are explicit
    with no default. The proxy resolves the name itself, checks every
    resolved address against a fixed private-range denylist including
    `169.254.169.254`, and dials the address it resolved rather than
    re-resolving the name.
6.  **Egress requires two independent yeses** — an operator-configured
    allowlist and a `SANDBOX_NETWORK` resolution, which the `default`
    policy profile denies. An approved `SANDBOX_NETWORK` grants the
    allowlist, not the internet.
7.  **Timeouts compose by minimum**, over the service hard cap, the
    environment's wall clock, the remaining run budget, and the
    model's clamped value. The service's cap applies whether or not
    anything else was supplied.
8.  **`ExecutionEnvironment` keeps three methods and workspace file
    access is a separate port.** A port carrying `execute` beside
    `read_file` hands arbitrary execution to every tool that only
    needed to read a file — including `artifact.export`, whose entire
    job is to copy one file.
9.  **No host path crosses the port in either direction.** The
    workspace is mounted at the constant `/workspace`, the handle
    carries no path, and `files_changed` paths are workspace-relative.
10. **`files_changed` is computed by the execution service** against a
    pre-command snapshot, not reported by the command. A command that
    reports its own changes can omit one, and the omitted one is the
    interesting case.
11. **Artifacts are streamed back through the worker** rather than
    written to the object store by the execution service, which holds
    no credential for it and must not be given one. The cost is a hop.
12. **The storage key is derived from `(tenant_id, artifact_id)` and
    nothing else.** A filename is metadata, sanitized on the way out
    rather than on the way in, and never a path component.
13. **`fake` becomes a fourth `SandboxMechanism` value**, a production
    adapter in the same sense as the in-memory repositories, running
    the contract suite unchanged. It is refused in production by the
    same startup check that refuses `docker`.
14. **Isolation is not in the contract suite.** It is a deployment
    property rather than a port semantic; the security gates assert it
    against the real runtime.
15. **Thirteen gates in a new twelfth area, `sandbox`.** Eleven at
    Milestone 6, one at Milestone 1 where the startup check lives, and
    one at Milestone 4 where path containment is written.
16. **`sandbox.run_command` is Milestone 6, not Milestone 5.** Section
    8.2's "the sandbox milestone" is Milestone 6. This corrects a
    transcription error in `builtin-tools.md` rather than reversing a
    decision, and `artifact.export` stays where it is.
17. **The red-team escape test becomes harness case 26**, in the
    security category, without renumbering the twenty-five.
18. **Artifacts expire after thirty days by default**, deleted by
    `expires_at` and never by reference counting. Recorded as an open
    question, because it is the one default here that silently deletes
    something a user might expect to keep.

## Consequences

- Milestone 6 becomes implementable. The eight undeclared types have
  declarations, the egress allowlist has a grammar and an owner, the
  workspace has a lifecycle, the limits have numbers, and the
  timeout layering has one rule.
- Thirteen hard gates are added, taking Milestone 6 from zero to
  eleven and closing the last milestone with implementation work and
  no verification. The registry gains a twelfth area, `sandbox`, and
  goes from one hundred and five entries to one hundred and eighteen.
  The milestone map's table and census and the harness's gate table
  are updated; ADR-0027 and ADR-0028 are not, because each is a record
  of what was true when it was decided.
- `ArtifactMetadata` and `ArtifactRef` acquire declarations, which
  closes the referenced-and-undeclared list ADR-0028 left at two.
  `WorkspaceHandle`, `ArtifactWriter`, and `CredentialResolver` close
  with them.
- `tool-system.md`'s dependency on an egress allowlist is satisfied
  without editing that document. The guard ships at Milestone 6 with
  the policy; its first caller arrives at Milestone 8.
- `builtin-tools.md` is edited in three places for the milestone
  correction. Its classification tables are untouched.
- The harness gains one case, numbered 26, and the heading "the
  twenty-five cases" is unchanged because an anchor gate depends on
  it and the document promises the twenty-five stay twenty-five.
- The model has to be told the workspace is not durable. This is a
  sentence in a tool description rather than a mechanism, and leaving
  it out produces a class of failure no amount of retry logic fixes.
- Milestone 6 is the last milestone whose readiness verdict was "not
  ready" with work in it. Milestone 8's skills half remains undesigned
  and is the next document.

## Alternatives considered

- **A durable workspace that follows a run across workers**: rejected
  under decision 2. It is the design most systems reach for and it
  buys convenience with shared mutable storage across the exact
  boundary the topology exists to establish. Recorded as an open
  question for task runs specifically, where the loss is real.
- **Filtering the worker's environment rather than building a new
  one**: rejected. A filter has to be complete to be correct and a
  build has to be wrong on purpose. Every credential that has ever
  leaked into a subprocess leaked through a filter that was missing an
  entry.
- **Falling back to the parent environment when the passthrough list
  cannot be read**: rejected, and it is the tempting failure mode
  because it keeps deployments working. A sandbox missing an optional
  variable is degraded; a sandbox holding the worker's environment is
  a breach.
- **A `security` gate area**: rejected. Areas in this registry name
  subjects with a spec behind each, not cross-cutting properties, and
  a `security` area would eventually pull gates from six other
  documents into a category that owns none of them.
- **Splitting the gates across `structure` and `tool`**: rejected. One
  document owns all thirteen, and `memory` already demonstrates the
  established direction — two specs sharing one area, rather than one
  spec straddling two.
- **An `allow` egress mode for trusted deployments**: rejected. An
  open mode is one configuration typo away from being selected, and
  there is no deployment that needs it that cannot write a list.
- **Permitting IP-address destinations in the allowlist**: rejected.
  It is a way to write down precisely what the private-range check
  exists to refuse, and the entries that would use it are the ones
  nobody reviews.
- **Suffix matching for wildcards**: rejected. `*.example.com` as a
  suffix match accepts `evilexample.com`. Label-boundary matching is
  not harder and is not wrong.
- **Letting an approved `SANDBOX_NETWORK` open general egress**:
  rejected. It would make the allowlist a decoration and would make
  the approval prompt a lie, since no approver can enumerate the
  internet.
- **Trusting the model's `timeout_seconds` as the timeout**: rejected
  by Section 28.4 already; this document supplies the composition
  rule. A model can lower the bound and can never raise it.
- **Adding `read_file` and `write_file` to `ExecutionEnvironment`**:
  rejected. It collapses two capabilities into one port and makes
  every holder of the read capability a holder of arbitrary execution.
- **Letting the execution service write artifacts directly to the
  object store**: rejected. It saves a hop and it puts a storage
  credential on the host that runs untrusted code, which is the one
  thing the topology exists to prevent.
- **Composing the storage key from the filename**: rejected. It is
  the traversal bug, and defending it with sanitization means the
  defence is a string function that has to be right forever.
- **Reference-counting artifacts against events and memory records**:
  rejected. It is a distributed garbage collector, and the failure
  mode of getting it wrong is either a leak nobody notices or a
  deletion nobody can explain. An expired reference resolving to 404
  is honest.
- **Treating `fake` as a test double outside the mechanism enum**:
  rejected, following the precedent of the in-memory repositories. A
  double outside the enum is a double nobody runs the contract suite
  against.
- **Renumbering the harness cases to fit the escape test in**:
  rejected. `gate.harness.anchor_resolves` depends on the heading and
  the document promises the twenty-five stay twenty-five.
- **Leaving `sandbox.run_command` at Milestone 5 and treating the
  plan as wrong**: rejected on evidence. Section 21's milestone titles
  and Milestone 6's implement list agree with each other, the
  milestone map agrees with both, and the tool needs an
  `ExecutionEnvironment` adapter that does not exist until Milestone
  6.
