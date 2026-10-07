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

ADR-0148 adds individually approved rich-text typing and the separately
classified image-upload contract below. Other file transfers remain unavailable.

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
profile material. During a device sign-in the user's trusted client briefly
holds one site's session and hands it only to the isolated provider
(ADR-0128). Sandboxed model-generated code receives neither. The model sees
bounded observations and opaque element references only.

## Capability model

`BrowserProvider` is bound by trusted composition to exactly one principal,
one opaque profile reference, and one domain policy for an execution. A hosted
deployment may pin that binding globally or resolve it from the session created
by an authenticated principal; the resolved per-session adapter still owns
exactly one profile and origin policy. It exposes:

```text
navigate(BrowserNavigateRequest) -> BrowserObservation
observe() -> BrowserObservation
expand(BrowserObservationExpansion) -> BrowserObservation  # optional capability, ADR-0157
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
- optional candidate-window coverage and an opaque continuation (ADR-0157);
- an optional screenshot artifact reference added by a later slice.

It excludes HTML, scripts, styles, hidden inputs, password values, request and
response headers, cookies, storage, browser logs, and provider diagnostics.
Element references are valid only for the observation revision that produced
them. An action supplies `expected_revision`; a mismatch fails before the
provider dispatches the action. `navigate` and `act` return the observation
taken once the page has settled (see "Bounds and stable failures"), so its
revision and references are current and the model can act on them without
calling `observe` first.

`browser.observe@1.1.0` can recover controls and text omitted from another
browser tool's head/tail excerpt (ADR-0151). Its optional `element_offset`
(0–256) and `text_offset` (0–262,144 Unicode characters) default to zero. It
returns a fresh observation in complete JSON that fits the configured serialized
inline output budget, including escaping, instead of an unreadable partial
object. Each page carries its current revision, complete element references
and states, `total_elements`, `total_text_characters`, the applied offsets,
and nullable `next_element_offset` and `next_text_offset`. Set the element
offset to `total_elements` to read text alone. Offsets beyond a shorter live
page return an empty terminal slice. Each read refreshes the page; controls
may move between reads, and only the newest revision and references may be
used for an action.

The displayed title is limited to 128 UTF-8 bytes and each element name to
256, with an ellipsis and explicit `title_truncated` or `name_truncated` flag.
Provider observations and full approval facts stay unchanged. Metadata or a
single complete reference that cannot fit produces `tool.browser.output_invalid`.
Pagination is read-only, stays under the same profile and origin authority,
and changes neither the generic output admission cap nor artifact access.
Version 1.0.0 remains registered for pinned chats; a new profile-bound chat
selects the new schema. Navigate, act and upload keep their existing versions.

## Tool contract

The stable builtin namespace is `browser`:

| Tool | Operation | Side effect | Risk | Idempotency |
| --- | --- | --- | --- | --- |
| `browser.navigate` | Open one allowed public HTTPS URL | `NETWORK_READ` | `LOW` | `READ_ONLY` |
| `browser.observe` | Refresh the page or expand its current observation | `NETWORK_READ` | `LOW` | `READ_ONLY` |
| `browser.act` | Click, type, select, check, press, or scroll | `EXTERNAL_WRITE` | `HIGH` | `NON_IDEMPOTENT` |
| `browser.upload` | Select one conversation image in a file control | `EXTERNAL_WRITE` | `HIGH` | `NON_IDEMPOTENT` |

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
domain policy validates the initial URL, every top-level redirect, the final
URL, and popups. Page resources and embedded frames may load from public HTTPS
services under the separate resource transport boundary in ADR-0098; their
origins never become profile navigation or grant authority, and a task grant's
scope comes only from owner configuration (ADR-0129).

When hosted composition resolves profiles per session, the frozen context plan
advertises its browser tools only if that trusted reserved metadata contains
a selected profile, and then always defines them: they rank ahead of every other
candidate and are never deferred (ADR-0130). Runs in such a session use the
browser-task limits in [runtime-loop.md](runtime-loop.md). An ordinary session
therefore keeps public web capabilities
without paying for or guessing unusable browser operations. A deployment-wide
hosted profile, an explicitly injected provider, and the origin-pinned Playwright
adapter remain globally bound runtime environments and keep their browser tools.

Downloads, uploads other than the image contract below, clipboard access,
notifications, geolocation, camera, microphone, password-manager access, and
new windows are denied until each has
its own classified contract. Cross-origin popups are closed and reported.
The ephemeral provider also refuses typing into password fields or controls
whose autocomplete semantics identify a current password, new password, or
one-time code. Opaque references bind stable element handles rather than
selectors that could retarget after DOM reordering. Observation obtains at most
4,097 candidate handles using an isolated-world selector, including one lookahead,
then checks visibility of at most 4,096 candidate nodes in one browser call, by the same
rule as Playwright's `is_visible` (a `display: contents` control counts when a
child is visible), and keeps the first 256 visible ones in traversal order, so
hidden nodes never take an element slot; it releases every other handle and
those of the observation it replaces.
It then reads bounded metadata from each kept handle in one browser call,
avoiding repeated selector resolution and excessive round trips while a login
form is changing. At most eight elements are inspected concurrently; results
preserve snapshot order and retain the existing visibility, state, and revision
checks. A control the page removes between its visibility check and its state
read, as a re-rendering timeline does, takes no slot, like a hidden one; any
other failed read still fails the observation. When a plan offers `browser.navigate`, each request's runtime metadata
names the origins it accepts (never the profile id), so the model does not
guess a bare domain.

### Individually approved rich-text input and image upload

Under individual approval, `browser.act` typing accepts native inputs and text
areas and live contenteditable regions, including inherited editability and
plain-text editing hosts. It uses the browser's normal fill operation and input
events; it does not click a publication control. The credential restrictions
above still apply. Standing and task grants retain their native-field-only
typing restriction (ADR-0129).

`browser.upload@1.0.0` takes exactly `image_id`, `expected_revision` and `ref`.
The ID names an image attached to, generated in, or exported within this chat.
The reference names either a visible native file input or a visible control
that opens a native file chooser. One individually approved call selects one
PNG, JPEG or WebP, at most 5 MiB, and returns the updated observation. A site can
send the image immediately on selection; approval authorizes that transfer.
Publishing the post remains a separate `browser.act` with its ordinary approval.

Upload is a serial, high-risk, non-idempotent external write requiring
`artifact.read`; neither kind of browser grant covers it. Its approval view
names the image UUID and the observed page and control. After approval, the
existing conversation image resolver checks principal, session, origin, expiry,
size, signature and checksum. Unclaimed uploads and foreign, expired, knowledge
or tool-output artifacts are unavailable. Arguments contain no URL, path, bytes,
selector or script. Playwright receives an in-memory payload with a generated
filename; only a file input in the current main document can receive it.

The hosted adapter sends the image through `/v1/browser-sessions:upload`, an
authenticated and sequence-bound service route with a 7 MiB JSON limit. It
revalidates decoded bytes inside the isolated service. Nginx streams this exact
route without request buffering; other control-plane routes retain 64 KiB.
Bytes stay outside tool arguments, events, logs and results. A possibly sent
upload or chooser click is uncertain on failure and is never blindly retried.

Providers advertise upload only when they implement the optional image-upload
port. Bound hosted chats keep it defined with the other browser tools. Existing
frozen chats retain their roster; start a new profile-bound chat to use upload.
Downloads, arbitrary files and videos remain unavailable. ADR-0148 records this
owner-requested extension and its verification requirements.

## Profiles and authentication

A durable `BrowserProfile` belongs to one tenant and principal and contains an
opaque provider reference, allowed origins, lifecycle status, creation and
last-use metadata, and encryption-key version. It contains no cookie or token
bytes. Provider profile material is encrypted at rest outside PostgreSQL and
is addressable only through the opaque reference.

Authentication is a user-controlled ceremony, not a model tool:

1. The user creates or selects a dedicated browser profile.
2. An interactive surface opens that profile directly to the identity
   provider or website: the isolated service's remote browser, or a
   non-automated web view in the user's own client (ADR-0128).
3. The user enters passwords, passkeys, and MFA codes without model access.
4. The provider seals the resulting profile and reports only success, expiry,
   and allowed origins. After a device sign-in the client hands over only the
   profile's site session, and the provider verifies it before sealing.
5. Expiry, CAPTCHA, reauthentication, or consent pages return `needs_user` and
   suspend rather than invite the model to handle a secret.

Profiles can be revoked and deleted. Deletion removes provider material and
invalidates every lease and standing grant that refers to the profile.
Revocation, deletion, and every sign-in, which advances the profile generation
(ADR-0128), also end every task grant that refers to the profile.

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
cannot remain valid accidentally. Every sign-in attempt also increments it
without changing status (ADR-0128): beginning a ceremony in either mode does so
in the unit of work that records the ceremony, and a `ready` outcome recorded
for a profile that is already `READY` does so again. Grants pin the
generation, so none survives a sign-in, including one that replaces a ready
profile's session with another account's. No standing or task grant is
created while the profile's newest ceremony has no recorded outcome, whether
or not it has expired: such a grant would pin the generation the begin set and
keep authorizing on a session the service seals if no client ever reads the
outcome. Recording that outcome through status or cancel, or recording the
outcome of a later sign-in, lifts the refusal.

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
profile material and live browser state remain wholly inside the isolated
service, and so does login interaction in the remote ceremony. In the device
ceremony the user's trusted client holds one site's session briefly and sends
it only to the isolated service (ADR-0128). Orchestration receives only
secret-free metadata, opaque service capabilities, and bounded observations.

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
considered. Create uses insert-on-conflict detection; bind, transition,
generation advance, and delete each put the expected generation and owning
principal in the write predicate and distinguish a missing row from a stale or
invalid state without ever returning another principal's metadata.

## Sessions, isolation, and provider placement

One browser lease belongs to one run attempt and one profile. Concurrent use of
the same mutable profile is rejected unless the provider offers copy-on-write
isolation with a single serialized commit. The lease has a deadline and is
closed on terminal run, cancellation, policy invalidation, or worker loss. It
spans the run's parking on its own approval and its wait to resume; any other
parking, such as for the user's answer or a delegated child, closes it.

Two placements implement the same port:

- A device provider operates a dedicated profile on a connected user device
  through the reserved `device.*` routing seam. Device absence returns
  `tool.device_offline`.
- A hosted provider operates an isolated browser process with encrypted
  profile storage. It is preferred for reliable unattended schedules, but it
  requires the profile and login surfaces before rollout.

The ephemeral adapter launches a non-persistent headless Chromium child process
with a temporary home, scrubbed environment, downloads and service workers
disabled, popup closure, request interception, and the audited browser egress
proxy. Main-frame request interception enforces the exact navigation origins
before every document request and redirect. Resources and embedded frames load
automatically over public HTTPS. The dedicated browser proxy accepts only
CONNECT to valid public DNS hostnames on port 443, checks all resolved addresses,
and dials a checked address. It blocks plaintext HTTP, IP literals, loopback,
link-local, private, metadata, and single-label destinations. The sandbox and
ordinary worker proxy retain their exact allowlists. This adapter holds no
durable authentication state and is not the hosted-profile topology.

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
arbitrary-browser-command endpoint. Session material enters from outside only
through the device ceremony's single-use handoff on the direct surface channel,
which orchestration never carries.

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
mutable lease for a profile, caps each requested deadline at fifteen minutes
and a lease's life at sixty, and treats a service restart as invalidating every
outstanding lease. The requested deadline is the run attempt's, never one tool
call's: the run deadline when the run has one, otherwise the cap. An acquire
that repeats a live lease's profile, principal, provider reference, run, and
attempt returns that lease and its action sequence, whatever deadline it asks
for, so a run resumed in another process continues its own page. A different
run cannot take a live lease whose run still needs it and is refused as
`tool.browser.profile_unavailable`. Later calls of the attempt, including one
resumed after its approval, reuse the lease while it outlives the call; the
provider drops a lease the service no longer honours or does not answer for,
and an expired lease is replaced.

Every hosted browser is headed and shares one container, so the service
admits at most three at once (ADR-0145): each lease and each remote ceremony
counts one, and a device verification counts two from the moment its handoff
is accepted. A new lease or remote ceremony beyond that is refused before its
browser starts, as retryable `tool.browser.provider_unavailable`; re-attaching
to a live lease needs no room. A handoff that finds no room for its two
browsers still spends its capability and ends as a verification that could not
run.

A lease renews in steps of at most fifteen minutes, up to sixty minutes after
acquisition and never past the run's deadline, only while its run is running,
queued to resume, or parked on its own approval (ADR-0127). The run worker that
holds the lease runs this upkeep about once a minute. It renews a lease within
five minutes of expiry and closes one whose run has ended, including a run
cancelled while parked. Renewing an expired or revoked lease fails.

Every navigate, observe, and act request authenticates the service caller and
revalidates the complete lease tuple, expiry, revocation fence, allowed origins,
and operation sequence before browser dispatch. Action also revalidates the
page revision. Closing a healthy lease seals the browser runtime's storage state
back into the encrypted profile before releasing exclusivity. A failed or
expired lease is closed without sealing its state or accepting client-supplied
profile bytes, however late the close arrives. The orchestration caller can
never upload, download, or name a filesystem path for profile material.

Revocation first persists the encrypted revocation fence and then synchronously
closes every live runtime for that profile. New operations fail from the fence
even if a stale process retains a lease reference. Deletion requires that fence,
closes any residual runtime, deletes encrypted material, and invalidates any
authentication ceremony. Lease references are stored only as keyed hashes in
service memory, compared in constant time, bounded to 128 characters, and never
logged.

The data-plane HTTP surface consists only of acquire, renew, navigate,
observe, act, upload, and close. It uses the same authenticate-before-buffering
boundary, 64-KiB JSON ceiling (7 MiB only for image upload), generic error
responses, and exact idempotency
rules as lifecycle. Acquire, renew, and close are idempotent for the same
complete request. Navigate and observe are read-only. Act is sequence-bound and
never retried after dispatch; an ambiguous response is
`tool.browser.outcome_unknown`, and the provider then retires the lease.

Navigate, observe, and act responses may also carry secret-free element facts
as an optional sibling of the observation, which a caller that does not know
them ignores: a closed field kind, which follows what the runtime acts on, so
a text area, a region the page made editable, a select, a native check box or
radio, and a choice control the page built from another element are each a
kind of their own; the element's label sources (accessible-name
attributes, associated labels, alternative text, button values, and visible
text, each capped at 256 characters), leaving out only a source the classifier
reads exactly as the element's name, so one that merely normalizes to it, such
as "Continue $" beside "Continue", is carried, where the associated labels include
those of any check box or radio a click on it would change, through a label
or inside it; whether any label source was cut, or was longer than the 1,024
characters the runtime reads; whether a link or form target is
same-origin, its first path segment, and whether any of its path segments is
sensitive; a download flag; and the enclosing dialog's accessible name, capped
at 128 characters. A form target is where the browser would submit: the
formaction of the submit control a click activates or, for Enter in a field,
of the form's default button, else the form's action. An element's form is its
form owner, including the one a form-associated custom element names with its
`form` attribute. A fragment or empty link
has no target only when it stays on the page, since a `<base>` element can
send it elsewhere; a `javascript:` link, and any target that is not HTTPS, is
outside every origin. An SVG link's targets are both its written and its
animated value. Facts follow the flat tree the browser renders, through
open shadow roots and slots: visible text includes what a shadow root renders,
an `aria-labelledby` reference resolves in the element's own tree, and a form's
default button is found in the form's own tree. A click lands on whatever lies
at the element's centre, so the targets are those of the link or submit control
around the element, its label's control, and every link and submit control
inside it; the facts carry one target that is inside a prefix only when every
one of them is. An element with an embedded document or an image map inside
it, or an SVG `<use>` whose copy could hold a link or embedded content or
comes from another document, or one too large to walk, has a target outside
every origin. Raw target URLs never leave the
runtime. Facts feed the
action classifier and the approval view and never enter a model-visible
result. An act request may carry a dispatch constraint naming the grant kind,
origins, an optional path prefix, an expiry, a consequence ceiling, and a text
cap. The runtime uses it only to refuse: before dispatch it checks the expiry,
the live page URL, and the live element's labels, consequence, and facts, and
it refuses an element with a label source longer than the 1,024 characters it
reads. For a selection it reads every option the selection could choose, by
value or by label with white space collapsed and wherever it lies in the list,
with that option's label, value, text, accessible name, title, and group
label, and it refuses a select with more than 4,096 options or more than 64
that match.
The same read arms a click guard: until the action is sent, it stops, with its
default action, any trusted click aimed at an element the read did not cover,
which are the element, its ancestors and descendants, the controls of labels
among them, and its form's default button; if it had to, the act's outcome is
`tool.browser.outcome_unknown`. Only the act's own input makes trusted clicks,
so a control that lies over the click point inside the button or link around
the element, where the click is aimed, or a label whose control the page swaps
after the read, never takes the click.
A key press or typed text goes to whatever holds focus, so under a constraint
the runtime focuses the element and refuses unless the element itself then
holds focus, through open shadow roots, and never an embedded document. Until
the key or text is sent, it stops, with its default action, any key or text
event aimed at another element; if it had to, the act's outcome is
`tool.browser.outcome_unknown`. While a task-grant act runs and settles, the
runtime also refuses every document request, in any frame, and every
hyperlink-auditing ping whose URL is not on the grant's origin inside its
prefix with no sensitive segment, whatever the page hid from the facts, such
as a closed shadow root. When it refuses the page's own document, the page is
left on the browser's error page and the act's outcome is
`tool.browser.outcome_unknown`. A new window never loads, including its first
navigation, which it issues before its frame exists. The ephemeral adapter's
headless browser denies web-created windows inside Chromium before creation,
so a popup cannot submit while an asynchronous close is pending. Full headed
Chromium has no such switch. In every hosted browser, which is headed
(ADR-0145), document responses gain an additional CSP sandbox policy
that permits scripts, forms, same-origin access and navigation, but never
popups. The runtime fetches each headed document through the browser context's
request transport and the same audited proxy, with automatic retries and
redirect following disabled and Chromium's cookie selection preserved, then
fulfills the browser request with that response (ADR-0138). Chromium handles
each redirect through the existing origin guard;
a failed form exchange is never replayed. Existing headers, including cookies
and the site's own CSP, remain intact; body bytes are forwarded unchanged.
This transport uses Playwright's HTTP client, so a site that requires the
browser's TLS fingerprint may reject a hosted browser: the remote ceremony, a
run-attempt lease, or a device sign-in's verification. The owner's own web
view in a device sign-in is unaffected. The policy also prevents legacy
`document.domain` relaxation. Unexpected pages retain a closure guard. Popup closure uses the
Chromium target protocol so context interception remains active until the
target is destroyed; marking a Playwright page as closing first can bypass
interception of a form submitted from its initial blank document. The guard
waits for the close event before detaching its protocol session. If closure
fails or is cancelled while the popup is still open, the runtime ends the
whole browser session. Page script is outside
these checks, an accepted limit of ADR-0129: it can change the page's own
controls, as `element.click()` from the element's own handler does on a
check box, and send its own requests on the allowed origins, and a document it
requests after the act settles, such as a refresh or timer the act scheduled
for later, is not fenced.
`tool.browser.grant_not_applicable` is a refusal given before dispatch; the
lease and its action sequence are unchanged, and the runtime forgets the
observation's element handles, so the next action needs a new observation
(ADR-0129).

### User-controlled authentication ceremony

Authentication has a public orchestration record and a direct isolated-service
channel. `POST /v1/browser-profiles/{profile_id}/authentication-ceremonies`
requires `browser.profile.write`, reads the profile through tenant/principal
scope, and asks the isolated service to begin a five-minute, single-use
ceremony in `remote` mode, the default, or `device` mode (see the device
sign-in ceremony below). The response contains a public ceremony id, expiry,
status, and a direct launch URL. The URL's fragment capability is 256 random
bits in either mode, is returned once, and is never persisted, logged, included
in events, or accepted from a model tool. List, status, cancellation, and
profile views never return it.

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

The remote ceremony's browser is headed (ADR-0106). Websites attach an
abuse-classification signal to the login request and refuse a browser that
reports itself headless, answering even correct credentials with a generic
credential error. The isolated runtime therefore launches full Chromium for an
interactive ceremony. On Linux it first starts a private virtual display for
that one ceremony, passes only that display to the browser's scrubbed
environment, and destroys it when the runtime closes; elsewhere it uses the
native display. A display that cannot start fails the launch as
`tool.browser.provider_unavailable`; the runtime never falls back to headless.
The browser reports its real user agent and automation state: the runtime
overrides no user agent and masks no automation indicator, so a website that
still refuses the browser is never evaded. A website can refuse a headless
browser on any page, so run-attempt leases and a device sign-in's two
verification browsers are headed in the same way, each on a private display
of its own (ADR-0145). The direct surface relays each key as the user presses
it,
and the runtime delivers text as one key press per character with no added
timing; a field that fills with no keyboard events is scored as automated and
refused.

Full Chromium asks its vendor's services for things no page requested, which
the headless shell does not. A hosted browser's launch turns off each such
request that has a switch (ADR-0146): the query to the autofill service that
describes the forms on a page, the network-time query, the search engine's
AI Mode eligibility request and idle connection, and the push-messaging
check-in with the registrations and long-lived connection that follow it.
Chromium honours only its last feature-disabling switch and Playwright passes
one of its own, so the runtime's switch repeats Playwright's list, and a test
compares the two. The service image carries a managed policy, in the
directory each Chromium flavour reads, that stops the spelling-dictionary
download, which has no switch. One request remains: the browser lists Google
accounts at `accounts.google.com` once as it starts, from its own empty
profile, with no cookie. It has no switch, and the proxy does not refuse a
host that websites load for sign-in. None of these settings changes what a
website can observe of the browser. Each depends on the Chromium build that a
Playwright version installs, so hosted CI measures the built image's own
requests on both architectures whenever the image, its limits, the Playwright
version or the switches change (ADR-0152).

For a remote ceremony the trusted client presents the returned launch URL behind
a user-initiated continue action and treats a rejected platform handoff as a
failed setup. It cancels the ceremony and revokes and deletes the unused profile
so retry does not collide with abandoned state. It also surrenders a live
ceremony when the connection that began it changes: before it replaces or
deletes that bearer credential it cancels the ceremony through the transport and
credential that began it and drops the retained launch URL, so a capability
bound to the former principal cannot outlive it in a client the user has
repointed. That cancellation is best effort, since the former credential may
already be rejected, and it never blocks the connection change; the client
forgets the ceremony either way. The direct surface explains the screenshot and
focused-field interaction, disables its credential controls until the runtime
connects, and tells the user to return to the client and start over when the
non-persisted fragment capability is missing or expired. After the user clicks
a website field in the screenshot, a capture field on the surface takes focus:
each character, paste, or completed composition is relayed as a text event and
each navigation key as a key event, in order, as it happens, with no separate
send step. The capture field is cleared as each event is taken, and typed text
is never placed in diagnostics or durable client state. When an event fails,
the surface drops the rest of the queue and tells the user to clear the field
and type again, so a password is never submitted with a character missing.

Only the isolated service determines completion. It may report `ready`,
`needs_user`, `authentication_required`, `expired`, or `cancelled`; no caller
can assert success. In the remote ceremony a caller cannot submit a
credential. In the device ceremony the user's client submits the site session
once, and the service's own verification decides the outcome. CAPTCHA, MFA,
reauthentication, consent, password fields, and one-time-code fields keep a
remote ceremony in `needs_user` until the user completes them directly.

In the remote ceremony the runtime reports `ready` only on evidence of a sign-in
it mediated itself: during this ceremony the user sent text while one of those
challenges was visible, no challenge is visible now, the page is on an allowed
origin, and the context holds storage state. Storage state is not that evidence.
A signed-out page sets analytics and consent cookies with no user action, and
they keep arriving after the launch navigation, so neither their presence nor a
change since launch distinguishes a sign-in; clicks alone, such as dismissing a
consent banner, do not either. Until the evidence exists a page that shows no
challenge stays `authentication_required`, including a page that saved state
already signs in, where the user signs in again or cancels. The runtime keeps
one boolean for this rule and never inspects, compares, or records the text the
user sent or any cookie value.

That evidence is necessary, not sufficient. The runtime is site-independent: a
profile names allowed origins and nothing about a site's pages, so no marker
tells it that a page is signed in. A sign-in the site rejects normally shows its
form again and stays `needs_user`. One that replaces the form with a page
showing no challenge, or a status check made between submission and the site's
answer, can still seal a profile that holds no session. Such a profile grants
nothing: the model cannot type into a password or one-time-code field, so a run
meets the signed-out page and the user repeats the ceremony. A signed-in marker
declared per site would close the gap; the profile contract carries none, and
cookie names or flags are not a substitute, since sites keep sessions in
script-readable cookies and in origin storage as well.

A ready result atomically seals storage state, releases the authentication
lease, and advances metadata from `AUTHENTICATION_REQUIRED` or `NEEDS_USER` to
`READY`. Failure and expiry discard the runtime state and never overwrite the
last sealed profile.

A sign-in the user finishes without returning to the client is not lost. While
a remote ceremony is open, the client refreshes its status whenever the app
becomes active and whenever Website Access appears. On opening Website Access
it also refreshes the newest open ceremony of each profile that is not ready.
The service sweeps every fifteen seconds and closes expired runtimes without
waiting for a request. It checks each open remote ceremony once in its last
twenty seconds and seals it only when two checks 2.5 seconds apart both find
the evidence above, so a check made while a submission is in flight does not
seal. The service keeps each terminal outcome for twenty-four hours, in memory,
so the public record can still learn it.

### Device sign-in ceremony

The device ceremony (ADR-0128) serves websites that refuse an
automation-flagged sign-in from the service's datacenter address but accept a
session created elsewhere. The user signs in on their own device, and the
isolated service receives only that site's session, verifies it from its own
address, and seals it.

Begin takes `mode: "device"` on the same route, with the same scope, admission
lock, one-open-ceremony rule, five-minute expiry, and login-URL validation as
the remote ceremony. The isolated service starts no browser. Its launch URL is
`/authentication/{ceremony_id}/handoff` on the ceremony origin, with the
capability in the fragment. That capability authorizes only the handoff: it
opens no frame and sends no event, and a remote capability cannot hand off.
The trusted client begins a device ceremony only when the user confirms they
are signed in, so the capability lives for seconds, and sends the root of the
confirmed page's origin as the login URL. Beginning either kind of ceremony
advances the profile generation (see the profile control-plane contract). The
begin response is `Cache-Control: private, no-store`. The isolated service can
refuse device begins by configuration, with
`tool.browser.provider_unavailable`, without affecting remote ones.

The client signs the user in inside its own app. It uses a web view with a
non-persistent data store and its platform's default user agent, with no user
script, script message handler, or automation setting, so it reads nothing the
user types. Top-level navigation stays on the profile's allowed origins. New
windows, downloads, and other schemes are refused; subframes load as the site's
resources do. Before a new profile exists, the first load may adopt a redirect
between a bare hostname and its `www` subdomain as the profile's origin. When
the user confirms, the client reads the store's cookies and keeps those whose
domain is an allowed host or a parent of one. It reads `localStorage` of the
allowed origins it visited in an isolated script world, and sends them once with
the confirmed page's URL. It then clears the store. It never persists or logs a
cookie, storage value, or capability, never places one in durable or published
client state, and never sends the handoff with its Veetbot API credential or to
any address but the launch URL. A begin whose answer is lost, or that meets an
open ceremony, is recovered once by cancelling the newest open ceremony and
beginning again.

`POST /authentication/{ceremony_id}/handoff` is a direct surface route. The
boundary authenticates `X-Browser-Ceremony-Capability` before buffering, then
requires `application/json` and at most 1 MiB. The reverse proxy allows that
size on this path only and streams the body to the service unbuffered, so it is
never written to the proxy's disk. Every surface path carries the ceremony id
in the lowercase hyphenated form the service issues, and nothing follows the
operation. The proxy's handoff location is anchored at the absolute end of the
path, and the boundary answers any other path under `/authentication/`,
including one with a decoded trailing newline, with `404` before reading its
body. The body has exactly `confirmed_url`,
`cookies`, and `origins`, in Playwright's storage-state field names.
`confirmed_url` is a public HTTPS URL on an allowed origin. Each of at most 300
cookies has exactly `name`, `value`, `domain`, `path`, `expires`, `httpOnly`,
`secure`, and `sameSite`, with these constraints:

- a name of 1 to 256 printable characters other than separators;
- a value without control characters or `;`, with name and value together at
  most 4096 bytes;
- a lowercase ASCII domain with at most one leading dot;
- a path starting with `/`;
- a numeric `expires` of `-1` or Unix seconds, fractions allowed;
- `sameSite` of `Strict`, `Lax`, or `None`, with `None` only when secure;
- `__Host-` and `__Secure-` prefixes carrying their attributes.

Each of at most 64 origins has a normalized `origin` and at most 2000
`localStorage` items whose names are at most 1024 characters. Duplicate cookie
`(name, domain, path)` triples, origins, or item names are invalid. A malformed
body is `400 invalid_request` and leaves the capability usable.

The service filters without trusting the client's filter:

- It keeps a host-only cookie only when its domain equals an allowed origin's
  host.
- It keeps a domain cookie only when its domain, without the dot, equals an
  allowed host or is a parent of one, and is not a public suffix under the
  Public Suffix List, private entries included.
- It drops a cookie whose domain is an IP address or ends in an all-digit
  label.
- It drops expired cookies and cuts expiries beyond 400 days.
- It keeps storage only for an exactly allowed origin.

The first well-formed handoff consumes the capability, whatever follows; a
repeated or concurrent handoff is `401`. The service verifies without holding
its service-wide lock. It starts two headed run-attempt runtimes (ADR-0145)
through the audited egress proxy, one from the filtered state and one from
none, and loads
the confirmed page in both until `DOMContentLoaded`, including deferred
scripts, then waits up to five seconds for the network to be briefly idle,
within thirty seconds in total. A pending image, including one inside an
embedded page, does not hold verification until the full-page `load` event.
The five-second network wait expiring does not make a pending authentication
request safe to ignore: documents, scripts, stylesheets, XHR and fetch requests
must finish and remain idle for 500 ms before the path and challenge evidence
below can be positive. A fetch or XHR event stream with HTTP 200 and media
type `text/event-stream` instead counts as established when its response
headers arrive (ADR-0147); its continuing body does not hold the wait open.
The media type is case-insensitive and may have parameters. An unanswered
stream, another status or media type, and every ordinary application request
still wait for completion. A visible sign-in challenge, disallowed origin, or path
outside the confirmed page is already negative evidence; background work on
that signed-out page does not hold verification open. A possible signed-in
page whose application requests never settle fails verification at the
overall deadline. Request listeners are removed on every
exit, including cancellation; no request URL or content becomes a diagnostic.

The result is `ready` only when the site itself tells the two apart. With the
session, the page stays on an allowed origin at the confirmed path or below it
and shows no sign-in challenge. Without it, the page shows a challenge, moves
to another path, or leaves the allowed origins. That also shows the site
accepts the session from the service's address. On `ready`, and only while the
ceremony is live and the profile unrevoked, the service seals the verifying
runtime's own storage state as a healthy lease close would, replacing the
previous material.

Every other outcome writes nothing, keeps the last sealed profile, ends the
ceremony `cancelled`, and returns a fixed code:

| Status | Code | Meaning |
| --- | --- | --- |
| `422` | `session_empty` | nothing in scope remained |
| `422` | `session_signed_out` | with the session the page showed a challenge, moved, or left the allowed origins |
| `422` | `session_unconfirmed` | the page behaves the same without the session |
| `409` | `tool.browser.provider_unavailable` | verification could not run or exceeded its time |
| `409` | `tool.browser.profile_unavailable` | the profile was revoked or deleted meanwhile |

Both runtimes close in every case. A ready handoff returns `200` with
`{"status": "ready"}`, and the client then reads the ceremony status through
the public API, which moves the profile to `READY`. Nothing from the body, and
not the capability, is logged, placed in an error, event, metric, or
diagnostic, or kept after the handoff returns. The route and its boundary answer
every failure with a fixed response, and the service's log configuration
replaces the text, traceback, and causes of any logged exception, including
the web server's own records, with a fixed message and the exception's class.

The handoff path writes one diagnostic of its own (ADR-0128, amendment of
2026-10-06). A verification that started its two loads and ends
`409 tool.browser.provider_unavailable` writes a single warning record to the
service log, so an operator can tell where it stopped. The message is always
`device verification did not finish`, followed by only these values:

- The load that stopped it, `with_session` or `without_session`, with the
  stage that load was in. A load that failed is named alone. Otherwise every
  load the deadline interrupted is named. When both pages had loaded,
  `with_session` is named at `capture`.
- The seconds since the loads were started, to one decimal place.

The stages are, in order:

| Stage | What the load is doing |
| --- | --- |
| `start` | starting the egress proxy and the browser |
| `navigate` | loading the confirmed page until `DOMContentLoaded` |
| `idle` | waiting up to five seconds for the network to be briefly idle |
| `inspect` | reading where the page landed and whether it shows a sign-in challenge |
| `settle` | waiting for the application's requests to finish and stay idle |
| `reinspect` | looking for a sign-in challenge again after that wait |
| `capture` | reading the verifying browser's storage state |

The record holds nothing else: no ceremony, profile, tenant, or principal
identifier; no URL, origin, path, or page content; no cookie, storage value, or
capability; and no exception class or text. The runtime reports each stage as
it begins, because the deadline reaches a load as a cancellation, which cannot
say where it landed. The service keeps only a value from the table; any other
value fails that load and is not recorded. A verification that ends `ready` or
with a `422` code writes no record, and neither does a handoff refused before
its loads start. The response is the same fixed body in every case.

The handoff crosses TLS 1.2 or later to the ceremony host and then host
loopback, the path the remote ceremony's keystrokes already take, and adds no
application-layer encryption (ADR-0128). The device ceremony cannot sign in to a
site that keeps its session in IndexedDB, `sessionStorage`, or a device-bound
credential, or that needs a passkey or an identity provider on another origin.
The remote ceremony stays available for those.

### Profile API contract

The public API adds profile create/list/get/revoke/delete and authentication
ceremony begin/status/cancel routes. They use only `BrowserProfileView` and
`BrowserAuthenticationView`; neither includes provider references, lease
references, launch capabilities after creation, key versions, storage state, or
provider diagnostics. Cross-principal access is 404. Creation validates one to
64 unique public-HTTPS origins. Revoke is generation guarded and takes effect
before returning. Delete is allowed only after revoke. Every mutation is
idempotent under the ordinary HTTP idempotency contract. Ceremony begin takes
an optional `mode` of `remote`, the default, or `device`; any other value is
`400 malformed_request`. The begin response, the only one that carries a
launch capability, is `Cache-Control: private, no-store`.

Before launching authentication, the application validates the login URL against
the owned profile's exact public-HTTPS origins, including the distinction
between a bare hostname and its `www` subdomain. Invalid or mismatched URLs
return `400 malformed_request` with a fixed, actionable message and no submitted
URL or provider diagnostics. Scope and profile ownership are checked first;
rejection starts no provider operation and leaves the profile available for a
corrected retry.

In a remote ceremony a site can still redirect the launch navigation to an
origin the profile does not list, most often from a bare hostname to its `www`
subdomain. Chromium does not consult Playwright's route handler for each
redirect, so the runtime uses Chromium request-stage document interception to
refuse each disallowed hop before dispatch, even when that host previously
served a page resource. The isolated runtime records the disallowed navigation
request, reports the launch as `tool.browser.url_disallowed`, and discards the
browser; any other launch navigation failure is
`tool.browser.provider_unavailable`. Neither carries raw browser text. The
application turns the disallowed redirect into the same `400 malformed_request`
with a fixed message that names the bare-versus-`www` case and asks for the
website's final address, creates no ceremony record, and leaves the profile
available for a corrected retry.

The native Website Access surface requires one Website URL: a home page or a
login page. An omitted scheme defaults to HTTPS. The client derives the primary
allowed origin from that URL and opens the full URL, preserving its path, query,
and fragment. It signs in on the device by default and offers the remote
browser as the alternative, and any profile that is not revoked can be signed
in again on the device. Website scripts, stylesheets, images, fonts,
cross-origin APIs, and embedded verification frames load automatically without
CDN configuration. This resource permission also applies when an existing
profile is reused. A resource origin cannot authorize top-level navigation or an
agent action; every document redirect is checked before dispatch. Ordinary
browser cookie and CORS rules, private-network denial, and user-only
authentication remain in force.

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

`browser.act` requires an ordinary approval by default. Two durable grants can
stand in for it, and neither is ever created through conversation text or page
content. Unattended operation requires a standing `BrowserGrant` created
through an explicit approval surface. An interactive task may use a
`BrowserTaskGrant` that the owner creates from a `browser.act` approval card,
only inside a site scope the owner configured (ADR-0129, accepted by the owner
2026-09-25). A standing grant pins:

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
surface and records the authenticated principal as approver. Creation needs a
`READY` profile whose newest sign-in has a recorded outcome, and is otherwise
`409`. A model, tool, page, scheduled prompt, or ordinary conversation endpoint
cannot create, broaden, select, or revoke a grant.

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
`BrowserActionConsequence` vocabulary. One shared, deny-biased classifier
assigns every candidate action a consequence from the element's role and each
of its label sources (accessible-name attributes, associated labels,
alternative text, button values, and visible text), its enclosing dialog's
name, a selected option, its field kind, and every path segment of its
navigation target. The worker applies it to the observation that named the
element; the isolated runtime applies it again to the live page before a
grant-authorized dispatch. A label or context that matches the exclusion
vocabulary yields a named consequence, never `routine` or `unknown`. Each text
is read as written and as displayed: in the order a right-to-left override
shows it, and with Cyrillic, Greek, and Armenian letters, Latin small capitals
and IPA letters, and the digits 0 and 1 that look like Latin letters read as
those letters. Name matching still reads English words in the DOM: a label in
another language, a lookalike letter outside that set, or a label drawn with
CSS, including a CSS bidirectional override, or with an image that has no
alternative text can defeat it, and a task grant's path prefix is then the
boundary (ADR-0129). A
standing grant can authorize only `routine` interaction, and only when every
label source reads as the same routine word; `unknown` stays hard-excluded
from it. A task grant can authorize `routine` and `unknown` interaction and no
named consequence (owner-approved 2026-09-25, ADR-0129). Every named
consequence always requires fresh approval or is denied. Page text can move an
action toward a named consequence but cannot widen a grant's scope, duration,
action cap, or field and target rules, which the isolated runtime rechecks
against the live page.

Payments, purchases, account recovery, password or MFA changes, permission
changes, legal acceptance, publication, destructive actions, file transfer,
and security-setting changes cannot be covered by a standing grant or a task
grant. Policy revalidates the profile, grant, arguments, observation revision,
and origin immediately before every action, and a grant-authorized action also
carries a dispatch constraint that the isolated runtime checks against the
live page URL and element before dispatch. Revocation takes effect at the next
action, not the next scheduled run.

### Task grants from the approval card

Task grants are offered only inside site scopes the owner configures. The
server persists the list per tenant and principal; Website Access settings
on Mac, iPhone and iPad read and edit it through `/v1/browser-task-scopes`
(ADR-0141). Each of at most sixteen unique entries is an exact public-HTTPS
origin and one nonsensitive path segment, such as
`https://www.example.com/lesson`. Adding or removing an entry takes effect
without a restart. No model tool edits the list; adding a scope does not
create a grant or replace the owner's approval of each task.

