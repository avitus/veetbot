# ADR-0134: Tool scope sets serialize sorted

- Status: Proposed
- Date: 2026-09-26
- Related: Sections 10.1 and 11 of the engineering plan; ADR-0020, ADR-0094,
  ADR-0123, ADR-0131
- Amends: nothing
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

Every deploy restarts the workers. Today each deploy therefore re-plans about
half of the chats whose plan pins either tool. In about one restart in 25, the
first message of every such chat fails. Chats have pinned `email.feedback`
since Email mode (Milestone 26) and `email.unsubscribe` since Milestone 31.

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
2. **Plan identity is tested across hash seeds.** Two tests sweep hash seeds
   covering each iteration behavior above. One checks the specification's
   JSON and hashes through copies and reloads. The other answers a
   production-shaped chat and re-renders its stored plan. Future sets or
   seed-dependent rendering anywhere in the prefix fail the second test.

## Consequences

- A recorded plan hashed with the pair sorted, about half of those that pin
  either tool, stays current.
- Each other such plan fails the planner's comparison once, at its chat's next
  message, and rotates with `agent_prefix_changed`. That is the rotation any
  restart causes today, and the deploy of this change restarts the workers
  anyway, but it is the last one. It rebuilds the plan, so a run parked on an
  approval in such a chat can fail with `tool_pin_mismatch` if its selection
  moves, notably in a `context-builder@11` chat.
- After the deploy, restarts no longer rotate chats or fail their first
  message over scope order.
- `ToolSpec`'s validation schema is unchanged. Its serialization schema drops
  `uniqueItems`, and nothing reads that schema.
- `ApprovalRequest.required_scopes`, `ProposedAction.required_scopes` and
  `Principal.scopes` are sets that never reach a hash. They stay as they are.
  The ADR-0131 discovery key already sorts its server's scopes.
- No hard gate is registered, and milestone gate counts do not change. The
  tests are `tests/unit/test_tool_scope_order.py` and
  `tests/gates/test_scope_order_identity_adr0134.py`.

## Alternatives considered

- **Re-key a plan hashed unsorted instead of re-planning it.** If the stored
  hash matches the plan rendered with the scope order its event recorded, the
  planner could append the same plan as a new epoch with canonical hashes.
  Nothing would be rebuilt and no run's pins would move. It needs a legacy
  path in the planner for as long as older chats can resume.
- **Pin `PYTHONHASHSEED` in the systemd units.** Every worker would share
  one order, but the CLI, maintenance scripts and tests could still disagree.
  A pinned seed that reverses a pair on each rebuild would rotate those chats
  after every restart. The identity would still depend on how the
  interpreter hashes strings.
- **Make the field a tuple or a frozenset.** A frozenset iterates exactly like
  a set. A tuple breaks the set operations authorization relies on,
  `issubset` and difference, and still depends on how each caller builds it.
- **Leave scopes out of the prefix identity.** They never reach the model.
  But every existing plan with any scope would rotate, not only the unsorted
  half, and the replay identity would lose a field policy depends on.
