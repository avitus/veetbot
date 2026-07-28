---
title: Sandbox Isolation and Artifacts
status: design
canonical: true
---

# Isolated execution, the egress boundary, and the artifact store

## Eight implement bullets, a named dependency, and a zero

[readiness.md](readiness.md) gives Milestone 6 the only verdict of its
kind in the corpus: not ready, zero gates, no specification. Every
other milestone either has a detailed design behind it or has so
little work in it that the plan's own section is enough. Milestone 6
has eleven implement bullets, the sentence "completion of this
milestone defines Version 0.1", and nothing between the two.

That is the visible symptom. The cause is narrower and worth stating
precisely, because "the sandbox is undocumented" is not true.

Section 28 is a real design and it settles the hard part. It names
six threats, rejects the Docker socket outright, chooses a
kernel-isolating runtime as the multi-tenant default with plain
shared-kernel containers demoted to a development fallback, puts
sandbox lifecycle in a dedicated execution service that holds no
secrets, and lists the runtime restrictions by category. Section 18
adds the tool: an argument vector rather than a shell string, a
result shape with `files_changed`, and four rules for artifact
storage. ADR-0008 records the decision.

What the corpus does not contain is the layer underneath. Section 7
declares an `ExecutionEnvironment` port with three methods whose four
parameter and return types — `EnvironmentSpec`, `EnvironmentHandle`,
`ExecutionCommand`, `ExecutionResult` — appear nowhere else in fifty
documents. `ArtifactStore` declares two methods over `ArtifactMetadata`
and `ArtifactRef`, which are likewise named once and never defined;
[http-api-and-streaming.md](http-api-and-streaming.md) already notes
that after `SessionStatus` was declared these two were the only
referenced-and-undeclared types left in the corpus. `WorkspaceHandle`,
`ArtifactWriter`, and `CredentialResolver` sit on `ToolExecutionContext`
in the same condition.

And one document has already been written against a mechanism this
one was supposed to establish. [tool-system.md](tool-system.md) says
of tenant-configured MCP servers that their URLs "go through the same
egress allowlist the sandbox spec establishes, so a server URL is
subject to the same destination policy as any other outbound
request", and adds that "the proxy is what makes a tenant-supplied
URL safe to dial; without it, server configuration is an SSRF surface
pointed at the worker's network." No such allowlist exists. Section
28.5 says egress is denied by default and that enabled egress is
routed "through an allowlisting proxy", which is the right shape and
is not a grammar, an evaluation order, or an owner.

This document supplies that layer: the eight undeclared types, the
allowlist with its grammar and its two enforcement points, the
workspace lifecycle, the resource limits as numbers, the timeout
layering rule, and thirteen gates that take Milestone 6 from zero.

## What this document does not change

Section 28 remains the statement of what isolation is for and which
mechanism the platform runs. This document is subordinate to it the
way [runtime-loop.md](runtime-loop.md) is subordinate to Section 12:
where the two overlap, Section 28's sentence is the requirement and
this document's is the mechanism.

Specifically unchanged: the threat model; the rejection of the Docker
socket; the choice of a microVM or gVisor-backed runtime as the
production default and rootless Docker as a development-only
fallback; the rule that production startup refuses the development
fallback; the topology in which a dedicated execution service owns
sandbox lifecycle and holds no secrets; the rule that the worker
calls the port and never a container runtime; and every runtime
restriction listed in Section 28.4.

Unchanged from Section 18: the argument vector, the refusal to accept
a shell string by default, the `sandbox.run_command` request and
result field names, and the four artifact-storage rules — outside the
source tree, opaque identifiers, SHA-256, metadata in PostgreSQL.

Unchanged from other documents, and deliberately not restated here:
output truncation and artifactization, which
[tool-system.md](tool-system.md) specifies completely, down to the
head-and-tail split and the hard ceiling at four times
`maximum_output_bytes`; the fourteen-step tool pipeline, of which
sandbox execution is step 11; the programmatic orchestration bridge,
whose socket, token, dedup rule, and approval-hold behaviour are
tool-system's; the trust label `EXTERNAL_UNTRUSTED` and what the
context engine does with it; and the rule that artifact content is
always served as an attachment, which is ADR-0028's.

What this document does change is one milestone assignment, in
[builtin-tools.md](builtin-tools.md), and the change is a correction
of an off-by-one rather than a decision. It is recorded under
contradictions below.

## Where the boundary is

Section 28.3 draws the topology. The picture below is the same one
with the data that crosses each line written on it, because the
question an implementer asks is not "which process owns the sandbox"
but "what is in this message".

```text
  worker process                    execution service
  -------------                     -----------------
  tenant, run, lease                sandbox lifecycle
  database credentials              resource limits
  provider API keys                 the egress proxy
  object store credentials          no secrets at all
  the credential broker             no database access
        |                                  |
        |  EnvironmentSpec                 |
        |  ExecutionCommand                |
        |--------------------------------->|
        |                                  |
        |  ExecutionResult                 |     sandbox
        |  artifact bytes (streamed)       |    ---------
        |<---------------------------------|    untrusted
        |                                  |    code runs
        |                                  |    here
```

Three properties of that picture are load-bearing, and each of the
three is a gate below.

The first is that nothing on the worker's side of the line appears in
`EnvironmentSpec`. The specification carries identifiers, limits, an
image digest, an egress policy, and an environment mapping that has
already passed the scrubber. It carries no credential, no connection
string, and no bearer token except the one-time bridge token, which
authorizes exactly one orchestration turn against the worker and is
worthless outside it.

The second is that the worker imports no container runtime. Not
`docker`, not `podman`, not a Kubernetes client, not a subprocess
call to any of them. The worker holds the `ExecutionEnvironment`
port and the execution service holds the adapter. This is the
ports-and-adapters rule the repository already enforces with an
import-boundary walk, applied to one more edge, and it is the
difference between "the execution service holds no secrets" being a
deployment property and being a code property.

The third is that no host path crosses the line in either direction.
The workspace is mounted at a fixed path inside the sandbox and the
handle carries no path at all. An implementer who needs a host path
to write the adapter has found a bug in the adapter's design, not a
missing field.

## The execution service holds nothing worth stealing

Section 28.3 says the execution service holds no application secrets,
no database credentials, and no provider keys. Stated as a positive,
the service holds: the runtime it drives, the image cache, the
workspace volumes, the egress proxy and its configuration, and the
resource-limit enforcement. It has no route to PostgreSQL, no object
store credential, and no model provider key.

That has one consequence worth writing down, because it looks like an
omission when an implementer meets it. Artifacts produced in a
sandbox are streamed back through the worker rather than written to
the object store by the execution service. The service cannot write
to the store because it holds no credential for it, and giving it one
would put the credential on the host that runs untrusted code, which
is the thing the topology exists to prevent. The cost is one hop of
bandwidth. The benefit is that a total compromise of the execution
service yields workspace bytes and nothing else.

The service exposes one internal API, reachable only from the worker
network, authenticated with a service credential the sandbox cannot
read. In development the service is in-process and the API is a
direct call; the port is the same either way, which is the point of
having a port.

## The `ExecutionEnvironment` port

Section 7 fixes the three methods and their signatures. This section
supplies the types and the failure modes, and changes neither.

```python
class ExecutionEnvironment(Protocol):
    async def provision(
        self,
        specification: EnvironmentSpec,
    ) -> EnvironmentHandle:
        ...

    async def execute(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        ...

    async def destroy(self, environment: EnvironmentHandle) -> None:
        ...
```

Three methods and not six. There is no `read_file`, no `write_file`,
and no `list_directory` here. Workspace file access is a separate and
much narrower port, `WorkspaceHandle`, specified below, and both are
implemented by the same execution service.

Keeping them apart is what lets a tool that needs to read a file be
handed the ability to read a file and nothing else. A single port
carrying `execute` alongside `read_file` is a port that every holder
can run processes with, and `artifact.export` — an in-process tool
whose entire job is to copy one workspace file into the artifact
store — would then hold arbitrary code execution in order to do it.

### `EnvironmentSpec`

