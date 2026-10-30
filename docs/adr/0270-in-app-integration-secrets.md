# ADR-0270: An in-app store for integration keys

- **Status:** Accepted; MVP implementation authorized on 2026-10-30, production activation pending
- **Date:** 2026-10-30
- **Related:** ADR-0024, ADR-0067, ADR-0255, ADR-0268

## Purpose and scope

The owner wants to enter integration keys in Veetbot and have the platform
retain and use them securely, without a production Doppler dependency. The
MVP is a Settings screen, one encrypted PostgreSQL table, and a scoped adapter
for the existing credential broker. Agent-written jobs are the first consumer:
an approved job can read an API using a saved key without receiving its bytes.

The owner accepted this decision and authorized the MVP implementation on
2026-10-30. This does not authorize production activation of jobs. No milestone,
acceptance gate, policy version or production flag moves. This extends
ADR-0268 decision 8, which previously deferred a
Settings-managed credential store. The engineering plan's server-side broker,
no-credentials-in-sandboxes and no-secrets-in-events requirements remain intact.

## What we reuse

- The credential resolver in `src/agent_core/ports/credentials.py` and
  `SecretValue` wrapper. The existing mapping resolver continues serving
  operator-provisioned credentials.
- The MCP AES-GCM sealer in `src/agent_core/adapters/mcp/sealing.py`, its
  `SecretSealer` contract and the existing `cryptography` dependency. Extract
  only the shared sealing primitive; preserve the current MCP envelope and
  additional-data format so existing sign-ins need no migration.
- PostgreSQL repositories, row-level security, short transactions and
  `process_events` for Settings audit records, following ADR-0255.
- Native Settings, authenticated HTTPS routes and the existing `settings.read`
  and `settings.write` scopes. These scopes permit management of the caller's
  own records, never retrieval of a stored value.
- ADR-0268's private job grants and HTTP broker in `src/agent_core/adapters/jobs/http.py`:
  the grant chooses a credential for an exact host; job code cannot supply a
  credential argument, header or replacement destination policy.

Existing deployment keys, Gmail files, MCP OAuth grants, browser profiles and
the GitHub App stay where they are. Development Doppler use is unchanged.

## Owner experience

Settings gains **Integration keys**, with Add, Replace and Delete actions.
Add asks for a short label, an exact HTTPS origin such as
`https://api.example.com`, an authentication type, and the key in a secure field.
The MVP supports `Authorization: Bearer` and `X-API-Key` only. It accepts one
opaque, nonempty printable-ASCII value of at most 4 KiB, without whitespace;
PEM files, JSON credentials, passwords, cookies and OAuth flows are outside scope.

The screen shows the label, destination, authentication type, revision and
creation/update dates. It never returns the stored value, a suffix or a
value-derived fingerprint. It offers **Copy reference**, producing an opaque
`stored:<uuid>` reference that the owner may give Chat when requesting a job.
No secret-listing or secret-reading model tool is added. Labels are plain text,
bounded to 80 characters and screened for known credential patterns; they are
metadata, not another place to paste a key.

The client sends the value only to the dedicated API over authenticated HTTPS.
It keeps it only in the form's transient memory and clears it on save or dismiss;
it does not persist it in drafts, preferences, offline caches or telemetry.
There is no reveal, export, chat-message or attachment input path. If Chat needs
a missing key, it directs the owner to Settings instead of asking for the value.

Saving does not contact the destination or grant any job access. The owner
then approves the existing job grant, which displays the key's label,
destination and reference alongside the job's authority. Missing/deleted keys
produce an actionable Settings message; a successful save means stored, not
verified with the provider. The initial consumer supports authenticated GET
requests only. The UI recommends provider-issued read-only, narrowly scoped
keys; GET alone does not prove an endpoint is harmless or a key is read-only.

## One table and one host key

`integration_secrets` fields:

| Field | Purpose |
| --- | --- |
| `tenant_id`, `principal_id`, `id` | Owner identity and stable random UUID |
| `label` | Owner-facing name, not a lookup authority |
| `origin`, `auth_scheme` | Immutable destination and header behavior |
| `sealed_value` | Versioned AES-256-GCM ciphertext, nullable only after deletion |
| `revision` | Monotonic concurrency and replacement counter |
| `created_at`, `updated_at`, `deleted_at` | Lifecycle metadata |

