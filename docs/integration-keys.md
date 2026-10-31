# Integration keys

The ADR-0273 MVP stores owner-entered keys in PostgreSQL, encrypted with a
separate host-held AES-256-GCM key. It adds no production Doppler dependency.
It is off by default; this implementation does not enable production use.

## In the app

Open **Settings → Integration keys**. Add a label, an exact public HTTPS origin
(such as `https://api.example.com`), Bearer or X-API-Key authentication, and the
key. Values must be 1–4096 printable ASCII characters without whitespace. The
origin uses a lowercase ASCII hostname (Punycode for international names) with
no trailing dot, path, port, query, user information, wildcard or IP address.
Use provider-issued read-only keys with the smallest necessary scope.

Saving stores the key; it does not test it against the provider or approve a
job. **Copy reference** copies only `stored:<uuid>`. Give that reference to Chat
when requesting an integration. Never paste the key into Chat, a job or an
attachment. The app never reads back the value or persists the secure form.
The metadata list shows labels, destinations, authentication types, revisions
and dates. Each owner can have at most 100 active keys.

Replace changes only the value. Existing approved jobs use the replacement on
their next request. Changing a label, destination or authentication type means
adding a new key and approving a new grant. Delete clears the live ciphertext
and permanently tombstones the reference. It does not revoke the upstream key,
cancel a request already admitted, or remove copies from old backups/WAL.
Revoke at the provider too when compromise is suspected.

## Provision and enable

First deploy the additive migration through `e8a270b10301`. Keep the feature off
until the operator has escrowed the master key separately and verified a
synthetic restore. No migration or startup generates a master key.

1. Provision one cryptographically random, raw 32-byte key at
   `/etc/veetbot/secrets/integration-secrets.key`, owned by root with mode `0400`.
   For a new installation, use an exclusive creation operation so a rerun cannot
   overwrite the existing key. Do not use a text/base64 file or reuse the MCP key.
2. Store that key in the owner's off-host password manager or encrypted offline
   storage. Never put it in PostgreSQL, source control, CI, logs or the location
   holding database backups. Losing it makes existing ciphertext unrecoverable.
3. Create root-owned `/etc/veetbot/integration-secrets.env` with
   `AGENT_INTEGRATION_SECRETS_ENABLED=1`. Leave the flag off in the shared
   environment. Do not place the key bytes or its path in that shared file.
4. Install `deploy/systemd/integration-secrets.conf` as a systemd drop-in for
   `veetbot-api.service`. After the separate jobs activation requirements are
   satisfied, also install it for the interactive and async run workers
   (`veetbot-worker.service`, `veetbot-async-worker.service`). Legacy deployments
   must identify each trusted role that actually executes jobs before enabling
   the consumer. Do not install it on maintenance, schedule, notify, surface,
   call, ingress or execution services.
5. Reload systemd and restart the opted-in roles. `LoadCredential` supplies a
   private file at `%d/integration-secrets`; the loader requires an absolute,
   regular file owned by the process user, mode `0400` or `0600`, exactly 32
   bytes, and refuses symlinks. Missing or invalid files refuse startup. A wrong
   key refuses decryption/replacement without rewriting the existing ciphertext.
6. Confirm the owner's token has `settings.read,settings.write`. Test with a
   synthetic key before storing a real provider key. A disabled server exposes
   no integration-secret routes; the client reports that keys are not enabled.

For local development, explicitly create an owner-only raw key file outside
the repository and set both `AGENT_INTEGRATION_SECRETS_ENABLED=1` and
`INTEGRATION_SECRETS_KEY_FILE` to its absolute path. The master key is distinct
from `Settings.credentials` and never enters the sandbox environment.

## Job access

New sessions use `job.run` version 1.1.0 when the store is enabled; pinned
version 1.0.0 keeps its prior specification and cannot propose stored bindings.
The first consumer is an approved job grant with a host mapping such as
`{"api.example.com":"stored:<uuid>"}`. Approval shows the label, origin and
reference. Ownership and the exact origin are checked before granting access
and again on every HTTP request. Naming a reference alone grants nothing.

Stored keys support GET only, without a request body. The existing GitHub
reader's read-only GraphQL exception is unchanged. The operator host restriction,
public DNS/address checks, pinned dialing, TLS hostname checks and redirect
refusal still apply. The broker injects the header only after those checks.
It rejects upstream JSON that echoes the key directly, JSON-unescaped,
URL-encoded, Base64-encoded or Base64url-encoded, including errors and object
keys. Arbitrary transformations by a malicious provider are outside that
bounded check: the chosen destination is trusted with its own key.

Keep jobs disabled until ADR-0268's outstanding activation requirements have
separate evidence. Local secret-store tests do not satisfy those broader gates
or constitute a live provider smoke test.

## Backup, restore and loss

The production host runs a nightly `veetbot-backup.timer`. It takes a PostgreSQL
dump and archives host state, including `/etc/veetbot`, in the same encrypted
off-host backup. Before provisioning the master key, exclude
`etc/veetbot/secrets/integration-secrets.key` from the state tar and verify
the exclusion against a synthetic file. Preserve it whenever the host backup
script is replaced. The database dump retains ciphertext and tombstones.
Keep the master key in the owner's separate off-host escrow.

On a separate test instance, restore a database backup with the feature off,
provision the escrowed key, and verify that a synthetic stored row decrypts.
Verify that a different key refuses it. Record the backup date and restore
outcome without values or ciphertext. The automated tests exercise a logical
row snapshot and fresh worker processes; they do not certify a production
backup service or the operator's escrow.

Restore production with the feature off. Reconcile deletions and revocations
since the backup before enabling use; restoring an old grant must never silently
restore its authority. Stop credential-bearing requests while recovering. On
master-key loss, delete the unusable records and re-enter provider credentials
under fresh references. There is no recovery backdoor.

On suspected master-key compromise: disable the feature on every opted-in role,
revoke the affected provider keys, then use a controlled Settings-only recovery
window to delete all old records (deletion does not require decryption). Close
that window, provision and escrow a fresh master key, and add replacement keys
under fresh IDs and grants. Preserve tombstones. Old backups remain sensitive.
There is no automatic master-key rotation or historical-value service.

To disable, set the opt-in env file's flag to `0` and restart every opted-in role;
remove the drop-ins if desired. Keep the additive schema. Downgrade refuses while
any records or tombstones exist, rather than silently reviving old references.
