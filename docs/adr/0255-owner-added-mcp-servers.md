# ADR-0255: Owner-added MCP servers with in-app sign-in

- Status: Accepted by owner request on 2026-10-26
- Date: 2026-10-26
- Related: ADR-0008, ADR-0021, ADR-0119, ADR-0123, ADR-0131, ADR-0141, ADR-0153, ADR-0253
- Amends: the tool system's deferral of user-delegated OAuth, and its rule
  that tenant-configured HTTP servers reach the network only through the
  sandbox egress allowlist
- Detailed design: `docs/plan/tool-system.md`

## Context

The owner asked to connect the MagicPath MCP server
(`https://api.magicpath.ai/mcp`) and to add further MCP servers from the
Settings window instead of from deployment configuration. Three facts stood
in the way. MagicPath answers an unauthenticated `initialize` with `401` and
RFC 9728 protected-resource metadata; its authorization server offers only
the authorization-code grant with PKCE, dynamic client registration and
refresh tokens, which the tool system deferred. Production's sandbox egress
policy is `deny`, so no tenant HTTP server could pass `destination_allowed`.
And the deferred tool index is already at its 2,000-token cap (ADR-0253), so
a new server's tools would mostly be skipped.

On 2026-10-26 the owner chose: build in-app sign-in; let the owner choose
each server's access level; keep server management out of the model's reach;
and give owner-added servers their own index budget.

## Decision

1. **A default-off feature.** `AGENT_MCP_SERVERS_ENABLED=1` mounts the routes
   below, starts the owner MCP transport and lets the runtime use owner-added
   rows. It requires `MCP_AUTHORIZATION_KEY`, a base64url 32-byte key that
   seals stored client secrets and tokens, and `MCP_OAUTH_CALLBACK_URL`, the
   absolute URL of the callback route (HTTPS, or HTTP on a loopback host for
   development). Startup fails closed without either. While the flag is off,
   owner-added rows are ignored and their routes return 404.

2. **Owner-added servers are `mcp_servers` rows with `owner_managed=true`.**
   They are HTTP, never operator-configured, and authenticate with `none` or
   the new scheme `oauth2_authorization`. Their identifier matches
   `^[a-z][a-z0-9_]{0,31}$` and may not begin `gmail_`, `bland_` or `svp_`
   or equal an operator row. The URL is absolute HTTPS on port 443 with a
   public hostname, no credentials and no fragment. A tenant holds at most
   sixteen. Their tools require no scope; the routes do.

3. **The owner classifies, per server.** `read_only` maps to `network_read`,
   `low`, `read_only`, which the default policy allows as it does any
   operator-classified MCP read. `changes`, the default, maps to
   `external_write`, `high`, `non_idempotent`, which requires approval for
   every call. No server annotation changes either, and no policy document
   changes.

4. **Settings, not the model, manages servers.** Seven routes, mounted only
   with the flag, under the existing `settings.read` and `settings.write`
   (ADR-0119), so no scope enters the vocabulary. No model-callable tool adds,
   changes or signs in to a server.

   ```text
   GET    /v1/mcp-servers                       settings.read
   POST   /v1/mcp-servers                       settings.write
   PATCH  /v1/mcp-servers/{server_id}           settings.write
   DELETE /v1/mcp-servers/{server_id}           settings.write
   POST   /v1/mcp-servers/{server_id}/sign-in   settings.write
   DELETE /v1/mcp-servers/{server_id}/sign-in   settings.write
   GET    /v1/mcp-oauth/callback                public; the state is the authority
   ```

   Every view has `server_id`, `url` (null for operator rows), `managed_by`
   (`owner` or `operator`), `access` (`read_only` or `changes`),
   `authentication` (`none`, `sign_in` or `operator`), `status` (`ready` or
   `sign_in_required`), `sign_in_host` and `signed_in_at`. The list is
   `{"servers": [...]}`, operator rows included and read-only.

   Adding takes `server_id`, `url` and `access`, and probes the URL first. A
   `2xx` to an unauthenticated `initialize` stores `none`. A `401` follows
   RFC 9728 and RFC 8414 discovery and stores `oauth2_authorization` with the
   authorization server's metadata, which must offer the code flow, `S256`
   and a registration endpoint. A new server is `201`; an identical replay is
   `200`; a different server under a taken or reserved name, or a seventeenth
   one, is `409`. An unreachable server is `502 mcp_server_unavailable`, and
   one whose sign-in Veetbot cannot perform is `422 mcp_sign_in_unsupported`.
   `PATCH` changes only `access`. Deleting a missing server is `204`, so a
   retried delete is safe; an operator row is `409` for every write.

