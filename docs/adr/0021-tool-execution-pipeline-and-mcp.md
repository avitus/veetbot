# ADR-0021: Tool execution pipeline, effect watermarking, and MCP integration

- Status: Proposed
- Date: 2026-07-25
- Related: Milestones 1, 4, 6, 8, Sections 7 (`Tool` port), 8 (tool contract,
  registry, execution pipeline, idempotency), 9.2 (unknown-tool denial),
  11.1 (tool filtering), 12.4 (multiple tool calls), 12.5 (loop detection),
  13 (error taxonomy), 15 (`tool_invocations`), 18.3/18.4 (artifacts),
  19 (spans and metrics), 22 (security controls), 26 (subagents),
  27.3 (`conversation.ask_user`), 29.4, 30 (skills),
  ADR-0002 (provider-neutral model protocol), ADR-0005/0006 (policy,
  approvals), ADR-0008 (sandbox isolation), ADR-0013 (self-improving
  skills), ADR-0015 (programmatic tool orchestration), ADR-0020 (context
  engine)
- Detailed design: `docs/plan/tool-system.md`

## Context

The tool system is the only component whose failure can leave a mark that no
rollback removes. Everything else in the platform manipulates representations;
this is where the platform touches the world. It is also the largest undefined
surface in the plan.

`Tool.execute` is Section 7's central port, and neither of the two types in its
signature — `ToolResult` and `ToolExecutionContext` — is defined anywhere in
the plan, the six sibling specs, or the twenty existing ADRs. The registry has
a module path, three prose mentions, and no interface. Five of the twelve
concrete tool names the plan uses have no `ToolSpec`, and three of those are
*control* tools whose effect is on the run rather than on the world — a
category `ToolSpec` cannot currently express, because every field on it
presumes an outward-facing action.

Two structural gaps matter more than the missing types.

First, the event-log spec's crash-recovery path dispatches on
`IdempotencyClass` and ends by marking "ambiguous non-idempotent executions
`UNCERTAIN`". Nothing defines how a worker that did not run the call decides
whether a `RUNNING` row is ambiguous. Absent a definition it must assume the
worst for every non-idempotent tool, so every such row becomes `UNCERTAIN` and
lands in a human review queue — and most of them are calls that never left the
process.

Second, Milestone 8 requires MCP tool discovery and "namespaced server IDs",
while Section 9.2's decision matrix ends with `Unknown tool -> Deny`. Nothing
joins them. Without a join, every discovered MCP tool is an unknown tool, and
Milestone 8 does not function at all.

Alongside these, the plan carries a live contradiction (`ToolResultItem.trust`
has two conflicting defaults), two independent repeated-call breakers that
would ship as two disagreeing counters, and no statement of where the
`argument_trust` and `origin_trust` labels the policy engine consumes actually
come from.

## Decision

1. **Define the port's two missing types.** `ToolResult` carries `ok`,
   content, optional validated structure, artifacts, an optional
   `ToolFailure`, and an optional trust label that may only be *lowered*. A
   tool cannot return a status: status is the pipeline's judgement, and a tool
   able to claim `denied` could launder a denial into a refusal it authored.
2. **`ToolExecutionContext` carries no database session, no repository, no
   policy engine, and no registry.** A tool cannot reach the pipeline that
   invoked it, cannot open a transaction across its own I/O (Section 12.2),
   and cannot call another tool. It receives identity, budget, deadline,
   target, workspace, artifact writer, credential resolver, cancellation, and
   `mark_effect_sent`.
3. **The pipeline is fourteen steps, one function, one call site.** Section
   8.3's ten steps are preserved in order; four persistence points are
   inserted where Section 12.2's transaction discipline requires them.
   Nothing before step 8 has touched the world; nothing after step 10 can undo
   step 10. The bridge, the device channel, and the MCP adapter supply a
   `Tool` and re-enter this function rather than implementing variants —
   ADR-0015's "the bridge is the enforcement point" is only true if there is
   exactly one enforcement point.
4. **`effect_sent_at` makes recovery decidable.** A `NON_IDEMPOTENT` or
   `CONDITIONALLY_IDEMPOTENT` tool calls `mark_effect_sent()` immediately
   before the operation that can leave a mark. Recovery then reads a fact
   instead of making an inference, and `UNCERTAIN` is reserved for calls whose
   watermark is set. Section 8.4's "do not automatically retry a
   non-idempotent tool left `RUNNING`" is preserved exactly for calls that may
   have escaped; a worker that died during argument marshalling is retried
   safely. The watermark is written *before* the effect, so a set value proves
   the effect *may* have happened, not that it did — the asymmetry is the
   correct one, and a tool that omits the call is a contract violation
   asserted by the contract suite and flagged in production.
