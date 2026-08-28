---
title: Web Access
status: implementation
canonical: true
---

# Web access

This specification expands [engineering-plan.md](engineering-plan.md#32-web-access)
and records the mechanism selected by
[ADR-0054](../adr/0054-provider-neutral-web-access.md). It adds public-web
discovery and page extraction without making a vendor API part of the agent's
tool contract.

## Scope

The first tranche exposes two read-only builtin capability tools:

| Tool | Purpose | Recommended provider |
| --- | --- | --- |
| `web.search` | Discover and rank public pages for a query | Tavily |
| `web.fetch` | Extract readable Markdown from one selected page | Firecrawl |

Both Tavily and Firecrawl implement the same `WebProvider` port and both support
both operations. The recommendation is a deployment default, not a coupling:
an operator may select either provider independently for either tool.

Crawling a site, interactive browsing, screenshots, research jobs, provider
answers, provider-generated summaries, and provider-specific scrape actions are
out of scope. Search returns source records rather than a synthesized answer;
fetch returns page content rather than an interpretation of it.

## Provider-neutral contract

`WebProvider` has three operations — `search(WebSearchRequest)`, `fetch(url)`,
and `close()` — plus a stable `name` attribute. Search returns only `title`,
`url`, and `snippet`. Fetch returns only `url`, optional `title`, and
`content`. Provider request identifiers, scores, usage fields, raw errors,
screenshots, images, links, and billing data do not cross the port. The tool
result does name the serving provider in its `provider` field, so the model
can attribute a source record without the vendor API shaping the contract.

Search-result URLs are held to a looser standard than fetch targets: a result
may use plain HTTP or a non-standard port because providers index such pages,
while `web.fetch` still refuses those URLs under its own rule below. Tavily
extraction never returns a page title; Firecrawl reports the page's canonical
`sourceURL`, which may differ from the requested URL.

`web.search` accepts a non-empty query of at most 500 characters, one through
ten results, an optional include-domain list or exclude-domain list, and an
optional `day`, `week`, `month`, or `year` recency. Include and exclude modes
are mutually exclusive. Each domain list holds at most ten unique entries;
duplicates are rejected. Domain filters are normalized DNS hostnames; IP
literals and private-shaped hostnames are rejected.

`web.fetch` accepts one URL. It requires HTTPS, forbids credentials and
non-standard ports in the URL, and rejects IP literals, localhost,
single-label names, and the `.internal`, `.local`, `.localhost`, `.home`, and
`.lan` suffixes before a provider call. An all-numeric final DNS label is also
rejected so abbreviated IPv4 forms cannot pass as hostnames. The remote provider
remains responsible for DNS-resolution and redirect defenses inside its own
fetch boundary.

## Provider mappings

The Tavily adapter calls the fixed endpoints `https://api.tavily.com/search`
and `https://api.tavily.com/extract`. Search explicitly requests `basic` depth,
no answer, and no raw page content. Extract explicitly requests basic Markdown
without images. These fields follow the official
[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)
and [Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)
contracts.

The Firecrawl adapter calls the fixed endpoints
`https://api.firecrawl.dev/v2/search` and
`https://api.firecrawl.dev/v2/scrape`. Search requests only the web source,
tolerates invalid indexed URLs (`ignoreInvalidURLs`), and maps recency to
Firecrawl's `qdr` values. Scrape requests Markdown, main content,
and explicitly keeps TLS verification enabled. These fields follow the official
[Firecrawl Search](https://docs.firecrawl.dev/api-reference/endpoint/search)
and [Firecrawl Scrape](https://docs.firecrawl.dev/api-reference/endpoint/scrape)
contracts. The baselines were verified on 2026-08-18.

## Configuration and credentials

Web access is disabled by default. The environment layer owns two independent
selectors:

```text
WEB_SEARCH_PROVIDER=disabled | tavily | firecrawl
WEB_FETCH_PROVIDER=disabled | tavily | firecrawl
```

The recommended hybrid is:

```text
WEB_SEARCH_PROVIDER=tavily
WEB_FETCH_PROVIDER=firecrawl
```

`TAVILY_API_KEY` and `FIRECRAWL_API_KEY` enter the existing credential broker
as the references `tavily` and `firecrawl`. Adapters resolve the reference at
call time and place the secret only in the fixed provider request's bearer
header. Secrets do not enter tool arguments, results, events, configuration
documents, or model context. Selecting a provider controls tool registration
and default advertisement independently: a disabled capability is absent from
the registry.

The bootstrap fallback agent remains within the context plan's immutable
6,000-token tool-definition cap. When either web capability is selected, that
fallback omits the test-only `demo.external_write` tool; when both are selected,
it also omits the specialized `knowledge.ingest` writer. Persisted or explicitly
supplied `AgentSpec.enabled_tools` lists remain authoritative and are not
rewritten. This is bootstrap curation, not runtime or model-directed routing.

## Policy, egress, and trust

Both tools declare `NETWORK_READ`, `LOW`, `READ_ONLY`, parallel execution,
bounded time and output, and `target_kind: web_provider`. Registration accepts
that target only for builtin `web.*` tools with this exact read-only
classification and `EXTERNAL_UNTRUSTED` output.

The deterministic policy's `host_on_allowlist` condition recognizes the
`web_provider` target because the composition root selects the adapter and the
adapter hard-codes its HTTPS API endpoint. A model-authored `url` or
`host_allowed` argument cannot create this target or change its egress host.
Sandbox networking remains default-deny, and model-generated code receives no
provider credential.

Every search snippet and fetched page is `EXTERNAL_UNTRUSTED`. The label is
declared on the tool specification, returned on the `ToolResult`, and persisted
on the invocation result. Page text therefore cannot become policy,
configuration, a credential, or a trusted skill-authoring source.

## Bounds and failures

Provider HTTP responses are streamed into a two-MiB hard bound before JSON
decoding; a larger response is invalid provider output. Search snippets,
titles, result counts, fetched content, and final tool outputs have lower
contract bounds: `web.fetch` truncates page content to its declared tool
output limit, and `web.search` drops trailing results until its rendered
output fits the declared limit, so one schema-bounded result always returns
inline. Redirect following is disabled for provider API calls so a bearer
credential cannot be forwarded to another host.

The adapter never returns upstream response text. Stable failures distinguish
credential rejection, provider quota exhaustion, temporary provider
unavailability, permanent provider rejection, invalid provider output, and a
disallowed fetch URL; arguments that fail the tool schema return the platform's
ordinary `tool.arguments_invalid`. HTTP 402 and Tavily's documented 432/433
usage-limit responses become `tool.web.quota_exceeded` with an operator-action
message and without the upstream body. Timeouts, transport failures, HTTP
408/425/429, and server errors are retryable; auth, exhausted quota, other
client errors, schema failures, and local URL refusals are not. The tool
pipeline retains ownership of any retry decision within the run deadline.

## Acceptance criteria

- Tavily and Firecrawl pass the same search and fetch port contract and
  normalize to byte-compatible domain shapes.
- The recommended composition routes Tavily to `web.search` and Firecrawl to
  `web.fetch`; each selector can also choose the other provider independently.
- Disabled capabilities are neither registered nor advertised by default.
- A web-enabled context plan advertises the selected `web.search` and
  `web.fetch` capabilities directly. If the pinned skill catalog is empty, it
  does not advertise `skill.load`; web discovery is a builtin capability, not
  a guessed skill name.
- A complete agent tool call passes schema validation and policy, persists its
  invocation, and returns external-untrusted content to the next model step.
- Credentials and raw upstream diagnostics never appear in tool results or
  durable events; credential, quota, and request rejections remain distinct,
  and retryability is stable and platform-defined.
- Fetch rejects non-public or non-HTTPS destinations before provider
  execution, and provider responses and tool outputs are bounded.

## Hard gates

1. **Provider contract.** Tavily and Firecrawl both pass the complete shared
   search-and-fetch contract and normalize to the same domain shapes. **M10.**
2. **Capability routing.** The recommended hybrid and both single-provider
   configurations bind the requested capabilities without changing either
   agent-visible tool schema. **M10.**
3. **Default-off registration.** A disabled capability is absent from both the
   registry and the advertised default tool set. **M10.**
4. **Context advertisement.** A web-enabled run advertises the selected web
   tools directly and does not invent or advertise an unavailable skill loader.
   **M10.**
5. **Invocation trust.** A complete web invocation passes validation and
   policy, persists its invocation, and returns `EXTERNAL_UNTRUSTED` content to
   the next model step. **M10.**
6. **Failure and secret boundary.** Missing or rejected credentials, exhausted
   provider quota, rate limits, transport failures, permanent rejections, and
   invalid output produce stable platform failures without exposing credentials
   or upstream text. **M10.**
7. **Fetch confinement and bounds.** Non-public and non-HTTPS URLs are rejected
   before provider dispatch, provider responses are hard-bounded, and complete
   tool output remains within its declared byte ceiling. **M10.**

These seven registry-backed gates, the shared port contract, and repository
checks are the tranche's blocking delivery contract. They do not advance the
verified gate ceiling until Milestone 10 as a whole completes.