```python
@dataclass(frozen=True)
class EnvironmentSpec:
    tenant_id: TenantId
    run_id: RunId
    lease_epoch: int
    image_digest: str
    limits: ResourceLimits
    egress: EgressPolicy
    environment: Mapping[str, str]
    bridge: BridgeEndpoint | None
```

`image_digest` is a digest, never a tag. A tag is a mutable pointer,
and an image that changes underneath a tenant is both a
reproducibility problem and a supply-chain one. The registry
reference is resolved to a digest at configuration load and the
resolved digest is logged at startup, so the image a deployment is
running is a fact in the log rather than a question for a registry.

`lease_epoch` is carried so the reaper can answer "does this sandbox
still belong to a live worker" without a database lookup. It is the
same fencing token
[event-log-and-persistence.md](event-log-and-persistence.md)
establishes for the run queue, used here for a second purpose.

`environment` has already passed the scrubber described below. The
port takes it as data because the execution service must not be the
component that decides what a credential is; that decision is on the
worker's side of the boundary, where the configuration lives.

`bridge` is present only for a turn that runs programmatic
orchestration, and it carries the socket path inside the sandbox and
the one-time token. Both belong to [tool-system.md](tool-system.md);
they appear here because the specification is what carries them in.

### `ResourceLimits`

```python
@dataclass(frozen=True)
class ResourceLimits:
    cpu_millicores: int
    memory_bytes: int
    pids_max: int
    workspace_bytes: int
    inodes_max: int
    wall_clock_seconds: int
```

Every field is required and none has a default at this layer. Defaults
belong to configuration, and a limit that defaults to unlimited when a
field is forgotten is the failure mode this shape exists to prevent.
The numbers are in the limits section below.

### `EnvironmentHandle`

```python
@dataclass(frozen=True)
class EnvironmentHandle:
    environment_id: str
    tenant_id: TenantId
    run_id: RunId
    lease_epoch: int
    created_at: datetime
    expires_at: datetime
```

No path, no container id, no host, no socket. `environment_id` is
opaque and is the only thing the worker may use to name a sandbox.
The handle carries `tenant_id` so that `execute` can assert the
sandbox it was handed belongs to the tenant the call is for, which
turns a class of adapter bug into an error instead of a cross-tenant
read.

The workspace is mounted at `/workspace` inside every sandbox. It is a
constant, not a field, because a configurable mount point is a value
that has to travel and there is nothing to gain by letting it vary.

`expires_at` is `created_at` plus the service's hard cap, and the
service enforces it whether or not anyone reads it. A sandbox that
reaches `expires_at` is destroyed with `KillReason.EXPIRED`
regardless of what it is doing.

### `ExecutionCommand`

```python
@dataclass(frozen=True)
class ExecutionCommand:
    argv: Sequence[str]
    working_directory: PurePosixPath
    timeout_seconds: int
    stdin: bytes | None
    maximum_output_bytes: int
```

`argv` is a vector, per Section 18.3. The vector may name a shell —
`["bash", "-lc", "pytest -q && echo ok"]` is a legal command — and
that is not a contradiction of the rule. What Section 18.3 forbids is
an API that takes a string and picks a shell for it, because then the
quoting is done by the platform on behalf of a caller who did not
know it was being done. When the shell is in the vector, the intent
is visible in the recorded invocation and the escaping is the
caller's.

`argv[0]` is resolved against a fixed `PATH` baked into the image. It
is not resolved against the workspace: a command must be named
absolutely or found on the image `PATH`, so that a file dropped in
the workspace cannot shadow `python`.

`working_directory` is relative to `/workspace` and is resolved by the
containment function before the call is made, not by the sandbox
afterwards. A path that escapes fails validation with
`ToolValidationError` and never reaches the execution service.

`maximum_output_bytes` is enforced twice: here, by the execution
service, which stops reading and kills the process, and again by the
tool pipeline, which applies the ceiling and the artifactization rule
[tool-system.md](tool-system.md) specifies. The two are not
redundant. The pipeline's enforcement protects the context window;
this one protects the host from a process writing to a pipe faster
than anything drains it.

### `ExecutionResult`

```python
@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    killed_by: KillReason | None
    files_changed: Sequence[FileChange]
    duration_ms: int
```

`exit_code` is `None` exactly when the process did not exit on its
own, which is when `killed_by` is set. Section 18.3's sample result
shows `exit_code` and `timed_out`; both are kept with their names and
meanings, and `killed_by` refines `timed_out` rather than replacing
it — a timeout sets both, and the other kill reasons set `killed_by`
with `timed_out` false.

```python
class KillReason(StrEnum):
    TIMEOUT = "TIMEOUT"
    MEMORY = "MEMORY"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    PIDS = "PIDS"
    DISK = "DISK"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    SERVICE_SHUTDOWN = "SERVICE_SHUTDOWN"
```

The vocabulary is closed and every value is a thing the execution
service can distinguish. It matters more than it looks: a model that
sees "the command failed" retries it, and a model that sees
`MEMORY` reduces the batch size. The reason is surfaced in the tool
result, and `OUTPUT_LIMIT` and `MEMORY` are the two that most often
change what the model does next.

```python
@dataclass(frozen=True)
class FileChange:
    path: PurePosixPath
    change: ChangeKind
    size_bytes: int
    sha256: str | None


class ChangeKind(StrEnum):
    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
```

`files_changed` is computed by the execution service by comparing the
workspace against the snapshot it took before the command ran, not by
the command and not by the tool. A command that reports its own
changes is a command that can lie about them, and the interesting
case — a script that wrote a file it did not mention — is exactly the
one self-reporting misses.

`sha256` is `None` for a deletion and set otherwise. `path` is
relative to `/workspace` and never absolute, which keeps the host
path out of the result as well as out of the handle.

The listing is bounded: `files_changed` is capped at a configured
count, default 1000, and a command that changes more sets a
`files_changed_truncated` flag on the tool result rather than
returning a listing nobody reads. The cap is on the listing, not on
the workspace; the workspace cap is `inodes_max`.

### The three methods and what may fail

`provision` is the only method that may block for a noticeable time,
and the number that matters is sandbox start latency, which is the
difference between a microVM and a container that most affects the
feel of the product. It is a tracked metric below.

`execute` is called at most once per `sandbox.run_command` and may be
called many times against one handle within a lease. It is not
parallel-safe against a single handle and the builtin declares
`allow_parallel no`, which is where that constraint is enforced.

`destroy` is idempotent, returns `None`, and never raises for a
handle that is already gone. A destroy that fails loudly on a
sandbox the reaper already collected turns cleanup into a source of
errors, and cleanup that logs errors is cleanup that gets muted.

Three failure classes cross the port, and they join the taxonomy in
Section 13 rather than forming a new one:

1. `ExecutionUnavailable` — the service is unreachable, out of
   capacity, or shutting down. Retriable, and the run's retry policy
   applies unchanged.
2. `ExecutionRejected` — the specification is invalid: an unknown
   image digest, a limit above the operator's ceiling, an egress
   policy naming a host the operator did not configure. Not
   retriable; it is a bug in the caller.
3. `SandboxEscapeSuspected` — an invariant the service checks was
   violated: a workspace whose device or inode changed, a handle
   whose tenant does not match, a process surviving destroy. Not
   retriable, the sandbox is destroyed, and it is the one failure
   that raises an operational alert rather than only a log line.

## The workspace is a cache, not state

This is the architectural decision in the document and everything
about crash-resume follows from it.

A run's workspace exists for the life of a **worker's lease on that
run**, not for the run's logical lifetime. It is created lazily at
the first sandbox-targeted tool call, it lives as long as the lease
does, and it is destroyed with the sandbox when the lease ends —
whether the lease ends by completion, by cancellation, by a hold long
enough to release it, or by the worker dying. A run that resumes on
another worker gets a fresh, empty workspace.

Anything that must survive is an artifact. That is the whole rule.

The alternative — a durable per-run workspace that follows a run
across workers — was considered and rejected, and it is worth saying
why, because it is the design most systems reach for. It requires
shared storage between execution hosts, which reintroduces a
cross-tenant blast radius the topology just removed; it makes the
workspace a piece of state whose consistency with the event log
nobody owns; and it makes crash-resume a recovery problem rather than
a restart. With the workspace as a cache, resume needs no recovery at
all: the run's durable state is the event log, and the event log
already replays.

