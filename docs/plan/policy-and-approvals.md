---
title: Policy and Approvals
status: design
canonical: true
---

# The policy engine and the approval lifecycle

This expands Sections 8.3, 8.4, 9, 11.2, 13, and 22 of the
[engineering plan](engineering-plan.md), and Milestone 4. It does not replace
them. Where this document adds a type, a column, or a rule, it is an addition of
the kind Section 15 already sanctions when it says to create migrations for *at
least* the tables it lists, and Section 4 sanctions when it says to create
directories as the implementation reaches them.

Recorded as [ADR-0005](../adr/0005-deterministic-policy-engine.md) and
[ADR-0006](../adr/0006-no-private-reasoning-storage.md), and constrained by
[ADR-0017](../adr/0017-layered-approval-and-inbound-surface-security.md).

## The policy engine is the only thing standing between a model and the world

Section 2.5 states the premise in one line: *a prompt instruction is not an
authorization mechanism*. Section 5's eleventh dependency rule states the
consequence: *the policy engine must not depend on prompts or model judgment*.
Everything below is those two sentences made mechanical.

The reason this layer is worth more care than its size suggests is that it is
the only component whose failures are silent in the direction that matters. A
broken model gateway produces an error. A broken projection produces a wrong
answer someone eventually notices. A policy engine that allows one action it
should have gated produces a completed action, a satisfied user, and no signal
at all — until the action was a payment, a deletion, or an email to a customer
list.

That asymmetry drives four properties this document is written to guarantee.

- **The decision is a pure function of its inputs.** Same action, same
  principal, same ruleset, same answer — always, and provably, because the
  evaluator performs no I/O and reads no clock.
- **There is exactly one place a proposed action can become an authorized
  one.** Not one per surface. One, with an import-boundary test that says so.
- **Nothing downstream of the model can widen a decision.** Tool output,
  memory, retrieved documents, and the model's own argument can only ever make
  a decision more restrictive, and most of them cannot move it at all.
- **A decision that cannot be classified is a denial.** Not a default-allow,
  not an exception that some caller might catch.

The plan describes this engine at the level of its interface. What it does not
supply — and what Milestone 4 cannot be built without — is the vocabulary the
interface is written in. `ProposedAction`, `ApprovalStatus`, `SideEffectClass`,
`RiskLevel`, and `IdempotencyClass` each appear exactly once in the plan, as
the type of a field, with no definition anywhere. The next section defines them
in the only way that is safe: derived from statements the plan already makes,
so that each value exists because something in the plan requires it and not
because it seemed useful.

## The vocabulary the interface is written in

### `SideEffectClass`

`ToolSpec.side_effect` (Section 8.1) is typed `SideEffectClass` and never
defined. Section 9.2's default policy matrix is keyed on a prose column called
"Action category" with sixteen rows and no referent. These are the same thing
seen from two ends, and joining them is what makes the matrix executable.

```python
class SideEffectClass(str, Enum):
    # Each comment names the Section 9.2 matrix row this value came from.
    NONE = "none"                            # Pure computation
    WORKSPACE_READ = "workspace_read"        # Read isolated workspace
    WORKSPACE_WRITE = "workspace_write"      # Write isolated workspace
    NETWORK_READ = "network_read"            # Read approved resource
    CODE_EXECUTION = "code_execution"        # Execute code
    PACKAGE_INSTALL = "package_install"      # Install packages
    SANDBOX_NETWORK = "sandbox_network"      # Enable sandbox network
    EXTERNAL_MESSAGE = "external_message"    # Send a message
    EXTERNAL_WRITE = "external_write"        # Modify external data
    EXTERNAL_DELETE = "external_delete"      # Delete external data
    FINANCIAL = "financial"                  # Spend money
    PUBLICATION = "publication"              # Publish content
    CREDENTIAL_ACCESS = "credential_access"  # Access raw credentials
    HOST_ACCESS = "host_access"              # Access host filesystem
    PRIVILEGED = "privileged"                # Privileged container op
```

Fifteen values for Section 9.2's first fifteen rows. The sixteenth row,
"Unknown tool", is not a side-effect class and is discussed below.

The comments are load-bearing. They are the evidence that this enum was read
off the plan rather than designed, and a test asserts the correspondence is
total in both directions: every enum value has exactly one matrix row, and
every matrix row has exactly one enum value.

### `RiskLevel`

```python
class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
```

**Risk never selects the decision.** The side-effect class does. Risk is
carried on the decision and used for three things that are not authorization:
ordering the pending-approval queue so a human sees the consequential item
first, choosing the default approval expiry, and grouping metrics.

This restraint is deliberate. If risk could also select a decision there would
be two matrices, they would disagree, and the disagreement would surface as an
allow. A profile may define a rule keyed on the pair `(side_effect, risk)` when
it genuinely needs to distinguish, but no rule may be keyed on risk alone.

### `IdempotencyClass`

Section 8.4 states four crash-recovery behaviours. The enum is those four
behaviours named, and nothing more.

```python
class IdempotencyClass(str, Enum):
    # Each comment is Section 8.4's crash-recovery rule for that class.
    READ_ONLY = "read_only"                                # retry
    IDEMPOTENT = "idempotent"                              # retry
    CONDITIONALLY_IDEMPOTENT = "conditionally_idempotent"  # key required
    NON_IDEMPOTENT = "non_idempotent"                      # never retry
```

The event-log spec's recovery path already dispatches on this classification
when it decides whether a tool left `RUNNING` by a crash may be retried or must
resolve to `UNCERTAIN`. That dispatch is the reason the classification is
denormalized onto `tool_invocations` below rather than resolved from a
historical `ToolSpec` version at recovery time.

### `ActionKind`, and actions that are not tool calls

The plan's approval object hangs off `tool_invocation_id`, non-nullable. But
Section 30.3 requires that authoring a skill be gated by policy and approval,
and the memory specs require governance on the write path. A memory write is
not a tool invocation at all. Skill authoring is one — `skill_manage`, a
capability tool, per [skills.md](skills.md) — but its approval carries a diff
rather than an argument blob, and the payload is selected by the action kind.
Rather than inventing a synthetic tool invocation for the memory case — which
would put rows in `tool_invocations` that no tool ever executed, and corrupt
every metric computed over that table — the action becomes the general case and
the tool call becomes the common one.

```python
class ActionKind(str, Enum):
    TOOL_CALL = "tool_call"
    MEMORY_WRITE = "memory_write"
    SKILL_AUTHORING = "skill_authoring"
    ARTIFACT_EXPORT = "artifact_export"
```

