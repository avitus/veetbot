# ADR-0042: Milestone 6 sandbox, bridge, and artifact seams

- Status: Proposed
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
   and `EnvironmentSpec` accepts only a `sha256:` digest.
4. **A named volume is the lease-scoped workspace cache.** No host path appears
   in a port value or mount argument. One environment is created lazily for a
   `(tenant_id, run_id, lease_epoch)` tuple and destroyed on every terminal run
   path; the reaper destroys anything whose lease epoch is no longer live.
5. **Portable workspace quotas use an active service-side monitor.** Docker's
   local volume driver has no portable per-volume byte or inode quota. The
   adapter measures both while a command runs, kills on the first violation,
   and repeats the measurement after exit to close short-command races. CPU,
   memory, and process bounds remain runtime-enforced cgroup limits.
6. **Allowlisted egress uses an internal-only network and a disposable proxy.**
   The proxy performs the shared policy evaluation, resolves once, rejects
   private addresses before matching the allowlist, dials the resolved numeric
   address, and emits structured decisions. The sandbox has no direct route to
   the external bridge network.
7. **The programmatic bridge crosses the boundary through `docker exec`
   standard I/O.** A tiny relay in the execution image owns the mode-0600 Unix
   socket inside the workspace. The worker never mounts that socket. The
   execution adapter forwards newline-delimited requests over the runtime
   process pipes to a one-turn `ProgrammaticBridgeSession`, which adds the
   bearer token, derives stable call IDs, caps calls at 64, and re-enters the
   ordinary tool pipeline.
8. **General artifacts use store-then-metadata commit order.** The application
   writer spools a bounded stream, computes its digest and size, writes bytes
   under a key derived only from tenant and artifact IDs, and then commits
   metadata. A metadata failure deletes the just-written bytes. Reads recompute
   and verify the digest before exposing content.
9. **Large tool output is an artifact plus a bounded model view.** The pipeline
   stores at most four times the declared output bound, returns a head and tail
   excerpt with an explicit elision marker and file reference, and persists the
   byte count, truncation flag, and artifact ID on the invocation.
10. **The sandbox security profile exposes a documentation conflict for owner
    review.** `sandbox-isolation.md` requires six operator-set resource limits
    plus an egress policy, while `bootstrap-and-composition.md` freezes an
    exhaustive 106-knob inventory that contains none of them. The implementation
    adds `sandbox/limits.yaml` so the security requirements are configurable and
    does not silently rewrite the 106-knob inventory. If this ADR is accepted,
    the inventory should be amended in a documentation-governance follow-up to
    count and describe the seven sandbox fields.

## Consequences

- Development and CircleCI can exercise real isolation behavior with Docker,
  while production retains the plan's gVisor requirement.
- The bridge socket is present inside the sandbox without exposing a host path
  or adding a network listener.
- Workspace byte and inode enforcement is portable but approximate to the
  monitor interval on Docker; production runtimes may add native quotas below
  the unchanged port.
- Artifact bytes never depend on a caller-supplied filename or a database
  storage URI, and an integrity failure returns no content.
- Until decision 10 is accepted, the existing 106-knob inventory test does not
  claim the seven sandbox-profile fields; both documents remain visible rather
  than one requirement being silently weakened to fit the other.

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
