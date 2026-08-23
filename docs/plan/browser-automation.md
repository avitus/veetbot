---
title: Authenticated Browser Automation
status: implementation
canonical: true
---

# Authenticated browser automation

This specification expands [engineering-plan.md](engineering-plan.md#33-authenticated-browser-automation)
and records the mechanism selected by
[ADR-0058](../adr/0058-authenticated-browser-automation.md). It defines a
general capability for operating authenticated websites. A language-learning
site is an evaluation example, not a privileged product integration.

## Goals and boundaries

The platform may open a website through a principal-owned browser profile,
observe the rendered page, and perform policy-authorized interactions. The
same contract must support local device browsers and isolated hosted browsers.
Provider-specific automation APIs, selectors, cookies, and credentials do not
enter model-visible schemas.

The first three construction slices provide navigation and observation through
a provider-neutral `BrowserProvider`, a default-off ephemeral Playwright
adapter, and revision-bound interaction through `browser.act`. Persistent
profile storage, user-delegated authentication, standing grants, Milestone 11
scheduling integration, downloads, uploads, and audio land in later slices of
this tranche. Browser automation remains disabled and absent from the registry
until a provider is explicitly bound.

The following are not goals:

- bypassing CAPTCHA, multi-factor authentication, paywalls, bot controls, or a
  site's access restrictions;
- exposing arbitrary JavaScript, DevTools, cookies, local storage, headers, or
  raw DOM to the model;
- allowing page text to authorize an action or broaden a domain grant;
- using a user's ordinary browser profile without an explicit profile binding;
- concealing automation where a site requires identification or forbids it.

## Trust and threat model

Every page, accessibility node, screenshot, redirect, download name, and error
originating at a website is `EXTERNAL_UNTRUSTED`. Page content can propose no
policy, tool, memory, skill, profile, grant, credential, or schedule change.
The important threats are prompt injection in rendered content, credential and
cookie disclosure, cross-origin navigation, stale element actions, duplicate
external writes after retry, tenant/profile confusion, malicious downloads,
browser escape, and a scheduled run continuing after its authorization changes.

The worker owns policy and orchestration but never receives raw website
credentials or cookies. A browser provider owns the browser process and opaque
profile material. Sandboxed model-generated code receives neither. The model
sees bounded observations and opaque element references only.

## Capability model

`BrowserProvider` is bound by trusted composition to exactly one principal,
one opaque profile reference, and one domain policy for an execution. A hosted
deployment may pin that binding globally or resolve it from the session created
by an authenticated principal; the resolved per-session adapter still owns
exactly one profile and origin policy. It exposes:

```text
navigate(BrowserNavigateRequest) -> BrowserObservation
observe() -> BrowserObservation
act(BrowserActionRequest) -> BrowserObservation
close() -> None
```

The initial implementation supplies the port and the first two operations.
`act` is specified now so the observation and revision contract does not have
to change when writes arrive.

A `BrowserObservation` contains only:

- the final public HTTPS URL, optional title, and an opaque page revision;
- bounded readable text;
- bounded interactive elements with opaque reference, role, accessible name,
  and state needed to choose an action;
- an optional screenshot artifact reference added by a later slice.

It excludes HTML, scripts, styles, hidden inputs, password values, request and
response headers, cookies, storage, browser logs, and provider diagnostics.
Element references are valid only for the observation revision that produced
them. An action supplies `expected_revision`; a mismatch fails before the
provider dispatches the action.

## Tool contract

The stable builtin namespace is `browser`:

| Tool | Operation | Side effect | Risk | Idempotency |
| --- | --- | --- | --- | --- |
| `browser.navigate` | Open one allowed public HTTPS URL | `NETWORK_READ` | `LOW` | `READ_ONLY` |
| `browser.observe` | Read the current rendered page | `NETWORK_READ` | `LOW` | `READ_ONLY` |
| `browser.act` | Click, type, select, check, press, or scroll | `EXTERNAL_WRITE` | `HIGH` | `NON_IDEMPOTENT` |

The write classification is deliberately conservative. A click that appears
local can submit a form, mark a message read, accept terms, or mutate an
account. `browser.act` therefore follows the external-write approval path even
when a provider predicts that one action is harmless. Separate read-only tools
keep observation available without granting mutation.

The model never selects a tenant, principal, profile, device, provider, cookie
jar, credential, or grant in tool arguments. Those values come from the pinned
run and trusted composition. Interactive clients may ask the session-creation
surface to bind one principal-owned `READY` profile by opaque UUID. The server
stores that UUID under a reserved, non-model-visible metadata key only after a
tenant/principal-scoped repository read; ordinary client metadata cannot set or
override it. A URL cannot authorize its own origin. The bound
domain policy validates the initial URL, every redirect, the final URL, popup,
iframe, and resource navigation according to provider enforcement rules.

Downloads, uploads, clipboard access, notifications, geolocation, camera,
microphone, password-manager access, and new windows are denied until each has
its own classified contract. Cross-origin popups are closed and reported.
The ephemeral provider also refuses typing into password fields or controls
whose autocomplete semantics identify a current password, new password, or
one-time code. Opaque references bind stable element handles rather than
selectors that could retarget after DOM reordering.

## Profiles and authentication

A durable `BrowserProfile` belongs to one tenant and principal and contains an
opaque provider reference, allowed origins, lifecycle status, creation and
last-use metadata, and encryption-key version. It contains no cookie or token
bytes. Provider profile material is encrypted at rest outside PostgreSQL and
is addressable only through the opaque reference.

Authentication is a user-controlled ceremony, not a model tool:

1. The user creates or selects a dedicated browser profile.
2. An interactive surface opens that profile directly to the identity
   provider or website.
3. The user enters passwords, passkeys, and MFA codes without model access.
4. The provider seals the resulting profile and reports only success, expiry,
   and allowed origins.
5. Expiry, CAPTCHA, reauthentication, or consent pages return `needs_user` and
   suspend rather than invite the model to handle a secret.

Profiles can be revoked and deleted. Deletion removes provider material and
invalidates every lease and standing grant that refers to the profile.

### Profile control-plane contract

The orchestration boundary uses two ports. `BrowserProfileRepository` stores
metadata only. `BrowserProfileControlPlane` is implemented by the isolated
profile provider and owns all secret-bearing material. No method on either port
returns cookies, storage state, passwords, tokens, headers, or an encryption
key.

`BrowserProfile` contains an opaque UUID, tenant and principal ids, a provider
name and opaque provider reference, the exact normalized allowed origins,
status, generation, encryption-key version, and creation/update/last-use
timestamps. The provider reference is control-plane data: public application
views and model tools omit it. The lifecycle states are `PROVISIONING`,
`AUTHENTICATION_REQUIRED`, `READY`, `NEEDS_USER`, and `REVOKED`. Deleted
profiles are absent rather than represented by a reusable state.

Creation first reserves the UUID as scoped `PROVISIONING` metadata with no
provider reference. It then asks the control plane to provision encrypted
material and atomically binds the returned opaque reference under the
reservation's expected generation. This ordering prevents an id collision or
concurrent duplicate from provisioning and then deleting an existing profile's
material. Provision failure marks the reservation revoked; bind failure deletes
the newly provisioned material before marking the reservation revoked. A
profile becomes `READY` only after the control plane reports successful
user-delegated authentication. Every state transition uses an expected
generation; a winner increments the generation so an already issued lease
cannot remain valid accidentally.

Revocation first commits `REVOKED` metadata with a new generation and then asks
the control plane to revoke every provider lease. From the metadata commit
onward, acquisition and action revalidation fail closed even if provider
cleanup must retry. Deletion is allowed only from `REVOKED`: it deletes provider
material before removing metadata, so a database failure cannot leave an
apparently deleted profile whose material remains usable. Repeating revoke or
delete is idempotent. Tenant/principal mismatch is indistinguishable from an
unknown profile.

The implementation includes the domain contract, shared repository and
control-plane contract suites, memory and PostgreSQL metadata adapters, the
separately deployed encrypted control plane, and the hosted session provider.
Its HTTPS client sends the tenant/principal/profile scope and a stable
idempotency key, bounds responses, and redacts provider diagnostics. Encrypted
profile material, live browser state, and login interaction remain wholly
inside the isolated service; orchestration receives only secret-free metadata,
opaque service capabilities, and bounded observations.

### Durable profile metadata

PostgreSQL stores one `browser_profiles` row per profile. Its columns are the
metadata fields above; there is deliberately no material, cookie, token,
storage-state, credential, header, or encrypted-blob column. Provider name,
opaque provider reference, and encryption-key version are nullable only while
the row is an unbound `PROVISIONING` reservation or a revoked reservation whose
provisioning never completed. A partial unique index prevents two live metadata
rows from naming the same non-null provider reference.

The table has a tenant/principal/creation index, a non-negative generation
constraint, a closed status constraint, and a binding-consistency constraint.
Tenant row-level security is both enabled and forced. Repository reads and
writes additionally include tenant and principal predicates so the in-memory
and PostgreSQL adapters have the same visibility contract even before RLS is
considered. Create uses insert-on-conflict detection; bind, transition, and
delete each put the expected generation and owning principal in the write
predicate and distinguish a missing row from a stale or invalid state without
ever returning another principal's metadata.

## Sessions, isolation, and provider placement

One browser lease belongs to one run attempt and one profile. Concurrent use of
the same mutable profile is rejected unless the provider offers copy-on-write
isolation with a single serialized commit. The lease has a deadline and is
closed on terminal run, cancellation, policy invalidation, or worker loss.

Two placements implement the same port:

- A device provider operates a dedicated profile on a connected user device
  through the reserved `device.*` routing seam. Device absence returns
  `tool.device_offline`.
- A hosted provider operates an isolated browser process with encrypted
  profile storage. It is preferred for reliable unattended schedules, but it
  requires the profile and login surfaces before rollout.

The ephemeral adapter launches a non-persistent headless Chromium child process
with a temporary home, scrubbed environment, downloads and service workers
disabled, popup closure, request interception, and the audited worker egress
proxy. Both layers enforce an exact public-HTTPS origin set; the proxy performs
DNS resolution and blocks loopback, link-local, private, metadata, and
single-label destinations. This adapter holds no durable authentication state
and is not the hosted-profile topology.

The hosted-profile provider additionally runs in OS/container isolation with
resource limits, no host filesystem access, and encrypted profile storage.
That stronger boundary is required before persistent authentication or
unattended rollout. Playwright documents non-persistent contexts as isolated
incognito-like sessions and requires a separately installed browser binary;
see the [browser-context](https://playwright.dev/python/docs/api/class-browsercontext)
and [browser-installation](https://playwright.dev/python/docs/browsers)
documentation.

### Hosted profile control plane boundary

The hosted control plane is a separately deployed process, not a filesystem or
encryption helper imported by the API, worker, or model runtime. Its service
identity is the only Veetbot identity permitted to read the profile-encryption
key and profile-material volume. The orchestration processes receive neither.
The authenticated control channel accepts only provision, revoke, and delete;
it has no material-export, cookie-export, token-export, arbitrary-file, or
arbitrary-browser-command endpoint.

Every request carries a trusted tenant, principal, profile identifier, exact
allowed-origin set, and an idempotency key. The service binds those values into
the encrypted record and rejects a replay whose scope differs. It returns only
an opaque provider reference and encryption-key version. Provider references
are random capabilities, are never filesystem paths, and are insufficient by
themselves: service authorization also verifies the caller identity and
tenant/principal binding. Revoke synchronously prevents new leases; delete is
idempotent and makes both active and future leases unusable before reporting
success.

Stored material uses authenticated encryption with a fresh nonce and binds the
profile identifier, tenant, principal, provider reference, allowed origins,
format version, and key version as authenticated metadata. Writes are atomic;
plaintext is never written to durable storage or logs. Startup fails closed
when the key source, storage permissions, or durable schema is invalid. Key
rotation is explicit and crash recoverable, retains the old key only while
records remain on that version, and never exposes either key to orchestration.

The initial store format is a versioned JSON envelope containing only the
authenticated metadata, a 96-bit random nonce, and ciphertext produced by
AES-256-GCM. The canonical JSON encoding of every metadata field is additional
authenticated data. Keys are exactly 256 bits and are resolved by an opaque
version through a service-local `ProfileKeyring`; no fallback or derived
default key exists. The implementation uses the maintained `cryptography`
package rather than implementing a primitive. Plaintext material is bounded to
64 MiB before encryption, the envelope is bounded before parsing, and malformed,
unknown-version, missing-key, duplicate-profile, or authentication-tag failures
fail closed.

`EncryptedProfileStore` is an internal service port, not an orchestration port.
It supports scoped create, metadata lookup by profile id or provider reference,
internal material load/update, revoke, delete, and one-record rotation. Create
is exclusive. Updates stage a same-directory file, flush and `fsync` it, replace
the destination atomically, and `fsync` the directory. Delete unlinks and
`fsync`s the directory. The filesystem adapter hashes opaque provider
references into filenames and never treats them as paths. On construction it
requires a private store directory, validates every existing envelope and its
authenticated tag, rejects duplicate profile ids, and therefore cannot start
partially over corrupt or inaccessible state.
The POSIX filesystem implementation serializes refresh-and-write operations
with a private store lock so two live service processes cannot assign different
provider references to the same profile or reuse one reference concurrently.

The lifecycle service uses the profile id as its durable idempotency identity.
A repeated provision with the exact tenant, principal, and origin scope returns
the original opaque reference after restart; a changed scope conflicts. Revoke
atomically records the revocation fence before returning, and internal material
loads then fail. Delete requires the exact profile/ref/scope tuple and is
idempotent only when nothing occupies either identity. Rotation decrypts with
the record's named old key and atomically rewrites under the current key. A
rotation sweep is restartable record by record: already-current records are
no-ops, and operators may remove an old key only after no envelope names it.

The session/data-plane port is distinct from the lifecycle control plane. It
opens a browser only for an authorized lease, applies the stored origin policy
server-side, and returns bounded observations rather than profile material.
Durable profile gate 8 therefore aggregates the isolated service,
encrypted-store tests, deployment isolation checks, hosted provider, and
session integration rather than treating the lifecycle client as sufficient.

### Lifecycle service HTTP and deployment contract

The lifecycle route family exposes exactly three authenticated mutations that
match the hosted client. The same isolated service also owns the separately
specified session and authentication route families plus unauthenticated
`/health/live` and `/health/ready` probes; it disables documentation and OpenAPI
routes. Every lifecycle mutation requires `application/json`, a Bearer service credential, and the
operation-specific `Idempotency-Key` value
`browser-profile:{profile_id}:{operation}`. Authentication runs before body
buffering. Bodies are capped at 64 KiB including chunked requests, request
models reject extra fields, route and body profile identifiers must match, and
all tenant, principal, reference, and origin bounds are validated before the
service core runs.

Provision returns only the provider name, opaque reference, and key version
with status 201. Revoke and delete return 204. Authentication failure is 401,
scope or replay conflict is 409, malformed input is 400, unsupported media is
415, an oversized body is 413, and an unexpected service failure is a generic
500. None of those responses or logs includes request bodies, authorization,
provider material, upstream diagnostics, filesystem paths, or exception text.
Liveness means only that the process can answer; readiness is true only after
configuration, keyring, and the complete encrypted store have validated.

The service reads its Bearer credential and session-capability secret from
private regular files and its keyring from a private directory containing a
`current` version file and base64-encoded `<version>.key` files. Paths must be
absolute, owned by the service uid, non-symlinks, and inaccessible to group or
other users. Unknown files, invalid version names, duplicate decoded keys,
missing current keys, and non-256-bit keys fail startup. Key and secret bytes
are never accepted through command-line arguments or ordinary environment
variables; environment values may name mount paths only.

The production container runs as a dedicated unprivileged uid with bounded
CPU, memory, process, and shared-memory resources; a read-only root filesystem;
all capabilities dropped; `no-new-privileges`; a temporary in-memory `/tmp`;
no Docker socket; no database credential or database network; an internal
control network; and a distinct outbound network used only through the audited
origin allowlist proxy. Its HTTP listener publishes only to host loopback for
an HTTPS reverse proxy. Only that container mounts the read-only service,
session, and key secrets and the writable named profile-material volume. The
API and worker receive the HTTPS endpoint and private-file client credential
needed to call it, but never mount the key directory or material volume. The
service image installs its pinned Chromium binary, has a dedicated entry point,
and does not start the public API, worker, or model runtime.

### Hosted session and lease contract

The hosted data plane is a second port and route family; it is not an expansion
of `BrowserProfileControlPlane`. Trusted composition supplies the profile id,
principal, provider reference, run id, attempt number, and deadline. None is a
model tool argument. Acquisition returns a random opaque lease reference to the
provider adapter, never to the model. The service permits at most one live
mutable lease for a profile, caps the deadline at fifteen minutes, and treats a
service restart as invalidating every outstanding lease.

Every navigate, observe, and act request authenticates the service caller and
revalidates the complete lease tuple, expiry, revocation fence, allowed origins,
and operation sequence before browser dispatch. Action also revalidates the
page revision. Closing a healthy lease seals the browser runtime's storage state
back into the encrypted profile before releasing exclusivity. A failed or
expired lease is closed without accepting client-supplied profile bytes. The
orchestration caller can never upload, download, or name a filesystem path for
profile material.

Revocation first persists the encrypted revocation fence and then synchronously
closes every live runtime for that profile. New operations fail from the fence
even if a stale process retains a lease reference. Deletion requires that fence,
closes any residual runtime, deletes encrypted material, and invalidates any
authentication ceremony. Lease references are stored only as keyed hashes in
service memory, compared in constant time, bounded to 128 characters, and never
logged.

The data-plane HTTP surface consists only of acquire, navigate, observe, act,
and close. It uses the same authenticate-before-buffering boundary, 64-KiB JSON
ceiling, generic error responses, and exact idempotency rules as lifecycle.
Acquire and close are idempotent for the same complete scope. Navigate and
observe are read-only. Act is sequence-bound and never retried after dispatch;
an ambiguous response is `tool.browser.outcome_unknown`.

### User-controlled authentication ceremony

Authentication has a public orchestration record and a direct isolated-service
channel. `POST /v1/browser-profiles/{profile_id}/authentication-ceremonies`
requires `browser.profile.write`, reads the profile through tenant/principal
scope, and asks the isolated service to begin a five-minute, single-use
ceremony. The response contains a public ceremony id, expiry, status, and a
direct launch URL. The URL's random fragment capability is returned once and is
never persisted, logged, included in events, or accepted from a model tool.
List, status, cancellation, and profile views never return it.

Admission permits at most one unexpired non-terminal ceremony per owned
profile. The application acquires a profile-scoped repository lock before the
active-record check and holds it through isolated-service launch and durable
record creation. PostgreSQL implements that lock with `SELECT ... FOR UPDATE`
inside the same unit of work; a concurrent begin waits, observes the winner's
record, and returns `409 conflict` without launching another ceremony. The lock
wait is capped at five seconds, while the isolated-service launch has a separate
thirty-second total application deadline; neither can hold the transaction
indefinitely.

The launch channel terminates at the isolated browser service, not the public
API or worker. A same-site, no-store browser surface binds its unguessable
capability to the profile, principal, expiry, and one browser runtime. Password,
passkey, MFA, CAPTCHA, and consent interaction occurs inside that browser
surface. The orchestration API sees neither keystrokes nor browser protocol
frames and has no generic proxy endpoint.

Only the isolated runtime determines completion. It may report `ready`,
`needs_user`, `authentication_required`, `expired`, or `cancelled`; a caller
cannot submit a credential or assert success. CAPTCHA, MFA, reauthentication,
consent, password fields, and one-time-code fields keep the ceremony in
`needs_user` until the user completes them directly. A ready result atomically
seals storage state, releases the authentication lease, and advances metadata
from `AUTHENTICATION_REQUIRED` or `NEEDS_USER` to `READY`. Failure and expiry
discard the runtime state and never overwrite the last sealed profile.

### Profile API contract

The public API adds profile create/list/get/revoke/delete and authentication
ceremony begin/status/cancel routes. They use only `BrowserProfileView` and
`BrowserAuthenticationView`; neither includes provider references, lease
references, launch capabilities after creation, key versions, storage state, or
provider diagnostics. Cross-principal access is 404. Creation validates one to
64 unique public-HTTPS origins. Revoke is generation guarded and takes effect
before returning. Delete is allowed only after revoke. Every mutation is
idempotent under the ordinary HTTP idempotency contract.

`POST /v1/sessions` also accepts an optional `browser_profile_id` from a trusted
authenticated client surface. Supplying it requires `browser.profile.read`; the
service re-reads the profile under the request principal and accepts only
`READY`. It persists only the opaque UUID as reserved session metadata. The
field never appears in a model tool schema or prompt, and a model-authored
message or metadata object cannot select a profile. Hosted composition without
a deployment-wide `BROWSER_PROFILE_ID` resolves this binding before checking
the selected profile's exact origin policy and acquiring its run-attempt lease.

## Policy, approvals, and standing grants

Navigation and observation satisfy `NETWORK_READ` only when their target is a
trusted `browser_provider`, their exact tool classification matches this
document, and the provider binding enforces the origin policy. Otherwise they
are denied.

`browser.act` requires an ordinary approval by default. Unattended operation
requires a durable `BrowserGrant` created through an explicit approval surface,
never through conversation text or page content. A grant pins:

- tenant, principal, browser profile, agent version, and policy version;
- allowed origins and action kinds;
- optional element-role and accessible-name constraints;
- schedule or run-purpose restriction;
- creation approval, start time, expiry, and revocation state;
- exclusions that always require a fresh approval.

`BrowserGrant` is durable metadata, not a prompt or tool argument. Its id,
profile id, tenant, principal, agent version, policy version, exact origins,
action kinds, optional element-role/name constraints, optional purpose, start,
expiry, revocation, approval actor, and timestamps live in PostgreSQL. It stores
no browser material. The public grant surface requires `browser.grant.read` or
`browser.grant.write`; creation itself is the explicit authenticated approval
surface and records the authenticated principal as approver. A model, tool,
page, scheduled prompt, or ordinary conversation endpoint cannot create,
broaden, select, or revoke a grant.

Trusted run composition may pin one grant id and one profile id. Immediately
after deterministic policy returns `REQUIRE_APPROVAL` for `browser.act`, and
before an approval request is created, the pipeline may ask the standing-grant
authorizer about that already-validated action. The authorizer returns only a
typed allow/deny result and audit reason. It re-reads both profile and grant in
one tenant-scoped unit of work and requires: profile `READY`; exact principal,
profile, agent version, policy version, origin, action kind, optional role/name,
purpose, and time match; no revocation; and an expiry after the action deadline.
It then re-runs deterministic policy. A policy denial, hard exclusion, stale
revision, changed profile generation, mismatch, missing pin, or repository
failure falls back to ordinary approval or denial and never widens authority.

Payments, purchases, account recovery, password or MFA changes, permission
changes, legal acceptance, publication, destructive actions, file transfer,
and security-setting changes are represented by a closed
`BrowserActionConsequence` vocabulary. Provider observations conservatively
classify candidate controls. Unknown consequence is hard-excluded. A standing
grant can authorize only `routine` interaction; every other consequence always
requires fresh approval or is denied. Page text cannot supply or downgrade the
classification.

Payments, purchases, account recovery, password or MFA changes, permission
changes, legal acceptance, publication, destructive actions, file transfer,
and security-setting changes cannot be covered by a standing grant in the
initial release. Policy revalidates the profile, grant, arguments, observation
revision, and origin immediately before every action. Revocation takes effect
at the next action, not the next scheduled run.

## Reliability and retries

Browser mutation uses the existing effect-sent boundary. Once an action may
have reached the site, transport loss produces `UNCERTAIN`; the runtime does
not replay it blindly. Recovery first observes the site and applies a
workflow-specific postcondition. Only a proved-unsent or proved-idempotent
action may retry automatically.

Every action includes the page revision and opaque element reference. A stale
revision returns `tool.browser.page_changed`. Redirect loops, closed pages,
profile contention, provider loss, and session expiry have stable failure
codes. Raw provider and site error text is never persisted as a diagnostic.

## Milestone 11 scheduling integration

A scheduled browser run pins a principal, agent version, policy profile, tool
set, browser profile reference, optional standing-grant reference, budget,
deadline, and audit record. Neither the schedule nor its prompt contains
credentials or cookie material. Scheduler retries obey the browser
effect-sent/uncertainty rules and may not turn a failed write into a duplicate.

If the provider is device-local, dispatch requires matching device presence.
If it is hosted, profile availability and grant validity are checked before
the first model call. A missing device, expired profile, or absent grant is a
visible run outcome and notification, never an implicit permission expansion.

## Bounds and stable failures

Readable text, element count, accessible names, URLs, titles, screenshots, and
provider responses have independent byte and count ceilings. The first slice
limits readable text to 256 KiB and interactive elements to 256. Provider
responses are validated before they enter a tool result.

The stable reason-code family includes:

- `tool.browser.url_disallowed`
- `tool.browser.provider_unavailable`
- `tool.browser.profile_unavailable`
- `tool.browser.authentication_required`
- `tool.browser.needs_user`
- `tool.browser.page_changed`
- `tool.browser.element_not_found`
- `tool.browser.action_not_allowed`
- `tool.browser.output_invalid`
- `tool.browser.outcome_unknown`

## Delivery plan

1. **Contracts and read-only seam:** domain values, port, fake provider,
   `browser.navigate`, `browser.observe`, exact registration and policy checks,
   default-off composition, and persisted external-untrusted results.
2. **Isolated ephemeral provider:** Playwright adapter without persistent
   authentication, strict origin confinement, bounded semantic observations,
   scrubbed process state, and crash cleanup.
3. **Interactive actions:** revision-bound `browser.act`, ordinary approvals,
   effect-sent uncertainty, stable element handles, and controlled browser
   coverage.
4. **Profiles and login:** encrypted per-principal profile storage, interactive
   authentication surface, revocation/deletion, MFA/CAPTCHA suspension, and
   complete secret-leak tests.
5. **Standing grants:** narrowly scoped durable grants, approval UI, policy
   revalidation, expiry/revocation, and hard exclusions.
6. **Scheduler and device integration:** pinned profile/grant dispatch,
   offline handling, notifications, and retry/postcondition behavior.
7. **Evaluation and rollout:** controlled sites first, then consenting external
   services; default-off tenant rollout with policy-failure and uncertain-write
   thresholds.

## Acceptance criteria

- Normal configuration registers no browser tool and starts no browser
  process. An explicitly bound provider registers only the capabilities it
  implements.
- Navigation and observation pass schema validation and deterministic policy,
  persist bounded `EXTERNAL_UNTRUSTED` observations, and cannot authorize
  arbitrary worker egress.
- The model and durable stores never receive a password, MFA value, cookie,
  bearer token, storage value, raw DOM, or browser-profile bytes.
- Every browser profile and grant is tenant/principal scoped, revocable,
  deletable, encrypted at rest, and inaccessible through model-authored ids.
- Every redirect and action remains within the provider-bound origin policy;
  private-network, non-HTTPS, popup, download, upload, clipboard, and device
  access fail closed unless a later explicit contract authorizes them.
- Mutating actions require an approval or a matching unexpired standing grant;
  the initial hard exclusions always require fresh approval or remain denied.
- A stale page reference is rejected before action dispatch. A possibly sent
  non-idempotent action is never blindly retried and records an uncertain
  outcome until a postcondition resolves it.
- Authentication expiry, MFA, CAPTCHA, device absence, provider crash, invalid
  output, and revocation return stable outcomes without upstream text or secret
  leakage.
- Scheduled browser runs pin their profile and grant, revalidate before every
  action, and cannot broaden authority through prompts or page content.
- Contract, policy, isolation, persistence, integration, and adversarial
  prompt-injection suites pass before the feature is enabled for any tenant.

## Hard gates

1. **Provider contract.** The shared provider contract covers navigation,
   observation, action, and cleanup without changing the model-visible schema
   between ephemeral, hosted, and device-local placements. **M10.**
2. **Default-off composition.** With no explicitly bound provider, no browser
   tool is registered or advertised and no browser process starts. **M10.**
3. **Origin isolation.** Every initial URL, redirect, request, final URL, and
   action remains inside the exact public-HTTPS origin policy, with an audited
   deny-first egress layer beneath browser interception. **M10.**
4. **Observation trust.** Navigation and observation pass validation and
   policy, persist bounded `EXTERNAL_UNTRUSTED` results, and expose no raw DOM,
   hidden value, header, cookie, storage item, or provider diagnostic. **M10.**
5. **Action authorization.** Every `browser.act` is a serial high-risk,
   non-idempotent external write and reaches dispatch only after a valid
   approval or a future exact standing grant. **M10.**
6. **Revision binding.** An action can target only the stable element handle
   from its exact observation revision; stale or mismatched references fail
   before browser dispatch. **M10.**
7. **Uncertain writes.** An action that may have reached a website records the
   effect watermark and an uncertain outcome and is never replayed blindly.
   **M10.**
8. **Profile lifecycle.** Profile metadata and provider material are
   tenant/principal scoped, encrypted, revocable, deletable, lease-invalidating,
   and inaccessible through model-authored identifiers. **M10.**
9. **Authentication boundary.** Login is a user-controlled platform ceremony;
   passwords, passkeys, MFA values, cookies, tokens, and profile bytes never
   enter model-visible or durable orchestration data, and CAPTCHA, MFA,
   reauthentication, and consent produce `needs_user`. **M10.**
10. **Standing grants.** A standing grant is exact, expiring, revocable,
    policy-revalidated before every action, and unable to cover any initial
    hard exclusion. **M10.**

These ten registry-backed gates are the browser tranche's blocking delivery
contract. All ten resolve to executable Milestone 10 checks. Milestone 11
scheduling consumes the resulting profile and grant references but owns its own
future gates and is not part of this Milestone 10 gate area.