`tool_invocation_id` becomes nullable and is populated only when the action
has one behind it, which is every `TOOL_CALL` and every `SKILL_AUTHORING`;
a new `action_id` carries the reference in every case. Nothing about the tool
path changes.

### `ApprovalStatus`

```python
class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
```

Five statuses, but Section 9.3 lists only two resolutions, `APPROVE_ONCE` and
`DENY`. The lists differ in length because `EXPIRED` and `CANCELLED` are
statuses reached *without* a resolution — by the reaper and by the cancellation
edge respectively, both of which Section 9.3 assigns to the application service.

`CANCELLED` is kept distinct from `DENIED` for a reason that shows up later, in
the metrics: a run cancelled by its own user reaps a pending approval, and
counting that as a human denial makes the denial rate a measure of how often
people change their minds rather than a measure of how often the agent proposes
something unwelcome.

```python
class ApprovalResolutionType(str, Enum):
    APPROVE_ONCE = "approve_once"
    DENY = "deny"
```

Exactly Section 9.3's two. Session-wide and permanent grants are explicitly out
of scope for version 0.1 and no value is reserved for them.

## `ProposedAction`

The policy engine's primary input appears once in the entire plan, as the type
of the first parameter of `PolicyEngine.evaluate` (Section 7). Everything the
engine can decide with is exactly what this object carries, so its field list
is the real specification of the engine's power.

```python
class ProposedAction(BaseModel):
    kind: ActionKind
    action_id: UUID
    tenant_id: str
    session_id: UUID
    run_id: UUID
    step_number: int

    name: str                        # tool name, or the action name
    version: str | None              # ToolSpec.version when there is one
    summary: str                     # human sentence for the approval UI

    side_effect: SideEffectClass
    risk: RiskLevel
    idempotency: IdempotencyClass
    required_scopes: set[str]

    arguments: dict[str, Any]        # already normalized (Section 8.3)
    normalized_arguments_hash: str
    argument_trust: dict[str, TrustLevel]
    origin_trust: TrustLevel

    target: ExecutionTarget
    evaluated_at: datetime           # passed in; never read from a clock
```

Four fields deserve their justification.

`argument_trust` and `origin_trust` exist because the same action is not the
same action twice. `workspace.write_text` proposed because the user asked for a
file is an ordinary write. The identical call, with identical arguments,
proposed because a fetched web page contained text instructing the agent to
write it, is an injection succeeding. Section 11.2 already requires every
context item to carry a trust label; carrying that label forward into the
decision is what lets a rule say *require approval when any argument derives
from `EXTERNAL_UNTRUSTED` content* — which is a mechanical defense, unlike
asking the model to be careful.

`target` is what makes Section 9.2's "Allow only in sandbox" executable.

```python
class ExecutionTarget(BaseModel):
    kind: str                # in_process | sandbox | device | mcp_server
    isolated: bool
    network_enabled: bool
    device_id: str | None
    server_id: str | None
```

Section 29 is explicit that a device is an execution target and not a policy or
credential authority — "it ran on my laptop" is not an authorization. Modelling
the device as a field on the input rather than as a separate evaluation path is
how that stays true.

`evaluated_at` is passed in rather than read. A rule that needs the time — a
business-hours approval window, an expiry computation — receives it as data.
The moment the evaluator can call `now()` it stops being replayable, and the
determinism gate below stops being testable.

## The scope vocabulary, and the check that precedes policy

`required_scopes` is a field on `ToolSpec` and on `ProposedAction`, and
Section 8.3 checks it at step 3, ahead of the engine. Three things were
never written down: which strings are legal, how the check compares them,
and where a worker gets the principal's scope set. Each is an answer an
implementer would otherwise have to invent, and two of the three have a
wrong version that looks right.

### One vocabulary, not two

[http-api-and-streaming.md](http-api-and-streaming.md) enumerates the
scopes its routes require and states the property that matters: the
vocabulary is closed, because a scope the policy engine checks against a
string no document contains is a scope that gets misspelled. It made
`skill.write` the demonstration — enumerated there, in no row of its route
table, checked here.

The rest of the tool-checked scopes belong in the same list for the same
reason. That document is Milestone 5 and this check is Milestone 4, so the
union is written here:

```text
session.read      session.write
run.read          run.write        run.cancel
approval.read     approval.resolve
artifact.read     artifact.write
workspace.read    workspace.write
sandbox.execute
skill.write
demo.write
knowledge.write
```

Fifteen strings. Nine are enumerated by the API document; the five after
them are the `required_scopes` the builtin roster declares in
[builtin-tools.md](builtin-tools.md), and `knowledge.write` is the one
[knowledge-documents.md](knowledge-documents.md) adds for ingestion.
`artifact.read` and `artifact.write` are the pair that shows this is one
namespace rather than two that happen to collide: the first gates a read
route, the second gates `artifact.export`, and they are two actions on
one resource.

### The grammar, and the contributor a closed list cannot hold

A scope is two or more lowercase segments matching `[a-z][a-z0-9_]*`
joined by dots, of which the last is the action. All fifteen have
exactly two.

A closed list needs no grammar, so the grammar exists for the one
contributor the list cannot enumerate. `tool-system.md:1188` takes an MCP
tool's `required_scopes` from server configuration — the operator declares
them, never the server — and an operator-declared string is outside a
closed set by construction. The rule is therefore that an entry is legal
when it is one of the fifteen, or when its first segment is `mcp` and its
second is the server id. `mcp.files.write` is legal on a tool from the
`files` server. `run.cancel` on that tool is not.

The escalation this prevents is quiet, which is why it is worth naming.
`required_scopes` reads as a restriction, so a misdeclaration looks
harmless — the apparent worst case is a tool nobody can call. It is not.
An operator who declares that a remote filesystem-write tool requires
`session.write` has handed every principal that can open a session the
ability to write files on that server, because that principal already
holds the scope. Borrowing a platform scope for a capability the platform
never granted is how a scope system stops meaning anything, and it is one
configuration typo away. Registration validation rejects it.

### The comparison is a subset test over exact strings

`required_scopes` is a set and the rule is all of them:

```python
missing = action.required_scopes - principal.scopes
```

Empty passes. Non-empty raises `AuthorizationError`, which the pipeline
converts into `denied` with `reason_code = policy.scope.missing`.

