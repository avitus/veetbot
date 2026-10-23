# ADR-0240: Cockpit mode and agent-authored widgets

- Status: Accepted — owner authorized the Milestone 35 design on 2026-10-22
- Date: 2026-10-22
- Related: ADR-0008, ADR-0092, ADR-0105, ADR-0123, ADR-0168, ADR-0211, ADR-0237
- Design: [Cockpit mode and agent-authored widgets](../plan/cockpit.md)

## Context

The owner wants a third client mode, **Cockpit**: a heads-up, real-time
display of everything the owner monitors. Its defining requirement is that the
production assistant itself creates, updates and deletes the widgets the
display is made of, and that the widget code lives in this repository. The
owner also chose MagicPath, the canvas the A2 designs already live in, as the
place the widgets' visual layer is designed.

Four facts about the existing system shape the answer:

- Client modes are presentation modules over one assistant (ADR-0092). The
  Apple client hard-codes two of them, and the server advertises none.
- Nothing the assistant writes is executed by a client today. The only web
  view is the device sign-in sheet, and the artifact viewer never executes
  HTML.
- The assistant has no git or GitHub capability. Its sandbox is one
  Python-only image under gVisor with networking off.
- `avitus/veetbot` is a **public** repository, so anything committed to it is
  published permanently, and deleting a file never removes it from history.

The owner decided five points in chat on 2026-10-22:

1. The assistant creates, updates and deletes widgets.
2. MagicPath holds the widget visual layer.
3. Each widget change lands as a commit on a dedicated branch,
   `cockpit/widgets`, in this repository and goes live in about a minute.
   People may commit there too, and the server picks their commits up.
4. The repository stays public, so widget code is data-free.
5. Widget changes from the owner's own turns ship without a per-change
   approval.

## Decisions

1. **Cockpit is a third client mode on the ADR-0092 shell.** `ClientMode`
   gains `cockpit` on iPhone, iPad and Mac, and every two-way mode assumption
   becomes an exhaustive switch. Selecting the mode confers no authority, and
   Memory, Persona, Schedules and Settings stay global. As with Email, a server
   without the routes shows Cockpit as unavailable rather than failing.
2. **Widgets are React and TypeScript on a MagicPath-designed kit.** A
   `cockpit/` TypeScript workspace on `main` holds four parts: the kit
   (`@veetbot/cockpit-kit`, ported from approved MagicPath designs and carrying
   A2 tokens from `AppTheme.swift`), the host page, the builder, and the
   schemas. A widget is a manifest, one component, and synthetic fixtures. It
   imports only React and the kit. Widgets ship without an App Store release.
   Native SwiftUI was rejected because Swift cannot be hot-deployed.
3. **Widget code executes only inside a network-less sandboxed realm.** The
   app's single `WKWebView` loads a trusted host page through a custom scheme
   handler. Inside it, one `sandbox="allow-scripts"` iframe with an opaque
   origin runs every widget behind per-widget error boundaries and a watchdog.
   The following controls hold:
   - The scheme handler serves only content-addressed assets the current
     snapshot names, and it never proxies the API.
   - A content rule list blocks every `http` and `https` load.
   - A document-start script removes the network primitives CSP does not
     govern.
   - The bearer token stays in native code. Native code fetches assets and
     data and pushes them in.

   The lint rules in the builder are hygiene. The boundary is the realm, and an
   adversarial widget corpus proves it in a real WebKit view.
4. **The repository is the source of truth for widget code, on an orphan
   branch.**
   - `cockpit/widgets` holds only `cockpit/widgets/<slug>/` and `main` never
     contains that path. Widgets always build against the kit deployed from
     `main`.
   - A durable outbox in the maintenance role commits each accepted revision
     through the GitHub Git Data API as a fast-forward compare-and-swap. It
     never force-pushes and never writes outside that path or that ref.
   - A revision goes live only after its commit is recorded. A GitHub outage
     delays changes but never breaks the display, because live bundles render
     from PostgreSQL.
   - The importer builds human commits on the branch and activates the valid
     ones; an invalid commit never replaces a live revision. When a person and
     the assistant change the same widget at once, the person's commit wins.
5. **Widgets are data-free because the branch is public.**
   - Widget code and its titles stay generic. Personal values arrive at
     runtime as private per-widget settings and feeds held in PostgreSQL:
     names, tickers, flight numbers, places and accounts.
   - Fixtures are synthetic, and commit messages follow a fixed template.
   - A mandatory privacy screen blocks a commit that contains any of: a
     credential-shaped string, an email address or phone number, a known
     contact or People name, or any current private settings value.