GET requires `browser.grant.read`; PUT requires `browser.grant.write` and
carries the observed revision and replacement list. Stale edits conflict;
an exact immediate replay returns the saved result without a duplicate audit.
Successful changes append `browser.task_scopes.updated` in the same transaction.
The policy row lock serializes edits with grant creation and use consumption.
Removal ends unended grants outside the new list with `scope_removed` and
an ended audit; re-adding cannot revive them. Actions authorized before removal
may finish in flight. Offers and authorization read persisted scopes, without
process caches.

`BROWSER_TASK_GRANT_SCOPES` is a one-time bootstrap seed for the configured
owner when no persisted policy exists. It never overwrites client edits or
a saved empty list. Other principals start empty. The code names no site.

A `browser.act` approval may carry a server-authored task-grant offer: the
configured scope whose origin and path prefix contain the page the pending
action was observed on, thirty minutes, and two hundred actions. The grant's
origin and prefix are the configured entry, never values taken from the page.
The owner accepts it by resolving the approval with `approve_for_task`, which
approves the pending action once and, in the same transaction, creates a
`BrowserTaskGrant`; the request repeats the offered origin and prefix and
also requires `browser.grant.write`. Nothing else creates a task grant. While
the profile's newest sign-in has no recorded outcome, the resolution is
`task_grant_unavailable` and leaves the approval pending (ADR-0128 decision
10).

