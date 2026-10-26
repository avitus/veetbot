---
title: Cockpit Mode and Agent-Authored Widgets
status: design
canonical: true
---

# Cockpit mode and agent-authored widgets

This document specifies Milestone 35. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md). It reuses rather than replaces
these existing pieces:

- the client-mode shell of [email-experience.md](email-experience.md);
- the execution service of [sandbox-isolation.md](sandbox-isolation.md);
- the deferred tool index of [context-engine.md](context-engine.md);
- the policy pipeline of [policy-and-approvals.md](policy-and-approvals.md);
- the streaming conventions of
  [http-api-and-streaming.md](http-api-and-streaming.md).

[ADR-0240](../adr/0240-cockpit-mode-and-agent-authored-widgets.md) records the
architectural decisions and the owner's authorization.

The owner wants a heads-up, real-time display of everything the owner
monitors, and wants the assistant itself to build it. Milestone 35 delivers
**Cockpit**, a third client mode beside Chat and Email. Cockpit is a board of
**widgets**. A widget is a small React component that the production assistant
creates, updates and deletes. Its code is committed to a dedicated branch of
this repository and rendered on the owner's iPhone, iPad and Mac inside a
sandbox that has no network. Widgets draw their data from **feeds**. A feed is
either a projection of state the platform already holds or a snapshot the
assistant publishes from an approved schedule. The visual layer is a **kit** of
primitives designed in MagicPath and ported into code, so that every widget the
assistant writes looks like part of the same instrument.

## Scope

### Owner decisions

The owner decided these on 2026-10-22:

| Area | Decision |
| --- | --- |
| Authorship | The assistant creates, updates and deletes widgets. |
| Visual layer | Designed in MagicPath, in the Veetbot project beside the A2 designs. |
| Source of truth | Widget code lives in this repository, on the dedicated branch `cockpit/widgets`. A change goes live in about a minute. People and Claude Code may commit there too, and the server picks their commits up. |
| Privacy | The repository stays public, so widget code is data-free. Personal values live in private settings and feeds. |
| Approval | Widget changes from the owner's own turns ship without a per-change approval. Turns influenced by untrusted content still ask. |

### In scope

1. The Cockpit mode on iPhone, iPad and Mac. Mac (full screen) and iPad (with
   a keep-screen-on toggle) are the primary heads-up screens.
2. The `cockpit/` TypeScript workspace on `main`, which holds:
   - the kit;
   - the host page and realm;
   - the builder;
   - the schemas;
   - reference and adversarial example widgets.
3. The widget lifecycle: validate, privacy-screen, build, commit, activate,
   revert, delete and restore, plus import of human commits.
4. The builder sandbox image.
5. The GitHub App adapter and the commit outbox.
6. Platform feeds and assistant-published feeds.
7. The private widget settings and per-device-class layouts.
8. The snapshot route, the event stream and the asset routes.
9. Five deferred builtin tools.
10. The native Cockpit mode, including the Mac right panel.

### Out of scope

- Deterministic collectors that fetch outside data with no model run. These
  would serve sub-minute external data, but they need an egress allowlist and
  their own design.
- A browser or television rendering of Cockpit.
- WidgetKit, Lock Screen widgets and Live Activities.
- The assistant using MagicPath at runtime.
- Webhook-driven import; polling comes first.
- Widget-to-widget communication.
- Widgets that act: a widget can only display and offer two
  navigation intents.
- More than one tenant's widgets on one branch.

## Foundations this design reuses

- **The mode shell.** `ClientMode`, the app coordinator and the per-mode right
  panel (ADR-0092, ADR-0237). The Email design says a new mode is "a small
  mode descriptor and a presentation module"
  (`email-experience.md:52-53`), and that a mode confers no authority.
- **The execution service.** It runs gVisor containers with a read-only root,
  networking off, an empty environment and run leases. Its one-image
  assumption is an open question in `sandbox-isolation.md:1735-1739`. This
  design answers it for exactly one additional, platform-owned image.
- **The tool pipeline.** Classification decides policy, and a `tool_rules`
  entry can allow one tool by exact name. The trust overlay escalates an
  untrusted origin or untrusted arguments. The credential-to-egress hardline
  rule covers `publication` arguments.
- **The deferred tool index.** Definitions are capped at thirty items, and
  that cap already binds the owner's roster (`context-engine.md:379-383`).
  The deferred index (`context-engine.md:410-415`) admits a tool as a
  one-line entry reached through `tool.call`.
- **Durable live delivery.** The run event stream already works this way: a
  row is written, then NOTIFY sends its sequence inside the same transaction,
  and readers replay from the table, send a heartbeat, and resume from
  `Last-Event-ID`.
- **Agent-authored, later-executed content.** `skill.manage` set the pattern
  of an expected-revision edit, a validator, untrusted storage, revision
  history and archive-not-delete. Widgets follow it, and add public
  publication and a rendering sandbox.

## The domain model

Every row is tenant- and principal-scoped.

- **Widget.** Fields: `slug` (unique per principal, `^[a-z][a-z0-9-]{1,47}$`),
  `state` (`active` or `deleted`), `live_revision_id` and `created_at`. A
  deleted widget keeps its revisions and settings until purged.
- **Revision.** An immutable package. Fields:
  - `files`: the manifest, component and fixtures;
  - `source`: `assistant`, `owner_git` or `revert`;
  - `origin_run_id` when the assistant wrote it;
  - `state`;
  - `diagnostics`;
  - `bundle` and `bundle_sha256`;
  - `kit_api`;
  - `commit_sha`.

  The states run in this order:

  ```text
  validating ─► rejected
             └► pending_commit ─► committed ─► live ─► superseded
                              └► conflicted
  ```

  Only `live` renders. At most one revision per widget is `live`.
- **Settings.** The private value document for a widget, validated against
  the settings schema in the live manifest. Settings never leave PostgreSQL,
  except to the owner's own clients and the widget that declares them.
- **Feed.** `id` is either `veetbot.<name>` (platform) or `agent.<slug>`
  (assistant). A feed also has a closed JSON Schema of at most 8 KiB, a schema
  version and a trust label.
- **Feed snapshot.** `observed_at`, `expected_interval_seconds`, `data` (at
  most 64 KiB) and `sequence`. A feed keeps a bounded history (knobs below).
- **Layout.** One per device class (`compact`, `regular`, `wide`). It is an
  ordered list of placements, each a widget slug and a size. Layouts are flow
  grids, not coordinates.
- **Commit outbox entry.** Fields: the revision, the operation (`write` or
  `remove`), attempts, `next_attempt_at`, the last error and the expected
  parent sha.
- **Import failure.** The branch commit, the widget slug, the diagnostics, and
  whether the owner has seen it.
- **Cockpit event.** A per-tenant monotonic `sequence`, a `kind` and a small
  payload. It is retained for the stream's resume window.

## The widget package

A widget lives on the branch at `cockpit/widgets/<slug>/`:

```text
cockpit/widgets/<slug>/
  widget.json             manifest
  widget.tsx              one default-exported React component
  fixtures/typical.json   synthetic feed and settings values
  fixtures/empty.json
  fixtures/stale.json
```

The **manifest** contains:

- `schema: 1`;
- `slug`;
- a generic `title` of at most 40 characters and a generic `purpose` of at
  most 200 characters, such as "Flight tracker" and "Shows departure,
  delay and gate for one flight";
- `kit_api`, an integer;
- `sizes`, a subset of the kit's tile sizes `sm` (1×1), `md` (2×1), `tall`
  (1×2), `lg` (2×2) and `wide` (the full row);
- `feeds`, a list of `{id, history}` where `history` is at most the knob
  ceiling;
- `settings`, a closed JSON Schema whose properties are strings, numbers,
  booleans or enumerations with bounds, and which the owner can edit as a
  generated form.

The **component** receives `{feeds, settings, size, theme}`, where:

- each declared feed is
  `{observedAt, expectedIntervalSec, error, data, history}`, with instants in
  epoch milliseconds; before its first snapshot it is a pending envelope with
  `observedAt` and `data` null, `expectedIntervalSec` 0 and no history, so the
  kit renders it as pending;
- time comes from the kit's clock (`useNow`), which the realm drives with
  server-corrected time and each fixture freezes, so rendering is
  deterministic for a given input.

The component may import only `react` and `@veetbot/cockpit-kit`. It renders
through kit primitives and plain elements styled with kit tokens.

The **fixtures** are synthetic. They exist for the build's render check, for
previews and for the gallery. Each one supplies feed values and settings
values that satisfy the declared schemas.

Package limits:

- at most twelve files and 256 KiB in total;
- the component at most 64 KiB;
- each fixture at most 64 KiB;
- the built bundle at most 256 KiB.

## The kit and the runtime

### The `cockpit/` workspace

The workspace follows the `website/` precedent. It has its own `package.json`
and lockfile, ESLint and TypeScript configuration, and Node tests. It adds a
`make test-cockpit` target that joins `make check` and the CircleCI static
lane, with a lockfile-keyed cache.

```text
cockpit/
  kit/        @veetbot/cockpit-kit: tokens and primitives
  host/       the trusted host page, the realm document, the bridge
  builder/    the validate/screen/build/render CLI
  schema/     JSON Schemas: manifest, settings, feed envelope, bridge messages
  examples/   reference widgets and the adversarial corpus
```

`main` must never contain `cockpit/widgets/`, and a structure test enforces
this. Widgets live only on their branch. A `make cockpit-widgets-worktree`
target checks the branch out under `.worktrees/` so a person can build it
locally against the kit on `main`.

### The kit

- **Tokens.** The kit's tokens are A2's, taken from
  `clients/apple/Veetbot/Views/AppTheme.swift` for both light and dark, plus a
  night treatment for an always-on screen.
- **Primitives.** Tile, Metric, Delta, Sparkline, Bars, Gauge, Countdown,
  StatusPill, List and Row, Timeline, and the Empty, Stale, Error and Pending
  states, plus the boards' layout helpers Split, Headline and Between.
  `TileHost` lets the realm give each widget's Tile its placement size,
  selection and tap handler.
- **Behaviour every primitive shares.**
  - It shows freshness from the feed envelope its Tile is given; a stale or
    failed Tile dims every primitive inside it.
  - It renders strings only as text, never as markup.
  - It offers no link, telephone or URL affordance.
  - It reserves amber for needs-attention.