There is no hierarchy, no wildcard, and no prefix rule. `run.write` does
not admit `run.read`, and `mcp.files.write` does not admit
`mcp.files.read` — a shared prefix is a naming convention and not an
evaluation input. This is the API document's exact-match rule applied to a
set rather than to one route scope, and the argument is unchanged: a
hierarchy needs an evaluation order, an evaluation order is a thing that
can be subtly wrong, and the direction it is wrong in grants access nobody
intended.

An empty `required_scopes` is a tool with no scope check. That is what the
two Milestone 1 builtins carry, and
[builtin-tools.md](builtin-tools.md) gives the reason: a scope that gates
arithmetic is a scope that gets granted to everyone, which teaches the
scope system to be ignored.

### The scope denial names the scope, and no other denial names anything

The section below argues at length that a denial must not tell the model
what fired. The scope denial is the exception, and
[skills.md](skills.md) already takes it — the denial names the scope.

The distinction is that a policy rule is an evasion gradient and a scope
is not. A model that learns a rule denied a write at four hundred
kilobytes tries three hundred and ninety-nine. A model that learns
`workspace.write` was missing learns nothing it can act on, because it
cannot grant itself a scope; that sentence is for the human reading the
transcript. The missing scopes are named. The set the principal does hold
never is, because that is a map of the surface still worth probing.

### Where a worker gets the scope set

The check runs on a worker and a worker has no credential.
[http-api-and-streaming.md](http-api-and-streaming.md) turns a credential
into a `Principal` inside a request; [runtime-loop.md](runtime-loop.md)
declares `PrincipalResolver.for_run`, which produces one from a `Run`, and
nothing said from what.

**The scope set is captured at submission and stamped on the run**, and
`for_run` reads the stamp. It does not read a principal table.
Re-deriving the set on the worker would be wrong rather than merely
slower: the runtime already fixes that a permission change takes effect on
the next run and not mid-run, and that guarantee holds only if the run
carries the set it was submitted under. A re-derivation makes it depend on
how long the run sat in the queue, which is the kind of guarantee that
passes every test and fails under load.

This is a Milestone 2 column serving a Milestone 4 check. ADR-0032
established that direction for the consent stamp and the reason transfers:
a run that started before the column existed has no honest value to
backfill, so the column has to precede the first run whose decisions
anyone might later have to explain.

Approval resumption is the one path that deliberately ignores the stamp.
The revalidation table below voids an approval when the principal's scopes
are narrowed, and the runtime names resumption as its one exception to
resolving a principal once. Both say the same thing: a human's consent is
authority the human held at the moment they gave it, so resumption
compares against the current principal.

The advertisement filter uses the same predicate at the other end. Section
11.1's second filter is principal authorization, and it is this subset
test run once at session open. That is why a tool can be advertised and
then denied at call time: the filter ran against the session, the check
runs per action against the run's stamp, and
[tool-system.md](tool-system.md) fixes that the pinned set is never
rewritten in between.

### Roles are declared in 0.1 and resolved in the version after it

`Principal.roles` exists in Section 6.2, and the API document says roles
are bundles that authentication resolves into a scope set. In 0.1 there is
one token and one configured principal, so there is no bundle to resolve:
the configured principal's scopes are configured directly, `roles` is
populated for audit and for log lines, and nothing reads it as an
authorization input. Naming the mechanism without building it is the
posture the corpus already takes for multi-tenancy — the field is shaped
now so that it does not have to be added later, and the resolution step
arrives with the second principal.

`AUTH_MODE=dev` binds the full scope set, and this section is what "full"
means: all fifteen, and no `mcp.` scope. Those exist only once a server
is configured, and a development principal that silently held every scope
an operator could declare would make the misdeclaration above the one
class of mistake development cannot surface.

## The decision, and what may change it

### Restrictiveness is an ordering

ADR-0017 permits the advisory layer to make a decision "more restrictive" and
never defines the comparison. Over `PolicyDecisionType` (Section 9.1):

```text
rank 0   ALLOW
rank 1   ALLOW_WITH_MODIFICATIONS      narrows the action
rank 2   REQUIRE_APPROVAL              suspends the action
rank 3   DENY                          ends the action
```

Combination is `max` by rank. The ordering is total, so combination is
associative and commutative, so the order in which layers are consulted cannot
change the outcome — which is what makes the layering safe to reason about.

Two constraints keep the ordering honest.

- **Only the deterministic layer may produce modifications.** Combining two
  different `modified_arguments` sets is undefined, and an advisory layer that
  could rewrite arguments would be an injection vector wearing a safety badge.
  Exactly one rule may modify: the highest-precedence rule that matches.
- **A hardline block is not a rank.** It is evaluated first and short-circuits.
  No later layer sees it, so no later layer can be induced to soften it. This
  is the mechanical meaning of "frozen at load" in ADR-0017: not merely that
  the rules cannot be edited, but that nothing runs after them that could
  reinterpret them.

### `ALLOW_WITH_MODIFICATIONS` and the idempotency key

The fourth decision type has a sequencing problem the plan does not mention.
Section 8.4 derives the idempotency key from the normalized arguments hash, and
Section 8.3 normalizes arguments *before* policy runs. If policy then modifies
the arguments, the key that was computed no longer describes the call that will
execute, and a crash-recovery lookup will match a key for an action that never
happened.

The resolution: when a decision carries `modified_arguments`, the pipeline
re-normalizes and recomputes the idempotency key from the modified arguments
before execution, and persists both hashes on the invocation — the proposed one
for tracing the model's intent, the effective one for idempotency. The
`normalized_arguments_hash` on `ProposedAction` is always the proposed one.

Because this type has no consumer anywhere in the plan, the default profile
ships **zero rules that produce it**. The plumbing exists, is tested, and is
unused until a rule needs it. Recorded as a decision below.

## The four layers

Section 9.3's closing paragraphs describe four layers. In evaluation order:

### 1. Hardline rules

Never-bypassable patterns, evaluated first, frozen at load, disabled by
nothing.

**Where they live.** `src/agent_core/policy/hardline.yaml`, packaged inside the
distribution so it is immutable in a built image. Section 22 lists "Policy
rules" as *Trusted*; a database table is editable at runtime by anyone holding a
connection string, which is not a trust boundary, so rules are files under
version control and not rows.

**How they are frozen.** The loader reads the file once at import, validates it,
computes its SHA-256, and exposes an immutable frozen structure. There is no
setter, no reload path, and no environment variable that alters the set. A test
asserts the module exposes no public mutator and that mutating the loaded
object raises.