5. **Sign-in is the authorization-code flow with PKCE, completed on the
   server.** `POST .../sign-in` registers a client through RFC 7591 when none
   is stored for the current callback URL, stores a ten-minute, single-use
   state (SHA-256 only) with a sealed PKCE verifier, and returns
   `authorization_url`, `callback_scheme` (`veetbot`) and `expires_at`. The
   client opens the URL in an authentication session. The authorization
   server redirects to the callback, which consumes the state, exchanges the
   code with the verifier and the RFC 8707 resource, stores the sealed
   tokens, and answers `303` to
   `veetbot://mcp-sign-in?server_id=…&outcome=signed_in|failed`, or to
   `…?outcome=expired` for an unknown state. No upstream text reaches the page.
   `DELETE .../sign-in` forgets the tokens and keeps the registration.

6. **Tokens live in PostgreSQL, sealed, and refresh at use.**
   `mcp_server_authorizations` holds one row per server under row-level
   security, deleted with its server. Client secrets, verifiers and tokens
   are AES-GCM sealed with additional data naming the tenant, server and
   field. The runtime asks for an access token when it connects. A token
   within sixty seconds of expiry is refreshed under a row lock, so the API
   and the worker never spend one rotating refresh token twice, and a rotated
   refresh token replaces the old one in the same transaction. A rejected
   refresh clears the tokens and the server reads `sign_in_required`. In the
   once-per-session 401 ladder, step 2 for this scheme is a forced refresh.
   This reverses the tool system's "refresh tokens are not used" for this
   scheme only, because the code grant has no other way to stay signed in.

7. **Owner MCP traffic has its own audited transport.** A process-local proxy
   transport, `mcp_https`, carries every owner-added connection, discovery,
   registration and token request: `CONNECT` only, port 443, a public
   hostname, every resolved address public, the checked address dialled, and
   each decision logged. Adding the server is the owner's egress decision;
   the sandbox allowlist is unchanged and still governs sandbox code and
   operator-configured tenant rows. This is the same rule the browser and
   unsubscribe transports already use.

8. **Owner-added tools get their own index budget.** They never take a full
   definition. When the agent enables `tool.call`, they fill a separate
   `owner_tool_index` class (forty items, 2,000 tokens) rendered with the
   other discovered entries, and the rest are recorded in
   `skipped_tool_names`. Region A's ceiling rises from 22,000 to 24,000 tokens
   to hold it. A plan with no owner-added tools is selected exactly as
   before, so no chat is re-planned and the builder version is unchanged. A
   new or newly signed-in server reaches chats created afterwards, as roster
   changes always have.

9. **Settings changes reach chats already open.** Before every call to an
   owner-added server, in whichever process runs it, the runtime reads the
   server's current row. A removed server, the feature switched off, or an
   access level tightened from `read_only` to `changes` refuse the call with
   `tool.withdrawn`; a server signed out refuses it with
   `tool.server_unauthorized`. The refusal is terminal for that session's
   connection, which closes. A level loosened to `read_only` keeps working,
   because every call was authorized under the stricter one, and a call
   already dispatched finishes.

## Consequences

The owner can add MagicPath by URL and sign in from Settings, and the same
path serves any server that offers no authentication or the code flow.
Every request in a chat with an owner-added server carries up to 2,000 more
prefix tokens, almost all of them cached. A server that needs a static API
key or a pre-registered client is refused with `mcp_sign_in_unsupported`;
both are later additions to this scheme set. Revocation at the authorization
server is not attempted when the owner signs out or removes a server.

## Alternatives considered

- **Token paste only.** Smaller, but MagicPath offers no API key, so it could
  not connect.
- **A per-tool access choice.** Better fitted to mixed read/write servers, but
  the owner chose per-server access, and a per-tool list is a later UI.
- **Share the existing deferred index.** No budget change, but the index is
  full in production, so most of MagicPath's tools would be skipped.
- **A client-side redirect (`veetbot://` as the OAuth redirect URI).** No
  public callback, but authorization servers accept HTTPS redirects more
  reliably than custom schemes, and the code would pass through the client.
- **Let Chat add servers behind an approval.** A prompt injection could then
  propose an attacker's server; the owner kept management in Settings.