A task grant is bound to the tenant, principal, session, browser profile and
its generation, agent version, policy version, origin, path prefix, creation
and expiry times, action cap, and the approval that created it. Only an
interactive top-level run can use it, in a session that carries the trusted
browser-profile binding and no schedule binding and whose newest user message
came from the owner; scheduled, inbound-surface, and device-ingested sessions,
delegated child runs, and other sessions cannot. A session holds at most one
active task grant. Thirty minutes, two hundred actions, and 4,096 typed
characters are fixed, not configurable, and enforced by database constraints.
Each authorization consumes one use and its typed characters atomically before
dispatch.

No offer is made outside a configured scope, for a page with a sensitive path
segment, when the pending action would not itself be covered, or outside an
eligible run with a `READY` hosted profile.

A task grant covers click, select, check, press, scroll, and typing into text,
search, and multi-line fields, including `unknown`-consequence actions, and
only in a model turn whose tool calls since the owner's newest message are all
browser tools. Each grant covers typing only into an input
or a text area, not a region the page made editable; select in a select; and
check on a native check box or radio, not a choice control the page built.
Typed text is covered only when it is at most 256 characters
and contains no `@`, `://`, `www.`, run of four or more digits, or
credential-shaped value. A task grant never covers a named consequence; a
credential, one-time-code, payment, or identity field; a key press on a choice
control or a select, since an arrow key moves a radio group or a list to an
unclassified option; a disabled element; a
key press or typed text on an element that does not itself hold focus; an
unnamed element; an element with a label source the facts do not carry
whole, whose unread part could hold an excluded word; a link or form target
outside the origin or prefix or with a sensitive path segment; a download
link or file input; or a page outside the
prefix or with a sensitive path segment. Any click on an element in a form,
and Enter or Space pressed on one, counts as a submission whatever the
element's role. A path segment is sensitive when it matches the exclusion
vocabulary or names an administrative, authentication, pricing, or messaging
area. The worker checks coverage against the observation that named the
element; the isolated runtime repeats it against the live page and refuses
with `tool.browser.grant_not_applicable`. The worker decides before it
consumes a use, which is never refunded, so on a page that has not changed it
covers nothing the runtime refuses; the exceptions are what the observation
cannot show, an element that does not hold focus once focused and the options
a selection reads, and each can still cost a use.