The lifecycle, precisely:

1. **Created** on the first tool call whose `ExecutionTarget.kind` is
   `sandbox`. A run that never executes code never provisions a
   sandbox and never pays for one. This is why `provision` is
   separate from `execute`.
2. **Held** across steps within a lease, so a run that writes a file
   in one step reads it in the next. This is the common case and it
   works.
3. **Held across a short hold.** A run waiting for an approval keeps
   its sandbox for up to `approval_hold_seconds`, which
   [tool-system.md](tool-system.md) sets at 300 for the orchestration
   bridge and which is the same number here. The wall clock keeps
   running against `expires_at` during a hold; a hold does not extend
   a sandbox's life.
4. **Released** when the hold exceeds that, when the run reaches a
   terminal state, or when the lease is lost. The sandbox is
   destroyed and the workspace goes with it.
5. **Fresh on resume.** A run that resumes finds no workspace and
   provisions a new one on its next sandbox-targeted call.

Two consequences the implementer must carry into the product rather
than leaving in the design.

The first is that the model has to know. `sandbox.run_command`'s
description says the workspace does not survive an interruption and
that files worth keeping should be exported. This is not a nicety: a
model that assumes durability writes `results.csv`, waits for an
approval, resumes, and reads a file that is not there. Telling it
once in the tool description is cheaper than every recovery path that
follows from not telling it.

The second is that the orchestration bridge's re-execution rule and
this rule are the same rule seen from two sides.
[tool-system.md](tool-system.md) already says a script is re-executed
from the beginning after an approval past the hold, with dedup
returning recorded outcomes for calls already made. That design is
only correct if the workspace is disposable, and it is. The two
documents agree because they are describing one behaviour.

### `WorkspaceHandle`

`ToolExecutionContext.workspace` is `WorkspaceHandle | None`. It is
present for any tool declaring a `WORKSPACE_READ` or `WORKSPACE_WRITE`
side effect and for any tool whose execution target is the sandbox,
and `None` otherwise. Execution target and workspace access are
independent: the three `workspace.` tools and `artifact.export` are
`in_process` and hold a handle, and a tool can hold a handle without
being able to run anything.

The handle is not a filesystem. It is a narrow port over the run's
workspace volume, implemented by the execution service, which owns
the volume and can read it without entering the sandbox.

```python
class WorkspaceProvenance(StrEnum):
    UNKNOWN = "unknown"
    TOOL_WRITTEN = "tool_written"
    SANDBOX_WRITTEN = "sandbox_written"


class WorkspaceHandle(Protocol):
    @property
    def root(self) -> PurePosixPath:
        ...

    def resolve(self, path: str | PurePosixPath) -> PurePosixPath:
        ...

    async def read(self, path: str) -> bytes:
        ...

    async def write(self, path: str, data: bytes) -> None:
        ...

    async def listdir(self, path: str) -> Sequence[FileChange]:
        ...

    async def provenance(self, path: str) -> WorkspaceProvenance:
        ...
```

`root` is `/workspace`, always. `resolve` is the containment function
and it is the only way a path is turned into a path anywhere in the
system:

1. Reject any component that is empty, `.`, or `..` before joining.
   Rejecting the component is different from normalizing it, and
   normalizing is where traversal bugs come from.
2. Reject absolute inputs and inputs containing a NUL byte.
3. Join under `root` and require the result to be under `root` after
   resolution, so a symlink pointing outward fails even though every
   component was legal.
4. Reject any component longer than 255 bytes and any result longer
   than 4096, so a path that would fail at the syscall fails at the
   boundary with a message.
5. On any rejection raise `WorkspaceEscape`, which the pipeline maps
   to `ToolValidationError` and which never reaches the tool.

Rule 3 is the one that needs the symlink check rather than a string
prefix comparison. `resolve` is called on the execution service's
side of the boundary for anything that touches the real filesystem,
because a check performed on the worker against a path the sandbox
can change between check and use is a check with a race in it.

The property gate below tests `resolve` against generated inputs
rather than a list of known-bad strings, because the list is always
missing one.

### Provenance, and why the handle answers it

`provenance` reports which of three things put a file where it is.
It exists because [builtin-tools.md](builtin-tools.md) needs the
answer to decide whether `workspace.read_text` hands its bytes back
labelled `INTERNAL_TOOL` or `EXTERNAL_UNTRUSTED`, and the decision
has to be made from something more durable than the tool's own
memory of the current turn.

The record is written by `write`, in the same operation that writes
the bytes, and by nothing else. That is the entire mechanism; the
three properties below follow from it rather than from rules stated
alongside it.

`write` records `TOOL_WRITTEN`. A tool ran, the call came from the
platform's own code through the execution pipeline, and a run
reading back what it wrote is reading its own working memory.

The Milestone 6 adapter records `SANDBOX_WRITTEN` for anything a
container leaves behind. The value is defined at Milestone 4, before
anything can produce it, precisely so that the Milestone 6
implementer inherits it rather than deciding it. Bytes produced by
code we did not write are the case `EXTERNAL_UNTRUSTED` exists for,
and passing through this port does not launder them.

Everything else is `UNKNOWN` — a file a fixture placed, a file an
archive extracted, a file whose writer predates the record.
`UNKNOWN` is also what the port returns for a path it has never
seen, so a missing record and an untrusted file give the same
answer and a lost record fails in the safe direction.

The record lives with the volume and dies with it. There is no table
and no migration: a workspace that is gone has no provenance to
report, and a read against it fails before anyone asks. Keeping the
knowledge here rather than in a repository is also what lets
`ToolExecutionContext` go on carrying no database session, which
[tool-system.md](tool-system.md) is deliberate about.

`listdir` does not carry provenance on its entries. A caller that
needs it asks per path, which is cheap: the record is a map the
execution service holds beside the volume, so a thousand-entry
listing costs a thousand lookups and no syscalls.

Both adapters — the Milestone 4 local directory and the Milestone 6
volume — run one contract suite over `resolve` and `provenance`
together, for the same reason the containment rule is written once.

### `ArtifactWriter`

```python
class ArtifactWriter(Protocol):
    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
    ) -> ArtifactRef:
        ...
```

Deliberately narrower than `ArtifactStore`. A tool supplies bytes, a
suggested filename, a media type, and a trust label. It does not
supply the tenant, the run, the size, the checksum, or the storage
key — the pipeline supplies those from the execution context, which
is why a tool cannot write an artifact into another run even if it
tries. `ArtifactStore` is the port the adapter implements;
`ArtifactWriter` is the capability a tool is handed, and the gap
between them is the authorization.

## The environment a sandbox sees, and the three tiers

Section 22 requires "tiered credential scrubbing for child runs and
subprocesses, with env passthrough that fails closed on platform and
provider credentials", and the milestone table places it at Milestone
6 with the note "env passthrough happens at the sandbox". This is
that design.

The environment handed to a sandbox is **built, not filtered**. It
starts empty. Nothing from the worker's own environment is present
unless something put it there deliberately. That inversion is the
whole mechanism, and the tiers describe what may put things there:

**Tier 0 — never present, and not configurable.** Platform and
provider credentials. The database URL, the object store credentials,
every model provider key, the API auth token, cloud instance
credentials, and the service credential the worker uses to reach the
execution service. Because the environment is built rather than
filtered, tier 0 needs no blocking code to be empty. The named list
exists anyway, and it exists for one purpose: a test asserts that no
tier-0 name appears in a constructed environment under any
configuration, so that a future change from build to filter fails
loudly instead of quietly.

**Tier 1 — present only when an operator names it.** A deployment
that needs a proxy variable, a locale, or a tenant-neutral
configuration value lists the names in configuration. The list is
operator-only: not tenant-settable in 0.1, and never model-settable.
A name that also appears in tier 0 is a configuration error at
startup, not a silent drop, because a silent drop is a deployment
that thinks it passed something it did not.