5. **Registry names match a fixed grammar with partitioned domains.** `mcp`
   and `device` are reserved; registering a builtin in either is a startup
   error, which is what stops a server configuration from shadowing
   `workspace.write_text`. MCP tools are named
   `mcp.{server_id}.{normalized_remote_name}` by a deterministic
   normalization, which is the join that makes Section 9.2's unknown-tool row
   compatible with Milestone 8. Same-server normalization collisions drop both
   tools rather than resolving by iteration order.
6. **An MCP server does not classify itself.** `side_effect`, `risk`,
   `idempotency`, and `required_scopes` come from operator configuration at
   the server level, never from the server's own declaration, and the default
   for an unclassified server is the most restrictive combination the type
   system permits. `output_trust` is *forced* to `EXTERNAL_UNTRUSTED` for
   every MCP, device, and sandbox source at registration, which also resolves
   the plan's conflicting `ToolResultItem.trust` defaults.
7. **Tenant-configured servers are HTTP-only through the egress allowlist;
   stdio servers are operator-configured only.** A stdio server is a child
   process in the worker's trust zone, so a tenant able to name one would have
   remote code execution there.
8. **MCP's other surfaces map deliberately, not naturally.** Resources become
   one synthetic per-server read tool that runs the full pipeline — not an
   automatic context source, which would put externally controlled text into
   assembled context on a schedule the platform does not control. Prompts
   become read-only `EXTERNAL_UNTRUSTED` skills that never enter the cacheable
   prefix. Sampling and roots are declined at capability negotiation in v0.1,
   so a server never gets to spend a tenant's model budget or request
   filesystem scope.
9. **A catalog change is recorded and not applied mid-session.** ADR-0020's
   pinned-advertisement rule is extended to cover MCP: a withdrawn tool stays
   advertised and returns `unavailable` at call time. Otherwise an external
   server could rewrite a tenant's byte-stable prefix at will, which is a cost
   attack and a cache-timing side channel as well as a correctness problem.
10. **The idempotency key adds a version tag and `tool_version` to Section
    8.4's five inputs and deliberately excludes `attempt_number`,** so a
    bounded retry reuses the key and the external service's own idempotency
    mechanism can deduplicate it. `step_number` is included, so a legitimate
    repeat in a later step executes.
11. **Trust labels have a defined and fail-closed origin.** `origin_trust` is
    the minimum trust label over the context items present in the request that
    produced the call — it answers "was untrusted content in the room".
    `argument_trust` defaults to `EXTERNAL_UNTRUSTED` and is **not** inferred
    by matching argument text against `USER`-labelled context. A verbatim
    sixteen-character match was the original rule and it is unsound:
    equality demonstrates that two values are equal, not that this one came
    from that source, and an `EXTERNAL_UNTRUSTED` document can simply quote a
    string it can see in the same request. The failure is not a wasted
    approval — raising the label *removes* an approval that would otherwise
    have been required, so a successful copy buys an injected action a
    trusted path. Provenance is therefore carried, not reconstructed: an
    argument's label is set where the call is constructed, and where any
    untrusted item contributed to a field the lower label stands. An
    argument whose provenance is unknown stays `EXTERNAL_UNTRUSTED`.
12. **Large output is excerpted head-and-tail and artifactized, never
    summarized.** A summarizer over untrusted tool output producing
    trusted-looking prose is the exact laundering ADR-0020 forbids. Excerpts
    never split a trust envelope.
13. **All four non-success outcomes share one six-field shape** with a stable
    `reason_code` and a fixed message drawn from a checked-in table. External
    text is data, never narration: a remote system's error string goes into
    enveloped `EXTERNAL_UNTRUSTED` content, never into `message`.
14. **One circuit breaker with one counter** serves both the policy spec's
    repeated-denial rule (3) and Section 12.5's loop detection (5), plus a
    third rule: an invocation that resolved `UNCERTAIN` is never proposed
    again in the same run.
15. **A step is one model call plus the complete disposition of every tool
    call it produced.** A batch shares one `step_number` and is distinguished
    inside the key by `call_id`. Parallelism requires the *whole* batch to
    qualify; a mixed batch runs sequentially, because splitting it would
    require inferring independence and Section 12.4 warns against exactly
    that.
16. **Control tools are `ToolKind.CONTROL`, run the full pipeline, and suspend
    via a nullable marker rather than a new status,** so no consumer of
    `tool_invocations.status` has to learn about suspension and the
    lease-expiry sweep excludes them by predicate.
17. **The bridge synthesizes `call_id` from the script hash and a
    bridge-counted call ordinal,** so a replayed orchestration script
    deduplicates at step 8. Replay safety is conditional on script
    determinism; the effect watermark is what bounds the rest. Approval holds
    are bounded (default 300s), after which the sandbox is torn down and the
    script re-executes from the start against recorded outcomes.
