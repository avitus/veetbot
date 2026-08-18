# ADR-0042: Milestone 6 sandbox, bridge, and artifact seams

- Status: Accepted
- Date: 2026-08-04
- Related: Milestone 6, ADR-0008, ADR-0015, ADR-0021, ADR-0029,
  ADR-0031
- Detailed design: `docs/plan/sandbox-isolation.md` and
  `docs/plan/tool-system.md`

## Context

Milestone 6 turns hostile model-generated code into an execution-service
request, adds a lease-scoped workspace and durable artifacts, and makes the
programmatic tool bridge executable. The plan fixes the security boundary and
the three-method `ExecutionEnvironment` port, but a concrete deployment still
has to select a runtime, carry a Unix socket across the service boundary without
a host bind mount, enforce workspace limits on a portable development runtime,
and choose how artifact bytes are committed with database metadata.

These choices are proposed for owner review. Docker remains a development and
CI mechanism; it is not presented as the production kernel-isolation boundary.

## Proposed decisions

1. **The execution adapter owns every container-runtime subprocess.** Runtime,
   worker, and tool packages hold only the execution and workspace ports. The
   adapter uses the Docker CLI because the project has no runtime SDK
   dependency, and it normalizes command failures before they cross the port.
2. **Docker is the development fallback and gVisor is the production container
   mechanism.** The same adapter selects gVisor with `--runtime=runsc`.
   Production startup continues to reject `docker` and `fake`; selecting the
   unconfigured microVM mechanism fails explicitly. A local Docker security
   pass proves the configured development runtime only, while a production
   deployment must install and verify `runsc`.
3. **The execution image is immutable by the time composition completes.** Its
   base image is pinned by digest. The configured local reference is resolved
   to a Docker image ID and logged during asynchronous application composition,
   and `EnvironmentSpec` accepts only a `sha256:` digest. Provisioning captures
   the initialized workspace as the first snapshot, so the private
   `.agent-initialized` marker is baseline state rather than user provenance.
4. **A named volume is the lease-scoped workspace cache.** No host path appears
   in a port value or mount argument. One environment is created lazily for a
   `(tenant_id, run_id, lease_epoch)` tuple and destroyed on every terminal run
   path; the reaper destroys anything whose lease epoch is no longer live. A
   newly created, unexpired environment receives a 60-second grace window
   before a missing lease snapshot can reap it, closing the race between lease
   acquisition and the maintenance worker's database read. The reaper also
   revalidates each candidate against the repository immediately before
   deletion, so a lease acquired after the snapshot fences teardown. Claimed
   run completion releases only its `(run_id, lease_epoch)` resources; inline
   completion retains the whole-run cleanup path. Local fallback workspace
   handles and directories use the same lease epoch boundary. Release and
   process shutdown first prevent new matching lease operations, wait for
   active execution and workspace operations, and then attempt every teardown.
   Cancellation is re-raised only after those bounded attempts finish, while
   failed handles remain available to a later cleanup retry.
   The environment expiry is the hard resource cap and takes precedence over a
   live-lease snapshot or revalidation result.
5. **Portable workspace quotas use an active service-side monitor.** Docker's
   local volume driver has no portable per-volume byte or inode quota. The
   adapter measures both while a command runs, kills on the first violation,
   and repeats the measurement after exit to close short-command races. The
   safety-net poll backs off from 250 milliseconds to one second, uses one
   metadata read per file, and reuses the prior command's snapshot. Snapshot
   hashing is repeated only when size, mtime, or unforgeable ctime changes, so
   untrusted code cannot hide a content change by restoring mtime. CPU, memory,
   and process bounds remain runtime-enforced cgroup limits.
6. **Allowlisted egress uses an internal-only network and a disposable proxy.**
   The proxy performs the shared policy evaluation, resolves once, rejects
   private addresses before matching the allowlist, dials the resolved numeric
   address, and emits structured decisions. The proxy listens only on its
   internal-network address and separately joins Docker's external bridge for
   outbound dialing; it does not expose the listener on that bridge. The
   sandbox has no direct route to the external bridge network.
   CONNECT remains a byte tunnel after one authorization. Plaintext HTTP is
   deliberately one request per proxy connection: the proxy rejects transfer
   encoding, non-HTTP absolute targets, and missing, invalid, or repeated
   content-length framing; it forwards a bounded body, forces upstream
   `Connection: close`, and closes the client connection after the response.
   Pipelined requests therefore cannot inherit the first request's host
   authorization or evade its audit record.
