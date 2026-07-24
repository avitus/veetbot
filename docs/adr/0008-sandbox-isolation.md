# ADR-0008: Sandbox isolation

- Status: Accepted
- Date: 2026-07-17
- Related: Section 18 (sandbox and artifacts), Section 28 (isolation architecture)

## Context

Model-generated code must be assumed hostile. The v1.0 phrase
"Docker-compatible containers" understates a real security decision: the
mechanism the worker uses to *create* containers is itself what an escape will
target. The platform is multi-tenant, so a single container escape must not
reach another tenant's data or the orchestrator's credentials.

Threats: escape to the host kernel; access to database, object-storage, provider,
or cloud credentials; lateral movement across tenants; network exfiltration;
resource exhaustion; persistence via a dirty or shared workspace.

## Decision

1. Put the mechanism behind the existing `ExecutionEnvironment` port and default
   the container adapter, for production, to a **kernel-isolating runtime**: a
   microVM (Firecracker or Cloud Hypervisor, e.g. via Kata) or gVisor. This
   raises the cost of escape from "one host-kernel bug" to "one hypervisor or
   sentry bug."
2. **Never** mount the Docker socket (`/var/run/docker.sock`) into the worker or
   any process that handles or orchestrates untrusted code.
3. Run a **dedicated, least-privileged execution service** that owns sandbox
   lifecycle, holds no application secrets, no database credentials, and no
   provider keys, and runs on separate hosts/nodes where the deployment allows.
   The worker calls the port; it never talks to a container runtime directly.
4. Sandboxes are **ephemeral and per-run** (or per tool call) and never reused
   across tenants.
5. Enforce, at the execution service: network deny by default; non-root with
   user namespaces; all capabilities dropped except those required;
   `no-new-privileges`; a seccomp allowlist; a read-only root image; a fresh
   read-write workspace per run; CPU, memory (with OOM handling), PID, disk and
   inode quotas; and a hard wall-clock timeout independent of any model-supplied
   `timeout_seconds`.
6. Hardened **rootless Docker** is a development-only fallback selected by
   configuration. Production startup must refuse to run untrusted code under the
   development fallback when `AUTH_MODE` is not `dev`.

## Consequences

- A strong, multi-tenant-appropriate isolation boundary.
- Higher operational complexity and per-sandbox startup/overhead than plain
  containers.
- Because the execution service holds no secrets, an escape lands somewhere with
  nothing worth stealing.
- Contract tests (Section 20.4) run against both the fake sandbox and the real
  runtime behind the same port.

## Alternatives considered

- **Docker socket from the worker**: rejected; effectively root on the host.
- **Shared-kernel containers with hardening only**: acceptable for low-risk
  single-tenant use, insufficient as the default for a multi-tenant platform.
- **Run code in the worker/API process**: rejected outright.