**Tier 2 — synthesized by the platform.** `HOME=/workspace`,
`PWD`, a fixed `PATH`, `TMPDIR=/tmp`, `LANG=C.UTF-8`, and — for a
turn that runs programmatic orchestration — the bridge socket path
and its one-time token. Nothing here comes from the host.

Fail-closed has a precise meaning: if the tier-1 list cannot be read
or cannot be parsed, the environment is tier 2 alone and the failure
is logged as a configuration error. It does not fall back to the
parent environment, and it does not fail the run. A sandbox with a
missing optional variable is a degraded sandbox; a sandbox with the
worker's environment is a breach.

`CredentialResolver` completes the picture from the other side.

```python
class CredentialResolver(Protocol):
    async def resolve(
        self,
        reference: CredentialRef,
    ) -> SecretValue:
        ...
```

Section 22's rule is that a tool needing a credential obtains it from
a broker during execution and the model receives only a reference.
The resolver is that broker's tool-facing interface, and it lives
strictly on the worker's side. A tool whose execution target is the
sandbox is handed a resolver that raises `CredentialUnavailable` for
every reference, because a credential resolved inside a sandbox is a
credential inside a sandbox no matter how it got there.

`SecretValue` is an opaque wrapper whose `__str__` and `__repr__`
return a redaction marker and which does not survive JSON
serialization. It is not a security boundary — anything holding it
can call `.reveal()` — it is a way to make the accidental case, a
secret in a log line or an exception message, structurally hard.

## Resource limits, as numbers

Section 28.4 lists the categories. These are the defaults, and the
point of writing them down is that a limit whose value is "configure
it" is a limit that ships unset.

```text
limit                  default    ceiling     enforced by
cpu_millicores         2000       operator    cgroup cpu.max
memory_bytes           2 GiB      operator    cgroup memory.max
pids_max               512        4096        cgroup pids.max
workspace_bytes        4 GiB      operator    filesystem quota
inodes_max             100000     1000000     filesystem quota
wall_clock_seconds     300        1800        execution service
files_changed listing  1000       10000       execution service
sandbox start budget   30 s       60 s        execution service
```

Four notes on that table.

`memory_bytes` is enforced with the OOM killer rather than by
refusing an allocation, so the observable behaviour is a killed
process with `killed_by = MEMORY` and whatever it had written to
stdout up to that point. The partial output is returned rather than
discarded, because the last thing a process printed before it died is
usually why it died.

`workspace_bytes` and `inodes_max` are both required. A quota on
bytes alone is defeated by a million empty files, which exhausts the
host's inode table without approaching the byte limit, and the
failure lands on every other tenant on that host.

`wall_clock_seconds` at 300 matches `sandbox.run_command`'s declared
`timeout_s` in [builtin-tools.md](builtin-tools.md), and the ceiling
at 1800 exists so that a long-running build in a task run can be
configured up without editing code. Both are enforced by the service.

`sandbox start budget` bounds `provision`. A provision that exceeds
it raises `ExecutionUnavailable` rather than hanging, which keeps a
capacity problem in the execution tier from looking like a slow model
call to everything upstream.

### Timeouts compose by minimum, and the model is the weakest input

Four numbers can bound one command and there is exactly one rule:

```text
effective_timeout = min(
    execution service hard cap,        # operator, always applies
    spec.limits.wall_clock_seconds,    # per-environment
    remaining run budget,              # from the run's deadline
    command.timeout_seconds,           # from the model, clamped
)
```

The model's `timeout_seconds` — the one Section 18.3 puts in the tool
request — is clamped to the tool's declared `timeout_s` before it
becomes `command.timeout_seconds`, and it can only ever lower the
effective value. A model asking for 3600 seconds gets 300. A model
asking for 5 gets 5, which is a useful thing for it to be able to do
and is why the field is accepted at all.

The service's hard cap applies whether or not any of the other three
were supplied. This is the layering rule Section 28.4 asks for when
it says the timeout is "enforced by the execution service (not by the
model-provided `timeout_seconds` alone)": a caller that forgets to
pass a timeout still gets a bounded command, because the enforcement
does not depend on the caller.

`remaining run budget` connects the sandbox to the run deadline
[runtime-loop.md](runtime-loop.md) owns. A command is not started if
the remaining budget is under a configured floor, default 5 seconds,
because starting a command that is certain to be killed wastes the
sandbox start and produces a confusing result.

Cancellation is cooperative above the port and immediate below it.
The runtime loop's `CancellationToken` fires, `execute` is cancelled,
and the execution service kills the process group with
`killed_by = CANCELLED`. The partial output is returned, because a
cancelled command's output is evidence.

## The egress boundary

This is the mechanism [tool-system.md](tool-system.md) already
depends on by name. It is one policy with two enforcement points, and
reading it as a sandbox-only feature is the mistake the tool system's
sentence is warning against.

The two points:

1. **The sandbox proxy.** A sandbox has no route to any address
   except the proxy. Nothing else is reachable at the network
   namespace, so a process that ignores `HTTP_PROXY` reaches nothing
   rather than reaching the internet.
2. **The worker's outbound guard.** Any URL the platform dials on
   behalf of a tenant — an HTTP MCP server the tenant configured, a
   webhook, a fetch — is checked against the same policy before the
   connection is made. This is the point that makes a tenant-supplied
   URL safe, and it is on the worker because that is where the URL
   is.

One policy object, evaluated by one function, called from two places.
A second implementation on the worker side is how the two drift, and
the drift is only ever discovered by the request that should have
been refused.

### The allowlist grammar

```yaml
egress:
  mode: deny            # deny | allowlist
  destinations:
    - host: pypi.org
      ports: [443]
    - host: "*.pythonhosted.org"
      ports: [443]
    - host: api.example.com
      ports: [443]
```

The rules:

1. `mode: deny` is the default and denies everything. `allowlist`
   permits exactly the destinations listed. There is no `allow` mode,
   because an open egress mode is a configuration typo away from
   being selected and there is no deployment that needs it.
2. A wildcard is one leftmost label and nothing else. `*.example.com`
   matches `a.example.com`; it does not match `example.com`, and it
   does not match `a.b.example.com`. Suffix matching without label
   boundaries matches `evilexample.com`, which is the classic bug in
   this grammar.
3. `ports` is required and explicit. There is no default port,
   because the default would be 443 and the destination that matters
   is the one on 8080 nobody wrote down.
4. There is no scheme. The proxy sees `CONNECT host:port` and the
   tunnel is opaque after that, so a scheme in the grammar would be a
   field that looks enforced and is not.
5. There is no IP-address destination form. An allowlist entry is a
   name. Permitting a literal address invites entries that bypass the
   private-range check below by writing the address the check exists
   to catch.
6. The policy is operator configuration. It is not tenant-settable in
   0.1 and it is never model-settable. A model that could add a
   destination could exfiltrate to it, which makes the allowlist a
   decoration.

### What the proxy does on `CONNECT`

1. Match the requested name against the destination list. A miss is
   refused with a 403 and logged.
2. Resolve the name at the proxy, using the platform's resolver. The
   sandbox has no resolver reachable and never performs DNS itself,
   which is what stops DNS from being an exfiltration channel of its
   own.
3. Check every resolved address against the address denylist below.
   One bad address refuses the whole connection; a name resolving to
   both a public and a private address is refused, not partially
   allowed.
4. Dial **the address that was resolved**, not the name. One
   resolution, one dial. Re-resolving between the check and the
   connection is DNS rebinding, and it is the reason this is written
   as a sequence rather than a set of properties.
5. Log the decision with run, tenant, host, port, resolved address,
   and outcome. Refusals carry a reason. This log is how an operator
   answers "what did that run talk to", and it is the only place the
   answer exists.

The address denylist is not configurable and applies in both modes:

```text
0.0.0.0/8          this network
10.0.0.0/8         private
127.0.0.0/8        loopback
169.254.0.0/16     link-local, incl. 169.254.169.254 metadata
172.16.0.0/12      private
192.168.0.0/16     private
100.64.0.0/10      carrier-grade NAT
::1/128            loopback
fc00::/7           unique local
fe80::/10          link-local
::ffff:0:0/96      IPv4-mapped, checked as its IPv4 address
```