**Why they are not behind a port.** Every other collaborator in this system is a
Protocol with a substitutable implementation. Hardline evaluation is a
module-level pure function, deliberately. A substitutable never-bypassable rule
is a contradiction: the substitution point *is* the bypass. This is the one
place in the architecture where the ports-and-adapters rule is suspended, and
the suspension is the point.

**Format.**

```yaml
rules:
  - id: destructive_root_delete
    kind: command_regex
    applies_to: [code_execution, sandbox_network]
    pattern: '\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/(\s|$)'
    message_code: policy.hardline.destructive_command
    near_miss: 'rm -rf ./build'
  - id: credential_class
    kind: side_effect
    applies_to: [credential_access]
    message_code: policy.hardline.credential_access
    near_miss: 'read a credential *reference* from configuration'
```

**Why every rule carries a `near_miss`.** ADR-0017 already flags the false-block
hazard. The `near_miss` field is a required legitimate input that the rule must
*not* block, and the test suite asserts both directions for every rule. A rule
that cannot name something similar it permits is a rule that is too broad, and
the schema will not let it load. This is the discipline that keeps the hardline
list from growing into the deterministic layer.

The initial set covers what ADR-0017 names — destructive commands and secret
exfiltration — plus the classes Section 9.2 denies outright:

- Root-scoped destructive commands and filesystem-destroying operations.
- Writes to host paths outside the workspace: `/etc`, `/usr`, `~/.ssh`,
  `.git/config`, `.env` and its variants.
- Any action classified `CREDENTIAL_ACCESS`, `HOST_ACCESS`, or `PRIVILEGED`.
- Egress to link-local metadata addresses.
- Reading a credential-shaped value and passing it to an egress or messaging
  action within the same action.

### 2. The deterministic layer

Authoritative, per Section 9.3. This is Section 9.2's matrix, keyed on
`SideEffectClass`, extended with the one column the prose form was missing.

| `side_effect` | Condition | Decision | Otherwise |
|---------------|-----------|----------|-----------|
| `NONE` | — | Allow | — |
| `WORKSPACE_READ` | path inside workspace | Allow | Deny |
| `WORKSPACE_WRITE` | path inside workspace | Allow | Deny |
| `NETWORK_READ` | host on the allowlist | Allow | Deny |
| `CODE_EXECUTION` | `target.isolated` | Allow | Deny |
| `PACKAGE_INSTALL` | — | Deny | — |
| `SANDBOX_NETWORK` | — | Deny | — |
| `EXTERNAL_MESSAGE` | — | Require approval | — |
| `EXTERNAL_WRITE` | — | Require approval | — |
| `EXTERNAL_DELETE` | — | Require approval | — |
| `FINANCIAL` | — | Require approval | — |
| `PUBLICATION` | — | Require approval | — |
| `CREDENTIAL_ACCESS` | — | Deny | — |
| `HOST_ACCESS` | — | Deny | — |
| `PRIVILEGED` | — | Deny | — |

Three of Section 9.2's decision strings are not `PolicyDecisionType` values.
They resolve as follows, without changing any outcome the plan states.

- **"Allow with restrictions"** (read approved network resource) is `ALLOW`
  guarded by a predicate, with `DENY` when the predicate fails. The restriction
  was always a condition on the argument; the condition column is where it goes.
- **"Allow only in sandbox"** (execute code) is the same shape, with
  `target.isolated` as the predicate.
- **"Deny initially"** (install packages, enable sandbox network) is `DENY` in
  the `default` profile. "Initially" is a statement about which profile is
  loaded, not about a fifth decision type — which is precisely what profiles
  are for.

**Trust overlay.** One rule applies across the table: if any argument's trust
label is `EXTERNAL_UNTRUSTED`, a decision of `ALLOW` for a class other than
`NONE`, `WORKSPACE_READ`, or `NETWORK_READ` is raised to `REQUIRE_APPROVAL`.
This is a `max` combination like any other, so it can only tighten.

**Unclassifiable actions.** Section 9.2's "Unknown tool → Deny" row looks
unreachable, because Section 8.3 resolves the tool before policy runs and an
unresolvable name already fails with `ToolNotFoundError`. The row is not
unreachable; it is more general than its name. The engine denies any action
whose classification it cannot determine:

- a `side_effect` value the loaded profile has no rule for;
- an action arriving through the in-sandbox RPC bridge (Section 8.5) or a
  device channel (Section 29) whose name resolves to no registered tool;
- a `ProposedAction` failing schema validation.

All three return `DENY` with `reason_code = policy.unclassifiable_action`. The
row's intent — fail closed on the unknown — is preserved and given a reachable
home.

### 3. The advisory layer

Optional, off by default, sequenced after Milestone 6 by Section 21.1.

- Its output is constrained to `abstain`, `require_approval`, or `deny`, via a
  schema-validated structured response. It cannot emit `ALLOW` and it cannot
  emit modifications, so by construction it cannot lower a rank.
- It runs **only** when the deterministic decision is `ALLOW` or
  `ALLOW_WITH_MODIFICATIONS`. There is nothing for it to add to a decision that
  already denies or already suspends, and skipping it there keeps the expensive
  path off the common path.
- It never sees the hardline list or the profile contents. Showing an
  injectable component the rules it is protecting is showing the injection the
  rules it needs to evade.
- Its input is injection-hardened per ADR-0017: comments stripped, untrusted
  content XML-delimited.
- **On timeout or error it abstains.** Failing open is correct here and only
  here: the advisory layer can only escalate, so an unavailable advisor leaves
  the system exactly as safe as the deterministic gate, which is authoritative
  by Section 9.3. Blocking on it would make an optional component load-bearing
  for availability, which ADR-0017 forbids in the safety direction and which is
  no better in this one.

### 4. Human approval

The gate for consequential actions, specified in the next section.

## Policy profiles and `policy_version`

`AgentSpec.policy_profile` (Section 6.1) is a `str` selecting a profile by name.
`PolicyDecision.policy_version` (Section 9.1) is a `str` with no stated
producer, format, or storage — and the context engine's `ContextPlan` already
consumes it. This section is what makes that reference resolve.

A **profile** is a named, versioned, file-backed rule set at
`src/agent_core/policy/{name}.yaml`. Version 0.1 ships one, `default`, which is
the matrix above.