7. **The programmatic bridge crosses the boundary through `docker exec`
   standard I/O.** A tiny relay in the execution image owns the mode-0600 Unix
   socket inside the workspace. The worker never mounts that socket. The
   execution adapter forwards newline-delimited requests over the runtime
   process pipes to a one-turn `ProgrammaticBridgeSession`, which adds the
   bearer token, derives stable call IDs, caps calls at 64, and re-enters the
   ordinary tool pipeline. Both directions have a 64-KiB message ceiling; an
   unexpected response-stream overrun terminates the relay instead of allowing
   leftover bytes to be interpreted as a later response. The host socket is
   created with a restrictive process umask so it is mode 0600 from the instant
   it is bound rather than being tightened in a later check/use window. Relay
   writes use an asynchronous pipe with backpressure, and host response EOF
   terminates the relay. An approval-hold timeout consumes its ordinal and is
   therefore explicitly non-retryable; replaying it cannot produce a second
   invocation under a different bridge call ID.
8. **General artifacts use store-then-metadata commit order.** The application
   writer spools a bounded stream, computes its digest and size, writes bytes
   under a key derived only from tenant and artifact IDs, and then commits
   metadata. A metadata failure deletes the just-written bytes. A process crash
   between those operations can still leave bytes without metadata, so the
   maintenance worker reconciles objects older than a one-hour safety margin
   against the artifact repository and deletes unmatched objects. That
   store-wide orphan pass has an independent one-hour cadence, while the
   frequent maintenance loop idempotently deletes expired general-artifact
   bytes and then their metadata. Reads resolve explicit trajectory-export
   membership rather than trusting the artifact origin string, open and verify
   the selected backing object before the HTTP streaming response is created,
   and translate a missing object into the public not-found boundary. Artifact
   metadata always has an expiry; adapters reject a database row that violates
   that invariant rather than carrying an optional expiry through the
   application. PostgreSQL has a partial expiry index for non-trajectory rows,
   and principal-scoped download responses use `Cache-Control: private,
   no-store` for both content and ETag revalidation responses. A failed metadata
   commit retains its original exception even if the best-effort byte rollback
   also fails; orphan reconciliation remains the recovery backstop.
9. **Large tool output is an artifact plus a bounded model view.** The pipeline
   stores at most the configured hard-ceiling multiplier times the declared
   output bound, returns a head and tail excerpt with an explicit elision marker
   and file reference, revalidates any structured result changed during
   artifactization, and persists the byte count, truncation flag, and artifact
   ID on the invocation. `artifact.export` remains `SANDBOX_EXPORT` even though
   the plan intentionally classifies it as an in-process capability; other
   tool-created output uses `TOOL_OUTPUT`. Artifactization appends its output
   artifact instead of discarding references already returned by the tool, and
   invocation metadata explicitly selects the truncated-output artifact.
10. **The sandbox security profile exposes a documentation conflict for owner
    review.** `sandbox-isolation.md` requires six operator-set resource limits
    plus an egress policy, while `bootstrap-and-composition.md` freezes an
    exhaustive 106-knob inventory that contains none of them. The implementation
    adds `sandbox/limits.yaml` so the security requirements are configurable,
    including artifact retention and maximum size, and does not silently
    rewrite the 106-knob inventory. If this ADR is accepted, the inventory
    should be amended in a documentation-governance follow-up to count and
    describe the nine sandbox and artifact fields.
11. **Environment construction is an allowlist.** Operator-requested
    passthrough is limited to the reviewed tier-1 proxy and locale names, and a
    secret-name pattern rejects credential-like names in addition to the fixed
    tier-0 vocabulary. In-memory storage is an evaluation tier and is accepted
    only when the deployment explicitly selects the fake sandbox mechanism;
    other combinations fail before sandbox construction.