Enforce owner predicates on every repository operation and tenant RLS using the
existing persistence convention. The list is bounded to 100 active keys per
owner; creation enforces that limit transactionally. References never resolve
by label, across principals, or through a fallback to an environment variable.
Deletion clears ciphertext and retains a small tombstone: an old grant can
never acquire a new secret because somebody reused its identifier. The origin,
authentication type and label are fixed for an ID; changing them means creating
a new key and obtaining a new grant. Replace changes only the secret value.

Each write uses a fresh 96-bit nonce. Additional authenticated data uses an
unambiguous canonical encoding of a distinct `veetbot.integration-secret.v1`
purpose, tenant, principal, ID, origin, authentication type and revision. Copying
ciphertext to another owner, destination or revision fails authentication.
Keep a version in the envelope for later migration; do not build a key hierarchy
or multi-key rotation service for this MVP.

Use a **separate random 32-byte master key**, not `MCP_AUTHORIZATION_KEY`.
Provision it once at `/etc/veetbot/secrets/integration-secrets.key`, root-owned
with mode `0400`. Systemd `LoadCredential` supplies it to the API and the
credential-bearing job execution roles through `INTEGRATION_SECRETS_KEY_FILE`.
Reuse the fail-closed private-file loader pattern, including owner, regular-file,
permission and length checks. Local development uses an explicitly provisioned
owner-only file outside the repository. Never generate a replacement at startup.

`AGENT_INTEGRATION_SECRETS_ENABLED` defaults off. With it off, the routes are
absent and every `stored:` reference fails closed. With it on, a missing or
malformed master key refuses startup of roles that need it. A valid but wrong
key fails decryption with a content-free error; it never overwrites ciphertext.
The scheduler, notification processes and execution service receive no master
key. A scheduler carries only a reference; the job's trusted broker resolves it.

## API and concurrency

Use three routes under `/v1/integration-secrets`, returning metadata only and
`Cache-Control: private, no-store`:

| Route | Scope | Behavior |
| --- | --- | --- |
| `GET /v1/integration-secrets` | `settings.read` | List the caller's active keys |
| `PUT /v1/integration-secrets/{id}` | `settings.write` | Create with expected revision 0, or replace with the current revision |
| `DELETE /v1/integration-secrets/{id}` | `settings.write` | Clear the value using the expected revision |

The client allocates the UUID before creation. PUT and DELETE serialize on the
owned row and compare the expected revision; concurrent creation uses the
unique ID constraint. Wrong-owner and unknown existing resources are 404,
missing scopes 403, malformed bodies 400, oversized bodies 413, and stale
revisions or attempts to recreate a deleted ID 409. Cap request bodies at
8 KiB before JSON parsing. PUT rejects unknown fields and attempts to alter
the immutable binding. DELETE takes its revision precondition in a header.

For a PUT retry after a lost response, return the current metadata without a
second mutation only when its revision is exactly the expected revision plus
one and the submitted binding and value match the current record. Compare the
decrypted value in trusted memory; store no plaintext request, request hash or
response-body receipt. Otherwise return 409 and let the client refresh. A
repeated DELETE of an owned tombstone returns 204. It cannot delete a later
replacement because active rows still require the matching revision.

Plaintext request fields never enter validation-error details, exception
messages, request logging, tracing, replay storage or audit payloads. Treat
the whole write body as sensitive, including parser failures. This route's
intentional secret ingestion does not exempt chat, memory or any other surface
from credential scanning. Audit create/replace/delete in the same transaction
as the row change, using only actor/owner IDs, secret ID, revision, action and
timestamp. Existing tool/run events record use without key bytes or ciphertext.

## Using a key through a job

1. Grant creation resolves the reference's metadata under the authenticated
   owner. It verifies that the grant's exact host matches the saved origin and
   displays that binding for approval. Merely naming a reference grants nothing.
2. The existing job broker obtains the reference from the approved grant, not
   from job-supplied request arguments. Both the operator host restriction and
   the grant must allow the request. New stored keys permit GET only; retain
   the existing GitHub App's separate read-only GraphQL exception unchanged.
3. A resolver constructed for that owner and grant checks the live row for
   every request, including scheduled runs. It validates ownership, reference,
   destination, authentication scheme and deletion before decrypting into
   `SecretValue`. This is a narrow broker capability, not a global resolver
   that will open any stored ID. Do not put saved keys in `Settings.credentials`.
4. The trusted HTTP adapter adds the selected header after URL and public-address
   checks, dials the checked address with the original TLS name, and refuses
   every redirect. Only `https`, public DNS hosts and port 443 are supported;
   URL userinfo, IP literals, wildcards and subdomain matching are refused.
   No keys in query strings, bodies, job environment, code, inputs or outputs.