A **`policy_version`** identifies the entire evaluated ruleset — profile plus
hardline set — not a single file:

```text
{profile_name}@{profile_sha256[:12]}+h{hardline_sha256[:8]}

default@3f2a1c9d4e5b+h7c1e0a92
```

A content hash rather than a counter, for one reason: it cannot be forgotten.
A monotonic version number requires a human to remember to increment it while
editing a rule at an hour when humans do not remember things, and a stale
number is worse than no number because it asserts a falsehood. Two deployments
reporting the same string provably evaluated identical rules.

**Profiles are loaded once at process start and frozen**, exactly like the
hardline set, and a `policy.profile.loaded` event records the load. Rules
therefore change across a deploy, never within a process. This is what makes
Section 9.3's step 8 — "revalidate policy after approval in case policy or
arguments changed" — analyzable rather than a race: an approval can outlive a
deploy, which is exactly the window step 8 exists to close.

`policy_version` is written to:

- every `PolicyDecision`, hence `tool_invocations.policy_decision`;
- the `approvals` row, at request time and again at revalidation;
- `ContextPlan` (context engine), because the cacheable prefix advertises the
  tool list and its gating, so a ruleset change makes the prefix's claim about
  what requires approval stale;
- the `policy_profiles` audit table, so a version string found on a
  year-old decision can be resolved to the rules that produced it.

That last table is an audit record, not a rule store. Rules stay in files.

## The approval lifecycle

### The eight steps, and where each one can fail

Section 9.3's sequence, with the failure mode each step must survive. The
ordering is not negotiable: every step before the lease release must be durable,
because after the lease is released no worker is watching.

1. **Persist the proposed tool invocation** — status `WAITING_FOR_APPROVAL`,
   carrying the classification and the `PolicyDecision`.
2. **Persist the approval request** — same transaction as step 1. A proposed
   invocation with no approval row is a run that waits forever.
3. **Checkpoint the run** — the checkpoint records `pending_approval_ids`
   (Section 6.9), which is how the resumed worker knows what it is resuming.
4. **Transition the run to `WAITING_FOR_APPROVAL`** — a guarded transition from
   `RUNNING`, which fails if the run was cancelled concurrently.
5. **Release the worker lease** — last, and only after 1–4 committed. A crash
   before this point leaves the lease to expire and the run to be reclaimed and
   replayed from the checkpoint; a crash after it leaves a correctly parked run.
6. **Emit the approval event** — `approval.requested`. Per the event-log spec,
   notification is a latency hint, so the approval UI polls from a watermark
   and does not depend on the notification arriving.
7. **Resume only after an authenticated resolution** — via the run dispatcher,
   which re-queues the run.
8. **Revalidate policy** — specified below.

### Resolution is idempotent, and the first one wins

Section 29 requires that any authorized device may resolve an approval and that
resolution be idempotent, first resolution winning. Mechanically:

```sql
UPDATE approvals
   SET status = :new_status,
       resolution = :resolution,
       resolved_at = :now,
       resolved_by = :principal_id
 WHERE id = :approval_id
   AND tenant_id = :tenant_id
   AND status = 'PENDING'
RETURNING *;
```

Zero rows returned means someone else resolved it first. The response then
depends on whether the two callers agreed:

- **Same decision** — return `200` with the stored approval. This is what
  idempotent means: the caller's intent holds, and a phone that retried on a
  flaky connection should not see an error.
- **Different decision** — return `409` with the stored resolution in the body.
  First resolution still wins; the second caller is told, because silently
  discarding a human's explicit "deny" and reporting success is the one
  behaviour here that could mislead someone into thinking they had stopped
  something.

Authorization to resolve requires the `approval.resolve` scope and a matching
`tenant_id`. Cross-tenant access returns **not found**, never forbidden —
forbidden confirms the row exists, which is itself a leak, and Milestone 4
requires cross-tenant approval access to be rejected.

Self-approval — the principal who started the run resolving their own
approval — is permitted by default, because the single-user deployment
Section 6.2 anticipates has no one else. A profile may require a distinct
resolver; the check is a rule, not a hardcoded condition.

### Expiry and cancellation

Both edges belong to the application service, not the worker loop, because a
parked run has no worker (Section 9.3).

**The reaper** runs periodically, claims approvals past `expires_at` with a
guarded update identical in shape to the one above, marks the tool invocation
`DENIED`, emits `approval.resolved` with an expiry resolution — the plan
specifies this event, so no new event type is introduced — and completes or
fails the run deterministically. Default expiry by risk: 24 hours for `LOW` and
`MEDIUM`, 4 hours for `HIGH`, 1 hour for `CRITICAL`. An expired approval is
never auto-approved, in any profile, at any risk level.

**Cancellation** of a `QUEUED` or `WAITING_FOR_APPROVAL` run transitions
directly to `CANCELLED` and reaps any pending approval to `CANCELLED` — not
`DENIED`, per the status discussion above.

The reaper and the cancellation path can race for the same row. Both use the
same `WHERE status = 'PENDING'` guard, so one wins and the other is a no-op;
neither needs a lock and the losing side has nothing to undo.

### Revalidation after approval

Step 8 says "in case policy or arguments changed" and leaves the comparison
unspecified. Four things are compared between request and resume:

| Changed since the request | Consequence |
|---------------------------|-------------|
| `normalized_arguments_hash` | Approval void — different bytes were approved |
| Principal scopes narrowed | Approval void — authority was revoked |
| `AgentSpec.version` | Approval void — tools or profile may have changed |
| `policy_version` | Re-evaluate from scratch |

The first three void the approval outright, emit `approval.invalidated`, and
deny the call. They are not re-askable: the human approved a specific action,
and a changed action is a new question.

The fourth re-runs the engine. If it returns `ALLOW`, execution proceeds. If it
returns anything else — including a second `REQUIRE_APPROVAL` — the call is
denied with `reason_code = policy.revalidation.escalated`. Asking a second time
would be defensible, but it admits a loop in which a ruleset that always
escalates parks a run forever, and a denial the user can retry deliberately is
better than a pause the system cannot leave.

## Denial is a message to the model, and a message is an attack surface

Milestone 4's acceptance criterion "denial becomes a structured tool result"
has no shape anywhere in the plan. It needs one for a protocol reason and a
security reason.

The protocol reason: both first adapters treat a tool-use block with no
corresponding tool-result as a malformed conversation. A denial that simply
drops the call corrupts the next request.