12. **Enforced kills preserve a reusable lease environment.** Docker stops the
    whole container to terminate a runaway process, then restarts the same
    container before returning the structured kill result. Task cancellation
    performs the same bounded cleanup and re-raises `CancelledError` instead of
    converting caller cancellation into an ordinary tool result.
13. **Workspace path containment is descriptor-relative.** Local and Docker
    workspace reads open every component beneath the workspace directory with
    no-follow semantics, validate the resulting descriptor as a regular file,
    and stream from that descriptor. Docker writes and listings use the same
    component walk. This rejects intermediate and final symlinks without a
    check/use race and preserves distinct missing-path, non-directory, special
    file, and read-limit errors across both adapters.
14. **The bridge bearer token terminates in the trusted relay.** The adapter
    writes the one-time token to the relay over its standard-input pipe before
    forwarding requests. It does not place the token in the host `docker exec`
    argument vector or in the environment of model-generated code; that code
    receives only the workspace socket path. This intentionally narrows the
    tier-2 environment described by `sandbox-isolation.md:681-684`. The relay
    still authenticates each forwarded request to the host bridge, so the
    plan's bearer-token boundary remains enforced without making the bearer
    available to the least-trusted process.
15. **Operational waits have bounded cleanup.** Control-plane Docker CLI calls
    have a 60-second adapter timeout, and a direct workspace stream has one
    60-second deadline across all reads and process exit; their processes are
    killed and waited when the timeout or caller cancellation wins. Tool
    execution remains bounded by its declared
    command and lease limits. Execution monitor tasks are cancelled and joined
    on every path. Release waits up to five seconds for abandoned workspace
    streams before forcing teardown, but it still reconciles an in-flight
    provision before completing; artifact export explicitly closes its source
    stream after the store finishes or fails. These bounds trade a potentially
    incomplete abandoned read for deterministic lease cleanup.
16. **Raw artifact filenames are preserved and sanitized at download.** The
    implementation follows `sandbox-isolation.md:1100-1122` and
    `sandbox-isolation.md:1599-1601`: a producer name such as
    `../../etc/passwd` remains evidence-only metadata and never contributes to
    a storage key, while the HTTP response emits an attachment disposition with
    a separator-free quoted filename. This conflicts with
    `http-api-and-streaming.md:1166-1173`, which says quotes, newlines, and path
    separators are rejected at creation. Owner review must select one rule and
    align the other specification; this ADR does not silently rewrite either
    normative document.

## Consequences

- Development and CircleCI can exercise real isolation behavior with Docker,
  while production retains the plan's gVisor requirement.
- The bridge socket is present inside the sandbox without exposing a host path
  or adding a network listener, and model-generated code cannot read the bridge
  bearer from its environment or the host process list.
- Workspace byte and inode enforcement is portable but approximate to the
  monitor interval on Docker; production runtimes may add native quotas below
  the unchanged port.
- Artifact bytes never depend on a caller-supplied filename or a database
  storage URI, an integrity failure returns no content, and maintenance bounds
  the lifetime of crash-orphaned bytes.
- Until decision 10 is accepted, the existing 106-knob inventory test does not
  claim the nine sandbox-profile fields; both documents remain visible rather
  than one requirement being silently weakened to fit the other.
- Until decision 16 is resolved, the implementation preserves the original
  filename as required by the sandbox specification and enforces the HTTP
  document's output-side attachment and sanitization controls.

## Alternatives considered

- **Treat Docker as production isolation:** rejected because it shares the host
  kernel and contradicts ADR-0008 and ADR-0029.
- **Mount a host Unix socket or workspace directory:** rejected because it
  crosses the boundary with a host path and expands the escape surface.
- **Expose the bridge on TCP:** rejected because the sandbox network namespace
  is intentionally untrusted and may contain an egress proxy.
- **Check disk use only after command completion:** rejected because an
  unbounded writer could exhaust the execution host before returning.
- **Write metadata before artifact bytes:** rejected because a crash would
  leave an authorized reference to content that never existed.
- **Put filenames in storage keys:** rejected because sanitization would become
  the permanent traversal boundary.