`169.254.169.254` is inside the link-local range and is called out
because it is the single most valuable destination on the list. On
most cloud providers it hands out instance credentials to anything
that asks, and an allowlisted egress that forgets it has allowlisted
the cloud IAM role of the host running untrusted code.

IPv4-mapped IPv6 addresses are unwrapped and checked as IPv4. A check
that looks at `::ffff:169.254.169.254` as an IPv6 address and finds
it outside `fe80::/10` is a check that passes the thing it exists to
stop.

The proxy is not reconfigurable from inside a sandbox. It has no
admin interface on the sandbox-facing interface, it reads its
configuration from the execution service at start, and a policy
change takes effect for sandboxes provisioned after it. A running
sandbox keeps the policy it was provisioned with, which is both
simpler and more auditable than reconfiguring live tunnels.

### Egress needs two independent yeses

Network access is off unless both of these are true, and they are
independent on purpose:

1. The operator configured `mode: allowlist` with at least one
   destination. This is deployment configuration and no run can
   change it.
2. The tool call's `ExecutionTarget.network_enabled` is true, which
   requires the `SANDBOX_NETWORK` side-effect class to resolve to
   `ALLOW` — and [policy-and-approvals.md](policy-and-approvals.md)
   puts `SANDBOX_NETWORK` at `DENY` in the `default` profile, so in
   the default deployment this is an approval.

An approved `SANDBOX_NETWORK` grants the allowlist, not the internet.
That sentence is the one to read twice. The approval widens the
policy by zero destinations; it decides only whether this call may
use the destinations an operator already chose. A user approving
"allow network access" in a UI is not approving arbitrary egress, and
the approval prompt should say which destinations are on the list,
because an approval whose scope the approver cannot see is not
informed consent.

`PACKAGE_INSTALL` is a separate side-effect class and stays `DENY`
in the default profile. Installing a package is not the same decision
as reaching a host, even though one usually needs the other, and
collapsing them makes the narrower approval impossible to grant.

## `sandbox.run_command`

Section 18.3 fixes the request and result field names and
[builtin-tools.md](builtin-tools.md) fixes the classification. Neither
is changed here. What is added is the validation, the resolution, and
what the model is told.

The request, unchanged:

```json
{
  "command": ["python", "script.py"],
  "working_directory": ".",
  "timeout_seconds": 30
}
```

Validation, in order, all of it before the execution service is
touched:

1. `command` is a non-empty array of strings. Not a string. An
   argument vector arriving as a string is `ToolValidationError`
   with a message naming the vector form, because that error is a
   model-authoring problem and a message that teaches the shape is
   worth more than one that reports the type.
2. Every element is a string, contains no NUL byte, and the whole
   vector is under a size cap, default 64 KiB.
3. `command[0]` is either absolute or a bare name. A relative path
   containing a separator is rejected, so that `./setup.sh` is an
   explicit choice made by writing `["bash", "./setup.sh"]` rather
   than something the resolver does implicitly.
4. `working_directory` passes `WorkspaceHandle.resolve`. A traversal
   fails here, before provision, and is one of the paths harness case
   19 exercises.
5. `timeout_seconds` is a positive integer, clamped to the tool's
   declared `timeout_s` of 300 and then folded into the minimum rule
   above.

The result, unchanged in its field names and extended with the ones
`ExecutionResult` carries:

```json
{
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "timed_out": false,
  "killed_by": null,
  "duration_ms": 1840,
  "files_changed": [
    {
      "path": "output.csv",
      "change": "CREATED",
      "size_bytes": 1204,
      "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b..."
    }
  ]
}
```

The classification stays exactly as [builtin-tools.md](builtin-tools.md)
records it — `CODE_EXECUTION`, `HIGH`, `NON_IDEMPOTENT`,
`output_trust` forced to `EXTERNAL_UNTRUSTED`, scope
`sandbox.execute`, `timeout_s` 300, `max_output_bytes` 1048576,
`allow_parallel` no — and this document changes one thing about the
tool, its milestone, from 5 to 6. That is the off-by-one correction
recorded under contradictions.

`allow_parallel no` is worth one sentence of reinforcement now that
the port exists: `execute` is not parallel-safe against one handle,
and two commands sharing a workspace produce a `files_changed`
listing that attributes changes to whichever call finished second.

What the model is told, in the tool description, is short and covers
three things it cannot infer:

1. The command runs with no network unless network access has been
   enabled and approved for this call, and the error when it has not
   is a connection failure rather than a permission message.
2. The workspace does not survive an interruption. Files that matter
   should be exported with `artifact.export`.
3. Output above the limit is truncated to a head-and-tail excerpt and
   the full output becomes an artifact whose identifier is in the
   result. The model does not need to re-run the command to see the
   rest.

## Artifacts

Section 18.4 fixes four rules: store outside the source tree, use
opaque identifiers, checksum with SHA-256, keep metadata in
PostgreSQL. This section supplies the types and the key derivation.

### `ArtifactMetadata` and `ArtifactRef`

```python
@dataclass(frozen=True)
class ArtifactMetadata:
    tenant_id: TenantId
    run_id: RunId
    origin: ArtifactOrigin
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    trust: TrustLevel
    created_at: datetime
    expires_at: datetime | None


class ArtifactOrigin(StrEnum):
    SANDBOX_EXPORT = "SANDBOX_EXPORT"
    TOOL_OUTPUT = "TOOL_OUTPUT"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    UPLOAD = "UPLOAD"
    TRAJECTORY_EXPORT = "TRAJECTORY_EXPORT"


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: ArtifactId
    sha256: str
    size_bytes: int
    media_type: str
```

`ArtifactRef` is what travels: into a tool result, into an event
payload, into an API response. It carries no filename, no tenant, and
no path, so a reference that leaks into a context window leaks a
random identifier and three facts about bytes. `ArtifactMetadata` is
what the store holds and what `GET /v1/artifacts/{id}` reads after
authorization, which ADR-0028 already specifies.

`origin` exists because the five sources have different review
properties and an operator asking "what did this run produce" wants
them separated. `TOOL_OUTPUT` is the truncation path
[tool-system.md](tool-system.md) owns; `SANDBOX_EXPORT` is
`artifact.export`; `UPLOAD` is a client-supplied file;
`TRAJECTORY_EXPORT` is the redacted, consent-gated run export
[event-log-and-persistence.md](event-log-and-persistence.md) owns,
and it is the one origin whose contents are a function of the whole
run rather than of a single act inside it.

`trust` is inherited, never assigned by the producer. An artifact
written from a sandbox is `EXTERNAL_UNTRUSTED` because Section 28.5
says sandbox output is, and fetching it back returns it inside its
envelope with its label intact — the guarantee
[tool-system.md](tool-system.md) already makes for elided untrusted
spans, applied to the same bytes arriving by a different route.

`filename` is metadata and never a path. It is what a download is
named and it is sanitized on the way out, not on the way in, because
sanitizing on the way in loses the original and the original is
occasionally the evidence.

### The storage key is derived, never composed

```text
storage_key = f"{tenant_id}/{artifact_id[:2]}/{artifact_id}"
```

Two inputs, both platform-generated, neither caller-supplied.
`artifact_id` is a UUIDv7, which gives the key a time prefix that
makes listing and lifecycle scans cheap without making the identifier
guessable in any useful way. The two-character shard exists so that a
filesystem adapter does not put a million entries in one directory.

No component of the key comes from `filename`, from `media_type`, or
from anything a tool passed in. A filename of `../../etc/passwd` is
stored as metadata, is escaped on the way out, and has no effect on
where the bytes go. This is a structural gate below rather than a
test with adversarial filenames, because the property worth asserting
is about the function's inputs and a list of bad filenames is always
missing one.

### `ArtifactStore`

Section 7's two methods are unchanged:

```python
class ArtifactStore(Protocol):
    async def put(
        self,
        stream: AsyncIterator[bytes],
        metadata: ArtifactMetadata,
    ) -> ArtifactRef:
        ...

    async def open(self, ref: ArtifactRef) -> AsyncIterator[bytes]:
        ...
```