The security reason: the model is a *partially trusted* consumer (Section 22).
Telling it precisely which pattern blocked it tells it what to avoid, and a
model that reformulates until it gets through has been handed a search
gradient.

So the denial result is deliberately thin:

```json
{
  "status": "denied",
  "action": "demo.external_write",
  "reason_code": "policy.matrix.external_write",
  "message": "Not performed. Approval was required and was denied.",
  "retryable": false,
  "remediation": "none"
}
```

- `reason_code` is a stable enumerated string of the form
  `policy.{layer}.{rule}` or `approval.{outcome}`. Stability matters: the loop
  detector below keys on it.
- `message` is a fixed string per `reason_code`. It never contains the rule
  text, the pattern, the profile name, or another principal's data. The full
  `explanation` from `PolicyDecision` goes to the audit log and the human
  approval UI, not into the context window.
- `remediation` is one of `none`, `request_approval`, or `modify_arguments` —
  enough for the model to choose a next step, not enough to reverse-engineer
  the rule.
- A test asserts the serialized result matches an allowlist of exactly these
  fields.

**Scope failures produce this same shape.** Section 8.3 checks principal scopes
*before* policy, so a missing scope is an `AuthorizationError` and not a
`PolicyDecision` — it carries no `policy_version`, because no policy was
evaluated. The denial result is therefore produced by the pipeline rather than
by the engine, and covers both origins with `reason_code =
policy.scope.missing` in the second case.

**The repeated-denial circuit breaker.** A model that proposes a denied action,
reads a denial, and proposes it again will do so until the run budget is gone.
Because `reason_code` is stable and `normalized_arguments_hash` is computed
anyway, the runtime counts identical denied proposals per run keyed on
`(name, normalized_arguments_hash, reason_code)` and fails the run after the
third, with `ToolPolicyDenied` (Section 13). Three, not one: a model correcting
its arguments after a denial is legitimate and common, and it changes the hash.

## Trust tiers and trust labels

Section 22's three tiers and Section 11.2's seven `TrustLevel` labels do not map
onto one another anywhere, and two labels — `MEMORY` and `KNOWLEDGE` — have no
tier at all. That gap matters here because Section 11.2 also states that a tool
result must never redefine platform policy or change approval requirements,
which is an authorization claim about a trust label.

The mapping, plus the authorization weight each label carries:

| `TrustLevel` | Section 22 tier | May authorize |
|--------------|-----------------|---------------|
| `PLATFORM` | Trusted | Yes — carries policy |
| `TRUSTED_CONFIGURATION` | Trusted | Yes — within the deployed ruleset |
| `USER` | Partially trusted | Within the principal's scopes only |
| `INTERNAL_TOOL` | Partially trusted | No |
| `MEMORY` | Partially trusted | No |
| `KNOWLEDGE` | Untrusted for instructions | No |
| `EXTERNAL_UNTRUSTED` | Untrusted | No |

`MEMORY` is placed with the partially trusted content because it is
agent-authored from prior conversation, which means it is at most as trustworthy
as the least trustworthy content that produced it — and the memory formation
spec tracks that provenance. But it cannot authorize, and that is the important
half: a belief that says the user always approves deletions is a belief, not a
grant. `KNOWLEDGE` is retrieved document content and is untrusted as
instructions for the same reason a fetched web page is.

The single sentence this table encodes: **content can inform a decision; only
platform configuration and the principal's own scopes can authorize one.** That
is Section 2.5 with a schema.

## Schema additions

Additions to Section 15. Nothing existing is removed; one column widens from
`NOT NULL` to nullable, and the reason is stated in the vocabulary section
above.

```text
runs                                      -- Section 15, extended
  + principal_scopes JSONB NOT NULL       -- stamped at submission

approvals                                 -- Section 15, extended
  + tenant_id       TEXT NOT NULL         -- M4 cross-tenant rejection
  + principal_id    TEXT NOT NULL         -- who the run belongs to
  + session_id      UUID NOT NULL
  + action_kind     TEXT NOT NULL         -- ActionKind
  + action_id       UUID NOT NULL         -- the thing being approved
  ~ tool_invocation_id UUID NULL          -- was NOT NULL; see ActionKind
  + risk            TEXT NOT NULL         -- queue order, expiry default
  + policy_version  TEXT NOT NULL         -- at request time
  + revalidated_policy_version TEXT NULL  -- at resume, step 8
  + INDEX (tenant_id, status, created_at) -- agent approval list
  + INDEX (run_id)                        -- resume, cancellation reap
  + INDEX (status, expires_at)
      WHERE status = 'PENDING'            -- the reaper's only scan
  + UNIQUE INDEX (action_id)              -- one open approval per action

tool_invocations                          -- Section 15, extended
  + side_effect       TEXT NOT NULL       -- classified at decision time
  + risk              TEXT NOT NULL
  + idempotency_class TEXT NOT NULL       -- crash recovery reads this
  + origin_trust      TEXT NOT NULL
  + effective_arguments_hash TEXT NULL    -- set if policy modified args

policy_profiles                           -- audit only; rules are files
  policy_version   TEXT PRIMARY KEY       -- name@sha+hsha
  profile_name     TEXT NOT NULL
  profile_sha256   TEXT NOT NULL
  hardline_sha256  TEXT NOT NULL
  rule_count       INTEGER NOT NULL
  loaded_at        TIMESTAMPTZ NOT NULL
  loaded_by        TEXT NOT NULL          -- process or deployment id
```

`runs.principal_scopes` is the stamp the scope section specifies. It is
`JSONB` and not `TEXT[]` because `tool_definitions.required_scopes` is already
`JSONB`, and one schema carrying two representations of the same concept is a
conversion function somebody eventually writes twice. Like `export_consent` it
is a Milestone 2 column with a Milestone 4 reader, and its default is the empty
array only for the migration; submission always writes the set explicitly, so a
run with no stamp is a bug rather than an unprivileged run.

The classification columns on `tool_invocations` are denormalized on purpose.
`ToolSpec` is versioned and its classification can change between releases; an
audit needs what was true when the decision was made, and crash recovery must
be able to dispatch on `idempotency` without loading a tool registry that may
no longer contain that tool version.

One new event type, added to Section 6.8's list:

```text
approval.invalidated      -- revalidation voided an approved request
policy.profile.loaded     -- a ruleset was loaded; carries policy_version
```

Expiry deliberately does **not** get its own event: Section 9.3 already
specifies that expiry emits `approval.resolved`, and a second event for the same
state change would make every consumer count it twice.