`browser.task_grant.created`, `tool.call.authorized` (with the grant id, use
ordinal, and the approval view of the action), and exactly one
`browser.task_grant.ended` audit each grant. The owner lists and revokes task
grants through `/v1/browser-task-grants` under `browser.grant.read` and
`browser.grant.write`. Expiry, exhaustion, revocation, supersession, profile
revocation or deletion, a new sign-in (which advances the profile generation,
ADR-0128), a changed agent or policy version, and removal of the scope end a
grant. `BROWSER_TASK_GRANTS_ENABLED` gates offers, `approve_for_task`, and the
task-grant routes.

### The browser.act approval view

The approval for `browser.act` names what will happen. Its summary holds only
server-authored words, a closed role word, and the page host and path. Its
arguments are a view, not the action: the action kind, the page origin and path
(never the query or fragment), and, quoted as website content, the page title,
element role, accessible name, the element's visible text when it differs,
the enclosing dialog name, and the chosen option; plus the key, scroll
distance, or typed text. Typed text is shown so the owner can judge it, except
that a password, one-time-code, or payment field and any credential-shaped
value are redacted; text over 512 characters carries its digest. The view never
contains the element reference or page revision. When the worker can no longer
describe the element, the view says so and makes no offer.

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

The explicit read binding is carried by `ScheduleDefinition.browser_profile_id`
and its immutable revision (ADR-0150). The authenticated schedule surface
validates the owned ready profile and the requested `browser.profile.read`
scope. Each occurrence receives that reserved session binding. The run worker
checks it before model work, without expanding the scheduler's database role;
profile or read-tool unavailability fails visibly through ordinary run outcome
accounting. Existing schedules require an explicit full-definition update.
Hosted adapters compare tenant and principal identity and require the run's
scopes and roles to be subsets of the configured owner's authority; restricted
scheduled authority does not change profile ownership.
This read integration creates no action grant, and model-callable schedule
creation still selects no profile.