- **Change control.** The kit is ported from the approved MagicPath designs
  (see [The visual design](#the-visual-design)) and changes only through
  ordinary reviewed commits to `main`.
- **Versioning.** `kit_api` increases on any breaking change.
- **`kit.describe` catalogue.** A catalogue generated from the kit's types
  feeds the read tool's `kit` operation. It lists each primitive's props with
  an example and the authoring rules, including the naming rule that keeps
  identifiers clear of the credential pattern.

### The runtime

The runtime is four content-hashed assets: the host page, the realm document,
React, and the kit. They are built at release inside the builder image from
`main`'s `cockpit/` sources. They are copied into
`artifact_root/cockpit-runtime/` keyed by hash, and retained while any live
revision's `kit_api` needs them. They therefore outlive the release
directory's rotation. Widget bundles are classic IIFE scripts that read
`React` and `VeetbotKit` from the realm's globals. ES modules from an opaque
origin would need CORS headers the scheme handler does not send.

### The host and the realm

- **The host page** is trusted platform code. It owns these parts:
  - the grid;
  - edit-mode affordances drawn by native code;
  - the bridge to native code;
  - one realm iframe with `sandbox="allow-scripts"` and no `allow-same-origin`;
  - a CSP of `default-src 'none'` that permits scripts and styles only from
    the scheme, never `unsafe-eval`, and sets `connect-src 'none'`.
- **The realm** loads React and the kit once. It mounts each widget's bundle
  into its tile behind an error boundary. A thrown render shows the kit's
  Error state for that tile only.
- **The watchdog** has two halves, because the realm can share the host
  page's main thread.
  - The host sends the realm a heartbeat. If the realm misses the deadline
    (knob), or loads a second document because a widget navigated it, the
    host remounts the realm and quarantines the widget whose revision changed
    most recently.
  - A widget that never yields also stops the host. Native code therefore
    pings the host from outside the web view; when the host misses the
    deadline, native code reloads the view and leaves the most recently
    changed widget out of the next snapshot.

  A quarantined widget shows a "paused" state natively until its next
  revision or the owner's retry.
- **Data flow.** The realm receives only, per widget, its declared feeds at
  the declared history depth, its own settings, its size and the theme, with
  the clock's server offset. All of it arrives through typed postMessage
  envelopes; the realm's messages back are dropped unless they come from the
  realm's own window and name a placed widget.

## Validation, the privacy screen and the build

The builder CLI runs the same steps whether it is called from a tool, the
importer, release preflight or a developer's checkout. In order:

1. **Manifest and files.**
   - The schema validates.
   - The slug matches the directory.
   - Files are on the allowlist and within the size caps.
   - The fixtures exist and satisfy the declared feed and settings schemas.
   - No path is absolute, contains `..`, or is a symlink.
2. **Imports and lint.**
   - Imports come only from `react` and the kit.
   - These references are rejected: `fetch`, `XMLHttpRequest`, `WebSocket`,
     `EventSource`, `navigator.sendBeacon`, `RTCPeerConnection`, `eval`,
     `Function`, dynamic `import()`, `parent`, `top`, `opener`,
     `document.cookie`, `localStorage`, `sessionStorage`, `indexedDB` and
     `dangerouslySetInnerHTML`.
   - Literal colour values outside kit tokens are rejected.

   These rules are hygiene and fast feedback, not a security boundary;
   computed property access defeats them. The boundary is the realm.
3. **Credential shape.** Every file is checked against the pattern of the
   hardline `credential_to_egress` rule. Without this step, the hardline rule
   would deny the write tool outright on ordinary code such as
   `token: string`. Here the assistant gets a diagnostic it can act on
   instead.
4. **Privacy screen.** Every file, and the commit message the outbox will
   write, is checked for:
   - email addresses and telephone numbers;
   - the owner's name and the display names of People records and known
     contacts (multi-token names, or single tokens of four or more
     characters, matched case-insensitively on word boundaries). The owner's
     name is the calling configuration's `public_name` when calling is
     configured. People contribute every person's display name and every
     `name` identifier, in any state;
   - every current private settings string value of four or more characters,
     across all of the principal's widgets.

   A hit rejects the revision, and the diagnostic names the file and kind
   but never echoes the matched value.
5. **Typecheck and bundle.** `tsc --noEmit` runs against the kit's types, then
   `esbuild` produces the IIFE bundle, minified and content-hashed.
6. **Render.** Each fixture renders through `react-dom/server` at every
   declared size. A throw, an empty render or an output over the size limit
   rejects the revision.
7. **Preview** (check tool only, not on the commit path). Playwright WebKit
   renders the fixtures through the real host and realm, and the result is a
   PNG returned as an image reference part.

Rejection records the revision as `rejected` with diagnostics and nothing
else. No outbox entry, commit or event is created.

The builder runs in the **builder image**, `execution/cockpit-builder.Dockerfile`:

- **Base.** It is built from a digest-pinned Debian Node image, not the
  sandbox's Alpine base, because the preview renderer's Playwright WebKit
  needs glibc. It installs `python` for the execution service's workspace
  operations, the locked `cockpit/` runtime dependencies (including esbuild's
  Linux binary), and the kit built at image build time. Playwright WebKit
  joins it with the preview renderer.
- **Isolation.** It runs through the existing execution service under gVisor,
  with:
  - networking off;
  - a read-only root and a no-exec `/tmp`;
  - the standard limits;
  - an empty environment and no credentials.
- **Transport.** The package and the privacy screen travel on the command's
  stdin, and the report, bundle and manifest come back on stdout. Nothing is
  written to the workspace, so a hostile file name cannot overwrite the
  screen, and the builder judges the package's raw file names.
- **Configuration.** It has its own `AGENT_COCKPIT_BUILDER_IMAGE` setting and
  `SandboxManager`, constructed only when `AGENT_COCKPIT_ENABLED` is on. The
  execution service admits only the images its unit names with
  `--allow-image`, resolved on each admission, so a release that moves a
  `:production` tag needs no restart.
- **Leases.** Every build provisions a fresh sandbox, also inside a run, and
  releases it when the build returns or is cancelled. Nothing carries from one
  build to the next or into the run's own sandbox. The sandbox is labelled
  `purpose=cockpit_build`, and that label with its wall-clock deadline is the
  build's own lease. The reaper leaves a labelled sandbox alone until the
  deadline and removes it after, so a build that the importer or release
  preflight starts outside any run survives the reaper without a lease row.
- **Verdicts.** A rejection is a verdict with at least one diagnostic. The
  adapter raises instead of returning a verdict when the build:
  - timed out or exceeded a limit;
  - exited with a usage error;
  - printed something other than one report for the package;
  - returned an exit code that disagrees with its report;
  - returned a bundle that does not hash to its digest or exceeds the cap.

  Diagnostic fields are clamped to the domain's lengths.
- **Release preflight.** It builds the image, then builds the reference
  `weather` widget under runsc with the sandbox's flags as a canary. It also
  rebuilds every live widget against the release's kit. A widget whose
  rebuild fails stays on its previous `kit_api` runtime, the failure is
  reported, and nothing half-migrates.

## The repository branch and the commit outbox

### The branch

`cockpit/widgets` is an orphan branch of `avitus/veetbot`. It holds only
`cockpit/widgets/README.md` and the widget directories.

The owner creates it once at activation, or the adapter creates it with a
root commit if it is absent and the App is allowed to.

Repository rulesets enforce three things:

- people are exempt; the App may not write any other branch or any tag;
- nobody may force-push or delete `cockpit/widgets`;
- the App's installation is limited to this repository with `contents:write`.

No CircleCI workflow runs on the branch, and no CodeRabbit review applies to
it.

### The credential

A GitHub App's private key is delivered through systemd `LoadCredential` to
the **maintenance** role only. The adapter mints short-lived installation
tokens with the existing `cryptography` dependency and calls the REST API
through the existing HTTP client. The execution service, the builder image,
the API process and every sandbox stay credential-free.

The maintenance unit loads the key as `cockpit-github-app` and names it in
`AGENT_COCKPIT_GITHUB_KEY_FILE`. A whitespace-only `SetCredential` default
(`\n`; systemd rejects an empty value) keeps the unit starting before the
owner installs the key. With the flag on, the
maintenance role refuses to build its worker until the repository, App id,
installation id and a private, non-empty key are all present. The adapter only
ever touches a branch under `cockpit/`.

### Writing

The outbox worker in the maintenance role takes entries in order per widget.
For each one it:

1. reads the branch head;
2. creates blobs, a tree based on the head's tree that changes only
   `cockpit/widgets/<slug>/`, and a commit whose parent is the head;
3. updates the ref without `force`.

The adapter refuses three things before calling GitHub:

- any path outside `cockpit/widgets/<slug>/`;
- any ref other than the configured branch;
- any non-fast-forward update.

Commit messages are a fixed template:

```text
cockpit: <create|update|delete|restore|revert> <slug>

Cockpit-Widget: <slug>
Veetbot-Revision: <revision uuid>
```

When the commit is recorded, the revision becomes `committed`. The
application then makes it `live` and supersedes the previous live revision in
one transaction, and emits `widget.revised` or `widget.deleted`.

Acceptance and the outbox derive the commit message the same way, so the
message the screen checked is the message committed. The verb depends on the
change:

- `create`, when no live commit is being changed;
- `update`, when one is;
- `restore`, for a deleted widget's last live package;
- `revert`, for an earlier revision of a live widget.

Restore and revert revisions have source `revert`. A delete carries a fresh
operation id in its `Veetbot-Revision` trailer, never a revision id, so finding
it on the branch cannot mistake the widget's original commit for the removal.

An update names the widget's newest accepted revision as `expected_revision`:
the one waiting for its commit, or else the live one. An accepted change is
therefore never silently overwritten before it lands.

The rate limits and the importer's poll interval are the knobs of
`cockpit/profiles.yaml` so far; the other knobs join it with the steps that
read them.

**Commit before activate** holds without exception. During a GitHub outage
the entry retries with backoff and the previous live revision keeps rendering
from PostgreSQL. Any other failure of one entry is handled the same way: the
entry is logged and rescheduled with backoff, later entries for its widget wait
behind it, and the other widgets' entries in the same pass still commit.

### Ambiguity and conflict

- **Non-fast-forward.** If the head moved, the worker re-reads it and retries
  the same tree change on the new head.
- **A human changed the same widget.** If the new commits since the expected
  parent touched the same widget directory, the person's commit wins. The
  revision becomes `conflicted` and the importer takes the person's version.
  The expected parent is the commit of the live revision the change was based
  on. For a new widget it is the importer's cursor, after which every commit
  is unimported. A person's version that the importer made live after the
  change was queued also wins. A commit counts as the platform's only when its
  trailer names one of the principal's own revisions or a commit the outbox
  recorded, so a forged trailer does not hide a person's change.
- **Lost response.** If the ref update's response is lost, the worker reads
  the head and its ancestry for the revision's `Veetbot-Revision` trailer
  before retrying, so a retry never commits twice. The write tool is
  therefore `CONDITIONALLY_IDEMPOTENT`.

### Rate limits

At most twelve commits per widget per hour and sixty per principal per hour
(knobs). A rejected revision does not count.

### Importing human commits

The importer polls the branch head (knob, default sixty seconds). For each
commit that does not carry a `Veetbot-Revision` trailer this deployment
issued, it reads the changed widget directories and runs the builder on each.
Each commit's effects and its import record land in one transaction, and the
importer resumes after the last recorded commit, so a commit is never imported
twice. The importer screens a person's files but not their commit message,
which is theirs and already public.

- A valid package becomes a `owner_git` revision, then `committed`, then
  `live`.
- An invalid package records an import failure, surfaces it in Cockpit and in
  the widget's history, and leaves the live revision rendering.
- A deleted directory deletes the widget.
- Paths outside `cockpit/widgets/` record an `out_of_scope` import failure and
  are otherwise ignored.

### Delete, restore and revert

- **Delete.** Commits the directory's removal, then removes the widget from
  every layout and marks it `deleted`. Private settings remain until the purge
  window (knob) passes.
- **Restore.** Commits the last live package again, as a new commit, and
  reactivates it.
- **Revert.** Selecting an earlier revision creates a new `revert` revision
  with that revision's files. It passes the whole pipeline again, including
  the privacy screen against current settings.

## Feeds

### Platform feeds

The maintenance role projects these on a short timer
(`feeds.projection_interval_seconds`, default thirty seconds). Each is present
only when its source feature is enabled.

- **When it publishes.** A feed is published when its content changes. Its
  unchanged content is republished once `feeds.refresh_seconds` (default five
  minutes) has passed, and that period is the feed's expected interval. A quiet
  feed so stays honestly live, and a stopped projector shows as stale.
- **Times are absolute.** Every time is an ISO 8601 instant, never an age, so
  unchanged state projects to unchanged content. Widgets compute ages with
  the kit's clock.
- **Failures.** A source that fails publishes one error snapshot, worded by
  the platform ("The platform could not read this source."), and the others
  still publish. The reason is logged, never stored in the feed.
- **What each reads.** Each source reads as the owner, through the same
  repositories its routes read, and leaves out another principal's rows. The
  pending approvals feed also drops expired requests. The calls feed drops
  erased calls and those past the thirty-day retention. The people feed reads
  at the sensitive ceiling and drops names the people service would not show.

| Feed | Source | Data |
| --- | --- | --- |
| `veetbot.approvals.pending` | approvals, always | `count`; `oldestAt`; up to ten items, oldest first: `kind` (a platform label), `summary` (the server-authored action summary), `requestedAt`, `expiresAt`, `risk` |
| `veetbot.email.priority` | Email mode | `count`; up to five threads ranked as the priority view ranks them: `correspondent`, `subject`, `state` (`needs_reply`, `drafted` or `fyi`), `updatedAt` |
| `veetbot.schedules.health` | the schedule API | `onTime`, `total`, `failed` and `paused` counts for the last day; up to twelve timeline items (`id`, `time` on the schedule's own clock, `title`, `detail`, `state`, `at`): the day's runs, each active schedule's next run, then paused schedules |
| `veetbot.calls.recent` | calling | up to five newest calls: `direction`, `counterparty`, `status`, `summary`, `startedAt`, `durationMinutes` |
| `veetbot.people.recent` | People | up to five people most recently in touch: `name`, `lastAt`, and the last interaction's `kind` and `channel` |
| `veetbot.runs.activity` | runs, always | `running`, `waiting` and `failed` counts; up to eight conversations whose latest run is running, waiting, or failed in the last day: `title`, `state`, `since` |

The schedule items are the kit's `TimelineItem`, so a widget passes them to
`Timeline` directly. Each schema is closed, and the reference widgets'
fixtures are checked against it.

### Assistant feeds

`cockpit.feed.publish` writes a snapshot to `agent.<slug>`:

- The first publish registers the schema. A later publish validates against
  it, or carries a new schema and so bumps the schema version.
- A widget whose manifest declared the old version renders `error` until it
  is updated.
- Assistant feeds are labelled untrusted. Their usual writer is a scheduled
  run the owner approved, which keeps that schedule's existing approval,
  budget and failure pause. For example, a schedule every fifteen minutes
  reads a weather or market source with the existing web tools and publishes
  the result.
- A feed refreshes as often as its schedule fires (ADR-0254). An `INTERVAL`
  cadence fires every 1 to 1,440 minutes, and only a feed-keeping schedule,
  one that requests `cockpit.write`, may use it. A calendar cadence still
  serves a feed that changes once a day or less.
- A feed-keeping schedule that `schedule.create` makes notifies only for
  attention, because the widget is its report. An interval schedule skips an
  instant its previous run still overlaps without notifying anyone.

### Freshness and delivery

- **Freshness** is computed by the kit at render time, with the approved
  designs' thresholds:
  - `pending` before the first value;
  - `live` while the age is at most 1.5 times the expected interval;
  - `aging` up to three times the interval;
  - `stale` beyond three times;
  - `error` when the last attempt failed or the schema version mismatches.

  Clients compute it with server-corrected time. Stale data is never shown as
  fresh, including after a reconnect.
- **History** keeps the last `history_max` snapshots (knob, default 288) or
  `history_days` (knob, default 7), whichever is smaller. A widget receives at
  most the history depth its manifest declares.
- **Delivery.** A widget receives exactly the feeds its live manifest
  declares. The host never delivers an undeclared feed, another widget's
  settings, or any account or session identifier beyond what a declared feed
  contains.

### Refresh on entry

Each time Cockpit becomes the visible mode of an active app, the client asks
for every feed to be refreshed (ADR-0254). `POST /v1/cockpit/refresh` starts
one run, now, of each active, recurring, feed-keeping schedule:

- **The occurrence.** It is a manual occurrence keyed
  `cockpit.refresh:<UTC minute>`, so a repeated request in the same minute
  starts nothing new. Refresh runs do not count toward Run-now's five a day.
- **Skips.** A schedule whose latest run is still going reports `running`. One
  whose latest run started under a minute ago reports `recent`.
- **Checks.** Authority, scopes, the pinned agent and tenant admission are
  checked as for Run-now. A failed check reports `refused` with its reason
  for that schedule, and the others still start.
- **Notifications.** A completed refresh run sends no report notification. A
  failed one notifies as any scheduled run does.

Platform feeds are not re-projected, because the projector already publishes
their changes every thirty seconds. The answer is `202` with one
`{schedule_id, outcome, reason}` per feed-keeping schedule, where `outcome`
is `started`, `running`, `recent` or `refused`.

## The tools

The `cockpit` builtin domain registers only under `AGENT_COCKPIT_ENABLED`.
Its five tools are **deferred** index entries in the default agent's
configuration, so the thirty-item definition roster is untouched.

| Tool | Operations | Side effect | Default decision |
| --- | --- | --- | --- |
| `cockpit.read` | describe the kit, list widgets, read a widget (state, settings schema, file sizes, recent revisions, import failures), page a widget's file, list and read feeds, get layouts | `none` | allow |
| `cockpit.check` | validate a package; preview its fixtures as a PNG | `none`, built in the builder's own isolated sandbox | allow |
| `cockpit.widget.write` | create, update (`expected_revision`), delete, restore | `publication` | allow by exact-name `tool_rules` entry |
| `cockpit.configure` | set a widget's private settings; set a device class's layout | `none` | allow |
| `cockpit.feed.publish` | publish a snapshot (and schema) to `agent.<slug>` | `none` | allow |

**`cockpit.check` is classified `none`, not `code_execution`.** Its build
runs in the builder's own offline sandbox, exactly as a write's does, and it
runs no command the model chose. A `sandbox` target would label its verdict
`external_untrusted`, which taints the turn. The verdict is the one
`cockpit.widget.write` returns with internal trust, so that label would force
every check-then-write into an approval for content the model wrote itself.

**Every answer fits the inline budget.** A tool result reaches the model
whole only up to `tools/limits.yaml`'s `output.inline_maximum_bytes` (4 KiB),
so `cockpit.read` answers in pieces:

- `describe_kit` gives the rules and an index of the kit's primitives, then
  up to four primitives' props and examples by name, or the helpers or the
  shared types;
- `read_widget` gives file sizes rather than files;
- `read_file` pages one file of the head revision from a line, and a line
  longer than a page from a byte offset on a character boundary;
- `read_feed` returns at most ten earlier snapshots.

Builder diagnostics are cut to the first ten, each at most 300 characters.

**`cockpit.widget.write`** has several further rules:

- **Default profile entry.** The tool gets a commented `tool_rules` entry in
  `policy/default.yaml`, following the `device.sms.send` and `browser.act`
  precedent. `human_confirms_arguments` is not set.
- **Trust overlay.** The overlay still escalates a turn whose origin is not
  the user, the platform or trusted configuration, and any untrusted
  argument. So a widget written after reading an email asks first.
- **Hardline.** The credential-to-egress hardline rule applies to its
  arguments.
- **Policy version.** The profile edit moves the content-addressed policy
  version. Pending approvals revalidate under the existing rule, and the step
  that makes the edit updates the pinned shipped version and names any
  version-bound evidence it affects. The owner placed the edit in activation
  on 2026-10-24. Provider-assisted formation's release evidence is bound to
  the compiled policy version, so it is regenerated once there, on the new
  version, rather than at each profile change before then.
- **Where it is offered.** Session-environment filtering withholds the tool
  from scheduled, delegated, operational, email-task and device-triage
  sessions. Widget writes happen in interactive chat with the owner.
- **Waiting and results.** The tool waits up to `commit_wait_seconds` (knob,
  default thirty) for the outbox. It returns the final state when the outbox
  finishes in time, and `pending_commit` with the revision otherwise. A
  rejected package returns its diagnostics.
- **Replay.** The tool is conditionally idempotent. The accepted revision, or
  the queued removal, records the call's idempotency key in the same
  transaction, so a replay returns the first outcome and changes nothing.
- **Refused by name too.** Withholding the tool from a session's plan does
  not stop a call by name. So the tool itself refuses any run that is not
  interactive and any session that carries a schedule, a run kind, an
  operational email marker or a purpose.
- **Approval view.** When an escalation produces an approval, the approval
  view shows the file diff and the preview image.

**`cockpit.read`** returns revision content with output trust derived from
provenance:

- assistant-written and owner-git revisions are labelled as stored owner
  content (`internal_tool`), which leaves the turn's origin as it was;
- feed data is `external_untrusted`. An assistant feed holds what a run read.
  A platform feed's rendering trust is `platform`, but its projections quote
  mail, callers and people, so reading one is untrusted too.

Reading a widget before updating it therefore does not force every later turn
into approval.

**Feed publishing** is available in scheduled runs. A Cockpit deployment
registers `schedule.create` 1.6.0, whose `publish_cockpit_feed=true` asks for
`cockpit.read` and `cockpit.write` for that schedule's runs and no others, and
which offers the `INTERVAL` cadence to such a schedule (ADR-0254). It also
registers `schedule.update` 1.1.0, which can move a feed schedule to an
interval. Versions 1.5.0 and 1.4.0 stay registered for pinned runs, and a
deployment without the flag offers 1.4.0 unchanged. Widget writes stay
interactive only.

**Feed checks.** A feed's schema must be self-contained JSON Schema (draft
2020-12) that names no location outside itself, and each of its references
must resolve inside it, so checking data never retrieves anything and never
fails on a dangling reference. Data is checked against it unless the snapshot reports a
failed refresh. An observation more than a minute ahead of the server's clock
is refused. Each publish keeps at most `feeds.history_max` snapshots (knob,
default 288, the deepest history a manifest may declare) and appends one
`feed.updated` stream event.

## The routes and the stream

Every route is absent unless `AGENT_COCKPIT_ENABLED=1`. Each requires the
scope named below. Each answers with `Cache-Control: private, no-store`,
except the immutable asset route. A foreign or unknown widget, revision or
feed is an indistinguishable 404.

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /v1/cockpit` | `cockpit.read` | Snapshot: runtime asset hashes, live widgets with revision, manifest, bundle hash and when the revision changed, settings, layouts, current feed snapshots with declared history, the stream cursor, and the server's clock |
| `GET /v1/cockpit/stream` | `cockpit.read` | Server-sent events |
| `GET /v1/cockpit/assets/{sha256}` | `cockpit.read` | Runtime and bundle assets by hash; `immutable`, long-lived private caching |
| `GET /v1/cockpit/widgets/{slug}` | `cockpit.read` | Detail: revisions with commit links, import failures, settings schema |
| `PUT /v1/cockpit/widgets/{slug}/settings` | `cockpit.write` | Owner edits private settings |
| `PUT /v1/cockpit/layouts/{device_class}` | `cockpit.write` | Owner arranges a board |
| `POST /v1/cockpit/widgets/{slug}/revert` | `cockpit.write` | Owner reverts to a named revision |
| `POST /v1/cockpit/widgets/{slug}/delete` | `cockpit.write` | Owner deletes |
| `POST /v1/cockpit/widgets/{slug}/restore` | `cockpit.write` | Owner restores |
| `POST /v1/cockpit/refresh` | `cockpit.write` | Refresh every assistant feed now ([refresh on entry](#refresh-on-entry)) |

The owner's own write routes go through the same outbox and pipeline as the
tool. They need no approval, because the owner's gesture is the decision.
Revert, delete and restore answer `202` with the change: a revert or restore
carries its revision's state and diagnostics, and a delete is `queued`.

The routes' other contracts:

- **Errors.** Settings that fail the live manifest's settings schema, and a
  layout that places a widget that is not live or at a size its manifest does
  not offer, are `malformed_request` (400). A build that reached no verdict
  is `sandbox_execution_error` (502), and nothing is recorded.
- **Runtime assets.** The snapshot's runtime and the asset route read the
  manifest a release exports to `artifact_root/cockpit-runtime/`. The release
  copies the runtime out of the builder image there, never rewriting an asset
  already present, and switches the manifest at promotion; a rollback switches
  it back. `make cockpit-runtime` does the same for a local server. Until a
  runtime is exported, `runtime` is null.
- **Clocks.** The snapshot carries `server_time`, so a client renders
  freshness with server-corrected time, and each widget's `changed_at`, so the
  watchdog quarantines the most recently changed widget first.
- **The wire contract.** `tests/fixtures/api/cockpit_snapshot_contract.json`
  is the route's own rendering of one board. The Apple client decodes a
  checksum-pinned copy of it, so a change to the shape fails both sides.
- **Asset caching.** An asset answers
  `Cache-Control: private, max-age=31536000, immutable` and
  `X-Content-Type-Options: nosniff`.

**The stream** follows the run stream's pattern:

- Every check happens before the first byte.
- There is a fifteen-second heartbeat and proxy buffering is disabled.
- The client resumes from `Last-Event-ID`.

The stream's own rules:

- **Channel.** The LISTEN/NOTIFY channel is
  `cockpit_<first 32 hex of sha256(tenant)>`, which keeps within the
  identifier limit. NOTIFY carries only `{kind, sequence}`. Readers fetch
  rows from `cockpit_events`, and keep polling at the existing interval when a
  notification is missed.
- **Event kinds.** `feed.updated`, `widget.revised`, `widget.deleted`,
  `layout.updated`, `settings.updated`, `import.failed` and `stream.reset`.
- **Cursor.** Without a resume point the stream starts after the principal's
  latest event. It takes that cursor before it subscribes to the channel, so
  an event that commits while it subscribes is replayed, not lost.
- **Reset.** A resume point older than the retention window gets
  `stream.reset`, and the client refetches the snapshot. Sequences are shared
  across tenants, so one principal's have gaps. A resume point is always one
  of the principal's own sequences, though, and retention removes the oldest
  events first. So a nonzero resume point below the principal's oldest
  retained event, or with none retained, gets the reset. The check is made in
  the first replay page's unit of work, after that page is fetched, so a
  retention sweep between a client's connect and its first page costs at most
  a refetch and never a silently shortened replay.
- **Retention.** Events are kept for `stream.event_retention_seconds` (knob,
  default one day). The maintenance role prunes them on the importer's timer.
- **Start.** A stream without `Last-Event-ID` starts after the latest event;
  a client opens it with the snapshot's `cursor`.
- **Frames.** Each event's frame has the sequence as its id, the kind as its
  event and `{"subject": …}` as its data, naming the widget, feed or device
  class that changed.
- **Lifetime.** A stream lives at most `stream.max_seconds` (knob, default
  600) before a clean close, so token revocation takes effect on reconnect.

`cockpit.read` and `cockpit.write` join `PLATFORM_SCOPES`, the deployment
scope classification, and `AUTH_SCOPES` in `deploy/veetbot.env.example`. The
schedule principal gains `cockpit.write` with the feed publishing tool.

## The native experience

### The mode shell

`ClientMode` moves into its own file and gains `cockpit`, whose symbol is
`gauge.with.needle`. The owner set the order on 2026-10-22: the mode switch
reads Cockpit · Chat · Email and is the first thing at the top of each mode's
left pane, so Email's segment moves above its account header. A pure refactor first replaces every two-way assumption
with an exhaustive switch. These are the places that assume two modes:

- the mode title and symbol;
- the compact top bar and the More menu;
- the per-mode opacity and hit-testing blocks;
- the Mac toolbar;
- the mailbox sidebar's mode segment, which today silently drops any third
  mode;
- Chat's literal fallback title;
- the inspector's width and minimum window width table;
- the inspector panel's accessibility label;
- the coordinator's notification routing.

The existing source-text tests stay green. UI identifiers gain `mode.cockpit`.

### `CockpitModeView`

**Native chrome:**

- connection and freshness state;
- the last update time;
- an edit mode for reordering and resizing tiles and hiding them per device
  class;
- an unavailable state for a server without the routes.

**`CockpitWebView`** holds the one web view:

- a non-persistent website data store;
- a `WKURLSchemeHandler` for `veetbot-cockpit://` that resolves only the
  content hashes named by the current snapshot to the asset route, fetches
  them natively with the token, caches them by hash, and answers 404 for
  everything else;
- a `WKContentRuleList` that blocks every `http` and `https` load;
- a document-start `WKUserScript` in every frame that removes
  `RTCPeerConnection`, `WebSocket`, `EventSource` and `navigator.sendBeacon`;
- a pure, testable navigation policy, after `DeviceSignInNavigationPolicy`,
  that permits only the host document;
- one script message handler that accepts messages only from the main frame,
  and only `ready`, `tile_error` and two intents.

Native code pushes data with `callAsyncJavaScript` arguments, never string
interpolation.

**Intents** are navigation only. Neither has a side effect.

- `open` names a destination kind (approval, email thread, schedule, session)
  and an identifier present in a delivered feed, and native code validates
  both before navigating. A widget raises it from a kit `Row`'s `opens`
  target or the `useOpen` hook; the realm sends it with the tile's key, the
  host forwards only a known kind with a bounded identifier for a placed
  widget, and native code opens it only when that widget's own declared feeds
  carry the identifier as a value. The platform feeds therefore carry the
  identifiers to open: each approval's `id` and `sessionId`, each priority
  thread's `threadId`, each schedule item's `scheduleId` and each run's
  `sessionId`.
- `discuss` opens Chat with the widget's context attached as untrusted
  context, following the Email "Discuss in Chat" precedent. The owner writes
  and sends the message.

**`CockpitViewModel`:**

- loads the snapshot, then streams over the path-based `SSEReader`;
- reconnects with capped exponential backoff and no give-up limit, because
  the run stream's limits suit a run, not an always-on display;
- holds the stream only while Cockpit is the visible mode and the app is
  active;
- each time Cockpit becomes visible, asks the server to refresh every feed
  and reloads the board (ADR-0254). A refresh that fails leaves the board as
  it is;
- applies events in sequence order and refetches on `stream.reset`.

### Per platform

- **Mac**
  - Supports full screen.
  - The Cockpit right panel follows the ADR-0237 pattern. It shows the
    selected widget's purpose, feeds and their freshness, a settings form
    generated from the settings schema, revision history with commit links,
    import failures, and Revert, Delete and Restore.
- **iPad**
  - Uses the Mac layout at touch sizes, with a keep-screen-on toggle that
    sets the idle timer only while Cockpit is visible.
  - Widget details open as a sheet.
- **iPhone**
  - Shows a compact single column in `compact` layout order.
  - Widget details open as a bottom sheet.

The mode keeps its board, scroll position and selected widget across mode
switches, as the other modes do.

## The visual design

The visual layer is designed in MagicPath before the kit is ported. All
designs live in the Veetbot project beside the A2 designs and extend A2's
language: warm paper, one teal accent, and amber only for attention. They use
tokens derived from `AppTheme.swift`.

1. **`Design system · Cockpit A2`.** Covers:
   - glanceable density and tile anatomy;
   - the type scale for numbers read at a distance;
   - freshness, stale and error treatments;
   - the amber needs-attention rule;
   - the night treatment.
2. **`Cockpit · Kit primitives`.** An interactive gallery of every primitive
   in every state, in light, dark and night, written as prop-driven
   components that port one to one.
3. **Three boards.** `Mac — Cockpit · A2`, `iPad — Cockpit · A2` and
   `iPhone — Cockpit · A2`, inside the native chrome and the three-way mode
   switch. They show the seed widgets with synthetic data and an edit state.

The seed widgets are:

- approvals waiting;
- priority email;
- schedule health;
- recent calls;
- people recently in touch;
- weather;
- markets.

The owner approved the kit and the three boards on the canvas on 2026-10-22,
which closes that step. The approved designs fix these choices:

- Rising and falling values are never coloured: red means broken and amber
  means waiting on the owner.
- A stale tile sinks to a darker paper with a dashed edge and faint figures,
  and states its last update and expected interval.
- Lists fold the rows that do not fit into "+N more".
- Night watch is a third theme, darker than A2 dark, with amber kept at full
  strength.

The assistant authors the production versions of these seeds itself at
activation, as the milestone's first real use.

## Events, telemetry, and privacy

The platform records these on the ordinary event log:

- widget revision transitions;
- outbox attempts and their outcomes;
- importer passes;
- feed publishes;
- route writes.

Payloads carry identifiers, states, sizes, hashes and diagnostics codes, but
never package content, settings values or feed data.

The privacy screen's diagnostic names the rule and the file, never the
match.

Commit messages carry no free text.

Feed data, settings and layouts are private PostgreSQL state:

- they appear in no prompt unless a tool returns them;
- they are covered by the existing source-erasure and retention rules;
- deleting a People record makes any later commit that would contain that
  person's name fail the screen.

Public history is permanent. The design's protection is that personal values
never reach a commit, not that they can be removed afterwards.

## Migration, configuration, and operations

### Migration

One additive migration creates these tables:

- `cockpit_widgets` and `cockpit_widget_revisions`;
- `cockpit_widget_settings`;
- `cockpit_feeds` and `cockpit_feed_snapshots`;
- `cockpit_layouts`;
- `cockpit_events`;
- `cockpit_commit_outbox`;
- `cockpit_import_failures`.

Each has tenant and principal predicates, and the migration downgrades
cleanly. Each store has an in-memory and a PostgreSQL adapter under one
contract suite.

### Configuration

- **Flag.** `AGENT_COCKPIT_ENABLED`, exactly `0` or `1`, default `0`.
- **Settings.** The settings are:
  - `AGENT_COCKPIT_BUILDER_IMAGE`;
  - `AGENT_COCKPIT_GITHUB_REPOSITORY`;
  - `AGENT_COCKPIT_GITHUB_BRANCH`, default `cockpit/widgets`;
  - `AGENT_COCKPIT_GITHUB_APP_ID`;
  - `AGENT_COCKPIT_GITHUB_INSTALLATION_ID`;
  - `AGENT_COCKPIT_GITHUB_KEY_FILE`, which the maintenance unit points at its
    `cockpit-github-app` credential.

  The maintenance role refuses to start the outbox or the importer without
  the credential.
- **Tuning document.** The shipped configuration root gains
  `cockpit/profiles.yaml`, beside `folders/profiles.yaml`. This is distinct
  from the repository's `cockpit/` workspace. Its knobs:
  - history limits;
  - projection interval;
  - poll interval;
  - commit wait;
  - rate limits;
  - watchdog deadline;
  - stream lifetime;
  - event retention;
  - settings purge window.

  These knobs join the shipped knob census as each build step reads them.
  Ten are in it now: the two commit rate limits, the import poll interval and
  the commit wait under `publication`; the stream lifetime and event retention
  under `stream`; and `feeds.history_max`, `feeds.history_days`,
  `feeds.projection_interval_seconds` and `feeds.refresh_seconds`. Feed
  history older than `history_days` is pruned on the import timer.

### Release

The release builds the builder image and runs the runsc canary and the live
widget rebuild in preflight. It exports the runtime assets, then checks that
the image is present in the deployment check.

### Operations

`docs/deployment.md` gains the App, the rulesets, the credential, the flag,
and a runbook covering:

- a stuck outbox;
- a rejected import;
- a quarantined widget;
- rotating the App key.

## Safety

| Threat | Control |
| --- | --- |
| Widget code exfiltrates data | No network in the realm: CSP, a content rule list, removed primitives, no same-origin, a scheme handler that serves only named hashes and no API |
| Widget code reaches native code or the token | The token never enters web content; message handler accepts main-frame messages only; intents validated natively and side-effect free |
| A hung or crashing widget blanks the board | Per-widget error boundaries, watchdog reload and quarantine |
| Personal data published to the public branch | Data-free packages, private settings and feeds, synthetic fixtures, the privacy screen, fixed commit messages |
| Prompt injection writes a widget | Trust overlay escalates untrusted origins and arguments; writes only in interactive owner chat; never in scheduled or delegated runs |
| Malicious or broken human commit on the branch | The importer runs the same pipeline; invalid commits never replace a live revision; rulesets restrict who can write |
| The App credential is misused | Maintenance role only; short-lived installation tokens; one repository; one branch by ruleset and by adapter refusal |
| A kit release breaks live widgets | Preflight rebuild; prior `kit_api` runtime retained |
| Stale data read as current | Freshness computed at render with corrected time; stale treatment in every primitive |
| A rendered feed string impersonates an instruction | Feed strings render as text; `discuss` context enters Chat as untrusted |

## Hard gates

1. **The schema is additive and both stores agree.** The migration creates
   the widget, revision, settings, feed, snapshot, layout, event, outbox and
   import-failure tables with tenant and principal predicates, downgrades
   cleanly, and every Cockpit store passes one contract suite on both
   adapters. Registered as `gate.cockpit.schema_additive`, case. **M35.**
2. **The builder rejects what it must.** A package that breaks the manifest
   schema, the file allowlist, a size cap, the import allowlist, a banned
   reference, the credential pattern, the fixture requirements or the render
   check is `rejected` with diagnostics, and no outbox entry, commit or event
   exists for it. Registered as `gate.cockpit.builder_rejects_forbidden`,
   case. **M35.**
3. **Builds are offline and leased.** The builder image runs under gVisor with
   networking off, a read-only root and no credentials; the release canary
   builds under runsc; and a build started outside a run holds its own lease
   and survives the reaper. Registered as
   `gate.cockpit.builder_offline_under_runsc`, case. **M35.**
4. **The privacy screen blocks personal data.** Any email address, telephone
   number, owner or People display name, or current private settings value in
   any package file or the commit message rejects the revision, and the
   diagnostic never repeats the value. Registered as
   `gate.cockpit.privacy_screen_blocks_commit`, case. **M35.**
5. **Commits stay in scope and never force.** The adapter writes only under
   `cockpit/widgets/<slug>/` on the configured branch as a fast-forward, and
   refuses any other path, ref or non-fast-forward update before calling
   GitHub. Registered as `gate.cockpit.commit_scope_and_no_force`, case.
   **M35.**
6. **Nothing activates before its commit.** A revision becomes live only after
   its commit sha is recorded; while GitHub is unavailable the outbox retries
   and the previous live revision keeps rendering. Registered as
   `gate.cockpit.commit_before_activate`, case. **M35.**
7. **An ambiguous commit reconciles once.** After a lost ref-update response
   the worker finds the revision's trailer on the branch rather than
   committing again, and a concurrent human commit to the same widget wins
   while the assistant's revision becomes `conflicted`. Registered as
   `gate.cockpit.ambiguous_commit_reconciles`, case. **M35.**
8. **An invalid import keeps the live widget.** The importer activates a valid
   human commit as an `owner_git` revision, and records and surfaces an
   invalid or out-of-scope one without replacing the live revision.
   Registered as `gate.cockpit.invalid_import_keeps_live`, case. **M35.**
9. **Delete and restore round-trip.** Delete commits the directory's removal
   and removes the widget from every layout; restore commits the package
   again and reactivates it; revert is a new revision through the whole
   pipeline. Registered as `gate.cockpit.delete_and_restore`, case. **M35.**
10. **Untrusted turns escalate and other runs cannot write.** The write tool
    is allowed for owner-origin turns, escalates to approval for an untrusted
    origin or argument, remains subject to the credential-to-egress hardline
    rule, and is not offered in scheduled, delegated, operational,
    email-task or device-triage sessions. Registered as
    `gate.cockpit.untrusted_origin_escalates`, case. **M35.**
11. **A widget sees only what it declares.** The host delivers a widget only
    its manifest's feeds at no more than the declared history, only its own
    settings, and never another widget's data. Registered as
    `gate.cockpit.declared_feeds_only`, case. **M35.**
12. **Feed text is inert.** Feed and settings strings render as text in every
    kit primitive, with no markup, link or URL affordance, and `discuss`
    context enters Chat as untrusted. Registered as
    `gate.cockpit.feed_text_inert`, case. **M35.**
13. **Stale renders stale.** For any snapshot, interval and corrected
    clock, a feed older than three times its expected interval is `stale`
    and dims every primitive in its tile, including after reconnect and
    snapshot refetch. Registered as `gate.cockpit.stale_renders_stale`, property.
    **M35.**
14. **The stream resumes and resets.** The stream resumes from
    `Last-Event-ID` without loss or duplication, sends `stream.reset` past
    retention, heartbeats, closes at its lifetime, and never crosses tenants.
    Registered as `gate.cockpit.stream_resume_and_reset`, case. **M35.**
15. **Off means absent.** With `AGENT_COCKPIT_ENABLED=0` no Cockpit route,
    tool, builder manager, outbox, importer or projector exists, and the
    native client shows Cockpit as unavailable. Registered as
    `gate.cockpit.flag_off_absent`, structural. **M35.**
16. **The realm has no network.** Every member of the adversarial corpus —
    fetch, XHR, WebSocket, EventSource, beacon, peer connection, image,
    script, style, font and frame loads, evaluation, workers, popups, forms
    and navigation — fails in headless Chromium, in headless WebKit where it
    is installed, and in a hosted `WKWebView`; a navigating widget is
    quarantined. Registered as
    `gate.cockpit.realm_network_blocked`, case. **M35.**
17. **The token never enters web content.** No document or frame can read the
    bearer token or any credential, and the scheme handler's own requests
    carry none to the web content. Registered as
    `gate.cockpit.token_absent_from_web_content`, case. **M35.**
18. **The scheme handler serves only named assets.** It answers only content
    hashes named by the current snapshot, never proxies an API route, answers
    404 otherwise, and the navigation policy admits only the host document.
    Registered as `gate.cockpit.scheme_handler_allowlist`, case. **M35.**
19. **A kit upgrade rebuilds live widgets.** Release preflight rebuilds every
    live widget against a changed kit; one that fails keeps its prior runtime,
    which stays served while referenced. Registered as
    `gate.cockpit.kit_upgrade_rebuilds`, case. **M35.**
20. **Three modes keep their state.** Switching among Chat, Email and Cockpit
    preserves each mode's state, and every mode-dependent surface handles all
    three. Registered as `gate.cockpit.three_mode_state_parity`, case.
    **M35.**
21. **The stream runs only while Cockpit is visible.** The client opens the
    stream when Cockpit becomes the visible mode in an active app and closes
    it otherwise. Registered as `gate.cockpit.streams_only_while_visible`,
    case. **M35.**
22. **The realm fits its budget.** The seed board on the oldest supported iPad
    stays within the memory and first-paint budgets, and the watchdog
    quarantines a widget that hangs the realm. Registered as
    `gate.cockpit.realm_memory_budget`, case. **M35.**
23. **The release is evidenced.** Production runs with the flag on, the
    assistant authors a seed widget whose live revision's commit sha is on
    `cockpit/widgets`, and an audit of the public branch finds no personal
    data. Registered as `gate.cockpit.release_evidence`, case. **M35.**

## Tracked metrics

- The time from a tool call to a live revision, at the median and the 95th
  percentile.
- The share of the assistant's packages rejected, by rule.
- Privacy screen hits.
- Outbox retries and the age of the oldest pending entry.
- Import failures.
- Realm reloads and quarantines.
- Stream reconnects per hour.
- Feed staleness per feed.

## Build sequence

1. **MagicPath designs.** The design system, the kit gallery and the three
   boards. The owner's canvas review is the exit.
2. **The `cockpit/` workspace.** Schemas, the builder CLI, the host and
   realm, and the adversarial corpus, then the kit primitives ported from the
   approved designs, plus `make test-cockpit` in `make check` and CI. Gates
   2, 12, 13 and 16 (headless half).
3. **The builder image.** Release wiring, the runsc canary, the second
   manager, the image allowlist and reaper leases. Gate 3.
4. **Domain types, ports, migration, both store adapters under contract
   suites, and a fake git adapter.** Gate 1.
5. **The GitHub App adapter, the outbox, reconciliation, the importer and the
   privacy screen.** Gates 4 through 9.
6. **The routes, the scopes, the flag, the cockpit channel and the stream.**
   Gates 14 and 15.
7. **The tools, session-environment filtering, approval views and
   `kit.describe`, plus an evaluation in which a chat request yields a valid
   committed widget.** Gate 11.
8. **The platform feed projector, and feed publishing from scheduled runs.**
9. **The `ClientMode` refactor, as a pure refactor.**
10. **The native Cockpit mode.** The web view sandbox, the stream, the Mac
    panel and iPad keep-awake. Gates 16 (hosted half) through 22.
11. **Preview rendering.**
12. **Activation.** The owner sets up the App, the rulesets, the credential
    and the flag. The `tool_rules` entry lands with one regeneration of the
    formation release evidence on the new policy version. The assistant
    authors the seed widgets. Gates 10 and 23.

Steps 1, 2 (apart from the kit primitives) and 9 do not depend on one another.

## Decisions

1. **React in a web realm, not SwiftUI.** Agent-written code has to ship
   without an app release, and the MagicPath designs are already React. A
   declarative native format would make widgets configuration rather than
   code.
2. **One realm, not one iframe per widget.** Same-process iframes share a main
   thread, so per-widget frames would not contain a hang. The boundary worth
   paying for is between widget code and the network or native code.
3. **An orphan branch.** The branch then carries only widgets, people check it
   out beside `main`, and widgets always build against the deployed kit.
4. **Commit before activate.** The repository is the source of truth. An
   outage costs latency, never correctness of the live display.
5. **`publication` with an exact-name allow rule.** The classification is
   honest about pushing to a public repository. The allow rule records the
   owner's no-approval decision without weakening the trust overlay or the
   hardline rule.
6. **Deferred tools.** The definition roster's thirty items already bind, and
   Cockpit work is occasional.
7. **Feeds over fetching widgets.** Data acquisition stays in the governed
   tool pipeline and schedules, and widgets stay pure functions of their
   inputs.

## Open questions

1. Should the platform feed projector move from a timer to the triggering
   transactions once the projection cost is measured?
2. The deferred collectors. What egress allowlist, credential and budget model
   should an LLM-free collector have for sub-minute external data?
3. Should a widget be able to request a native share or copy action on its
   own displayed text, given that intents are navigation-only today?

## Implementation checkpoint: 2026-10-24

Build step 7 landed the five tools behind the flag as deferred index entries,
the `cockpit` builtin domain, the session filter, the write tool's refusal
outside interactive chat, and the delegated-child forbid. It also landed the
diff approval view, `describe_kit` over a catalogue generated from the kit's
source, the commit-wait and feed-history knobs, and the evaluation in which a
chat request yields a valid committed widget.

The evaluation runs the `approvals-waiting` reference package through the
real publication path with a manifest-checking builder. A gate test holds the
package identical to the reference widget the workspace's builder tests build.

The exact-name `tool_rules` entry is not in the shipped profile. The owner
deferred it to activation on 2026-10-24, where the formation release evidence
is regenerated once. Until then the publication row asks the owner, the
evaluation approves once, and gate 10 stays pending.

Build step 8 landed the same day:

- the platform feed projector with its six sources, on the maintenance role's
  timer behind the flag;
- feed history pruning on the import timer;
- `schedule.create` 1.5.0's `publish_cockpit_feed` in Cockpit deployments;
- three more knobs.

The two platform-feed reference widgets now read the projected shapes:
absolute times, and the schedule items' `at` and `paused` count.

## Implementation checkpoint: 2026-10-25

Build step 10 landed the native Cockpit mode behind the server's routes:

- the snapshot's `server_time` and `changed_at`, with a pinned wire fixture
  both sides test against;
- the release's runtime export, and its rollback;
- `CockpitWebView`: a non-persistent store, the scheme handler serving only
  the snapshot's hashes, the blocking rule list, the removed network
  primitives, the navigation policy, one main-frame message handler and the
  native watchdog;
- `CockpitViewModel`, which streams only while Cockpit is visible in an active
  app and reconnects without a give-up limit;
- the chrome: connection and last update, night watch, the iPad keep-awake
  toggle, the Mac's widget list and detail panel, the detail sheet elsewhere,
  the settings form, history with commit links, Revert, Delete and Restore,
  and an Arrange sheet that orders, sizes and hides each device class's tiles.

A hosted `WKWebView` test loads the real exported runtime: a widget renders,
and one that tries ten web escapes reaches no listener on the machine. Gates
16, 17, 18 and 21 are bound to native tests that run those suites with the
headless realm suite.

The `open` intent landed with it: approval rows in the reference widget open
the approval's conversation.

Still to come in this step's area:

- the `discuss` intent, which needs Chat to take a widget's context as
  untrusted context;
- gate 20 (state across all three modes) and gate 22 (the realm on the oldest
  supported iPad);
- gate 19's preflight rebuild of live widgets against a changed kit.

## Implementation checkpoint: 2026-10-26

ADR-0254 landed what the owner asked for that day:

- the `INTERVAL` cadence for feed-keeping schedules;
- `schedule.create` 1.6.0, `schedule.update` 1.1.0 and `schedule.list` 1.1.0;
- quiet feed upkeep;
- `POST /v1/cockpit/refresh`;
- the native client's refresh and reload each time Cockpit becomes visible.

The production widget's daily schedule keeps its cadence until Chat or the
owner changes it.

## Implementation checkpoint: 2026-10-23

Build step 3 landed the builder image, its own sandbox manager, the image
allowlist, the build reaper lease and the release's runsc canary. Build step 5
landed the publication path:

- the GitHub App adapter, which passes the repository contract suite against
  an in-memory GitHub remote;
- the commit outbox with lost-response reconciliation and person-wins
  conflicts;
- the branch importer;
- delete, restore and revert;
- the privacy screen's server-supplied names, settings and commit message;
- the maintenance role's credential wiring.

Gates 2 and 4 are bound to sandbox tests that build in the real builder image.
Gates 5 to 9 are bound to the publication path over the GitHub adapter and
the in-memory remote. Gate 3 awaits the first production release's canary.

Build step 6 landed the nine routes behind the flag, the two scopes, the
tenant channel with NOTIFY on the event log, the resumable stream, and event
retention. Gates 14 and 15 are bound. Tools and feeds are the next steps.

## Implementation checkpoint: 2026-10-22

Build step 2 landed the `cockpit/` workspace: the kit ported from the approved
MagicPath designs, the builder, and the host and realm. The adversarial corpus
fails to reach the network in headless Chromium and WebKit. Build step 4
landed the domain types, the five ports, one contract suite per port, the
additive migration with forced tenant isolation and a downgrade guard, and the
in-memory and PostgreSQL adapters plus the fake widget repository. Gate 1 is
bound to the PostgreSQL parity test, which runs every store suite's scenarios
on PostgreSQL. The other gates stay pending until their server and native
halves land.