`put` streams. It never loads the object into memory, it computes
SHA-256 while streaming, and it compares the computed digest against
`metadata.sha256` before committing. A mismatch raises
`ArtifactIntegrityError` and writes nothing, which turns a truncated
transfer into a failure rather than a corrupt artifact nobody
notices until it is read.

Both the execution service and the store compute the digest — the
service over the bytes it read from the workspace, the store over the
bytes it received — and the comparison is what makes the hop between
them verifiable. Section 28.4's "verify exported artifacts by
SHA-256" is this comparison.

`put` also enforces the size cap it was given, and a stream exceeding
it is abandoned mid-write with the partial object deleted. The cap is
per-artifact configuration, default 512 MiB.

`open` returns a stream and the caller authorizes before calling it.
The store is not an authorization boundary: it holds bytes by key and
the key is derived from a tenant, so a caller passing a ref it should
not have gets the bytes. Authorization lives in the application
service, which ADR-0028 puts before both metadata and content, and
this is stated plainly here so nobody adds a second check in the
store and assumes the first one is the store's job.

Milestone 6 ships the filesystem adapter. The port is shaped for an
object store — streaming both ways, opaque keys, no directory
semantics — so that the S3-shaped adapter is an adapter and not a
redesign.

### Retention

Artifacts have a default retention of 30 days and a per-tenant byte
cap, both operator configuration. `expires_at` is written at creation
and a sweeper deletes expired objects and their metadata rows.

The corpus does not currently bound artifacts anywhere, which
[http-api-and-streaming.md](http-api-and-streaming.md) notices from
the other direction when it asks whether events have a retention
policy. Thirty days is a starting number rather than a researched
one; it is recorded as an open question below, because unlike most
defaults this one silently deletes something a user might expect to
keep.

Deletion is by `expires_at` and never by "the run is finished",
because a finished run's artifacts are exactly what a user comes back
for. An artifact referenced by a memory record or an event payload is
still deleted on schedule; the reference remains and resolves to a
`404`, which is honest, and the alternative — reference counting
across the event log — is a distributed garbage collector nobody
asked for.

## Nothing outlives its lease

A sandbox is created by a worker holding a lease and must not survive
that lease. Three mechanisms enforce it and all three are needed,
because the interesting failure is the worker that dies without
running any cleanup code at all.

1. **The worker destroys what it provisioned**, in a finally block,
   on every path out of the run — completion, failure, cancellation,
   or a hold long enough to release the lease. This handles the
   ordinary case and handles it immediately.
2. **The service expires what it started.** Every sandbox has
   `expires_at` and the service destroys it on reaching that time
   regardless of what the worker is doing. This handles the worker
   that hangs.
3. **A reaper sweeps by lease epoch.** On an interval, default 60
   seconds, the execution service lists live sandboxes and asks the
   worker tier which `(run_id, lease_epoch)` pairs are current. A
   sandbox whose epoch is stale — the run was re-queued and claimed
   by another worker — is destroyed. This handles the worker that
   died, and it is why `lease_epoch` is on `EnvironmentSpec`.

The reaper's query is the only call the execution service makes back
toward the worker tier, and it carries no tenant data in either
direction: a list of opaque environment ids out, a set of stale ones
back. It is deliberately not a database read, because the execution
service has no database access and giving it one to support cleanup
would trade the property the topology exists for.

Destruction means the sandbox and the workspace volume, in that
order, with the volume erased rather than unlinked where the storage
layer distinguishes the two. Section 28.4's "wipe workspace after
export" is this step, and the ordering matters: destroying the
sandbox first means nothing is writing to the volume while it is
being erased.

A destroy that fails is retried on the reaper's next pass and raises
an operational alert after a configured number of failures. A
sandbox that cannot be destroyed is the one condition in this
document that should page someone.

## Development, production, and the refusal in between

Section 28.6 and
[bootstrap-and-composition.md](bootstrap-and-composition.md) already
settle the shape: a `SandboxMechanism` setting with three values, and
startup check 4 asserting that `deployment_mode == "production"`
implies `sandbox != "docker"`.

```text
mechanism   isolation            allowed in production
microvm     kernel per sandbox   yes
gvisor      syscall interception yes
docker      shared kernel        no, startup refuses
fake        none, in-process     no, startup refuses
```

`fake` is added here and it is the fourth value the setting needs.
It is not a test double bolted on beside the port; it is a production
adapter in the sense [engineering-plan.md](engineering-plan.md) uses
for the in-memory repositories — a real implementation of the port
that runs the contract suite unchanged. It executes nothing. It
records the commands it was given, returns scripted results, and
models the workspace as a dictionary.

That is what makes the deterministic tests possible. A test asserting
that a traversal is rejected, that a timeout produces
`killed_by = TIMEOUT`, or that `files_changed` reaches the model
correctly does not need a kernel; it needs a port that behaves. The
tests that do need a kernel are the security tests, and they are
listed as gates below with the runtime they require.

Both refusals are the same refusal and it is one startup check with
two conditions:

```text
if deployment_mode == "production":
    require sandbox in {microvm, gvisor}
if auth_mode != "dev":
    require sandbox in {microvm, gvisor}
```

The second condition is Section 28.6's sentence — "startup must
refuse to run untrusted code under the development fallback when
AUTH_MODE is not dev" — and it exists separately because the two
settings are set in different places and the failure being guarded
against is a staging deployment with real tokens and a development
sandbox.

The refusal is a startup failure with a message naming both settings
and their values. It is not a warning. A warning at startup is a
line in a log that a deployment scrolls past for a year.

### One contract suite, four adapters

[evaluation-harness.md](evaluation-harness.md) attaches contract
suites to ports rather than implementations and names "each sandbox
backend" as a case where the same suite runs against several. The
`ExecutionEnvironment` suite runs against `fake` in every test run,
against `docker` in the local development target, and against
`gvisor` and `microvm` in the environments that have them.

The suite asserts the port's semantics and nothing about isolation:
that `destroy` is idempotent, that a timeout sets both `timed_out`
and `killed_by`, that `files_changed` reports a created file and a
deleted one, that `execute` against a destroyed handle raises, that
a mismatched tenant on a handle raises, that `stdin` reaches the
process, and that output above the cap sets the truncation flag. A
`fake` that passes it and a `microvm` that passes it are
interchangeable from the runtime's point of view, which is the
property that lets the whole system be tested without a hypervisor.

Isolation is not in the contract suite because isolation is not a
port semantic — it is a property of a deployment, and asserting it
requires the real runtime. Those assertions are the security gates.

## Milestones

Almost everything here is Milestone 6, which is what Section 21
already says. The table exists for the three items that are not.

```text
# capability                                milestone
SandboxMechanism setting, startup refusal   M1
WorkspaceHandle, containment, provenance   M4
ExecutionEnvironment port and its types     M6
fake adapter and the contract suite         M6
kernel-isolating adapter, gvisor or microVM M6
ResourceLimits and their enforcement        M6
timeout layering, the minimum rule          M6
environment tiers, fail-closed construction M6
CredentialResolver, SecretValue             M6
EgressPolicy, grammar, proxy, denylist      M6
worker-side egress guard                    M6
sandbox.run_command                         M6
workspace lifecycle, lease binding, reaper  M6
ArtifactStore and the filesystem adapter    M6
ArtifactWriter and artifact.export          M6
artifact retention sweeper                  M6
tenant-configured MCP servers use the guard M8
object-store artifact adapter               deferred
per-tenant artifact byte accounting         deferred
```

The setting and its startup check are Milestone 1 because
[bootstrap-and-composition.md](bootstrap-and-composition.md) already
places them there, with the whole `Settings` object and the four
startup checks. It is the same shape as the `idempotency_keys` table
in [http-api-and-streaming.md](http-api-and-streaming.md) — the
guard exists before the thing it guards, which is the only ordering
that is ever safe.

