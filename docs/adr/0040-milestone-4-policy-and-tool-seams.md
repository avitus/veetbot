# ADR-0040: Milestone 4 policy, approval, and workspace seams

- Status: Proposed
- Date: 2026-08-03
- Related: Milestone 4, ADR-0005, ADR-0017, ADR-0021, ADR-0023,
  ADR-0026, ADR-0028, ADR-0029
- Detailed design: `docs/plan/policy-and-approvals.md`,
  `docs/plan/runtime-loop.md`, and `docs/plan/builtin-tools.md`

## Context

Milestone 4 is the first implementation of the deterministic policy gate,
durable approvals, principal scopes, resumable tool authorization, workspace
handles, and parallel read-only calls. The detailed designs fix the observable
security and recovery guarantees, while several composition and boundary
choices remain for the first implementation. Two requirements also meet
surfaces that the plan deliberately schedules later: all current events belong
to a session even though a profile-load event is process-wide, and the HTTP
transport is Milestone 5 even though the Milestone 4 implement list names an
approval API.

These choices are proposed for owner review. They do not weaken a hardline
policy rule, scope check, approval revalidation check, or workspace containment
rule.

## Proposed decisions

1. **Policy profiles and hardline rules are strict, content-addressed startup
   files.** The loader rejects unknown fields and incomplete effect matrices,
   freezes the resulting models, and constructs the normative
   `{profile}@{sha12}+h{sha8}` version. A `policy_profiles` row records the
   complete hashes and load identity in both persistence tiers. Rules never come
   from the database.
2. **The process-wide profile-load audit currently uses the audit table, not a
   synthetic session event.** The event model and database require every event
   to have a real `session_id`, while `policy.profile.loaded` is specified at
   process startup before a tenant or session exists. Creating a hidden session
   would misstate ownership and pollute session history; making event session
   identity nullable would change the established event-log invariant. The
   durable audit row therefore carries this fact until a global operational
   event stream is explicitly designed. This is a documented deviation from the
   detailed design's additional event requirement, not a claim that the event
   was emitted.
3. **The approval application service is Milestone 4's transport-neutral API;
   HTTP binding remains Milestone 5.** It provides tenant-scoped get, list,
   idempotent resolve, expiry, and authenticated resume operations, and the CLI
   calls it directly. Adding an ad hoc HTTP stack in this milestone would
   duplicate the server, authentication, error-envelope, and request-ID design
   assigned to Milestone 5. The Milestone 5 router must expose the already
   implemented service without moving approval state transitions into route
   handlers.
4. **Durable queue rows remain the resume signal.** Approval resolution applies
   the guarded `WAITING_FOR_APPROVAL` to `QUEUED` transition through the sole run
   state writer, then invokes `RunDispatcher.resume`. The PostgreSQL dispatcher
   needs no second message because the committed queue row is authoritative;
   the inline dispatcher executes immediately so deterministic and local
   operation retain the same application-service behavior.
5. **`approval.requested` is a post-park latency hint.** Invocation and approval
   creation share one transaction. Checkpoint, waiting transition, and lease
   release share the finalization transaction. Only after that commit does the
   runtime append `approval.requested`; an append failure is logged and does not
   undo a correctly parked run. Approval listing reads the repository rather
   than depending on notification delivery.
6. **The local workspace adapter uses a deterministic per-tenant, per-run root
   and never exposes its host path.** Tools receive only a `WorkspaceHandle`.
   The handle rejects absolute paths, parent traversal, empty path components,
   symlink escapes, invalid UTF-8, and NUL text. The empty string is the sole
   path exception and means the workspace root for `workspace.list_files`,
   because that tool's normative schema uses an empty default for root listing.
7. **Workspace provenance is conservative across process reconstruction.** A
   process-local handle remembers paths written through it and labels those
   reads `INTERNAL_TOOL`. Existing files, including files recovered after a
   process restart, are labeled `EXTERNAL_UNTRUSTED`. Persisting provenance can
   be added later, but a restart can only lower trust and never incorrectly
   raise it.
8. **The domain execution context keeps the workspace collaborator opaque.**
   Concrete workspace tools narrow the object to the `WorkspaceHandle` port at
   their boundary. Importing that port into the domain object would reverse the
   repository's domain-to-port dependency rule merely to improve a type
   annotation.
9. **Effect watermarking is conservative.** Every tool whose declared side
   effect is not `NONE` receives `effect_sent_at` before its implementation is
   invoked, even when a particular execution may only read. This can turn an
   ambiguous non-idempotent crash into `UNCERTAIN`; it cannot cause an effect to
   be silently replayed.
10. **Parallel admission is a closed allowlist.** A batch runs concurrently only
    when every call resolves to an enabled, scoped tool declared `READ_ONLY`,
    marked `allow_parallel`, classified as `NONE`, `WORKSPACE_READ`, or
    `NETWORK_READ`, and admitted by the remaining run budget. Any unknown,
    malformed, effectful, or mixed batch runs sequentially.

## Consequences

- Policy decisions and approval revalidation are deterministic and auditable by
  content hash across deploys.
- Approval waiting consumes no worker lease, slot, or open transaction, and a
  reconstructed worker resumes at the persisted invocation rather than asking
  again.
- A notification failure may delay a UI until its next repository poll but
  cannot lose an approval or strand a run.
- Workspace tools cannot name a host path, and restart provenance errs toward
  less authority.
- Milestone 5 has one existing approval service to bind and must add the HTTP
  transport rather than a second approval implementation.
- A future global event stream decision must either add the missing
  `policy.profile.loaded` event or explicitly amend that detailed-design
  requirement.

## Alternatives considered

- **Create one hidden session per process for policy events:** rejected because
  a process-wide ruleset has no honest tenant, principal, or session owner.
- **Make every event's session nullable in Milestone 4:** rejected because it
  changes allocation, replay, projection, and authorization invariants outside
  this milestone's smallest coherent scope.
- **Build a temporary approval-only HTTP server:** rejected because Milestone 5
  already owns the complete HTTP composition and security boundary.
- **Persist host paths on tool execution contexts:** rejected because it lets a
  tool bypass containment and couples tool code to an adapter.
- **Preserve internal provenance for every pre-existing workspace file:**
  rejected because origin cannot be proved after reconstruction.
- **Parallelize each individually safe subset of a mixed batch:** rejected
  because it introduces observable reordering around effectful calls.