## Bounds and stable failures

Readable text, element count, accessible names, URLs, titles, screenshots, and
provider responses have independent byte and count ceilings. The first slice
limits readable text to 256 KiB and interactive elements to the first 256
visible ones in traversal order, chosen from at most 4,096 candidate nodes per
window. ADR-0157 defines bounded continuation and explicit terminal scan coverage.
Provider responses are validated before they enter a tool result.

Navigation and every action wait for the page to settle before the provider
observes it (ADR-0130). After a document load the provider waits for
document readiness, then for the finite application requests already in flight
at settling entry to finish, then for the document to go 300 ms without a DOM mutation
(ADR-0159); an action that starts a main-frame navigation first waits for the new
document. The whole wait is bounded by 2 seconds. Settling never fails a call:
a page still changing at the bound is observed as it is, with `bound_expired`
readiness metadata.

Before a `browser.act` click, Playwright checks actionability with a trial bounded by
5 seconds. A trial timeout sends no click and returns the existing
`tool.browser.element_not_found` refusal, preserving the lease and action
sequence so the agent can observe an obstructing overlay. The actual click
also has a 5-second bound, leaving time within the 30-second tool budget for
validation, settling, observation, and transport. No forced click or automatic
publication retry is permitted. A failure after actual dispatch remains
uncertain; the outer tool deadline also preserves non-idempotent write
uncertainty once the effect watermark is persisted.