5. Replacement is observed on the next resolution without restarting workers.
   Deletion makes the next resolution fail closed. A request already admitted
   with the previous value may complete; deletion cannot recall it or revoke
   the upstream key. The UI tells the owner to revoke at the provider when
   compromise is suspected. Replacing a value under the same binding preserves
   grants and the owner is told that existing approved jobs will use it.

No long-lived plaintext cache or authenticated connection pool is added.
Before returning upstream JSON to job code, reject responses echoing the used
key, including JSON-unescaped, URL-encoded, Base64 and Base64url forms. Fail with
a fixed code, never the response content. Cover errors and response keys as well
as values. This is bounded leakage detection, not protection against an approved
destination deliberately encoding or transforming its own credential. That
destination is a trusted recipient; the owner must trust it with the key.

ADR-0268's outstanding activation requirements still apply: private authoring
input screening, execution authority bound to approved code, operator destination
restrictions and credential-echo handling. This decision does not certify or
bypass them. Do not enable the job consumer until those requirements have
evidence; the Settings store can be implemented and tested independently.

## Recovery and the security boundary

Encryption protects database copies from disclosure without the host key.
It does not protect against compromise of the credential-bearing application
process or host root. This is the existing trusted-worker boundary, not a new
vault service or a hardware-backed guarantee.

Before storing real keys, the operator puts the master key in the owner's
off-host password manager or offline encrypted storage and verifies recovery
with a synthetic database row on a separate test instance. The key must never
be in the database backup, repository, CI or the same backup location as the
ciphertext. Database backups include ciphertext; secret values are not copied
to events. Document the actual database backup mechanism and test restoring
ciphertext plus the separately held key. Milestone 15's planned escrow command
is not an implemented prerequisite we can claim to have; its automation remains
separate work. Until regular backups exist, the MVP promises persistence on
the running database, not recovery of subsequent writes after database loss.

Loss of the master key means re-entering credentials; there is no recovery
backdoor. No automatic master-key rotation is included. If it is compromised,
disable the feature, revoke affected credentials at their providers, tombstone
the old records, provision and escrow a fresh master key, then create fresh
records and grants. Old backups remain sensitive and must not silently restore
old grants into an enabled deployment. Restore with the feature off and
reconcile deletions/revocations before enabling use. Delete clears the live row,
not PostgreSQL WAL or old backups; their established retention still applies.

## Delivery and acceptance

Implement in three small steps, with regression/contract tests
first and documentation alongside each behavior change:

1. Add the table, repository contracts, sealing reuse, key provisioning and
   metadata-only API. Test restart persistence and recovery with synthetic keys.
2. Add native Settings entry, replacement, deletion and reference copying.
   Test that no value survives in client persistence, validation output or logs.
3. Connect saved references to approved jobs without changing existing GitHub
   behavior. Test real composed broker/grant boundaries, then perform an
   authorized read-only provider smoke after the jobs activation prerequisites.

Acceptance evidence must cover:

- A key saved in the app survives restart, authenticates one approved GET and
  never reaches model context, sandbox, logs, errors, audit, public job source
  or artifacts. Exercise success and failing HTTP/API paths with synthetic keys.
- Wrong tenant/principal, ungranted reference, destination mismatch, private
  address, redirect, unsupported method and missing/deleted key all refuse use.
- Tampered ciphertext, wrong key, wrong binding and missing key file fail closed;
  ciphertext copied to another record cannot open. Test PostgreSQL RLS too.
- Concurrent saves and lost-response retries produce one effective mutation;
  stale replacements/deletes do not overwrite a newer value. Replacement and
  deletion take effect across two independent worker processes at next resolve.
- Disabled feature, existing GitHub App, MCP OAuth and environment credentials
  retain their prior behavior. A copied reference alone grants no execution.
- A synthetic backup restores with the escrowed master key and fails without
  it. Operators can follow the documented loss/compromise procedure.

Implementation evidence is tracked in project state. Production review, CI,
escrow and delivery evidence remain outstanding. The implementation updates
the API, bootstrap, persistence and security documentation, extends ADR-0268's
credential contract, and documents both configuration variables in the
environment examples. Normal review/CI/deployment evidence remains required.

## Deferred deliberately

No production Doppler, Vault, KMS, new service, secret sharing, folders, arbitrary
header templates, provider discovery, OAuth rewrite, automatic rotation, secret
history, bulk migration, reveal/export endpoint or new model tool. Authenticated
POST, MCP static-key support and other consumers are later extensions with
their own authorization contracts. The shared store, opaque references and
versioned ciphertext leave room for them without building them now.