`WorkspaceHandle` is Milestone 4 because the three `workspace.` tools
are, and at Milestone 4 it is implemented over a plain local
directory: no sandbox exists yet and none is needed for the port to
be real. Milestone 6 supplies a second adapter over the execution
service's volume. The containment rule and its property test are
written once, at Milestone 4, and both adapters use them. The
provenance record ships with it, able to hold only `TOOL_WRITTEN`
and `UNKNOWN` until there is a sandbox to produce the third value.

The worker-side egress guard ships at Milestone 6 with the policy it
evaluates, even though its first caller — tenant-configured MCP
servers — arrives at Milestone 8. Building the guard when the policy
is designed rather than when the caller appears is what stops the
Milestone 8 implementer from writing a second one.

## Contradictions resolved

```text
# conflict                            resolution
1 sandbox.run_command at Milestone 5  M6; the sandbox milestone is 6
2 Milestone 6 had zero gates          thirteen, new `sandbox` area
3 red-team test with no harness case  case 26, security category
4 egress allowlist named, undefined   grammar and proxy, here
5 eight types used, never declared    declared here
6 "no host mounts" and a workspace    a volume is not a host mount
7 workspace durability unstated       lease-scoped; artifacts survive
```

Row 1 is the only one that edits another document, so it is worth
setting out in full.

Section 8.2 ends "Add `sandbox.run_command` only after the sandbox
milestone." [builtin-tools.md](builtin-tools.md) read "the sandbox
milestone" as Milestone 5 and said so twice — once in the roster's
milestone column and once in the reasoning for `artifact.export`.
Section 21 names Milestone 5 "HTTP API and SSE" and Milestone 6
"Isolated execution and artifacts", and Milestone 6's implement list
names `sandbox.run_command` in as many words. The tool is Milestone
6. The roster column and both sentences are corrected, and this is a
transcription error being fixed rather than a decision being made.

`artifact.export` stays at Milestone 6, and its reason is untouched:
Milestone 6 is where the model gains control tools and the
programmatic bridge, which is the first point at which the model
rather than the executor decides what leaves a run. What changes is
the argument against Milestone 5, which used to be "Milestone 5 is
the sandbox". Milestone 5 is the HTTP API, and that is a more
plausible home for an artifact tool rather than a less plausible one,
since Milestone 5 is where artifact metadata and content routes land.
It is still wrong, for a different reason: a route a client calls and
a tool the model calls are different surfaces with different
authorization stories, and the tool needs the workspace, which does
not exist until Milestone 6.

The concern that produced the original sentence survives, and this
document adopts it as a constraint rather than as a separation.
`sandbox.run_command` and `artifact.export` are now in the same
milestone, and they must not merge. `artifact.export` takes a
workspace path and produces an `ArtifactRef`; it does not take a
command result, it is `IDEMPOTENT` where the other is not, and it is
`in_process` where the other is `sandbox`. Exporting a file is not a
property of having run a command, and a convenience field on
`sandbox.run_command` that exports its outputs would make it one.

Row 6 is listed because an implementer reading Section 28.4's "no
host mounts" next to "a fresh read-write workspace per run" will ask
which one wins. Both hold. "No host mounts" forbids mounting a path
from the host filesystem into the sandbox — the source tree, `/var`,
a shared cache, the Docker socket. The workspace is a volume the
execution service creates for one sandbox, uses once, and destroys.
It is not a path on the host that exists before or after, and the
host path it happens to have is never named in any type this
document declares, which is what gate 3 asserts.

## Hard gates

Failing one of these blocks the milestone. They are registered in the
gate registry with identifiers, like every other gate, in a new
twelfth area, `sandbox`. Eleven are Milestone 6, the milestone that
had none; one is Milestone 1 and one is Milestone 4, because that is
where the code they check is written.

1. **Production refuses the development sandbox.** Startup with
   `deployment_mode = production` and `sandbox = docker` fails,
   and so does startup with `auth_mode != dev` and
   `sandbox = docker`. Both failures name both settings. A warning
   instead of a failure fails the gate.
   `gate.sandbox.production_refuses_dev`, structural. **M1.**
2. **The worker imports no container runtime.** The import-boundary
   walk is extended: no module under the runtime, tool, or worker
   packages may import a container or hypervisor client, directly or
   transitively, and none may spawn one as a subprocess. Only the
   execution-service adapter package may.
   `gate.sandbox.no_runtime_in_worker`, structural. **M6.**
3. **No host path crosses the port.** A structural check over the
   declared types asserts that `EnvironmentSpec`,
   `EnvironmentHandle`, `ExecutionResult`, and `FileChange` carry no
   field holding a host filesystem path, and that no adapter returns
   one in an error message. The workspace's host path exists in
   exactly one module.
   `gate.sandbox.spec_has_no_host_path`, structural. **M6.**
4. **No credential reaches a sandbox.** A run executes a command that
   dumps its entire environment, reads every file it can reach, and
   returns the lot. The result is asserted to contain no tier-0 name,
   no value from the worker's environment, and no string matching the
   secret-scanner patterns. Run with the worker's environment
   deliberately full of realistic-looking credentials, because an
   empty control environment proves nothing.
   `gate.sandbox.no_credential_reaches`, case. **M6.**
5. **Network is denied by default.** With no egress configuration, a
   command attempting TCP to a public address, a DNS query, and a
   request to `169.254.169.254` all fail, and they fail at the
   network namespace rather than at a proxy refusal — asserted by
   the absence of a proxy log line for the attempt.
   `gate.sandbox.network_denied`, case. **M6.**
6. **Egress reaches the allowlist and nothing else.** With
   `mode: allowlist` and one destination, a request to that
   destination succeeds and requests to a second public host, to a
   private address, to `169.254.169.254`, to a name resolving to a
   private address, and to the allowed host on an unlisted port all
   fail. Every outcome appears in the proxy log with its reason.
   `gate.sandbox.egress_allowlisted`, case. **M6.**
7. **Every limit is enforced and reported.** Five commands — a
   spin loop, an allocator, a fork bomb, a disk filler, and a
   file-count filler — are each killed, and each returns the
   matching `KillReason`. A command exceeding the effective timeout
   sets both `timed_out` and `killed_by = TIMEOUT`. Partial output
   is returned in every case.
   `gate.sandbox.limits_enforced`, case. **M6.**
8. **A container escape reaches nothing.** The red-team test. A
   command deliberately attempting the known escape paths for the
   configured runtime — the Docker socket, `/proc/self/exe`, a
   privileged mount, a host PID namespace, kernel modules — reaches
   no secret, no database, no other tenant's workspace, and no host
   process. This is harness case 26 and it runs against the real
   runtime, never against `fake`.
   `gate.sandbox.escape_denied`, case. **M6.**
9. **Workspaces do not see each other.** Two runs in two tenants
   execute concurrently. Neither can read, write, list, or stat the
   other's workspace by any path, including by inode, by
   `/proc/*/root`, and by a symlink planted before the second run
   started. Repeated with two runs in the same tenant, which must
   also be isolated.
   `gate.sandbox.workspace_isolated`, case. **M6.**
10. **Nothing outlives its lease.** A worker is killed mid-execution
    without cleanup. Every sandbox it created is destroyed within
    two reaper intervals, and no sandbox is live with a
    `lease_epoch` older than its run's current one. Repeated for a
    worker fenced by a supervisor rather than killed.
    `gate.sandbox.no_orphans`, case. **M6.**
11. **An artifact's bytes are the bytes that were exported.** A file
    exported from a workspace has the same SHA-256 when fetched
    through the API as it had in the workspace, byte for byte, for a
    file larger than the streaming buffer. A digest mismatch
    injected at the store boundary raises `ArtifactIntegrityError`
    and stores nothing.
    `gate.sandbox.artifact_checksum`, case. **M6.**
12. **The storage key is derived from platform values only.** A
    structural check asserts the key function's parameters are
    exactly `(tenant_id, artifact_id)` and that its module performs
    no path join with a value reachable from `ArtifactMetadata`'s
    caller-supplied fields. A filename is metadata and never a path.
    `gate.sandbox.artifact_key_opaque`, structural. **M6.**