The stable reason-code family includes:

- `tool.browser.url_disallowed`
- `tool.browser.provider_unavailable`
- `tool.browser.navigation_cancelled`
- `tool.browser.profile_unavailable`
- `tool.browser.authentication_required`
- `tool.browser.needs_user`
- `tool.browser.page_changed`
- `tool.browser.element_not_found`
- `tool.browser.action_not_allowed`
- `tool.browser.grant_not_applicable`
- `tool.browser.output_invalid`
- `tool.browser.outcome_unknown`

When the runtime dismisses a beforeunload dialog and Chromium aborts navigation
while the same allowed page remains open, navigation returns
`tool.browser.navigation_cancelled`, without retry, and retains the page and
lease. The model can observe the existing draft and continue under ordinary
action approval. This does not accept the dialog, classify other transport
failures as cancellations, or change uncertain-write handling (ADR-0151).
The ephemeral Playwright provider serializes navigation, observation, actions,
uploads, and closure on one operation lock, including startup and result
validation. A concurrent dispatch cannot consume another navigation's dialog
state or mutate its page before the navigation finishes. The hosted provider
retains its existing lease-operation serialization.

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
- Mutating actions require an approval, a matching unexpired standing grant,
  or a matching unexpired, unexhausted task grant inside an owner-configured
  scope; named consequences always require fresh approval or remain denied,
  and a task grant's origin, path prefix, expiry, and exclusions are rechecked
  against the live page before dispatch.
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
   approval, an exact standing grant, or an exact task grant. **M10.**
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
   reauthentication, and consent produce `needs_user`. A device ceremony's
   session reaches only the isolated service, once, through its single-use
   capability; the service accepts only cookies and storage scoped to the
   profile's origins and seals only its own verifying browser's state, only on
   its own verification. **M10.**
10. **Standing and task grants.** A standing or task grant is exact, expiring,
    revocable, policy-revalidated before every action, and unable to cover any
    named consequence. A standing grant covers only routine interaction. A task
    grant is created only from an approval card inside an owner-configured
    scope, bound to one session, capped at thirty minutes, two hundred
    actions, and 4,096 typed characters, confined to its origin and path
    prefix, and rechecked against the live page in the isolated runtime
    (ADR-0129). **M10.**

These ten registry-backed gates are the browser tranche's blocking delivery
contract. All ten resolve to executable Milestone 10 checks. Milestone 11
scheduling consumes the resulting profile and grant references but owns its own
future gates and is not part of this Milestone 10 gate area.

## Browser improvement program

ADR-0156 records the owner's instruction to implement the
[self-hosted improvement program](../browser-automation-improvement-proposal.md).
Its first construction slice requires real-browser verification and repairs
model presentation without changing action authority or provider observations.

At tool-output admission, a large successful builtin browser result is projected
as bounded valid JSON for the model. The projection retains the exact page
revision and whole element records, counts omitted elements and text bytes,
and states any omitted location/title. Prefer at least one complete control
that can fit over optional metadata and prose. If even the complete revision
and coverage cannot fit, emit an explicit whole-observation omission with no
actionable elements, never a shortened revision. Its serialized content and capture
reference fit the ordinary inline byte budget. Canonical content and the
owner-scoped full artifact remain unchanged. Projection uses only validated
result data inside the trusted pipeline; it never trusts an alternate result
supplied by a tool. The persisted projection replays without recomputation.

The projection preserves observation order and prevents broken JSON and lost
revisions. ADR-0157 adds bounded continuation through the same `browser.observe`
tool; it does not claim complete coverage of a changing page. Legacy history
retains its persisted presentation until it has a newly admitted browser result.

Observation capture publishes a revision and its handle map only after the
whole bounded result validates. Any capture or title failure, validation error,
or cancellation joins outstanding captures, releases acquired handles, and
invalidates the preceding revision. A failed observation leaves no actionable
references, continuation, or retained candidate offsets. ADR-0157 bounds handle
acquisition before the visibility scan as well.


### Bounded live expansion and synthetic component tasks (ADR-0157)

`browser.observe` version 1.7.0 accepts `{}`, or exactly one of `after` (a
current element reference, 1–128 characters) and `cursor` (a current opaque
provider continuation, 32–128 characters). Unknown fields and invalid types
are rejected before binding or dispatch. `browser.navigate` and `browser.act`
also use version 1.7.0 for their additive observation output contract. No
selector, script, offset, or profile is model selectable. An adapter without
expansion support returns `tool.browser.action_not_allowed`.

