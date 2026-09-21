# Bland calling setup

The Milestone 27 implementation is disabled by default. The owner approved public
message-taking, answers from a reviewed public profile, individually approved
outbound briefs, and owner-visible transcripts and summaries. Account activation
still needs the owner's number, profile, credentials and a live test recipient.
This guide does not authorize a purchase, account change, publication or test call.

## Save the API key privately

From the repository, run this command with a new absolute path outside Git:

```bash
uv run python -m bland_mcp bootstrap --output-file /absolute/private/bland-api-key
```

The hidden prompt creates a mode-0600 file and refuses to overwrite an existing
file or symlink. Do not put the key in chat, shell arguments, source control or
the public profile. Set `BLAND_API_KEY_FILE` to that path in the application and
call worker environments. Inline `BLAND_API_KEY` is rejected.

On the production host, save the key in the existing traverse-only secret
directory, which is the path in the example environment:

```bash
sudo /opt/veetbot/current/.venv/bin/python -m bland_mcp bootstrap \
  --output-file /etc/veetbot/secrets/bland-api-key
```

The command creates a missing parent directory with mode 0700. The release
preflight cannot traverse such a directory (see
[the service roles](#separate-the-two-service-roles)).

Inspect the account's numbers in the Bland dashboard first. Do not buy a number
automatically. Confirm the selected number belongs to the same account as the
key and supports the required incoming and outgoing destinations. The account
label below is a local binding, not a provider credential or caller identity.

## Review the public configuration

Create `/etc/veetbot/bland-configuration.json` on the server after the owner has
chosen a number and public name. This example contains placeholders:

```json
{
  "version": 1,
  "account_id": "primary",
  "phone_number": "+14155550100",
  "public_name": "OWNER'S APPROVED PUBLIC NAME",
  "assistant_name": "Veetbot",
  "public_profile": "Take messages only. No additional public facts are approved.",
  "voice": "maya",
  "max_duration_minutes": 5,
  "webhook_url": "https://YOUR_API_HOST/webhooks/bland"
}
```

`public_name` names the owner; `assistant_name` names the voice assistant and
defaults to `Veetbot` when omitted. `voice` selects the provider voice independently.
For example, owner `Andy`, assistant `Willow` and voice `Willow` produce
“Hi, this is Willow, Andy's assistant.” Neither direction announces AI identity
or transcription unprompted; asked directly, the assistant confirms both
(ADR-0108). The same identity is included in approved outbound call instructions. Deploy a
release supporting `assistant_name` before adding the field; older releases
reject it. Upgrading to the explicit assistant identity changes the normalized
configuration revision, so obtain fresh approvals for any pending calls.

The file is a bounded, closed schema. Keep it writable only by the operator.
Its SHA-256 revision pins the outbound approval; restart the application and
workers after a reviewed change. Existing approvals using an old revision cannot
dispatch against a new profile. Never copy private persona, email, contacts,
memories or previous conversations into this file.

Generate the exact proposed inbound settings without network access:

```bash
uv run python scripts/prepare_bland_profile.py \
  --configuration-file /etc/veetbot/bland-configuration.json
```

Review the generated JSON before applying it through Bland's number settings or
the operator's authenticated `POST /v1/inbound/{phone_number}`. It includes the
owner's introduction, truthful answers when asked about AI identity or
transcription, the approved profile, message-taking instructions,
a five-minute ceiling, recording off and no tools, transfers or dynamic context.
It explicitly clears pathway and memory references. A pathway can override the
prompt, so a prompt update alone is insufficient. Read back the number settings
and verify that every capability was actually cleared; a provider rejecting or
ignoring a clearing field blocks activation. Review any account-level memory or
persona attachment separately. Repeat the comparison after any provider change;
configuration drift must be reconciled before activation.
[Update number](https://docs.bland.ai/api-v1/post/inbound-number-update),
[Read number](https://docs.bland.ai/api-v1/get/inbound-number).

Confirm the actual plan's concurrency, rate, destination, usage and spending
controls. Veetbot reserves at most one outbound call. Inbound concurrency uses
the reviewed provider plan's limits; activation does not require reducing it to
one call at a time. Public incoming calls begin at Bland before any webhook
reaches Veetbot: local throttling cannot cap their spend. Verify provider-side
limits and busy behavior before enabling a public number. Do not assume that
this account has a particular plan or price.
[Agent Phone Plan](https://docs.bland.ai/platform/agent-phone-plan).

## Separate the two service roles

The application holds the private Bland credential for approved outbound tools.
The dedicated `veetbot-call` worker holds it for bounded result reconciliation
and termination. It has no owner API bearer or model credentials. Both calling
roles act for the owner tenant and principal but hold none of the owner's
scopes, so their environments leave `AUTH_SCOPES` empty. The separate
`veetbot-call-ingress` listener has only a webhook signing secret and receipt
database access. It verifies the raw bytes before parsing, queues only a call ID,
and listens at `127.0.0.1:8003`; the public proxy exposes only `/webhooks/bland`.

Provision the unprivileged Linux user/group `veetbot-call-ingress` before enabling
its unit. Never add it to the `veetbot` group, which can read the application
environment file. The listener must not be able to read that file. The signing
secret belongs to `veetbot-call-ingress` and the Bland API key to the
application's `veetbot` user; no other identity can read either. Keep the
secret in its own directory, `/etc/veetbot-call-ingress/`, rather than in the
application's secret directory. The reviewed public configuration may be
readable by both roles.

Release preflight runs as the unprivileged deploy identity, without sudo:
`veetbot` on the current host and `veetbot-deploy`, a member of the `veetbot`
group, in the [documented host model](deployment.md#one-time-host-preparation).
It reads both calling environment files, and inspects each credential with
`stat` and `getfacl` without opening it. Each calling environment file is
therefore owned by `root:veetbot` with mode 0640, like the application
environment. systemd reads the file as root before starting the unit, so the
listener needs no access to it. Every directory above a credential must let the
deploy identity traverse it: mode 0711 lets it reach the file without listing
the directory.

Install the host's `acl` package before enabling calling. Preflight requires
each credential to be a non-symlink regular file with mode 0600, the exact
service owner above, and only the base owner/group/other ACL entries.
Additional ACL entries are rejected, including currently masked grants.

Create distinct PostgreSQL login roles, with credentials set privately. Neither
role may own the tables, inherit privileged memberships, be a superuser, or have
`BYPASSRLS`, database/schema creation or unrelated table privileges. Apply the
normal migration as the migration operator, then provision these privileges:

| Role | Required access |
| --- | --- |
| `veetbot_call_ingress` | Schema usage; `SELECT` on `alembic_version`; `SELECT, INSERT, UPDATE` on `call_records`, restricted to receipt rows below |
| `veetbot_call` | Schema usage; `SELECT` on `alembic_version`, `sessions`, `memories`; `SELECT, INSERT, UPDATE` on `call_records`; `SELECT, UPDATE` on `events`, `tool_invocations`, `runs`, `session_history_items`, `recall_traces`, `artifacts`; `SELECT, DELETE` on `checkpoints`, `integrated_episodes`, `knowledge_documents`; `SELECT, INSERT` on `notification_outbox` when call notifications are enabled |

The migration forces tenant RLS on call records. In addition, the migration
operator must restrict the ingress role to content-free receipts:

```sql
CREATE POLICY call_ingress_receipts_only ON call_records
  AS RESTRICTIVE FOR ALL TO veetbot_call_ingress
  USING (kind = 'receipt') WITH CHECK (kind = 'receipt');
```

Verify that the ingress role cannot read call rows, sessions, transcripts or
other tables, and that it cannot insert a non-receipt row. Verify the worker can
commit a call and notification atomically and can erase a call's derived copies.
No sequence privileges are needed for the UUID/key-addressed calling writes.
Existing general maintenance removes expired artifact bytes; call erasure first
expires their records and removes knowledge derived from those artifacts.

Copy `deploy/veetbot-call.env.example` and
`deploy/veetbot-call-ingress.env.example` to their configured environment paths.
Use role-specific `DATABASE_URL` values. The owner tenant/principal, configuration
path and three calling flags must match the application environment. Do not copy
the full application environment into either role. Use the ingress-specific
secret directory for `BLAND_WEBHOOK_SECRET_FILE`.

Write the webhook signing secret to
`/etc/veetbot-call-ingress/bland-webhook-secret` without echoing it or putting
it in a shell argument. Then apply the ownership and modes; the commands are
safe to rerun:

```bash
sudo chown root:veetbot /etc/veetbot/veetbot-call.env \
  /etc/veetbot/veetbot-call-ingress.env
sudo chmod 0640 /etc/veetbot/veetbot-call.env \
  /etc/veetbot/veetbot-call-ingress.env
sudo install -d -m 0711 /etc/veetbot/secrets
sudo install -d -m 0711 -o veetbot-call-ingress -g veetbot-call-ingress \
  /etc/veetbot-call-ingress
sudo chown veetbot:veetbot /etc/veetbot/secrets/bland-api-key
sudo chown veetbot-call-ingress:veetbot-call-ingress \
  /etc/veetbot-call-ingress/bland-webhook-secret
sudo chmod 0600 /etc/veetbot/secrets/bland-api-key \
  /etc/veetbot-call-ingress/bland-webhook-secret
sudo setfacl -b /etc/veetbot/secrets/bland-api-key \
  /etc/veetbot-call-ingress/bland-webhook-secret
```

`deploy/app/release.test.sh` replays these commands and releases as both deploy
identities, so change the two together. A host prepared under earlier
guidance can fail with `calling role environment is not readable by the deploy
user` or `calling role credential directory is not traversable by the deploy
user`. Earlier guidance left the environment files owned by their service with
mode 0600, and the credential directories at mode 0700. Rerun the commands
above. If a credential is kept elsewhere, give each directory above it mode
0711.

## Activate and verify

Keep all flags at zero until the reviewed number configuration, database roles,
secret ownership and provider-side admission limits are verified:

| Setting | Meaning |
| --- | --- |
| `AGENT_CALL_ENABLED=1` | Add the fixed Bland tool rosters, owner call API and reconciliation worker |
| `AGENT_CALL_INGRESS_ENABLED=1` | Add the separate signed callback listener and exact public proxy route |
| `AGENT_CALL_NOTIFICATIONS_ENABLED=1` | Enqueue `call_finished`; requires the existing notification API and delivery setup |

Enabling the third flag touches all three environments. Each calling role
validates on its own, so the application environment and both calling role files
need `AGENT_CALL_NOTIFICATIONS_ENABLED=1`, and each calling role file also needs
`AGENT_NOTIFICATION_API_ENABLED=1` with `AGENT_NOTIFICATION_DISPATCH_ENABLED=1`,
which must always match each other. Those roles only enqueue: their push provider
stays disabled and the notification worker still delivers. Grant `veetbot_call`
`SELECT, INSERT` on `notification_outbox` before enabling, or its reconciliation
fails on the first finished call. A role whose environment fails validation exits
and its unit stops, so change the three files together and restart both calling
units with the application and worker units.

Give the owner API credential `call.read`, `call.cancel`, and `call.delete` as
needed. The model's Bland tools use separately installed MCP scopes. Incoming
callers receive none of these scopes. Notifications say only “New call result”
and contain call and notification IDs. Tapping one opens an authenticated result
sheet in Chat. The owner can also ask Chat to list or read retained calls.

Configure the account's separate webhook signing secret without rotating an
existing secret used by other integrations. The listener accepts only the
64-character lowercase hexadecimal SHA-256 HMAC in `X-Webhook-Signature` over
the exact raw HTTP body. Bland's example serializes JSON: prove the actual
delivered bytes against a private real fixture before enabling production
intake. Do not add an unsigned fallback or accept multiple signing conventions
without a reviewed change. Never save that secret or a real transcript in the
repository. [Webhook signing](https://docs.bland.ai/tutorials/webhook-signing).

Use the existing deployment procedure once separately authorized. A host whose
sudo contract predates calling support lacks the calling rules, so reinstall
`deploy/sudoers/veetbot-deploy` as [deployment](deployment.md#one-time-host-preparation)
describes before the first calling release. The release script validates
matching role bindings, grants the ingress user read access to the new release
and installs/restarts the optional units. It then fails unless each enabled
calling unit is still running without an automatic restart, so a unit that
cannot start fails the release. The proxy includes its call route only when the
active release enables ingress. Verify both units, HTTPS routing and release
identity. During rollback, stop both calling units before switching releases;
restart only units supported by the target release and restore its flags and
proxy configuration.

With an explicitly approved test recipient, verify one inbound call and one
outbound call. Confirm the greeting, transcription disclosure, voice quality,
duration limit, exact-brief approval, signed callback, summary, notification,
deletion, no-answer behavior and post-dispatch termination. A successful dispatch
means accepted, not answered. If its outcome is uncertain, reconciliation reads
provider history and never redials; keep the reservation until resolved.

Local transcripts and summaries expire after thirty days. Owner deletion also
redacts derived chat content, checkpoints, tool results and later generated
content in affected sessions; it preserves original owner messages. Active runs
must settle first. Other devices refresh their presentation on the next fetch.
Provider retention, recordings, external exports and backup retention remain
separate operational controls: local deletion does not claim deletion at Bland.

Milestone 27 remains in progress until live admission, signed delivery and the
authorized review/release evidence pass. See the
[canonical design](plan/bland-calling.md) and
[project state](status/project-state.yaml).