6. **The assistant reaches Cockpit through a new `cockpit` builtin domain.**
   - The domain registers behind `AGENT_COCKPIT_ENABLED` and is **deferred**
     under ADR-0123, because the definition roster already meets its
     thirty-item cap.
   - Its five tools: a read tool, a check tool that validates and previews
     in the builder sandbox, a write tool for create, update, delete and
     restore, a configure tool for private settings and layouts, and a feed
     publish tool.
   - Widget writes are classified `publication`, because they are pushes to a
     public repository. The default profile gains an exact-name `tool_rules`
     allow entry for the write tool, as `device.sms.send` and `browser.act`
     have.
   - The trust overlay still escalates an untrusted origin or untrusted
     arguments to an approval, and the credential-to-egress hardline rule
     still applies. Widget writes are offered only in interactive chat
     sessions, never in scheduled or delegated runs.
   - The profile edit moves the content-addressed policy version. That move
     is reviewed in the step that makes it.
7. **Builds run in a second sandbox image.** `execution/cockpit-builder` is
   built from the sandbox base with Node and the kit.
   - It runs through the existing execution service under gVisor, with
     networking off, a read-only root, and no credentials.
   - It has its own setting and manager and a server-side image allowlist.
   - Builds that start outside a run hold a reaper lease labelled with their
     purpose.
   - A release preflight builds a canary under runsc and rebuilds every live
     widget against a changed kit.

   This amends the single-image assumption recorded as an open question in
   [sandbox-isolation.md](../plan/sandbox-isolation.md).
8. **Feeds carry the data; widgets only render it.**
   - Platform feeds are projected in the maintenance role from existing
     services: approvals, priority email, schedule health, recent calls,
     recent People interactions and run activity.
   - Agent feeds are published by `cockpit.feed.publish`, usually from an
     approved schedule that keeps its existing approval and budget.
   - Each snapshot carries `observed_at` and an expected interval. Stale data
     renders as stale.
   - A widget receives only the feeds its manifest declares. Feed strings
     render as inert text.
   - Deterministic collectors that need no model run are deferred.
9. **Live delivery is a per-tenant stream.**
   - `GET /v1/cockpit` returns a snapshot.
   - `GET /v1/cockpit/stream` resumes from `Last-Event-ID` over a durable
     `cockpit_events` table. LISTEN/NOTIFY carries only kind and sequence. A
     resume point past retention returns `stream.reset`.
   - New scopes `cockpit.read` and `cockpit.write` gate every route.
   - The client streams only while the mode is visible, and it reconnects
     without the run stream's give-up limits.

## Scope admission and consequences

The owner authorized Milestone 35 as an independent parallel workstream on
2026-10-22. Its documentation-only authorization step adds the following:

- the engineering plan section and project state;
- the current milestone, the milestones page, the milestone map and
  readiness;
- AGENTS routing;
- contract pointers in the composition, HTTP, client-mode, sandbox, builtin
  tool and policy specifications;
- the executable gate registry, which admits a new `gate.cockpit.*` area of
  twenty-three gates.

Registration is not passing evidence, and the verified ceiling does not move.

The design admits:

- the `cockpit/` npm workspace, with React, the TypeScript compiler, esbuild
  and Playwright WebKit as development and builder-image dependencies (none
  enters the Python runtime);
- one builder image;
- PostgreSQL tables for widgets, revisions, private settings, feeds and their
  history, layouts, events, the commit outbox and import failures;
- one GitHub App credential, delivered only to the maintenance role;
- five deferred tools;
- the cockpit routes and two scopes;
- one environment flag;
- the native Cockpit mode.

The owner performs three steps at activation:

1. Create the GitHub App and its repository rulesets, so that the App can
   write only `cockpit/widgets` and nobody can force-push or delete it.
2. Install the credential.
3. Set the flag.

Roadmap items are unchanged. Widgets never enter the assistant's instructions
or context, so B1's self-authored skill activation is not amended. B11's
visual workflow builder stays deferred, and no standing approval grant (B8) is
introduced: the write tool's allow rule is a classification decision with the
trust overlay intact.

## Alternatives considered

- **Native SwiftUI widgets:** native feel, but they cannot be deployed without
  an app release, and the assistant could only fill in templates, never write
  a widget.
- **A declarative JSON widget format rendered natively:** safe and native, but
  it is configuration rather than code. New primitive kinds would need app
  releases, and the MagicPath designs would not carry over one to one.
- **Widgets on `dev`:** one tree, but every widget tweak would be a commit
  that parallel sessions rebase over, and it would ride into every reviewed
  `dev`→`main` promotion.
- **A live store mirrored to git as a record:** fast, but editing the code in
  git would not change the live widget, which defeats having it in the
  repository.
- **A private repository, or a separate private widgets repository:** better
  privacy, but the owner chose to keep this repository public and the widget
  code in it.
- **One iframe per widget:** stronger isolation between widgets, but
  same-process iframes share one main thread, so a hung widget still blocks the
  others. It would also cost a React copy per tile. The boundary that matters
  is between widget code and the network or native code.
- **An approval for every widget change:** safest, but the owner rejected it.
  The realm, the privacy screen, one-tap revert and the trust overlay carry
  that risk instead.

## Acceptance status

The owner approved the plan in chat on 2026-10-22. Implementation proceeds
through the design's build sequence under the repository's red-green rule,
beginning with the MagicPath designs and the `cockpit/` workspace. Pull request
creation, merge, the GitHub App and rulesets, and production activation keep
their explicit boundaries.
