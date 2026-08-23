---
title: Skills
status: design
canonical: true
---

# Procedural memory: the package, the catalog, and the authoring loop

## Eleven references, two acceptance criteria, and no design below them

[readiness.md](readiness.md) counts eleven inbound consuming
references to Section 30 and no expansion under it, and calls the
result "the largest undesigned area in the corpus, and the one with
the most load already resting on it." That is the finding. The shape
of the hole is worth stating more precisely than the count does.

Milestone 8 is named "Skills and MCP integration" and it has eleven
`Implement:` bullets. Every one of them is MCP. The skills half of
the milestone has a directory tree, a manifest example, one sentence
about loading, and two of the six acceptance criteria — *"Skill
metadata can be listed without loading full skill contents"* and *"A
selected skill is version-pinned in the run"* — with nothing between
the criteria and the tree. The second of those is the harder one. It
says a run pins a skill version, and nothing in fifty documents says
what a version of a skill is, where it is stored, or what pinning it
would record.

Below the plan the situation is not empty, and the claim that skills
have no design at all is the one thing in the readiness verdict this
document contradicts. [tool-system.md](tool-system.md) has a section
called "Skills, and the line between a skill and a tool" that settles
four questions properly: the agent may author skills but may not
register tools, the advertised metadata block is exactly four fields
and nothing else, `required_tools` is checked at load rather than at
authoring, and skill content is untrusted unless the platform or a
trusted operator wrote it. [context-engine.md](context-engine.md)
answers a fifth in its open questions — a skill body is Region B
content and is sticky for the session once selected.
[policy-and-approvals.md](policy-and-approvals.md) already carries
`ActionKind.SKILL_AUTHORING` so that an approval can hang off a skill
write without fabricating a tool invocation.
[runtime-loop.md](runtime-loop.md) already has the post-run hook that
enqueues the background review and already says its failure is logged
and never fatal.

So five documents have written the edges of a shape whose middle does
not exist. What is missing is everything with a name in it: the
package and what makes one invalid, the manifest schema, a skill's
identity, what a revision is and how it differs from the `version`
string the author types, where the bytes live, how the catalog is
built and bounded, what `skill.load` returns, what a run records so
that a replay resolves the same instructions, what `skill_manage`
actually is — the corpus calls it a control tool and, as classified,
it cannot be one — and which milestone each half belongs to.

This document supplies that middle: seven types, two tables, one
archive, two new context classes, a reference grammar, sixteen gates
in a new thirteenth area, and the split that gives Milestone 8 and
Milestone 10 the gates they currently do not have.

## What this document does not change

Section 30 remains the statement of what skills are for and why a
multi-tenant platform can let a model write its own instructions.
This document is subordinate to it the way
[sandbox-isolation.md](sandbox-isolation.md) is subordinate to
Section 28: where the two overlap, Section 30's sentence is the
requirement and this document's is the mechanism.

Specifically unchanged: skills are procedural memory and not a new
code tool; the agent may create and edit skills and may not register
arbitrary tools at runtime; the agentskills.io format is adopted;
authoring is a consequential action requiring scope and, by policy
profile, approval; every agent-authored version is pinned per run and
provenance-linked; the background review is a restricted child run
that reads before writing and may edit only skills it created; skill
authoring is confined to trusted turns; any executable a skill
carries runs in the sandbox under normal policy; only metadata enters
ordinary context and full instructions load on selection; skills are
tenant- and principal-scoped; and rollout of the authoring path is
gated on evaluation evidence.

Unchanged from [tool-system.md](tool-system.md): the two sources the
tool registry accepts, the four-field metadata block, the load-time
`required_tools` check with its `skill.tool.missing` note, the
`TRUSTED_CONFIGURATION`/`EXTERNAL_UNTRUSTED` split by author, and the
rule that MCP prompts register as read-only skills.

Unchanged from [context-engine.md](context-engine.md): the two
regions and the one rule that assigns items to them, the prefix epoch
mechanism, the advertise-versus-authorize separation, and the
assignment of skill bodies to Region B with session stickiness.