The published `browser.observe` 1.0.0 and offset-paged 1.1.0 contracts remain
registered for pinned chats, alongside `browser.navigate` and `browser.act`
1.0.0. New chats receive 1.7.0 and its provider-bound continuations. The older
offset pages continue to fit the configured inline budget. All ephemeral
expansion and extraction calls share the page-operation lock used by navigation,
actions and uploads.

Production providers implement this optional port capability throughout the
hosted path. The authenticated observe route accepts an optional `expansion`
object; the lease is validated and its lock held before the runtime is called.
Read expansion never advances the action sequence or grants action permission.
Observation facts are cached with the new revision under the same session.
Old services may reject this additive request; workers must not silently drop it.

The trusted selector walks the main document and open shadow roots depth first,
visiting shadow children before light children. It returns at most 4,097 handles,
then the runtime checks at most 4,096 candidates and retains at most 256 visible
controls. An `after` request includes its anchor in that scan window, validates
that it is still the same connected node at its recorded position, and excludes
it from the returned controls. Removed or moved anchors fail with
`tool.browser.page_changed`. The cursor moves beyond the last selected control
or the scanned window when no visible controls exist. Candidate positions stop
at 65,536. Every result includes `coverage` with `candidate_offset`,
`scanned_candidates`, optional `next_cursor`, and `scan_limit_reached`.

The output's `next_observe` is the next request object. Context projection
continues after the last contiguous control actually exposed to the model,
including when canonical output bounding retained only a prefix. It uses the
provider cursor only after exposing the whole window, or an empty one. If an
oversized first record cannot fit, it exposes other usable records but declares
`expansion_blocked: inline_control_too_large`; it never silently skips the gap.
The full owner-scoped artifact remains available. Continuation and coverage
are included in byte accounting. The progress digest includes candidate offset
so moving through empty windows is not classified as an identical observation.

References and cursors belong to the current runtime/document and expire on
observation replacement, capture failure, close, and main-frame navigation.
Expansion publishes a fresh revision and references. A stale or foreign cursor
fails with `tool.browser.page_changed`. Cursor windows read a live DOM; edits
before the offset can repeat or omit controls, so coverage never claims a frozen
whole-page snapshot. Frames, closed shadow roots, semantic relevance ranking,
and oversized-control recovery remain outside this slice.

Control names now prefer bounded `aria-labelledby`, `aria-label`, and native
HTML label text before the existing title, placeholder, and rendered-text
fallbacks. Stored input values remain excluded; independent label facts and
live task-grant checks still apply. This is not a complete accessible-name
algorithm.

`tests/browser_task_manifest.json` defines twelve scripted component tasks,
including lessons, multilingual names, dense and hidden controls, open shadows,
a slow SPA, and forms. Drivers consume observations at 1,024 or 4,096 bytes and
follow model-visible continuation. An independent synthetic server checks the
number and payload of effects; dispatch success alone is insufficient. The
mandatory browser baseline version 2 includes per-task IDs, outcomes, operation
counts, durations, effect counts, and budgets, without page text or values.
Missing tasks or mismatched effects prevent a verified report. Model calls are
zero and live task quality and model cost remain unmeasured. This complements,
not replaces, the existing policy, isolation, and hostile-page verification.

### Content-free operation diagnostics (ADR-0158)

Terminal builtin browser tool events may carry `browser_diagnostics` version 1.
Trusted execution collects at most 64 phase summaries, in completion order,
with total elapsed milliseconds and explicit truncation. Each record has a
closed phase, placement, outcome and failure category plus bounded elapsed
milliseconds. Nested durations overlap. Provider response content, exception
text and class names, URLs, page labels, input values and secret material are
excluded by the typed contract. Collection is task-local and has no effect on
authorization, write uncertainty or retry decisions. The hosted session protocol
may return the summary as optional metadata; invalid summaries are discarded as
a whole. Existing clients and services may omit it. Existing event retention,
access and deletion rules apply; no new raw tracing or capture store is enabled.

### Bounded readiness and visible postconditions (ADR-0159)

Version 1.2.0 adds optional `readiness` (`dom_quiet` or `bound_expired`) to
navigation/action observations. Settling waits for document readiness, drains a
snapshot of at most 256 pending document, script, stylesheet, fetch and XHR
requests, then waits for 300 ms without DOM mutation within the same two-second
budget. Later requests do not extend the snapshot; failed requests finish it
and successful event streams finish at their headers without body reads. A
tracking overflow or deadline expiry reports `bound_expired`; the set resets
with a new document. A bounded page round trip before the snapshot lets initial
request events queued after DOMContentLoaded enter the set. It does not require network silence. Explicit observation does not claim a settling check.

`browser.observe` may instead accept `wait_for`; `browser.act` may include
`postcondition`. Each is an exact visible-control predicate with nonempty role
(up to 64 characters), name (up to 1,024), optional disabled/checked booleans and
`timeout_ms` (0–5,000, default 2,000). Wait and expansion cannot be combined.
The trusted tool reads the current observation window, with at most twenty
additional observations spaced at least 250 ms apart. The predicate's deadline
and existing tool/run deadline bound all reads. A result reports `satisfied`,
`ambiguous` or `not_observed`, window scope, observation count and elapsed time.
It proves only the visible predicate, not the causal effect of an action.

Exactly one role/name match must also meet the requested state. Hidden values,
selectors, scripts and frame traversal are unavailable. Cancellation of an
in-flight capture returns no stale observation. Evidence loss after dispatch
retains `outcome_unknown`; unmet predicates never prove a write was unsent.
An outer tool timeout after the browser action effect watermark is also
uncertain and non-retryable. There is no automatic action retry or target substitution. Existing approval,
grant consumption and effect watermark behavior is unchanged.

### Bounded semantic page structure (ADR-0160)

Browser tools version 1.3.0 optionally return `regions` and `region_coverage`.
Regions are visible dialogs, alerts, status messages, forms and headings,
prioritized in that order within a main-document scan of at most 8,192 nodes.
At most 32 whole records are retained; each text summary inspects at most 256
nodes and contains at most 512 characters, with explicit truncation. Hidden
subtrees and input, textarea, select and editable content do not contribute.
Frames and shadow roots are outside this region scope. The existing general
readable-text field and control capture are unchanged.

Version 1 coverage records scope, scanned nodes, scan exhaustion and the known
omitted-region count within the scanned prefix. A bound is not whole-page
completeness. Region references bind to the enclosing revision but are evidence
identifiers only: `browser.act` and `browser.observe(after=...)` cannot use them.
Capture failures discard the entire observation as before. All region content
is external-untrusted and conveys no approval, authentication or effect proof.

Canonical byte bounding and model admission count omitted whole regions. The
model projection retains room for a usable control before optional summaries,
then favors bounded region evidence over duplicated prose. Hosted transport and
artifact replay preserve the fields; legacy observations may omit them. The
progress digest includes semantic evidence, excluding fresh opaque references.

### Deterministic collection extraction (ADR-0161)

Browser tools version 1.4.0 add `browser.observe(extract=...)`, exclusive with
wait/continuation. The request names the current revision, collection kind
(table/list/form), visible index 0–15, one to eight named primitive typed columns
and a 1–50 row limit. This reads the current collection at that position; it does
not promise that a dynamic site's collection kept its identity. A stale or
foreign revision fails before capture. A successful read replaces the ordinary
observation and its action references, adding revision-bound evidence references
that cannot be used for actions or expansion.

The optional provider capability uses the existing hosted observe route, lease,
lock and origin binding. It does not dispatch or consume action authority.
Capture failure/cancellation uses atomic cleanup; unsupported adapters refuse.
Collection discovery scans at most 8,192 main-document nodes; row discovery at
most 4,096 descendants; frames and shadow roots are excluded. Requested cell
summaries inspect at most 256 nodes and retain 256 UTF-16 code units without
splitting a character. Hidden and editable values are excluded. Table spanning
cells are unsupported; lists expose text; forms expose label, role, disabled,
checked and required columns, never entered or selected values.

Results retain bounded source text and typed values, explicit missing/invalid/
truncated statuses and row schema validity. Required missing and all invalid or
truncated cells invalidate the row. Numeric conversion is locale-independent,
finite and bounded to exact JSON integers; only true/false booleans are coerced.
Schema validity conveys neither truth nor action completion. Collection and
row scan bounds, row limits and known omissions are reported. The 64 KiB
extraction ceiling and smaller model budgets drop whole rows with counts;
otherwise the whole extraction is explicitly omitted. In extraction mode,
requested rows precede optional controls, regions and ordinary prose, with exact
revision, coverage, untrusted framing and durable artifact/replay preserved.
Fresh reference bytes never count as progress.

