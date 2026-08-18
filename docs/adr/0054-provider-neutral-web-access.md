# ADR-0054: Provider-neutral web access with capability-level routing

- Status: Proposed
- Date: 2026-08-18
- Related: Sections 8, 9, 11, 20, 22, and 32; ADR-0005, ADR-0008, ADR-0021
- Detailed design: `docs/plan/web-access.md`

## Context

The agent can reason over user input, memory, knowledge documents, workspaces,
and MCP results, but it cannot discover current public information. A request
to find publicly available information therefore stalls after clarification.
Tavily and Firecrawl both offer search and extraction, but they have different
strengths and vendor-specific schemas. Exposing either schema as a tool would
pin agent behavior and stored tool calls to that vendor.

The existing `NETWORK_READ` policy also fails closed because a model-authored
host cannot authorize itself. Web access needs a narrow way to distinguish a
fixed, operator-selected provider endpoint from an arbitrary URL supplied by a
model.

## Proposed decisions

1. **Expose two stable tools and one port.** `web.search` and `web.fetch` use a
   provider-neutral `WebProvider`; vendor payloads end at the adapter boundary.
2. **Route by capability.** `WEB_SEARCH_PROVIDER` and `WEB_FETCH_PROVIDER` are
   independent selectors. Tavily search plus Firecrawl fetch is recommended,
   while either adapter remains valid for either operation.
3. **Remain default-off.** A disabled selector registers and advertises no
   corresponding tool. Provider credentials alone do not expand an agent's
   capability set.
4. **Resolve credentials at call time.** The existing broker supplies bearer
   tokens by opaque reference. No token appears in a tool schema or durable
   value.
5. **Authorize the fixed target, not the argument.** `target_kind:
   web_provider` satisfies the network allowlist only for a registered builtin
   `web.*` read-only capability. Provider API endpoints are hard-coded HTTPS
   URLs. Model arguments cannot select the egress host.
6. **Treat all web content as untrusted.** Search results and fetched pages are
   `EXTERNAL_UNTRUSTED` at specification, result, context, and persistence
   boundaries.
7. **Constrain fetch before delegation.** Only public-shaped HTTPS DNS URLs are
   accepted. Credentials, IP literals, private suffixes, localhost, and
   non-standard ports are refused locally.
8. **Bound and normalize failures.** Provider responses are size-limited, raw
   upstream errors are discarded, and retryability uses stable platform reason
   codes.

## Consequences

- Agent prompts and evaluation cases refer to `web.search` and `web.fetch`, not
  to Tavily or Firecrawl.
- Deployments can use the recommended hybrid, one vendor for both operations,
  or only one capability without changing an `AgentSpec` tool name.
- A provider outage can reduce only the capability mapped to it; the other
  capability may remain available.
- The worker sends target URLs to the selected extraction service. This is a
  deliberate data-processing boundary operators must account for when choosing
  a provider.
- Interactive browsing, crawl jobs, screenshots, and provider-generated
  answers remain future, separately classified capabilities.

## Alternatives considered

- **Tavily for both operations:** simpler and a reasonable low-cost deployment,
  but weaker as the default for difficult, dynamic page extraction.
- **Firecrawl for both operations:** strongest single-vendor extraction path,
  but search is not as focused on concise agent discovery as Tavily.
- **Expose provider-specific tools:** rejected because it leaks vendor schemas
  into prompts, pins, evaluations, and persisted invocations.
- **Implement providers as tenant HTTP MCP servers:** rejected because the
  hosted MCP credential would sit in a model-visible URL or require a second
  authentication design, while the existing credential broker already solves
  the trusted-worker case.
- **Allow arbitrary `NETWORK_READ` tools:** rejected because it weakens the
  fail-closed host condition and lets a model-authored argument authorize
  egress.
