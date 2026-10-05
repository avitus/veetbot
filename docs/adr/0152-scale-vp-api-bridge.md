# ADR-0152: Scale VP data through its REST API, behind a first-party bridge that owns its sign-in

- Status: Proposed (drafted at the repository owner's request, 2026-10-05)
- Date: 2026-10-05
- Related: ADR-0021, ADR-0071, ADR-0097, ADR-0131
- Detailed design: `docs/plan/tool-system.md`

## Context

The owner wants Chat to read the Scale VP data service at
`https://scalevp-mcp.com`. The service offers a hosted MCP endpoint and a REST
API under `/api/v1/` that publishes an OpenAPI document. Both accept only a
bearer token from the service's own OAuth authorization server, which offers
the authorization-code grant with PKCE and dynamic client registration and
nothing else: no client-credentials grant and no static key (its published
metadata, checked 2026-10-05; the service's operator confirmed OAuth is the
intended route).

Nothing in the platform can complete that grant.
[tool-system.md](../plan/tool-system.md) defers the user-delegated flows for
platform-dialled MCP servers and refuses refresh tokens there, and the shipped
egress policy admits no remote MCP destination. The one precedent for an
authorization-code grant is Gmail (ADR-0071): a first-party stdio package
holds the grant, and the platform never learns that the credential is OAuth.

## Decision

Add read access to the service as a default-off, non-milestone extension. It
authorizes no milestone, changes no registered gate, and does not make
third-party MCP servers installable.

**The REST API, behind a third sibling package.** At the owner's direction
(2026-10-05) access goes through `/api/v1/`, not the hosted MCP endpoint.
`svp_mcp` joins `gmail_mcp` and `bland_mcp` as an operator-configured stdio
server, `svp_read`, that cannot import core. Its tool declarations ship with
the release, so the catalog reuse of ADR-0131 holds, and the remote service's
own tool names, descriptions and schemas never reach the advertisement.

**Three read tools over the published document.** `list_operations`,
`describe_operation` and `call_operation` are driven by the service's OpenAPI
document, which the package fetches with its grant. Only `GET` operations are
listed or callable; path and query parameters are the only request inputs,
and a query name the document does not declare is refused. The row is
`NETWORK_READ`, `LOW`, `READ_ONLY`, and requires `mcp.svp_read.use`, which
composition grants the owner as it does for the other first-party rows. No
write mode exists; one is a separate decision after the document has been
read, on the Bland read/call precedent.

**Confinement is the package's own.** A stdio child is not behind the egress
proxy. The origin, the API root and the three OAuth endpoints are package
constants. Every API request is built from a document path and refused
unless it is HTTPS to that host under `/api/v1/` with no dot segment;
redirects are never followed and ambient proxy settings are ignored. Header
and cookie parameters in the document are never sent. Bodies are bounded, and
upstream error text never crosses the pipe: failures are fixed `svp.*` codes.

**Sign-in is a one-time operator ceremony.** `python -m svp_mcp bootstrap`
registers a client, runs the authorization-code grant with PKCE (S256)
through an installed-app loopback redirect on the operator's machine after an
explicit data-use confirmation, proves the grant against the API by fetching
the document, and writes one owner-only file. The platform's deferral of
user-delegated flows stands; no route, callback or token store is added to
the API.

**The package owns its grant as a file, and that is the difference from
Gmail.** Google's refresh tokens do not rotate, so ADR-0071 passes a read-only
credential by value. This service may rotate its refresh token on use, a
rotated token must be durable before it is relied on, and several server
processes run at once. `SVP_CREDENTIAL_FILE` therefore names a private state
file; the credential resolver maps `svp_read` to that path rather than to the
file's contents, and the adapter's existing `env` scheme places it in the one
declared variable, `SVP_MCP_CREDENTIAL_FILE`. The package refreshes at use
with a sixty-second skew and no timer, under an exclusive advisory lock,
re-reading the file first so one process's rotation serves the others, and
replaces the file atomically before the new token is used. No token, client
secret or upstream body enters `argv`, an event, a log or a tool result.

**Activation.** `AGENT_SVP_ENABLED` defaults to false. Enabled, it requires
`SVP_CREDENTIAL_FILE` to be the absolute path of an owner-only regular file,
or startup fails closed. A row named `svp_read` is composed only by the flag.
The four owner units gain one optional writable path, `/var/lib/veetbot/svp`,
because under `ProtectSystem=strict` the grant's holder could not otherwise
replace it.

## Alternatives

- **Dial the hosted MCP endpoint as a tenant HTTP server.** This needs the
  interactive authorization surface the tool system defers, refresh tokens it
  refuses, and an egress destination the sandbox would share. It is the
  general "install an MCP server" feature, which nobody has authorized.
- **Run the vendor-suggested `npx mcp-remote` wrapper.** That is a stdio
  server whose code is downloaded at start, on a host with no Node runtime,
  and whose sign-in needs a desktop browser.
- **Pass the credential by value, as Gmail does.** A rotated refresh token
  could not be kept, so the grant would die at its first refresh.

## Consequences

New chats on a deployment with the flag advertise three more tools through
the existing deferred index; older chats keep their pinned roster. No
dependency, policy rule, adapter change or registered gate is introduced.
Three more read tools change which definitions fit in Chat's thirty, so the
flag joins the two production-roster gates before production enables it
(ADR-0124). A scheduled run is not offered the tools: the schedule role
composes its own principal and carries no credentials, and extending it is a
separate change.

The grant is one identity's. Whatever that sign-in may read, a run may read
on the owner's behalf, and results are external-untrusted text that reaches
the hosted model provider like any other tool output; the ceremony says so
before the browser opens.

A rotating grant has one holder. A crash between the service rotating a token
and the durable write, or a copy of the file left in use on a second machine,
breaks the chain, and the fix is to run the ceremony again. The file is moved
to the deployment, not copied.

Generic tools cost the model a lookup that typed tools would not, and an
endpoint that reads through `POST` is unreachable. Both are revisited once the
document can be read.

Three facts are unverified until the first sign-in, because the service
answers nothing without a token: that the document is served at
`/api/v1/openapi.json`, that the authorization server's tokens are the ones
the REST API accepts, and whether refresh tokens rotate. The ceremony tests
the first two before it writes anything; the design is correct either way on
the third. Offline tests cover the grant, rotation, confinement and tool
behaviour against a scripted transport. A real-service smoke by the owner
remains, and acceptance of this record waits on it.