Three things this document does change in other specs, each recorded
under [Contradictions resolved](#contradictions-resolved): the
classification of `skill_manage`, the spelling of the scope it
requires, and the prefix token ceiling.

## A skill is text, a tool is code, a memory is a fact

Section 30.1 draws two lines in one sentence and both matter for
different reasons, so it is worth separating them.

**A skill is not a tool.** A tool is code the platform runs; a skill
is text the model reads. The distinction is not stylistic. A tool
enters the registry, gets a policy classification, passes the
pipeline, and can be denied at call time. A skill enters the catalog,
gets a trust label, and changes what the model decides to do with the
tools it already has. `tool-system.md` states the consequence: the
registry accepts entries from the build and from MCP discovery at
session open, and skill installation is neither. A skill that ships a
script does not thereby gain a tool — the script is a file, and
running it is a `sandbox.run_command` call like any other.

**A skill is not a memory.** Memory is declarative: facts about a
user, a project, or the world, formed autonomously by the loop in
[memory-formation-and-consolidation.md](memory-formation-and-consolidation.md)
and retrieved by relevance. A skill is procedural: a named, versioned,
reviewable document about how a class of task is done. They differ in
every operational property. A memory is written without asking, is
scored, decays, and can be contradicted by a later observation. A
skill is written deliberately, is versioned, does not decay, and is
replaced rather than contradicted. They share a governance instinct
and nothing else, and the two subsystems have no code in common.

**A skill is also not a prompt fragment.** The agent instructions in
Region A are configuration: one document, pinned per run through
`AgentSpec`, present in every request of the session. A skill is
selected. The catalog advertises twenty of them and the model reads
none until it decides one is relevant. That difference is the entire
reason skills exist as a mechanism — it is how an agent can know
about forty procedures while paying for none of them.

## The package

The package is a directory. Section 21's Milestone 8 tree names five
entries and this adopts it unchanged, adding only the rules that make
a package valid or invalid.

```text
skills/<name>/
  SKILL.md          required. front matter + instructions
  references/       optional. loaded by path, on request
  templates/        optional. files a procedure produces from
  scripts/          optional. run through sandbox.run_command
  evals/            optional. cases in the harness fixture format
```

`SKILL.md` is the only required member and the only one loaded
automatically. Everything else is inert until something asks for it
by path, which is what makes a package with a large `references/`
tree cost nothing until it is used.

`evals/` holds cases in the format
[evaluation-harness.md](evaluation-harness.md) already defines. They
are not run by the platform at load. They exist so that a skill
carries the evidence for the claim Section 30.5 makes rollout
conditional on, and so that the evidence travels with the thing it is
evidence for.

### `SKILL.md` and its front matter

The manifest is YAML front matter, exactly as Milestone 8's example
writes it, followed by the instructions as Markdown.

```text
---
name: repository-analysis
version: 1.0.0
description: Analyze a code repository's structure, dependencies,
  and test coverage, and produce a written summary.
required_tools:
  - workspace.read_text
  - workspace.list_files
  - sandbox.run_command
---

# Instructions

...
```

Four fields, and the set is closed. They are the same four
`tool-system.md` calls the advertised metadata block, and the reason
the manifest carries nothing else is that everything in the manifest
is advertised — a field the catalog does not show would be a field
whose only purpose is to be read by the platform, and the platform
already has a database.

`name` is the skill's identity within a tenant. `version` is the
author's semantic version string and is documentation. `description`
is what selection runs against and is the field that most determines
whether a skill is ever used. `required_tools` is advertised because
it lets the model see, before loading, that a skill cannot run in
this session.

### What the validator rejects

Installation is total: a package either produces a revision or raises
a named error, and there is no third outcome and no partial install.
The rules are numbers so that a rejection can say which one.

```text
field           rule                                       on failure
--------------  -----------------------------------------  ----------
name            [a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?         reject
name            equals the directory name                  reject
name            unique among the tenant's active skills    reject
version         parses as semver                           reject
description     1 to 500 characters, no line breaks        reject
required_tools  0 to 10 entries, each a dotted tool name   reject
body            1 to 3,000 tokens after front matter       reject
metadata block  renders to 75 tokens or fewer              reject
package         64 files or fewer, 1 MiB or less           reject
package         no symlink, no path escaping the root      reject
scripts/        executable bit ignored; never inspected    --
```

The metadata rule is the one that will surprise an author, and it is
there because the catalog has a fixed token budget and twenty
entries. A skill whose description is 500 characters of prose will
render past 75 tokens and be rejected with the measured number, which
is a better failure than a catalog that silently drops it.

The body limit of 3,000 tokens is half the Region B allowance for
loaded skills, which is what makes the two-skill cap below hold by
arithmetic rather than by hope. A procedure genuinely longer than
3,000 tokens splits: the entry point is `SKILL.md` and the detail is
a file under `references/` that the instructions tell the model to
load when it reaches that step. That is the format working as
intended rather than a limit being worked around.

Symlinks are rejected rather than resolved. A package is extracted
into a sandbox workspace when a script runs, and a symlink is the
cheapest way to turn an extraction into a write outside the root.

## Identity, revisions, and the reference grammar

A skill has two version-shaped things and conflating them is the
mistake this section exists to prevent.

`version` is the string the author writes in the front matter. It is
semver, it is advisory, it is not checked for monotonicity, and two
revisions may carry the same one. It exists because the agentskills.io
format has it and because a human reading a catalog wants it.

`revision` is an integer the platform assigns. It starts at 1 for a
skill's first installed package and increments by one for every
subsequent package under the same name, in the same tenant, forever.
It is never reused, never reordered, and never derived from anything
the author controls.

Pinning uses `revision`. Section 30.3 requires that every version be
"pinned per run, exactly like AgentSpec", and `AgentSpec` is pinned
by an integer version for a reason that applies here without change:
a pin has to be totally ordered and has to be unforgeable by whoever
wrote the content. A semver string is neither.

References are strings, and the grammar is two forms.

```text
<name>              float: the newest ACTIVE revision at session open
<name>@<revision>   pin:   exactly that revision, ACTIVE or not
```

`AgentSpec.enabled_skills` is a list of these, which is why that field
could already exist at `engineering-plan.md:472` with no design behind
it and still be right. A floating reference is what an operator wants
for a skill they maintain; a pinned reference is what an operator
wants after a bad revision, and it is also the whole of the rollback
story — see
[Rollback is an `AgentSpec` edit](#rollback-is-an-agentspec-edit).

A reference naming an unknown skill, or a revision that does not
exist, fails at session open with the reference quoted. It does not
resolve to nothing and it does not warn. An agent configured with a
procedure it does not have is a different agent, and starting it
anyway is the class of degradation the plan refuses everywhere else.

## The types

Seven declarations. None of them exists in the corpus today — unlike
`sandbox-isolation.md`, which filled in eight types other documents
had already named, this document is naming them for the first time.
Nothing referenced-and-undeclared is being resolved here, which is
also why nothing else has to be edited to accommodate them.

### `SkillSource` and `SkillStatus`

```python
class SkillSource(str, Enum):
    BUILTIN = "builtin"      # ships in the platform image
    OPERATOR = "operator"    # installed by a tenant operator
    AGENT = "agent"          # written by skill_manage
    MCP = "mcp"              # a server prompt, seen at session open


class SkillStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
```

`SkillSource` is a different enum from `ToolSource` and shares two
member names with it on purpose. A skill's source is a property of
the skill's identity, not of a revision: a skill created by an
operator is an operator skill forever, and there is no operation that
moves one between sources. That single rule is what stops
`skill_manage` from taking over a platform skill by writing a
revision of it, and it is enforced by the column rather than by a
check, because the column is on the identity table.

`ARCHIVED` hides a revision from floating references and from the
catalog. It does not delete it. A pinned reference to an archived
revision still resolves, which is deliberate: archiving is how an
operator stops new sessions from picking something up, and breaking
the runs that already pinned it would make archiving a dangerous
operation rather than a routine one.

There is no `DRAFT`. A revision exists or it does not, and the
authoring loop's intermediate states live in the authoring run, not
in the table.

### `SkillManifest`

```python
@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    description: str
    required_tools: tuple[str, ...]
```

Four fields, frozen, and the same four the catalog renders. It is a
parse of the front matter and holds nothing derived.

### `SkillRevision`

```python
@dataclass(frozen=True)
class SkillRevision:
    skill_id: SkillId
    tenant_id: TenantId
    revision: int
    manifest: SkillManifest
    body: str
    body_tokens: int
    content_sha256: str
    package_key: str
    package_bytes: int
    file_count: int
    source: SkillSource
    trust: TrustLevel
    status: SkillStatus
    authored_by_run_id: RunId | None
    authored_by_principal_id: PrincipalId | None
    authored_by_invocation_id: InvocationId | None
    authoring_idempotency_key: str | None
    archived_by_invocation_id: InvocationId | None
    archive_idempotency_key: str | None
    created_at: datetime
```

`body` is `SKILL.md`'s Markdown, denormalized out of the archive so
that loading a skill is a row read rather than an object-store fetch
and a decompression. It is bounded at 3,000 tokens by the validator,
which is what makes the denormalization safe. Everything else in the
package stays in the archive and is fetched by path on request.

`content_sha256` is the digest of the whole archive, not of `body`.
It is what a pin records and what a replay compares, so it has to
cover every file a run could have read.

`trust` is derived from `source` at install and stored, rather than
computed at load. The mapping is two lines and it is the whole of
`tool-system.md`'s "untrusted unless it is ours" rule:
`BUILTIN` and `OPERATOR` become `TRUSTED_CONFIGURATION`, `AGENT` and
`MCP` become `EXTERNAL_UNTRUSTED`. It is stored because a trust label
that is recomputed is a trust label that can be recomputed
differently after a code change, and every event that referenced the
old value silently becomes wrong.

The two `authored_by` fields are null for `BUILTIN`, `OPERATOR`, and
`MCP` revisions and non-null for every `AGENT` revision. That is
Section 30.3's provenance requirement stated as a column and checked
as a gate; the run id is the link into the event log, which already
holds everything that run saw.

### `SkillRef` and `SkillPin`

```python
@dataclass(frozen=True)
class SkillRef:
    name: str
    revision: int | None      # None floats to newest ACTIVE

    @classmethod
    def parse(cls, text: str) -> "SkillRef": ...

    def __str__(self) -> str: ...


@dataclass(frozen=True)
class SkillPin:
    name: str
    revision: int
    content_sha256: str
```

A `SkillRef` is what configuration holds and a `SkillPin` is what a
run records. Resolution happens once, at session open, and turns
every ref into a pin. After that point the session has no floating
references and no way to acquire one.

`SkillPin` carries the hash as well as the revision because the
revision alone proves less than it looks like it does. A revision is
immutable by rule; the hash is what makes the rule checkable, and a
replay that resolves revision 7 and finds different bytes should fail
loudly rather than proceed.

### `CatalogEntry`

```python
@dataclass(frozen=True)
class CatalogEntry:
    manifest: SkillManifest
    revision: int
    trust: TrustLevel
```

What one row of the Region A catalog is built from. It carries no
availability flag for `required_tools`: the tool definitions are in
the same prefix, a few hundred tokens above, and the model comparing
two lists it can both see is more reliable than the platform
computing a boolean whose meaning it would then have to explain.

### `AuthoringContext`

`install` takes the provenance to record rather than reading it from ambient
state, because the composition root is the only place that knows both the run
and the principal, and a repository that reached for either would be reaching
outside its layer.

```python
@dataclass(frozen=True)
class AuthoringContext:
    """Who wrote a revision, for `SkillSource.AGENT` only."""

    run_id: RunId
    principal_id: PrincipalId
    invocation_id: InvocationId
    idempotency_key: str
```

It carries the authoring run and principal plus the invocation identity and the
pipeline's canonical-argument idempotency key. It is non-null when and only
when `source is SkillSource.AGENT`. At creation, all four corresponding revision
fields are non-null and the run foreign key resolves to the events that produced
the revision. The governed session-erasure flow may later null only
`authored_by_run_id`; principal, invocation, and idempotency-key provenance
remain durable so erasure does not make the revision anonymous or unreplayable.
Archive stores a separate invocation-and-key pair on the archived revision so a
crash after that effect can replay safely without overwriting creation
provenance.

### `SkillRepository` and `SkillPackageStore`

```python
class SkillRepository(Protocol):
    def install(
        self,
        tenant_id: TenantId,
        package: SkillPackage,
        source: SkillSource,
        expected_revision: int | None,
        authored_by: AuthoringContext | None,
    ) -> SkillRevision: ...

    def resolve(
        self, tenant_id: TenantId, ref: SkillRef
    ) -> SkillRevision: ...

    def list_active(
        self, tenant_id: TenantId, limit: int
    ) -> list[SkillRevision]: ...

    def archive(
        self,
        tenant_id: TenantId,
        name: str,
        revision: int,
        authored_by: AuthoringContext | None,
    ) -> SkillRevision: ...


class SkillPackageStore(Protocol):
    def put(
        self,
        tenant_id: TenantId,
        skill_id: SkillId,
        revision: int,
        archive: IO[bytes],
    ) -> str: ...

    def open_member(self, key: str, path: str) -> bytes: ...
```

`install` is the only writer and it is where validation, revision
assignment, archive upload, and the row write happen in one
transaction. `expected_revision` is optimistic concurrency and is
covered under [the authoring loop](#the-authoring-loop-is-milestone-10);
for `BUILTIN` and `OPERATOR` installs it is `None`.

`open_member` takes a path rather than returning a stream of the
whole archive because every caller wants one file. Returning the
archive would put extraction in four places and would make the path
check — which is the only thing standing between a skill and a
traversal — a caller's responsibility.

`SkillPackageStore` is deliberately not `ArtifactStore`.
[sandbox-isolation.md](sandbox-isolation.md) gives artifacts a
thirty-day expiry, a per-run lifecycle, and deletion by `expires_at`.
A skill is configuration. It has no expiry, it outlives every run
that used it, and deleting one because it is old would break the pin
that made a run reproducible. The two share an object-store adapter
and share no policy, and that is the correct amount of sharing.

## Storage: two tables, one archive, no expiry

```text
skills
  + id                         UUID PK
  + tenant_id                  UUID NOT NULL
  + name                       TEXT NOT NULL
  + source                     TEXT NOT NULL
  + created_at                 TIMESTAMPTZ NOT NULL
  + UNIQUE (tenant_id, name)

skill_revisions
  + id                         UUID PK
  + skill_id                   UUID NOT NULL -> skills(id)
  + revision                   INTEGER NOT NULL
  + version                    TEXT NOT NULL
  + description                TEXT NOT NULL
  + required_tools             JSONB NOT NULL
  + body                       TEXT NOT NULL
  + body_tokens                INTEGER NOT NULL
  + content_sha256             TEXT NOT NULL
  + package_key                TEXT NOT NULL
  + package_bytes              BIGINT NOT NULL
  + file_count                 INTEGER NOT NULL
  + trust                      TEXT NOT NULL
  + status                     TEXT NOT NULL
  + authored_by_run_id         UUID NULL
  + authored_by_principal_id   TEXT NULL
  + authored_by_invocation_id  UUID NULL
  + authoring_idempotency_key  TEXT NULL
  + archived_by_invocation_id  UUID NULL
  + archive_idempotency_key    TEXT NULL
  + created_at                 TIMESTAMPTZ NOT NULL
  + UNIQUE (skill_id, revision)
  + UNIQUE (authored_by_invocation_id) WHERE authored_by_invocation_id IS NOT NULL
  + UNIQUE (archived_by_invocation_id) WHERE archived_by_invocation_id IS NOT NULL
```

`source` sits on `skills` and not on `skill_revisions`, which is the
identity rule from above expressed as normalization. `trust` sits on
`skill_revisions` even though it is derived from `source`, because it
is the value that was in force when that revision was written and a
stored copy is what makes an old event's label verifiable.

A revision is never updated after insert except for archiving. Archiving writes
`status`, `archived_by_invocation_id`, and `archive_idempotency_key`; those are
the only fields any `UPDATE` in this subsystem changes.

The archive key is derived and never composed:

```text
skills/{tenant_id}/{skill_id}/{revision}.tar.zst
```

Three platform-controlled values and a constant, which is the same
rule `sandbox-isolation.md` applies to artifact keys and for the same
reason: a skill name is author-supplied, a skill name in a storage
path is a traversal, and the two facts meet at exactly this line.
The file names inside the archive are author-supplied and are checked
on extraction rather than on the way in, because a check on the way
in has to be repeated by every reader anyway.

Nothing sweeps this store. There is no retention job, no
`expires_at`, and no reference counting. The bytes for revision 3 of
a skill archived two years ago are still there, because a run from
two years ago pinned them and the audit story requires that the pin
still resolve.

## The catalog is pinned at session open

The catalog is the list of skills the model can see. It is built once
per session, from the agent's `enabled_skills` and the MCP prompts
discovered at session open, and it does not change for the life of
the prefix epoch.

That is the same rule
[context-engine.md](context-engine.md) already applies to tools —
"the tool set is resolved once at session open and pinned" — and it
is adopted here for the same two reasons and one more.

The two shared reasons: the prefix must be byte-stable, and a set
that can change mid-session cannot be in it; and the model reasons
about what it can see, so a set that shrinks under it produces
confident references to things that no longer exist.

The third reason is specific to skills and is the more important one.
`skill_manage` writes skills. If the catalog were live, an agent
could write a skill in one turn and load it in the next, inside the
same session, with the same context that produced it. That is a loop
with no human anywhere in it and no observation point in the middle,
and it is precisely the thing Section 30.3's governance list exists
to prevent. **A skill created or edited by `skill_manage` does not
enter the session that created it.** It enters the next one, after
the write is durable, after the approval it may have required, and
after whatever review the deployment applies to a new revision.

Building the catalog is deterministic, and it has to be, because a
non-deterministic prefix is a broken prefix:

1. Resolve every reference in `enabled_skills` to a revision, in the
   order the references are written. A reference that does not
   resolve fails the session open.
2. Append MCP prompt skills, in server-registration order, then in
   the server's declared prompt order.
3. Truncate at twenty entries. Entries dropped by the cap are
   recorded on the session-open event with their names, and the
   count is a tracked metric.
4. Render, and assert the rendered block fits the token cap.

Truncation is by configured order and never by a score. A relevance
ranking over the catalog would be a second selection mechanism
sitting underneath the model's own, invisible to it, and the first
question anyone would ask about a skill that failed to fire is which
of the two dropped it.

## Two new context classes and a ceiling that moves

Skills need one class in each region, and the region assignment was
decided before this document existed:
[context-engine.md](context-engine.md) put skill bodies in Region B
and made them sticky. Metadata goes to Region A because it is
resolved at session open, cannot differ between two requests in the
same session, and is exactly the kind of item the prefix is for.

The assembly-order table gains two rows and renumbers from five:

| # | Region | Content | Trust |
| --- | --- | --- | --- |
| 5 | A | Skill catalog (pinned at session open) | per entry |
| 9 | B | Loaded skill bodies, in load order | per skill |

The catalog sits directly after the tool definitions because that is
what it is adjacent to in meaning: here is what you can do, and here
is what you know how to do. Loaded bodies sit after the retained
conversation and before the working-state block, which puts a
procedure the model chose in front of the state it is applying that
procedure to.

Trust is per entry rather than fixed, like the retained-items row
above it. A catalog entry for a `BUILTIN` or `OPERATOR` skill renders
as `TRUSTED_CONFIGURATION`. An entry for an `AGENT` or `MCP` skill
renders as `EXTERNAL_UNTRUSTED` and is enveloped by the same
mechanism every other untrusted span uses. **The body of an untrusted
skill is never in Region A under any circumstance**, which is the
`tool-system.md` rule preserved exactly; what this document adds is
that the *metadata* of an untrusted skill may be, because a
description is data the model is comparing rather than instructions
it is following, and it is enveloped as such.

The budget table gains two rows:

| Region | Class | Cap | Scales | Yields |
| --- | --- | --- | --- | --- |
| A | Skill catalog | 20 skills / 1,500 tokens | No | Only at an epoch boundary |
| B | Skill bodies | 2 loaded / 6,000 tokens | No | Never — the load fails instead |

**The prefix ceiling moves from 13,500 to 15,000 tokens.** The
ceiling is the sum of the Region A classes, not an independent
number, so adding a class raises it; nothing about the failure
behaviour changes, and a plan that exceeds the new ceiling still
fails the session open with the offending class named. This is the
one number in another document that this one edits, and it is edited
because the alternative — taking 1,500 tokens from the tool
definitions or the memory snapshot to keep the total round — would
be paying for a skill catalog with capacity that was sized for
something else.

The item cap is the primary limit for the catalog, as it is for tools
and for the memory snapshot, and for the reason
`context-engine.md` already gives: selection accuracy degrades with
the number of candidates rather than with their token weight. Twenty
is chosen against that sentence rather than against the token budget,
and the 75-token metadata rule in the validator is what makes twenty
and 1,500 consistent.

Skill bodies never yield, and that is safe only because they are
bounded by construction. Two loaded skills at 3,000 tokens each is
6,000, the validator enforces the 3,000, and a third `skill.load`
fails rather than growing the plan. A class that yields is a class
whose content can disappear between two requests; a procedure the
model is halfway through executing is the worst possible thing to
have disappear, and the failure it produces — steps performed out of
order, from memory, with confidence — is one nobody would diagnose
correctly.

## Loading: `skill.load`, and what sticks

`skill.load` is a control tool and stays exactly what
`tool-system.md` classifies it as: `side_effect: NONE`,
`idempotency: IDEMPOTENT`, `target_kind: in_process`, in the closed
build-time set. It acts on the run and on nothing else, which is what
a control tool is.

```text
skill.load
  name    required. must name a catalog entry
  path    optional. a file inside the package

returns
  content       the text
  revision      the pinned revision
  trust         the label the content is enveloped with
  missing_tools names from required_tools not pinned this session
```

With no `path` it returns `SKILL.md`'s body from the row. With a
`path` it returns that member of the archive, which is how
`references/` becomes useful without costing anything until it is
needed. Both count against the Region B allowance, and a `path` load
does not require the body to have been loaded first — a skill whose
instructions were read three turns ago does not have to be re-read to
reach a file it mentions.

Four rules, and each of them exists because of a specific failure:

1. **A name not in the catalog fails**, and the failure lists the
   catalog. The model cannot load a skill it cannot see, and telling
   it what it can see is cheaper than a retry loop. When the pinned
   catalog is empty, the context plan does not advertise `skill.load`;
   a control tool with no valid argument must not invite guessed names.
   The background-review child retains its exact restricted tool
   allowlist because its confinement contract names `skill.load` even
   when the source agent currently pins no skills. Because a session
   keeps the exact tool version it was shown, the registry retains
   each superseded `skill.load` version as a compatibility
   registration — the same rule `memory.remember` already follows —
   so a process upgrade cannot turn an advertised control tool into
   an unknown capability for an existing session.
2. **A load is sticky for the session.** Once loaded, the content
   stays in Region B for every subsequent request until the run ends
   or it is unloaded. This is `context-engine.md`'s answer and its
   reason is caching: content that appears and disappears between
   turns invalidates the body's cache breakpoint every time it moves.
3. **A third load fails, and names the two that are loaded.** It does
   not evict. Eviction would pick, and the picker would be wrong at
   the moment it mattered; failing hands the choice to the only party
   that knows which procedure is finished.
4. **Unloading is `skill.load` with `unload: true`.** It is the same
   control tool because unloading is not a different kind of act, and
   a separate `skill.unload` would be a fifth entry in a set that is
   closed at build time and should stay small.

Loading the same name twice is a no-op that returns the same content,
which is what `IDEMPOTENT` means and what makes the tool safe to
retry after a crash.

## `required_tools` is checked at load, and the check is a note

This is `tool-system.md`'s rule and it is restated here only to say
where the note goes.

A skill naming a tool the session did not pin loads anyway. The names
that are missing come back in `missing_tools` and are recorded as
`skill.tool.missing` on the tool-invocation record. The pipeline
still denies the call if the model tries the missing tool, so nothing
is weakened; what moves is the point at which the problem is visible,
from "after the model wrote three steps against a tool that is not
there" to "at the moment it read the instructions".

Failing the load instead would make tool filtering silently disable
skills, and tool filtering happens for four separate reasons, one of
which is the 30-tool cap. A skill that lost its optional third tool
to a cap is usually still worth reading.

## A skill that ships a script does not ship a tool

`scripts/` holds files. Running one is a `sandbox.run_command` call,
made by the model, through the full pipeline, under the policy
profile in force, with the same egress rules and resource limits as
any other command. There is no skill-specific execution path and
there is no skill-specific exemption.

The mechanics are the part worth writing down. A skill's package is
extracted into the run's workspace under a reserved directory when
the first script from it is requested:

```text
<workspace>/.skills/<name>/
```

Extraction is subject to `WorkspaceHandle`'s containment rule, which
is the property gated at Milestone 4, so an archive member whose path
escapes the root raises `WorkspaceEscape` and the extraction fails
whole. The reserved directory is created by the platform, not by the
model, and a `workspace.write_text` targeting a path under `.skills/`
is denied — a skill that could rewrite its own scripts mid-run would
make the pinned `content_sha256` a claim about the past rather than
about what executed.

The directory is inside the workspace and therefore inside the
lease, which means it is destroyed with everything else when the
lease ends. A resumed run re-extracts from the archive, and because
the archive is immutable and the revision is pinned, it re-extracts
the same bytes.

## MCP prompts are skills, and they are read-only

`tool-system.md` already decides this: a prompt advertised by an MCP
server registers as a skill with `source: mcp`, is
`EXTERNAL_UNTRUSTED`, and is not editable by the self-improvement
path. Three mechanical consequences follow and belong here.

**They are not installed.** An MCP skill has no archive, no
`package_key`, and no row in `skill_revisions` that outlives the
session. It is discovered at session open, held for the session, and
discarded. Persisting a remote server's prompt would create a skill
whose content the platform cannot reproduce and whose revision number
would imply a history that does not exist.

That makes MCP the one source whose entries are session-scoped, and
it is worth being explicit that this does not break pinning: the pin
for an MCP skill records the revision as `0` and the
`content_sha256` of the text as it arrived, so a replay can detect
that the server has changed its prompt even though the platform never
stored the old one.

**The store refuses to write them.** `SkillRepository.install` raises
on `source: mcp` regardless of caller, which is a store-level
invariant rather than a check inside `skill_manage`, because there
will eventually be a second writer and the invariant should not have
to be remembered twice.

**They compete for the same twenty slots.** A tenant with three MCP
servers advertising eight prompts each has consumed the catalog. The
truncation order puts configured skills first and MCP prompts after,
which means the cap degrades the newer and less deliberate source
first. The drop count is a tracked metric because a tenant hitting
this will experience it as skills that stopped working.

## What a run records

`ContextPlan` gains one field:

```python
skill_pins: tuple[SkillPin, ...]
```

The catalog as resolved at session open, in catalog order. It is part
of the plan for the same reason `tool_names` is: it is what the
prefix was built from, and a plan that did not record it could not
explain its own `prefix_sha256`.

Three events carry skill information, and none of them is new:

```text
session.created       skill_pins, entries dropped by the cap
tool.call.completed   for skill.load: name, revision, path,
                      trust, missing_tools, bytes returned
model.request.started prefix_sha256 already covers the catalog
```

That is the whole of the observability surface for the substrate, and
it is deliberately thin. The interesting question about a skill is
whether the run that loaded it went better, and that question is
answered by the harness and by the tracked metrics below, not by a
per-skill event vocabulary.

**A replay resolves pins, never references.** Replaying a run reads
`skill_pins` from the plan, resolves each to its revision, and
compares `content_sha256`. A mismatch raises rather than proceeding.
This is the mechanism behind Milestone 8's second acceptance
criterion, and it is the reason `revision` had to be a platform
integer rather than the author's semver string.

## The authoring loop is Milestone 10

Everything above is Milestone 8 and none of it lets the agent write
anything. That split is the largest scheduling decision in this
document and it follows from two sentences the plan already contains.

Section 21.1's readiness table gives self-improving skills the entry
*after M8 \*, gated by eval evidence, carries risk*, and Section 21's
prose says they *"stay behind the static-skill substrate and
evaluation evidence (after Milestone 8)"*. So the authoring path is
already deferred past Milestone 8; what the plan does not say is
where it lands. Milestone 10 is the answer because the background
review is a child run, child runs are Milestone 10's subject, and
putting the foreground half in one milestone and the background half
in another would mean shipping `skill_manage` with half of its
governance and none of its second caller.

Milestone 8 therefore ships the substrate **and every refusal that
guards it**: the store's rejection of MCP writes, the catalog's
session-open pinning, the workspace denial on `.skills/`, and the
whole validator. A milestone that shipped the reading path and left
the refusals for the writing milestone would be a milestone whose
security properties were untested for two milestones.

### `skill_manage` is a capability tool, not a control tool

Section 30.2 at `engineering-plan.md:3834` calls it *"a skill_manage
control tool"*, and an earlier draft of `tool-system.md` repeated that
classification while also giving `skill_manage`
`idempotency: NON_IDEMPOTENT`. The registration rule at
`tool-system.md:240` requires every control tool to be `READ_ONLY` or
`IDEMPOTENT` and to carry `side_effect: NONE`. As written,
`skill_manage` would have been rejected at registration by the spec
that declared it.

The contradiction resolves in the direction that makes the
classification honest rather than the constraint softer. **A control
tool acts on the run. `skill_manage` acts on durable tenant state
that outlives the run.** It is a capability tool, and it was never in
`tool-system.md`'s control-tool table — that table has four entries
and `skill_manage` is not one of them. The table was right and the
sentence was wrong, and `tool-system.md:1347` carries the corrected
sentence now.

```text
skill.manage
  kind          CAPABILITY
  target_kind   in_process
  side_effect   EXTERNAL_WRITE
  idempotency   CONDITIONALLY_IDEMPOTENT
  risk          HIGH
  scope         skill.write
```

The registry name carries the dot the grammar at `tool-system.md:340`
requires of every registry entry; `skill_manage` is Section 30.2's
spelling and is not a name the registry can hold. Nothing else about the
tool changes with it, and the rest of this document keeps the plan's
spelling in prose where the string does not matter.

`side_effect: EXTERNAL_WRITE` is chosen from the closed fifteen
rather than added to them; the enum is total against Section 9.2's
matrix and a test asserts the correspondence in both directions, so
adding a value would be editing the matrix. `EXTERNAL_WRITE` is the
value that means "durably modify something outside this run", which
is what a skill write is. Archiving is also `EXTERNAL_WRITE` and not
`EXTERNAL_DELETE`, because archiving deletes nothing.

`CONDITIONALLY_IDEMPOTENT` is the classification whose comment in
`policy-and-approvals.md` reads *"key required"*. The key is the tool
invocation identity together with the canonical request-argument hash, as the
ordinary tool pipeline already derives it. `expected_revision` is a separate
compare-and-swap precondition: two distinct edits may both expect revision 7
and must not deduplicate to one result. A repeated invocation with the same
arguments replays safely; a different edit against the stale revision receives
`SkillRevisionConflict`. This is better than `NON_IDEMPOTENT` because a crash
after the durable write remains recoverable instead of becoming permanently
`UNCERTAIN`.

### Four operations

```text
create   a new name. revision 1. fails if the name exists
edit     a full package. revision n+1
patch    SKILL.md only; the rest of the archive carries forward
archive  status := ARCHIVED. writes no revision
```

Section 30.2 lists five — *create / edit / patch / version / archive*
— and `version` is not an operation here because every successful
`create`, `edit`, and `patch` produces a new revision. Versioning is
the outcome of the other three rather than a fifth call, and a
`version` operation that bumped a number without changing content
would create a revision whose `content_sha256` equals its
predecessor's, which is a row that means nothing.

`patch` exists because it is the common case and the cheap one. The
agent that just finished a hard task has learned something about the
instructions, not about the templates, and making it re-upload a
package to change a paragraph would make the loop expensive enough
that it would not run.

### `expected_revision`, and the edit that loses

Every `edit` and `patch` carries the revision the caller believes is
current. If it is not, the call fails with `SkillRevisionConflict`
carrying the actual current revision, and nothing is written.

Two runs refining the same skill concurrently is not a hypothetical.
It is the normal case for a background review, which runs after a
parent completes and therefore runs at exactly the moment other runs
are also completing. Last-write-wins here means one agent's learning
silently overwrites another's, with both runs reporting success and
the event log showing two writes and one survivor.

The losing caller gets the conflict, the current revision, and — for
a background review — nothing else, because there is no sensible
automatic merge of two prose revisions and attempting one is how a
skill acquires a paragraph that contradicts the paragraph above it.
It retries by reading the new current revision and deciding again.

### The scope is `skill.write`

An earlier draft used the `skills:write` scope. The current contract at
`tool-system.md:1351` requires `skill.write`. The earlier string was wrong in
two ways against
[http-api-and-streaming.md](http-api-and-streaming.md), which
enumerates the scope vocabulary as dotted `resource.action` strings
matched by exact string equality with no wildcard, no prefix rule,
and no hierarchy. `skills:write` uses a colon rather than a dot, and
it is not in the enumeration. A corpus-wide scan finds it is the only
colon-separated scope string anywhere in fifty documents, which makes
it a typo with a long life rather than a second convention.

It was corrected to `skill.write` and joins the enumeration:

```text
session.read      session.write
run.read          run.write        run.cancel
approval.read     approval.resolve
artifact.read
skill.write
```

Singular `skill`, because every other resource in the list is
singular. One scope and not two: nothing reads skills over the API in
0.1, and a `skill.read` with no route and no tool behind it would be
a scope that could never be checked. The catalog is built during
session open and is covered by `session.write`, which is the scope
that already authorizes opening a session.

### Authoring is confined to trusted turns

Section 30.3's injection-resistance bullet becomes one policy rule,
and `tool-system.md` already states its mechanism: `skill_manage` is
**denied when `origin_trust` is below `USER`**. An agent whose turn
has read untrusted content — a web page, an MCP result, another
skill's body — cannot write a skill in that turn.

`origin_trust` is already computed for every proposed action and
already denormalized onto `tool_invocations`, so this is a rule that
reads an existing field rather than a new mechanism. The denial is an
event with a reason, not a silent no-op, because an agent that
proposed a skill write after reading a web page is a thing worth
being able to count.

The rule is strict enough to be annoying in one legitimate case: an
agent that did genuinely useful work involving external content and
wants to write down what it learned. That case is what the background
review exists for. The review is a fresh run over the transcript, its
`origin_trust` starts at `USER`, and the transcript it reads is
enveloped data rather than instructions.

### The approval carries a diff, not an argument blob

`policy-and-approvals.md` declares `ActionKind.SKILL_AUTHORING` and
gives as its reason that skill authoring *"is not a tool
invocation"*. With `skill_manage` classified as a capability tool, it
is one. The `ActionKind` generalization survives unchanged and is
still right; the reason is narrower than the sentence claims.

What `SKILL_AUTHORING` buys is the approval's payload. A reviewer
approving a skill write needs the skill name, the current revision,
the proposed revision, and the diff between them. A reviewer looking
at a `TOOL_CALL` approval gets the tool name and its arguments, and
for `skill_manage` the arguments are a package. The action kind is
what selects the renderer, and that is worth a value in the enum on
its own.

So a `skill_manage` call raises an approval with
`kind = SKILL_AUTHORING` and a non-null `tool_invocation_id`, which
the nullable column permits. `MEMORY_WRITE` remains a genuine
non-tool action and is unaffected by any of this.

## The background review is a child run with four restrictions

`runtime-loop.md` already has the hook: `skill background review`,
enqueued on `COMPLETED` only, failure logged and never fatal,
enqueued after the terminal transition commits. This is what it
enqueues.

A child run with `parent_run_id` set to the completed run, whose
input is that run's transcript as enveloped data, and whose task is
to decide whether anything about how the work was done is worth
writing down. It does not join. The join hook wakes a *suspended*
parent waiting on `delegate.run`; this parent is `COMPLETED` and is
not waiting for anything, which is the whole point of doing the
review after the user has their answer.

Four restrictions, three of them from Section 30.3 and one from
arithmetic:

1. **A whitelisted tool set.** `memory.*`, `skill.load`, and
   `skill_manage`, and nothing else. No sandbox, no workspace, no
   network, no delegation. The filter is the runtime-environment
   stage `bootstrap-and-composition.md` already has, configured for
   this run kind rather than a new mechanism.
2. **Read before write.** `skill_manage` with `edit` or `patch` is
   denied unless this run has already loaded that skill's current
   revision. A review that rewrites instructions it has not read is
   not a review.
3. **Only agent-authored skills.** `edit`, `patch`, and `archive`
   are denied for any skill whose `source` is not `AGENT`. `create`
   is always allowed, which is how the first agent-authored skill
   comes into existence. Section 30.3 says "may edit only skills it
   created", and taken literally at the run level that is vacuous —
   each review is a fresh run and has created nothing. The source
   column is the reading with content in it, and it is noted as a
   question for review below.
4. **One review per parent, and only when there was work.** The hook
   enqueues at most once per parent run, and only for runs that made
   at least one tool call. A conversational turn produces no
   procedure worth capturing, and a review per turn would roughly
   double the platform's run volume for nothing.

Where the policy profile requires approval for `SKILL_AUTHORING`, a
review's proposal becomes a pending approval and no revision is
installed until it resolves. This is the existing approval queue with
no human attached to the originating run, which is fine — approvals
are resolved out of band by whoever watches the queue, and one that
nobody resolves expires on the schedule `RiskLevel` already chooses.

## Rollback is an `AgentSpec` edit

Section 30.6 requires that *"a bad skill version can be rolled back
by pinning an earlier version"*, and that sentence is satisfied by
machinery that already exists once `enabled_skills` entries are
references.

```text
enabled_skills: [repository-analysis]      floats
enabled_skills: [repository-analysis@6]    pinned to 6
```

`AgentSpec` is already versioned and already pinned per run, so a
rollback is a configuration change with the same audit trail as any
other configuration change, and the run that picked up the rollback
records the pin in its own `ContextPlan`. There is no rollback
operation, no `skill_manage` verb for it, and nothing that has to be
authorized separately.

Archiving is the other half and has a different blast radius.
Archiving revision 7 makes every floating reference resolve to 6
without touching a single `AgentSpec`. That is the fast action for an
operator who has just discovered a bad revision across forty agents;
pinning is the surgical one for a single agent that needs to stay
behind while the rest move on. Both are needed and neither replaces
the other.

Neither deletes anything, and a run that pinned revision 7 before it
was archived keeps resolving revision 7 forever. That is the property
that makes the audit story true, and it is the reason this store has
no retention policy.

Creating a skill does not edit `AgentSpec`. The new revision is a governed
candidate and remains absent from every agent catalog until an operator writes a
new `AgentSpec` version that enables its floating or pinned reference. Editing
an already-enabled floating skill needs no additional configuration write, but
the newer revision is visible only to sessions opened after publication. This
is the activation boundary that prevents authoring from silently broadening its
own future context.

## Rollout evidence

Runtime construction has two independent controls: foreground authoring and
background review. Both default off, and background review is invalid unless
foreground authoring is also enabled. Enabling construction makes the machinery
available; tenant activation remains a release decision.

The release gate uses paired evaluations over the declared self-authored-skill
target corpus. At least thirty paired samples are required. Let `b` be pairs
completed only by the authored arm, `c` pairs completed only by the baseline,
and `n` all pairs; the observed paired improvement is `(b - c) / n`. For
`b + c > 0`, compute a two-sided 95 percent Clopper-Pearson interval
`[p_low, p_high]` for `p = b / (b + c)` conditional on the number of discordant
pairs, then transform it to the paired-difference interval
`[(2 * p_low - 1) * (b + c) / n, (2 * p_high - 1) * (b + c) / n]`. When there
are no discordant pairs, the paired-difference interval is `[0, 0]`. The
authored arm must improve task completion by at least five absolute percentage
points, the lower bound of that paired interval must be above zero, and the
authored arm may introduce no additional policy failure. The deterministic
self-authored form of case 27 must also pass. Any policy regression blocks
rollout regardless of task improvement. Evidence is recorded per model-policy,
policy-profile, and authoring implementation version; it does not transfer to a
different combination.

## Milestones

Two milestones and a clean line between them. Everything that reads a
skill is Milestone 8; everything that writes one is Milestone 10.

```text
# capability                                milestone
package format, SKILL.md, the validator     M8
SkillRepository, SkillPackageStore, tables  M8
SkillRef grammar, enabled_skills resolution M8
the catalog, its caps, session-open pinning M8
skill.load, stickiness, the two-body cap    M8
the two context classes, ContextPlan field  M8
trust labelling derived from source         M8
.skills extraction, the workspace denial    M8
MCP prompts as read-only session skills     M8
archive, and the operator rollback path     M8
skill_manage and its four operations        M10
expected_revision and the conflict          M10
the skill.write scope, origin_trust denial  M10
SKILL_AUTHORING approvals carrying a diff   M10
the background-review child run             M10
per-skill usage and outcome metrics         M10
a catalog surface over HTTP or the CLI      deferred
relevance ranking over the catalog          deferred
skill packages shared across tenants        deferred
```

`archive` is Milestone 8 even though the other three `skill_manage`
operations are Milestone 10, because archiving is an operator action
and Milestone 8 ships operator-installed skills. An operator who can
install a bad skill and cannot withdraw it has half a mechanism.

The two context classes are Milestone 8 rather than Milestone 7,
where the rest of the context budget lands, and the reason is the one
`sandbox-isolation.md` gives for putting the sandbox setting at
Milestone 1: a class is defined where the content that fills it is
built. Milestone 7 has no skills to put in a skill class.

Nothing here is Milestone 9. Skills and memory are adjacent in the
plan's ordering and share nothing in this design, which is worth
saying because "procedural memory" makes them sound like one
subsystem and the milestone map is where that misreading would cost
something.

## Contradictions resolved

```text
#  conflict                           resolution
1  skill_manage called a control tool a capability tool
2  skill_manage NON_IDEMPOTENT        CONDITIONALLY_IDEMPOTENT
3  the scope spelled skills:write     skill.write, enumerated
4  "skills have no design at all"     tool-system.md:1306-1353
5  Milestone 8 had zero gates         ten, a new `skill` area
6  Milestone 10 had zero gates        six, in the same area
7  no harness case names a skill      case 27, Milestone 8
8  the prefix ceiling was 13,500      15,000, a fifth class
9  authoring "is not a tool call"     it is; the kind stays
10 Section 30.4 cited for gating      Section 30.3 states it
11 readiness.md cites the wrong line  corrected below, and checked
12 the gate token grammar says digit  a number; M10 has two
```

Rows 1 and 2 are one edit and they are the only ones that change a
sentence another document argues for, so they are argued in full
above, under the heading that calls `skill_manage`
[a capability tool](#skill_manage-is-a-capability-tool-not-a-control-tool).

Row 4 is a correction to a verdict rather than to a design.
`readiness.md` says skills have no specification at all and that no
document outside the plan and ADR-0013 mentions `SKILL.md`. The
second half is true. The first is not: `tool-system.md:1306-1353` is
forty-eight lines of real design that settles four questions, and
this document had to be written to fit inside it rather than on top
of it. The verdict is corrected where it is stated.

Rows 10 and 11 are citation errors and are worth listing because they
propagate. Four documents — `policy-and-approvals.md:137`,
`0005-deterministic-policy-engine.md:10` and
`0005-deterministic-policy-engine.md:148`, and
`docs/status/questions-for-review.md:391` — attribute the
policy-and-approval gating requirement to Section 30.4. The plan
states it in Section 30.3; Section 30.4 is loading and lifecycle. And
`readiness.md:734-735` cited `engineering-plan.md:2771` for the
version-pinning acceptance criterion, which is at
`engineering-plan.md:2771`; the line it named is an MCP
trust-labelling bullet. The ADR and the questions file are
historical records and are not edited. The two live statements are.
Both numbers moved by two after an `#### Acceptance criteria` heading
was inserted above them, which is the general hazard: a line-number
citation is correct only until the cited file is next edited, and an
insertion anywhere above the target moves it silently. A sweep of the
whole corpus found nine such citations already wrong, in five
documents, most of them predating this one. They are corrected, and
the hazard is now checked rather than remembered:
`scripts/check_citations.py` records the text each cited line held and
fails the docs check when it no longer holds it, and `make
citations-fix` repoints a citation whose text has merely moved. What
it cannot do is decide what a citation means when the text it named is
gone, so that case is reported rather than guessed.

Row 12 is a grammar correction. `milestone-map.md` writes the
per-gate milestone token as `**M<digit>.**` and describes the docs
check's parse the same way. Six of this document's gates are
Milestone 10, so the token is `**M<number>.**` and the parse takes
the digits rather than the digit. No existing token changes.

## Hard gates

Failing one of these blocks the milestone. They are registered in the
gate registry with identifiers, like every other gate, in a new
thirteenth area, `skill`. Ten are Milestone 8 and six are Milestone
10, which are the two milestones in the plan that had none.

1. **Only metadata reaches the prefix.** A session opens with five
   skills enabled, each carrying a distinctive sentinel string in
   its body. The rendered prefix contains every `description` and no
   sentinel. Repeated after `skill.load`, which must place the
   sentinel in the body and still not in the prefix.
   `gate.skill.metadata_only`, case. **M8.**
2. **The catalog is pinned at session open.** A skill is installed
   mid-session. It does not appear in the catalog, `skill.load` on
   its name fails, and `prefix_sha256` is unchanged across the
   installation. Repeated for an archive, which must also not take
   an entry out of a live session.
   `gate.skill.catalog_pinned`, case. **M8.**
3. **A skill never becomes a tool.** A structural check asserts that
   no module in the skills package calls `register_dynamic` or
   otherwise reaches the tool registry's write path, and that the
   registry's declared sources remain the build and MCP discovery.
   A package containing a file named like a tool manifest installs
   normally and registers nothing.
   `gate.skill.no_tool_from_skill`, structural. **M8.**
4. **Untrusted skill content is labelled and stays out of Region A.**
   An agent-authored and an MCP-sourced skill both render their
   catalog entries as `EXTERNAL_UNTRUSTED` inside an envelope, and
   both bodies appear only in Region B when loaded. A region
   assignment placing either body in A is a build failure, which is
   the mechanism `context-engine.md` already requires.
   `gate.skill.untrusted_body`, case. **M8.**
5. **A pinned skill resolves to the same bytes forever.** A run
   loads a skill; two newer revisions are installed; replaying the
   run resolves the original revision and the original
   `content_sha256`. The same replay with the stored archive
   corrupted raises rather than proceeding.
   `gate.skill.revision_pinned`, case. **M8.**
6. **A missing required tool does not fail the load.** A skill
   naming three tools, one of which the session did not pin, loads;
   the missing name is in `missing_tools` and in
   `skill.tool.missing`; and a call to the missing tool is still
   denied by the pipeline.
   `gate.skill.missing_tool_loads`, case. **M8.**
7. **The catalog is capped and deterministic.** A tenant with forty
   active skills and two MCP servers yields exactly twenty entries,
   configured skills before MCP prompts, in configuration order, and
   the dropped names are on the session-open event. Opening the same
   session twice produces byte-identical catalogs.
   `gate.skill.catalog_capped`, case. **M8.**
8. **Package validation is total.** A property test over generated
   packages — names, versions, descriptions, tool lists, body
   lengths, file counts, archive members including traversal paths
   and symlinks — asserts that every package either produces a
   revision or raises a named `SkillValidationError` subtype, and
   that no rejected package leaves a row, an archive object, or a
   partially written revision behind.
   `gate.skill.validation_total`, property. **M8.**
9. **Loaded bodies are bounded by construction.** A third
   `skill.load` fails and names the two loaded skills; no context
   plan in a thousand generated sessions carries more than two
   bodies or more than 6,000 body tokens; and an unload followed by
   a load succeeds.
   `gate.skill.body_cap`, case. **M8.**
10. **An MCP skill cannot be written.** `SkillRepository.install`
    raises for `source: mcp` from every caller, no MCP prompt
    produces a row that outlives its session, and an MCP skill's pin
    records revision `0` with the hash of the text as it arrived.
    `gate.skill.mcp_read_only`, case. **M8.**
11. **An untrusted turn cannot write a skill.** A corpus of runs,
    each of which reads untrusted content — a web page, an MCP tool
    result, another skill's body, a file the user uploaded — and
    then proposes a `skill_manage` write. Every proposal is denied
    with `origin_trust` as the reason, every denial is an event, and
    no revision exists afterwards.
    `gate.skill.authoring_trust`, corpus. **M10.**
12. **Authoring without the scope is denied.** A principal lacking
    `skill.write` is denied at the policy stage rather than at the
    store, the denial names the scope, and the same call with the
    scope succeeds. The scope string is matched by exact equality:
    `skill.writex` and `skill` are both denied.
    `gate.skill.authoring_scope`, case. **M10.**
13. **The background review is confined.** A review run is denied
    every tool outside `{memory.*, skill.load, skill_manage}`; is
    denied `edit` and `patch` on a skill it has not loaded; and is
    denied `edit`, `patch`, and `archive` on any skill whose source
    is not `AGENT`. Each denial is asserted separately.
    `gate.skill.review_confined`, case. **M10.**
14. **A failed review never touches its parent.** A review run that
    raises, times out, or is killed leaves its parent `COMPLETED`,
    leaves the parent's events unchanged, and produces exactly one
    logged failure. Repeated with the review enqueued and the worker
    killed before it starts.
    `gate.skill.review_never_fatal`, case. **M10.**
15. **Every agent-authored revision has durable provenance.** A structural
    check plus a query: every `skill_revisions` row whose skill has
    `source = agent` carries non-null principal, invocation, and idempotency-key
    provenance. `authored_by_run_id` is non-null and resolves to the event log
    while its session is retained; only governed session erasure may null that
    link. The insert path has no branch that can omit the durable fields.
    `gate.skill.provenance_complete`, structural. **M10.**
16. **Concurrent edits do not lose one.** Two runs `patch` the same
    skill with the same `expected_revision`. Exactly one revision is
    written, the loser receives `SkillRevisionConflict` carrying the
    winner's revision, no revision number is skipped, and the
    loser's content appears nowhere.
    `gate.skill.edit_conflict`, case. **M10.**

## Tracked metrics

Not gates. Watched, and a regression is an argument rather than a
build failure. The first three are Milestone 8; the rest arrive with
the authoring loop.

- **Catalog drop count**, the number of skills a session's cap
  removed, by tenant. A tenant that is silently losing half its
  catalog experiences it as skills that stopped working, and this is
  the only place that shows.
- **Skill selection rate**, the share of sessions whose catalog
  contained a skill and in which `skill.load` was called at all. A
  low number usually means descriptions are wrong rather than that
  skills are unhelpful.
- **Per-skill load count and load-to-completion rate.** A skill that
  is loaded often and whose runs complete less often than the
  baseline is a skill making things worse, and the number is the
  argument for archiving it.
- **Revisions per skill per month.** Churn is the signature of a
  procedure that has not converged. It is also the signature of two
  runs fighting over one skill, which the conflict rate separates.
- **Conflict rate on `expected_revision`.** Rising means concurrent
  reviews are colliding, which is a scheduling problem rather than a
  correctness one.
- **Authoring denials by reason**, split into scope, `origin_trust`,
  source, and read-before-write. The `origin_trust` share is the one
  to watch: a climbing number means agents are routinely trying to
  write skills out of untrusted turns.
- **Approval latency and outcome for `SKILL_AUTHORING`.** A queue
  where every skill approval expires unresolved is a governance
  mechanism that exists on paper.
- **Eval delta from self-authored skills**, which is the number
  Section 30.5 makes rollout conditional on. It is a tracked metric
  here and a release criterion there.

## Decisions

1. **`revision` is a platform integer and `version` is the author's
   string.** Pinning needs a total order that the author cannot
   forge. Semver is neither, and `AgentSpec` already made this
   choice for the same reason.
2. **The catalog is pinned at session open.** It is the tool set's
   rule applied to skills, and it is also what stops an agent from
   writing a skill and loading it in the same session.
3. **A skill written mid-session enters the next one.** The write is
   not blocked, delayed, or hidden — it simply does not appear in
   the context that produced it.
4. **Metadata may be in Region A even when the body may not.** A
   description is data the model compares; a body is instructions it
   follows. Enveloping the first is enough; the second is never in
   the prefix at all.
5. **The prefix ceiling moves to 15,000 rather than the catalog
   taking budget from tools or memory.** The ceiling is a sum, not a
   constant, and reusing capacity sized for something else is how
   two subsystems end up degrading each other.
6. **Twenty catalog entries, capped by items first.** Selection
   accuracy degrades with the number of candidates. The 75-token
   metadata rule in the validator is what makes twenty and 1,500
   consistent rather than aspirational.
7. **Two loaded bodies, and a third load fails rather than
   evicting.** Eviction would pick, and the picker cannot know which
   procedure is finished.
8. **Skill bodies never yield under pressure.** They are bounded by
   construction instead. A procedure that vanishes mid-execution
   produces confident wrong behaviour that nobody diagnoses.
9. **Truncation is by configured order, never by a score.** A
   ranking under the model's own selection is a second selector
   nobody can see.
10. **`SKILL.md`'s body is denormalized into the row.** Loading is a
    row read. The 3,000-token validator rule is what makes that
    safe, and the archive still holds everything.
11. **`content_sha256` covers the archive, not the body.** A pin has
    to cover every file a run could have read.
12. **`trust` is stored, not recomputed.** A recomputed label can be
    recomputed differently after a refactor, and every event that
    referenced the old one silently becomes wrong.
13. **`source` lives on the identity row.** A skill cannot change
    hands. This is what stops `skill_manage` from taking over a
    platform skill by writing a revision of it.
14. **Skill packages are not artifacts.** Artifacts expire in thirty
    days and belong to a run. Skills are configuration, outlive
    every run that used them, and deleting one would break a pin.
    Same object store, no shared policy.
15. **Nothing sweeps the package store.** No retention, no reference
    counting. The audit story requires that an old pin still
    resolve.
16. **`skill_manage` is a capability tool.** It acts on durable
    state outside the run, which is the definition the control-tool
    section already gives, and the control-tool table never listed
    it.
17. **`CONDITIONALLY_IDEMPOTENT` with invocation identity and the canonical
    argument hash as the key.** `expected_revision` remains the concurrency
    precondition. The separation resolves the registration contradiction and
    makes a crashed skill write recoverable without deduplicating distinct
    concurrent edits.
18. **The scope is `skill.write`, singular, dotted, enumerated, and
    alone.** No `skill.read`, because nothing reads skills over the
    API in 0.1 and an uncheckable scope is worse than a missing one.
19. **Authoring is denied below `USER` origin trust, and the
    background review is the path for the case that blocks.** The
    review starts a fresh run whose input is enveloped data.
20. **The approval is a `SKILL_AUTHORING` action with a non-null
    `tool_invocation_id`.** The action kind selects the payload — a
    diff, not an argument blob — which is worth the enum value on
    its own.
21. **The authoring loop is Milestone 10, with the substrate and
    every refusal at Milestone 8.** Section 21.1 already defers it
    past 8; the background review is a child run, which is 10.
22. **Rollback is an `AgentSpec` edit or an archive, and there is no
    rollback operation.** Both mechanisms already exist and both are
    already audited.
23. **A reference that does not resolve fails the session open.** An
    agent missing a procedure it was configured with is a different
    agent.

## Open questions for review

1. Is a new `skill` gate area right? It follows the `memory` and
   `sandbox` precedent — an area names a subject that one
   specification owns, and this is a thirteenth subject with a
   thirteenth spec. The alternative considered was splitting them
   across `context`, `tool`, and `policy`, which was rejected
   because one document owns all sixteen and because
   `gate.skill.metadata_only` and `gate.skill.authoring_trust` would
   land in different areas despite being two halves of the same
   governance story. This also answers the map's own open question 3
   — whether Milestone 8 should acquire gates — with yes.
2. **Resolved for Milestone 10A:** "may edit only skills it created" is
   satisfied by `source = AGENT`. Read literally at the run level it is vacuous: every
   background review is a fresh run and has created nothing, so a
   literal reading forbids all editing and leaves only `create`,
   which would produce a skill per review and no refinement at all.
   The source column is the reading with content in it. A third
   reading — only skills created by *this agent*, tracked by an
   `authored_by_agent_id` — is narrower and implementable, and would
   stop two agents in one tenant from editing each other's
   procedures. It is not chosen because nothing in the plan asks for
   per-agent skill ownership and adding it would make skills the
   only tenant-scoped resource with a sub-tenant owner.
3. Should there be a catalog surface — `GET /v1/skills`, or an
   `agent skill` command? Neither exists here, deliberately: the
   Milestone 5 route table was fourteen routes and the CLI is twelve commands,
   both closed for 0.1, and the substrate needs neither. ADR-0050's later
   session list and delete routes do not add a skill catalog. But an
   operator who has to read PostgreSQL to find out what an agent
   knows will not audit it, and the authoring milestone makes that
   worse rather than better. The likely answer is a CLI command at
   Milestone 10 and no route until a client needs one.
4. Is 3,000 tokens the right body limit? It is derived from the
   two-body cap rather than from any procedure anyone has written,
   which is the wrong direction for a number to come from. The
   agentskills.io ecosystem will have real examples and this should
   be checked against them before Milestone 8 rather than after.
5. Should a skill be able to declare a policy profile, or a
   narrower one? A procedure that says "run this destructive
   command" is currently governed by the session's profile, and a
   skill that could only ever run under a stricter one would be a
   useful thing to be able to write. It would also be a second
   policy input with an authoring path attached to it, which is why
   it is a question rather than a section.
6. Should skill packages be shareable across tenants? Everything
   here is tenant-scoped, which means a platform skill has to ship
   in the image and a good operator skill cannot be lent to another
   tenant. A read-only shared catalog is the obvious shape and it
   brings a trust question with it that this document has no answer
   for: a shared skill is `TRUSTED_CONFIGURATION` for whom?
7. **Resolved for Milestone 10A:** a background review cannot archive. Archive
   remains a foreground/operator action because an autonomous false positive
   has catalog-wide effect even though the bytes remain recoverable.
8. **Resolved for Milestone 10A:** the threshold is defined under
   [Rollout evidence](#rollout-evidence). Case 27 remains the deterministic
   mechanism gate; the paired capability evidence decides tenant activation.