13. **The workspace boundary holds against generated input.** A
    property test over `WorkspaceHandle.resolve` with generated
    paths — separators, dot segments, encodings, NUL bytes, absolute
    forms, symlink chains, over-long components — asserts the result
    is under `root` or `WorkspaceEscape` is raised, and never
    anything else. This is the machinery harness case 19 exercises
    at the tool level.
    `gate.sandbox.workspace_containment`, property. **M4.**

## Tracked metrics

Not gates. Watched, and a regression is an argument rather than a
build failure.

- **Sandbox provision latency**, at the median and the 99th
  percentile, reported per mechanism. It is the number that most
  separates a microVM from a container and the one a user feels as
  the pause before code runs.
- **Sandbox utilization**, the share of a sandbox's lifetime spent
  inside `execute`. A low number means sandboxes are being held
  across waits that should have released them.
- **Egress refusal rate**, split by reason. A rising allowlist-miss
  rate usually means a legitimate destination is missing from
  configuration; a rising private-address rate means something is
  trying, and it should be looked at.
- **Kill reason distribution.** `MEMORY` and `TIMEOUT` climbing
  together generally means the limits are wrong for the workload.
  `OUTPUT_LIMIT` climbing means a tool is being used to page through
  something it should be filtering.
- **Orphan reap count.** Should be near zero. Any sustained non-zero
  value means workers are dying in a way nobody has looked at.
- **Artifact bytes stored per tenant**, and the share deleted by
  retention rather than by request. It is the input to whether
  thirty days is the right default.

## Decisions

1. **The workspace is a cache, not state.** It lives for a worker's
   lease on a run, not for the run. Anything that must survive is an
   artifact. This makes crash-resume a restart rather than a
   recovery, and it is the decision every other lifecycle rule here
   follows from.
2. **The environment a sandbox sees is built, not filtered.** It
   starts empty and three tiers say what may be added. A filter has
   to be complete to be correct; a build has to be wrong on purpose.
3. **Fail-closed means tier 2 alone, not the parent environment and
   not a failed run.** A sandbox missing an optional variable is
   degraded. A sandbox holding the worker's environment is a breach.
4. **Egress is one policy with two enforcement points**, the sandbox
   proxy and the worker's outbound guard. A second implementation is
   how the two drift, and the drift is only ever found by the
   request that should have been refused.
5. **The allowlist has no `allow` mode and no IP destination form.**
   An open mode is one typo from being selected. A literal address
   entry is a way to write down exactly what the private-range check
   exists to refuse.
6. **A wildcard is one leftmost label.** Suffix matching without
   label boundaries matches `evilexample.com`, and that is the
   standard bug in this grammar rather than an exotic one.
7. **The proxy resolves once and dials the address it resolved.**
   Re-resolving between the check and the connection is DNS
   rebinding, which is why the check is written as a sequence.
8. **`169.254.169.254` is denied explicitly even though the
   link-local range covers it.** It is the most valuable destination
   on the list and it should be visible in the configuration rather
   than implied by a CIDR.
9. **Egress needs an operator yes and a policy yes, and the approval
   grants the allowlist rather than the internet.** An approval that
   widened the destination list would make the list a decoration.
10. **Timeouts compose by minimum and the model's value can only
    lower it.** The service's hard cap applies whether or not
    anything else was supplied, so a caller that forgets a timeout
    still gets a bounded command.
11. **`ExecutionEnvironment` has three methods and workspace access
    is a separate port.** A port carrying `execute` beside
    `read_file` gives arbitrary execution to every tool that only
    needed to read a file.
12. **The handle carries no path, no container id, and no host.** An
    adapter that needs one has a design bug, and the check is
    structural rather than a review comment.
13. **`files_changed` is computed by the execution service.** A
    command that reports its own changes can omit one, and the
    omitted one is the interesting case.
14. **`KillReason` is a closed vocabulary surfaced to the model.** A
    model that sees "failed" retries; a model that sees `MEMORY`
    changes what it does. The distinction is worth a field.
15. **The storage key comes from `(tenant_id, artifact_id)` and
    nothing else.** A filename is metadata. Sanitizing on the way out
    keeps the original, and the original is occasionally evidence.
16. **Artifacts are streamed back through the worker rather than
    written by the execution service.** The service holds no store
    credential, and giving it one would put a credential on the host
    that runs untrusted code. One extra hop buys that.
17. **`fake` is a fourth `SandboxMechanism` value and a production
    adapter, not a test double.** It runs the contract suite
    unchanged, which is what lets the whole system be tested without
    a hypervisor. It is refused in production by the same check that
    refuses `docker`.
18. **Isolation is not in the contract suite.** It is a deployment
    property, not a port semantic, and asserting it needs the real
    runtime. Those assertions are the security gates.
19. **Thirteen gates in a new `sandbox` area** rather than a
    `security` area or a split across `structure` and `tool`. The
    reasoning is under open question 1.
20. **`sandbox.run_command` is Milestone 6.** A transcription error
    corrected, not a decision reversed.
21. **Artifacts expire after thirty days by default.** The corpus
    bounds nothing here today. The number is a placeholder with a
    mechanism behind it, and it is an open question below.
22. **Deletion is by `expires_at`, never by reference counting.** An
    artifact referenced by an event resolves to a 404 after
    expiry, which is honest. The alternative is a distributed
    garbage collector nobody asked for.
23. **Provenance is recorded by `write` and lives with the volume.**
    One method and one enum on `WorkspaceHandle`, with no table and
    no migration behind them, because who put these bytes here is
    answerable only where the bytes are written and is meaningless
    once the workspace is gone. `SANDBOX_WRITTEN` is defined two
    milestones before anything can produce it so that the question
    is settled rather than inherited.

## Open questions for review

1. Is a new `sandbox` gate area right? The map's eleven areas are
   subjects with a spec behind each, and this document is a twelfth
   subject with a twelfth spec, which is the pattern. Two
   alternatives were considered. A `security` area would group these
   with the secret scanner and the import walk, and was rejected
   because areas in this registry name subjects rather than
   cross-cutting properties, and a `security` area would eventually
   want to hold gates from six other specs. Splitting them across
   `structure` and `tool` was rejected because one document owns all
   thirteen, and `memory` already shows two specs sharing an area
   rather than one spec straddling two. The map's own open question
   asked between the first two options; this is a third.
2. Is thirty days the right artifact retention? It is the one
   default in this document that silently deletes something a user
   might expect to keep, and it is chosen from nothing. A per-tenant
   byte cap with no time limit is the obvious alternative and has
   the opposite failure — a heavy tenant evicting its own recent
   work.
3. Should a tenant be able to configure egress destinations, subject
   to an operator-set superset? 0.1 says no and that is the safe
   default, but a platform whose tenants each need one API host will
   route every request through an operator ticket. The mechanism —
   intersect the tenant list with the operator list — is small; the
   question is whether the tenant boundary is the right place for
   that trust.
4. Should the workspace be durable across a resume for task runs
   specifically? Interactive runs are short and lose little.
   A twelve-hour task run that loses a workspace to an approval
   pause loses real work, and the counter-argument — shared storage
   between execution hosts — is a deployment cost rather than a
   design impossibility. Deciding no for 0.1 is cheap to reverse;
   deciding yes is not.
5. Which kernel-isolating runtime should be the default,
   gVisor or a microVM? The trade is provision latency against
   isolation strength and syscall compatibility, and the numbers
   depend on the host. This document requires one of the two and
   does not choose, which is a choice deferred rather than made.
6. Should `sandbox.run_command` stream output as it is produced?
   Section 16's sixth observation point mentions "long-running
   sandbox execution where possible", and a five-minute command that
   emits nothing until it finishes is a poor experience. Streaming
   tool output is a change to the event vocabulary and to the tool
   result shape, which is why it is a question rather than a section.
7. Should the sandbox image be per-tenant configurable? One image
   for everybody is simple and is what 0.1 assumes. A tenant needing
   a language the base image lacks currently has no path, and
   per-tenant images bring image supply chain, storage, and warming
   with them.
8. Is there a size at which an artifact should not be created at
   all? The 512 MiB cap is a guard against a runaway, not a product
   decision, and a tenant legitimately producing a 2 GiB export has
   no answer today.
