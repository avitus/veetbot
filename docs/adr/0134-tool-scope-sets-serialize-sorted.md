# ADR-0134: Tool scope sets serialize sorted

- Status: Accepted (authorized by the repository owner, 2026-09-26)
- Date: 2026-09-26
- Related: Sections 10.1 and 11 of the engineering plan; ADR-0020, ADR-0094,
  ADR-0123, ADR-0131
- Amends: nothing; it keeps ADR-0123 decision 6 through the change
- Detailed design: `docs/plan/context-engine.md`

## Context

A Chat plan's identity is two hashes: `prefix_sha256` over the frozen
prefix and the pinned tool specifications, and `tool_schema_sha256` over the
defined specifications. Both are taken over `model_dump(mode="json")`.
`canonical_json_bytes` sorts object keys but keeps arrays in order.

`ToolSpec.required_scopes` is a `set[str]`, and a JSON dump lists a set in
iteration order. That order follows the process's string hash seed, which
differs in every worker process: the systemd units do not set
`PYTHONHASHSEED`. When two members share a hash-table slot, the order also
follows insertion order, and every `model_copy(deep=True)` and every read of
a plan event rebuilds the set.

Two tools have more than one scope, `email.feedback` and `email.unsubscribe`,
both `{email.read, email.write}`. Production's roster defines the first and
defers the second. Over `PYTHONHASHSEED` 0 to 99 on CPython 3.12:

- 94 seeds give each process one fixed order, which is sorted in about half
  of them;
- two keep insertion order;
- four (3, 8, 82 and 96) reverse the pair on every rebuild.

A production-shaped plan was created under one seed, and the chat's next
message was answered under another, as after a restart:

- When the two processes order the pair differently, the planner no longer
  recognizes the plan and rotates it (`agent_prefix_changed`). A rotation
  rebuilds the plan: it recalls a new memory snapshot, re-caches the prefix
  at full price and may move the tool selection. A run parked on an approval
  then fails with `tool_pin_mismatch`, which ADR-0123 decision 6 avoids.
- Under a seed that reverses on every rebuild, the planner checks a copy
  rebuilt twice and the builder renders one rebuilt three times. The planner
  keeps the plan, and the builder refuses the run with "the frozen context
  prefix no longer matches its plan". The next message rotates and recovers.

Every deploy restarts the workers. After a restart, a chat whose plan pins
either tool therefore re-plans at its next message about half the time. In
about one restart in 25, that next message fails instead. Chats have pinned
`email.feedback` since Email mode (Milestone 26) and `email.unsubscribe`
since Milestone 31.

A read-only query over production on 2026-09-26 measured the exposure:

- 633 of the 1,058 sessions with a plan pin either tool in their current plan.
  85 of those plans recorded the pair reversed. Plans cluster by worker
  lifetime, so the split is not even.
- The last 30 days hold two epoch rotations in all: one
  `run_authority_changed` and one `agent_prefix_changed`. Few chats that pin
  these tools get another message after a restart, so the risk rarely came
  due.

## Decisions

1. **A set that reaches hashed identity serializes sorted.**
   - `ToolSpec.required_scopes` stays a set in Python.
   - Its JSON form is a sorted array. A JSON-mode serializer writes it, so
     event payloads, checkpoints and both hashes carry one order.
   - An audit of every model dumped into either hash found no other set.
     That covers `ContextPlan`, the prefix conversation items, the skill
     catalog metadata and the schemas of all seventy tools the
     production-shaped composition registers.
   - A new plan now has one identity in every process. Scopes never reach the
     model, so the prompt does not change.
2. **A plan hashed unsorted is re-keyed, not re-planned.**
   - A recorded plan whose hash matches its canonical rendering stays current.
     This covers every plan hashed with the pair sorted: 548 of the 633 in
     production.
   - Otherwise the planner renders the plan with each scope set in the order
     its plan event recorded. If that matches the stored hash and nothing
     else changed, only the plan's identity is stale.
   - The planner then appends the same plan as a new epoch with reason
     `prefix_hash_canonicalized`. Only the two hashes, the epoch and
     `created_at` change. The tools, pins, snapshot, persona and builder
     version stay the same.
   - The provider receives the same bytes, so this rotation re-caches nothing.
     No run's tool pins move, which keeps ADR-0123 decision 6.
   - Any other difference rotates exactly as before.
3. **Plan identity is tested across hash seeds.** Two tests sweep hash seeds
   covering each iteration behavior above. One checks the specification's
   JSON and hashes through copies and reloads. The other answers a
   production-shaped chat and re-renders its stored plan. Future sets or
   seed-dependent rendering anywhere in the prefix fail the second test.

## Consequences

- At its next message, each chat whose plan hashed the pair reversed gains
  one `context.epoch.rotated` event: at most 85 in production, and one of
  them had a run open when measured. Its epoch count rises by one, once.
  Nothing else about the chat changes.
- A plan created under a seed that reverses on every rebuild recorded the
  order after one rebuild, not the order it hashed. About half of those plans
  rotate once with `agent_prefix_changed`, as any restart rotates them today.
  Such seeds start about one worker in 25.
- After the deploy, restarts no longer rotate chats or fail their first
  message over scope order.
- The re-key path only fires for plans recorded before this change: every
  scope list recorded after it is sorted. It stays as long as older chats
  can resume. Removing it later would rotate the chats it would have re-keyed.
- `ToolSpec`'s validation schema is unchanged. Its serialization schema drops
  `uniqueItems`, and nothing reads that schema.
- `ApprovalRequest.required_scopes`, `ProposedAction.required_scopes` and
  `Principal.scopes` are sets that never reach a hash. They stay as they are.
  The ADR-0131 discovery key already sorts its server's scopes.
- No hard gate is registered, and milestone gate counts do not change. The
  tests are `tests/unit/test_tool_scope_order.py`,
  `tests/gates/test_scope_order_identity_adr0134.py`, two planner contract
  cases, and `tests/integration/test_scope_order_postgres.py`, which reads
  the recorded order back from jsonb.

## Alternatives considered

- **Sort the scopes and let old plans rotate.** It is smaller. Each of the 85
  reversed plans would re-plan once when its chat is continued, as a restart
  can already make it do. Nineteen of them are `context-builder@11` plans,
  whose re-planning ADR-0123 decision 6 exists to avoid. Re-keying costs one
  event per chat and moves nothing.
- **Pin `PYTHONHASHSEED` in the systemd units.** Every worker would share
  one order, but the CLI, maintenance scripts and tests could still disagree.
  A pinned seed that reverses a pair on each rebuild would rotate those chats
  after every restart. The identity would still depend on how the
  interpreter hashes strings.
- **Make the field a tuple or a frozenset.** A frozenset iterates exactly like
  a set. A tuple breaks the set operations authorization relies on,
  `issubset` and difference, and still depends on how each caller builds it.
- **Leave scopes out of the prefix identity.** They never reach the model.
  But every existing plan with any scope would rotate, not only the 85
  unsorted ones, and the replay identity would lose a field policy depends on.
- **Accept either order without a rotation.** The planner and the builder
  would both need each old plan's recorded order on every request, for as
  long as its chat lives, where re-keying needs it once.