18. **No `tool_registry_snapshots` table.** ADR-0020's context plan already
    pins `tool_names` and `tool_schema_sha256`; with the MCP catalog history
    that fully answers "what did this session advertise", and a third record
    of the same fact is a third place for it to disagree.

## Consequences

- The central port becomes implementable. `ToolResult` and
  `ToolExecutionContext` were blocking Milestone 1, not just Milestone 8.
- `UNCERTAIN` becomes rare and meaningful. Under the current design every
  non-idempotent call interrupted by a crash escalates to a person; with the
  watermark, only calls that may genuinely have escaped do. The cost is one
  extra committed write per consequential call and a false-positive class
  (crash between watermark and request) that is the correct direction to be
  wrong in.
- Every tool author now has a contract obligation that a type checker cannot
  enforce, so the contract suite and a production flag carry it instead.
- Milestone 8 becomes buildable rather than blocked, and MCP arrives as an
  adapter that cannot classify its own risk, cannot alter a running session,
  and cannot reach the context without going through the pipeline.
- Operators gain a real configuration burden: an unclassified MCP server is
  maximally restricted, so onboarding one requires a deliberate classification
  step. This is intentional and will be experienced as friction.
- `tool_invocations` gains fourteen columns and two new tables are added.
  Section 15's existing columns are unchanged.
- Section 6.8's event list gains seven entries; no tool event is added, since
  the seven that exist are sufficient once the payload rule is fixed.
- Ten hard gates and two new alerting metrics are added, most of them attached
  to Milestone 1 rather than Milestone 8.
- The "event carries classification, row carries payload" rule means the SSE
  stream is no longer a place to debug a failing tool call, which is a
  deliberate loss of convenience in exchange for not having tenant data under
  three retention policies.

## Alternatives considered

- **Letting a tool return its own status**: rejected; it lets a tool
  manufacture a `denied` or an `uncertain`, both of which are claims about the
  platform's own judgement rather than about the world.
- **Giving `ToolExecutionContext` a database session**: rejected; it makes
  Section 12.2's "no transaction across tool I/O" unenforceable by
  construction, and it is the shortest path to a tool that calls a tool.
- **Inferring ambiguity from timestamps** (for example, treating a call whose
  `started_at` is older than some threshold as having escaped): rejected; it
  is a guess dressed as a rule, and its error rate varies with unrelated
  infrastructure latency.
- **Writing the watermark after the effect**: rejected; it inverts the safety
  property. An unset value would then mean "may have happened", which is the
  common case, so nothing would be ruled out.
- **A separate `EFFECT_SENT` status instead of a column**: rejected; status is
  consumed by projections, the API, and the reaper, and adding a value to it
  is a change every consumer must handle. A nullable column is additive.
- **Trusting the server's declared risk or `annotations`**: rejected; it is a
  claim by the party the classification exists to constrain.
- **Per-tool operator classification for MCP**: deferred rather than rejected;
  per-server is coarser and safe, and the configuration surface is a place to
  move slowly. Recorded as an open question.
- **Applying `tools/list_changed` immediately**: rejected; it hands an
  external server the ability to invalidate a tenant's prompt cache at will,
  which is unbounded cost imposed by a third party, on top of the timing
  channel. An operator who needs immediate removal has ADR-0020's forced
  prefix epoch, which is explicit, logged, and rate-limited.
- **Attaching MCP resources to context automatically**: rejected; it is the
  injection surface the trust labelling exists to close, and it would place
  externally controlled text in the region ADR-0020 keeps stable.
- **Implementing sampling so servers can request model calls**: rejected for
  v0.1; it is an external party spending a tenant's budget on a prompt the
  platform did not compose.
- **Including `attempt_number` in the idempotency key**: rejected; it would
  turn every bounded retry of an idempotent external write into a duplicate
  write, which is precisely what Section 12.3 forbids.
- **Lowering `argument_trust` on a match against untrusted content** (the
  intuitive direction): rejected; its failure mode is an injection passing as
  trusted, whereas the raise-only rule's failure mode is an unnecessary
  approval.
- **Summarizing large tool output before returning it**: rejected; see
  ADR-0020's laundering argument. Excerpting loses more raw text and launders
  nothing.
- **Putting the remote error string in the outcome `message`**: rejected; it
  is attacker-controlled text in the one field the model is invited to read as
  the platform speaking.
- **Two separate counters for repeated denials and repeated calls**: rejected;
  they are the same mechanism at two thresholds, and two implementations would
  drift and disagree about what "identical" means.
- **Splitting a mixed batch into a parallel read group and a sequential write
  group**: rejected; it requires inferring that no read depends on a write in
  the same batch, which is the inference Section 12.4 explicitly warns
  against.
- **A `tool_registry_snapshots` table**: rejected as redundant with the
  context plan's pin plus the catalog history.
- **Allowing tenant-configured stdio servers**: rejected; it is remote code
  execution in the worker's trust zone.