### Bounded readable snapshots (ADR-0162)

Version 1.5.0 replaces whole-body readable-text acquisition with a filtered,
iterative collector inspired by Stagehand's snapshot-before-inference design.
Read the main document and open shadow roots, excluding frames and closed roots.
Visit at most 8,192 nodes, inspect 262,144 UTF-16 text units and emit at most
256 KiB of UTF-8. Hidden, ARIA-hidden and editable subtrees, input/textarea/select
values, custom editable roles, scripts and embedded documents supply no text.

To preserve ADR-0148 draft continuation, an individually approved rich-text type
may retain one submitted value (at most 4,096 characters) in the runtime until
closure, replacement or loss of its live binding. An observation may echo this
already-known value as `Confirmed submitted draft` only after a boolean check
that the same visible, non-credential editor in the same document still renders
exactly that value. The comparison bounds the editor to 256 nodes and 4,096 text
units before rendering; no editable page text crosses the browser boundary.
A focused read outside the editor omits a still-valid receipt without clearing it.
Hidden, changed, oversized or credential editors invalidate the receipt, which
cannot later reappear. The confirmation fits within the existing 256 KiB text
ceiling or is omitted. It does not expose pre-existing drafts, read other input
values, authorize publication or prove that a server accepted a submission.
Normalize whitespace while preserving block and table-cell boundaries and
adjacent inline text. Do not duplicate slotted content or split Unicode.

Optional `text_coverage` version 1 carries scope, scanned nodes and text units,
node/text limit flags, and known bytes omitted by canonical bounding. Model
projection preserves it alongside its separate omission count, or explicitly
omits the whole observation when essential metadata cannot fit. Its compact
model view retains text/region scope/version and uses typed defaults for absent
false flags, null cursors and zero text-omission counts in provider coverage.
For observations supporting continuation, reserve the first control that can
fit after removing optional location/title context before admitting that context
or prose, keeping prefix discovery usable. Hosted transport and replay retain it; legacy providers may omit it. Limits describe incomplete
evidence, never absence. Failed capture invalidates handles and revision through
the existing cleanup; action authority and control continuation are unchanged.

### Focus and bounded recovery (ADR-0163)

`browser.observe` accepts `region_ref` and `expected_revision`, with optional
`text_offset`, exclusively of other modes. A current semantic reference selects
one stable main-document region. Focused controls and text share that region;
control expansion retains it. `focus` metadata identifies the current region,
byte offset and captured text size. `next_text` advances only over text actually
returned, including when the model budget is smaller than the canonical result.
Text continuation rechecks unchanged captured text. Detached regions, stale
revisions and changed continuation text fail with `tool.browser.page_changed`.
Main and section regions are discoverable; active dialog controls precede general
navigation. Existing node, candidate, UTF-8, privacy and origin limits apply.

A stale observation failure permits one separately audited fresh read, at most
three automatic recovery reads per run, within tool and run budgets and deadlines.
It never permits action replay. The original failure remains visible alongside
any fresh read evidence. Authentication challenges discard references and require
owner-controlled sign-in. The runtime may suspend for that owner action twice;
the failed call is already complete and is not replayed on resume. Existing
profile generation checks invalidate old leases, approvals and grants. Uncertain
writes retain `tool.browser.outcome_unknown` and are never automatically resent.

For small model budgets, retain coverage scope/version, limit flags and omission
counts inline. Scan counters stay in the canonical artifact with
`diagnostics_in_artifact: true`; this amends ADR-0162's counter projection only.
Reserve a compact region label so the next focused read remains discoverable.
Focused model output names `coverage.scope: focused_region`; full range metadata
remains canonical and `next_text` uses the current opaque text cursor and admitted
byte count. Cursor rotation binds every continuation to the latest observation.
A sign-in challenge after an already dispatched action returns `interruption:
needs_user` without controls and can suspend the run; it does not prove the write
completed. `browser.act` records that case as `outcome_unknown` with a fixed,
content-free sign-in marker; the runtime may ask the owner without retrying it.
CAPTCHA detection excludes the passive reCAPTCHA badge and its descendants;
credential fields still interrupt there. Detection is bounded negative evidence, not
positive login verification.

### Autonomous verification and continuation (ADR-0164)

The owner-authorized minimal-user-involvement tranche extends visible conditions
with positive rendered text, exact location, semantic regions and typed rows.
Success and optional failure predicates share one bounded read budget; conflicting
evidence remains ambiguous. Missing evidence cannot authorize a repeated write.
Site authentication definitions are maintained and versioned by the trusted
implementation, never supplied by page content. Positive protected-state checks
and the existing challenge guard determine readiness; valid sessions are reused.

Authentication waits retain the trusted run/profile/generation binding. A
verified service outcome may resolve the matching wait and queue a fresh read
without a user reply. The authentication record and its profile update share
one timestamp, so a real clock cannot make verified evidence appear stale.
Resolution is durable and idempotent, preserves cancellation
and run limits, and never transfers a grant across a sign-in generation. Ordinary
recovery and workflow steps use the same authorization and effect pipeline.
Repeated notices of one blocker are consolidated; unresolved work ends with a
truthful bounded outcome. The detailed decisions are in
[ADR-0164](../adr/0164-browser-autonomy-and-verified-resumption.md).

Completion conditions accept exactly one legacy role/name predicate or an
`evidence` predicate: one complete normalized rendered-text line, one complete
semantic region, an exact public HTTPS URL, or a unique schema-valid collection
row with typed expected cells. A simple `failure_evidence` predicate may accompany
success. Conflicting predicates or duplicate matches are ambiguous; hidden,
truncated or absent evidence cannot prove completion. Row checks use fresh
revision-bound extraction. A stale verification read permits one fresh capture
within the shared five-second/21-read limit, never another action. Condition
status, rather than unrelated page churn, drives repeated-wait detection.

The isolated service optionally loads `BROWSER_PROFILE_VERIFICATION_FILE`, a
private absolute mode-0600 JSON file of at most 64 KiB. Its version-1 `sites`
catalog contains at most 32 closed definitions: `id`, `version`, `profile_id`,
`origin`, `protected_path`, positive text/region `ready` evidence and exact region
`account` evidence. Each profile has one definition. The protected path is a
literal path, not a selector, script, credential or permission. Both device and
remote sign-in compare isolated signed-in and empty-session loads. A configured
site must show the intended account and protected state only in the signed-in
load. Stored configured sessions undergo another bounded protected-page check
before lease publication. Wrong accounts and public-page matches fail closed.
Lease startup and protected-page verification reserve capacity and exclude other
acquisitions for that profile without holding the service-wide admission lock.
Publication rechecks the profile identity, revocation and deadline; failure or
cancellation releases capacity only after the runtime closes.
Readiness is recorded only after sealing succeeds. An older ceremony cannot
change a profile once a newer ceremony exists.

`runtime/browser-workflows.yaml` is a closed version-1 catalog of reviewed
definitions. A recipe binds ID/version, exact origin, explicit task-intent
phrases, at most 16 navigate/observe/act/extract steps and a maximum 900-second
duration. Selection requires one unambiguous matching owner request and a ready
selected profile. Actions require unique fresh role/name targets and a positive
postcondition. Every tool call uses normal validation, policy, approvals, current
budgets, cancellation and invocation persistence. Recipe/profile/agent/policy
bindings and verified progress survive suspension; generation changes invalidate
pending actions. An uncertain dispatched action gets read-only reconciliation,
then a verified or explicitly unverified stop. Final reporting cannot issue more
tools. The workflow cap is 64 calls and reserves one ordinary run step for the
report. These are ceilings, not additional budget allowances.

The shipped recipe catalog and site definitions are empty. Engineering-owned
definitions require site-specific fixtures and review before activation; this
tranche proves the mechanism using controlled account/report fixtures and makes
no claim of configured live-site coverage. Users do not author or maintain these
technical definitions. Unknown sites retain protected-page differential sign-in
verification and the ordinary agent execution path.

All hosted browsers remain headed (ADR-0145). A trusted ceremony or differential
verification explicitly permits login-page reads; headed agent leases still
check authentication interruptions. Remote verification reserves two additional
browsers within the existing service-wide cap of three, until both loads close,
including after cancellation. Frame and input requests wait for the ceremony
operation lock without holding the service admission lock, then revalidate the
capability before accessing the page.
