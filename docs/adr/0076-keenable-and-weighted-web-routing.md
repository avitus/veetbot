# ADR-0076: Keenable and deterministic weighted web routing

- Status: Proposed
- Date: 2026-08-31
- Related: Section 32; ADR-0021, ADR-0024, ADR-0054
- Detailed design: `docs/plan/web-access.md`

## Context

ADR-0054 deliberately keeps `web.search` and `web.fetch` independent of a
vendor schema, but its composition chooses exactly one provider for each
capability. The owner wants to compare Keenable with the incumbent search and
fetch providers by sending 50 percent of each capability's traffic to it. A
clean comparison must preserve the existing tool schemas, record which provider
served a call, and keep retries from silently changing experimental arms.

Keenable exposes fixed search and fetch REST endpoints with normalized fields
that fit the existing `WebProvider` port. Its authentication and domain-filter
surface differ: it uses `X-API-Key`, and search accepts one `site` rather than
the platform's include/exclude lists.

## Proposed decisions

1. **Keep adapters and routing separate.** Tavily, Firecrawl, and Keenable each
   implement `WebProvider`. `WebProviderRouter` selects an implementation for a
   tool invocation; no provider-specific branch enters either tool.
2. **Use deterministic weighted allocations.** Each capability has an ordered
   set of positive integer weights. SHA-256 of the capability and durable tool
   invocation identifier selects a bucket. The identifier survives retries and
   recovery, so one invocation never switches providers.
3. **Start with independent 50/50 comparisons.** Search allocates 50 percent to
   Tavily and 50 percent to Keenable. Fetch allocates 50 percent to Firecrawl
   and 50 percent to Keenable.
4. **Do not fail over within an invocation.** A provider error remains assigned
   to the selected provider. Automatic failover would undercount failures,
   distort latency, and make retry behavior depend on adapter order.
5. **Preserve provider attribution.** Successful tool output names the serving
   provider. A provider-dispatched failure also stores the selected provider in
   the durable invocation's structured result, while the model receives only
   the existing stable failure vocabulary.
6. **Add plural weighted selectors without breaking singular selectors.**
   `WEB_SEARCH_PROVIDERS` and `WEB_FETCH_PROVIDERS` accept unique
   `provider:percentage` entries summing to 100. The singular selectors remain
   valid for one-provider deployments and as the default-off compatibility
   path; an enabled singular value and a non-empty plural value are ambiguous
   and fail startup.
7. **Adapt Keenable at the existing boundary.** The adapter fixes egress to
   `https://api.keenable.ai/v1/search` and `/v1/fetch`, resolves the `keenable`
   credential at call time, and sends it only as `X-API-Key`. Multiple include
   domains fan out to bounded site-filtered searches. Excludes use at most
   fifty upstream rows followed by local suffix-aware hostname filtering.
   Fetch uses bounded live Markdown without a provider-generated prompt.
8. **Retain default-off registration.** Credentials and allocation syntax do
   not register another agent-visible tool. A capability is present only when
   its resolved allocation is non-empty.

## Consequences

- Adding or removing a provider requires one port-conforming adapter and one
  composition mapping; tool definitions, stored tool arguments, and model
  prompts do not change.
- Provider selection is reproducible from an invocation identifier and the
  configured allocation. Across many independently identified invocations, the
  hash buckets approximate the configured percentages; a small sample is not
  guaranteed to be exactly half.
- Keenable's single-site API can cost more than one upstream request for one
  platform search when multiple include domains are supplied. That bounded
  fan-out preserves semantics and is visible in provider billing.
- The comparison measures real provider failures rather than a failover-masked
  success rate. Operators must remove or reweight an unhealthy provider
  explicitly.
- Keenable receives the requested search query or fetch URL as a new external
  data-processing boundary, under the same trust and credential rules already
  applied to Tavily and Firecrawl.

## Alternatives considered

- **Random choice per attempt:** rejected because recovery could move one
  durable invocation between comparison arms and make failures irreproducible.
- **Round-robin counters:** rejected because process restarts, worker counts,
  and concurrent scheduling make the counter neither durable nor globally
  meaningful.
- **Automatic failover:** rejected for the initial comparison because it hides
  reliability differences and changes the provider on retry.
- **Provider-specific Keenable tools:** rejected for the same reason ADR-0054
  rejected Tavily- and Firecrawl-specific tools: vendor schemas would leak into
  prompts, pins, evaluations, and persisted calls.
- **Drop unsupported domain filters:** rejected because it would silently
  weaken the existing `WebProvider` contract for one adapter.