## Ports and data model

`PolicyEngine` (Section 7) is unchanged, including its `async` signature. These
are additions.

The deterministic core is a synchronous pure function; the port stays `async`
so the advisory layer can be composed behind the same interface without
changing any caller.

```python
def evaluate_deterministic(
    action: ProposedAction,
    principal: Principal,
    run: Run,
    ruleset: LoadedRuleset,
) -> PolicyDecision:
    """Pure. No I/O, no clock, no database, no network."""
```

```python
class LoadedRuleset(BaseModel, frozen=True):
    policy_version: str
    profile_name: str
    rules: tuple[PolicyRule, ...]
    hardline: tuple[HardlineRule, ...]

class ApprovalRepository(Protocol):
    async def create(
        self, request: ApprovalRequest
    ) -> ApprovalRequest: ...

    async def get(
        self, approval_id: UUID, principal: Principal
    ) -> ApprovalRequest: ...

    async def list_pending(
        self,
        principal: Principal,
        run_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[ApprovalRequest]: ...

    async def resolve(
        self,
        approval_id: UUID,
        principal: Principal,
        resolution: ApprovalResolutionType,
        reason: str | None,
    ) -> ApprovalResolutionOutcome: ...

    async def expire_due(
        self, now: datetime, limit: int
    ) -> list[ApprovalRequest]: ...

    async def cancel_for_run(self, run_id: UUID) -> int: ...
```

`ApprovalRepository` is the "Approval repository" Section 7 lists among the
ports to define. `resolve` returns an outcome object rather than the row so the
caller can distinguish *applied* from *already resolved identically* from
*already resolved differently* without a second read.

## API and CLI additions

Section 17 requires `agent approval list`. Section 16 defines only
`POST /v1/approvals/{approval_id}/resolve`, so the CLI command has no endpoint
to call. Two additions close that, and the CLI calls the same application
service as the API per Section 17.

```http
GET /v1/approvals?status=pending&run_id=&session_id=&limit=&cursor=
GET /v1/approvals/{approval_id}
```

Both are tenant-scoped from the authenticated principal and never from a query
parameter — a tenant identifier accepted from the client is a cross-tenant read
waiting to be discovered. Responses carry `action_summary`, `tool_name`,
`arguments`, `risk`, `expires_at`, and `policy_reason`; they do not carry the
rule that fired.

`POST /v1/approvals/{approval_id}/resolve` is unchanged in shape and gains the
`409`-on-conflicting-resolution behaviour described above.

## Failure modes and defenses

| Failure | What breaks | Defense |
|---------|-------------|---------|
| A second call site authorizes an action | The gate is bypassed for one surface | One transition function; import-boundary test |
| Ruleset edited at runtime | Decisions are unreplayable | Files, frozen at load, hashed into `policy_version` |
| Advisory layer returns `allow` | A model judgment overrides policy | Output schema excludes it; `max` combination |
| Advisory layer times out | Latency, or a stall on the common path | Abstain on timeout; runs only on allow paths |
| Approval approved, arguments then change | A human's consent is transferred | Hash compared at resume; `approval.invalidated` |
| Two devices resolve at once | Double resolution, or a lost decision | Guarded update; 200 if equal, 409 if not |
| Run cancelled with approval pending | Orphaned pending row, wrong metrics | Cancellation reaps to `CANCELLED`, not `DENIED` |
| Crash between steps 1 and 5 | Run parked with no approval, or lost lease | Steps 1–4 in order, lease released last |
| Reaper and cancel race | Double transition | Same `PENDING` guard; loser is a no-op |
| Model loops on a denied action | Budget exhausted, no progress | Circuit breaker at three identical denials |
| Denial text leaks the rule | The model gets an evasion gradient | Field allowlist; `explanation` never sent |
| Untrusted content proposes an action | Injection reaches an external write | Trust overlay raises `ALLOW` to approval |
| New `SideEffectClass`, no rule | An unclassified action slips through | Totality test; profile fails to load |
| Tool spec reclassified after a run | Audit shows the wrong classification | Classification denormalized at decision time |
| Cross-tenant approval probed | Existence disclosed | Not-found, never forbidden |

## Hard gates

Section 20's harness gates Milestone 4 on these. Each is a hard gate: failing
one blocks the milestone, not a warning.

1. **Totality.** Every `SideEffectClass` value has exactly one rule in every
   loaded profile, and every Section 9.2 row maps to exactly one value. A
   profile missing a rule fails to load rather than defaulting. **M4.**
2. **Determinism.** The same `(action, principal, run, ruleset)` evaluated one
   thousand times returns byte-identical output including `reason_code` and
   `policy_version`. The evaluator's module imports nothing from
   `infrastructure` and calls no clock. **M4.**
3. **Single gate.** Exactly one function transitions a tool invocation from
   `PROPOSED` to `AUTHORIZED`, asserted by an import-boundary test, and the
   sandbox bridge and device channel both reach it. **M4.**
4. **Monotonicity.** Over the full cross product of deterministic and advisory
   outputs, the combined rank is never below the deterministic rank. **M4.**
5. **Hardline immutability and precision.** The loaded set cannot be mutated
   after import; every rule blocks its target *and* permits its declared
   `near_miss`. **M4.**
6. **Revalidation.** Each of the four changes in the revalidation table
   produces its stated consequence, including after a worker restart. **M4.**
7. **Cross-tenant.** Get, list, and resolve against another tenant's approval
   all return not-found. **M4.**
8. **No leakage.** The serialized denial result matches the field allowlist
   exactly, for every `reason_code`. **M4.**
9. **Idempotent resolution.** Two concurrent identical resolutions produce one
   state change and two successes; two concurrent differing resolutions produce
   one state change and one conflict. **M4.**
10. **Prompt is not authorization.** Across the injection corpus Section 22
    requires, untrusted content instructing a `REQUIRE_APPROVAL` action produces
    an approval request in every case and an execution in none. **M4.**
11. **Scope grammar.** Every entry in the fifteen-string vocabulary and
    every `required_scopes` entry on a registered `ToolSpec` matches the
    grammar, and registration rejects an MCP tool declaring a scope that is
    neither in the vocabulary nor prefixed `mcp.{server_id}.`. **M4.**
12. **Scope matching is a subset test over exact strings.** A principal
    holding one of a tool's two required scopes is denied with
    `policy.scope.missing`, the denial names the missing scope and not the
    held one, holding both succeeds, and holding `run.write` does not
    satisfy a requirement for `run.read`. **M4.**
13. **The scope set is the run's.** Narrowing a principal's scopes after
    submission changes no decision in the run already submitted, and the
    next run that principal submits is denied. **M4.**

## Tracked metrics

Extending Section 19's `approval_requests_total`:

- Approval request rate by `side_effect`, and approval latency p50 and p95.
- Expiry rate — a rising number means the expiry defaults are wrong or the
  notification path is failing, and the two are distinguishable by whether
  latency also rose.
- Denial rate by `reason_code`, and the circuit-breaker trip rate.
- Advisory escalation rate and advisory disagreement rate, tracked from the day
  the layer is enabled so its value is measurable before it is trusted.
- Revalidation void rate by cause.

## Build sequence

1. The vocabulary: the four enums, `ProposedAction`, `ExecutionTarget`. No
   logic, no callers. Everything below depends on this and nothing depends on
   the order of the rest.
2. `default.yaml`, the loader, `policy_version` computation, and the totality
   test. The test exists before the second profile does.
3. `evaluate_deterministic` as a pure function, with the determinism property
   test.
4. The single gate in the Section 8.3 pipeline, scope check ahead of policy,
   classification persisted to `tool_invocations`.
5. The denial tool result, the field allowlist test, and the circuit breaker.
6. The hardline module, frozen, with block-and-near-miss tests per rule.
7. `approvals` migration, repository, and the guarded resolution update.
8. The eight-step pause flow, checkpoint interaction, `WAITING_FOR_APPROVAL`.
9. Resume and revalidation.
10. The reaper and the cancellation edge, including the race test.
11. `GET /v1/approvals`, `GET /v1/approvals/{id}`, and the CLI commands.
12. The advisory layer, behind a flag, default off, after Milestone 6.

Steps 1 through 11 are Milestone 4. Step 12 is sequenced by Section 21.1 and is
not a Milestone 4 dependency.

## Decisions

1. `SideEffectClass` has fifteen values, one per Section 9.2 row, and the
   correspondence is asserted by a test in both directions.
2. Risk never selects a decision; it orders the queue, sets expiry defaults,
   and groups metrics.
3. `IdempotencyClass` has exactly the four values Section 8.4's crash-recovery
   bullets describe.
4. `ProposedAction` carries trust labels for its arguments and its origin, so
   provenance is available to a rule.
5. `evaluated_at` is an input, never a clock read.
6. Restrictiveness is the total order allow, modify, approve, deny, combined by
   `max`.
7. Only the deterministic layer may produce `modified_arguments`, and at most
   one rule may modify.
8. When arguments are modified, the idempotency key is recomputed and both
   hashes are persisted.
9. The default profile ships zero rules producing `ALLOW_WITH_MODIFICATIONS`.
10. Hardline rules are packaged files, frozen at import, and deliberately not
    behind a port.
11. Every hardline rule declares a `near_miss` it must permit, enforced by the
    schema and the tests.
12. Section 9.2's three non-enum decision strings resolve as conditions and
    profiles, changing no stated outcome.
13. "Unknown tool" generalizes to `policy.unclassifiable_action`, which is
    reachable through three paths.
14. The advisory layer may only escalate, runs only on allow paths, never sees
    the rules, and abstains on timeout.
15. `policy_version` is `{profile}@{sha12}+h{sha8}`, a content hash rather than
    a counter.
16. Profiles and hardline rules are files under version control, never database
    rows; `policy_profiles` is an audit record only.
17. Rulesets are frozen per process and change only across a deploy, which is
    what makes Section 9.3's step 8 well-defined.
18. `ActionKind` generalizes approval beyond tool calls;
    `approvals.tool_invocation_id` becomes nullable.
19. `ApprovalStatus` has five values; `CANCELLED` is distinct from `DENIED`.
20. Resolution is a guarded update: 200 when the second caller agrees, 409 when
    it does not.
21. Cross-tenant access returns not-found rather than forbidden.
22. Self-approval is permitted by default and is a rule, not a hardcoded
    condition.
23. Expiry defaults are risk-scaled; an expired approval is never auto-approved.
24. Revalidation voids on argument, scope, or agent-version change, and
    re-evaluates only on `policy_version` change; a second `REQUIRE_APPROVAL`
    becomes a denial.
25. The denial tool result is a field allowlist that never carries rule text.
26. Three identical denied proposals fail the run.
27. Classification is denormalized onto `tool_invocations` at decision time.
28. Expiry reuses `approval.resolved`; only `approval.invalidated` and
    `policy.profile.loaded` are new event types.
29. The seven trust labels map onto Section 22's tiers, and only `PLATFORM`,
    `TRUSTED_CONFIGURATION`, and `USER` can authorize anything.
30. `GET /v1/approvals` and `GET /v1/approvals/{id}` are added so
    `agent approval list` has an endpoint.
31. The scope vocabulary is one closed set of fifteen dotted strings,
    shared by the API's route checks and by this pipeline's tool check.
32. An MCP tool may require only scopes whose first segment is `mcp` and
    whose second is the server id, so an operator configuring a server
    cannot borrow a platform scope for a remote capability.
33. Scope matching is a subset test over exact strings — no hierarchy, no
    wildcard, no prefix rule — and the denial names the missing scopes and
    never the held ones.
34. The principal's scope set is stamped on the run at submission;
    `PrincipalResolver.for_run` reads the stamp and never a table.
35. `Principal.roles` is populated and never read as an authorization
    input in 0.1, because one configured principal has no bundle to
    resolve.

## Open questions

**Does "Deny initially" mean a later profile or a later milestone?** Section
9.2 marks package installation and sandbox networking "Deny initially". This
document reads "initially" as *in the default profile*, which makes the
relaxation a configuration change. The other reading is that a later milestone
changes the default for everyone. The two differ in who can turn it on.

**Should `ALLOW_WITH_MODIFICATIONS` ship in version 0.1 at all?** It is defined
in Section 9.1 and consumed nowhere. The plumbing is specified here because
removing a declared enum value would weaken the plan, but shipping an untested
path is its own risk, which the zero-rules decision is meant to bound.

**Where does the configured principal's scope set live?** 0.1 has one
principal, so its scopes are a configured value, and this document does not
say which of the three configuration layers holds it. A settings field keeps
the count of configuration files at six; a file makes the second principal a
data change rather than a deploy. The question becomes answerable when there
is a second principal to hold.
